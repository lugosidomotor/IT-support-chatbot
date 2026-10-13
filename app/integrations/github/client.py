"""Read-only GitHub REST client restricted to an explicit repository allowlist.

Security properties:
* Only fixed, trusted origins are contacted (api.github.com, raw.githubusercontent.com).
* Only repositories listed in GITHUB_REPOS can be addressed; identifiers, paths and refs
  are validated before being embedded in a URL.
* Only GET requests are issued. Redirects are followed manually and only to trusted hosts.
* Every request has a timeout and a bounded response size; retries are bounded.
* The optional token is attached as a request header only and never returned or logged.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urljoin, urlparse

import httpx

from app.config import ConfigError, is_safe_ref, validate_repo_identifier

logger = logging.getLogger(__name__)

API_ORIGIN = "https://api.github.com"
RAW_ORIGIN = "https://raw.githubusercontent.com"
WEB_ORIGIN = "https://github.com"
TRUSTED_HOSTS = frozenset({"api.github.com", "raw.githubusercontent.com"})

DEFAULT_JSON_MAX_BYTES = 4_000_000
TREE_MAX_BYTES = 16_000_000
MAX_REDIRECTS = 3
MAX_ATTEMPTS = 3
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


# --------------------------------------------------------------------------- errors


class GitHubError(Exception):
    code = "github_error"

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message}


class RepoNotAllowedError(GitHubError):
    code = "repo_not_allowed"


class InvalidInputError(GitHubError):
    code = "invalid_input"


class NotFoundError(GitHubError):
    code = "not_found"


class AuthFailedError(GitHubError):
    code = "auth_failed"


class ForbiddenError(GitHubError):
    code = "forbidden"


class UpstreamError(GitHubError):
    code = "upstream_unavailable"


class RateLimitedError(GitHubError):
    code = "rate_limited"

    def __init__(self, message: str, *, reset_at: float | None = None, status: int | None = None) -> None:
        super().__init__(message, status=status)
        self.reset_at = reset_at

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        if self.reset_at:
            data["retry_after_seconds"] = max(0, int(self.reset_at - time.time()))
        data["incomplete"] = True
        return data


# --------------------------------------------------------------------------- helpers


@dataclass
class RawResponse:
    status: int
    headers: httpx.Headers
    content: bytes
    truncated: bool
    url: str


@dataclass
class RateLimitInfo:
    limit: int | None = None
    remaining: int | None = None
    reset_at: float | None = None
    blocked_until: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "limit": self.limit,
            "remaining": self.remaining,
            "reset_in_seconds": max(0, int(self.reset_at - time.time())) if self.reset_at else None,
        }


class TTLCache:
    """Small LRU cache with per-entry expiry; process-local and short-lived by design."""

    def __init__(self, ttl_seconds: float, max_entries: int = 512) -> None:
        self.ttl = ttl_seconds
        self.max_entries = max_entries
        self._data: OrderedDict[Any, tuple[float, Any]] = OrderedDict()

    def get(self, key: Any) -> Any | None:
        if self.ttl <= 0:
            return None
        item = self._data.get(key)
        if item is None:
            return None
        expires, value = item
        if expires < time.monotonic():
            self._data.pop(key, None)
            return None
        self._data.move_to_end(key)
        return value

    def set(self, key: Any, value: Any) -> None:
        if self.ttl <= 0:
            return
        self._data[key] = (time.monotonic() + self.ttl, value)
        self._data.move_to_end(key)
        while len(self._data) > self.max_entries:
            self._data.popitem(last=False)


def validate_path(path: str | None) -> str:
    """Normalise a repository-relative path, rejecting traversal and URL tricks."""
    if path is None:
        return ""
    if not isinstance(path, str):
        raise InvalidInputError("Path must be a string.")
    cleaned = path.strip().strip("/")
    if len(cleaned) > 1024:
        raise InvalidInputError("Path is too long.")
    if _CONTROL_CHARS.search(cleaned) or "\\" in cleaned or "://" in cleaned or "?" in cleaned or "#" in cleaned:
        raise InvalidInputError(f"Invalid repository path '{path}'.")
    segments = cleaned.split("/") if cleaned else []
    if any(seg in {"", ".", ".."} for seg in segments):
        raise InvalidInputError(f"Invalid repository path '{path}': path traversal is not allowed.")
    return cleaned


def validate_ref(ref: str | None) -> str | None:
    if ref is None or ref == "":
        return None
    if not isinstance(ref, str) or not is_safe_ref(ref.strip()):
        raise InvalidInputError(f"Invalid git ref '{ref}'.")
    return ref.strip()


def _int_header(headers: httpx.Headers, name: str) -> int | None:
    try:
        return int(headers[name])
    except (KeyError, ValueError):
        return None


def _truncate(text: str | None, limit: int) -> str | None:
    if text is None:
        return None
    return text if len(text) <= limit else text[:limit] + f"\n…[truncated, {len(text) - limit} more characters]"


def _user(obj: dict[str, Any] | None) -> str | None:
    return obj.get("login") if isinstance(obj, dict) else None


# --------------------------------------------------------------------------- client


class GitHubClient:
    def __init__(
        self,
        repos: list[str],
        token: str | None = None,
        *,
        default_ref: str | None = None,
        timeout_seconds: float = 15.0,
        cache_ttl_seconds: float = 300.0,
        max_file_bytes: int = 200_000,
        max_retry_wait_seconds: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._allowed: dict[str, str] = {}
        for repo in repos:
            canonical = validate_repo_identifier(repo)
            self._allowed[canonical.lower()] = canonical
        self._token = token.strip() if token and token.strip() else None
        self.default_ref = default_ref
        self.max_file_bytes = max_file_bytes
        self.max_retry_wait = max_retry_wait_seconds
        self._cache = TTLCache(cache_ttl_seconds)
        self._inflight: dict[Any, asyncio.Future[RawResponse]] = {}
        self.rate_limit = RateLimitInfo()
        self.last_error: GitHubError | None = None
        self.last_success_at: float | None = None
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds, connect=min(10.0, timeout_seconds)),
            follow_redirects=False,
            transport=transport,
            headers={
                "User-Agent": "developer-support-agent/0.1",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    # ------------------------------------------------------------------ public helpers

    @property
    def authenticated(self) -> bool:
        return self._token is not None

    @property
    def repositories(self) -> list[str]:
        return list(self._allowed.values())

    def resolve_repo(self, repo: str) -> str:
        """Return the canonical allowlisted `owner/repo`, or raise RepoNotAllowedError."""
        if not isinstance(repo, str) or not repo.strip():
            raise InvalidInputError("A repository in the form 'owner/repo' is required.")
        try:
            candidate = validate_repo_identifier(repo)
        except ConfigError as exc:
            raise InvalidInputError(str(exc)) from None
        canonical = self._allowed.get(candidate.lower())
        if canonical is None:
            raise RepoNotAllowedError(
                f"Repository '{candidate}' is not in the configured allowlist. "
                f"Allowed repositories: {', '.join(self.repositories) or '(none)'}."
            )
        return canonical

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ transport

    def _headers_for(self, host: str, accept: str | None) -> dict[str, str]:
        headers = {"Accept": accept or "application/vnd.github+json"}
        if self._token and host in TRUSTED_HOSTS:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _update_rate_limit(self, response: httpx.Response) -> None:
        if urlparse(str(response.url)).hostname != "api.github.com":
            return
        limit = _int_header(response.headers, "x-ratelimit-limit")
        remaining = _int_header(response.headers, "x-ratelimit-remaining")
        reset = _int_header(response.headers, "x-ratelimit-reset")
        if limit is not None:
            self.rate_limit.limit = limit
        if remaining is not None:
            self.rate_limit.remaining = remaining
        if reset is not None:
            self.rate_limit.reset_at = float(reset)

    async def _fetch_once(self, url: str, params: dict[str, Any] | None, accept: str | None, max_bytes: int) -> RawResponse:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            parsed = urlparse(current)
            if parsed.scheme != "https" or parsed.hostname not in TRUSTED_HOSTS:
                raise UpstreamError("GitHub redirected to an untrusted host; request aborted.")
            async with self._http.stream(
                "GET", current, params=params, headers=self._headers_for(parsed.hostname, accept)
            ) as response:
                self._update_rate_limit(response)
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if not location:
                        raise UpstreamError("GitHub returned a redirect without a location.")
                    current = urljoin(current, location)
                    params = None  # the redirect target already carries the query string
                    continue
                chunks: list[bytes] = []
                size = 0
                truncated = False
                async for chunk in response.aiter_bytes():
                    remaining = max_bytes - size
                    if len(chunk) > remaining:
                        chunks.append(chunk[:remaining])
                        size = max_bytes
                        truncated = True
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                return RawResponse(response.status_code, response.headers, b"".join(chunks), truncated, current)
        raise UpstreamError("Too many redirects from GitHub.")

    def _rate_limit_wait(self, response: RawResponse) -> float | None:
        """Seconds to wait if this response is a rate-limit response, else None."""
        if response.status not in (403, 429):
            return None
        body = response.content[:2000].decode("utf-8", "replace").lower()
        retry_after = _int_header(response.headers, "retry-after")
        remaining = _int_header(response.headers, "x-ratelimit-remaining")
        reset = _int_header(response.headers, "x-ratelimit-reset")
        is_rate_limit = response.status == 429 or remaining == 0 or "rate limit" in body or retry_after is not None
        if not is_rate_limit:
            return None
        if retry_after is not None:
            return float(retry_after)
        if remaining == 0 and reset is not None:
            return max(0.0, reset - time.time()) + 1.0
        return 60.0  # secondary rate limit without guidance: GitHub recommends waiting at least a minute

    async def _get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        accept: str | None = None,
        max_bytes: int = DEFAULT_JSON_MAX_BYTES,
        not_found_message: str = "Not found.",
    ) -> RawResponse:
        key = (url, tuple(sorted((params or {}).items())), accept, max_bytes)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        existing = self._inflight.get(key)
        if existing is not None:
            return await asyncio.shield(existing)

        future: asyncio.Future[RawResponse] = asyncio.get_running_loop().create_future()
        self._inflight[key] = future
        try:
            result = await self._get_uncached(url, params, accept, max_bytes, not_found_message)
        except BaseException as exc:
            if not future.done():
                future.set_exception(exc)
                future.exception()  # mark retrieved to avoid "never retrieved" warnings
            if isinstance(exc, GitHubError):
                self.last_error = exc
            raise
        else:
            future.set_result(result)
            self._cache.set(key, result)
            self.last_success_at = time.time()
            self.last_error = None
            return result
        finally:
            self._inflight.pop(key, None)

    async def _get_uncached(
        self, url: str, params: dict[str, Any] | None, accept: str | None, max_bytes: int, not_found_message: str
    ) -> RawResponse:
        host = urlparse(url).hostname
        if host == "api.github.com" and self.rate_limit.blocked_until > time.time():
            raise RateLimitedError(
                "GitHub API rate limit exceeded; results are incomplete until the limit resets."
                + ("" if self.authenticated else " Configure GITHUB_TOKEN to raise the limit."),
                reset_at=self.rate_limit.blocked_until,
            )

        last_exc: GitHubError | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = await self._fetch_once(url, params, accept, max_bytes)
            except httpx.TimeoutException:
                last_exc = UpstreamError("GitHub request timed out.")
            except httpx.TransportError as exc:
                last_exc = UpstreamError(f"Could not reach GitHub ({type(exc).__name__}).")
            else:
                if response.status == 200:
                    return response
                wait = self._rate_limit_wait(response)
                if wait is not None:
                    if wait <= self.max_retry_wait and attempt < MAX_ATTEMPTS:
                        logger.info("github rate limited; retrying", extra={"wait_seconds": round(wait, 1)})
                        await asyncio.sleep(wait)
                        continue
                    reset_at = time.time() + wait
                    if host == "api.github.com":
                        self.rate_limit.blocked_until = reset_at
                    raise RateLimitedError(
                        "GitHub rate limit exceeded; results are incomplete."
                        + ("" if self.authenticated else " Anonymous access is limited to 60 API requests/hour; "
                           "configure GITHUB_TOKEN to raise the limit."),
                        reset_at=reset_at,
                        status=response.status,
                    )
                if response.status == 401:
                    raise AuthFailedError("GitHub rejected the configured GITHUB_TOKEN (HTTP 401).", status=401)
                if response.status == 403:
                    raise ForbiddenError("GitHub denied access to this resource (HTTP 403).", status=403)
                if response.status == 404:
                    raise NotFoundError(not_found_message, status=404)
                if response.status == 409:
                    raise NotFoundError("The repository is empty or the reference does not exist.", status=409)
                if response.status == 422:
                    raise InvalidInputError("GitHub rejected the request parameters (HTTP 422).", status=422)
                if response.status >= 500:
                    last_exc = UpstreamError(f"GitHub returned HTTP {response.status}.", status=response.status)
                else:
                    raise UpstreamError(f"Unexpected GitHub response (HTTP {response.status}).", status=response.status)
            if attempt < MAX_ATTEMPTS:
                await asyncio.sleep(0.5 * attempt)
        assert last_exc is not None
        raise last_exc

    async def _get_json(self, path: str, *, params: dict[str, Any] | None = None, max_bytes: int = DEFAULT_JSON_MAX_BYTES,
                        not_found_message: str = "Not found.") -> Any:
        response = await self._get(f"{API_ORIGIN}{path}", params=params, max_bytes=max_bytes,
                                   not_found_message=not_found_message)
        if response.truncated:
            raise UpstreamError("GitHub response exceeded the configured size limit.")
        try:
            return json.loads(response.content)
        except ValueError:
            raise UpstreamError("GitHub returned an invalid JSON response.") from None

    # ------------------------------------------------------------------ repository-level reads

    async def repository_info(self, repo: str) -> dict[str, Any]:
        repo = self.resolve_repo(repo)
        data = await self._get_json(f"/repos/{repo}", not_found_message=f"Repository '{repo}' was not found or is not accessible.")
        return {
            "repository": data.get("full_name", repo),
            "description": data.get("description"),
            "html_url": data.get("html_url", f"{WEB_ORIGIN}/{repo}"),
            "default_branch": data.get("default_branch"),
            "visibility": data.get("visibility") or ("private" if data.get("private") else "public"),
            "archived": data.get("archived"),
            "language": data.get("language"),
            "topics": data.get("topics", []),
            "stars": data.get("stargazers_count"),
            "forks": data.get("forks_count"),
            "open_issues_and_prs": data.get("open_issues_count"),
            "license": (data.get("license") or {}).get("spdx_id"),
            "homepage": data.get("homepage"),
            "pushed_at": data.get("pushed_at"),
            "size_kb": data.get("size"),
        }

    async def default_branch(self, repo: str) -> str:
        if self.default_ref:
            return self.default_ref
        info = await self.repository_info(repo)
        return info.get("default_branch") or "HEAD"

    async def readme(self, repo: str, ref: str | None = None) -> dict[str, Any]:
        repo = self.resolve_repo(repo)
        ref = validate_ref(ref) or self.default_ref
        params = {"ref": ref} if ref else None
        meta = await self._get_json(f"/repos/{repo}/readme", params=params,
                                    not_found_message=f"No README found in '{repo}'.")
        text, truncated, binary = self._decode_contents(meta)
        return {
            "repository": repo,
            "path": meta.get("path"),
            "html_url": meta.get("html_url"),
            "truncated": truncated,
            "content": None if binary else text,
        }

    def _decode_contents(self, meta: dict[str, Any]) -> tuple[str, bool, bool]:
        raw = base64.b64decode(meta.get("content") or "") if meta.get("encoding") == "base64" else b""
        truncated = len(raw) > self.max_file_bytes
        raw = raw[: self.max_file_bytes]
        if b"\x00" in raw[:8192]:
            return "", truncated, True
        return raw.decode("utf-8", "replace"), truncated, False

    async def fetch_file_bytes(self, repo: str, path: str, ref: str | None, max_bytes: int | None = None) -> RawResponse:
        """Fetch raw file bytes.

        Anonymous mode reads from raw.githubusercontent.com, which does not consume the
        60 requests/hour REST API budget. Authenticated mode uses the contents API so
        private repositories and token permissions are respected.
        """
        repo = self.resolve_repo(repo)
        path = validate_path(path)
        if not path:
            raise InvalidInputError("A file path is required.")
        ref = validate_ref(ref)
        limit = max_bytes or self.max_file_bytes
        encoded_path = quote(path, safe="/")
        missing = f"File '{path}' was not found in '{repo}'" + (f" at ref '{ref}'." if ref else ".") + \
            " If it is a directory, use list_directory."
        if not self._token:
            url = f"{RAW_ORIGIN}/{repo}/{quote(ref or self.default_ref or 'HEAD', safe='/')}/{encoded_path}"
            return await self._get(url, accept="*/*", max_bytes=limit, not_found_message=missing)
        params = {"ref": ref} if ref else None
        response = await self._get(
            f"{API_ORIGIN}/repos/{repo}/contents/{encoded_path}",
            params=params,
            accept="application/vnd.github.raw+json",
            max_bytes=limit,
            not_found_message=missing,
        )
        ctype = response.headers.get("content-type", "")
        if ctype.startswith("application/json") and response.content[:1] == b"[":
            raise InvalidInputError(f"'{path}' is a directory; use list_directory instead.")
        return response

    async def file_contents(
        self, repo: str, path: str, ref: str | None = None, start_line: int | None = None, end_line: int | None = None
    ) -> dict[str, Any]:
        repo = self.resolve_repo(repo)
        response = await self.fetch_file_bytes(repo, path, ref)
        path = validate_path(path)
        ref_label = validate_ref(ref) or self.default_ref or "HEAD"
        html_url = f"{WEB_ORIGIN}/{repo}/blob/{quote(ref_label, safe='/')}/{quote(path, safe='/')}"
        if b"\x00" in response.content[:8192]:
            return {"repository": repo, "path": path, "ref": ref_label, "html_url": html_url, "binary": True,
                    "content": None, "note": "Binary file; content not shown."}
        text = response.content.decode("utf-8", "replace")
        lines = text.splitlines()
        total = len(lines)
        start = max(1, start_line or 1)
        end = min(total, end_line or total)
        if start > end and total:
            raise InvalidInputError(f"Requested line range {start}-{end} is outside the file (1-{total}).")
        selected = lines[start - 1 : end]
        numbered = "\n".join(f"{i:>5}| {line}" for i, line in enumerate(selected, start=start))
        result: dict[str, Any] = {
            "repository": repo,
            "path": path,
            "ref": ref_label,
            "html_url": html_url,
            "lines": f"{start}-{end}" if total else "0",
            "total_lines": total,
            "truncated": response.truncated,
            "content": numbered,
        }
        if response.truncated:
            result["note"] = f"File exceeds {self.max_file_bytes} bytes; only the beginning is shown."
        return result

    async def list_directory(self, repo: str, path: str = "", ref: str | None = None) -> dict[str, Any]:
        repo = self.resolve_repo(repo)
        path = validate_path(path)
        ref = validate_ref(ref) or self.default_ref
        params = {"ref": ref} if ref else None
        data = await self._get_json(
            f"/repos/{repo}/contents/{quote(path, safe='/')}" if path else f"/repos/{repo}/contents",
            params=params,
            not_found_message=f"Directory '{path or '/'}' was not found in '{repo}'.",
        )
        if isinstance(data, dict):
            raise InvalidInputError(f"'{path}' is a file; use get_file_contents instead.")
        entries = sorted(
            ({"name": e.get("name"), "path": e.get("path"), "type": e.get("type"), "size": e.get("size")} for e in data),
            key=lambda e: (e["type"] != "dir", str(e["name"]).lower()),
        )
        ref_label = ref or "HEAD"
        return {
            "repository": repo,
            "path": path or "/",
            "html_url": f"{WEB_ORIGIN}/{repo}/tree/{quote(ref_label, safe='/')}/{quote(path, safe='/')}".rstrip("/"),
            "entries": entries[:500],
            "entry_count": len(entries),
            "truncated": len(entries) > 500,
        }

    async def tree(self, repo: str, ref: str | None = None) -> tuple[str, list[dict[str, Any]], bool]:
        """Return (ref, entries, truncated_by_github) for the recursive tree of a ref."""
        repo = self.resolve_repo(repo)
        ref = validate_ref(ref) or await self.default_branch(repo)
        data = await self._get_json(
            f"/repos/{repo}/git/trees/{quote(ref, safe='/')}",
            params={"recursive": "1"},
            max_bytes=TREE_MAX_BYTES,
            not_found_message=f"Ref '{ref}' was not found in '{repo}'.",
        )
        entries = [
            {"path": e.get("path"), "type": "dir" if e.get("type") == "tree" else "file", "size": e.get("size")}
            for e in data.get("tree", [])
            if e.get("type") in ("blob", "tree")
        ]
        return ref, entries, bool(data.get("truncated"))

    async def repository_tree(
        self, repo: str, ref: str | None = None, path_prefix: str | None = None, max_entries: int = 400
    ) -> dict[str, Any]:
        repo = self.resolve_repo(repo)
        prefix = validate_path(path_prefix)
        resolved_ref, entries, github_truncated = await self.tree(repo, ref)
        if prefix:
            entries = [e for e in entries if e["path"] == prefix or str(e["path"]).startswith(prefix + "/")]
        max_entries = max(1, min(int(max_entries), 2000))
        return {
            "repository": repo,
            "ref": resolved_ref,
            "path_prefix": prefix or "/",
            "html_url": f"{WEB_ORIGIN}/{repo}/tree/{quote(resolved_ref, safe='/')}/{quote(prefix, safe='/')}".rstrip("/"),
            "entries": entries[:max_entries],
            "matching_entries": len(entries),
            "complete": not github_truncated and len(entries) <= max_entries,
            "note": (
                "GitHub truncated the tree for this large repository; listing is partial." if github_truncated
                else (f"Showing first {max_entries} of {len(entries)} entries; narrow with path_prefix."
                      if len(entries) > max_entries else None)
            ),
        }

    # ------------------------------------------------------------------ pull requests / issues / commits

    async def list_pull_requests(self, repo: str, state: str = "open", per_page: int = 20, page: int = 1,
                                 base: str | None = None) -> dict[str, Any]:
        repo = self.resolve_repo(repo)
        if state not in {"open", "closed", "all"}:
            raise InvalidInputError("state must be one of: open, closed, all.")
        params: dict[str, Any] = {"state": state, "per_page": max(1, min(per_page, 50)), "page": max(1, min(page, 50)),
                                  "sort": "updated", "direction": "desc"}
        if base:
            params["base"] = validate_ref(base)
        data = await self._get_json(f"/repos/{repo}/pulls", params=params)
        return {
            "repository": repo,
            "state": state,
            "pull_requests": [
                {
                    "number": pr.get("number"),
                    "title": pr.get("title"),
                    "state": pr.get("state"),
                    "draft": pr.get("draft"),
                    "merged_at": pr.get("merged_at"),
                    "author": _user(pr.get("user")),
                    "created_at": pr.get("created_at"),
                    "updated_at": pr.get("updated_at"),
                    "base": (pr.get("base") or {}).get("ref"),
                    "head": (pr.get("head") or {}).get("ref"),
                    "html_url": pr.get("html_url"),
                }
                for pr in data
            ],
        }

    async def get_pull_request(self, repo: str, number: int, include_files: bool = True) -> dict[str, Any]:
        repo = self.resolve_repo(repo)
        number = _positive_int(number, "number")
        pr = await self._get_json(f"/repos/{repo}/pulls/{number}",
                                  not_found_message=f"Pull request #{number} was not found in '{repo}'.")
        result: dict[str, Any] = {
            "repository": repo,
            "number": pr.get("number"),
            "title": pr.get("title"),
            "state": pr.get("state"),
            "merged": pr.get("merged"),
            "merged_at": pr.get("merged_at"),
            "merge_commit_sha": pr.get("merge_commit_sha"),
            "author": _user(pr.get("user")),
            "created_at": pr.get("created_at"),
            "base": (pr.get("base") or {}).get("ref"),
            "head": (pr.get("head") or {}).get("ref"),
            "html_url": pr.get("html_url"),
            "body": _truncate(pr.get("body"), 6000),
            "additions": pr.get("additions"),
            "deletions": pr.get("deletions"),
            "changed_files": pr.get("changed_files"),
            "labels": [label.get("name") for label in pr.get("labels", [])],
        }
        if include_files:
            files = await self._get_json(f"/repos/{repo}/pulls/{number}/files", params={"per_page": 50})
            result["files"] = _summarize_files(files)
        return result

    async def list_issues(self, repo: str, state: str = "open", labels: str | None = None, per_page: int = 20,
                          page: int = 1) -> dict[str, Any]:
        repo = self.resolve_repo(repo)
        if state not in {"open", "closed", "all"}:
            raise InvalidInputError("state must be one of: open, closed, all.")
        params: dict[str, Any] = {"state": state, "per_page": max(1, min(per_page, 50)), "page": max(1, min(page, 50)),
                                  "sort": "updated", "direction": "desc"}
        if labels:
            if len(labels) > 200 or _CONTROL_CHARS.search(labels):
                raise InvalidInputError("Invalid labels filter.")
            params["labels"] = labels
        data = await self._get_json(f"/repos/{repo}/issues", params=params)
        return {
            "repository": repo,
            "state": state,
            "issues": [
                {
                    "number": issue.get("number"),
                    "title": issue.get("title"),
                    "state": issue.get("state"),
                    "state_reason": issue.get("state_reason"),
                    "labels": [label.get("name") for label in issue.get("labels", []) if isinstance(label, dict)],
                    "author": _user(issue.get("user")),
                    "comments": issue.get("comments"),
                    "created_at": issue.get("created_at"),
                    "updated_at": issue.get("updated_at"),
                    "html_url": issue.get("html_url"),
                }
                for issue in data
                if "pull_request" not in issue  # the issues endpoint also returns PRs
            ],
        }

    async def get_issue(self, repo: str, number: int, include_comments: bool = True) -> dict[str, Any]:
        repo = self.resolve_repo(repo)
        number = _positive_int(number, "number")
        issue = await self._get_json(f"/repos/{repo}/issues/{number}",
                                     not_found_message=f"Issue #{number} was not found in '{repo}'.")
        result: dict[str, Any] = {
            "repository": repo,
            "number": issue.get("number"),
            "title": issue.get("title"),
            "state": issue.get("state"),
            "state_reason": issue.get("state_reason"),
            "is_pull_request": "pull_request" in issue,
            "author": _user(issue.get("user")),
            "labels": [label.get("name") for label in issue.get("labels", []) if isinstance(label, dict)],
            "created_at": issue.get("created_at"),
            "closed_at": issue.get("closed_at"),
            "html_url": issue.get("html_url"),
            "body": _truncate(issue.get("body"), 6000),
        }
        if include_comments and issue.get("comments"):
            comments = await self._get_json(f"/repos/{repo}/issues/{number}/comments", params={"per_page": 20})
            result["comments"] = [
                {"author": _user(c.get("user")), "created_at": c.get("created_at"),
                 "body": _truncate(c.get("body"), 1500), "html_url": c.get("html_url")}
                for c in comments
            ]
        return result

    async def list_commits(self, repo: str, ref: str | None = None, path: str | None = None, since: str | None = None,
                           until: str | None = None, per_page: int = 20, page: int = 1) -> dict[str, Any]:
        repo = self.resolve_repo(repo)
        params: dict[str, Any] = {"per_page": max(1, min(per_page, 50)), "page": max(1, min(page, 50))}
        if ref := validate_ref(ref) or self.default_ref:
            params["sha"] = ref
        if path:
            params["path"] = validate_path(path)
        for name, value in (("since", since), ("until", until)):
            if value:
                if not re.match(r"^\d{4}-\d{2}-\d{2}(T[\d:.]+Z?)?$", value):
                    raise InvalidInputError(f"{name} must be an ISO 8601 date, e.g. 2024-01-31.")
                params[name] = value
        data = await self._get_json(f"/repos/{repo}/commits", params=params)
        return {
            "repository": repo,
            "commits": [
                {
                    "sha": c.get("sha"),
                    "message": _truncate((c.get("commit") or {}).get("message"), 500),
                    "author": ((c.get("commit") or {}).get("author") or {}).get("name"),
                    "date": ((c.get("commit") or {}).get("author") or {}).get("date"),
                    "html_url": c.get("html_url"),
                }
                for c in data
            ],
        }

    async def get_commit(self, repo: str, sha: str) -> dict[str, Any]:
        repo = self.resolve_repo(repo)
        sha = validate_ref(sha)
        if not sha:
            raise InvalidInputError("A commit SHA is required.")
        data = await self._get_json(f"/repos/{repo}/commits/{quote(sha, safe='')}",
                                    not_found_message=f"Commit '{sha}' was not found in '{repo}'.")
        commit = data.get("commit") or {}
        return {
            "repository": repo,
            "sha": data.get("sha"),
            "message": _truncate(commit.get("message"), 4000),
            "author": (commit.get("author") or {}).get("name"),
            "date": (commit.get("author") or {}).get("date"),
            "html_url": data.get("html_url"),
            "stats": data.get("stats"),
            "parents": [p.get("sha") for p in data.get("parents", [])],
            "files": _summarize_files(data.get("files", [])),
        }

    async def code_search_paths(self, repo: str, query: str, limit: int = 30) -> list[str]:
        """Authenticated-only GitHub code search, returning matching file paths (default branch)."""
        repo = self.resolve_repo(repo)
        if not self._token:
            return []
        cleaned = re.sub(r'["\\]', " ", query)[:120].strip()
        if not cleaned:
            return []
        data = await self._get_json("/search/code", params={"q": f"{cleaned} repo:{repo}", "per_page": limit})
        return [item.get("path") for item in data.get("items", []) if item.get("path")]

    async def check_repositories(self) -> list[dict[str, Any]]:
        """Probe each allowlisted repository (used for startup status)."""
        results = []
        for repo in self.repositories:
            try:
                info = await self.repository_info(repo)
                results.append({"repository": repo, "accessible": True, "visibility": info.get("visibility"),
                                "default_branch": info.get("default_branch")})
            except GitHubError as exc:
                results.append({"repository": repo, "accessible": False, "error": exc.code, "message": exc.message})
        return results


def _positive_int(value: Any, name: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise InvalidInputError(f"{name} must be a positive integer.") from None
    if number < 1 or number > 10_000_000:
        raise InvalidInputError(f"{name} must be a positive integer.")
    return number


def _summarize_files(files: list[dict[str, Any]], patch_budget: int = 24_000) -> list[dict[str, Any]]:
    summary = []
    used = 0
    for f in files[:50]:
        patch = f.get("patch")
        if patch and used < patch_budget:
            patch = _truncate(patch, min(2000, patch_budget - used))
            used += len(patch or "")
        else:
            patch = None if not patch else "[patch omitted: size budget reached]"
        summary.append({
            "filename": f.get("filename"),
            "status": f.get("status"),
            "additions": f.get("additions"),
            "deletions": f.get("deletions"),
            "patch": patch,
        })
    return summary
