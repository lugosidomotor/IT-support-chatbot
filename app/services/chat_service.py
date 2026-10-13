"""Chat orchestration: sessions, validation, and the streaming event protocol."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

from app.agent.runtime import AgentRuntime, Event
from app.agent.sessions import ChatSession, SessionStore, is_valid_session_id
from app.config import Settings
from app.integrations.registry import IntegrationRegistry
from app.services.attachments import (
    MAX_PER_MESSAGE,
    Attachment,
    AttachmentError,
    build_attachment,
    build_user_content,
    check_session_quota,
)

logger = logging.getLogger(__name__)


class ChatError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def format_sse(event: Event) -> str:
    """Server-Sent Events framing: `event: <type>` + one JSON `data:` line."""
    return f"event: {event.get('type', 'message')}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


class ChatService:
    def __init__(self, settings: Settings, registry: IntegrationRegistry, runtime: AgentRuntime,
                 sessions: SessionStore) -> None:
        self.settings = settings
        self.registry = registry
        self.runtime = runtime
        self.sessions = sessions

    def create_session(self) -> ChatSession:
        return self.sessions.create()

    def delete_session(self, session_id: str) -> bool:
        if not is_valid_session_id(session_id):
            return False
        session = self.sessions.delete(session_id)
        if session is None:
            return False
        self.runtime.forget_session(session)
        return True

    def get_session(self, session_id: str) -> ChatSession:
        if not is_valid_session_id(session_id):
            raise ChatError(422, "invalid_session_id", "Invalid session id.")
        session = self.sessions.get(session_id)
        if session is None:
            raise ChatError(404, "session_not_found",
                            "Session not found (it may have expired or the server restarted). Start a new conversation.")
        return session

    def add_attachment(self, session_id: str, filename: str | None, data: bytes) -> Attachment:
        session = self.get_session(session_id)
        try:
            attachment = build_attachment(filename, data)
            check_session_quota(session.attachments, attachment)
        except AttachmentError as exc:
            raise ChatError(exc.status_code, "invalid_attachment", exc.message) from None
        session.attachments[attachment.id] = attachment
        logger.info("attachment uploaded", extra={"session": session.id, "kind": attachment.kind,
                                                  "size": attachment.size})
        return attachment

    def remove_attachment(self, session_id: str, attachment_id: str) -> bool:
        return self.get_session(session_id).attachments.pop(attachment_id, None) is not None

    def take_attachments(self, session: ChatSession, ids: list[str]) -> list[Attachment]:
        if len(ids) > MAX_PER_MESSAGE:
            raise ChatError(413, "too_many_attachments", f"At most {MAX_PER_MESSAGE} attachments per message.")
        missing = [i for i in ids if i not in session.attachments]
        if missing:
            raise ChatError(422, "attachment_not_found", "An attachment was not found; please upload it again.")
        return [session.attachments.pop(i) for i in dict.fromkeys(ids)]

    def prepare(self, session_id: str | None, message: str, attachment_ids: list[str] | None = None) -> ChatSession:
        """Validate a chat request and resolve its session (raises ChatError)."""
        text = (message or "").strip()
        if not text and attachment_ids:
            text = "Please analyse the attached file(s)."
        if not text:
            raise ChatError(422, "empty_message", "Message must not be empty.")
        if len(text) > self.settings.max_message_chars:
            raise ChatError(413, "message_too_long",
                            f"Message exceeds the maximum of {self.settings.max_message_chars} characters.")
        if session_id is None:
            if attachment_ids:
                raise ChatError(422, "attachment_not_found", "Attachments require an existing session.")
            return self.sessions.create()
        session = self.get_session(session_id)
        if session.lock.locked():
            raise ChatError(409, "session_busy", "This conversation is still processing the previous message.")
        return session

    async def stream(self, session: ChatSession, message: str,
                     attachments: list[Attachment] | None = None) -> AsyncIterator[Event]:
        started = time.monotonic()
        message = message.strip() or "Please analyse the attached file(s)."
        content = build_user_content(message, attachments) if attachments else None
        async with session.lock:
            yield {"type": "session", "session_id": session.id, "model": self.settings.openrouter_model}
            llm = self.registry.llm
            if not llm.usable:
                yield {"type": "error", "code": f"llm_{llm.state}", "message": llm.detail or "LLM is not configured.",
                       "retryable": False}
                yield {"type": "done", "session_id": session.id}
                return
            had_error = False
            async for event in self.runtime.run_turn(session, message, content):
                had_error = had_error or event.get("type") == "error"
                yield event
            session.turns += 1
            logger.info("chat turn finished", extra={"session": session.id, "turn": session.turns,
                                                     "error": had_error,
                                                     "elapsed_ms": int((time.monotonic() - started) * 1000)})
            yield {"type": "done", "session_id": session.id}

    async def complete(self, session: ChatSession, message: str,
                       attachments: list[Attachment] | None = None) -> dict[str, Any]:
        """Non-streaming variant: aggregate the event stream into one JSON document."""
        answer: list[str] = []
        tools: dict[str, dict[str, Any]] = {}
        result: dict[str, Any] = {"session_id": session.id, "sources": [], "errors": [], "notices": []}
        async for event in self.stream(session, message, attachments):
            etype = event["type"]
            if etype == "delta":
                answer.append(event["text"])
            elif etype == "tool":
                tools.setdefault(event["id"], {}).update({k: v for k, v in event.items() if k != "type"})
            elif etype == "source":
                result["sources"].append({k: v for k, v in event.items() if k != "type"})
            elif etype == "usage":
                result["usage"] = {k: v for k, v in event.items() if k != "type"}
            elif etype == "error":
                result["errors"].append({k: v for k, v in event.items() if k != "type"})
            elif etype == "notice":
                result["notices"].append(event["message"])
            elif etype == "session":
                result["model"] = event.get("model")
        result["answer"] = "".join(answer)
        result["tools"] = list(tools.values())
        return result
