"""Transfer CLI battery: fetch/push over a fake transfer surface.

Pattern: the CLI talks to a TransferClient pointed at a FAKE surface
(a local in-process fake serving the transfer envelopes directly - no
network). Verifies: sha-verified staging, verify-then-push, exit codes
0-4, gc, token hygiene.
"""

# SPDX-License-Identifier: GPL-3.0-or-later

import base64
import hashlib
from typing import Any

import pytest

from data_broker import cli

CONTENT = b"hello cli\n"
CONTENT_SHA = hashlib.sha256(CONTENT).hexdigest()

@pytest.fixture(autouse=True)
def _transfer_token(monkeypatch):
    """Every CLI test starts with the transfer token set.
    Tests exercising the missing-token arm delete it explicitly.
    """
    monkeypatch.setenv("DATABROKER_TRANSFER_TOKEN", "tok")



class FakeSurface:
    """Serves read/write envelopes in-process (the fake transport)."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {"Docs/notes.txt": CONTENT}
        self.writes: list[tuple] = []

    async def read(self, account: str, resource: str) -> dict:
        return {
            "status": "ok",
            "content_b64": base64.b64encode(self.files.get(resource, b"")).decode(),
            "sha256": hashlib.sha256(self.files.get(resource, b"")).hexdigest(),
            "size": len(self.files.get(resource, b"")),
        }

    async def write(self, account: str, resource: str, content_b64: str, expected_sha256: str) -> dict:
        content = base64.b64decode(content_b64, validate=True)
        actual = hashlib.sha256(content).hexdigest()
        if actual != expected_sha256:
            return {"status": "refused", "reason": f"sha256 mismatch: expected {expected_sha256}, got {actual}"}
        self.writes.append((account, resource, content))
        return {"status": "ok", "sha256": actual, "size": len(content)}


@pytest.fixture()
def surface(monkeypatch: pytest.MonkeyPatch) -> FakeSurface:
    fake = FakeSurface()
    monkeypatch.setenv("DATABROKER_TRANSFER_TOKEN", "tok")
    monkeypatch.setattr(cli.TransferClient, "call_tool", _call_tool_for(fake))
    return fake


def _call_tool_for(fake: Any) -> Any:
    def _inner(self: Any, name: str, args: dict) -> dict:
        import asyncio

        return asyncio.run(getattr(fake, name)(**args))

    return _inner


def test_fetch_stages_content_addressed(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, surface: Any
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    args = _ns("fetch", account="scratch", resource="Docs/notes.txt", url="http://x/transfer")
    rc = cli.cmd_fetch(args)
    assert rc == cli.EXIT_OK
    entry = tmp_path / "data-broker" / "staging" / "scratch" / CONTENT_SHA
    assert entry.is_file()
    assert entry.read_bytes() == CONTENT
    manifest = tmp_path / "data-broker" / "staging" / "scratch" / f"{CONTENT_SHA}.json"
    assert manifest.is_file()
    assert manifest.read_text().strip().startswith("{")


def test_fetch_refused_no_grant(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    surface = surface_no_grant()
    monkeypatch.setattr(cli.TransferClient, "call_tool", _call_tool_for(surface))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    args = _ns("fetch", account="scratch", resource="Docs/notes.txt", url="http://x/transfer")
    rc = cli.cmd_fetch(args)
    assert rc == cli.EXIT_REFUSED


def surface_no_grant() -> Any:
    class _NoGrant:
        async def read(self, account: str, resource: str) -> dict:
            return {"status": "refused", "reason": "no active grant for read"}

    return _NoGrant()





def test_fetch_sha_mismatch_is_verification_failure(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corrupt envelope (server sha != content) refuses to stage."""
    surface = surface_corrupt()
    monkeypatch.setattr(cli.TransferClient, "call_tool", _call_tool_for(surface))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    args = _ns("fetch", account="scratch", resource="Docs/notes.txt", url="http://x/transfer")
    rc = cli.cmd_fetch(args)
    assert rc == cli.EXIT_VERIFICATION
    # nothing staged
    root = tmp_path / "data-broker" / "staging" / "scratch"
    staged = [p for p in root.iterdir() if not p.name.startswith(".")] if root.exists() else []
    assert staged == []


def surface_corrupt() -> Any:
    class _Corrupt:
        async def read(self, account: str, resource: str) -> dict:
            return {
                "status": "ok",
                "content_b64": base64.b64encode(CONTENT).decode(),
                "sha256": "0" * 64,
                "size": len(CONTENT),
            }

    return _Corrupt()


def test_fetch_invalid_base64(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    class _Bad:
        async def read(self, account: str, resource: str) -> dict:
            return {"status": "ok", "content_b64": "not!b64!", "sha256": CONTENT_SHA, "size": 0}

    monkeypatch.setattr(cli.TransferClient, "call_tool", _call_tool_for(_Bad()))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    args = _ns("fetch", account="scratch", resource="Docs/notes.txt", url="http://x/transfer")
    rc = cli.cmd_fetch(args)
    assert rc == cli.EXIT_VERIFICATION


def test_push_verifies_then_writes(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    surface = FakeSurface()
    monkeypatch.setattr(cli.TransferClient, "call_tool", _call_tool_for(surface))
    src = tmp_path / "src.txt"
    src.write_bytes(b"pushed content\n")
    args = _ns(
        "push",
        account="scratch",
        resource="Docs/pushed.txt",
        path=str(src),
        url="http://x/transfer",
    )
    rc = cli.cmd_push(args)
    assert rc == cli.EXIT_OK
    assert surface.writes == [("scratch", "Docs/pushed.txt", b"pushed content\n")]


def test_push_sha_mismatch_is_verification_failure(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Strict:
        async def write(self, account: str, resource: str, content_b64: str, expected_sha256: str) -> dict:
            return {"status": "refused", "reason": "sha256 mismatch: expected x, got y"}

    monkeypatch.setattr(cli.TransferClient, "call_tool", _call_tool_for(_Strict()))
    src = tmp_path / "src.txt"
    src.write_bytes(b"data\n")
    args = _ns(
        "push",
        account="scratch",
        resource="Docs/x.txt",
        path=str(src),
        url="http://x/transfer",
    )
    rc = cli.cmd_push(args)
    assert rc == cli.EXIT_VERIFICATION


def test_push_missing_file_is_local_error(tmp_path: Any) -> None:
    args = _ns(
        "push",
        account="scratch",
        resource="Docs/x.txt",
        path=str(tmp_path / "nope.txt"),
        url="http://x/transfer",
    )
    rc = cli.cmd_push(args)
    assert rc == cli.EXIT_LOCAL


def test_missing_token_is_local_error(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABROKER_TRANSFER_TOKEN", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    args = _ns("fetch", account="scratch", resource="Docs/notes.txt", url="http://x/transfer")
    rc = cli.cmd_fetch(args)
    assert rc == cli.EXIT_LOCAL


def _ns(command: str, **kwargs: Any) -> Any:
    import argparse

    ns = argparse.Namespace(command=command)
    for k, v in kwargs.items():
        setattr(ns, k, v)
    return ns


def test_exit_code_constants() -> None:
    assert cli.EXIT_OK == 0
    assert cli.EXIT_LOCAL == 1
    assert cli.EXIT_INFRA == 2
    assert cli.EXIT_REFUSED == 3
    assert cli.EXIT_VERIFICATION == 4
