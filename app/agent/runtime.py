"""Claude Agent SDK runtime configured for OpenRouter with strict read-only tooling.

Each chat turn spawns the Claude Code CLI (bundled with the SDK) through
`ClaudeSDKClient`. Conversation context is preserved by resuming the session
transcript, which lives in a private, temporary CLAUDE_CONFIG_DIR that is deleted
on shutdown (history is intentionally lost on restart).
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
import warnings
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    CLIConnectionError,
    CLINotFoundError,
    ProcessError,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk.types import CanUseToolShadowedWarning, McpSdkServerConfig

from app.agent.permissions import DISALLOWED_BUILTIN_TOOLS, ToolPolicy, qualified_name
from app.agent.prompts import build_system_prompt
from app.agent.sessions import ChatSession
from app.agent.sources import SourceCollector
from app.config import Settings
from app.integrations.atlassian.mcp_client import tool_product
from app.integrations.github.mcp_server import TOOL_NAMES as GITHUB_TOOL_NAMES
from app.integrations.github.mcp_server import build_github_mcp_server
from app.integrations.registry import IntegrationRegistry
from app.logging_config import redact

logger = logging.getLogger(__name__)

# can_use_tool is a deliberate deny-by-default fallback next to dontAsk + allowed_tools.
warnings.filterwarnings("ignore", category=CanUseToolShadowedWarning)

Event = dict[str, Any]

_ERROR_MESSAGES = {
    "authentication_failed": ("llm_auth_failed",
                              "OpenRouter rejected the API key. Check OPENROUTER_API_KEY in your .env file."),
    "billing_error": ("llm_billing", "OpenRouter reports a billing problem or insufficient credits for this key."),
    "rate_limit": ("llm_rate_limited", "The model provider is rate limiting requests. Please wait a moment and retry."),
    "invalid_request": ("llm_invalid_request",
                        "OpenRouter rejected the request. Check that OPENROUTER_MODEL is a valid model id."),
    "server_error": ("llm_unavailable", "The model provider is temporarily unavailable. Please retry shortly."),
}

_STATUS_ERRORS = {401: "authentication_failed", 403: "authentication_failed", 402: "billing_error",
                  429: "rate_limit", 400: "invalid_request", 404: "invalid_request"}

_TOOL_LABELS = {
    "list_repositories": "List repositories",
    "get_repository_info": "Repository info",
    "get_file_contents": "Read file",
    "list_directory": "List directory",
    "get_repository_tree": "File tree",
    "list_pull_requests": "List pull requests",
    "get_pull_request": "Read pull request",
    "list_issues": "List issues",
    "get_issue": "Read issue",
    "list_commits": "List commits",
    "get_commit": "Read commit",
    "search_repository_content": "Search code",
}
_SUMMARY_KEYS = ("repository", "query", "path", "path_prefix", "number", "sha", "state", "jql", "cql",
                 "issueIdOrKey", "pageId", "id", "spaceKey", "spaceId")


def classify_error(code: str | None, status: int | None, model: str) -> Event:
    key = code if code in _ERROR_MESSAGES else _STATUS_ERRORS.get(status or 0)
    if key is None and status and status >= 500:
        key = "server_error"
    if key is None:
        return {"type": "error", "code": "llm_error", "message": "The model request failed. Please retry.",
                "retryable": True}
    error_code, message = _ERROR_MESSAGES[key]
    if key == "invalid_request":
        message = f"OpenRouter rejected the request. Check that OPENROUTER_MODEL ('{model}') is a valid model id."
    return {"type": "error", "code": error_code, "message": message,
            "retryable": key in {"rate_limit", "server_error"}}


def describe_tool(qualified: str) -> tuple[str, str]:
    """Return (integration, human label) for a fully qualified MCP tool name."""
    parts = qualified.split("__", 2)
    if len(parts) != 3:
        return "agent", qualified
    _, server, tool = parts
    if server == "github":
        return "github", _TOOL_LABELS.get(tool, tool)
    if server == "atlassian":
        product = tool_product(tool)
        return ("atlassian" if product == "shared" else product), tool
    return server, tool


def summarize_input(tool_input: dict[str, Any]) -> str:
    items = []
    for key in _SUMMARY_KEYS:
        value = tool_input.get(key)
        if isinstance(value, (str, int)) and str(value):
            items.append(f"{key}={str(value)[:90]}")
        if len(items) >= 3:
            break
    return redact(", ".join(items))


class TurnTranslator:
    """Translates Claude Agent SDK messages into the chat API's event protocol."""

    def __init__(self, session: ChatSession, model: str) -> None:
        self.session = session
        self.model = model
        self.text_emitted = False
        self.error_emitted = False
        self._streamed_messages: set[str] = set()
        self._current_message: str | None = None
        self._last_text_message: str | None = None

    def _text(self, text: str, message_id: str | None) -> list[Event]:
        if not text:
            return []
        events: list[Event] = []
        if self.text_emitted and message_id != self._last_text_message:
            events.append({"type": "delta", "text": "\n\n"})  # paragraph break between assistant messages
        self._last_text_message = message_id
        self.text_emitted = True
        events.append({"type": "delta", "text": text})
        return events

    def handle(self, message: Any) -> list[Event]:
        if isinstance(message, SystemMessage):
            if message.subtype == "init":
                self.session.started = True
            return []
        if isinstance(message, StreamEvent):
            return self._stream_event(message.event)
        if isinstance(message, AssistantMessage):
            return self._assistant(message)
        if isinstance(message, UserMessage):
            return self._tool_results(message)
        if isinstance(message, ResultMessage):
            return self._result(message)
        return []

    def _stream_event(self, event: dict[str, Any]) -> list[Event]:
        etype = event.get("type")
        if etype == "message_start":
            self._current_message = (event.get("message") or {}).get("id")
        elif etype == "content_block_delta":
            delta = event.get("delta") or {}
            if delta.get("type") == "text_delta":
                if self._current_message:
                    self._streamed_messages.add(self._current_message)
                return self._text(delta.get("text", ""), self._current_message)
        return []

    def _assistant(self, message: AssistantMessage) -> list[Event]:
        events: list[Event] = []
        if message.error:
            events.extend(self._error(message.error, None))
            return events
        if message.model == "<synthetic>":
            return events
        streamed = message.message_id in self._streamed_messages if message.message_id else bool(self._streamed_messages)
        for block in message.content:
            if isinstance(block, TextBlock) and not streamed:
                events.extend(self._text(block.text, message.message_id))
            elif isinstance(block, ToolUseBlock):
                integration, label = describe_tool(block.name)
                events.append({"type": "tool", "id": block.id, "status": "running", "integration": integration,
                               "name": block.name, "label": label, "input": summarize_input(block.input)})
        return events

    def _tool_results(self, message: UserMessage) -> list[Event]:
        events: list[Event] = []
        if not isinstance(message.content, list):
            return events
        for block in message.content:
            if isinstance(block, ToolResultBlock):
                event: Event = {"type": "tool", "id": block.tool_use_id,
                                "status": "error" if block.is_error else "done"}
                if block.is_error:
                    event["detail"] = redact(_content_text(block.content))[:240]
                events.append(event)
        return events

    def _error(self, code: str | None, status: int | None) -> list[Event]:
        if self.error_emitted:
            return []
        self.error_emitted = True
        return [classify_error(code, status, self.model)]

    def _result(self, message: ResultMessage) -> list[Event]:
        events: list[Event] = []
        if message.is_error and not self.error_emitted:
            if message.subtype == "error_max_turns":
                self.error_emitted = True
                events.append({"type": "error", "code": "max_turns",
                               "message": "The agent reached the maximum number of steps (AGENT_MAX_TURNS) before "
                                          "finishing. Try a more specific question.", "retryable": False})
            else:
                events.extend(self._error(None, message.api_error_status))
        usage = message.usage or {}
        events.append({
            "type": "usage",
            "model": self.model,
            "models_used": sorted((message.model_usage or {}).keys()),
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
            "num_turns": message.num_turns,
            "duration_ms": message.duration_ms,
            "sdk_estimated_cost_usd": message.total_cost_usd,
        })
        return events


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
    return ""


