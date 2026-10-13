"""Bounded, repository-scoped content search.

Anonymous GitHub code search is not available, so this module implements a fallback:
1. read the repository tree once (one API request),
2. rank candidate files by path relevance (docs, configuration and source first),
3. fetch at most `max_files` files (raw CDN when anonymous, so the REST quota is spared),
4. scan them for the query, within byte, file-count and wall-clock limits.

Results always state whether the search was complete; it never claims exhaustiveness
when limits were hit.
"""

from __future__ import annotations

import asyncio
import posixpath
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from app.integrations.github.client import (
    WEB_ORIGIN,
    GitHubClient,
    GitHubError,
    InvalidInputError,
    RateLimitedError,
    validate_path,
    validate_ref,
)

DOC_EXTENSIONS = {".md", ".mdx", ".rst", ".txt", ".adoc"}
CONFIG_EXTENSIONS = {".toml", ".yaml", ".yml", ".json", ".ini", ".cfg", ".conf", ".properties", ".xml", ".gradle",
                     ".env", ".example", ".tf", ".hcl"}
SOURCE_EXTENSIONS = {".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".go", ".java", ".kt", ".kts", ".rb",
                     ".rs", ".php", ".cs", ".c", ".h", ".cc", ".cpp", ".hpp", ".swift", ".scala", ".sh", ".bash",
                     ".sql", ".html", ".css", ".scss", ".vue", ".svelte", ".proto", ".graphql", ".lua", ".ex", ".exs"}
SPECIAL_FILES = {"dockerfile", "makefile", "procfile", "justfile", "gemfile", "pipfile", "jenkinsfile",
                 "requirements.txt", ".env.example", "codeowners"}
SKIP_DIRS = {"node_modules", "vendor", "dist", "build", ".git", "__pycache__", ".venv", "venv", "site-packages",
             "third_party", "third-party", ".next", "coverage", ".tox", "target", ".mypy_cache", ".pytest_cache"}
SKIP_FILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "uv.lock", "cargo.lock",
              "composer.lock", "gemfile.lock", "go.sum"}
SKIP_SUFFIXES = (".min.js", ".min.css", ".map", ".svg", ".lock", ".snap")
# Translated documentation trees (e.g. docs/de/...) are deprioritised in favour of English sources.
_TRANSLATED_DOCS = re.compile(r"(^|/)docs?/(?!en/)[a-z]{2}(-[a-z]{2,4})?/", re.IGNORECASE)
_WORD = re.compile(r"[A-Za-z0-9_.$@-]{2,}")
GENERIC_TERMS = {
    "a", "an", "and", "are", "by", "class", "code", "def", "defined", "definition", "do", "does", "file", "files",
    "find", "for", "from", "func", "function", "how", "implement", "implementation", "implemented", "implements",
    "in", "is", "it", "method", "module", "of", "on", "or", "show", "that", "the", "this", "to", "usage", "use",
    "used", "what", "where", "which", "why", "with",
}
MAX_TOTAL_BYTES = 12_000_000
MAX_RESULT_FILES = 25
MAX_LINES_PER_FILE = 6
CONCURRENCY = 8


@dataclass
class SearchLimits:
    max_files: int = 100
    max_file_bytes: int = 200_000
    timeout_seconds: float = 25.0
    max_total_bytes: int = MAX_TOTAL_BYTES


@dataclass
class _FileResult:
    path: str
    score: float
    matches: list[tuple[int, str]] = field(default_factory=list)
    match_count: int = 0
    best_line: int = 0


def classify(path: str) -> str | None:
    """Return 'docs', 'config', 'source' or None (not searchable)."""
    lower = path.lower()
    parts = lower.split("/")
    if any(part in SKIP_DIRS for part in parts[:-1]):
        return None
    name = parts[-1]
    if name in SKIP_FILES or lower.endswith(SKIP_SUFFIXES):
        return None
    if name in SPECIAL_FILES or name.startswith("dockerfile") or name.startswith("docker-compose"):
        return "config"
    if name.startswith("readme"):
        return "docs"
    ext = posixpath.splitext(name)[1]
    if ext in DOC_EXTENSIONS:
        return "docs"
    if ext in CONFIG_EXTENSIONS:
        return "config"
    if ext in SOURCE_EXTENSIONS:
        return "source"
    return None


def query_terms(query: str) -> list[str]:
    terms: list[str] = []
    for word in _WORD.findall(query.lower()):
        word = word.strip(".-")
        if len(word) >= 2 and word not in terms:
            terms.append(word)
    # Words like "class" or "where" match almost every file; drop them when something more specific remains.
    specific = [t for t in terms if t not in GENERIC_TERMS]
    return (specific or terms)[:8]


