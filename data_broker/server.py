"""MCP server wiring (S4-5): MCPServer (v2 API), dual-surface routing.

The agent surface registers on the MCP server; the transfer surface is
a separate route behind its own token (the CLI addresses it directly).
Wildcard bind refusal and no tracebacks across the tool boundary, per
the suite pattern.
"""

# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer

from data_broker import tools


def validate_bind_host(host: str) -> str:
    """Reject wildcard binds (refuse-to-start guard, suite pattern)."""
    if host in {"0.0.0.0", "::", ""}:
        raise ValueError(f"refusing wildcard bind host: {host!r}")
    return host


def build_server(cfg: Any, broker_context: Any) -> MCPServer:
    """Construct the MCPServer with the registered agent surface."""
    server = MCPServer(name="data-access-broker", version="0.1.0")
    tools.register_tools(server, broker_context)
    return server
