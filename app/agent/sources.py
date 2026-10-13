"""Collects source references produced by tool calls during one agent turn.

Sources come only from actual tool results (never from model text), so every
reference shown in the UI points at something the agent really retrieved.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class Source:
    title: str
    url: str
    integration: str  # github | jira | confluence | atlassian
    kind: str  # file, repository, pull_request, issue, page, ...

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


class SourceCollector:
    def __init__(self, on_new: Callable[[Source], None] | None = None, limit: int = 60) -> None:
        self._seen: dict[str, Source] = {}
        self._on_new = on_new
        self._limit = limit

    def add(self, title: str, url: str | None, integration: str, kind: str) -> None:
        if not url or len(self._seen) >= self._limit:
            return
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            return
        key = url.split("#", 1)[0]
        if key in self._seen:
            return
        source = Source(title=(title or url)[:200], url=url, integration=integration, kind=kind)
        self._seen[key] = source
        if self._on_new:
            self._on_new(source)

    @property
    def sources(self) -> list[Source]:
        return list(self._seen.values())
