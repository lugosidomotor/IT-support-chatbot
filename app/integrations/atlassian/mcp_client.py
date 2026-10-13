"""Official Atlassian Rovo MCP integration (Jira + Confluence), strictly read-only.

The application connects to the official remote endpoint (default
https://mcp.atlassian.com/v2/mcp) with a service account API key (Bearer auth).
It discovers the remote tool catalog at startup and re-exposes to the agent ONLY
tools on an explicit read/search allowlist, through an in-process MCP server.

Why filter in-process instead of handing the remote URL to the agent runtime:
* the agent never sees write/delete/admin tools at all (not merely "denied");
* every call is re-checked against the allowlist before it is forwarded;
* the API key stays inside this Python process and is never passed to the
  Claude Code subprocess or to the model.

No OAuth flow is ever attempted; if the key is missing the client is not created.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import anyio
from mcp import types
from mcp.server import Server

from app.agent.sources import SourceCollector
from app.integrations.status import IntegrationState, IntegrationStatus
from app.logging_config import redact

logger = logging.getLogger(__name__)

SERVER_NAME = "atlassian"
MAX_TOOL_OUTPUT_CHARS = 60_000

# Explicit allowlist (Rovo MCP permission groups read_jira / search_jira). Both current
# (v2) and legacy (v1) tool names are listed; unknown names are simply not exposed.
JIRA_READ_TOOLS = frozenset({
    "searchJiraIssuesUsingJql",
    "getJiraIssue",
    "listJiraIssueComments",
    "listJiraIssueRemoteIssueLinks",
    "getJiraIssueRemoteIssueLinks",
    "listJiraIssueLinkTypes",
    "listJiraIssueChangelogs",
    "listJiraProjects",
    "getVisibleJiraProjects",
    "listJiraStatuses",
    "listJiraProjectComponents",
    "getJiraProjectVersions",
    "listJiraProjectIssueTypesMetadata",
    "getJiraProjectIssueTypesMetadata",
})
# read_confluence / search_confluence
CONFLUENCE_READ_TOOLS = frozenset({
    "searchConfluence",
    "searchConfluenceUsingCql",
    "getConfluenceContent",
    "getConfluencePage",
    "listConfluenceContent",
    "listConfluenceSpaces",
    "getConfluenceSpaces",
    "getConfluenceSpace",
    "getPagesInConfluenceSpace",
    "getConfluencePageDescendants",
    "listConfluenceComments",
    "getConfluenceComment",
    "getConfluencePageFooterComments",
    "getConfluencePageInlineComments",
})
# Common / search_atlassian tools that are read-only and useful for both products.
SHARED_READ_TOOLS = frozenset({"search", "fetch", "getAccessibleAtlassianResources"})

APPROVED_TOOLS = JIRA_READ_TOOLS | CONFLUENCE_READ_TOOLS | SHARED_READ_TOOLS

# Indirection tools that could execute arbitrary (including write) tools by name.
DENIED_INDIRECTION_TOOLS = frozenset({"discover", "executeRead", "executeWrite", "executeDestructive"})
_READ_PREFIXES = ("get", "list", "search", "fetch")


class AtlassianAuthError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"Atlassian rejected the service account API key (HTTP {status}).")
        self.status = status


class AtlassianUnavailableError(Exception):
    pass


class McpClientLike(Protocol):
    async def list_tools(self, *, cursor: str | None = None) -> types.ListToolsResult: ...

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> types.CallToolResult: ...


Connector = Callable[[], AbstractAsyncContextManager[McpClientLike]]


def is_read_only_name(name: str) -> bool:
    """Defence in depth: an approved tool must also *look* like a read operation."""
    return name.startswith(_READ_PREFIXES)


def is_approved_tool(name: str) -> bool:
    return name in APPROVED_TOOLS and name not in DENIED_INDIRECTION_TOOLS and is_read_only_name(name)


def tool_product(name: str) -> str:
    if name in JIRA_READ_TOOLS or "Jira" in name:
        return "jira"
    if name in CONFLUENCE_READ_TOOLS or "Confluence" in name:
        return "confluence"
    return "shared"


def with_tools_all(url: str) -> str:
    """Ask the v2 endpoint for its full catalog up front (gateway mode) instead of deferred tools."""
    parsed = urlparse(url)
    if not parsed.path.startswith("/v2/"):
        return url
    query = dict(parse_qsl(parsed.query))
    query.setdefault("tools", "all")
    return urlunparse(parsed._replace(query=urlencode(query)))


def _describe(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup):
        return _describe(exc.exceptions[0])
    message = str(exc) or type(exc).__name__
    return redact(message)[:300]


class AtlassianMCP:
    def __init__(
        self,
        url: str,
        api_key: str,
        *,
        timeout_seconds: float = 30.0,
        site_url: str | None = None,
        connector: Connector | None = None,
    ) -> None:
        self._url = with_tools_all(url)
        self._api_key = api_key
        self.timeout = timeout_seconds
        self.site_url = site_url
        self._connector = connector or self._default_connector
        self.jira = IntegrationStatus(IntegrationState.CONNECTING, "Discovering Atlassian MCP tools…")
        self.confluence = IntegrationStatus(IntegrationState.CONNECTING, "Discovering Atlassian MCP tools…")
        self.approved: dict[str, types.Tool] = {}
        self.rejected_tool_count = 0
        self.sites: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ connection

    @asynccontextmanager
    async def _default_connector(self) -> AsyncIterator[McpClientLike]:
        import httpx2
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client

        statuses: list[int] = []

        async def capture(response: Any) -> None:
            if response.status_code >= 400:
                statuses.append(response.status_code)

        http = httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {self._api_key}", "User-Agent": "developer-support-agent/0.1"},
            timeout=httpx2.Timeout(self.timeout, connect=min(10.0, self.timeout)),
            follow_redirects=False,
            event_hooks={"response": [capture]},
        )
        try:
            async with http:
                async with Client(
                    streamable_http_client(self._url, http_client=http),
                    mode="legacy",
                    read_timeout_seconds=self.timeout,
                ) as client:
                    yield client
        except Exception as exc:
            auth_status = next((s for s in statuses if s in (401, 403)), None)
            if auth_status:
                raise AtlassianAuthError(auth_status) from None
            if statuses:
                raise AtlassianUnavailableError(f"Atlassian MCP returned HTTP {statuses[-1]}.") from None
            raise AtlassianUnavailableError(f"Atlassian MCP request failed: {_describe(exc)}") from None

    # ------------------------------------------------------------------ startup discovery

    def _set_both(self, state: IntegrationState, detail: str) -> None:
        self.jira = IntegrationStatus(state, detail)
        self.confluence = IntegrationStatus(state, detail)

    async def initialize(self) -> None:
        try:
            with anyio.fail_after(self.timeout):
                async with self._connector() as client:
                    tools: list[types.Tool] = []
                    cursor: str | None = None
                    for _ in range(20):
                        page = await client.list_tools(cursor=cursor)
                        tools.extend(page.tools)
                        cursor = page.next_cursor
                        if not cursor:
                            break
        except AtlassianAuthError as exc:
            logger.warning("atlassian authentication failed", extra={"http_status": exc.status})
            self._set_both(IntegrationState.AUTH_FAILED,
                           f"{exc} Check ATLASSIAN_SERVICE_ACCOUNT_API_KEY and that API token authentication is "
                           "enabled for the Rovo MCP server in Atlassian Administration.")
            return
        except (TimeoutError, AtlassianUnavailableError, Exception) as exc:  # noqa: BLE001
            detail = "Timed out connecting to Atlassian MCP." if isinstance(exc, TimeoutError) else _describe(exc)
            logger.warning("atlassian mcp unavailable", extra={"detail": detail})
            self._set_both(IntegrationState.UNAVAILABLE, detail)
            return

        names = [t.name for t in tools]
        self.approved = {t.name: t for t in tools if is_approved_tool(t.name)}
        self.rejected_tool_count = len(tools) - len(self.approved)
        logger.info(
            "atlassian tools discovered",
            extra={"discovered": len(tools), "approved": sorted(self.approved), "rejected": self.rejected_tool_count},
        )

        if not self.approved and any(n in DENIED_INDIRECTION_TOOLS for n in names):
            self._set_both(IntegrationState.UNAVAILABLE,
                           "The Atlassian MCP endpoint only exposed deferred tools (discover/execute*). "
                           "Use the v2 endpoint with '?tools=all' so read tools can be allowlisted explicitly.")
            return

        await self._load_sites()
        products = self._products_from_scopes()
        for product, read_tools in (("jira", JIRA_READ_TOOLS), ("confluence", CONFLUENCE_READ_TOOLS)):
            available = sorted(n for n in self.approved if n in read_tools)
            label = product.capitalize()
            if not available:
                status = IntegrationStatus(IntegrationState.UNAVAILABLE,
                                           f"No permitted {label} read/search tools are available to this service "
                                           "account (check its scopes and product access).")
            elif products is not None and product not in products:
                status = IntegrationStatus(IntegrationState.UNAVAILABLE,
                                           f"The service account has no {label} scopes on any accessible site.")
            else:
                status = IntegrationStatus(IntegrationState.CONNECTED, None,
                                           {"tools": available, "sites": [s.get("url") for s in self.sites if s.get("url")]})
            setattr(self, product, status)

    async def _load_sites(self) -> None:
        if "getAccessibleAtlassianResources" not in self.approved:
            return
        try:
            result = await self._forward("getAccessibleAtlassianResources", {})
        except Exception as exc:  # noqa: BLE001 - best effort only
            logger.info("could not list accessible atlassian sites", extra={"detail": _describe(exc)})
            return
        if result.is_error:
            return
        for block in result.content:
            if isinstance(block, types.TextContent):
                try:
                    data = json.loads(block.text)
                except ValueError:
                    continue
                items = data if isinstance(data, list) else data.get("resources") or data.get("sites") or []
                for item in items if isinstance(items, list) else []:
                    if isinstance(item, dict) and item.get("id"):
                        self.sites.append({"id": item.get("id"), "url": item.get("url"), "name": item.get("name"),
                                           "scopes": item.get("scopes") or []})
        if not self.site_url and len(self.sites) == 1 and self.sites[0].get("url"):
            self.site_url = self.sites[0]["url"]

    def _products_from_scopes(self) -> set[str] | None:
        scopes = [str(s).lower() for site in self.sites for s in site.get("scopes", [])]
        if not scopes:
            return None  # unknown: rely on tool availability and per-call errors
        products = set()
        if any("jira" in s for s in scopes):
            products.add("jira")
        if any("confluence" in s for s in scopes):
            products.add("confluence")
        return products

    # ------------------------------------------------------------------ tool exposure

    def enabled_products(self) -> set[str]:
        products = set()
        if self.jira.state == IntegrationState.CONNECTED:
            products.add("jira")
        if self.confluence.state == IntegrationState.CONNECTED:
            products.add("confluence")
        return products

    def exposed_tools(self) -> list[types.Tool]:
        products = self.enabled_products()
        if not products:
            return []
        return [
            tool for name, tool in sorted(self.approved.items())
            if tool_product(name) in products or (tool_product(name) == "shared")
        ]

    def exposed_tool_names(self) -> list[str]:
        return [tool.name for tool in self.exposed_tools()]

    async def _forward(self, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        with anyio.fail_after(self.timeout):
            async with self._connector() as client:
                return await client.call_tool(name, arguments)

    async def call_tool(self, name: str, arguments: dict[str, Any],
                        collector: SourceCollector | None = None) -> types.CallToolResult:
        # Server-side enforcement: re-check the allowlist on every call.
        if name not in self.exposed_tool_names() or not is_approved_tool(name):
            logger.warning("blocked non-approved atlassian tool", extra={"tool": name})
            return _error(f"Tool '{name}' is not permitted. Only approved read/search tools are available.")
        try:
            result = await self._forward(name, arguments)
        except AtlassianAuthError as exc:
            self._set_both(IntegrationState.AUTH_FAILED, str(exc))
            return _error(f"{exc} Jira/Confluence results are unavailable.")
        except TimeoutError:
            return _error("The Atlassian MCP request timed out. Results are unavailable for this query.")
        except Exception as exc:  # noqa: BLE001 - failures must not crash the agent turn
            return _error(f"Atlassian MCP call failed: {_describe(exc)}")
        content: list[types.ContentBlock] = []
        for block in result.content:
            if isinstance(block, types.TextContent):
                text = redact(block.text)
                if len(text) > MAX_TOOL_OUTPUT_CHARS:
                    text = text[:MAX_TOOL_OUTPUT_CHARS] + "\n…[output truncated: refine the query or request fewer results]"
                content.append(types.TextContent(type="text", text=text))
        if collector is not None and not result.is_error:
            for source in extract_sources(name, content, self.site_url):
                collector.add(*source)
        return types.CallToolResult(content=content or [types.TextContent(type="text", text="(empty result)")],
                                    is_error=bool(result.is_error))

    def build_mcp_server(self, collector: SourceCollector | None = None) -> Server:
        tools = [
            types.Tool(
                name=tool.name,
                description=tool.description,
                input_schema=tool.input_schema,
                annotations=types.ToolAnnotations(read_only_hint=True, destructive_hint=False),
            )
            for tool in self.exposed_tools()
        ]

        async def on_list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
            return types.ListToolsResult(tools=tools)

        async def on_call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
            return await self.call_tool(params.name, dict(params.arguments or {}), collector)

        return Server(SERVER_NAME, version="0.1.0", on_list_tools=on_list_tools, on_call_tool=on_call_tool)


def _error(message: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=message)], is_error=True)


# ---------------------------------------------------------------------- source extraction

_ISSUE_KEY = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")
_URL_KEYS = ("url", "webUrl", "browseUrl", "webui", "link", "pageUrl")
_TITLE_KEYS = ("title", "summary", "name", "key")


def _trusted_host(url: str, site_url: str | None) -> bool:
    host = urlparse(url).hostname or ""
    if site_url and host == urlparse(site_url).hostname:
        return True
    return host.endswith(".atlassian.net")


def _source_product(tool: str, url: str) -> tuple[str, str]:
    if "/browse/" in url or tool_product(tool) == "jira":
        return "jira", "issue"
    if "/wiki/" in url or tool_product(tool) == "confluence":
        return "confluence", "page"
    return "atlassian", "item"


def extract_sources(tool: str, content: list[types.ContentBlock], site_url: str | None,
                    limit: int = 8) -> list[tuple[str, str, str, str]]:
    """Pull (title, url, integration, kind) tuples out of structured tool output."""
    found: list[tuple[str, str, str, str]] = []

    def add(title: str, url: str) -> None:
        if len(found) >= limit or not url.startswith("https://") or not _trusted_host(url, site_url):
            return
        integration, kind = _source_product(tool, url)
        found.append((title or url, url, integration, kind))

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 6 or len(found) >= limit:
            return
        if isinstance(node, dict):
            title = next((str(node[k]) for k in _TITLE_KEYS if isinstance(node.get(k), str)), "")
            fields = node.get("fields")
            if isinstance(fields, dict) and isinstance(fields.get("summary"), str):
                title = f"{node.get('key', '')}: {fields['summary']}".strip(": ")
            url = next((node[k] for k in _URL_KEYS if isinstance(node.get(k), str) and node[k].startswith("https://")), None)
            links = node.get("_links")
            if url is None and isinstance(links, dict) and isinstance(links.get("webui"), str):
                base = links.get("base") or (f"{site_url}/wiki" if site_url else None)
                if base:
                    url = f"{base.rstrip('/')}{links['webui']}"
            key = node.get("key")
            if url is None and isinstance(key, str) and _ISSUE_KEY.match(key) and site_url:
                url = f"{site_url.rstrip('/')}/browse/{key}"
            if url:
                add(title, url)
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value, depth + 1)
        elif isinstance(node, list):
            for item in node:
                walk(item, depth + 1)

    for block in content:
        if not isinstance(block, types.TextContent):
            continue
        try:
            walk(json.loads(block.text))
        except ValueError:
            for match in re.finditer(r"https://[A-Za-z0-9.-]+\.atlassian\.net/(?:browse/[A-Z][A-Z0-9_]+-\d+|wiki/[^\s)\]\"'<>]+)",
                                     block.text):
                add(match.group(0), match.group(0))
    return found
