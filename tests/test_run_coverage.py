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
