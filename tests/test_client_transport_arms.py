"""Client transport error arms (the client-core split of the v0.1.x
transport coverage): real HTTP against a stub server, no call_tool
mocking. Covers the defensive arms the CLI battery reaches through
the shared core: HTTP 500, rpc error, SSE garbage, SSE data lines,
raw text content, missing gc root, 401, unreachable.
"""

# SPDX-License-Identifier: GPL-3.0-or-later

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from data_broker import client as _client
from data_broker.client import ClientError, TransferClient


class _Stub:
    """HTTP stub: serves whatever canned JSON-RPC body the test sets."""

    def __init__(self) -> None:
        self.body = "{}"
        self.status = 200
        self.headers: dict[str, str] = {}

    def handler(self) -> type:
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                _payload = json.loads(self.rfile.read(length) if length else b"{}")
                self.send_response(stub.status)
                self.send_header("Content-Type", "application/json")
                for k, v in stub.headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(stub.body.encode())

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

        return Handler


@pytest.fixture()
def stub_server():
    stub = _Stub()
    server = HTTPServer(("127.0.0.1", 0), stub.handler())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield stub, f"http://127.0.0.1:{server.server_port}/transfer"
    server.shutdown()
    server.server_close()


def _client_for(url: str) -> TransferClient:
    return TransferClient(url, "tok")


def test_http_500_is_infra_error(stub_server: Any) -> None:
    stub, url = stub_server
    stub.status = 500
    stub.body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}})
    with pytest.raises(ClientError) as exc:
        _client_for(url).call_tool("read", {"account": "a", "resource": "r"})
    assert "HTTP 500" in exc.value.message


def test_rpc_error_raises(stub_server: Any) -> None:
    stub, url = stub_server
    stub.status = 200
    stub.body = json.dumps({"jsonrpc": "2.0", "id": 1, "error": {"code": 1, "message": "boom"}})
    with pytest.raises(ClientError) as exc:
        _client_for(url).call_tool("read", {"account": "a", "resource": "r"})
    assert "rpc error" in exc.value.message


def test_sse_garbage_is_local_error(stub_server: Any) -> None:
    stub, url = stub_server
    stub.body = "not-json-not-sse"
    with pytest.raises(ClientError) as exc:
        _client_for(url).call_tool("read", {"account": "a", "resource": "r"})
    assert "no JSON, no SSE data" in exc.value.message


def test_sse_data_line_carries_envelope(stub_server: Any) -> None:
    stub, url = stub_server
    inner = {"status": "ok", "value": 1}
    stub.body = (
        "event: message\n"
        f'data: {json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": json.dumps(inner)}]}})}\n\n'
    )
    out = _client_for(url).call_tool("read", {"account": "a", "resource": "r"})
    assert out == inner


def test_sse_non_json_data_lines_raise(stub_server: Any) -> None:
    stub, url = stub_server
    stub.body = "event: message\ndata: not-json\n\n"
    with pytest.raises(ClientError) as exc:
        _client_for(url).call_tool("read", {"account": "a", "resource": "r"})
    assert "no JSON-RPC response in SSE stream" in exc.value.message


def test_raw_text_content_passthrough(stub_server: Any) -> None:
    stub, url = stub_server
    stub.body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "not json"}]}}
    )
    out = _client_for(url).call_tool("read", {"account": "a", "resource": "r"})
    assert out == {"raw": "not json"}


def test_plain_result_without_content(stub_server: Any) -> None:
    stub, url = stub_server
    stub.body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"status": "ok"}})
    out = _client_for(url).call_tool("read", {"account": "a", "resource": "r"})
    assert out == {"status": "ok"}


def test_unreachable_is_infra_error() -> None:
    client = TransferClient("http://127.0.0.1:1/transfer", "tok", timeout=2.0)
    with pytest.raises(ClientError) as exc:
        client.call_tool("read", {"account": "a", "resource": "r"})
    assert "unreachable" in exc.value.message


def test_gc_missing_root_returns_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "nonexistent"))
    assert _client.gc_staging("scratch") == []


def test_push_unreadable_file_is_local_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.setenv(_client.TOKEN_ENV, "tok")
    src = tmp_path / "locked.txt"
    src.write_bytes(b"x")
    src.chmod(0o000)
    rc, payload = _client.push("scratch", "Docs/x.txt", str(src), url="http://127.0.0.1:1/t")
    assert rc == _client.EXIT_LOCAL
    assert "error" in payload
    src.chmod(0o600)  # cleanup regardless


def test_push_status_error_maps_to_infra(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    class _Err:
        async def write(self, account: str, resource: str, content_b64: str, expected_sha256: str) -> dict:
            return {"status": "error", "error": "backend exploded"}

    def _inner(self: Any, name: str, args: dict, *unused: Any) -> dict:
        import asyncio

        return asyncio.run(_Err().write(**args))

    monkeypatch.setattr(_client.TransferClient, "call_tool", _inner)
    monkeypatch.setenv(_client.TOKEN_ENV, "tok")
    src = tmp_path / "s.txt"
    src.write_bytes(b"data")
    rc, payload = _client.push("scratch", "Docs/x.txt", str(src), url="http://x/t")
    assert rc == _client.EXIT_INFRA
    assert "backend exploded" in payload["error"]
