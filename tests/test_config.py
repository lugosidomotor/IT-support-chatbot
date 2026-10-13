from __future__ import annotations

import json
import logging

import httpx
import pytest

from app.config import DEFAULT_OPENROUTER_MODEL, ConfigError, load_settings
from app.integrations.registry import IntegrationRegistry, check_llm
from app.integrations.status import IntegrationState
from app.logging_config import JsonFormatter, redact, register_secrets
from tests.conftest import ATLASSIAN_KEY, GITHUB_TOKEN, OPENROUTER_KEY
from tests.fakes import FakeGitHub, failing_connector


def test_valid_openrouter_configuration(make_settings):
    s = make_settings(openrouter_api_key=OPENROUTER_KEY, openrouter_model="anthropic/claude-sonnet-5.5")
    assert s.openrouter_configured
    assert s.openrouter_model == "anthropic/claude-sonnet-5.5"
    assert s.openrouter_base_url == "https://openrouter.ai/api"
    assert s.model_is_claude
    assert OPENROUTER_KEY not in repr(s)


def test_missing_openrouter_key_is_reported_not_fatal(make_settings):
    s = make_settings()
    assert not s.openrouter_configured


async def test_missing_openrouter_key_status(make_settings):
    status = await check_llm(make_settings())
    assert status.state == "not_configured"
    assert "OPENROUTER_API_KEY" in (status.detail or "")
    assert not status.usable


def test_empty_model_uses_documented_default(monkeypatch):
    monkeypatch.setenv("OPENROUTER_MODEL", "")
    assert load_settings(_env_file=None).openrouter_model == DEFAULT_OPENROUTER_MODEL


@pytest.mark.parametrize("model", ["claude sonnet", "anthropic/", "/claude", "anthropic/claude sonnet"])
def test_invalid_model_id_is_actionable_error(model):
    with pytest.raises(ConfigError, match="OPENROUTER_MODEL"):
        load_settings(_env_file=None, openrouter_model=model)


def test_non_claude_model_is_allowed_with_warning(make_settings):
    s = make_settings(openrouter_model="openai/gpt-5")
    assert not s.model_is_claude


