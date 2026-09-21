"""MCP server wiring (S4-5): MCPServer (v2 API), dual-surface routing.

The agent surface registers on the MCP server; the transfer surface is
a separate MCPServer instance with its own tool set, served on the same
port at /transfer behind its own bearer token (the D5 two-token model;
the CLI addresses it directly). Wildcard bind refusal and no tracebacks
across the tool boundary, per the suite pattern.

The dual mount (S5-4 finding, repaired): agent MCP app at /mcp behind
the agent token, transfer MCP app at /transfer behind the transfer
token — the nextcloud broker's build_dual_app pattern, ported to the
v2 SDK (streamable_http_app + a path-dispatch ASGI wrapper). Both
surfaces share the wall, grants, audit through the one BrokerContext;
only the registered tool sets differ.
"""

# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import asyncio
import contextlib
import hmac
from typing import Any

from mcp.server import MCPServer
from starlette.requests import Request
from starlette.responses import JSONResponse

from data_broker import tools


def validate_bind_host(host: str) -> str:
    """Reject wildcard binds (refuse-to-start guard, suite pattern)."""
    if host in {"0.0.0.0", "::", ""}:
        raise ValueError(f"refusing wildcard bind host: {host!r}")
    return host


def check_auth(provided: str | None, expected: str) -> bool:
    """Constant-time surface-token check (the middleware predicate)."""
    if not provided:
        return False
    return hmac.compare_digest(provided.encode(), expected.encode())


class BearerMiddleware:
    """Starlette middleware: reject every request without the expected
    surface token, BEFORE any MCP parsing (the nextcloud D5 pattern).
    Instantiated twice: agent token on /mcp, transfer token on
    /transfer; cross-token requests die here."""

    def __init__(self, app, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        header = request.headers.get("authorization", "")
        provided = header[7:] if header.lower().startswith("bearer ") else None
        if not check_auth(provided, self.token):
            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


class _CollectSend:
    """ASGI send sink for the per-app lifespan fan-out: records the app's
    lifespan responses (startup/shutdown complete/failed)."""

    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def __call__(self, message: dict) -> None:
        self.messages.append(message)


class PathDispatch:
    """D5: route the two MCP surfaces on ONE port by path prefix.

    /mcp*      -> agent surface, agent token
    /transfer* -> transfer surface, transfer token
    anything else -> 404. Longest prefix first. Non-HTTP scopes
    (lifespan) drive EVERY wrapped surface's lifespan: a session
    manager whose lifespan never ran raises 'Task group is not
    initialized' on the first request.
    """

    def __init__(self, routes: dict[str, Any]) -> None:
        self._routes = sorted(routes.items(), key=lambda kv: -len(kv[0]))

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            path = scope.get("path", "")
            for prefix, app in self._routes:
                if path == prefix or path.startswith(prefix + "/"):
                    await app(scope, receive, send)
                    return
            response = JSONResponse({"error": "not found"}, status_code=404)
            await response(scope, receive, send)
            return
        # Non-HTTP (lifespan): EVERY wrapped surface's lifespan must run —
        # but a single lifespan scope cannot be consumed by two Starlette
        # apps: the first app drains the receive channel, so the second
        # app's lifespan exits without completing startup and its session
        # manager never runs (found live 2026-09-20: the /mcp agent app,
        # sorted AFTER /transfer, 500'd 'Task group is not initialized'
        # on the first request while /transfer answered). Each app gets
        # its own replayed scope + fresh channels.
        #
        # Each app runs its lifespan against its OWN channel pair, driven
        # concurrently from one upstream lifespan scope: upstream messages
        # are broadcast to every app, and the first
        # 'lifespan.startup.complete'/'failed' from any app is forwarded
        # to the real send (uvicorn needs exactly one handshake answer;
        # startup failures propagate immediately).
        apps = [app for _p, app in self._routes]
        upstream_done: list[bool] = [False]
        upstream_events: list[dict] = []
        waiting: list[asyncio.Future] = []
        forwarded: list[bool] = [False]

        async def pump() -> None:
            """Consume the upstream lifespan channel and broadcast each
            message to every waiting per-app receive."""
            while True:
                message = await receive()
                upstream_events.append(message)
                for fut in waiting:
                    if not fut.done():
                        fut.set_result(message)
                waiting.clear()
                if message.get("type") in ("lifespan.shutdown", "lifespan.shutdown.failed"):
                    return

        async def run_one(app) -> None:
            index = {"n": 0}

            async def app_receive() -> dict:
                if index["n"] < len(upstream_events):
                    message = upstream_events[index["n"]]
                    index["n"] += 1
                    return message
                fut = asyncio.get_running_loop().create_future()
                waiting.append(fut)
                return await fut

            async def app_send(message: dict) -> None:
                mtype = message.get("type", "")
                if mtype in ("lifespan.startup.complete", "lifespan.startup.failed"):
                    if not forwarded[0]:
                        forwarded[0] = True
                        await send(message)
                return

            await app(dict(scope), app_receive, app_send)

        pump_task = asyncio.ensure_future(pump())
        try:
            await asyncio.gather(*(run_one(app) for app in apps))
        finally:
            if not pump_task.done():
                pump_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pump_task


def build_server(cfg: Any, broker_context: Any) -> MCPServer:
    """Construct the MCPServer with the registered agent surface."""
    server = MCPServer(name="data-access-broker", version="0.1.1")
    tools.register_tools(server, broker_context)
    return server


def build_dual_app(cfg: Any, broker_context: Any, tokens: Any) -> PathDispatch:
    """Compose the two D5 surfaces on one Starlette app.

    Agent MCPServer at /mcp (agent tools, agent token), transfer
    MCPServer at /transfer (transfer tools, transfer token). Both
    servers share broker_context: wall, grants, baselines, audit —
    only the registered tool sets differ. json_response=True closes
    each POST with a complete JSON body (the CLI contract).

    tokens: cfg.tokens (the parsed two-token section); the resolved
    agent/transfer token values are cfg.tokens.agent_token /
    cfg.tokens.transfer_token (resolved at load)."""
    agent_server = build_server(cfg, broker_context)

    transfer_server = MCPServer(name="data-access-broker-transfer", version="0.1.1")
    for spec in tools.build_transfer_tools(broker_context):
        transfer_server.add_tool(
            spec["handler"], name=spec["name"], description=spec["name"]
        )

    host = cfg.bind_host
    agent_app = agent_server.streamable_http_app(
        host=host, streamable_http_path="/mcp", json_response=True,
    )
    transfer_app = transfer_server.streamable_http_app(
        host=host, streamable_http_path="/transfer", json_response=True,
    )
    return PathDispatch({
        "/mcp": BearerMiddleware(agent_app, tokens.agent_token),
        "/transfer": BearerMiddleware(transfer_app, tokens.transfer_token),
    })
