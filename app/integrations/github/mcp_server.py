"""Read-only GitHub MCP server built with the official MCP Python SDK.

The server is served in-process to the Claude Agent SDK (an `sdk`-type MCP server),
so no network port is opened. It can also be run standalone over stdio for debugging:

    uv run python -m app.integrations.github.mcp_server
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import jsonschema
from mcp import types
from mcp.server import Server

from app.agent.sources import SourceCollector
from app.integrations.github.client import WEB_ORIGIN, GitHubClient, GitHubError
from app.integrations.github.search import SearchLimits, search_repository_content

logger = logging.getLogger(__name__)

SERVER_NAME = "github"
MAX_TOOL_OUTPUT_CHARS = 60_000

_REPO = {"type": "string", "description": "Repository in the form 'owner/repo'. Must be one of the configured repositories."}
_REF = {"type": "string", "description": "Optional branch, tag or commit SHA. Defaults to the repository default branch."}
_PATH = {"type": "string", "description": "Repository-relative path, e.g. 'src/app/main.py'."}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    schema: dict[str, Any]
    handler: Callable[[GitHubClient, dict[str, Any], SearchLimits], Awaitable[dict[str, Any]]]


def _obj(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False}


async def _list_repositories(client: GitHubClient, args: dict[str, Any], _: SearchLimits) -> dict[str, Any]:
    repos = []
    for repo in client.repositories:
        try:
            info = await client.repository_info(repo)
            repos.append({k: info.get(k) for k in ("repository", "description", "html_url", "default_branch",
                                                   "visibility", "language")})
        except GitHubError as exc:
            repos.append({"repository": repo, **exc.to_dict()})
    return {"repositories": repos, "authentication": "token" if client.authenticated else "anonymous"}


async def _repository_info(client: GitHubClient, args: dict[str, Any], _: SearchLimits) -> dict[str, Any]:
    info = await client.repository_info(args["repository"])
    if args.get("include_readme", False):
        try:
            readme = await client.readme(args["repository"])
            info["readme"] = {"path": readme["path"], "html_url": readme["html_url"],
                              "content": (readme.get("content") or "")[:20_000], "truncated": readme["truncated"]}
        except GitHubError as exc:
            info["readme"] = exc.to_dict()
    return info


async def _file_contents(client: GitHubClient, args: dict[str, Any], _: SearchLimits) -> dict[str, Any]:
    return await client.file_contents(args["repository"], args["path"], args.get("ref"), args.get("start_line"),
                                      args.get("end_line"))


async def _list_directory(client: GitHubClient, args: dict[str, Any], _: SearchLimits) -> dict[str, Any]:
    return await client.list_directory(args["repository"], args.get("path", ""), args.get("ref"))


async def _repository_tree(client: GitHubClient, args: dict[str, Any], _: SearchLimits) -> dict[str, Any]:
    return await client.repository_tree(args["repository"], args.get("ref"), args.get("path_prefix"),
                                        args.get("max_entries", 400))


async def _list_pull_requests(client: GitHubClient, args: dict[str, Any], _: SearchLimits) -> dict[str, Any]:
    return await client.list_pull_requests(args["repository"], args.get("state", "open"), args.get("per_page", 20),
                                           args.get("page", 1), args.get("base"))


async def _get_pull_request(client: GitHubClient, args: dict[str, Any], _: SearchLimits) -> dict[str, Any]:
    return await client.get_pull_request(args["repository"], args["number"], args.get("include_files", True))


async def _list_issues(client: GitHubClient, args: dict[str, Any], _: SearchLimits) -> dict[str, Any]:
    return await client.list_issues(args["repository"], args.get("state", "open"), args.get("labels"),
                                    args.get("per_page", 20), args.get("page", 1))


async def _get_issue(client: GitHubClient, args: dict[str, Any], _: SearchLimits) -> dict[str, Any]:
    return await client.get_issue(args["repository"], args["number"], args.get("include_comments", True))


async def _list_commits(client: GitHubClient, args: dict[str, Any], _: SearchLimits) -> dict[str, Any]:
    return await client.list_commits(args["repository"], args.get("ref"), args.get("path"), args.get("since"),
                                     args.get("until"), args.get("per_page", 20), args.get("page", 1))


async def _get_commit(client: GitHubClient, args: dict[str, Any], _: SearchLimits) -> dict[str, Any]:
    return await client.get_commit(args["repository"], args["sha"])


async def _search(client: GitHubClient, args: dict[str, Any], limits: SearchLimits) -> dict[str, Any]:
    return await search_repository_content(client, args["repository"], args["query"], ref=args.get("ref"),
                                           path_prefix=args.get("path_prefix"), limits=limits,
                                           max_files=args.get("max_files"))


_STATE = {"type": "string", "enum": ["open", "closed", "all"], "default": "open"}
_PAGE = {"type": "integer", "minimum": 1, "maximum": 50, "default": 1}
_PER_PAGE = {"type": "integer", "minimum": 1, "maximum": 50, "default": 20}

TOOLS: list[ToolSpec] = [
    ToolSpec("list_repositories",
             "List the GitHub repositories this assistant is allowed to read, with description and default branch.",
             _obj({}), _list_repositories),
    ToolSpec("get_repository_info",
             "Get metadata for a configured repository (description, default branch, language, topics). "
             "Set include_readme=true to also return the README.",
             _obj({"repository": _REPO, "include_readme": {"type": "boolean", "default": False}}, ["repository"]),
             _repository_info),
    ToolSpec("get_file_contents",
             "Read a text file from a configured repository. Returns line-numbered content (cite lines as "
             "html_url#L<n>). Use start_line/end_line to read part of a large file.",
             _obj({"repository": _REPO, "path": _PATH, "ref": _REF,
                   "start_line": {"type": "integer", "minimum": 1},
                   "end_line": {"type": "integer", "minimum": 1}}, ["repository", "path"]),
             _file_contents),
    ToolSpec("list_directory",
             "List files and sub-directories of one directory in a configured repository.",
             _obj({"repository": _REPO, "path": {**_PATH, "description": "Directory path; empty for the root."},
                   "ref": _REF}, ["repository"]),
             _list_directory),
    ToolSpec("get_repository_tree",
             "Get the recursive file tree of a configured repository (optionally under path_prefix). "
             "Useful to locate files by name before reading them.",
             _obj({"repository": _REPO, "ref": _REF, "path_prefix": {"type": "string"},
                   "max_entries": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 400}},
                  ["repository"]),
             _repository_tree),
    ToolSpec("list_pull_requests",
             "List pull requests of a configured repository, most recently updated first.",
             _obj({"repository": _REPO, "state": _STATE, "base": {"type": "string"}, "per_page": _PER_PAGE,
                   "page": _PAGE}, ["repository"]),
             _list_pull_requests),
    ToolSpec("get_pull_request",
             "Get one pull request including description and (optionally) changed files with truncated patches.",
             _obj({"repository": _REPO, "number": {"type": "integer", "minimum": 1},
                   "include_files": {"type": "boolean", "default": True}}, ["repository", "number"]),
             _get_pull_request),
    ToolSpec("list_issues",
             "List GitHub issues (not pull requests) of a configured repository, most recently updated first.",
             _obj({"repository": _REPO, "state": _STATE,
                   "labels": {"type": "string", "description": "Comma-separated label names."},
                   "per_page": _PER_PAGE, "page": _PAGE}, ["repository"]),
             _list_issues),
    ToolSpec("get_issue",
             "Get one GitHub issue with its description and up to 20 comments.",
             _obj({"repository": _REPO, "number": {"type": "integer", "minimum": 1},
                   "include_comments": {"type": "boolean", "default": True}}, ["repository", "number"]),
             _get_issue),
    ToolSpec("list_commits",
             "List commits of a configured repository, optionally only those touching `path` "
             "(useful to find which change introduced a file or setting).",
             _obj({"repository": _REPO, "ref": _REF, "path": _PATH,
                   "since": {"type": "string", "description": "ISO 8601 date"},
                   "until": {"type": "string", "description": "ISO 8601 date"},
                   "per_page": _PER_PAGE, "page": _PAGE}, ["repository"]),
             _list_commits),
    ToolSpec("get_commit",
             "Get one commit with message, stats and changed files (patches truncated).",
             _obj({"repository": _REPO, "sha": {"type": "string", "description": "Commit SHA"}},
                  ["repository", "sha"]),
             _get_commit),
    ToolSpec("search_repository_content",
             "Search text inside a configured repository (bounded scan of the most relevant docs, config and "
             "source files). Returns matching lines with links. Results may be PARTIAL for large repositories; "
             "check `complete` and `partial_reasons`. Use short, specific queries such as an identifier, "
             "setting name or error message fragment.",
             _obj({"repository": _REPO, "query": {"type": "string", "minLength": 2, "maxLength": 200},
                   "ref": _REF, "path_prefix": {"type": "string", "description": "Limit search to a directory."},
                   "max_files": {"type": "integer", "minimum": 1, "maximum": 500}}, ["repository", "query"]),
             _search),
]

TOOL_NAMES = [tool.name for tool in TOOLS]
_TOOLS_BY_NAME = {tool.name: tool for tool in TOOLS}


def record_sources(collector: SourceCollector, tool_name: str, result: dict[str, Any]) -> None:
    """Translate a tool result into source references."""
    repo = result.get("repository")
    if tool_name == "get_file_contents":
        collector.add(f"{repo}: {result.get('path')}", result.get("html_url"), "github", "file")
    elif tool_name == "get_repository_info":
        collector.add(f"{repo} (repository)", result.get("html_url"), "github", "repository")
        if isinstance(result.get("readme"), dict):
            collector.add(f"{repo}: {result['readme'].get('path')}", result["readme"].get("html_url"), "github", "file")
    elif tool_name == "list_directory":
        collector.add(f"{repo}: {result.get('path')} (directory)", result.get("html_url"), "github", "directory")
    elif tool_name == "get_repository_tree":
        collector.add(f"{repo} file tree", result.get("html_url"), "github", "directory")
    elif tool_name in ("get_pull_request", "get_issue"):
        kind = "pull_request" if tool_name == "get_pull_request" else "issue"
        collector.add(f"{repo}#{result.get('number')}: {result.get('title')}", result.get("html_url"), "github", kind)
    elif tool_name == "get_commit":
        message = (result.get("message") or "").splitlines()[0:1]
        collector.add(f"{repo}@{str(result.get('sha'))[:7]}: {message[0] if message else ''}", result.get("html_url"),
                      "github", "commit")
    elif tool_name == "list_pull_requests":
        collector.add(f"{repo} pull requests", f"{WEB_ORIGIN}/{repo}/pulls", "github", "pull_request_list")
    elif tool_name == "list_issues":
        collector.add(f"{repo} issues", f"{WEB_ORIGIN}/{repo}/issues", "github", "issue_list")
    elif tool_name == "list_commits":
        collector.add(f"{repo} commit history", f"{WEB_ORIGIN}/{repo}/commits", "github", "commit_list")
    elif tool_name == "search_repository_content":
        for item in result.get("results", [])[:6]:
            collector.add(f"{repo}: {item.get('path')}", item.get("html_url"), "github", "file")


def _cap_output(text: str) -> str:
    if len(text) <= MAX_TOOL_OUTPUT_CHARS:
        return text
    return text[:MAX_TOOL_OUTPUT_CHARS] + "\n…[output truncated: narrow the request, e.g. with start_line/end_line]"


def _error_result(payload: dict[str, Any]) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(payload))], is_error=True)


async def call_github_tool(
    client: GitHubClient,
    name: str,
    arguments: dict[str, Any],
    limits: SearchLimits,
    collector: SourceCollector | None = None,
) -> types.CallToolResult:
    spec = _TOOLS_BY_NAME.get(name)
    if spec is None:
        return _error_result({"error": "unknown_tool", "message": f"Tool '{name}' does not exist."})
    try:
        jsonschema.validate(arguments, spec.schema)
    except jsonschema.ValidationError as exc:
        return _error_result({"error": "invalid_input", "message": exc.message})
    try:
        result = await spec.handler(client, arguments, limits)
    except GitHubError as exc:
        logger.info("github tool error", extra={"tool": name, "error": exc.code})
        return _error_result(exc.to_dict())
    except Exception:  # noqa: BLE001 - tool failures must not crash the agent turn
        logger.exception("github tool crashed", extra={"tool": name})
        return _error_result({"error": "internal_error", "message": "The GitHub tool failed unexpectedly."})
    if collector is not None:
        record_sources(collector, name, result)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=_cap_output(json.dumps(result, ensure_ascii=False)))]
    )


def build_github_mcp_server(
    client: GitHubClient, limits: SearchLimits, collector: SourceCollector | None = None
) -> Server:
    tools = [
        types.Tool(
            name=spec.name,
            description=spec.description,
            input_schema=spec.schema,
            annotations=types.ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True),
        )
        for spec in TOOLS
    ]

    async def on_list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
        return types.ListToolsResult(tools=tools)

    async def on_call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        return await call_github_tool(client, params.name, dict(params.arguments or {}), limits, collector)

    return Server(SERVER_NAME, version="0.1.0", on_list_tools=on_list_tools, on_call_tool=on_call_tool)


async def _run_stdio() -> None:  # pragma: no cover - manual debugging entry point
    from mcp.server.stdio import stdio_server

    from app.config import get_settings

    settings = get_settings()
    client = GitHubClient(
        settings.github_repos,
        settings.github_token.get_secret_value() if settings.github_token else None,
        default_ref=settings.github_default_ref,
        cache_ttl_seconds=settings.cache_ttl_seconds,
        max_file_bytes=settings.github_max_file_bytes,
    )
    limits = SearchLimits(settings.github_max_files_per_search, settings.github_max_file_bytes,
                          settings.github_search_timeout_seconds)
    server = build_github_mcp_server(client, limits)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":  # pragma: no cover
    import anyio

    anyio.run(_run_stdio)