ClientFactory = Callable[[ClaudeAgentOptions], Any]


class AgentRuntime:
    def __init__(self, settings: Settings, registry: IntegrationRegistry,
                 client_factory: ClientFactory = ClaudeSDKClient) -> None:
        self.settings = settings
        self.registry = registry
        self._client_factory = client_factory
        self._config_dir = tempfile.mkdtemp(prefix="dsa-claude-config-")
        self._workdir = tempfile.mkdtemp(prefix="dsa-agent-cwd-")  # empty working directory

    def close(self) -> None:
        shutil.rmtree(self._config_dir, ignore_errors=True)
        shutil.rmtree(self._workdir, ignore_errors=True)

    def forget_session(self, session: ChatSession) -> None:
        """Best-effort removal of a session transcript from the private config dir."""
        import pathlib

        for path in pathlib.Path(self._config_dir).glob(f"projects/*/{session.claude_session_id}*"):
            with suppress(OSError):
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    path.unlink()

    # ------------------------------------------------------------------ options

    def cli_env(self) -> dict[str, str]:
        s = self.settings
        key = s.openrouter_api_key.get_secret_value() if s.openrouter_api_key else ""
        model = s.openrouter_model
        tool_timeout_ms = int(max(s.github_search_timeout_seconds, s.atlassian_timeout_seconds) + 20) * 1000
        return {
            # OpenRouter through the Anthropic-compatible API (ANTHROPIC_API_KEY must be empty).
            "ANTHROPIC_BASE_URL": s.openrouter_base_url,
            "ANTHROPIC_AUTH_TOKEN": key,
            "ANTHROPIC_API_KEY": "",
            # Every model alias the CLI might use internally must resolve to the configured OpenRouter id.
            "ANTHROPIC_MODEL": model,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
            "ANTHROPIC_SMALL_FAST_MODEL": model,
            "CLAUDE_CODE_SUBAGENT_MODEL": model,
            # Bounded retries and timeouts for the model API.
            "API_TIMEOUT_MS": str(s.llm_request_timeout_seconds * 1000),
            "CLAUDE_CODE_MAX_RETRIES": str(s.llm_max_retries),
            "MCP_TOOL_TIMEOUT": str(tool_timeout_ms),
            "MAX_MCP_OUTPUT_TOKENS": "30000",
            # Isolation: private config dir, no user settings/memory/telemetry/extra traffic.
            "CLAUDE_CONFIG_DIR": self._config_dir,
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_TELEMETRY": "1",
            "DISABLE_ERROR_REPORTING": "1",
            "DISABLE_AUTOUPDATER": "1",
            "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY": "1",
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
            "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
            "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
            "ENABLE_TOOL_SEARCH": "false",
            # Credentials the CLI must never see (they are used only inside this Python process).
            "OPENROUTER_API_KEY": "",
            "GITHUB_TOKEN": "",
            "GH_TOKEN": "",
            "ATLASSIAN_SERVICE_ACCOUNT_API_KEY": "",
            "CLAUDE_CODE_OAUTH_TOKEN": "",
        }

    def build_options(self, session: ChatSession, collector: SourceCollector) -> tuple[ClaudeAgentOptions, ToolPolicy]:
        mcp_servers: dict[str, McpSdkServerConfig] = {}
        approved: list[str] = []
        registry = self.registry
        if registry.github is not None:
            server = build_github_mcp_server(registry.github, registry.search_limits, collector)
            mcp_servers["github"] = McpSdkServerConfig(type="sdk", name="github", instance=server)
            approved += [qualified_name("github", name) for name in GITHUB_TOOL_NAMES]
        if registry.atlassian is not None and registry.atlassian_tools_enabled():
            server = registry.atlassian.build_mcp_server(collector)
            mcp_servers["atlassian"] = McpSdkServerConfig(type="sdk", name="atlassian", instance=server)
            approved += [qualified_name("atlassian", name) for name in registry.atlassian.exposed_tool_names()]
        policy = ToolPolicy(approved)
        options = ClaudeAgentOptions(
            model=self.settings.openrouter_model,
            system_prompt=build_system_prompt(registry.prompt_context()),
            tools=[],  # disable every built-in Claude Code tool (Bash, Read, Write, WebFetch, ...)
            allowed_tools=policy.approved,
            disallowed_tools=list(DISALLOWED_BUILTIN_TOOLS),
            permission_mode="dontAsk",  # anything not explicitly allowed is denied
            can_use_tool=policy.can_use_tool,
            hooks=policy.hooks(),  # type: ignore[arg-type]
            mcp_servers=mcp_servers,  # type: ignore[arg-type]
            strict_mcp_config=True,
            setting_sources=[],
            verbatim_prompts=True,  # no @file expansion or slash commands from user text
            max_turns=self.settings.agent_max_turns,
            cwd=self._workdir,
            env=self.cli_env(),
            include_partial_messages=True,
            session_id=None if session.started else session.claude_session_id,
            resume=session.claude_session_id if session.started else None,
            stderr=_log_cli_stderr,
        )
        return options, policy

    # ------------------------------------------------------------------ turns

    async def run_turn(self, session: ChatSession, message: str,
                       content: list[dict[str, Any]] | None = None) -> AsyncIterator[Event]:
        """Run one agent turn, yielding chat events. Never raises for agent/tool failures.

        `content` optionally carries Anthropic content blocks (text, image, document) for
        messages with attachments; otherwise the plain `message` string is sent.
        """
        queue: asyncio.Queue[Event | None] = asyncio.Queue()
        producer = asyncio.create_task(self._produce(session, message, content, queue.put_nowait))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.settings.agent_timeout_seconds
        try:
            while True:
                remaining = deadline - loop.time()
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=max(0.01, remaining))
                except TimeoutError:
                    yield {"type": "error", "code": "timeout", "retryable": True,
                           "message": f"The agent did not finish within {self.settings.agent_timeout_seconds}s "
                                      "(AGENT_TIMEOUT_SECONDS). Partial results are shown above."}
                    break
                if event is None:
                    break
                yield event
        finally:
            if not producer.done():
                producer.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await producer

    async def _produce(self, session: ChatSession, message: str, content: list[dict[str, Any]] | None,
                       emit: Callable[[Event | None], None]) -> None:
        collector = SourceCollector(on_new=lambda source: emit({"type": "source", **source.to_dict()}))
        translator = TurnTranslator(session, self.settings.openrouter_model)
        try:
            for attempt in range(2):
                received_any = False
                try:
                    options, _ = self.build_options(session, collector)
                    async with self._client_factory(options) as client:
                        await client.query(message if content is None else _single_message(content))
                        async for sdk_message in client.receive_response():
                            received_any = True
                            for event in translator.handle(sdk_message):
                                emit(event)
                    return
                except (CLIConnectionError, ProcessError) as exc:
                    text = str(exc)
                    if session.started and "No conversation found" in text and attempt == 0:
                        # The transcript is gone (e.g. evicted); continue in a fresh runtime session.
                        logger.warning("agent transcript missing; starting a fresh runtime session")
                        session.reset_runtime_session()
                        emit({"type": "notice", "message": "Earlier context of this conversation was lost; "
                                                           "continuing without it."})
                        continue
                    if not received_any and attempt == 0 and not isinstance(exc, CLINotFoundError):
                        logger.warning("agent runtime failed to start; retrying once", extra={"error": redact(text)[:300]})
                        await asyncio.sleep(1.0)
                        continue
                    raise
        except CLINotFoundError:
            logger.error("claude code cli not found")
            emit({"type": "error", "code": "runtime_missing", "retryable": False,
                  "message": "The Claude Agent SDK runtime (Claude Code CLI) was not found. Reinstall dependencies."})
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the chat must never crash on agent failures
            logger.exception("agent turn failed")
            if not translator.error_emitted:
                emit({"type": "error", "code": "agent_error", "retryable": True,
                      "message": f"The agent failed while processing the request ({type(exc).__name__})."})
        finally:
            emit(None)


async def _single_message(content: list[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
    yield {"type": "user", "message": {"role": "user", "content": content}, "parent_tool_use_id": None}


def _log_cli_stderr(line: str) -> None:
    logger.debug("claude cli", extra={"stderr": redact(line)[:500]})
