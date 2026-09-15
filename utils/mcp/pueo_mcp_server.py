"""Pueo MCP server — exposes a safe read-heavy subset of Pueo's diagnostic tools.

All `mcp` SDK imports are deferred into method bodies so Pueo starts normally when
the package is absent (the MCP feature simply won't start).

`_dispatch()` and `_check_auth()` have no mcp imports and are fully unit-testable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from utils.core.logging import get_logger

if TYPE_CHECKING:
    from utils.agent.tool_executor import ToolExecutor

log = get_logger("pueo_mcp_server")

_MCP_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "query_knowledge",
        "read_config",
        "read_logs",
        "get_ha_profile",
        "get_disk_usage",
        "check_entity_status",
        "read_pueo_log",
        "search_log",
        "fetch_ha_docs",
        "remember",
        "recall",
        "search_integrations",
        "get_dashboard_entity_health",
    }
)


def _check_auth(authorization_header: str, expected_token: str) -> bool:
    """Return True when the request is authorised to use the MCP server.

    With no token configured every request is allowed. With a token the
    Authorization header must be exactly ``Bearer <token>``.
    """
    if not expected_token:
        return True
    return authorization_header == f"Bearer {expected_token}"


class PueoMCPServer:
    """Starlette ASGI app that serves Pueo's diagnostic tools over MCP/SSE."""

    def __init__(self, executor: "ToolExecutor") -> None:
        self._executor = executor
        self._uvicorn_server: Any = None

    async def _dispatch(self, name: str, arguments: dict) -> str:
        """Route an MCP tool call to ToolExecutor — testable without the mcp package."""
        from utils.agent.tool_registry import ToolCall

        if name not in _MCP_TOOL_NAMES:
            return f"Error: tool '{name}' is not available via MCP"
        try:
            result = await self._executor.execute(
                ToolCall(name=name, arguments=arguments)
            )
            return (
                result.output
                if result.success
                else f"Error: {result.error or 'unknown'}"
            )
        except Exception as exc:
            log.error("mcp_tool_dispatch_failed", tool=name, error=str(exc))
            return f"Error executing {name}: {exc}"

    def _build_asgi_app(self) -> Any:
        """Wire the mcp SDK into a Starlette ASGI app.

        Only call this after confirming the mcp package is importable. All SDK
        imports live here so that the rest of the module remains importable even
        when mcp is not installed.
        """
        import config as _cfg
        from mcp.server import Server
        from mcp.server.sse import SseServerTransport
        import mcp.types as types
        from starlette.applications import Starlette
        from starlette.middleware import Middleware
        from starlette.middleware.base import BaseHTTPMiddleware
        from starlette.requests import Request
        from starlette.responses import Response
        from starlette.routing import Route
        from utils.agent.tool_registry import (
            CHECK_ENTITY_STATUS,
            FETCH_HA_DOCS,
            GET_DASHBOARD_ENTITY_HEALTH,
            GET_DISK_USAGE,
            GET_HA_PROFILE,
            QUERY_KNOWLEDGE,
            READ_CONFIG,
            READ_LOGS,
            READ_PUEO_LOG,
            RECALL,
            REMEMBER,
            SEARCH_INTEGRATIONS,
            SEARCH_LOG,
        )

        _all_tool_defs = [
            QUERY_KNOWLEDGE,
            READ_CONFIG,
            READ_LOGS,
            GET_HA_PROFILE,
            GET_DISK_USAGE,
            CHECK_ENTITY_STATUS,
            READ_PUEO_LOG,
            SEARCH_LOG,
            FETCH_HA_DOCS,
            REMEMBER,
            RECALL,
            SEARCH_INTEGRATIONS,
            GET_DASHBOARD_ENTITY_HEALTH,
        ]
        tool_defs = [td for td in _all_tool_defs if td.name in _MCP_TOOL_NAMES]
        token = _cfg.MCP_TOKEN

        mcp_server = Server("pueo-diagnostics")

        @mcp_server.list_tools()
        async def list_tools() -> list[types.Tool]:
            return [
                types.Tool(
                    name=td.name,
                    description=td.description,
                    inputSchema=td.parameters,
                )
                for td in tool_defs
            ]

        @mcp_server.call_tool()
        async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
            text = await self._dispatch(name, arguments)
            return [types.TextContent(type="text", text=text)]

        sse_transport = SseServerTransport("/messages")

        async def handle_sse(request: Request) -> Response:
            async with sse_transport.connect_sse(
                request.scope, request.receive, request._send
            ) as streams:
                await mcp_server.run(
                    streams[0],
                    streams[1],
                    mcp_server.create_initialization_options(),
                )
            return Response()

        async def handle_messages(request: Request) -> Response:
            await sse_transport.handle_post_message(
                request.scope, request.receive, request._send
            )
            return Response()

        class _BearerAuthMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, req: Request, call_next):  # type: ignore[override]
                if not _check_auth(req.headers.get("Authorization", ""), token):
                    from starlette.responses import JSONResponse

                    return JSONResponse({"error": "Unauthorized"}, status_code=401)
                return await call_next(req)

        middleware = [Middleware(_BearerAuthMiddleware)] if token else []
        return Starlette(
            routes=[
                Route("/sse", handle_sse),
                Route("/messages", handle_messages, methods=["POST"]),
            ],
            middleware=middleware,
        )

    async def run(self) -> None:
        import config as _cfg

        try:
            import mcp  # noqa: F401
        except ImportError:
            log.warning(
                "mcp_package_missing",
                detail="Install 'mcp' to enable the MCP server (pip install mcp)",
            )
            return

        import uvicorn

        uvi_config = uvicorn.Config(
            self._build_asgi_app(),
            host="0.0.0.0",  # nosec B104 — must be reachable from HA over LAN
            port=_cfg.MCP_PORT,
            log_level="warning",
        )
        self._uvicorn_server = uvicorn.Server(uvi_config)
        log.info("mcp_server_starting", port=_cfg.MCP_PORT, auth=bool(_cfg.MCP_TOKEN))
        await self._uvicorn_server.serve()

    def stop(self) -> None:
        if self._uvicorn_server is not None:
            self._uvicorn_server.should_exit = True
