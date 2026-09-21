"""S5-4: the dual-surface production mount battery.

The dry-run finding, repaired in code: the D5 two-token model was
config-enforced but never actually SERVED - only the agent MCP surface
was mounted; /transfer existed nowhere in production. This battery
proves the repaired mount: constant-time token check, bearer middleware
refusal before MCP parsing, path dispatch (both surfaces, 404
otherwise, lifespan fan-out), and the assembled dual app's routes.
"""

# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import urllib.error

import yaml
from pathlib import Path
from typing import Any

import pytest

from data_broker import server as server_mod
from data_broker.server import (
    BearerMiddleware,
    PathDispatch,
    build_dual_app,
    check_auth,
)

# --------------------------------------------------------------- check_auth


def test_check_auth_ok_and_constant_time_shape() -> None:
    assert check_auth("secret", "secret")
    assert not check_auth("wrong", "secret")
    assert not check_auth(None, "secret")
    assert not check_auth("", "secret")


# --------------------------------------------------------- BearerMiddleware


class _Recorder:
    def __init__(self) -> None:
        self.called = False

    async def __call__(self, scope, receive, send) -> None:
        self.called = True


def _http_scope(auth: str | None = None, path: str = "/mcp") -> dict:
    headers = []
    if auth is not None:
        headers.append((b"authorization", auth.encode()))
    return {"type": "http", "path": path, "headers": headers}


async def _noop_receive() -> dict:  # pragma: no cover - never awaits a body
    return {"type": "http.request"}


async def _noop_send(message: dict) -> None:  # pragma: no cover
    return None


async def test_bearer_middleware_passes_valid_token() -> None:
    inner = _Recorder()
    mw = BearerMiddleware(inner, "tok")
    await mw(_http_scope("Bearer tok"), _noop_receive, _noop_send)
    assert inner.called


async def test_bearer_middleware_refuses_missing_and_wrong() -> None:
    for auth in (None, "Bearer wrong", "wrong-scheme", ""):
        inner = _Recorder()
        mw = BearerMiddleware(inner, "tok")
        await mw(_http_scope(auth), _noop_receive, _noop_send)
        assert not inner.called  # refused before MCP parsing


async def test_bearer_middleware_passes_non_http_scopes() -> None:
    """Lifespan scopes carry no tool surface and pass through untouched."""
    inner = _Recorder()
    mw = BearerMiddleware(inner, "tok")
    await mw({"type": "lifespan"}, _noop_receive, _noop_send)
    assert inner.called


# ------------------------------------------------------------- PathDispatch


async def test_path_dispatch_routes_both_prefixes_and_404() -> None:
    seen: list[str] = []

    def app_for(name: str):
        async def app(scope, receive, send) -> None:
            seen.append(f"{name}:{scope['path']}")

        return app

    dispatch = PathDispatch({"/mcp": app_for("mcp"), "/transfer": app_for("xfer")})
    await dispatch(_http_scope(path="/mcp"), _noop_receive, _noop_send)
    await dispatch(_http_scope(path="/transfer/sub"), _noop_receive, _noop_send)
    await dispatch(_http_scope(path="/other"), _noop_receive, _noop_send)
    assert seen == ["mcp:/mcp", "xfer:/transfer/sub"]


async def test_path_dispatch_lifespan_fans_out() -> None:
    """Non-HTTP scopes drive EVERY wrapped surface's lifespan (a session
    manager whose lifespan never ran raises on the first request)."""
    seen: list[str] = []

    def app_for(name: str):
        async def app(scope, receive, send) -> None:
            seen.append(name)

        return app

    dispatch = PathDispatch({"/mcp": app_for("mcp"), "/transfer": app_for("xfer")})
    await dispatch({"type": "lifespan"}, _noop_receive, _noop_send)
    assert sorted(seen) == ["mcp", "xfer"]


# ------------------------------------------------------------ build_dual_app


def _cfg_stub() -> Any:
    class _Tokens:
        agent_token = "a" * 40
        transfer_token = "t" * 40

    class _Cfg:
        bind_host = "127.0.0.1"
        tokens = _Tokens()

    return _Cfg()


