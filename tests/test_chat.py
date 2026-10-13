from __future__ import annotations

import json
from typing import Any

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from fastapi.testclient import TestClient

from app.agent.runtime import TurnTranslator, classify_error
from app.agent.sessions import ChatSession
from app.integrations.registry import IntegrationRegistry
from app.main import create_app
from app.services.chat_service import format_sse
from tests.conftest import GITHUB_TOKEN, OPENROUTER_KEY
from tests.fakes import FakeGitHub


def result_message(**kwargs: Any) -> ResultMessage:
    base = dict(subtype="success", duration_ms=1200, duration_api_ms=900, is_error=False, num_turns=1,
                session_id="x", usage={"input_tokens": 10, "output_tokens": 5}, model_usage={"m": {}})
    base.update(kwargs)
    return ResultMessage(**base)


class FakeSDKClient:
    """Stands in for ClaudeSDKClient; replays a scripted list of SDK messages."""

    instances: list[FakeSDKClient] = []
    script: list[Any] = []
    fail_with: Exception | None = None

    def __init__(self, options: Any) -> None:
        self.options = options
        self.prompts: list[str] = []
        FakeSDKClient.instances.append(self)

    async def __aenter__(self) -> FakeSDKClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def query(self, prompt: Any) -> None:
        if not isinstance(prompt, str):
            prompt = [message async for message in prompt]
        self.prompts.append(prompt)

    async def receive_response(self):
        for item in FakeSDKClient.script:
            yield item
        if FakeSDKClient.fail_with is not None:
            raise FakeSDKClient.fail_with


@pytest.fixture
def fake_sdk():
    FakeSDKClient.instances = []
    FakeSDKClient.script = [
        SystemMessage(subtype="init", data={}),
        AssistantMessage(content=[TextBlock("Checking the code.")], model="m", message_id="m1"),
        AssistantMessage(content=[ToolUseBlock(id="t1", name="mcp__github__get_file_contents",
                                               input={"repository": "acme/backend", "path": "app/auth.py"})],
                         model="m", message_id="m1"),
        UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="{}", is_error=False)]),
        AssistantMessage(content=[TextBlock("Auth is in [app/auth.py](https://github.com/acme/backend).")],
                         model="m", message_id="m2"),
        result_message(num_turns=2),
    ]
    FakeSDKClient.fail_with = None
    return FakeSDKClient


@pytest.fixture
def client(make_settings, fake_sdk):
    settings = make_settings(openrouter_api_key=OPENROUTER_KEY, github_repos="acme/backend")
    app = create_app(settings, client_factory=fake_sdk,
                     registry_factory=lambda s: IntegrationRegistry(s, github_transport=FakeGitHub().transport()))
    with TestClient(app) as test_client:
        yield test_client


def parse_sse(text: str) -> list[dict[str, Any]]:
    events = []
    for frame in text.strip().split("\n\n"):
        lines = frame.split("\n")
        assert lines[0].startswith("event: ")
        data = json.loads(lines[1].removeprefix("data: "))
        assert data["type"] == lines[0].removeprefix("event: ")
        events.append(data)
    return events


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_integration_status(client):
    data = client.get("/api/integrations").json()
    assert data["github"]["status"] == "connected"
    assert data["github"]["authentication"] == "anonymous"
    assert data["github"]["repositories"] == ["acme/backend"]
    assert data["jira"]["status"] == "disabled" and data["confluence"]["status"] == "disabled"
    assert data["llm"]["model"] == "anthropic/claude-sonnet-5.5"
    assert data["has_data_sources"] is True


def test_index_and_security_headers(client):
    response = client.get("/")
    assert response.status_code == 200 and "Developer Support" in response.text
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert client.get("/static/marvin.js").status_code == 200


def test_session_lifecycle(client):
    session_id = client.post("/api/sessions").json()["session_id"]
    assert client.delete(f"/api/sessions/{session_id}").status_code == 204
    assert client.delete(f"/api/sessions/{session_id}").status_code == 404
    response = client.post("/api/chat", json={"session_id": session_id, "message": "hi"})
    assert response.status_code == 404 and response.json()["error"] == "session_not_found"


