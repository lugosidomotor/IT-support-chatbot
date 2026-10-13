"""FastAPI application entry point.

Run locally:  uv run uvicorn app.main:app --reload
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.agent.runtime import AgentRuntime, ClientFactory
from app.agent.sessions import SessionStore
from app.api import chat, health
from app.config import Settings, get_settings
from app.integrations.registry import IntegrationRegistry
from app.logging_config import configure_logging, register_secrets
from app.services.chat_service import ChatService

logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
        "connect-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


def create_app(
    settings: Settings | None = None,
    *,
    registry_factory: Callable[[Settings], IntegrationRegistry] | None = None,
    client_factory: ClientFactory | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        cfg = settings or get_settings()
        configure_logging(cfg.log_level)
        register_secrets(cfg.secret_values())
        logger.info("starting developer support agent", extra={
            "model": cfg.openrouter_model, "github_repos": cfg.github_repos,
            "github_auth": "token" if cfg.github_authenticated else "anonymous",
            "atlassian": cfg.atlassian_mcp_enabled, "atlassian_key_present": cfg.atlassian_key_present,
        })
        registry = registry_factory(cfg) if registry_factory else IntegrationRegistry(cfg)
        await registry.startup()
        runtime_kwargs: dict[str, Any] = {"client_factory": client_factory} if client_factory else {}
        runtime = AgentRuntime(cfg, registry, **runtime_kwargs)
        app.state.settings = cfg
        app.state.registry = registry
        app.state.chat_service = ChatService(cfg, registry, runtime,
                                             SessionStore(cfg.max_sessions, cfg.session_idle_ttl_seconds))
        try:
            yield
        finally:
            runtime.close()
            await registry.shutdown()

    app = FastAPI(title="Developer Support Agent", version="0.1.0", lifespan=lifespan,
                  docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json")

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Callable[[Request], Any]) -> Response:
        response: Response = await call_next(request)
        if not request.url.path.startswith("/api/docs"):
            for header, value in SECURITY_HEADERS.items():
                response.headers.setdefault(header, value)
        return response

    app.include_router(health.router)
    app.include_router(chat.router)

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


app = create_app()


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    cfg = get_settings()
    uvicorn.run("app.main:app", host=cfg.app_host, port=cfg.app_port, log_config=None)
