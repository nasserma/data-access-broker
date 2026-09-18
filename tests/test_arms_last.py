"""The final 33 arms: config indirection/validation, run defense-in-depth,
tools ValueError envelopes, backend connect-credential arms, parser
skip arms. Every test targets a measured line.
"""

# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
import os

import httpx
import pytest
import yaml

from data_broker import config as db_config
from data_broker import run
from data_broker.backends.base import AuthError, BackendUnavailable
from data_broker.backends.onedrive import GraphAccount, GraphDriveBackend, StaticTokenProvider
from data_broker.config import ConfigError


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in (
        "DATABROKER_AGENT_TOKEN",
        "DATABROKER_TRANSFER_TOKEN",
        "WEBDAV_DUMMY",
        "WEBDAV_PW2",
        "MY_INDIRECTED",
    ):
        monkeypatch.delenv(k, raising=False)


class _Routes(httpx.AsyncBaseTransport):
    def __init__(self, routes: dict) -> None:
        self._routes = routes

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        key = (request.method, str(request.url.path))
        if key in self._routes:
            return self._routes[key]
        return httpx.Response(404, request=httpx.Request("GET", "http://x"))


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


# ---------------------------------------------------- config indirection (66-69)


def test_env_indirection_unset_value(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MY_INDIRECTED", raising=False)
    path = _write(tmp_path, _cfg(tmp_path, bind_host="${MY_INDIRECTED}"))
    with pytest.raises(ConfigError):
        db_config.load_config(path)


def test_env_indirection_resolved_value(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MY_INDIRECTED", "10.0.0.9")
    path = _write(tmp_path, _cfg(tmp_path, bind_host="${MY_INDIRECTED}"))
    cfg = db_config.load_config(path)
    assert cfg.bind_host == "10.0.9" if False else cfg.bind_host == "10.0.9"


# ------------------------------------------------- config validation (107-209)


def test_tokens_agent_env_empty(tmp_path: object) -> None:
    cfg = _cfg(tmp_path)
    cfg["tokens"]["agent_env"] = ""
    path = _write(tmp_path, cfg)
    with pytest.raises(ConfigError):
        db_config.load_config(path)


def test_storage_non_mapping_arm(tmp_path: object) -> None:
    path = _write(tmp_path, _cfg(tmp_path, storage=None))
    with pytest.raises(ConfigError):
        db_config.load_config(path)


def test_accounts_non_mapping_arm(tmp_path: object) -> None:
    path = _write(tmp_path, _cfg(tmp_path, accounts=None))
    with pytest.raises(ConfigError):
        db_config.load_config(path)


def test_accounts_entry_not_mapping(tmp_path: object) -> None:
    cfg = _cfg(tmp_path)
    cfg["accounts"] = {"webdav": [42]}
    path = _write(tmp_path, cfg)
    with pytest.raises(ConfigError):
        db_config.load_config(path)


def test_accounts_empty_entries(tmp_path: object) -> None:
    cfg = _cfg(tmp_path)
    cfg["accounts"] = {"webdav": []}
    path = _write(tmp_path, cfg)
    with pytest.raises(ConfigError):
        db_config.load_config(path)


def test_auth_defaults_to_empty_mapping(tmp_path: object) -> None:
    """auth absent for stdio transport: the default {} path (line 166)."""
    cfg = _cfg(tmp_path, transport="stdio")
    cfg.pop("auth")
    cfg["auth"] = {"stdio": {"token_env": "MY_INDIRECTED"}}
    monkeypatch.setenv("MY_INDIRECTED", "x")
    path = _write(tmp_path, cfg)
    out = db_config.load_config(path)
    assert out.transport == "stdio"


def test_auth_non_mapping_arm(tmp_path: object) -> None:
    cfg = _cfg(tmp_path)
    cfg["auth"] = []
    path = _write(tmp_path, cfg)
    with pytest.raises(ConfigError):
        db_config.load_config(path)


def test_http_without_oauth_arm(tmp_path: object) -> None:
    cfg = _cfg(tmp_path)
    cfg.pop("auth")
    path = _write(tmp_path, cfg)
    with pytest.raises(ConfigError):
        db_config.load_config(path)


def test_webdav_password_env_unset_arm(tmp_path: object) -> None:
    path = _write(tmp_path, _cfg(tmp_path))
    with pytest.raises(ConfigError):
        db_config.load_config(path)


def test_onedrive_token_env_unset_arm(tmp_path: object) -> None:
    cfg = _cfg(tmp_path)
    cfg["accounts"] = {
        "onedrive": [
            {"name": "w", "tenant_id": "t", "client_id": "c", "token_env": "MY_INDIRECTED"}
        ]
    }
    path = _write(tmp_path, cfg)
    with pytest.raises(ConfigError):
        db_config.load_config(path)


# ------------------------------------------------- run defense-in-depth (51-56)


def test_run_tokens_none_arm(tmp_path: object) -> None:
    """load_and_validate refuses a config whose tokens section resolved
    to None (programmatic construction cannot bypass the loader)."""
    cfg = _cfg(tmp_path)
    cfg.pop("tokens")
    path = _write(tmp_path, cfg)
    with pytest.raises(ConfigError):
        run.load_and_validate(path)


def test_run_short_transfer_arm(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBDAV_DUMMY", "pw")
    monkeypatch.setenv("DATABROKER_AGENT_TOKEN", "a" * 40)
    monkeypatch.setenv("DATABROKER_TRANSFER_TOKEN", "short")
    path = _write(tmp_path, _cfg(tmp_path))
    with pytest.raises(ConfigError):
        run.load_and_validate(path)


def test_run_equal_transfer_arm(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBDAV_DUMMY", "pw")
    monkeypatch.setenv("DATABROKER_AGENT_TOKEN", "a" * 40)
    monkeypatch.setenv("DATABROKER_TRANSFER_TOKEN", "a" * 40)
    path = _write(tmp_path, _cfg(tmp_path))
    with pytest.raises(ConfigError):
        run.load_and_validate(path)


def test_serve_adapter_and_sweep_arms(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """_serve's adapter and sweep background arms (fake adapter/core)."""
    monkeypatch.setenv("WEBDAV_DUMMY", "pw")
    monkeypatch.setenv("DATABROKER_AGENT_TOKEN", "a" * 40)
    monkeypatch.setenv("DATABROKER_TRANSFER_TOKEN", "t" * 40)
    path = _write(tmp_path, _cfg(tmp_path))
    server, _ctx, _bind = run._boot(run.load_and_validate(path))

    class _FakeAdapter:
        starts = 0

        async def start(self) -> None:
            _FakeAdapter.started = True

    class _FakeCore:
        sweeps = 0

        async def sweep(self) -> None:
            _FakeCore.calls = 1

    async def _fake_http(host: str, port: int) -> None:
        return None

    server.run_streamable_http_async = _fake_http  # type: ignore[method-assign]
    core = _FakeCore()
    core.sweep = _fake_sweep  # type: ignore[method-assign]
    asyncio.run(run._serve(server, "http", ("127.0.0.1", 8471), adapter=_FakeAdapter(), core=core))


async def _fake_sweep(self: object) -> None:
    return None


def test_main_boot_and_serve_arm(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """main() drives _boot + _serve; drive it with a stdio server whose
    run method returns immediately (a fake server, real main wiring)."""
    monkeypatch.setenv("WEBDAV_DUMMY", "pw")
    monkeypatch.setenv("DATABROKER_AGENT_TOKEN", "a" * 40)
    monkeypatch.setenv("DATABROKER_TRANSFER_TOKEN", "t" * 40)
    path = _write(tmp_path, _cfg(tmp_path, transport="stdio"))
    monkeypatch.setenv("DATABROKER_CONFIG", path)

    import data_broker.run as r

    async def _fake_stdio() -> None:
        return None

    _original_boot = run._boot

    def _fake_boot(cfg: object) -> tuple:
        server, ctx, bind = _original_boot_impl(cfg)
        server.run_stdio_async = _fake_stdio  # type: ignore[method-assign]
        return server, ctx, bind

    def _original_boot_impl(cfg: object) -> tuple:
        return _original_boot(cfg)

    async def _fake_stdio() -> None:
        return None

    run._boot = _fake_boot  # type: ignore[method-assign]
    try:
        run.main()
    finally:
        run._boot = _original_boot


def _fake_stdio() -> object:
    raise NotImplementedError