_SPLIT = re.compile(r"[_\-./:$@]+|(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def sub_terms(query: str) -> list[str]:
    """Split identifiers into parts (`OAuth2PasswordBearer` -> oauth2, password, bearer)."""
    parts: list[str] = []
    for word in _WORD.findall(query):
        for part in _SPLIT.split(word):
            part = part.lower()
            if len(part) >= 3 and part not in parts:
                parts.append(part)
    return parts[:12]


def score_path(path: str, kind: str, terms: list[str], parts: list[str] | None = None) -> float:
    lower = path.lower()
    name = lower.rsplit("/", 1)[-1]
    score = {"docs": 12.0, "config": 10.0, "source": 6.0}[kind]
    if name.startswith("readme"):
        score += 8
    for term in terms:
        if term in name:
            score += 30
        elif term in lower:
            score += 15
    for part in parts or []:
        if part in terms:
            continue
        if part in name:
            score += 10
        elif part in lower:
            score += 4
    depth = lower.count("/")
    score -= min(depth, 6)
    if "test" in lower:
        score -= 3
    if _TRANSLATED_DOCS.search(lower):
        score -= 25
    return score


def scan_text(text: str, query: str, terms: list[str]) -> tuple[float, int, list[tuple[int, str]], int]:
    """Return (score, match_count, sample lines, best line number) for one file."""
    # Match the literal query and also the query without generic words ("OAuth2PasswordBearer class").
    phrases = list(dict.fromkeys(p for p in (query.lower().strip(), " ".join(terms)) if p))
    identifier = next((p for p in phrases if re.fullmatch(r"[a-z0-9_$]+", p)), None)
    definition = (
        re.compile(rf"\b(class|def|func|function|interface|type|struct|enum|const|let|var)\s+{re.escape(identifier)}\b")
        if identifier else None
    )
    scored: list[tuple[float, int, str]] = []
    match_count = 0
    defines = False
    terms_seen: set[str] = set()
    for number, line in enumerate(text.splitlines(), start=1):
        lower = line.lower()
        if any(p in lower for p in phrases):
            is_definition = definition is not None and definition.search(lower) is not None
            defines = defines or is_definition
            line_score = 25.0 if is_definition else 10.0
        else:
            hits = [t for t in terms if t in lower]
            if not hits:
                continue
            terms_seen.update(hits)
            if len(terms) > 1 and len(hits) < max(1, len(terms) // 2):
                continue
            line_score = 2.0 * len(hits)
        match_count += 1
        terms_seen.update(t for t in terms if t in lower)
        scored.append((line_score, number, line.strip()[:240]))
    if not scored:
        return 0.0, 0, [], 0
    total = sum(s for s, _, _ in scored[:50]) + 3.0 * len(terms_seen)
    if defines:
        total += 60.0  # "where is X defined" is the most common intent: the defining file wins
    best = sorted(scored, key=lambda item: (-item[0], item[1]))[:MAX_LINES_PER_FILE]
    return total, match_count, sorted(((n, t) for _, n, t in best), key=lambda item: item[0]), best[0][1]


async def search_repository_content(
    client: GitHubClient,
    repo: str,
    query: str,
    *,
    ref: str | None = None,
    path_prefix: str | None = None,
    limits: SearchLimits | None = None,
    max_files: int | None = None,
) -> dict[str, Any]:
    limits = limits or SearchLimits()
    repo = client.resolve_repo(repo)
    if not isinstance(query, str) or not query.strip():
        raise InvalidInputError("A non-empty search query is required.")
    query = query.strip()[:200]
    terms = query_terms(query)
    if not terms:
        raise InvalidInputError("The search query must contain at least one word of two or more characters.")
    parts = sub_terms(query)
    prefix = validate_path(path_prefix)
    ref = validate_ref(ref)
    file_budget = max(1, min(max_files or limits.max_files, limits.max_files))

    started = time.monotonic()
    deadline = started + limits.timeout_seconds
    partial_reasons: list[str] = []

    resolved_ref, entries, tree_truncated = await client.tree(repo, ref)
    hinted: set[str] = set()
    if client.authenticated:
        # GitHub code search needs authentication; use it only to prioritise candidates.
        try:
            hinted = set(await client.code_search_paths(repo, query))
        except GitHubError:
            hinted = set()
    if tree_truncated:
        partial_reasons.append("GitHub truncated the repository tree (very large repository); some files were not visible.")

    candidates: list[tuple[float, str, int]] = []
    skipped_large = 0
    path_matches: list[str] = []
    for entry in entries:
        path = str(entry.get("path") or "")
        if entry.get("type") != "file" or (prefix and not (path == prefix or path.startswith(prefix + "/"))):
            continue
        kind = classify(path)
        if kind is None:
            continue
        name = path.lower().rsplit("/", 1)[-1]
        if any(t in name for t in terms):
            path_matches.append(path)
        size = int(entry.get("size") or 0)
        if size > limits.max_file_bytes:
            skipped_large += 1
            continue
        score = score_path(path, kind, terms, parts) + (100.0 if path in hinted else 0.0)
        candidates.append((score, path, size))

    candidates.sort(key=lambda item: (-item[0], item[1]))
    selected: list[tuple[float, str, int]] = []
    budget_bytes = 0
    for candidate in candidates:
        if len(selected) >= file_budget:
            break
        if budget_bytes + candidate[2] > limits.max_total_bytes:
            continue
        selected.append(candidate)
        budget_bytes += candidate[2]
    if len(candidates) > len(selected):
        partial_reasons.append(
            f"Searched the {len(selected)} highest-priority of {len(candidates)} candidate files "
            f"(limit GITHUB_MAX_FILES_PER_SEARCH={file_budget})."
        )
    if skipped_large:
        partial_reasons.append(f"{skipped_large} files larger than {limits.max_file_bytes} bytes were skipped.")

    semaphore = asyncio.Semaphore(CONCURRENCY)
    results: list[_FileResult] = []
    searched = 0
    bytes_searched = 0
    rate_limited: RateLimitedError | None = None
    errors = 0

    async def scan(path: str, path_score: float) -> None:
        nonlocal searched, bytes_searched, rate_limited, errors
        if rate_limited is not None:
            return
        async with semaphore:
            if rate_limited is not None:
                return
            try:
                response = await client.fetch_file_bytes(repo, path, resolved_ref, max_bytes=limits.max_file_bytes)
            except RateLimitedError as exc:
                rate_limited = exc
                return
            except GitHubError:
                errors += 1
                return
        searched += 1
        bytes_searched += len(response.content)
        if b"\x00" in response.content[:8192]:
            return
        score, count, lines, best_line = scan_text(response.content.decode("utf-8", "replace"), query, terms)
        if count:
            if "test" in path.lower():
                score *= 0.6
            if _TRANSLATED_DOCS.search(path.lower()):
                score *= 0.3
            results.append(_FileResult(path, score + path_score / 10, lines, count, best_line))

    tasks = [asyncio.create_task(scan(path, score)) for score, path, _ in selected]
    if tasks:
        done, pending = await asyncio.wait(tasks, timeout=max(0.1, deadline - time.monotonic()))
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
            partial_reasons.append(
                f"Search time limit ({limits.timeout_seconds:.0f}s) reached; {len(pending)} files were not scanned."
            )
    if rate_limited is not None:
        partial_reasons.append(f"GitHub rate limit reached during the search: {rate_limited.message}")
    if errors:
        partial_reasons.append(f"{errors} files could not be read.")

    results.sort(key=lambda r: (-r.score, r.path))
    ref_segment = quote(resolved_ref, safe="/")
    return {
        "repository": repo,
        "ref": resolved_ref,
        "query": query,
        "path_prefix": prefix or "/",
        "results": [
            {
                "path": r.path,
                # Anchored at the best-scoring line; link other lines as <file URL>#L<line>.
                "html_url": f"{WEB_ORIGIN}/{repo}/blob/{ref_segment}/{quote(r.path, safe='/')}"
                + (f"#L{r.best_line}" if r.best_line else ""),
                "match_count": r.match_count,
                "matches": [{"line": n, "text": t} for n, t in r.matches],
            }
            for r in results[:MAX_RESULT_FILES]
        ],
        "path_matches": path_matches[:15],
        "stats": {
            "candidate_files": len(candidates),
            "files_searched": searched,
            "bytes_searched": bytes_searched,
            "files_with_matches": len(results),
            "elapsed_seconds": round(time.monotonic() - started, 2),
        },
        "complete": not partial_reasons,
        "partial_reasons": partial_reasons,
        "note": (
            "Search was exhaustive over all searchable text files." if not partial_reasons
            else "PARTIAL RESULTS: the search did not cover the whole repository; absence of a match is not proof "
                 "that the text does not exist."
        ),
    }
