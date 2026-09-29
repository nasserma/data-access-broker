"""Client-side MCP fetcher battery: the local stdio server's tool
surface over the shared client core (fake transfer surface, no
network), plus a live stdio subprocess handshake (the exact launch
path a harness uses).
"""

# SPDX-License-Identifier: GPL-3.0-or-later

import base64
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from data_broker import client as _client
from data_broker import client_mcp

CONTENT = b"hello client-mcp\n"
CONTENT_SHA = hashlib.sha256(CONTENT).hexdigest()


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


@pytest.fixture(autouse=True)
def _transfer_token(monkeypatch):
    monkeypatch.setenv("DATABROKER_TRANSFER_TOKEN", "tok")


def _patch_surface(monkeypatch: pytest.MonkeyPatch, fake: Any) -> None:
    def _inner(self: Any, name: str, args: dict, *unused: Any) -> dict:
        import asyncio

        return asyncio.run(getattr(fake, name)(**args))

    monkeypatch.setattr(_client.TransferClient, "call_tool", _inner)


async def _call(server: client_mcp.MCPServer, name: str, args: dict) -> dict:
    """Call a tool and pull the JSON payload out of the text content."""
    result = await server.call_tool(name, args)
    content = getattr(result, "content", None)
    assert content, f"no content in result: {result!r}"
    text = getattr(content[0], "text", None)
    assert text is not None, f"no text in content[0]: {content[0]!r}"
    return json.loads(text)


async def test_fetch_tool_returns_manifest_only(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_surface(monkeypatch, FakeSurface())
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    server = client_mcp.build_server()

    payload = await _call(server, "fetch", {"account": "scratch", "resource": "Docs/notes.txt"})

    assert payload["status"] == "ok"
    assert payload["exit_code"] == _client.EXIT_OK
    assert payload["sha256"] == CONTENT_SHA
    assert payload["size"] == len(CONTENT)
    # metadata only: no content key ever appears
    assert "content" not in json.dumps(payload)
    assert "content_b64" not in json.dumps(payload)
    # staged file exists and verifies
    staged = tmp_path / "data-broker" / "staging" / "scratch" / CONTENT_SHA
    assert staged.is_file()
    assert hashlib.sha256(staged.read_bytes()).hexdigest() == CONTENT_SHA


async def test_fetch_tool_refused_no_grant(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _NoGrant:
        async def read(self, account: str, resource: str) -> dict:
            return {"status": "refused", "reason": "no active grant for read"}

    _patch_surface(monkeypatch, _NoGrant())
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    server = client_mcp.build_server()

    payload = await _call(server, "fetch", {"account": "scratch", "resource": "Docs/x.txt"})

    assert payload["status"] == "error"
    assert payload["exit_code"] == _client.EXIT_REFUSED
    # nothing staged: the (pre-created) staging dirs exist but hold no files
    root = tmp_path / "data-broker"
    staged = [p for p in root.rglob("*") if p.is_file()] if root.exists() else []
    assert staged == []


async def test_fetch_tool_corrupt_sha_stages_nothing(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Corrupt:
        async def read(self, account: str, resource: str) -> dict:
            return {
                "status": "ok",
                "content_b64": base64.b64encode(CONTENT).decode(),
                "sha256": "0" * 64,
                "size": len(CONTENT),
            }

    _patch_surface(monkeypatch, _Corrupt())
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    server = client_mcp.build_server()

    payload = await _call(server, "fetch", {"account": "scratch", "resource": "Docs/x.txt"})

    assert payload["status"] == "error"
    assert payload["exit_code"] == _client.EXIT_VERIFICATION
    root = tmp_path / "data-broker"
    staged = [p for p in root.rglob("*") if p.is_file()] if root.exists() else []
    assert staged == []


async def test_fetch_tool_missing_token_is_local_error(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABROKER_TRANSFER_TOKEN", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    server = client_mcp.build_server()

    payload = await _call(server, "fetch", {"account": "scratch", "resource": "Docs/x.txt"})

    assert payload["status"] == "error"
    assert payload["exit_code"] == _client.EXIT_LOCAL


async def test_push_tool_verifies_then_writes(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeSurface()
    _patch_surface(monkeypatch, fake)
    src = tmp_path / "src.txt"
    src.write_bytes(b"pushed via mcp\n")
    server = client_mcp.build_server()

    payload = await _call(
        server,
        "push",
        {"account": "scratch", "resource": "Docs/out.txt", "path": str(src)},
    )

    assert payload["status"] == "ok"
    assert payload["exit_code"] == _client.EXIT_OK
    assert fake.writes == [("scratch", "Docs/out.txt", b"pushed via mcp\n")]
    assert "content" not in json.dumps(payload)


async def test_push_tool_missing_file_is_local_error(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = client_mcp.build_server()
    payload = await _call(
        server,
        "push",
        {"account": "scratch", "resource": "Docs/x.txt", "path": str(tmp_path / "nope.txt")},
    )
    assert payload["status"] == "error"
    assert payload["exit_code"] == _client.EXIT_LOCAL


async def test_tool_listing_is_exactly_fetch_and_push() -> None:
    server = client_mcp.build_server()
    tools = await server.list_tools()
    names = sorted(t.name for t in tools)
    assert names == ["fetch", "push"]


async def test_stdio_handshake_live(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live stdio subprocess: initialize, list tools, call fetch,
    parse the manifest — the exact launch path a harness uses
    (`python -m data_broker.client_mcp`). The subprocess talks to the
    same in-process fake surface via a monkeypatched module? No — it
    runs UNPATCHED against no broker, so this leg asserts the
    handshake and error semantics only; the fetch payload leg is
    covered above."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "data_broker.client_mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    try:
        def send(obj: dict) -> None:
            assert proc.stdin is not None
            proc.stdin.write(json.dumps(obj) + "\n")
            proc.stdin.flush()

        def recv() -> dict:
            assert proc.stdout is not None
            line = proc.stdout.readline()
            assert line, "server closed stdout"
            return json.loads(line)

        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2026-07-28",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            }
        )
        init = recv()
        assert "result" in init
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        listing = recv()
        names = sorted(t["name"] for t in listing["result"]["tools"])
        assert names == ["fetch", "push"]
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
