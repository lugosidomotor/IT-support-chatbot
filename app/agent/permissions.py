"""Technical read-only enforcement for the agent runtime.

Layers (all deny-by-default):
1. Tool surface: built-in Claude Code tools are disabled (`tools=[]`) and only the
   in-process GitHub and filtered Atlassian MCP servers are registered.
2. CLI permission rules: `permission_mode="dontAsk"` denies anything that is not
   explicitly listed in `allowed_tools`; dangerous built-ins are also listed in
   `disallowed_tools` so they are removed from the model context.
3. PreToolUse hook: every tool call is checked against the approved set and denied
   otherwise, regardless of what the model (or injected content) asks for.
4. `can_use_tool` callback: denies anything that would otherwise reach a prompt.
5. Server side: each MCP server re-validates its inputs (repository allowlist,
   Atlassian tool allowlist) before doing any network request.
"""

from __future__ import annotations

import logging
from typing import Any

from claude_agent_sdk import HookMatcher, PermissionResultAllow, PermissionResultDeny, ToolPermissionContext

logger = logging.getLogger(__name__)

# Built-in Claude Code tools that must never be available (filesystem, shell, network,
# code execution, sub-agents, MCP resource access, ...). `tools=[]` already removes
# built-ins; this list is a second, explicit barrier.
DISALLOWED_BUILTIN_TOOLS = [
    "Bash", "BashOutput", "KillShell", "KillBash", "PowerShell", "Monitor",
    "Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "NotebookRead", "Glob", "Grep", "LS",
    "WebFetch", "WebSearch",
    "Task", "Agent", "TodoWrite", "Skill", "SlashCommand", "ExitPlanMode", "EnterPlanMode",
    "ListMcpResourcesTool", "ReadMcpResourceTool", "ToolSearch", "EnterWorktree", "ExitWorktree",
    "CronCreate", "CronDelete", "RemoteTrigger", "SendMessage",
]


def qualified_name(server: str, tool: str) -> str:
    return f"mcp__{server}__{tool}"


class ToolPolicy:
    """The single source of truth for which tools the agent may call in a turn."""

    def __init__(self, approved: list[str]) -> None:
        self._approved = frozenset(approved)

    @property
    def approved(self) -> list[str]:
        return sorted(self._approved)

    def is_allowed(self, tool_name: str) -> bool:
        return tool_name in self._approved

    async def pre_tool_use(self, hook_input: dict[str, Any], tool_use_id: str | None, context: Any) -> dict[str, Any]:
        tool_name = str(hook_input.get("tool_name", ""))
        if self.is_allowed(tool_name):
            return {}
        logger.warning("denied tool call", extra={"tool": tool_name, "layer": "pre_tool_use_hook"})
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f"Tool '{tool_name}' is not permitted. This assistant is strictly read-only and may only use "
                    "the approved GitHub/Jira/Confluence read and search tools."
                ),
            }
        }

    async def can_use_tool(
        self, tool_name: str, tool_input: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResultAllow | PermissionResultDeny:
        if self.is_allowed(tool_name):
            return PermissionResultAllow()
        logger.warning("denied tool call", extra={"tool": tool_name, "layer": "can_use_tool"})
        return PermissionResultDeny(message=f"Tool '{tool_name}' is not permitted (read-only assistant).")

    def hooks(self) -> dict[str, list[HookMatcher]]:
        return {"PreToolUse": [HookMatcher(matcher=None, hooks=[self.pre_tool_use])]}
