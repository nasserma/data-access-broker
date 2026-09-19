"""TransferClient coverage battery (the fresh-context review finding):
the CLI's HTTP/auth/SSE code is exercised for real — no call_tool
mocking — against a local HTTP server that records the Authorization
header and serves canned JSON-RPC envelopes.

Covers: session capture (Mcp-Session-Id echo), bearer header attach,
401 handling, non-OK HTTP, SSE parsing, malformed responses.
"""

# SPDX-License-Identifier: GPL-3.0-or-later

import base64
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from data_broker import cli


class _BrokerStub:
    """HTTP stub serving canned JSON-RPC responses; records auth headers."""

    def __init__(self) -> None:
        self.auth_headers: list[str] = []
        self.bodies: dict[str, Any] = {}
        self.session_id = "sess-1"

    def handler(self) -> type:
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                stub.auth_headers.append(self.headers.get("Authorization", ""))
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) if length else b"{}")
                method = body.get("method", "")
                name = body.get("params", {}).get("name", "")
                if method == "initialize":
                    # the session contract: initialize first, session id
                    # returned on that call (S5-4 CLI repair)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Mcp-Session-Id", stub.session_id)
                    self.end_headers()
                    self.wfile.write(json.dumps({
                        "jsonrpc": "2.0", "id": body.get("id"),
                        "result": {"protocolVersion": "2026-07-28",
                                   "capabilities": {},
                                   "serverInfo": {"name": "stub", "version": "0"}},
                    }).encode())
                elif name in stub.bodies:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps(stub.bodies[name]).encode())
                else:
                    self.send_response(404)
                    self.end_headers()
                    self.wfile.write(b"{}")

            def log_message(self, *args: Any) -> None:  # silence
                return None

        return Handler


def _serve(stub: _BrokerStub) -> tuple[HTTPServer, str]:
    server = HTTPServer(("127.0.0.1", 0), stub.handler())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


_CONTENT = b"hello cli coverage\n"
_CONTENT_SHA = hashlib.sha256(_CONTENT).hexdigest()


def test_transfer_client_bearer_and_read_envelope(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The real client hits a real HTTP server: the Authorization header
    carries the bearer token; the read envelope decodes; sha verifies."""
    stub = _BrokerStub()
    stub.bodies["read"] = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "content": [
                {
                    "text": json.dumps(
                        {
                            "status": "ok",
                            "content_b64": base64.b64encode(_CONTENT).decode(),
                            "sha256": _CONTENT_SHA,
                            "size": len(_CONTENT),
                        }
                    )
                }
            ]
        },
    }
    server, thread = _serve(stub)
    try:
        client = cli.TransferClient(f"http://127.0.0.1:{server.server_address[1]}/transfer", "tok-1")
        out = client.call_tool("read", {"account": "scratch", "resource": "Docs/notes.txt"})
        assert out["status"] == "ok"
        assert base64.b64decode(out["content_b64"]) == _CONTENT
        assert out["sha256"] == _CONTENT_SHA
        # the Authorization header was attached on every request
        assert stub.auth_headers and all("Bearer tok" in h for h in stub.auth_headers)
        # the session id was captured from the first response
        assert client._session_id == stub.session_id
    finally:
        server.shutdown()
        thread.join()


def test_transfer_client_401(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """401 -> CliError naming the token (the CLI's token-hygiene arm)."""
    stub = _BrokerStub()

    class _Denied(stub.handler()):  # type: ignore[misc,valid-type]
        def do_POST(self) -> None:
            self.send_response(401)
            self.end_headers()

    server = HTTPServer(("127.0.0.1", 0), _Denied)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = cli.TransferClient(f"http://127.0.0.1:{server.server_address[1]}/transfer", "bad")
        with pytest.raises(cli.CliError) as excinfo:
            client.call_tool("read", {"account": "a", "resource": "r"})
        assert "transfer token" in excinfo.value.message
    finally:
        server.shutdown()


def test_transfer_client_unreachable() -> None:
    client = cli.TransferClient("http://127.0.0.1:1/transfer", "tok")
    with pytest.raises(cli.CliError) as excinfo:
        client.call_tool("read", {"account": "a", "resource": "r"})
    assert "unreachable" in excinfo.value.message


def test_transfer_client_sse_parsing(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """An SSE body whose data: lines carry the envelope parses to the
    inner envelope (the streamable-HTTP transport shape)."""
    inner = json.dumps({"status": "ok", "content_b64": "", "sha256": "x", "size": 0})
    sse = f"data: {json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': {'content': [{'text': inner}]}})}\n\n"

    class _SSEHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(sse.encode())

        def log_message(self, *args: Any) -> None:
            return None

    server = HTTPServer(("127.0.0.1", 0), _SSEHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = cli.TransferClient(f"http://127.0.0.1:{server.server_address[1]}/transfer", "tok")
        out = client.call_tool("read", {"account": "a", "resource": "r"})
        assert out["status"] == "ok"
    finally:
        server.shutdown()


def test_cli_error_exit_mapping() -> None:
    assert cli._cli_error_exit(cli.CliError("REFUSED: no grant")) == cli.EXIT_REFUSED
    assert cli._cli_error_exit(cli.CliError("broker unreachable: x")) == cli.EXIT_INFRA
    assert cli._cli_error_exit(cli.CliError("local: missing")) == cli.EXIT_LOCAL


def test_envelope_error_mapping() -> None:
    assert cli._envelope_error({"status": "refused", "reason": "r"}, "fetch").message.startswith("REFUSED")
    assert cli._envelope_error({"status": "error", "error": "e"}, "fetch").message.startswith("INFRA")
    assert cli._envelope_error({"status": "weird"}, "fetch").message.startswith("INFRA")
    assert cli._envelope_error({"status": "ok"}, "fetch") is None


def test_gc_staging_removes_aged_entries(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """gc_staging (a file-DELETION path — the fresh-context review
    finding) removes entries older than GC_MAX_AGE_DAYS and touches
    nothing else. Isolation-verified recipe."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    import os
    import time

    root = tmp_path / "data-broker" / "staging" / "scratch"
    root.mkdir(parents=True)
    old = root / "old.bin"
    old.write_bytes(b"old")
    fresh = root / "fresh.bin"
    fresh.write_bytes(b"fresh")
    ancient = time.time() - (cli.GC_MAX_AGE_DAYS + 1) * 86400
    os.utime(old, (ancient, ancient))
    removed = cli.gc_staging("scratch")
    assert any("old.bin" in p for p in removed)
    assert not old.exists()
    assert fresh.exists()


def test_transfer_client_unreachable_broker() -> None:
    """A dead socket raises CliError with the unreachable message
    (the URLError arm; S5-4 coverage while wiring the initialize fix)."""
    client = cli.TransferClient("http://127.0.0.1:1/transfer", "tok", timeout=1)
    with pytest.raises(cli.CliError, match="unreachable"):
        client.call_tool("read", {"account": "scratch", "resource": "x"})


def test_transfer_client_http_error_500() -> None:
    """A 500 from the broker raises CliError('broker HTTP 500') (the
    non-401 HTTPError arm)."""
    server = HTTPServer(("127.0.0.1", 0), _500_handler())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = cli.TransferClient(
            f"http://127.0.0.1:{server.server_address[1]}/transfer", "tok"
        )
        with pytest.raises(cli.CliError, match="broker HTTP 500"):
            client.call_tool("read", {"account": "scratch", "resource": "x"})
    finally:
        server.shutdown()


def _500_handler() -> type:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args: Any) -> None:
            return None

    return Handler
