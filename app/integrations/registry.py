"""Startup wiring and live status for the LLM provider and all knowledge-source integrations."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.agent.prompts import PromptContext
from app.config import Settings
from app.integrations.atlassian.mcp_client import AtlassianMCP, Connector
from app.integrations.github.client import GitHubClient
from app.integrations.github.search import SearchLimits
from app.integrations.status import IntegrationState, IntegrationStatus

logger = logging.getLogger(__name__)


@dataclass
class LLMStatus:
    state: str  # ready | unverified | not_configured | auth_failed | invalid_model
    model: str
    detail: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return self.state in {"ready", "unverified"}

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"status": self.state, "provider": "openrouter", "model": self.model}
        if self.detail:
            data["detail"] = self.detail
        if self.warnings:
            data["warnings"] = self.warnings
        return data


async def check_llm(settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> LLMStatus:
    """Validate the OpenRouter configuration (key presence, key validity, model id)."""
    model = settings.openrouter_model
    warnings = []
    if not settings.model_is_claude:
        warnings.append("Non-Claude models are outside the guaranteed compatibility scope of this POC.")
    if not settings.openrouter_configured:
        return LLMStatus("not_configured", model,
                         "OPENROUTER_API_KEY is not set. Add it to .env and restart the application.", warnings)
    if not settings.openrouter_validate_model:
        return LLMStatus("unverified", model, "Startup validation disabled (OPENROUTER_VALIDATE_MODEL=false).", warnings)

    key = settings.openrouter_api_key.get_secret_value()  # type: ignore[union-attr]
    base = settings.openrouter_base_url
    try:
        async with httpx.AsyncClient(timeout=10.0, transport=transport, follow_redirects=False) as http:
            key_response = await http.get(f"{base}/v1/key", headers={"Authorization": f"Bearer {key}"})
            if key_response.status_code in (401, 403):
                return LLMStatus("auth_failed", model,
                                 "OpenRouter rejected OPENROUTER_API_KEY (HTTP 401). Check the key.", warnings)
            models_response = await http.get(f"{base}/v1/models")
            models_response.raise_for_status()
            ids = {m.get("id") for m in models_response.json().get("data", [])}
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("could not verify openrouter configuration", extra={"error": type(exc).__name__})
        return LLMStatus("unverified", model, f"Could not verify OpenRouter configuration ({type(exc).__name__}).",
                         warnings)
    if model not in ids:
        return LLMStatus(
            "invalid_model", model,
            f"OPENROUTER_MODEL '{model}' was not found in the OpenRouter model catalog. "
            "Use an explicit id such as 'anthropic/claude-sonnet-5.5'.", warnings)
    return LLMStatus("ready", model, None, warnings)


class IntegrationRegistry:
    def __init__(
        self,
        settings: Settings,
        *,
        github_transport: httpx.AsyncBaseTransport | None = None,
        atlassian_connector: Connector | None = None,
        llm_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self._github_transport = github_transport
        self._atlassian_connector = atlassian_connector
        self._llm_transport = llm_transport
        self.llm = LLMStatus("unverified", settings.openrouter_model, "Checking…")
        self.github: GitHubClient | None = None
        self.github_checks: list[dict[str, Any]] = []
        self._github_checked_at: float = 0.0
        self.atlassian: AtlassianMCP | None = None
        self._atlassian_task: asyncio.Task[None] | None = None
        self.search_limits = SearchLimits(
            max_files=settings.github_max_files_per_search,
            max_file_bytes=settings.github_max_file_bytes,
            timeout_seconds=settings.github_search_timeout_seconds,
        )

    # ------------------------------------------------------------------ lifecycle

    async def startup(self) -> None:
        s = self.settings
        self.llm = await check_llm(s, self._llm_transport)
        logger.info("llm configuration", extra=self.llm.to_dict())

        if s.github_enabled:
            self.github = GitHubClient(
                s.github_repos,
                s.github_token.get_secret_value() if s.github_token else None,
                default_ref=s.github_default_ref,
                timeout_seconds=s.github_request_timeout_seconds,
                cache_ttl_seconds=s.cache_ttl_seconds,
                max_file_bytes=s.github_max_file_bytes,
                transport=self._github_transport,
            )
            self.github_checks = await self.github.check_repositories()
            self._github_checked_at = time.time()
            logger.info("github integration", extra={"repositories": self.github_checks,
                                                      "authentication": self._github_auth_mode()})

        if s.atlassian_requested and s.atlassian_key_present:
            self.atlassian = AtlassianMCP(
                s.atlassian_mcp_url,
                s.atlassian_service_account_api_key.get_secret_value(),  # type: ignore[union-attr]
                timeout_seconds=s.atlassian_timeout_seconds,
                site_url=s.atlassian_site_url,
                connector=self._atlassian_connector,
            )
            # Discovery runs in the background so a slow Atlassian endpoint never blocks startup.
            self._atlassian_task = asyncio.create_task(self._init_atlassian())
        logger.info("integration status", extra={"integrations": self.to_dict()})

    async def _init_atlassian(self) -> None:
        assert self.atlassian is not None
        try:
            await self.atlassian.initialize()
        except Exception:  # noqa: BLE001
            logger.exception("atlassian initialisation crashed")
        logger.info("atlassian status", extra={"jira": self.atlassian.jira.to_dict(),
                                               "confluence": self.atlassian.confluence.to_dict()})

    async def wait_ready(self) -> None:
        if self._atlassian_task is not None:
            await asyncio.shield(self._atlassian_task)

    async def shutdown(self) -> None:
        if self._atlassian_task is not None and not self._atlassian_task.done():
            self._atlassian_task.cancel()
        if self.github is not None:
            await self.github.aclose()

    # ------------------------------------------------------------------ status

    def _github_auth_mode(self) -> str:
        return "token" if self.settings.github_authenticated else "anonymous"

    def github_status(self) -> IntegrationStatus:
        s = self.settings
        if not s.github_enabled or self.github is None:
            return IntegrationStatus(IntegrationState.DISABLED, "No repositories configured (GITHUB_REPOS).")
        extra: dict[str, Any] = {
            "authentication": self._github_auth_mode(),
            "repositories": self.github.repositories,
            "repository_checks": self.github_checks,
            "rate_limit": self.github.rate_limit.to_dict(),
        }
        if any(c.get("error") == "auth_failed" for c in self.github_checks) or (
            self.github.last_error is not None and self.github.last_error.code == "auth_failed"
        ):
            return IntegrationStatus(IntegrationState.AUTH_FAILED, "GitHub rejected GITHUB_TOKEN (HTTP 401).", extra)
        now = time.time()
        if self.github.rate_limit.blocked_until > now:
            minutes = max(1, int((self.github.rate_limit.blocked_until - now) / 60))
            return IntegrationStatus(
                IntegrationState.UNAVAILABLE,
                f"GitHub API rate limit reached; resets in about {minutes} min. Results may be incomplete."
                + ("" if self.github.authenticated else " Set GITHUB_TOKEN for a higher limit."),
                extra,
            )
        accessible = [c for c in self.github_checks if c.get("accessible")]
        recovered = (self.github.last_success_at or 0) > self._github_checked_at
        if not accessible and not recovered:
            reasons = "; ".join(f"{c['repository']}: {c.get('message')}" for c in self.github_checks)
            return IntegrationStatus(IntegrationState.UNAVAILABLE, reasons or "Repositories are not reachable.", extra)
        detail = None
        if accessible and len(accessible) < len(self.github_checks):
            detail = f"{len(accessible)} of {len(self.github_checks)} repositories are accessible."
        return IntegrationStatus(IntegrationState.CONNECTED, detail, extra)

    def _atlassian_product_status(self, product: str) -> IntegrationStatus:
        s = self.settings
        if s.atlassian_mcp_enabled == "false":
            return IntegrationStatus(IntegrationState.DISABLED, "Disabled by ATLASSIAN_MCP_ENABLED=false.")
        if not s.atlassian_key_present:
            if s.atlassian_mcp_enabled == "true":
                return IntegrationStatus(
                    IntegrationState.UNAVAILABLE,
                    "ATLASSIAN_MCP_ENABLED=true but ATLASSIAN_SERVICE_ACCOUNT_API_KEY is empty; integration not started.",
                )
            return IntegrationStatus(IntegrationState.DISABLED, "No Atlassian service account API key configured.")
        if self.atlassian is None:
            return IntegrationStatus(IntegrationState.CONNECTING, "Starting…")
        return getattr(self.atlassian, product)

    def jira_status(self) -> IntegrationStatus:
        return self._atlassian_product_status("jira")

    def confluence_status(self) -> IntegrationStatus:
        return self._atlassian_product_status("confluence")

    def to_dict(self) -> dict[str, Any]:
        github = self.github_status()
        jira = self.jira_status()
        confluence = self.confluence_status()
        has_sources = (
            github.state != IntegrationState.DISABLED
            or jira.state == IntegrationState.CONNECTED
            or confluence.state == IntegrationState.CONNECTED
        )
        return {
            "llm": self.llm.to_dict(),
            "github": github.to_dict(),
            "jira": jira.to_dict(),
            "confluence": confluence.to_dict(),
            "has_data_sources": has_sources,
        }

    # ------------------------------------------------------------------ agent wiring

    def atlassian_tools_enabled(self) -> bool:
        return self.atlassian is not None and bool(self.atlassian.exposed_tool_names())

    def prompt_context(self) -> PromptContext:
        atl = self.atlassian
        enabled = atl.enabled_products() if atl is not None else set()
        return PromptContext(
            github_repos=self.github.repositories if self.github is not None else [],
            github_authenticated=self.settings.github_authenticated,
            jira_enabled="jira" in enabled,
            confluence_enabled="confluence" in enabled,
            atlassian_site_url=atl.site_url if atl is not None else None,
            atlassian_cloud_ids=[str(site["id"]) for site in atl.sites if site.get("id")] if atl is not None else [],
        )
