"""Live integration tests against real external services.

Opt-in:  RUN_INTEGRATION_TESTS=1 uv run pytest -m live
Each test is skipped when its credentials are not available (environment or .env).
"""

from __future__ import annotations

import os

import pytest
from dotenv import dotenv_values

from app.config import load_settings

pytestmark = pytest.mark.live

# Captured at import time, before the autouse fixture isolates the environment.
_LIVE = {**{k: v for k, v in dotenv_values(".env").items() if v}, **{k: v for k, v in os.environ.items() if v}}
ENABLED = _LIVE.get("RUN_INTEGRATION_TESTS") == "1"

requires_live = pytest.mark.skipif(not ENABLED, reason="set RUN_INTEGRATION_TESTS=1 to run live tests")
requires_openrouter = pytest.mark.skipif(not (ENABLED and _LIVE.get("OPENROUTER_API_KEY")),
                                         reason="needs RUN_INTEGRATION_TESTS=1 and OPENROUTER_API_KEY")
requires_atlassian = pytest.mark.skipif(not (ENABLED and _LIVE.get("ATLASSIAN_SERVICE_ACCOUNT_API_KEY")),
                                        reason="needs RUN_INTEGRATION_TESTS=1 and ATLASSIAN_SERVICE_ACCOUNT_API_KEY")


def live_settings(**overrides):
    keys = ["OPENROUTER_API_KEY", "OPENROUTER_MODEL", "ATLASSIAN_SERVICE_ACCOUNT_API_KEY", "ATLASSIAN_MCP_URL",
            "ATLASSIAN_SITE_URL"]
    values = {k.lower(): _LIVE[k] for k in keys if _LIVE.get(k)}
    values.update(overrides)
    return load_settings(_env_file=None, **values)


@requires_live
async def test_live_github_anonymous_public_repository():
    from app.integrations.github.client import GitHubClient
    from app.integrations.github.search import SearchLimits, search_repository_content

    client = GitHubClient(["fastapi/fastapi"])  # no token on purpose
    info = await client.repository_info("fastapi/fastapi")
    assert info["visibility"] == "public" and info["default_branch"]
    readme = await client.readme("fastapi/fastapi")
    assert "FastAPI" in readme["content"]
    file = await client.file_contents("fastapi/fastapi", "fastapi/security/oauth2.py", end_line=40)
    assert "import" in file["content"]
    result = await search_repository_content(client, "fastapi/fastapi", "OAuth2PasswordBearer",
                                             limits=SearchLimits(max_files=60))
    assert any(r["path"] == "fastapi/security/oauth2.py" for r in result["results"])
    assert result["complete"] is False  # large repository: honestly reported as partial
    await client.aclose()


@requires_openrouter
async def test_live_openrouter_answers_with_github_sources():
    from app.agent.runtime import AgentRuntime
    from app.agent.sessions import SessionStore
    from app.integrations.registry import IntegrationRegistry

    settings = live_settings(github_repos="fastapi/fastapi", openrouter_validate_model=True, agent_max_turns=8)
    registry = IntegrationRegistry(settings)
    await registry.startup()
    assert registry.llm.state == "ready", registry.llm.detail
    runtime = AgentRuntime(settings, registry)
    session = SessionStore().create()
    events = [e async for e in runtime.run_turn(
        session, "In fastapi/fastapi, which class implements the OAuth2 password bearer flow and in which file?")]
    followup = [e async for e in runtime.run_turn(session, "Repeat only the file path from your previous answer.")]
    runtime.close()
    await registry.shutdown()
    errors = [e for e in events + followup if e["type"] == "error"]
    assert not errors, errors
    answer = "".join(e["text"] for e in events if e["type"] == "delta")
    assert "OAuth2PasswordBearer" in answer
    assert any(e["type"] == "source" and "github.com/fastapi/fastapi" in e["url"] for e in events)
    assert "oauth2.py" in "".join(e["text"] for e in followup if e["type"] == "delta")


@requires_atlassian
async def test_live_atlassian_read_only_discovery():
    from app.integrations.atlassian.mcp_client import APPROVED_TOOLS, AtlassianMCP
    from app.integrations.status import IntegrationState

    s = live_settings()
    atl = AtlassianMCP(s.atlassian_mcp_url, s.atlassian_service_account_api_key.get_secret_value(),
                       site_url=s.atlassian_site_url)
    await atl.initialize()
    states = {atl.jira.state, atl.confluence.state}
    assert IntegrationState.AUTH_FAILED not in states, atl.jira.detail
    assert IntegrationState.CONNECTED in states, (atl.jira.detail, atl.confluence.detail)
    assert set(atl.exposed_tool_names()) <= APPROVED_TOOLS
