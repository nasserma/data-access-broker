"""S4-4 battery: the OneDrive backend (Graph drives, recorded fixtures).

Per the goal contract section 3: MS Graph drives via the msal/msgraph
stack validated in the groupware build; Graph drive resources here are
a DISTINCT family from the groupware broker's Graph mail/calendar
family (no shared token scope between brokers). Live-tenant wiring is
a deployment-session task; this battery runs against recorded HTTP
fixtures (the groupware teams_graph test pattern).

The backend implements the same store backend Protocol as WebDAV:
connect (token must resolve), list, read, write, move, trash, mkdir,
capabilities - against /me/drive/root children endpoints.

Error translation per the groupware pattern: 401/403 -> AuthError;
429 -> BackendUnavailable (Retry-After); 5xx/connection ->
BackendUnavailable; everything else -> ProtocolError.
"""

# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import asyncio
import json as _json
from typing import Any

import httpx
import pytest

from data_broker.backends.base import (
    AuthError,
    NodeInfo,
)
from data_broker.backends.onedrive import GraphAccount, GraphDriveBackend, StaticTokenProvider

T0 = "2026-09-18T12:00:00Z"


def make_account() -> GraphAccount:
    return GraphAccount(
        name="personal",
        tenant_id="t-tenant",
        client_id="t-client",
        token_env="ONEDRIVE_TEST_TOKEN",
    )


def fixture_response(status: int, body: dict[str, Any] | list[Any] | None = None) -> httpx.Response:
    import json

    content = json.dumps(body).encode() if body is not None else b""
    return httpx.Response(
        status,
        content=content,
        headers={"Content-Type": "application/json"},
        request=httpx.Request("GET", "https://graph.microsoft.com/v1.0"),
    )


