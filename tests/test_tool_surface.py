"""Tool-surface battery (the supervisory-review coverage finding): every
curated agent-surface tool through the booted production context, over
a fake backend (the gate's envelope contract per tool).

The fake backend records calls and returns NodeInfo listings; the gate
arms (free lane, pending, grant, baseline) are covered by the boot-path
battery. This battery reaches the tool HANDLERS: list, move, trash,
mkdir, request_access, check_access, revoke_access, read.
"""

# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest
from access_broker_core.baselines import BaselineEngine
from access_broker_core.grants import GrantStore

from data_broker.backends.webdav import NodeInfo
from data_broker.tools import BrokerContext, build_agent_tools, register_tools


class FakeWebDAVBackend:
    """Records verb calls; returns canned listings (no network)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.list_calls: list[tuple] = []
        self.listing = [
            NodeInfo(name="notes.txt", is_dir=False, size=7, modified=None),
            NodeInfo(name="Sub", is_dir=True, size=None, modified=None),
        ]

    async def list(self, account: str, resource: str) -> list[NodeInfo]:
        self.list_calls.append((account, resource))
        return self.listing

    async def read(self, account: str, resource: str) -> bytes:
        self.list_calls.append(("read", account, resource))
        return b"content\n"

    async def write(self, account: str, resource: str, content: bytes) -> None:
        self.list_calls.append(("write", account, resource, content))

    async def move(self, account: str, src: str, dst: str) -> None:
        self.list_calls.append(("move", account, src, dst))

    async def trash(self, account: str, resource: str) -> None:
        self.list_calls.append(("trash", account, resource))

    async def mkdir(self, account: str, resource: str) -> None:
        self.list_calls.append(("mkdir", account, resource))

    async def close(self) -> None:
        self.list_calls.append(("close",))


class FakeGraphDriveBackend(FakeWebDAVBackend):
    """Same shape for the onedrive family."""


@pytest.fixture()
def ctx(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> BrokerContext:
    """A production-wired BrokerContext with fake backends (no network):
    the real grant store, baseline engine, audit log, wall registry."""
    from datetime import UTC, datetime

    from access_broker_core.audit import AuditLog
    from data_broker import policy

    clock = lambda: datetime.now(UTC)  # noqa: E731 - battery clock
    registry = policy.build_registry()
    store = GrantStore(str(tmp_path / "g.sqlite3"), clock=clock, registry=registry)
    baselines = BaselineEngine(store, clock=clock)
    audit = AuditLog(tmp_path / "a.jsonl", clock=clock)
    backend = FakeWebDAVBackend()
    return BrokerContext(
        store=store,
        baselines=baselines,
        audit=audit,
        backends={"webdav": backend, "onedrive": backend},
        accounts={"scratch": "webdav", "work": "onedrive"},
        registry=registry,
    )


def _specs(ctx: BrokerContext) -> dict[str, Any]:
    return {spec["name"]: spec for spec in build_agent_tools(ctx)}


def test_list_tool_envelope(ctx: BrokerContext) -> None:
    specs = _specs(ctx)
    result = asyncio.run(specs["list"]["handler"]("scratch", "Docs"))
    assert result["status"] == "ok"
    assert result["entries"][0] == {"name": "notes.txt", "type": "file", "size": 7}
    assert result["entries"][1]["type"] == "dir"


def test_read_tool_metadata_shaped(ctx: BrokerContext) -> None:
    """The agent-surface read returns LISTING shapes only: bulk content
    stays on the transfer surface (D5)."""
    specs = _specs(ctx)
    result = asyncio.run(specs["read"]["handler"]("scratch", "Docs/notes.txt"))
    assert result["status"] == "ok"
    assert "content" not in result


def test_gated_tools_pend_without_grant(ctx: BrokerContext) -> None:
    specs = _specs(ctx)
    for name, args in (
        ("move", ("scratch", "Docs/a.txt", "Docs/b.txt")),
        ("trash", ("scratch", "Docs/a.txt")),
        ("mkdir", ("scratch", "Docs/Sub")),
    ):
        result = asyncio.run(specs[name]["handler"](*args))
        assert result["status"] == "pending"
        assert result["request_number"] > 0


def test_gated_tools_execute_with_grant(ctx: BrokerContext) -> None:
    number = ctx.store.submit(
        "hint",
        [{"backend": "webdav", "account": "scratch", "resource": "Docs", "ops": ["move", "trash", "mkdir"]}],
        "approved",
    )
    ctx.store.approve(number)
    specs = _specs(ctx)
    for name, args in (
        ("move", ("scratch", "Docs/a.txt", "Docs/b.txt")),
        ("trash", ("scratch", "Docs/a.txt")),
        ("mkdir", ("scratch", "Docs/New")),
    ):
        result = asyncio.run(specs[name]["handler"](*args))
        assert result["status"] == "ok"


def test_request_access_submits_and_pends(ctx: BrokerContext) -> None:
    specs = _specs(ctx)
    result = asyncio.run(specs["request_access"]["handler"]("scratch", "Docs", "read,write", "j"))
    assert result["status"] == "pending"
    number = result["request_number"]
    record = ctx.store.get_record(number)
    assert record.state == "pending"


def test_request_access_invalid_op_refused(ctx: BrokerContext) -> None:
    specs = _specs(ctx)
    result = asyncio.run(specs["request_access"]["handler"]("scratch", "Docs", "undeclared", "j"))
    assert result["status"] == "refused"


def test_check_access_envelope(ctx: BrokerContext) -> None:
    specs = _specs(ctx)
    result = asyncio.run(specs["check_access"]["handler"]("scratch"))
    assert result["status"] == "ok"
    assert result["pending"] == []
    assert result["active"] == []
    assert "budget" in result


def test_check_access_scoped(ctx: BrokerContext) -> None:
    specs = _specs(ctx)
    number = ctx.store.submit(
        "hint",
        [{"backend": "webdav", "account": "scratch", "resource": "Docs", "ops": ["read"]}],
        "j",
    )
    ctx.store.approve(number)
    result = asyncio.run(specs["check_access"]["handler"]("scratch", "Docs", "read"))
    assert result["status"] == "ok"
    assert result["active"][0]["request_number"] == number


def test_check_access_unknown_account(ctx: BrokerContext) -> None:
    specs = _specs(ctx)
    result = asyncio.run(specs["check_access"]["handler"]("nope", "Docs", "read"))
    assert result["status"] == "refused"


def test_revoke_access_roundtrip(ctx: BrokerContext) -> None:
    number = ctx.store.submit(
        "hint",
        [{"backend": "webdav", "account": "scratch", "resource": "Docs", "ops": ["read"]}],
        "j",
    )
    ctx.store.approve(number)
    specs = _specs(ctx)
    result = asyncio.run(specs["revoke_access"]["handler"]("scratch", number))
    assert result["status"] == "ok"
    assert result["revoked"] is True


def test_revoke_access_unknown_number(ctx: BrokerContext) -> None:
    specs = _specs(ctx)
    result = asyncio.run(specs["revoke_access"]["handler"]("scratch", 999))
    assert result["status"] == "refused"


def test_revoke_access_account_mismatch(ctx: BrokerContext) -> None:
    number = ctx.store.submit(
        "hint",
        [{"backend": "webdav", "account": "scratch", "resource": "Docs", "ops": ["read"]}],
        "j",
    )
    ctx.store.approve(number)
    specs = _specs(ctx)
    result = asyncio.run(specs["revoke_access"]["handler"]("work", number))
    assert result["status"] == "refused"


def test_register_tools_type_guard() -> None:
    with pytest.raises(TypeError):
        register_tools(None, "not-a-context")  # type: ignore[arg-type]


def test_gated_backend_error_envelope(ctx: BrokerContext) -> None:
    """A backend failure inside a granted op returns the error envelope
    (the gate never leaks tracebacks)."""
    number = ctx.store.submit(
        "hint",
        [{"backend": "webdav", "account": "scratch", "resource": "Docs", "ops": ["read"]}],
        "j",
    )
    ctx.store.approve(number)

    async def _boom() -> dict:
        raise RuntimeError("backend exploded")

    result = asyncio.run(ctx.execute("scratch", "Docs", "read", _boom))
    assert result["status"] == "error"