def test_public_github_without_token(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOS", "fastapi/fastapi, pydantic/pydantic")
    monkeypatch.setenv("GITHUB_TOKEN", "")
    s = load_settings(_env_file=None)
    assert s.github_repos == ["fastapi/fastapi", "pydantic/pydantic"]
    assert s.github_enabled
    assert not s.github_authenticated
    assert s.github_token is None


def test_private_github_with_token(make_settings):
    s = make_settings(github_repos="myorg/backend,myorg/frontend", github_token=GITHUB_TOKEN)
    assert s.github_authenticated
    assert GITHUB_TOKEN not in repr(s) and GITHUB_TOKEN not in str(s.model_dump())


def test_duplicate_repos_are_collapsed(make_settings):
    assert make_settings(github_repos="a/b,A/B, a/b").github_repos == ["a/b"]


@pytest.mark.parametrize("repo", ["../etc", "https://github.com/a/b", "a/b/c", "a b/c", "a/..", "-bad/repo", "owner"])
def test_invalid_repositories_are_rejected(repo):
    with pytest.raises(ConfigError, match="GitHub repository"):
        load_settings(_env_file=None, github_repos=repo)


def test_missing_atlassian_credentials_disable_integration(make_settings):
    s = make_settings()
    assert s.atlassian_mcp_enabled == "auto"
    assert not s.atlassian_key_present
    assert not s.atlassian_requested


def test_atlassian_key_enables_auto_mode(make_settings):
    assert make_settings(atlassian_service_account_api_key=ATLASSIAN_KEY).atlassian_requested


def test_atlassian_forced_off(make_settings):
    assert not make_settings(atlassian_service_account_api_key=ATLASSIAN_KEY, atlassian_mcp_enabled="false").atlassian_requested


@pytest.mark.parametrize("url", ["http://mcp.atlassian.com/v2/mcp", "https://evil.example.com/mcp",
                                 "https://user:pw@mcp.atlassian.com/v2/mcp"])
def test_untrusted_atlassian_endpoints_rejected(url):
    with pytest.raises(ConfigError):
        load_settings(_env_file=None, atlassian_mcp_url=url)


def test_openrouter_base_url_requires_https_except_loopback():
    with pytest.raises(ConfigError):
        load_settings(_env_file=None, openrouter_base_url="http://openrouter.ai/api")
    assert load_settings(_env_file=None, openrouter_base_url="http://127.0.0.1:9999").openrouter_base_url


async def test_no_integrations_configured(make_settings):
    registry = IntegrationRegistry(make_settings())
    await registry.startup()
    status = registry.to_dict()
    assert status["github"]["status"] == "disabled"
    assert status["jira"]["status"] == "disabled"
    assert status["confluence"]["status"] == "disabled"
    assert status["has_data_sources"] is False
    assert registry.github is None and registry.atlassian is None


async def test_invalid_atlassian_credentials_do_not_block_startup(make_settings):
    gh = FakeGitHub()
    s = make_settings(github_repos="acme/backend", atlassian_service_account_api_key=ATLASSIAN_KEY)
    registry = IntegrationRegistry(s, github_transport=gh.transport(), atlassian_connector=failing_connector(401))
    await registry.startup()
    await registry.wait_ready()
    assert registry.jira_status().state == IntegrationState.AUTH_FAILED
    assert registry.confluence_status().state == IntegrationState.AUTH_FAILED
    assert registry.github_status().state == IntegrationState.CONNECTED
    assert ATLASSIAN_KEY not in json.dumps(registry.to_dict())
    await registry.shutdown()


async def test_atlassian_enabled_without_key_is_unavailable_not_fatal(make_settings):
    registry = IntegrationRegistry(make_settings(atlassian_mcp_enabled="true"))
    await registry.startup()
    assert registry.atlassian is None
    assert registry.jira_status().state == IntegrationState.UNAVAILABLE
    assert "ATLASSIAN_SERVICE_ACCOUNT_API_KEY" in (registry.jira_status().detail or "")


def _openrouter_transport(key_status: int = 200, models: list[str] | None = None, fail: bool = False):
    def handler(request: httpx.Request) -> httpx.Response:
        if fail:
            raise httpx.ConnectError("offline")
        if request.url.path.endswith("/v1/key"):
            assert request.headers["authorization"] == f"Bearer {OPENROUTER_KEY}"
            return httpx.Response(key_status, json={"data": {}})
        return httpx.Response(200, json={"data": [{"id": m} for m in (models or [DEFAULT_OPENROUTER_MODEL])]})

    return httpx.MockTransport(handler)


async def test_llm_check_ready(make_settings):
    s = make_settings(openrouter_api_key=OPENROUTER_KEY, openrouter_validate_model=True)
    assert (await check_llm(s, _openrouter_transport())).state == "ready"


async def test_llm_check_auth_failed(make_settings):
    s = make_settings(openrouter_api_key=OPENROUTER_KEY, openrouter_validate_model=True)
    status = await check_llm(s, _openrouter_transport(key_status=401))
    assert status.state == "auth_failed" and not status.usable


async def test_llm_check_unknown_model(make_settings):
    s = make_settings(openrouter_api_key=OPENROUTER_KEY, openrouter_validate_model=True,
                      openrouter_model="anthropic/claude-nonexistent-9")
    status = await check_llm(s, _openrouter_transport())
    assert status.state == "invalid_model"
    assert "anthropic/claude-nonexistent-9" in (status.detail or "")


async def test_llm_check_offline_is_unverified_but_usable(make_settings):
    s = make_settings(openrouter_api_key=OPENROUTER_KEY, openrouter_validate_model=True)
    status = await check_llm(s, _openrouter_transport(fail=True))
    assert status.state == "unverified" and status.usable


def test_secrets_are_redacted_from_logs():
    register_secrets([OPENROUTER_KEY, GITHUB_TOKEN])
    record = logging.LogRecord("t", logging.INFO, __file__, 1, f"key={OPENROUTER_KEY} token={GITHUB_TOKEN}", None, None)
    record.header = "Authorization: Bearer abc.def-ghi"
    line = JsonFormatter().format(record)
    assert OPENROUTER_KEY not in line and GITHUB_TOKEN not in line and "abc.def-ghi" not in line
    assert "[REDACTED]" in line
    assert "ghp_" not in redact("leaked ghp_" + "a" * 36)