async def test_build_dual_app_routes_both_surfaces(tmp_path: Path) -> None:
    """The assembled dual app: /transfer with the TRANSFER token reaches
    the transfer surface (tools/call 'check_access' answers), and the
    agent token is refused there (cross-token dies at the middleware)."""
    import yaml

    from data_broker import run as run_mod

    os.environ.setdefault("WEBDAV_DUMMY", "pw")
    os.environ.setdefault("DATABROKER_AGENT_TOKEN", "a" * 40)
    os.environ.setdefault("DATABROKER_TRANSFER_TOKEN", "t" * 40)
    cfg_dict = {
        "bind_host": "127.0.0.1",
        "bind_port": 8478,
        "transport": "http",
        "auth": {"oauth": {"issuer": "https://issuer", "audience": "data-access-broker"}},
        "storage": {
            "data_dir": str(tmp_path / "data"),
            "grants_db": str(tmp_path / "g.sqlite3"),
            "audit_log": str(tmp_path / "a.jsonl"),
        },
        "accounts": {"webdav": [{"name": "scratch", "url": "http://127.0.0.1:8466",
                                 "username": "anonymous", "password_env": "WEBDAV_DUMMY"}]},
        "tokens": {"agent_env": "DATABROKER_AGENT_TOKEN",
                   "transfer_env": "DATABROKER_TRANSFER_TOKEN", "min_length": 32},
    }
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump(cfg_dict))
    cfg = run_mod.load_and_validate(str(p))
    _server, ctx, _bind, _gw = run_mod._boot(cfg)
    app = build_dual_app(cfg, ctx, cfg.tokens)

    async def _serve():
        import uvicorn

        config = uvicorn.Config(app, host="127.0.0.1", port=8478, log_level="warning")
        u = uvicorn.Server(config)
        serve_task = asyncio.ensure_future(u.serve())
        for _ in range(50):
            await asyncio.sleep(0.1)
            try:
                with socket.create_connection(("127.0.0.1", 8478), timeout=0.3):
                    break
            except OSError:
                continue
        else:
            pytest.fail("dual app never bound")
        try:
            # initialize + call with the TRANSFER token
            def post(payload: dict, token: str) -> tuple[int, bytes]:
                req = urllib.request.Request(
                    "http://127.0.0.1:8478/transfer",
                    data=json.dumps(payload).encode(),
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json, text/event-stream",
                        "Authorization": f"Bearer {token}",
                    },
                    method="POST",
                )
                try:
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        return resp.status, resp.read()
                except urllib.error.HTTPError as exc:
                    return exc.code, exc.read()

            init = {
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2026-07-28", "capabilities": {},
                           "clientInfo": {"name": "t", "version": "0"}},
            }
            st, _body = post(init, "t" * 40)
            assert st == 200, _st_err(_body)
            call = {
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "check_access",
                           "arguments": {"account": "scratch", "resource": "Docs", "op": "list"}},
            }
            st, body = post(call, "t" * 40)
            assert st == 200
            payload = json.loads(body)
            inner = json.loads(payload["result"]["content"][0]["text"])
            assert inner["status"] == "ok", inner
            # cross-token: the AGENT token on the transfer surface dies 401
            st, _body = post(call, "a" * 40)
            assert st == 401
        finally:
            serve_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await serve_task


def _st_err(body: bytes) -> str:
    return body.decode()[:200]


# ------------------------------------------------- lifespan fan-out regression


async def test_lifespan_initializes_both_session_managers(
    tmp_path, monkeypatch
) -> None:
    """A single lifespan scope cannot be consumed by two Starlette apps:
    the first app drained it, the second app's session manager never ran,
    and requests to that surface 500'd 'Task group is not initialized'
    (found live 2026-09-20: /mcp agent surface on production). The
    dispatch must fan the lifespan to BOTH apps — each with its own
    channel pair — and both managers must answer initialize."""
    monkeypatch.setenv("WEBDAV_DUMMY", "pw")
    monkeypatch.setenv("DATABROKER_AGENT_TOKEN", "a" * 40)
    monkeypatch.setenv("DATABROKER_TRANSFER_TOKEN", "t" * 40)
    import yaml
    cfg = {
        "bind_host": "127.0.0.1",
        "bind_port": 8471,
        "transport": "http",
        "auth": {"oauth": {"issuer": "https://issuer", "audience": "data-access-broker"}},
        "storage": {
            "data_dir": str(tmp_path / "data"),
            "grants_db": str(tmp_path / "grants.sqlite3"),
            "audit_log": str(tmp_path / "audit.jsonl"),
        },
        "accounts": {
            "webdav": [
                {
                    "name": "scratch",
                    "url": "http://127.0.0.1:8466",
                    "username": "anonymous",
                    "password_env": "WEBDAV_DUMMY",
                }
            ]
        },
        "tokens": {
            "agent_env": "DATABROKER_AGENT_TOKEN",
            "transfer_env": "DATABROKER_TRANSFER_TOKEN",
            "min_length": 32,
        },
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))
    from data_broker import run as run_mod

    server, ctx, _bind, _gateway = run_mod._boot(run_mod.load_and_validate(str(path)))
    loaded = run_mod.load_and_validate(str(path))
    dual = server_mod.build_dual_app(loaded, ctx, loaded.tokens)

    # drive the lifespan scope (startup only), as uvicorn would
    sent: list[dict] = []
    events: list[dict] = [{"type": "lifespan.startup"}]

    async def receive():
        return events.pop(0) if events else {"type": "lifespan.shutdown"}

    async def send(message):
        sent.append(message)

    await dual({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send)
    startup = [m for m in sent if m.get("type") == "lifespan.startup.complete"]
    assert startup, sent
    assert not [m for m in sent if m.get("type") == "lifespan.startup.failed"], sent
