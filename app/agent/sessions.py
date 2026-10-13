"""In-memory chat session store (history is lost on restart by design)."""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.attachments import Attachment

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def is_valid_session_id(value: str) -> bool:
    return bool(_SESSION_ID_RE.match(value or ""))


@dataclass
class ChatSession:
    id: str
    claude_session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    started: bool = False  # True once the agent runtime has a transcript to resume
    turns: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    attachments: dict[str, Attachment] = field(default_factory=dict)  # uploaded, not yet sent

    def reset_runtime_session(self) -> None:
        self.claude_session_id = str(uuid.uuid4())
        self.started = False


class SessionStore:
    def __init__(self, max_sessions: int = 200, idle_ttl_seconds: float = 7_200) -> None:
        self._sessions: OrderedDict[str, ChatSession] = OrderedDict()
        self.max_sessions = max_sessions
        self.idle_ttl = idle_ttl_seconds

    def _evict(self) -> list[ChatSession]:
        now = time.time()
        evicted = [s for s in self._sessions.values() if now - s.last_active > self.idle_ttl and not s.lock.locked()]
        for session in evicted:
            self._sessions.pop(session.id, None)
        while len(self._sessions) >= self.max_sessions:
            oldest = next((s for s in self._sessions.values() if not s.lock.locked()), None)
            if oldest is None:
                break
            self._sessions.pop(oldest.id, None)
            evicted.append(oldest)
        return evicted

    def create(self) -> ChatSession:
        self._evict()
        session = ChatSession(id=uuid.uuid4().hex)
        self._sessions[session.id] = session
        return session

    def get(self, session_id: str) -> ChatSession | None:
        session = self._sessions.get(session_id)
        if session is None:
            return None
        if time.time() - session.last_active > self.idle_ttl and not session.lock.locked():
            self._sessions.pop(session_id, None)
            return None
        session.last_active = time.time()
        self._sessions.move_to_end(session_id)
        return session

    def delete(self, session_id: str) -> ChatSession | None:
        return self._sessions.pop(session_id, None)

    def __len__(self) -> int:
        return len(self._sessions)
