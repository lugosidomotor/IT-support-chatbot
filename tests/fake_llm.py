"""A tiny fake Anthropic Messages API used to drive the real Claude Code CLI deterministically.

It lets the agent tests exercise the complete pipeline (CLI subprocess, MCP servers,
permission hooks, streaming) without any credentials or network access.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Callable
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

# A scripted reply is a list of blocks: {"text": "..."} or {"tool": "name", "input": {...}}
Script = Callable[[dict[str, Any]], list[dict[str, Any]]]


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


class FakeAnthropic:
    def __init__(self, script: Script, model: str = "anthropic/claude-sonnet-5.5") -> None:
        self.script = script
        self.model = model
        self.requests: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self._counter = 0
        self.port = _free_port()
        app = Starlette(routes=[
            Route("/v1/messages", self._messages, methods=["POST"]),
            Route("/v1/messages/count_tokens", self._count, methods=["POST"]),
            Route("/v1/models", self._models, methods=["GET"]),
            Route("/v1/key", self._key, methods=["GET"]),
        ])
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning", lifespan="off")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> FakeAnthropic:
        self._thread.start()
        deadline = time.time() + 10
        while not self._server.started and time.time() < deadline:
            time.sleep(0.02)
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)

    @property
    def message_requests(self) -> list[dict[str, Any]]:
        """Main-loop requests (those that carry the tool list)."""
        return [r for r in self.requests if r.get("tools") is not None]

    async def _models(self, request: Request) -> Response:
        return JSONResponse({"data": [{"id": self.model}]})

    async def _key(self, request: Request) -> Response:
        return JSONResponse({"data": {"label": "fake"}})

    async def _count(self, request: Request) -> Response:
        return JSONResponse({"input_tokens": 42})

    async def _messages(self, request: Request) -> Response:
        body = await request.json()
        self.requests.append(body)
        self.headers.append({k.lower(): v for k, v in request.headers.items()})
        blocks = self.script(body)
        self._counter += 1
        msg_id = f"msg_fake_{self._counter}"
        stop_reason = "tool_use" if any("tool" in b for b in blocks) else "end_turn"
        if not body.get("stream"):
            content = []
            for i, b in enumerate(blocks):
                if "tool" in b:
                    content.append({"type": "tool_use", "id": f"toolu_{self._counter}_{i}", "name": b["tool"],
                                    "input": b.get("input", {})})
                else:
                    content.append({"type": "text", "text": b["text"]})
            return JSONResponse({"id": msg_id, "type": "message", "role": "assistant", "model": self.model,
                                 "content": content, "stop_reason": stop_reason, "stop_sequence": None,
                                 "usage": {"input_tokens": 100, "output_tokens": 20}})

        def stream() -> Any:
            yield _sse("message_start", {"type": "message_start", "message": {
                "id": msg_id, "type": "message", "role": "assistant", "model": self.model, "content": [],
                "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 100, "output_tokens": 1}}})
            for i, b in enumerate(blocks):
                if "tool" in b:
                    yield _sse("content_block_start", {"type": "content_block_start", "index": i, "content_block": {
                        "type": "tool_use", "id": f"toolu_{self._counter}_{i}", "name": b["tool"], "input": {}}})
                    yield _sse("content_block_delta", {"type": "content_block_delta", "index": i, "delta": {
                        "type": "input_json_delta", "partial_json": json.dumps(b.get("input", {}))}})
                else:
                    yield _sse("content_block_start", {"type": "content_block_start", "index": i,
                                                       "content_block": {"type": "text", "text": ""}})
                    text = b["text"]
                    for start in range(0, len(text), 12):
                        yield _sse("content_block_delta", {"type": "content_block_delta", "index": i, "delta": {
                            "type": "text_delta", "text": text[start:start + 12]}})
                yield _sse("content_block_stop", {"type": "content_block_stop", "index": i})
            yield _sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop_reason,
                                                                             "stop_sequence": None},
                                         "usage": {"output_tokens": 20}})
            yield _sse("message_stop", {"type": "message_stop"})

        return StreamingResponse(stream(), media_type="text/event-stream")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def last_user_content(body: dict[str, Any]) -> list[dict[str, Any]]:
    for message in reversed(body.get("messages", [])):
        if message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str):
                return [{"type": "text", "text": content}]
            return content or []
    return []


def user_texts(body: dict[str, Any]) -> list[str]:
    texts = []
    for message in body.get("messages", []):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            texts.append(content)
        else:
            texts.extend(b.get("text", "") for b in content if b.get("type") == "text")
    return texts
