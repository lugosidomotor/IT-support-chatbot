from __future__ import annotations

import json
from contextlib import asynccontextmanager

from mcp import types

from app.agent.sources import SourceCollector
from app.integrations.atlassian.mcp_client import (
    APPROVED_TOOLS,
    DENIED_INDIRECTION_TOOLS,
    AtlassianMCP,
    extract_sources,
    is_approved_tool,
    with_tools_all,
)
from app.integrations.registry import IntegrationRegistry
from app.integrations.status import IntegrationState
from app.logging_config import register_secrets
from tests.conftest import ATLASSIAN_KEY
from tests.fakes import (
    ATLASSIAN_TOOLS,
    failing_connector,
    fake_atlassian_server,
    in_process_connector,
    unreachable_connector,
)

WRITE_TOOLS = {"createJiraIssue", "editJiraIssue", "transitionJiraIssue", "deleteJiraIssue", "addOrEditJiraIssueComment",
               "createConfluenceContent", "updateConfluenceContent", "moveConfluenceContent", "executeWrite"}


def make_atlassian(server=None, connector=None, **kwargs) -> AtlassianMCP:
    return AtlassianMCP("https://mcp.atlassian.com/v2/mcp", ATLASSIAN_KEY, timeout_seconds=5,
                        connector=connector or in_process_connector(server or fake_atlassian_server()), **kwargs)


async def test_client_not_initialised_without_credentials(make_settings):
    calls = []

    @asynccontextmanager
    async def connector():
        calls.append(1)
        yield None

    registry = IntegrationRegistry(make_settings(), atlassian_connector=connector)
    await registry.startup()
    assert registry.atlassian is None
    assert calls == []  # never connected, never attempted OAuth
    assert registry.jira_status().state == IntegrationState.DISABLED
    assert registry.confluence_status().state == IntegrationState.DISABLED


async def test_client_initialised_when_configured(make_settings):
    s = make_settings(atlassian_service_account_api_key=ATLASSIAN_KEY)
    registry = IntegrationRegistry(s, atlassian_connector=in_process_connector(fake_atlassian_server()))
    await registry.startup()
    await registry.wait_ready()
    assert registry.jira_status().state == IntegrationState.CONNECTED
    assert registry.confluence_status().state == IntegrationState.CONNECTED
    ctx = registry.prompt_context()
    assert ctx.jira_enabled and ctx.confluence_enabled
    assert ctx.atlassian_cloud_ids == ["cloud-123"]
    assert ctx.atlassian_site_url == "https://acme.atlassian.net"
    await registry.shutdown()


async def test_only_read_and_search_tools_are_exposed():
    atl = make_atlassian()
    await atl.initialize()
    exposed = set(atl.exposed_tool_names())
    assert exposed == {"searchJiraIssuesUsingJql", "getJiraIssue", "searchConfluence", "getConfluenceContent",
                       "getAccessibleAtlassianResources"}
    assert exposed <= APPROVED_TOOLS
    assert not exposed & WRITE_TOOLS
    assert not exposed & DENIED_INDIRECTION_TOOLS
    assert "listJiraIssueAssignableUsers" not in exposed  # read-only but not on the allowlist
    assert atl.rejected_tool_count == len(ATLASSIAN_TOOLS) - len(exposed)


async def test_write_tools_are_rejected_server_side():
    calls: list = []
    atl = make_atlassian(fake_atlassian_server(calls=calls))
    await atl.initialize()
    calls.clear()
    for name in sorted(WRITE_TOOLS | {"executeRead", "discover"}):
        result = await atl.call_tool(name, {"summary": "pwned"})
        assert result.is_error, name
        assert "not permitted" in result.content[0].text
    assert calls == []  # nothing was forwarded to the remote server


def test_approval_rules_are_defence_in_depth():
    for name in WRITE_TOOLS | DENIED_INDIRECTION_TOOLS | {"manageJiraSprint", "archiveConfluenceContent"}:
        assert not is_approved_tool(name), name
    assert all(is_approved_tool(n) for n in APPROVED_TOOLS)


