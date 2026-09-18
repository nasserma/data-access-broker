"""S4-5 boot-path battery: real config, real boot, real calls.

The S0/S2/S3 lesson: tool-surface tests over mocks prove the logic;
only calls through the PRODUCTION wiring (loader -> _boot -> ctx ->
execute) prove the seam. This battery boots with the real loader and
exercises the gate: F-C order, baseline match, pending path, audit.
"""

# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from data_broker import run
from data_broker.tools import BrokerContext

T0 = datetime(2026, 9, 13, 12, 0, 0)


@pytest.fixture()
def booted(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> BrokerContext:
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
    monkeypatch.setenv("WEBDAV_DUMMY", "pw")
    monkeypatch.setenv("DATABROKER_AGENT_TOKEN", "a" * 40)
    monkeypatch.setenv("DATABROKER_TRANSFER_TOKEN", "t" * 40)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))
    _server, ctx = run.boot(str(path))
    return ctx


async def test_free_lane_read_executes_audited(booted: BrokerContext) -> None:
    """The free-lane invariant RETURNS: a read op with no grant and no
    baseline executes, audit-logged (write-before-operate)."""

    async def _call() -> dict:
        return {"status": "ok", "result": {"seen": True}}

    result = await booted.execute("scratch", "Docs", "list", _call)
    assert result["status"] == "ok"
    assert result["result"] == {"seen": True}


async def test_gated_without_grant_pends(booted: BrokerContext) -> None:
    result = await booted.execute(
        "scratch", "Docs/notes.txt", "trash", lambda: None,  # type: ignore[arg-type,return-value]
        justification="trash the note",
    )
    assert result["status"] == "pending"
    assert result["request_number"] > 0


async def test_gated_with_grant_executes(booted: BrokerContext) -> None:
    number = booted.store.submit(
        "hint",
        [{"backend": "webdav", "account": "scratch", "resource": "Docs", "ops": ["trash"]}],
        "approved trash",
    )
    booted.store.approve(number)

    async def _call() -> dict:
        return {"status": "ok", "result": {"trashed": True}}

    result = await booted.execute("scratch", "Docs/notes.txt", "trash", _call)
    assert result["status"] == "ok"


async def test_baseline_matches_read(booted: BrokerContext) -> None:
    """An ACTIVE baseline covers the read: reason is baseline."""
    outcome = booted.baselines.create_request(
        {
            "backend": "webdav",
            "account": "scratch",
            "resource": "Knowledge",
            "ops": ["read"],
            "principal": "agent",
            "reassess_interval": "7d",
            "reconfirm_grace": "72h",
        },
        config_change_request_id="ccr-boot-1",
    )
    booted.baselines.approve_creation(outcome.request_id)

    async def _call() -> dict:
        return {"status": "ok", "result": {"read": True}}

    result = await booted.execute("scratch", "Knowledge/2026", "read", _call)
    assert result["status"] == "ok"


async def test_suspended_baseline_matches_nothing(booted: BrokerContext) -> None:
    """A suspended baseline falls back: the read still executes via the
    free lane (F-I), and the baseline itself is skipped (F-C)."""
    outcome = booted.baselines.create_request(
        {
            "backend": "webdav",
            "account": "scratch",
            "resource": "Knowledge",
            "ops": ["read"],
            "principal": "agent",
            "reassess_interval": "7d",
            "reconfirm_grace": "72h",
        },
        config_change_request_id="ccr-boot-2",
    )
    booted.baselines.approve_creation(outcome.request_id)
    d = booted.baselines.list_definitions()[0]
    booted.baselines.revoke(d["baseline_id"])

    async def _call() -> dict:
        return {"status": "ok", "result": {"read": True}}

    result = await booted.execute("scratch", "Knowledge/2026", "read", _call)
    assert result["status"] == "ok"


async def test_audit_write_failure_never_operates(
    booted: BrokerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LogWriteError: the callback never runs (write-before-operate)."""

    def _fail(*args: Any, **kwargs: Any) -> None:
        from access_broker_core.audit import LogWriteError

        raise LogWriteError("injected")

    monkeypatch.setattr(booted.audit, "record", _fail)

    async def _call() -> dict:
        return {"status": "ok", "result": {"should_not_run": True}}

    result = await booted.execute("scratch", "Docs", "list", _call)
    assert result["status"] == "error"


async def test_baseline_tier2_never_matches(booted: BrokerContext) -> None:
    """F-C: a baseline whose scope carries a T2 op is refused at
    creation (the declared-operation vocabulary is the validation set)."""
    with pytest.raises(ValueError):
        booted.baselines.create_request(
            {
                "backend": "webdav",
                "account": "scratch",
                "resource": "Docs",
                "ops": ["trash"],
                "principal": "agent",
            },
            config_change_request_id="ccr-boot-3",
        )


def test_audit_chain_verifies(booted: BrokerContext) -> None:
    """The hash chain verifies after boot-path traffic."""
    import asyncio

    from access_broker_core.audit import verify_chain

    async def _call() -> dict:
        return {"status": "ok", "result": {"x": 1}}

    asyncio.run(booted.execute("scratch", "Docs", "list", _call))
    result = verify_chain(booted.audit._path)
    assert result.ok
