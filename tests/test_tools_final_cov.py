"""tools.py final coverage arms: JSON-safety branches, unknown-account
refusals, ValueError propagations, audit-failure logging paths, gateway
notify paths, and check_access entry arms. All deterministic.
"""

# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
from typing import Any

import pytest
from access_broker_core.baselines import BaselineEngine
from access_broker_core.grants import GrantStore

from data_broker import run as run_mod
from data_broker.tools import BrokerContext, _to_json

# --------------------------------------------------------------- ctx fixture


class FakeWebDAVBackend:
    """Records verb calls; returns canned listings (no network)."""

    def __init__(self) -> None:
        from data_broker.backends.webdav import NodeInfo

        self.list_calls: list = []
        self.listing = [
            NodeInfo(name="notes.txt", is_dir=False, size=7, modified=None),
            NodeInfo(name="Sub", is_dir=True, size=None, modified=None),
        ]

    async def list(self, account: str, resource: str) -> list:
        self.list_calls.append((account, resource))
        return self.listing

    async def trash(self, account: str, resource: str) -> None:
        self.list_calls.append((account, resource))

    async def move(self, account: str, src: str, dst: str) -> None:
        self.list_calls.append((account, src, dst))

    async def mkdir(self, account: str, resource: str) -> None:
        self.list_calls.append((account, resource))

    async def close(self) -> None:
        self.list_calls.append(("closed",))


@pytest.fixture()
def ctx(tmp_path: Any) -> Any:
    """A production-wired BrokerContext with a fake backend (the same
    production wiring as the tool-surface battery)."""
    from datetime import UTC, datetime

    from access_broker_core.audit import AuditLog

    from data_broker import policy

    clock = lambda: datetime.now(UTC)  # noqa: E731 - battery clock
    registry = policy.build_registry()
    store = GrantStore(str(tmp_path / "g.sqlite3"), clock=clock, registry=registry)
    baselines = BaselineEngine(store, clock=clock)
    audit = AuditLog(tmp_path / "a.jsonl", clock=clock)
    return BrokerContext(
        store=store,
        baselines=baselines,
        audit=audit,
        backends={"webdav": FakeWebDAVBackend(), "onedrive": FakeWebDAVBackend()},
        accounts={"scratch": "webdav", "work": "onedrive"},
        registry=registry,
        custody=run_mod.build_custody_registry(),
    )


# ----------------------------------------------------------------- _to_json


def test_to_json_scalars() -> None:
    assert _to_json("s") == "s"
    assert _to_json(1) == 1
    assert _to_json(1.5) == 1.5
    assert _to_json(True) is True
    assert _to_json(None) is None


def test_to_json_containers() -> None:
    assert _to_json({"a": 1}) == {"a": 1}
    assert _to_json([1, "x"]) == [1, "x"]
    assert _to_json((1, 2)) == [1, 2]


def test_to_json_object_falls_to_repr() -> None:
    sentinel = object()
    out = _to_json(sentinel)
    assert isinstance(out, str) and out  # repr string


# ------------------------------------------------- unknown account refusal


def test_execute_unknown_account(ctx: Any) -> None:
    async def _call() -> dict:
        return {"status": "ok"}

    result = asyncio.run(ctx.execute("nope", "Docs", "list", _call))
    assert result["status"] == "refused"


def test_backend_for_unknown_account(ctx: Any) -> None:
    with pytest.raises(ValueError):
        ctx.backend_for("nope")


# ----------------------------------------------------- justification defaults


def test_pending_default_justification(ctx: Any) -> None:
    """Gated without grant and no justification: the gate composes one."""
    result = asyncio.run(ctx.execute("scratch", "Docs/n.txt", "trash", lambda: None))
    assert result["status"] == "pending"
    number = result["request_number"]
    record = ctx.store.get_record(number)
    assert record.justification == "trash on Docs/n.txt (account scratch)"