async def test_read_tool_is_forwarded_and_sources_extracted():
    calls: list = []
    atl = make_atlassian(fake_atlassian_server(calls=calls))
    await atl.initialize()
    collector = SourceCollector()
    result = await atl.call_tool("searchJiraIssuesUsingJql", {"cloudId": "cloud-123", "jql": "text ~ 403"}, collector)
    assert not result.is_error
    assert calls[-1] == ("searchJiraIssuesUsingJql", {"cloudId": "cloud-123", "jql": "text ~ 403"})
    assert [s.url for s in collector.sources] == ["https://acme.atlassian.net/browse/OPS-42"]
    assert collector.sources[0].integration == "jira"
    await atl.call_tool("searchConfluence", {"cql": "title ~ migration"}, collector)
    assert collector.sources[-1].url == "https://acme.atlassian.net/wiki/spaces/OPS/pages/1/DB+migration+runbook"
    assert collector.sources[-1].integration == "confluence"


async def test_authentication_failure_handled_gracefully():
    atl = make_atlassian(connector=failing_connector(401))
    await atl.initialize()
    assert atl.jira.state == IntegrationState.AUTH_FAILED
    assert atl.confluence.state == IntegrationState.AUTH_FAILED
    assert atl.exposed_tool_names() == []
    assert ATLASSIAN_KEY not in (atl.jira.detail or "")


async def test_unreachable_endpoint_is_unavailable():
    atl = make_atlassian(connector=unreachable_connector())
    await atl.initialize()
    assert atl.jira.state == IntegrationState.UNAVAILABLE
    assert "network unreachable" in (atl.jira.detail or "")


async def test_confluence_only_partial_availability():
    tools = {k: v for k, v in ATLASSIAN_TOOLS.items() if "Jira" not in k}
    atl = make_atlassian(fake_atlassian_server(tools=tools))
    await atl.initialize()
    assert atl.jira.state == IntegrationState.UNAVAILABLE
    assert atl.confluence.state == IntegrationState.CONNECTED
    assert all("Jira" not in n for n in atl.exposed_tool_names())


async def test_jira_only_partial_availability_by_scopes():
    atl = make_atlassian(fake_atlassian_server(scopes=["read:jira-work"]))
    await atl.initialize()
    assert atl.jira.state == IntegrationState.CONNECTED
    assert atl.confluence.state == IntegrationState.UNAVAILABLE
    assert "searchConfluence" not in atl.exposed_tool_names()
    result = await atl.call_tool("searchConfluence", {"cql": "x"})
    assert result.is_error


async def test_deferred_only_catalog_is_reported():
    tools = {"discover": "d", "executeRead": "r", "executeWrite": "w"}
    atl = make_atlassian(fake_atlassian_server(tools=tools))
    await atl.initialize()
    assert atl.jira.state == IntegrationState.UNAVAILABLE
    assert "tools=all" in (atl.jira.detail or "")


async def test_mcp_server_lists_only_exposed_tools():
    from mcp import Client

    atl = make_atlassian()
    await atl.initialize()
    async with Client(atl.build_mcp_server()) as mcp:
        names = {t.name for t in (await mcp.list_tools()).tools}
        assert names == set(atl.exposed_tool_names())
        blocked = await mcp.call_tool("createJiraIssue", {"summary": "x"})
        assert blocked.is_error


async def test_secrets_in_remote_output_are_redacted():
    register_secrets([ATLASSIAN_KEY])

    async def on_list_tools(ctx, params):
        return types.ListToolsResult(tools=[types.Tool(name="getJiraIssue", input_schema={"type": "object"})])

    async def on_call_tool(ctx, params):
        return types.CallToolResult(content=[types.TextContent(type="text", text=f"echo {ATLASSIAN_KEY}")])

    from mcp.server import Server

    atl = make_atlassian(Server("x", on_list_tools=on_list_tools, on_call_tool=on_call_tool))
    await atl.initialize()
    result = await atl.call_tool("getJiraIssue", {"issueIdOrKey": "OPS-1"})
    assert ATLASSIAN_KEY not in result.content[0].text


def test_tools_all_query_parameter():
    assert with_tools_all("https://mcp.atlassian.com/v2/mcp") == "https://mcp.atlassian.com/v2/mcp?tools=all"
    assert with_tools_all("https://mcp.atlassian.com/v2/mcp?tools=x") == "https://mcp.atlassian.com/v2/mcp?tools=x"
    assert with_tools_all("https://mcp.atlassian.com/v1/mcp") == "https://mcp.atlassian.com/v1/mcp"


def test_source_extraction_ignores_untrusted_hosts():
    payload = {"results": [{"title": "Evil", "url": "https://evil.example.com/x"},
                           {"title": "Good", "url": "https://acme.atlassian.net/wiki/x"}]}
    found = extract_sources("searchConfluence", [types.TextContent(type="text", text=json.dumps(payload))], None)
    assert [f[1] for f in found] == ["https://acme.atlassian.net/wiki/x"]
