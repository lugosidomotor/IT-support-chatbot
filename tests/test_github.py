from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest
from mcp import Client

from app.agent.sources import SourceCollector
from app.integrations.github.client import (
    GitHubClient,
    InvalidInputError,
    NotFoundError,
    RateLimitedError,
    RepoNotAllowedError,
    UpstreamError,
)
from app.integrations.github.mcp_server import TOOL_NAMES, build_github_mcp_server
from app.integrations.github.search import SearchLimits, classify, search_repository_content, sub_terms
from tests.conftest import GITHUB_TOKEN
from tests.fakes import AUTH_PY, FakeGitHub

REPO = "acme/backend"


def make_client(fake: FakeGitHub, token: str | None = None, **kwargs) -> GitHubClient:
    return GitHubClient([REPO], token, transport=fake.transport(), max_retry_wait_seconds=0.05, **kwargs)


async def test_repository_metadata_anonymously():
    fake = FakeGitHub()
    client = make_client(fake)
    info = await client.repository_info(REPO)
    assert info["repository"] == REPO
    assert info["default_branch"] == "main"
    assert info["visibility"] == "public"
    assert all("authorization" not in r.headers for r in fake.calls)
    assert client.rate_limit.limit == 60


async def test_readme_anonymously():
    fake = FakeGitHub()
    readme = await make_client(fake).readme(REPO)
    assert readme["path"] == "README.md"
    assert "OAuth2 bearer tokens" in readme["content"]
    assert all("authorization" not in r.headers for r in fake.calls)


async def test_read_file_anonymously_uses_raw_cdn_not_api_quota():
    fake = FakeGitHub()
    result = await make_client(fake).file_contents(REPO, "app/auth.py")
    assert result["html_url"] == f"https://github.com/{REPO}/blob/HEAD/app/auth.py"
    assert "    3| class OAuth2PasswordBearer:" in result["content"]
    assert result["total_lines"] == len(AUTH_PY.splitlines())
    assert [r.url.host for r in fake.calls] == ["raw.githubusercontent.com"]


async def test_read_file_line_range():
    result = await make_client(FakeGitHub()).file_contents(REPO, "app/auth.py", start_line=3, end_line=3)
    assert result["lines"] == "3-3"
    assert result["content"].strip() == "3| class OAuth2PasswordBearer:"


async def test_private_repo_with_token_uses_api_and_auth_header():
    fake = FakeGitHub(private=True)
    client = make_client(fake, token=GITHUB_TOKEN)
    info = await client.repository_info(REPO)
    content = await client.file_contents(REPO, "app/auth.py")
    assert info["visibility"] == "private"
    assert "OAuth2PasswordBearer" in content["content"]
    assert all(r.headers["authorization"] == f"Bearer {GITHUB_TOKEN}" for r in fake.calls)
    assert all(r.url.host == "api.github.com" for r in fake.calls)
    assert GITHUB_TOKEN not in json.dumps([info, content])


async def test_missing_file_is_not_found():
    with pytest.raises(NotFoundError, match="not found"):
        await make_client(FakeGitHub()).file_contents(REPO, "nope.py")


async def test_repository_allowlist_enforced():
    fake = FakeGitHub()
    client = make_client(fake)
    for repo in ["torvalds/linux", "acme/other", "ACME/backend-evil"]:
        with pytest.raises(RepoNotAllowedError):
            await client.repository_info(repo)
    for repo in ["https://github.com/acme/backend", "acme/backend/../x", "../acme", ""]:
        with pytest.raises(InvalidInputError):
            await client.repository_info(repo)
    assert fake.calls == []  # rejected before any network request
    assert (await client.repository_info("ACME/Backend"))["repository"] == REPO  # case-insensitive match