def test_chat_streaming_event_format(client, fake_sdk):
    session_id = client.post("/api/sessions").json()["session_id"]
    response = client.post("/api/chat", json={"session_id": session_id, "message": "How does auth work?"},
                           headers={"Accept": "text/event-stream"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(response.text)
    types = [e["type"] for e in events]
    assert types[0] == "session" and types[-1] == "done"
    assert "usage" in types
    text = "".join(e["text"] for e in events if e["type"] == "delta")
    assert text == "Checking the code.\n\nAuth is in [app/auth.py](https://github.com/acme/backend)."
    tools = [e for e in events if e["type"] == "tool"]
    assert tools[0]["status"] == "running" and tools[0]["integration"] == "github"
    assert tools[0]["label"] == "Read file" and "acme/backend" in tools[0]["input"]
    assert tools[1] == {"type": "tool", "id": "t1", "status": "done"}
    assert fake_sdk.instances[-1].prompts == ["How does auth work?"]


def test_chat_non_streaming_json(client):
    data = client.post("/api/chat", json={"message": "hello", "stream": False}).json()
    assert data["answer"].startswith("Checking the code.")
    assert data["usage"]["num_turns"] == 2
    assert data["tools"][0]["status"] == "done"
    assert data["errors"] == []


def test_chat_validation(client):
    assert client.post("/api/chat", json={"message": "   "}).status_code == 422
    assert client.post("/api/chat", json={"message": "x" * 9000}).status_code == 413
    assert client.post("/api/chat", json={"session_id": "../../etc", "message": "x"}).status_code == 422


def test_conversation_context_and_session_isolation(client, fake_sdk):
    a = client.post("/api/sessions").json()["session_id"]
    b = client.post("/api/sessions").json()["session_id"]
    client.post("/api/chat", json={"session_id": a, "message": "first", "stream": False})
    client.post("/api/chat", json={"session_id": a, "message": "second", "stream": False})
    client.post("/api/chat", json={"session_id": b, "message": "other", "stream": False})
    first, second, other = (i.options for i in fake_sdk.instances[-3:])
    assert first.session_id and first.resume is None
    assert second.resume == first.session_id and second.session_id is None  # same transcript continued
    assert other.session_id not in (first.session_id, None) and other.resume is None  # isolated


def test_tool_failure_does_not_crash_chat(client, fake_sdk):
    fake_sdk.script = [
        SystemMessage(subtype="init", data={}),
        AssistantMessage(content=[ToolUseBlock(id="t9", name="mcp__github__search_repository_content",
                                               input={"repository": "acme/backend", "query": "x"})], model="m"),
        UserMessage(content=[ToolResultBlock(tool_use_id="t9", is_error=True,
                                             content=[{"type": "text", "text": '{"error": "rate_limited"}'}])]),
    ]
    fake_sdk.fail_with = RuntimeError("boom")
    events = parse_sse(client.post("/api/chat", json={"message": "x"}, headers={"Accept": "text/event-stream"}).text)
    tool_error = [e for e in events if e["type"] == "tool" and e["status"] == "error"]
    assert tool_error and "rate_limited" in tool_error[0]["detail"]
    errors = [e for e in events if e["type"] == "error"]
    assert errors[0]["code"] == "agent_error" and errors[0]["retryable"]
    assert events[-1]["type"] == "done"
    assert client.get("/health").status_code == 200


def test_llm_not_configured_returns_clear_error(make_settings, fake_sdk):
    app = create_app(make_settings(), client_factory=fake_sdk)
    with TestClient(app) as c:
        events = parse_sse(c.post("/api/chat", json={"message": "hi"}, headers={"Accept": "text/event-stream"}).text)
    assert [e["type"] for e in events] == ["session", "error", "done"]
    assert events[1]["code"] == "llm_not_configured" and "OPENROUTER_API_KEY" in events[1]["message"]
    assert fake_sdk.instances == []  # the agent runtime is never started


def test_secrets_never_in_responses_or_logs(make_settings, fake_sdk, caplog):
    caplog.set_level("DEBUG")
    settings = make_settings(openrouter_api_key=OPENROUTER_KEY, github_repos="acme/backend", github_token=GITHUB_TOKEN)
    app = create_app(settings, client_factory=fake_sdk,
                     registry_factory=lambda s: IntegrationRegistry(s, github_transport=FakeGitHub().transport()))
    with TestClient(app) as c:
        bodies = [c.get("/api/integrations").text,
                  c.post("/api/chat", json={"message": "hi"}, headers={"Accept": "text/event-stream"}).text]
    from app.logging_config import JsonFormatter

    formatted = "\n".join(JsonFormatter().format(r) for r in caplog.records)
    for secret in (OPENROUTER_KEY, GITHUB_TOKEN):
        assert all(secret not in body for body in bodies)
        assert secret not in formatted


# ---------------------------------------------------------------------- translator units


def test_translator_streams_deltas_without_duplication():
    session = ChatSession(id="s" * 16)
    tr = TurnTranslator(session, "m")
    events = []
    for message in [
        SystemMessage(subtype="init", data={}),
        StreamEvent(uuid="u", session_id="x", event={"type": "message_start", "message": {"id": "m1"}}),
        StreamEvent(uuid="u", session_id="x", event={"type": "content_block_delta",
                                                     "delta": {"type": "text_delta", "text": "Hel"}}),
        StreamEvent(uuid="u", session_id="x", event={"type": "content_block_delta",
                                                     "delta": {"type": "text_delta", "text": "lo"}}),
        AssistantMessage(content=[TextBlock("Hello")], model="m", message_id="m1"),
    ]:
        events += tr.handle(message)
    assert session.started
    assert "".join(e["text"] for e in events if e["type"] == "delta") == "Hello"


@pytest.mark.parametrize(("code", "status", "expected"), [
    ("authentication_failed", None, "llm_auth_failed"),
    (None, 401, "llm_auth_failed"),
    ("rate_limit", None, "llm_rate_limited"),
    (None, 429, "llm_rate_limited"),
    (None, 402, "llm_billing"),
    ("invalid_request", None, "llm_invalid_request"),
    (None, 529, "llm_unavailable"),
    (None, None, "llm_error"),
])
def test_provider_error_mapping(code, status, expected):
    event = classify_error(code, status, "anthropic/claude-sonnet-5.5")
    assert event["code"] == expected and event["message"]


def test_synthetic_error_message_is_not_shown_as_answer():
    tr = TurnTranslator(ChatSession(id="s" * 16), "m")
    events = tr.handle(AssistantMessage(content=[TextBlock("Failed to authenticate. API Error: 401")],
                                        model="<synthetic>", error="authentication_failed"))
    events += tr.handle(result_message(is_error=True, api_error_status=401))
    assert [e["type"] for e in events] == ["error", "usage"]
    assert events[0]["code"] == "llm_auth_failed"


def test_max_turns_reported():
    tr = TurnTranslator(ChatSession(id="s" * 16), "m")
    events = tr.handle(result_message(is_error=True, subtype="error_max_turns"))
    assert events[0]["code"] == "max_turns"


def test_format_sse():
    assert format_sse({"type": "delta", "text": "á\n"}) == 'event: delta\ndata: {"type": "delta", "text": "á\\n"}\n\n'


# ---------------------------------------------------------------------- attachments

PNG = bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 64
PDF = b"%PDF-1.4\n%fake\n%%EOF"


def test_upload_and_send_attachments(client, fake_sdk):
    session_id = client.post("/api/sessions").json()["session_id"]
    image = client.post(f"/api/sessions/{session_id}/attachments", files={"file": ("shot.png", PNG, "image/png")})
    log = client.post(f"/api/sessions/{session_id}/attachments",
                      files={"file": ("app.log", b"ERROR 403 forbidden\n", "text/plain")})
    pdf = client.post(f"/api/sessions/{session_id}/attachments", files={"file": ("spec.pdf", PDF, "application/pdf")})
    assert [r.status_code for r in (image, log, pdf)] == [201, 201, 201]
    assert (image.json()["kind"], log.json()["kind"], pdf.json()["kind"]) == ("image", "text", "pdf")
    ids = [r.json()["id"] for r in (image, log, pdf)]
    response = client.post("/api/chat", json={"session_id": session_id, "message": "What is wrong?",
                                              "attachments": ids, "stream": False})
    assert response.status_code == 200
    [message] = fake_sdk.instances[-1].prompts[-1]
    blocks = message["message"]["content"]
    assert [b["type"] for b in blocks] == ["text", "text", "image", "text", "document"]
    assert "ERROR 403 forbidden" in blocks[3]["text"]
    # attachments are consumed by the message that used them
    again = client.post("/api/chat", json={"session_id": session_id, "message": "x", "attachments": ids[:1]})
    assert again.status_code == 422 and again.json()["error"] == "attachment_not_found"


def test_attachment_only_message_gets_default_prompt(client, fake_sdk):
    session_id = client.post("/api/sessions").json()["session_id"]
    att = client.post(f"/api/sessions/{session_id}/attachments", files={"file": ("a.png", PNG, "image/png")}).json()
    client.post("/api/chat", json={"session_id": session_id, "message": "", "attachments": [att["id"]], "stream": False})
    [message] = fake_sdk.instances[-1].prompts[-1]
    assert message["message"]["content"][0]["text"] == "Please analyse the attached file(s)."


@pytest.mark.parametrize(("name", "data", "status"), [
    ("tool.exe", b"MZ\x90\x00" + bytes(range(256)) * 4, 415),
    ("archive.zip", b"PK\x03\x04" + bytes(range(256)) * 4, 415),
    ("empty.txt", b"", 422),
    ("huge.png", bytes.fromhex("89504e470d0a1a0a") + b"\x00" * (6 * 1024 * 1024), 413),
])
def test_invalid_uploads_are_rejected(client, name, data, status):
    session_id = client.post("/api/sessions").json()["session_id"]
    response = client.post(f"/api/sessions/{session_id}/attachments", files={"file": (name, data, "image/png")})
    assert response.status_code == status
    assert response.json()["error"] == "invalid_attachment"


def test_upload_requires_known_session(client):
    response = client.post("/api/sessions/deadbeefdeadbeef/attachments", files={"file": ("a.txt", b"hi", "text/plain")})
    assert response.status_code == 404


def test_attachment_can_be_removed(client):
    session_id = client.post("/api/sessions").json()["session_id"]
    att = client.post(f"/api/sessions/{session_id}/attachments", files={"file": ("a.txt", b"hi", "text/plain")}).json()
    assert client.delete(f"/api/sessions/{session_id}/attachments/{att['id']}").status_code == 204
    assert client.delete(f"/api/sessions/{session_id}/attachments/{att['id']}").status_code == 404


def test_filenames_are_sanitized():
    from app.services.attachments import build_attachment

    assert build_attachment("../../etc/<script>.log", b"x").name == "_script_.log"
