"""S4-5 transfer surface battery (the supervisory-review finding 3): the
second MCP surface behind its own token.

Contract (goal contract S4-5; the D5 pattern ported from
nextcloud-access-broker broker/cli.py, GPL-3.0-or-later):

- the transfer surface serves check_access, read, write behind the
  transfer token; bulk content NEVER enters LLM context;
- read returns content_b64 + sha256; the CLI verifies both;
- write takes content_b64 + expected_sha256; the server writes only
  after sha verification passes (verify-then-write);
- content-addressed staging on the client side; the server verifies
  and audits write-before-operate;
- the wall applies in full on the transfer surface: a read/write
  without a grant is REFUSED (envelope refused, never silent).

Server wiring: build_transfer_server() constructs a second MCPServer
registered with the transfer tool set; run.boot wires both surfaces.
"""

# SPDX-License-Identifier: GPL-3.0-or-later

import base64
import hashlib
from typing import Any

import pytest

from data_broker.backends.webdav import NodeInfo
from data_broker.tools import BrokerContext, build_transfer_tools


class FakeWebDAVBackend:
    """Records verb calls; returns canned content (no network)."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.files: dict[str, bytes] = {"Docs/notes.txt": b"hello transfer\n"}
        self.listing = [
            NodeInfo(name="notes.txt", is_dir=False, size=15, modified=None),
            NodeInfo(name="Sub", is_dir=True, size=None, modified=None),
        ]

    async def list(self, account: str, resource: str) -> list[NodeInfo]:
        self.calls.append(("list", account, resource))
        return self.listing

    async def read(self, account: str, resource: str) -> bytes:
        self.calls.append(("read", account, resource))
        if resource in self.files:
            return self.files[resource]
        raise FileNotFoundError(resource)

    async def write(self, account: str, resource: str, content: bytes) -> None:
        self.calls.append(("write", account, resource, content))
        self.files[resource] = content

    async def move(self, account: str, src: str, dst: str) -> None:
        self.calls.append(("move", account, src, dst))

    async def trash(self, account: str, resource: str) -> None:
        self.calls.append(("trash", account, resource))

    async def mkdir(self, account: str, resource: str) -> None:
        self.calls.append(("mkdir", account, resource))

    async def close(self) -> None:
        self.calls.append(("closed",))


@pytest.fixture()
def ctx(tmp_path: Any) -> Any:
    from datetime import UTC, datetime

    from access_broker_core.audit import AuditLog
    from access_broker_core.baselines import BaselineEngine
    from access_broker_core.grants import GrantStore

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
        accounts={"scratch": "webdav"},
        registry=registry,
    )


def _specs(ctx: BrokerContext) -> dict[str, Any]:
    return {s["name"]: s["handler"] for s in build_transfer_tools(ctx)}


_CONTENT = b"hello transfer\n"
_CONTENT_SHA = hashlib.sha256(_CONTENT).hexdigest()


def test_transfer_tools_set(ctx: BrokerContext) -> None:
    names = {s["name"] for s in build_transfer_tools(ctx)}
    assert names == {"check_access", "read", "write"}


def test_transfer_read_returns_b64_and_sha(ctx: BrokerContext) -> None:
    """A granted read returns content_b64 + sha256: the CLI verifies
    both and stages locally; content never enters LLM context."""
    number = ctx.store.submit(
        "hint",
        [{"backend": "webdav", "account": "scratch", "resource": "Docs", "ops": ["read"]}],
        "j",
    )
    ctx.store.approve(number)
    specs = _specs(ctx)
    result = asyncio_run(specs["read"]("scratch", "Docs/notes.txt"))
    assert result["status"] == "ok"
    decoded = base64.b64decode(result["content_b64"], validate=True)
    assert decoded == _CONTENT
    assert result["sha256"] == _CONTENT_SHA
    assert result["size"] == len(_CONTENT)


def test_transfer_read_without_grant_refused(ctx: BrokerContext) -> None:
    """No grant: REFUSED (the D5 wall applies on the transfer surface)."""
    specs = _specs(ctx)
    result = asyncio_run(specs["read"]("scratch", "Docs/notes.txt"))
    assert result["status"] == "refused"


def test_transfer_write_verify_then_write(ctx: BrokerContext) -> None:
    """A granted write with a matching expected sha writes; the response
    echoes the verified sha (verification before the backend call)."""
    number = ctx.store.submit(
        "hint",
        [{"backend": "webdav", "account": "scratch", "resource": "Docs", "ops": ["write"]}],
        "j",
    )
    ctx.store.approve(number)
    specs = _specs(ctx)
    payload = b"new content\n"
    result = asyncio_run(
        specs["write"](
            "scratch",
            "Docs/new.txt",
            base64.b64encode(payload).decode(),
            hashlib.sha256(payload).hexdigest(),
        )
    )
    assert result["status"] == "ok"
    assert result["sha256"] == hashlib.sha256(payload).hexdigest()


def test_transfer_write_sha_mismatch_refused(ctx: BrokerContext) -> None:
    """A mismatched expected sha refuses BEFORE the backend write: the
    D5 verification contract (treat as corrupt, never stage)."""
    number = ctx.store.submit(
        "hint",
        [{"backend": "webdav", "account": "scratch", "resource": "Docs", "ops": ["write"]}],
        "j",
    )
    ctx.store.approve(number)
    specs = _specs(ctx)
    payload = b"new content\n"
    bad_sha = hashlib.sha256(b"different").hexdigest()
    result = asyncio_run(
        specs["write"](
            "scratch",
            "Docs/new.txt",
            base64.b64encode(payload).decode(),
            bad_sha,
        )
    )
    assert result["status"] == "refused"
    # the backend never received the write
    backend = ctx.backends["webdav"]
    assert not any(c[0] == "write" for c in backend.calls)


def test_transfer_write_without_grant_refused(ctx: BrokerContext) -> None:
    specs = _specs(ctx)
    payload = b"new content\n"
    result = asyncio_run(
        specs["write"](
            "scratch",
            "Docs/new.txt",
            base64.b64encode(payload).decode(),
            hashlib.sha256(payload).hexdigest(),
        )
    )
    assert result["status"] == "refused"


def test_transfer_check_access_envelope(ctx: BrokerContext) -> None:
    specs = _specs(ctx)
    result = asyncio_run(specs["check_access"]("scratch", "Docs", "read"))
    assert result["status"] == "ok"


def test_transfer_invalid_base64_refused(ctx: BrokerContext) -> None:
    """A write whose content_b64 is not valid base64 refuses before any
    sha check (strict decode, the CLI contract)."""
    number = ctx.store.submit(
        "hint",
        [{"backend": "webdav", "account": "scratch", "resource": "Docs", "ops": ["write"]}],
        "j",
    )
    ctx.store.approve(number)
    specs = _specs(ctx)
    result = asyncio_run(specs["write"]("scratch", "Docs/x.txt", "not!base64!", "0" * 64))
    assert result["status"] == "refused"


def test_transfer_read_unknown_resource_errors_envelope(ctx: BrokerContext) -> None:
    """A granted read of a missing file maps to the error envelope (the
    gate never leaks tracebacks)."""
    number = ctx.store.submit(
        "hint",
        [{"backend": "webdav", "account": "scratch", "resource": "Docs", "ops": ["read"]}],
        "j",
    )
    ctx.store.approve(number)
    specs = _specs(ctx)
    result = asyncio_run(specs["read"]("scratch", "Docs/missing.txt"))
    assert result["status"] in ("error", "refused")


def test_transfer_audit_write_before_operate(ctx: BrokerContext) -> None:
    """Every transfer-surface execution is audited write-before-operate:
    the audit chain verifies after traffic."""
    from access_broker_core.audit import verify_chain

    number = ctx.store.submit(
        "hint",
        [{"backend": "webdav", "account": "scratch", "resource": "Docs", "ops": ["read"]}],
        "j",
    )
    ctx.store.approve(number)
    specs = _specs(ctx)
    result = asyncio_run(specs["read"]("scratch", "Docs/notes.txt"))
    assert result["status"] == "ok"
    chain = verify_chain(ctx.audit._path)
    assert chain.ok


def asyncio_run(coro: Any) -> Any:
    import asyncio

    return asyncio.run(coro)