@pytest.mark.parametrize("path", ["../secrets", "a/../../b", "/../etc/passwd", "a\\b", "https://evil.com/x", "a?x=1", "a\x00b"])
async def test_path_traversal_and_url_injection_rejected(path):
    fake = FakeGitHub()
    with pytest.raises(InvalidInputError):
        await make_client(fake).file_contents(REPO, path)
    assert fake.calls == []


async def test_leading_slash_is_repository_relative():
    fake = FakeGitHub()
    result = await make_client(fake).file_contents(REPO, "/README.md")
    assert result["path"] == "README.md"
    assert str(fake.calls[0].url) == f"https://raw.githubusercontent.com/{REPO}/HEAD/README.md"


@pytest.mark.parametrize("ref", ["../main", "main..dev", "-x", "a b", "main?x=1"])
async def test_invalid_refs_rejected(ref):
    with pytest.raises(InvalidInputError):
        await make_client(FakeGitHub()).file_contents(REPO, "README.md", ref=ref)


async def test_rate_limit_is_reported_and_short_circuits():
    fake = FakeGitHub()
    fake.rate_limited = True
    client = make_client(fake)
    with pytest.raises(RateLimitedError) as exc_info:
        await client.repository_info(REPO)
    payload = exc_info.value.to_dict()
    assert payload["incomplete"] is True and payload["retry_after_seconds"] > 0
    assert "GITHUB_TOKEN" in exc_info.value.message
    calls = len(fake.calls)
    with pytest.raises(RateLimitedError):
        await client.list_issues(REPO)
    assert len(fake.calls) == calls  # no further requests while blocked


async def test_retry_after_is_respected_with_bounded_retries():
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(time.monotonic())
        if len(attempts) == 1:
            return httpx.Response(429, headers={"retry-after": "0"}, json={"message": "secondary rate limit"})
        return httpx.Response(200, json={"full_name": REPO, "default_branch": "main"})

    client = GitHubClient([REPO], transport=httpx.MockTransport(handler), max_retry_wait_seconds=1)
    assert (await client.repository_info(REPO))["repository"] == REPO
    assert len(attempts) == 2


async def test_unavailable_upstream_has_bounded_retries():
    fake = FakeGitHub()
    fake.unavailable = True
    with pytest.raises(UpstreamError):
        await make_client(fake).repository_info(REPO)
    assert len(fake.calls) == 3


async def test_unavailable_repository_reported_by_check():
    fake = FakeGitHub(repo="acme/other")  # the configured repo does not exist upstream
    checks = await make_client(fake).check_repositories()
    assert checks == [{"repository": REPO, "accessible": False, "error": "not_found",
                       "message": f"Repository '{REPO}' was not found or is not accessible."}]


async def test_redirect_to_untrusted_host_is_blocked():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example.com/steal"})

    client = GitHubClient([REPO], transport=httpx.MockTransport(handler))
    with pytest.raises(UpstreamError, match="untrusted host"):
        await client.repository_info(REPO)


async def test_same_host_redirect_is_followed():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/repos/{REPO}":
            return httpx.Response(301, headers={"location": "https://api.github.com/repositories/1"})
        return httpx.Response(200, json={"full_name": REPO, "default_branch": "main"})

    client = GitHubClient([REPO], transport=httpx.MockTransport(handler))
    assert (await client.repository_info(REPO))["default_branch"] == "main"


async def test_requests_are_cached_and_deduplicated():
    fake = FakeGitHub()
    client = make_client(fake)
    await asyncio.gather(*[client.repository_info(REPO) for _ in range(5)])
    await client.repository_info(REPO)
    assert len(fake.calls) == 1


async def test_large_files_are_truncated_to_byte_limit():
    fake = FakeGitHub()
    result = await make_client(fake, max_file_bytes=1_000).file_contents(REPO, "app/auth.py")
    assert not result["truncated"]
    small = await GitHubClient([REPO], transport=fake.transport(), max_file_bytes=1_000).fetch_file_bytes(
        REPO, "app/auth.py", None, max_bytes=20)
    assert small.truncated and len(small.content) == 20


