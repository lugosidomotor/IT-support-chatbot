from __future__ import annotations

import pytest

from app.config import Settings, load_settings

ENV_VARS = [
    "OPENROUTER_API_KEY", "OPENROUTER_MODEL", "OPENROUTER_BASE_URL", "OPENROUTER_VALIDATE_MODEL",
    "LLM_MAX_RETRIES", "LLM_REQUEST_TIMEOUT_SECONDS",
    "GITHUB_REPOS", "GITHUB_TOKEN", "GITHUB_DEFAULT_REF", "GITHUB_MAX_FILES_PER_SEARCH", "GITHUB_MAX_FILE_BYTES",
    "GITHUB_SEARCH_TIMEOUT_SECONDS", "GITHUB_REQUEST_TIMEOUT_SECONDS",
    "ATLASSIAN_MCP_ENABLED", "ATLASSIAN_MCP_URL", "ATLASSIAN_SERVICE_ACCOUNT_API_KEY", "ATLASSIAN_SITE_URL",
    "ATLASSIAN_TIMEOUT_SECONDS", "APP_HOST", "APP_PORT", "LOG_LEVEL", "AGENT_MAX_TURNS", "AGENT_TIMEOUT_SECONDS",
    "CACHE_TTL_SECONDS",
]

OPENROUTER_KEY = "sk-or-v1-unit-test-key-0123456789"
GITHUB_TOKEN = "fake-github-token-for-unit-tests-0123456789"
ATLASSIAN_KEY = "ATSTTunit-test-atlassian-key-0123456789"


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let a developer's real .env / environment leak into unit tests."""
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def make_settings():
    def factory(**overrides: object) -> Settings:
        defaults: dict[str, object] = {"openrouter_validate_model": False}
        defaults.update(overrides)
        return load_settings(_env_file=None, **defaults)

    return factory
