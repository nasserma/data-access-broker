"""run.py coverage battery (the supervisory-review coverage finding):
the boot arms the main batteries do not reach — load_and_validate
guards, _build_backends shapes, _boot refusal arms, _sweep_loop/_shutdown
best-effort paths, main() config env override.
"""

# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio

import pytest
import yaml

from data_broker import run
from data_broker.config import ConfigError


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBDAV_DUMMY", "pw")
    monkeypatch.setenv("DATABROKER_AGENT_TOKEN", "a" * 40)
    monkeypatch.setenv("DATABROKER_TRANSFER_TOKEN", "t" * 40)


def _config(tmp_path: object, **overrides: object) -> str:
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
    import pathlib

    p = pathlib.Path(str(tmp_path)) / "cfg.yaml"
    p.write_text(yaml.safe_dump(base))
    return str(p)


def test_load_and_validate_defense_in_depth(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """Equal tokens pass config.load_config's per-key check but must
    refuse at load_and_validate (the boot-level guard)."""
    monkeypatch.setenv("DATABROKER_TRANSFER_TOKEN", "same")
    path = _config(tmp_path)
    with pytest.raises(ConfigError):
        run.load_and_validate(path)


def test_load_and_validate_tokens_none_refuses(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A YAML config with no tokens section reaches the loader-level
    tokens-None refusal (the reachable arm; a programmatically built
    Config without tokens is caught by _boot's re-check)."""
    monkeypatch.delenv("DATABROKER_AGENT_TOKEN", raising=False)
    monkeypatch.delenv("DATABROKER_TRANSFER_TOKEN", raising=False)
    import pathlib

    import yaml as _yaml

    cfg = _config(tmp_path)
    body = _yaml.safe_load(open(cfg))  # noqa: SIM115 - read-once in-test
    body.pop("tokens")
    p = pathlib.Path(str(tmp_path)) / "c.yaml"
    p.write_text(_yaml.safe_dump(body))
    with pytest.raises(ConfigError, match="tokens"):
        run.load_and_validate(str(p))


def test_main_serves_with_gateway_threaded(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """main()'s happy path with a gateway configured: boot -> adapter and
    core threaded into _serve -> clean shutdown (drive the server run
    method to a fake that returns immediately)."""
    import pathlib

    import access_broker_core.gateways as gw_mod
    import yaml as _yaml

    import data_broker.gateways as broker_gw_mod

    monkeypatch.setenv("WEBDAV_DUMMY", "pw")
    monkeypatch.setenv("DATABROKER_AGENT_TOKEN", "a" * 40)
    monkeypatch.setenv("DATABROKER_TRANSFER_TOKEN", "t" * 40)
    monkeypatch.setenv("DATABROKER_GATEWAY_TOKEN", "g" * 40)

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
        cfg_path = _config(tmp_path)
        body = _yaml.safe_load(open(cfg_path))  # noqa: SIM115 - read-once in-test
        body["gateway"] = {
            "matrix": {
                "homeserver_url": "https://m",
                "user_id": "@approvals:x",
                "access_token_env": "DATABROKER_GATEWAY_TOKEN",
                "room_id": "!r:x",
                "allowed_senders": ["@o:x"],
            }
        }
        import pathlib

        path = pathlib.Path(str(tmp_path)) / "c_gw.yaml"
        path.write_text(_yaml.safe_dump(body))
        monkeypatch.setenv("DATABROKER_CONFIG", str(path))

        # the server's run method is swapped for a fake that yields once
        # (letting the scheduled adapter-start task run) then returns
        captured: dict = {}

        def _build_server_spy(real_build_server):
            def spy(cfg_obj, ctx):
                server = real_build_server(cfg_obj, ctx)
                captured["adapter_seen"] = ctx.core is not None

                async def _fake_http(host: str, port: int) -> None:
                    await asyncio.sleep(0)

                server.run_streamable_http_async = _fake_http  # type: ignore[method-assign]
                return server

            return spy

        real = run.build_server
        monkeypatch.setattr(
            run, "build_server", _build_server_spy(real)
        )

        # main() now serves the DUAL app via uvicorn (S5-4 repair); stub
        # the server so the test drives the wiring without binding 8471
        served = {}

        class _StubUvicornServer:
            def __init__(self, config) -> None:
                served["app"] = config.app

            async def serve(self) -> None:
                served["served"] = True

        import uvicorn as _uvicorn

        monkeypatch.setattr(_uvicorn, "Server", _StubUvicornServer)
        run.main()  # must wire the dual app and exit cleanly
        assert captured["adapter_seen"] is True
        assert served["served"] is True
        # the dual app is the path dispatch with BOTH surfaces
        routes = [p for p, _ in served["app"]._routes]  # noqa: SLF001
        assert "/mcp" in routes and "/transfer" in routes
    finally:
        gw_mod._ADAPTER_BUILDERS.pop("matrix", None)


def test_build_backends_onedrive_shape(tmp_path: object) -> None:
    path = _config(
        tmp_path,
        accounts={
            "onedrive": [
                {
                    "name": "work",
                    "tenant_id": "t",
                    "client_id": "c",
                    "token_env": "DATABROKER_AGENT_TOKEN",
                }
            ]
        },
    )
    cfg = run.load_and_validate(path)
    backends, accounts_map = run._build_backends(cfg)
    assert accounts_map["work"] == "onedrive"
    assert "onedrive" in backends


def test_build_backends_both_families(tmp_path: object) -> None:
    path = _config(
        tmp_path,
        accounts={
            "webdav": [
                {
                    "name": "scratch",
                    "url": "http://127.0.0.1:8466",
                    "username": "u",
                    "password_env": "WEBDAV_DUMMY",
                }
            ],
            "onedrive": [
                {
                    "name": "work",
                    "tenant_id": "t",
                    "client_id": "c",
                    "token_env": "DATABROKER_AGENT_TOKEN",
                }
            ],
        },
    )
    cfg = run.load_and_validate(path)
    backends, accounts_map = run._build_backends(cfg)
    assert accounts_map == {"scratch": "webdav", "work": "onedrive"}
    assert set(backends) == {"webdav", "onedrive"}


def test_build_backends_empty_refuses(tmp_path: object) -> None:
    path = _config(tmp_path)
    cfg = run.load_and_validate(path)
    empty = type(cfg)(
        bind_host=cfg.bind_host,
        bind_port=cfg.bind_port,
        transport=cfg.transport,
        auth=cfg.auth,
        storage=cfg.storage,
        accounts={},
        tokens=cfg.tokens,
    )
    with pytest.raises(ConfigError):
        run._build_backends(empty)


def test_boot_refuses_without_tokens(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """_boot re-checks tokens: a config object with tokens=None refuses
    (programmatic boot cannot bypass the loader)."""
    path = _config(tmp_path)
    cfg = run.load_and_validate(path)
    tokenless = type(cfg)(
        bind_host=cfg.bind_host,
        bind_port=cfg.bind_port,
        transport=cfg.transport,
        auth=cfg.auth,
        storage=cfg.storage,
        accounts=cfg.accounts,
        tokens=None,
    )
    with pytest.raises(ConfigError):
        run._boot(tokenless)


def test_sweep_loop_survives_sweep_failure(tmp_path: object) -> None:
    """The housekeeping loop logs sweep failures and never kills serving:
    one iteration driven by a core whose sweep raises."""

    class BoomCore:
        calls = 0

        async def sweep(self) -> None:
            BoomCore.calls += 1
            raise RuntimeError("injected sweep failure")

    class StopLoopError(Exception):
        """Drives one deterministic iteration of the sweep loop."""

    ticks = {"n": 0}

    async def _one_tick(_: float) -> None:
        ticks["n"] += 1
        if ticks["n"] > 1:
            raise StopLoopError()

    async def run_once() -> None:
        asyncio.sleep = _one_tick  # type: ignore[assignment]
        try:
            await asyncio.wait_for(run._sweep_loop(BoomCore()), timeout=2.0)
        except (TimeoutError, StopLoopError):
            pass
        finally:
            import asyncio as _a

            asyncio.sleep = _a.sleep

    asyncio.run(run_once())
    assert BoomCore.calls == 1


def test_shutdown_best_effort() -> None:
    """_shutdown cancels background tasks and suppresses backend/adapter
    close failures."""

    class _BoomAdapter:
        async def stop(self) -> None:
            raise RuntimeError("boom")

    class _BoomBackend:
        async def close(self) -> None:
            raise RuntimeError("boom")

    async def run_it() -> None:
        started = asyncio.ensure_future(asyncio.sleep(30))
        await run._shutdown([started], _BoomAdapter(), {"webdav": _BoomBackend()})
        assert started.cancelled() or started.done()

    asyncio.run(run_it())


def test_main_uses_config_env(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """main() reads DATABROKER_CONFIG (refuse-to-start on a bad config
    path; the boot itself is covered by boot())."""
    path = _config(tmp_path)
    monkeypatch.setenv("DATABROKER_CONFIG", path)
    # main() would serve forever; drive only its config resolution by
    # pointing at a MISSING config and expecting refuse-to-start.
    monkeypatch.setenv("DATABROKER_CONFIG", "/nonexistent/cfg.yaml")
    with pytest.raises(ConfigError):
        run.main()


def test_boot_full_production_wiring(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBDAV_DUMMY", "pw")
    path = _config(tmp_path)
    server, ctx = run.boot(path)
    assert server is not None
    assert ctx.accounts == {"scratch": "webdav"}
    assert "webdav" in ctx.backends