def test_audit_failure_on_pending_is_logged_not_fatal(
    ctx: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An audit failure on the pending path logs a warning, still pends."""
    from access_broker_core.audit import LogWriteError

    def _fail(*a: Any, **k: Any) -> None:
        raise LogWriteError("injected")

    monkeypatch.setattr(ctx.audit, "record", _fail)
    result = asyncio.run(ctx.execute("scratch", "Docs", "trash", lambda: None))
    assert result["status"] == "pending"


def test_gateway_notify_failure_never_blocks(
    ctx: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing gateway notification logs and still returns pending."""

    class _BoomCore:
        async def notify_request(self, *a: Any, **k: Any) -> None:
            raise RuntimeError("notify boom")

    ctx.core = _BoomCore()
    result = asyncio.run(ctx.execute("scratch", "Docs/n.txt", "trash", lambda: None))
    assert result["status"] == "pending"
    ctx.core = None


def test_request_access_with_gateway_notify(
    ctx: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """request_access posts through the gateway (notify path)."""

    class _RecordingCore:
        notified: list = []

        async def notify_request(self, number: int, text: str, items: list) -> None:
            _RecordingCore.notified.append(number)

    ctx.core = _RecordingCore()
    specs = {s["name"]: s["handler"] for s in __import__("data_broker.tools", fromlist=["build_agent_tools"]).build_agent_tools(ctx)}
    result = asyncio.run(specs["request_access"]("scratch", "Docs", "write", "j"))
    assert result["status"] == "pending"
    assert _RecordingCore.notified == [result["request_number"]]


def test_request_access_audit_failure_logged(
    ctx: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An audit failure on request_access logs a warning, still pends."""
    from access_broker_core.audit import LogWriteError

    def _fail(*a: Any, **k: Any) -> None:
        raise LogWriteError("injected")

    monkeypatch.setattr(ctx.audit, "record", _fail)
    specs = {s["name"]: s["handler"] for s in __import__("data_broker.tools", fromlist=["build_agent_tools"]).build_agent_tools(ctx)}
    result = asyncio.run(specs["request_access"]("scratch", "Docs", "write", "j"))
    assert result["status"] == "pending"


def test_request_access_notify_failure_logged(
    ctx: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _BoomCore:
        async def notify_request(self, *a: Any, **k: Any) -> None:
            raise RuntimeError("boom")

    ctx.core = _BoomCore()
    specs = {s["name"]: s["handler"] for s in __import__("data_broker.tools", fromlist=["build_agent_tools"]).build_agent_tools(ctx)}
    result = asyncio.run(specs["request_access"]("scratch", "Docs", "write", "j"))
    assert result["status"] == "pending"


def test_check_access_lists_pending_and_active(ctx: Any) -> None:
    specs = {s["name"]: s["handler"] for s in __import__("data_broker.tools", fromlist=["build_agent_tools"]).build_agent_tools(ctx)}
    number = ctx.store.submit(
        "hint",
        [{"backend": "webdav", "account": "scratch", "resource": "Docs", "ops": ["read"]}],
        "j",
    )
    ctx.store.approve(number)
    ctx.store.submit(
        "hint2",
        [{"backend": "webdav", "account": "scratch", "resource": "X", "ops": ["write"]}],
        "j2",
    )
    result = asyncio.run(specs["check_access"]("scratch"))
    assert result["status"] == "ok"
    assert result["active"] and result["pending"]
    assert any(e["request_number"] == number for e in result["active"])


def test_revoke_audit_failure_still_ok(ctx: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """An audit failure on revoke logs a warning; the envelope stays ok."""
    from access_broker_core.audit import LogWriteError

    def _fail(*a: Any, **k: Any) -> None:
        raise LogWriteError("injected")

    monkeypatch.setattr(ctx.audit, "record", _fail)
    number = ctx.store.submit(
        "hint",
        [{"backend": "webdav", "account": "scratch", "resource": "Docs", "ops": ["read"]}],
        "j",
    )
    ctx.store.approve(number)
    specs = {s["name"]: s["handler"] for s in __import__("data_broker.tools", fromlist=["build_agent_tools"]).build_agent_tools(ctx)}
    result = asyncio.run(specs["revoke_access"]("scratch", number))
    assert result["status"] == "ok"
    assert result["revoked"] is True
