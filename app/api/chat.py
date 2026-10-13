from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import APIRouter, Request, Response, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.services.attachments import MAX_UPLOAD_BYTES
from app.services.chat_service import ChatError, ChatService, format_sse

router = APIRouter(prefix="/api")


class ChatRequest(BaseModel):
    session_id: str | None = Field(default=None, max_length=64)
    message: str = Field(default="", max_length=100_000)
    attachments: list[str] = Field(default_factory=list, max_length=10)
    stream: bool | None = None


def _service(request: Request) -> ChatService:
    return request.app.state.chat_service


def _error(exc: ChatError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"error": exc.code, "message": exc.message})


@router.post("/sessions", status_code=201)
async def create_session(request: Request) -> dict[str, str]:
    session = _service(request).create_session()
    return {"session_id": session.id}


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(session_id: str, request: Request) -> Response:
    if not _service(request).delete_session(session_id):
        return JSONResponse(status_code=404, content={"error": "session_not_found", "message": "Session not found."})
    return Response(status_code=204)


@router.post("/sessions/{session_id}/attachments", status_code=201)
async def upload_attachment(session_id: str, file: UploadFile, request: Request) -> Response:
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    try:
        attachment = _service(request).add_attachment(session_id, file.filename, data)
    except ChatError as exc:
        return _error(exc)
    finally:
        await file.close()
    return JSONResponse(status_code=201, content=attachment.public())


@router.delete("/sessions/{session_id}/attachments/{attachment_id}", status_code=204)
async def delete_attachment(session_id: str, attachment_id: str, request: Request) -> Response:
    try:
        removed = _service(request).remove_attachment(session_id, attachment_id)
    except ChatError as exc:
        return _error(exc)
    if not removed:
        return JSONResponse(status_code=404, content={"error": "attachment_not_found", "message": "Not found."})
    return Response(status_code=204)


@router.post("/chat")
async def chat(body: ChatRequest, request: Request) -> Response:
    service = _service(request)
    try:
        session = service.prepare(body.session_id, body.message, body.attachments)
        attachments = service.take_attachments(session, body.attachments) if body.attachments else []
    except ChatError as exc:
        return _error(exc)

    wants_stream = body.stream if body.stream is not None else "text/event-stream" in request.headers.get("accept", "")
    if not wants_stream:
        return JSONResponse(await service.complete(session, body.message, attachments))

    async def events() -> AsyncIterator[str]:
        async for event in service.stream(session, body.message, attachments):
            yield format_sse(event)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
