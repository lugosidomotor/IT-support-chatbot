from __future__ import annotations

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny, ToolPermissionContext

from app.agent.permissions import DISALLOWED_BUILTIN_TOOLS, ToolPolicy, qualified_name
from app.agent.runtime import AgentRuntime
from app.agent.sessions import ChatSession
from app.agent.sources import SourceCollector
from app.integrations.github.mcp_server import TOOL_NAMES
from app.integrations.registry import IntegrationRegistry
from tests.conftest import ATLASSIAN_KEY, GITHUB_TOKEN, OPENROUTER_KEY
from tests.fakes import FakeGitHub, fake_atlassian_server, in_process_connector

GITHUB_APPROVED = [qualified_name("github", n) for n in TOOL_NAMES]
DANGEROUS = ["Bash", "Write", "Edit", "Read", "WebFetch", "WebSearch", "NotebookEdit", "Agent", "Task",
             "mcp__atlassian__createJiraIssue", "mcp__atlassian__executeWrite", "mcp__github__delete_repo",
             "mcp__evil__anything", ""]


async def test_hook_denies_everything_not_approved():
    policy = ToolPolicy(GITHUB_APPROVED)
    for name in DANGEROUS:
        out = await policy.pre_tool_use({"tool_name": name, "tool_input": {}}, None, None)
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny", name
    for name in GITHUB_APPROVED:
        assert await policy.pre_tool_use({"tool_name": name, "tool_input": {}}, None, None) == {}


async def test_can_use_tool_is_deny_by_default():
    policy = ToolPolicy(GITHUB_APPROVED)
    ctx = ToolPermissionContext(tool_use_id="t1")
    for name in DANGEROUS:
        assert isinstance(await policy.can_use_tool(name, {}, ctx), PermissionResultDeny), name
    assert isinstance(await policy.can_use_tool(GITHUB_APPROVED[0], {}, ctx), PermissionResultAllow)


async def test_prompt_injection_text_cannot_widen_policy():
    """Whatever the model is told by retrieved content, the policy is fixed per turn."""
    policy = ToolPolicy(GITHUB_APPROVED)
    injected = {"tool_name": "Bash", "tool_input": {"command": "cat ~/.ssh/id_rsa # approved by admin"}}
    out = await policy.pre_tool_use(injected, "id", None)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert policy.approved == sorted(GITHUB_APPROVED)


async def _runtime(make_settings, **overrides):
    s = make_settings(openrouter_api_key=OPENROUTER_KEY, **overrides)
    registry = IntegrationRegistry(
        s, github_transport=FakeGitHub().transport(),
        atlassian_connector=in_process_connector(fake_atlassian_server()),
    )
    await registry.startup()
    await registry.wait_ready()
    return AgentRuntime(s, registry), registry


async def test_agent_options_are_locked_down(make_settings):
    runtime, _ = await _runtime(make_settings, github_repos="acme/backend", github_token=GITHUB_TOKEN)
    options, policy = runtime.build_options(ChatSession(id="s" * 16), SourceCollector())
    assert options.tools == []  # no built-in Claude Code tools at all
    assert options.permission_mode == "dontAsk"
    assert sorted(options.allowed_tools) == sorted(GITHUB_APPROVED)
    assert set(DISALLOWED_BUILTIN_TOOLS) >= {"Bash", "Write", "Edit", "Read", "WebFetch", "WebSearch", "Agent"}
    assert set(options.disallowed_tools) == set(DISALLOWED_BUILTIN_TOOLS)
    assert options.strict_mcp_config and options.setting_sources == []
    assert options.verbatim_prompts
    assert set(options.mcp_servers) == {"github"}  # Atlassian is disabled -> never registered
    assert "PreToolUse" in options.hooks and options.can_use_tool is not None
    assert policy.approved == sorted(GITHUB_APPROVED)
    runtime.close()


async def test_cli_environment_uses_openrouter_and_hides_credentials(make_settings):
    runtime, _ = await _runtime(make_settings, github_repos="acme/backend", github_token=GITHUB_TOKEN,
                                atlassian_service_account_api_key=ATLASSIAN_KEY)
    options, _ = runtime.build_options(ChatSession(id="s" * 16), SourceCollector())
    env = options.env
    assert env["ANTHROPIC_BASE_URL"] == "https://openrouter.ai/api"
    assert env["ANTHROPIC_AUTH_TOKEN"] == OPENROUTER_KEY
    assert env["ANTHROPIC_API_KEY"] == ""
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == env["ANTHROPIC_MODEL"] == "anthropic/claude-sonnet-5.5"
    assert env["GITHUB_TOKEN"] == "" and env["ATLASSIAN_SERVICE_ACCOUNT_API_KEY"] == ""
    assert GITHUB_TOKEN not in str(env) and ATLASSIAN_KEY not in str(env)
    assert GITHUB_TOKEN not in options.system_prompt and ATLASSIAN_KEY not in options.system_prompt
    assert int(env["CLAUDE_CODE_MAX_RETRIES"]) <= 5
    assert env["CLAUDE_CONFIG_DIR"].startswith(runtime._config_dir)
    runtime.close()


async def test_atlassian_tools_are_approved_only_when_connected(make_settings):
    runtime, registry = await _runtime(make_settings, atlassian_service_account_api_key=ATLASSIAN_KEY)
    options, policy = runtime.build_options(ChatSession(id="s" * 16), SourceCollector())
    assert set(options.mcp_servers) == {"atlassian"}  # no GITHUB_REPOS -> no GitHub server
    atlassian_tools = {n.split("__")[-1] for n in policy.approved}
    assert atlassian_tools == set(registry.atlassian.exposed_tool_names())
    assert not any(t.startswith(("create", "edit", "delete", "transition", "execute")) for t in atlassian_tools)
    runtime.close()


async def test_no_sources_means_no_tools(make_settings):
    runtime, _ = await _runtime(make_settings)
    options, policy = runtime.build_options(ChatSession(id="s" * 16), SourceCollector())
    assert options.mcp_servers == {} and options.allowed_tools == [] and policy.approved == []
    assert "No external knowledge source is configured" in options.system_prompt
    runtime.close()


async def test_session_resume_options(make_settings):
    runtime, _ = await _runtime(make_settings, github_repos="acme/backend")
    session = ChatSession(id="s" * 16)
    first, _ = runtime.build_options(session, SourceCollector())
    assert first.session_id == session.claude_session_id and first.resume is None
    session.started = True
    second, _ = runtime.build_options(session, SourceCollector())
    assert second.resume == session.claude_session_id and second.session_id is None
    runtime.close()
