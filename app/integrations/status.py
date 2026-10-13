"""Shared integration status model reported by the UI and `/api/integrations`."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class IntegrationState(StrEnum):
    DISABLED = "disabled"  # disabled by configuration
    CONNECTING = "connecting"  # configured, startup checks still running
    CONNECTED = "connected"  # connected successfully
    UNAVAILABLE = "unavailable"  # configured but unavailable (network, permissions, rate limit, ...)
    AUTH_FAILED = "auth_failed"  # credentials rejected


@dataclass
class IntegrationStatus:
    state: IntegrationState
    detail: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"status": self.state.value}
        if self.detail:
            data["detail"] = self.detail
        data.update(self.extra)
        return data