async def test_list_issues_excludes_pull_requests():
    issues = (await make_client(FakeGitHub()).list_issues(REPO))["issues"]
    assert [i["number"] for i in issues] == [3]


async def test_search_finds_identifier_with_line_numbers():
    fake = FakeGitHub()
    result = await search_repository_content(make_client(fake), REPO, "OAuth2PasswordBearer")
    paths = [r["path"] for r in result["results"]]
    assert "app/auth.py" in paths
    hit = next(r for r in result["results"] if r["path"] == "app/auth.py")
    assert hit["matches"][0] == {"line": 3, "text": "class OAuth2PasswordBearer:"}
    assert hit["html_url"].endswith("/blob/main/app/auth.py#L3")
    # the 50 MB changelog is skipped, so the search honestly reports it is not exhaustive
    assert result["complete"] is False
    assert any("larger than" in reason for reason in result["partial_reasons"])


async def test_search_respects_file_limit_and_reports_partial_results():
    fake = FakeGitHub()
    limits = SearchLimits(max_files=1, max_file_bytes=200_000, timeout_seconds=5)
    result = await search_repository_content(make_client(fake), REPO, "database", limits=limits)
    assert result["stats"]["files_searched"] == 1
    assert result["complete"] is False
    assert "PARTIAL" in result["note"]


async def test_search_reports_rate_limit_as_partial():
    fake = FakeGitHub()
    client = make_client(fake)
    await client.tree(REPO)  # tree cached, then the API starts rate limiting
    fake.rate_limited = True
    result = await search_repository_content(client, REPO, "DATABASE_URL")
    assert result["results"]  # raw CDN reads still work anonymously
    assert result["ref"] == "main"


async def test_search_rejects_disallowed_repo():
    with pytest.raises(RepoNotAllowedError):
        await search_repository_content(make_client(FakeGitHub()), "other/repo", "x")


async def test_search_ignores_generic_words_and_links_best_line():
    from app.integrations.github.search import query_terms

    assert query_terms("where is the OAuth2PasswordBearer class defined") == ["oauth2passwordbearer"]
    assert query_terms("class") == ["class"]  # nothing more specific: keep it
    result = await search_repository_content(make_client(FakeGitHub()), REPO, "OAuth2PasswordBearer class")
    top = result["results"][0]
    assert top["path"] == "app/auth.py"
    assert top["html_url"].endswith("#L3")  # the definition line, not merely the first match


def test_search_helpers():
    assert sub_terms("OAuth2PasswordBearer") == ["auth2", "password", "bearer"]  # substring of oauth2.py
    assert classify("node_modules/x/index.js") is None
    assert classify("package-lock.json") is None
    assert classify("docs/guide.md") == "docs"
    assert classify("Dockerfile") == "config"
    assert classify("src/app.py") == "source"


async def test_mcp_server_exposes_only_read_tools_and_records_sources():
    fake = FakeGitHub()
    collector = SourceCollector()
    server = build_github_mcp_server(make_client(fake), SearchLimits(), collector)
    async with Client(server) as mcp:
        tools = (await mcp.list_tools()).tools
        assert sorted(t.name for t in tools) == sorted(TOOL_NAMES)
        assert all(t.annotations and t.annotations.read_only_hint for t in tools)
        ok = await mcp.call_tool("get_file_contents", {"repository": REPO, "path": "app/auth.py"})
        assert not ok.is_error
        denied = await mcp.call_tool("get_repository_info", {"repository": "torvalds/linux"})
        assert denied.is_error and "repo_not_allowed" in denied.content[0].text
        bad = await mcp.call_tool("get_file_contents", {"repository": REPO})
        assert bad.is_error and "invalid_input" in bad.content[0].text
        unknown = await mcp.call_tool("delete_repository", {"repository": REPO})
        assert unknown.is_error
    assert [s.title for s in collector.sources] == [f"{REPO}: app/auth.py"]
