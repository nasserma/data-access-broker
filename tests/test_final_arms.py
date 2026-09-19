"""The last coverage arms: config indirection/validation arms, run.py
defense-in-depth + serving, server bind guard, tools ValueError paths.
"""

# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
from typing import Any

import pytest
import yaml

from data_broker import run
from data_broker.config import ConfigError


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("DATABROKER_AGENT_TOKEN", "DATABROKER_TRANSFER_TOKEN", "WEBDAV_DUMMY"):
        monkeypatch.delenv(k, raising=False)


def _cfg(tmp_path: object, **overrides: object) -> dict:
    base: dict = {
        "bind_host": "127.0.0.1",
        "bind_port": 8471,
        "transport": "http",
        "auth": {"oauth": {"issuer": "https://i", "audience": "a"}},
        "storage": {
            "data_dir": str(tmp_path) + "/d",
            "grants_db": str(tmp_path) + "/g.sqlite3",
            "audit_log": str(tmp_path) + "/a.jsonl",
        },
        "accounts": {
            "webdav": [
                {
                    "name": "scratch",
                    "url": "http://127.0.0.1:8466",
                    "username": "u",
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
    base.update(overrides)
    return base


def _write(tmp_path: object, cfg: dict) -> str:
    import pathlib

    p = pathlib.Path(str(tmp_path)) / "c.yaml"
    p.write_text(yaml.safe_dump(cfg))
    return str(p)


# ----------------------------------------------------------------- config


def test_tokens_agent_env_empty_string(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path)
    cfg["tokens"]["agent_env"] = ""
    path = _write(tmp_path, cfg)
    with pytest.raises(ConfigError):
        run.load_and_validate(path)


def test_tokens_equal_guard_direct(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WEBDAV_DUMMY", "pw")
    monkeypatch.setenv("DATABROKER_AGENT_TOKEN", "a" * 40)
    monkeypatch.setenv("DATABROKER_TRANSFER_TOKEN", "a" * 40)
    path = _write(tmp_path, _cfg(tmp_path))
    with pytest.raises(ConfigError):
        run.load_and_validate(path)


def test_storage_missing_keys(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBDAV_DUMMY", "pw")
    cfg = _cfg(tmp_path)
    cfg["storage"].pop("grants_db")
    path = _write(tmp_path, cfg)
    with pytest.raises(ConfigError):
        run.load_and_validate(path)


def test_accounts_non_mapping(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBDAV_DUMMY", "pw")
    cfg = _cfg(tmp_path)
    cfg["accounts"] = "nope"
    path = _write(tmp_path, cfg)
    with pytest.raises(ConfigError):
        run.load_and_validate(path)


def test_accounts_entry_non_mapping(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBDAV_DUMMY", "pw")
    cfg = _cfg(tmp_path)
    cfg["accounts"]["webdav"] = ["flat-string"]
    path = _write(tmp_path, cfg)
    with pytest.raises(ConfigError):
        run.load_and_validate(path)


def test_auth_default_empty_when_given(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WEBDAV_DUMMY", "pw")
    monkeypatch.setenv("DATABROKER_AGENT_TOKEN", "a" * 40)
    monkeypatch.setenv("DATABROKER_TRANSFER_TOKEN", "t" * 40)
    cfg = _cfg(tmp_path, transport="stdio")
    cfg.pop("auth")
    cfg["auth"] = {"stdio": {"token_env": "DATABROKER_AGENT_TOKEN"}}
    path = _write(tmp_path, cfg)
    out = run.load_and_validate(path)
    assert out.transport == "stdio"


def test_auth_non_mapping(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _cfg(tmp_path)
    cfg["auth"] = "nope"
    path = _write(tmp_path, cfg)
    with pytest.raises(ConfigError):
        run.load_and_validate(path)


def test_config_invalid_yaml(tmp_path: object) -> None:
    import pathlib

    from data_broker import config

    p = pathlib.Path(str(tmp_path)) / "c.yaml"
    p.write_text("a: [unclosed\n")
    with pytest.raises(ConfigError):
        config.load_config(str(p))


def test_gateway_unregistered_adapter_refuses_boot(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gateway section naming an adapter this broker has not registered
    refuses the boot (the core's registry gate; 'telegram' is not in the
    data broker's adapter set, which registers 'matrix' at boot)."""
    monkeypatch.setenv("WEBDAV_DUMMY", "pw")
    monkeypatch.setenv("DATABROKER_AGENT_TOKEN", "a" * 40)
    monkeypatch.setenv("DATABROKER_TRANSFER_TOKEN", "t" * 40)
    cfg = _cfg(tmp_path)
    cfg["gateway"] = {"telegram": {"chat_id": 1, "token_env": "WEBDAV_DUMMY", "allowed_senders": ["@o:x"]}}
    path = _write(tmp_path, cfg)
    from access_broker_core.gateways import GatewayConfigError

    with pytest.raises(GatewayConfigError):
        run.boot(path)


# -------------------------------------------------------------------- run


def test_serve_stdio_arm(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """_serve over stdio transport: the server's run method records the
    call (a fake server, real _serve wiring)."""
    monkeypatch.setenv("WEBDAV_DUMMY", "pw")
    monkeypatch.setenv("DATABROKER_AGENT_TOKEN", "a" * 40)
    monkeypatch.setenv("DATABROKER_TRANSFER_TOKEN", "t" * 40)
    path = _write(tmp_path, _cfg(tmp_path, transport="stdio"))
    server, _ctx, _bind, _gateway = run._boot(run.load_and_validate(path))

    called = {"stdio": 0}

    async def _fake_stdio() -> None:
        called["stdio"] += 1

    server.run_stdio_async = _fake_stdio  # type: ignore[method-assign]
    asyncio.run(run._serve(server, "stdio", ("127.0.0.1", 8471)))
    assert called["stdio"] == 1


def test_serve_with_gateway_starts_adapter_and_sweep(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_serve with a gateway wired: the adapter starts and the sweep
    loop runs alongside the server (the S5-1 threading arm)."""
    monkeypatch.setenv("WEBDAV_DUMMY", "pw")
    monkeypatch.setenv("DATABROKER_AGENT_TOKEN", "a" * 40)
    monkeypatch.setenv("DATABROKER_TRANSFER_TOKEN", "t" * 40)
    monkeypatch.setenv("DATABROKER_GATEWAY_TOKEN", "g" * 40)

    import access_broker_core.gateways as gw_mod

    import data_broker.gateways as broker_gw_mod

    def fake_builder(core, fields, approver):
        class FakeTransport:
            async def send_message(self, text: str) -> str:
                return "evt"

            async def add_reaction(self, event_id: str, emoji: str) -> None:
                return None

        class FakeAdapter:
            def __init__(self, core, transport) -> None:
                self.core = core
                self._transport = transport
                self.calls: list[str] = []

            async def start(self) -> None:
                self.calls.append("start")

            async def stop(self) -> None:
                self.calls.append("stop")

        adapter = FakeAdapter(core, FakeTransport())
        core._transport = adapter._transport  # noqa: SLF001 - wiring
        return adapter, fields["room_id"]

    gw_mod.register_adapter("matrix", fake_builder)
    monkeypatch.setattr(broker_gw_mod, "register_adapter", lambda *a, **k: None)
    try:
        cfg = _cfg(tmp_path)
        cfg["gateway"] = {
            "matrix": {
                "homeserver_url": "https://m",
                "user_id": "@approvals:x",
                "access_token_env": "DATABROKER_GATEWAY_TOKEN",
                "room_id": "!r:x",
                "allowed_senders": ["@o:x"],
            }
        }
        path = _write(tmp_path, cfg)
        server, _ctx, _bind, gateway = run._boot(run.load_and_validate(path))
        core, adapter = gateway

        calls = {"http": 0}

        async def _fake_http(host: str, port: int) -> None:
            # yield once so the scheduled adapter-start task actually runs
            # (the real server coroutine awaits I/O here)
            await asyncio.sleep(0)
            calls["http"] += 1

        server.run_streamable_http_async = _fake_http  # type: ignore[method-assign]
        asyncio.run(run._serve(server, "http", ("127.0.0.1", 8471), adapter=adapter, core=core))
        assert calls["http"] == 1
        assert adapter.calls[0] == "start"  # the gateway adapter actually started
        assert adapter.calls[-1] == "stop"  # and shut down cleanly with the server
    finally:
        gw_mod._ADAPTER_BUILDERS.pop("matrix", None)


def test_serve_http_arm(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBDAV_DUMMY", "pw")
    monkeypatch.setenv("DATABROKER_AGENT_TOKEN", "a" * 40)
    monkeypatch.setenv("DATABROKER_TRANSFER_TOKEN", "t" * 40)
    path = _write(tmp_path, _cfg(tmp_path))
    server, ctx, bind, _gateway = run._boot(run.load_and_validate(path))
    called = {"n": 0}

    async def _fake_http(host: str, port: int) -> None:
        called["n"] += 1

    server.run_streamable_http_async = _fake_http  # type: ignore[method-assign]
    asyncio.run(run._serve(server, "http", bind))
    assert called["n"] == 1


# ------------------------------------------------------------------ server


def test_server_validate_bind_host() -> None:
    from data_broker.server import validate_bind_host

    assert validate_bind_host("127.0.0.1") == "127.0.0.1"
    for host in ("0.0.0.0", "::", ""):
        with pytest.raises(ValueError):
            validate_bind_host(host)


# ------------------------------------------------------------------ tools


def _ctx(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Boot the production wiring with per-test tmp storage (the boot-
    path battery's shape)."""
    monkeypatch.setenv("WEBDAV_DUMMY", "pw")
    monkeypatch.setenv("DATABROKER_AGENT_TOKEN", "a" * 40)
    monkeypatch.setenv("DATABROKER_TRANSFER_TOKEN", "t" * 40)
    import pathlib

    base: dict = {
        "bind_host": "127.0.0.1",
        "bind_port": 8471,
        "transport": "http",
        "auth": {"oauth": {"issuer": "https://i", "audience": "a"}},
        "storage": {
            "data_dir": str(tmp_path) + "/d",
            "grants_db": str(tmp_path) + "/g.sqlite3",
            "audit_log": str(tmp_path) + "/a.jsonl",
        },
        "accounts": {
            "webdav": [
                {
                    "name": "scratch",
                    "url": "http://127.0.0.1:8466",
                    "username": "u",
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
    p = pathlib.Path(str(tmp_path)) / "boot.yaml"
    p.write_text(yaml.safe_dump(base))
    _server, ctx = run.boot(str(p))
    return ctx


def test_execute_pending_path(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """A gated op with no grant submits and pends (the pending path's
    envelope, through the real boot)."""
    ctx = _ctx(tmp_path, monkeypatch)
    result = asyncio.run(ctx.execute("scratch", "Docs/n.txt", "trash", lambda: None))
    assert result["status"] == "pending"


def test_read_tool_via_surface(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """The curated read tool reaches the real backend through the gate:
    with the scratch server DOWN the backend's connection failure maps
    to the error envelope (the gate never leaks tracebacks)."""
    ctx = _ctx(tmp_path, monkeypatch)
    specs = {
        s["name"]: s["handler"]
        for s in __import__("data_broker.tools", fromlist=["build_agent_tools"]).build_agent_tools(ctx)
    }
    result = asyncio.run(specs["read"]("scratch", "Docs/n.txt"))
    assert result["status"] in ("ok", "error")  # envelope contract holds
    assert result["status"] != "pending"
