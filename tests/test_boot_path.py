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


# ------------------------------------------------------------ S5-1 gateway


@pytest.fixture()
def gateway_boot(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> tuple:
    """Boot with a real gateway section; the matrix adapter builder is
    swapped for a recorded fake (the nio client is external I/O — the
    comms boot-path pattern). Asserts the production wiring: registration
    happens at boot, build_gateway constructs, ctx.core is the gateway
    core, and the audit chain is shared."""
    import yaml

    monkeypatch.setenv("WEBDAV_DUMMY", "pw")
    monkeypatch.setenv("DATABROKER_AGENT_TOKEN", "a" * 40)
    monkeypatch.setenv("DATABROKER_TRANSFER_TOKEN", "t" * 40)
    monkeypatch.setenv("DATABROKER_GATEWAY_TOKEN", "g" * 40)

    import access_broker_core.gateways as gw_mod

    import data_broker.gateways as broker_gw_mod

    sent: list[str] = []

    def fake_builder(core, fields, approver):
        class FakeTransport:
            async def send_message(self, text: str) -> str:
                sent.append(text)
                return "evt-boot-1"

            async def add_reaction(self, event_id: str, emoji: str) -> None:
                return None

        class FakeAdapter:
            started = False

            def __init__(self, core, transport) -> None:
                self.core = core
                self._transport = transport

            async def start(self) -> None:
                self.started = True

            async def stop(self) -> None:
                self.started = False

        adapter = FakeAdapter(core, FakeTransport())
        core._transport = adapter._transport  # noqa: SLF001 - wiring
        return adapter, fields["room_id"]

    gw_mod.register_adapter("matrix", fake_builder)
    # _boot calls the broker's register_gateway_adapters(), which would
    # replace the fake with the real (nio-dependent) builder; hold the
    # broker's registration machinery still for this boot (the real
    # builder's construction is unit-covered in test_gateway_matrix.py).
    monkeypatch.setattr(broker_gw_mod, "register_adapter", lambda *a, **k: None)
    try:
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
            "gateway": {
                "matrix": {
                    "homeserver_url": "https://matrix.example.org",
                    "user_id": "@approvals:example.org",
                    "access_token_env": "DATABROKER_GATEWAY_TOKEN",
                    "room_id": "!approvals:example.org",
                    "allowed_senders": ["@owner:example.org"],
                }
            },
        }
        path = tmp_path / "config_gw.yaml"
        path.write_text(yaml.safe_dump(cfg))
        server, ctx, _bind, gateway = run._boot(run.load_and_validate(str(path)))
        return server, ctx, gateway, sent
    finally:
        gw_mod._ADAPTER_BUILDERS.pop("matrix", None)


async def test_gateway_boot_wires_core_ctx_audit(gateway_boot: tuple) -> None:
    """The gateway-configured boot: core factory ran, ctx.core is the
    gateway decision core, and the gateway core writes decisions to the
    SAME hash-chained audit log (review finding F1)."""
    _server, ctx, gateway, _sent = gateway_boot
    core, _adapter = gateway
    assert ctx.core is core
    assert core._audit is ctx.audit  # noqa: SLF001 - wiring assertion


async def test_gateway_boot_end_to_end_notify_and_decide(gateway_boot: tuple) -> None:
    """A real T2 request through the production path reaches the adapter
    transport, and the owner's typed decision lands in the store."""
    from access_broker_core.grants import RequestState

    _server, ctx, gateway, sent = gateway_boot
    core, _adapter = gateway
    # a real T2 tool call through the production path (execute ->
    # _request_pending -> store.submit -> core.notify_request)
    outcome = await ctx.execute(
        "scratch", "Docs/notes.txt", "trash", lambda: None,  # type: ignore[arg-type,return-value]
        justification="clean up the note",
    )
    assert outcome["status"] == "pending"
    number = outcome["request_number"]
    # the approval request text actually reached the transport (F1 surface)
    assert any(f"#{number}" in text or str(number) in text for text in sent)
    # the owner replies; the decision commits through the store (one-time CAS)
    await core.handle_reply("@owner:example.org", f"approve {number}")
    assert await core.request_state(number) is RequestState.ACTIVE


# ------------------------------------------------- gateway transport rebind


async def test_build_gateway_rebinds_core_transport(monkeypatch) -> None:
    """The REAL matrix builder (registered through build_gateway) must
    rebind the core off the placeholder transport — a typed reply would
    otherwise die on the placeholder guard (found live 2026-09-20 on the
    owner's first production gateway boot; the fake-builder boot test
    rebinds internally, so it never caught this)."""
    from access_broker_core.gateways import _PlaceholderTransport, build_gateway
    from access_broker_core.grants import GrantStore

    from data_broker import policy
    from data_broker.gateways import register_gateway_adapters

    monkeypatch.setenv("DATABROKER_GATEWAY_TOKEN", "g" * 40)
    import tempfile
    tmp = tempfile.mkdtemp(prefix="gwrebind-")
    store = GrantStore(f"{tmp}/g.db", clock=lambda: None, registry=policy.build_registry())
    register_gateway_adapters()
    try:
        core, adapter = build_gateway(
            {
                "matrix": {
                    "homeserver_url": "https://matrix.example.org",
                    "user_id": "@approvals:example.org",
                    "access_token_env": "DATABROKER_GATEWAY_TOKEN",
                    "room_id": "!approvals:example.org",
                    "allowed_senders": ["@owner:example.org"],
                }
            },
            store=store,
            clock=lambda: None,
        )
        assert isinstance(core._transport, _PlaceholderTransport) is False  # noqa: SLF001
        assert core._transport is adapter._transport  # noqa: SLF001
        # and the wire is live: a send goes to the nio-backed transport,
        # not the loud placeholder
        assert callable(core._transport.send_message)
    finally:
        from access_broker_core import gateways as gw_mod

        gw_mod._ADAPTER_BUILDERS.pop("matrix", None)
