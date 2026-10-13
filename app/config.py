"""Typed application configuration loaded from environment variables / `.env`."""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Verified against the public OpenRouter model catalog (GET /api/v1/models) on 2026-10-13.
DEFAULT_OPENROUTER_MODEL = "anthropic/claude-sonnet-5.5"
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api"
DEFAULT_ATLASSIAN_MCP_URL = "https://mcp.atlassian.com/v2/mcp"

# GitHub naming rules: owners are alphanumeric + single hyphens (max 39 chars),
# repositories are alphanumeric plus `.`, `_` and `-` (max 100 chars).
_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_MODEL_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._:-]*$")


class ConfigError(ValueError):
    """Raised for configuration problems the operator must fix."""


def validate_repo_identifier(value: str) -> str:
    """Validate an `owner/repo` identifier and return it unchanged.

    Rejects URLs, path traversal and anything that is not a plain GitHub name.
    """
    candidate = value.strip()
    parts = candidate.split("/")
    if len(parts) != 2:
        raise ConfigError(f"Invalid GitHub repository '{value}': expected the form 'owner/repo'.")
    owner, repo = parts
    if not _OWNER_RE.match(owner) or not _REPO_RE.match(repo) or repo in {".", ".."}:
        raise ConfigError(f"Invalid GitHub repository '{value}': owner or repository name contains invalid characters.")
    return candidate


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_ignore_empty=True,
        case_sensitive=False,
    )

    # LLM
    openrouter_api_key: SecretStr | None = None
    openrouter_model: str = DEFAULT_OPENROUTER_MODEL
    openrouter_base_url: str = DEFAULT_OPENROUTER_BASE_URL
    openrouter_validate_model: bool = True
    llm_max_retries: int = Field(default=2, ge=0, le=5)
    llm_request_timeout_seconds: int = Field(default=90, ge=10, le=600)

    # GitHub
    github_repos: Annotated[list[str], NoDecode] = Field(default_factory=list)
    github_token: SecretStr | None = None
    github_default_ref: str | None = None
    github_max_files_per_search: int = Field(default=100, ge=1, le=500)
    github_max_file_bytes: int = Field(default=200_000, ge=1_000, le=5_000_000)
    github_search_timeout_seconds: float = Field(default=25.0, ge=2.0, le=120.0)
    github_request_timeout_seconds: float = Field(default=15.0, ge=1.0, le=60.0)

    # Atlassian MCP
    atlassian_mcp_enabled: Literal["auto", "true", "false"] = "auto"
    atlassian_mcp_url: str = DEFAULT_ATLASSIAN_MCP_URL
    atlassian_service_account_api_key: SecretStr | None = None
    atlassian_site_url: str | None = None
    atlassian_timeout_seconds: float = Field(default=30.0, ge=2.0, le=120.0)

    # Application
    app_host: str = "127.0.0.1"
    app_port: int = Field(default=8000, ge=1, le=65535)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    agent_max_turns: int = Field(default=12, ge=1, le=50)
    agent_timeout_seconds: int = Field(default=120, ge=10, le=1800)
    cache_ttl_seconds: int = Field(default=300, ge=0, le=86_400)
    max_sessions: int = Field(default=200, ge=1, le=10_000)
    session_idle_ttl_seconds: int = Field(default=7_200, ge=60)
    max_message_chars: int = Field(default=8_000, ge=100, le=100_000)

    @field_validator("github_repos", mode="before")
    @classmethod
    def _split_repos(cls, value: object) -> object:
        if value is None:
            return []
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("github_repos")
    @classmethod
    def _validate_repos(cls, value: list[str]) -> list[str]:
        seen: dict[str, str] = {}
        for item in value:
            repo = validate_repo_identifier(item)
            seen.setdefault(repo.lower(), repo)
        return list(seen.values())

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator("atlassian_mcp_enabled", mode="before")
    @classmethod
    def _normalize_enabled(cls, value: object) -> object:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, str):
            lowered = value.strip().lower()
            return {"1": "true", "yes": "true", "on": "true", "0": "false", "no": "false", "off": "false"}.get(
                lowered, lowered
            )
        return value

    @field_validator("openrouter_model")
    @classmethod
    def _validate_model(cls, value: str) -> str:
        value = value.strip()
        if not _MODEL_RE.match(value):
            raise ConfigError(
                f"Invalid OPENROUTER_MODEL '{value}': expected an explicit OpenRouter model id such as "
                f"'{DEFAULT_OPENROUTER_MODEL}'."
            )
        return value

    @field_validator("openrouter_base_url", "atlassian_mcp_url")
    @classmethod
    def _require_https(cls, value: str) -> str:
        parsed = urlparse(value.strip())
        # Plain http is accepted only for a loopback gateway (local testing); everything else must be https.
        loopback_http = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}
        if (parsed.scheme != "https" and not loopback_http) or not parsed.hostname or parsed.username or parsed.password:
            raise ConfigError(f"Invalid URL '{value}': an https URL without embedded credentials is required.")
        return value.strip().rstrip("/")

    @field_validator("github_default_ref")
    @classmethod
    def _validate_ref(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not is_safe_ref(value):
            raise ConfigError(f"Invalid GITHUB_DEFAULT_REF '{value}'.")
        return value

    @field_validator("atlassian_site_url")
    @classmethod
    def _validate_site(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlparse(value.strip())
        if parsed.scheme != "https" or not parsed.hostname:
            raise ConfigError(f"Invalid ATLASSIAN_SITE_URL '{value}': expected e.g. https://your-site.atlassian.net")
        return f"https://{parsed.hostname}"

    @model_validator(mode="after")
    def _validate_atlassian_host(self) -> Settings:
        host = urlparse(self.atlassian_mcp_url).hostname or ""
        if host != "atlassian.com" and not host.endswith(".atlassian.com"):
            raise ConfigError(
                f"ATLASSIAN_MCP_URL host '{host}' is not an official Atlassian endpoint (*.atlassian.com)."
            )
        return self

    # Derived helpers -------------------------------------------------

    @property
    def openrouter_configured(self) -> bool:
        return bool(self.openrouter_api_key and self.openrouter_api_key.get_secret_value().strip())

    @property
    def github_enabled(self) -> bool:
        return bool(self.github_repos)

    @property
    def github_authenticated(self) -> bool:
        return bool(self.github_token and self.github_token.get_secret_value().strip())

    @property
    def atlassian_key_present(self) -> bool:
        key = self.atlassian_service_account_api_key
        return bool(key and key.get_secret_value().strip())

    @property
    def atlassian_requested(self) -> bool:
        """Whether the Atlassian integration should be initialised at all."""
        if self.atlassian_mcp_enabled == "false":
            return False
        if self.atlassian_mcp_enabled == "true":
            return True
        return self.atlassian_key_present

    @property
    def model_is_claude(self) -> bool:
        return self.openrouter_model.startswith("anthropic/claude")

    def secret_values(self) -> list[str]:
        """All configured secret values, used to redact logs and outbound text."""
        secrets = [self.openrouter_api_key, self.github_token, self.atlassian_service_account_api_key]
        return [s.get_secret_value() for s in secrets if s is not None and len(s.get_secret_value()) >= 6]


_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")


def is_safe_ref(value: str) -> bool:
    """Git ref / SHA validation strict enough to be safely embedded in a URL path."""
    return bool(
        _REF_RE.match(value)
        and ".." not in value
        and "//" not in value
        and not value.endswith("/")
        and not value.endswith(".lock")
    )


def load_settings(**overrides: object) -> Settings:
    """Load settings, converting pydantic errors into a readable ConfigError."""
    from pydantic import ValidationError

    try:
        return Settings(**overrides)  # type: ignore[arg-type]
    except ValidationError as exc:
        messages = []
        for err in exc.errors():
            field = ".".join(str(p) for p in err.get("loc", ())) or "settings"
            ctx_error = (err.get("ctx") or {}).get("error")
            message = str(ctx_error) if ctx_error else err.get("msg", "invalid value")
            messages.append(f"{field.upper()}: {message}")
        raise ConfigError("Invalid configuration:\n  - " + "\n  - ".join(messages)) from None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
