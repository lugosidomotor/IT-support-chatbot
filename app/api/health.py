from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/api/integrations")
async def integrations(request: Request) -> dict[str, Any]:
    return request.app.state.registry.to_dict()
