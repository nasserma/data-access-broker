# SPDX-License-Identifier: GPL-3.0-or-later
"""Local MCP fetcher: a minimal client-side stdio MCP server exposing
the data broker's transfer path as two ordinary tools, `fetch` and
`push`, so an AI harness can move bulk file content WITHOUT a shell
command (the 2026-09-28 operational gap: the only sanctioned content
path was the CLI, and harness terminal gates deadlock unattended
sessions).

Runs on the AGENT HOST next to the harness:

    python -m data_broker.client_mcp

Token from DATABROKER_TRANSFER_TOKEN env only (never argv, never a
config value); broker transfer URL from DATABROKER_URL env (default
http://127.0.0.1:8471/transfer). No policy, no grants, no audit lives
here — the remote broker enforces the wall exactly as it does for the
CLI; this server only stages bytes.

D5 invariant preserved: bulk content NEVER transits the LLM context.
The tool results carry a local staged path plus the sha256 manifest
only — never the bytes. The agent then reads the staged file with its
normal local file tools, inside the context budget of its choice.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from mcp.server import MCPServer

from data_broker import client as _client
from data_broker.client import (
    EXIT_OK,
)


def _result_for(rc: int, payload: dict[str, Any]) -> dict[str, Any]:
    """Map the client core's (exit_code, manifest) contract to a single
    JSON envelope for the tool result. Metadata only — never content."""
    out = {"status": "ok" if rc == EXIT_OK else "error", "exit_code": rc}
    out.update(payload)
    return out


def build_server() -> MCPServer:
    """Construct the local fetcher server with fetch and push tools."""
    server = MCPServer(name="data-broker-client", version=_client.CLIENT_VERSION)

    async def fetch(account: str, resource: str) -> str:
        """Fetch a file from the data broker into content-addressed local
        staging (verify-then-stage). Returns the staged path and sha256
        manifest — never the content."""
        rc, payload = await asyncio.to_thread(
            _client.fetch, account, resource
        )
        return json.dumps(_result_for(rc, payload))

    async def push(account: str, resource: str, path: str) -> str:
        """Push a local file to the data broker (verify-then-write on the
        server side). Returns the pushed resource and sha256 — never the
        content."""
        rc, payload = await asyncio.to_thread(
            _client.push, account, resource, path
        )
        return json.dumps(_result_for(rc, payload))

    server.add_tool(
        fetch,
        name="fetch",
        description=(
            "Fetch a file from the data broker into local content-addressed "
            "staging (requires an active read grant server-side). Returns "
            "{status, exit_code, staged, sha256, size}: a local file path "
            "and manifest, never file content."
        ),
    )
    server.add_tool(
        push,
        name="push",
        description=(
            "Push a local file to the data broker (server verifies sha256 "
            "before writing; requires an active write grant server-side). "
            "Returns {status, exit_code, pushed, sha256, size}, never "
            "content."
        ),
    )
    return server


async def main() -> None:
    """Serve over stdio. run_stdio_async owns the stdio_server context
    (opening a second one would double-claim fd 0 — found live)."""
    server = build_server()
    await server.run_stdio_async()


if __name__ == "__main__":
    asyncio.run(main())