class RecordingTransport(httpx.AsyncBaseTransport):
    """Recorded-fixture transport: maps (method, path) to canned
    responses; records every request for assertion."""

    def __init__(self, fixtures: dict[tuple[str, str], httpx.Response]) -> None:
        self._fixtures = dict(fixtures)
        self.requests: list[tuple[str, str, bytes | None]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path = str(request.url.path)
        self.requests.append((request.method, path, request.read()))
        if (request.method, path) in self._fixtures:
            return self._fixtures[(request.method, path)]
        return fixture_response(404, {"error": {"code": "itemNotFound", "message": "x"}})


# ------------------------------------------------------------------ account


def test_account_requires_tenant() -> None:
    with pytest.raises(ValueError):
        GraphAccount(name="x", tenant_id="", client_id="c", token_env="T")


def test_account_requires_client() -> None:
    with pytest.raises(ValueError):
        GraphAccount(name="x", tenant_id="t", client_id="", token_env="T")


def test_token_scope_is_drive_family() -> None:
    """Distinct token family from the groupware broker's Graph scopes."""
    account = make_account()
    assert "Files.Read" in account.scopes
    assert "Mail.Read" not in account.scopes


def test_token_provider_records() -> None:
    provider = StaticTokenProvider("tok-1")
    assert provider.get_token("personal") == "tok-1"


# ------------------------------------------------------------------ fixtures

_DRIVE_LIST = {
    "value": [
        {
            "id": "item-1",
            "name": "Work",
            "folder": {"childCount": 2},
            "lastModifiedDateTime": "2026-09-14T10:00:00Z",
        },
        {
            "id": "item-2",
            "name": "notes.txt",
            "file": {"mimeType": "text/plain"},
            "size": 42,
            "lastModifiedDateTime": "2026-09-14T11:00:00Z",
        },
    ]
}

_DRIVE_ROOT = {"id": "root-id", "name": "root", "folder": {"childCount": 2}}


# ------------------------------------------------------------------ transport


def test_list_via_fixtures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONEDRIVE_TEST_TOKEN", "tok")
    transport = RecordingTransport(
        {
            ("GET", "/v1.0/me/drive"): fixture_response(200, _DRIVE_ROOT),
            ("GET", "/v1.0/me/drive/root/children"): fixture_response(200, _DRIVE_LIST),
        }
    )
    backend = GraphDriveBackend(
        accounts={"personal": make_account()},
        token_provider=StaticTokenProvider("tok"),
        http_client=httpx.AsyncClient(transport=transport),
    )

    async def run() -> list:
        await backend.connect("personal")
        try:
            return await backend.list("personal", "")
        finally:
            await backend.close()


    entries = asyncio.run(run())
    assert entries == [
        NodeInfo(name="Work", is_dir=True, size=None, modified="2026-09-14T10:00:00Z"),
        NodeInfo(name="notes.txt", is_dir=False, size=42, modified="2026-09-14T11:00:00Z"),
    ]


def test_write_put_content(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONEDRIVE_TEST_TOKEN", "tok")
    transport = RecordingTransport(
        {
            ("GET", "/v1.0/me/drive"): fixture_response(200, _DRIVE_ROOT),
            ("PUT", "/v1.0/me/drive/root:/Work/notes.txt:/content"): fixture_response(
                201, {"id": "new-1", "name": "notes.txt", "size": 14}
            ),
        }
    )
    backend = GraphDriveBackend(
        accounts={"personal": make_account()},
        token_provider=StaticTokenProvider("tok"),
        http_client=httpx.AsyncClient(transport=transport),
    )

    async def run() -> None:
        await backend.connect("personal")
        try:
            await backend.write("personal", "Work/notes.txt", b"hello drive\n")
        finally:
            await backend.close()


    asyncio.run(run())
    method, path, body = transport.requests[-1]
    assert method == "PUT"
    assert body == b"hello drive\n"


def test_auth_failure_translates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONEDRIVE_TEST_TOKEN", "tok")
    transport = RecordingTransport(
        {
            ("GET", "/v1.0/me/drive"): fixture_response(200, _DRIVE_ROOT),
            ("GET", "/v1.0/me/drive/root/children"): fixture_response(401, {"error": {"code": "x", "message": "y"}}),
        }
    )
    backend = GraphDriveBackend(
        accounts={"personal": make_account()},
        token_provider=StaticTokenProvider("tok"),
        http_client=httpx.AsyncClient(transport=transport),
    )

    async def run() -> None:
        await backend.connect("personal")
        try:
            await backend.list("personal", "")
        finally:
            await backend.close()


    with pytest.raises(AuthError):
        asyncio.run(run())


# ------------------------------------------- schema-strict transport (S6-3 H1)


def test_move_forwards_real_patch_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """H1: move's PATCH body carries exactly the fields the real Graph
    update schema accepts (name + optional parentReference); the
    forwarded shape is asserted, not assumed."""
    monkeypatch.setenv("ONEDRIVE_TEST_TOKEN", "tok")
    transport = RecordingTransport(
        {
            ("GET", "/v1.0/me/drive"): fixture_response(200, _DRIVE_ROOT),
            ("PATCH", "/v1.0/me/drive/root:/Work/notes.txt"): fixture_response(200, {}),
        }
    )
    backend = GraphDriveBackend(
        accounts={"personal": make_account()},
        token_provider=StaticTokenProvider("tok"),
        http_client=httpx.AsyncClient(transport=transport),
    )

    async def run() -> None:
        await backend.connect("personal")
        try:
            await backend.move("personal", "Work/notes.txt", "Work/renamed.txt")
        finally:
            await backend.close()

    import asyncio as _asyncio

    _asyncio.run(run())
    method, path, body = transport.requests[-1]
    assert (method, path) == ("PATCH", "/v1.0/me/drive/root:/Work/notes.txt")
    sent = _json.loads(body or b"{}")
    assert sent == {
        "name": "renamed.txt",
        "parentReference": {"path": "/drive/root:/Work"},
    }


def test_mkdir_forwards_real_post_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """H1: mkdir's POST body is the real Graph create-folder schema
    (name, folder: {}, conflictBehavior fail)."""
    monkeypatch.setenv("ONEDRIVE_TEST_TOKEN", "tok")
    transport = RecordingTransport(
        {
            ("GET", "/v1.0/me/drive"): fixture_response(200, _DRIVE_ROOT),
            ("POST", "/v1.0/me/drive/root:/Work:/children"): fixture_response(201, {"id": "n1"}),
        }
    )
    backend = GraphDriveBackend(
        accounts={"personal": make_account()},
        token_provider=StaticTokenProvider("tok"),
        http_client=httpx.AsyncClient(transport=transport),
    )

    async def run() -> None:
        await backend.connect("personal")
        try:
            await backend.mkdir("personal", "Work/newdir")
        finally:
            await backend.close()

    import asyncio as _asyncio

    _asyncio.run(run())
    method, path, body = transport.requests[-1]
    assert (method, path) == ("POST", "/v1.0/me/drive/root:/Work:/children")
    sent = _json.loads(body or b"{}")
    assert sent == {
        "name": "newdir",
        "folder": {},
        "@microsoft.graph.conflictBehavior": "fail",
    }
