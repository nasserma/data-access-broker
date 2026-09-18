"""Backend transport coverage (the supervisory-review finding 2):
recorded-response batteries over both backends, plus the tool-surface
arms. Pattern: canned httpx responses per (method, path); no network.
"""

# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from data_broker.backends.base import (
    AuthError,
    BackendUnavailable,
    NodeInfo,
    ProtocolError,
)
from data_broker.backends.onedrive import (
    GraphAccount,
    GraphDriveBackend,
    StaticTokenProvider,
)
from data_broker.backends.webdav import WebDAVAccount, WebDAVBackend


@pytest.fixture(autouse=True)
def _backend_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBDAV_PW", "pw")
    monkeypatch.setenv("ONEDRIVE_TOKEN_TEST", "tok")


def _resp(status: int, body: Any = None) -> httpx.Response:
    kwargs: dict = {"request": httpx.Request("GET", "http://x")}
    if body is not None:
        kwargs["content"] = json.dumps(body).encode()
        kwargs["headers"] = {"Content-Type": "application/json"}
    return httpx.Response(status, **kwargs)


class RoutedTransport(httpx.AsyncBaseTransport):
    """Maps (method, exact path) to canned responses; records requests."""

    def __init__(self, routes: dict[tuple[str, str], httpx.Response]) -> None:
        self._routes = routes
        self.requests: list[tuple[str, str]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        key = (request.method, str(request.url.path))
        self.requests.append(key)
        if key in self._routes:
            return self._routes[key]
        return httpx.Response(404, text="not found")


def make_webdav() -> WebDAVBackend:
    account = WebDAVAccount(
        name="scratch", url="http://g.test/dav", username="u", password_env="WEBDAV_PW"
    )
    return WebDAVBackend({"scratch": account})


def make_onedrive() -> GraphDriveBackend:
    account = GraphAccount(
        name="work", tenant_id="t", client_id="c", token_env="ONEDRIVE_TOKEN_TEST"
    )
    return GraphDriveBackend(accounts={"work": account}, token_provider=StaticTokenProvider("tok"))


# ------------------------------------------------------------------ webdav


def _w_routes(overrides: dict | None = None) -> dict[tuple[str, str], httpx.Response]:
    routes: dict[tuple[str, str], httpx.Response] = {
        ("OPTIONS", "/dav"): httpx.Response(200, headers={"DAV": "1, 2"}, request=httpx.Request("GET", "http://x")),
        ("PROPFIND", "/dav/Docs"): httpx.Response(
            207,
            text=(
                '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">'
                "<d:response><d:href>/dav/Docs/a.txt</d:href>"
                "<d:propstat><d:prop><d:resourcetype/>"
                "<d:getcontentlength>7</d:getcontentlength></d:prop></d:propstat>"
                "</d:response></d:multistatus>"
            ),
            request=httpx.Request("PROPFIND", "http://x"),
        ),
        ("GET", "/dav/Docs/a.txt"): httpx.Response(200, content=b"a.txt\n", request=httpx.Request("GET", "http://x")),
        ("PUT", "/dav/Docs/a.txt"): httpx.Response(204, request=httpx.Request("PUT", "http://x")),
        ("MOVE", "/dav/Docs/a.txt"): httpx.Response(201, request=httpx.Request("GET", "http://x")),
        ("DELETE", "/dav/Docs/a.txt"): httpx.Response(204, request=httpx.Request("GET", "http://x")),
        ("MKCOL", "/dav/Docs/Sub"): httpx.Response(201, request=httpx.Request("GET", "http://x")),
    }
    routes.update(overrides or {})
    return routes


def test_webdav_full_backend_cycle() -> None:
    transport = RoutedTransport(_w_routes())
    backend = make_webdav()
    backend._client = httpx.AsyncClient(transport=transport)

    async def run() -> dict:
        await backend.connect("scratch")
        try:
            entries = await backend.list("scratch", "Docs")
            assert entries == [NodeInfo(name="a.txt", is_dir=False, size=7, modified=None)]
            content = await backend.read("scratch", "Docs/a.txt")
            assert content == b"a.txt\n"
            await backend.write("scratch", "Docs/a.txt", b"payload\n")
            await backend.move("scratch", "Docs/a.txt", "Docs/b.txt")
            await backend.trash("scratch", "Docs/a.txt")
            await backend.mkdir("scratch", "Docs/Sub")
            caps = await backend.capabilities("scratch")
            assert caps["dav_class1"]
        finally:
            await backend.close()
        return {"done": True}

    assert asyncio.run(run())["done"]


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, AuthError),
        (403, AuthError),
        (429, BackendUnavailable),
        (500, BackendUnavailable),
        (409, ProtocolError),
    ],
)
def test_webdav_error_translation(status: int, expected: type) -> None:
    transport = RoutedTransport(
        _w_routes({("GET", "/dav/Docs/a.txt"): httpx.Response(status, request=httpx.Request("GET", "http://x"))})
    )
    backend = make_webdav()
    backend._client = httpx.AsyncClient(transport=transport)

    async def run() -> None:
        await backend.connect("scratch")
        try:
            await backend.read("scratch", "Docs/a.txt")
        finally:
            await backend.close()

    with pytest.raises(expected):
        asyncio.run(run())


def test_webdav_connect_server_error() -> None:
    transport = RoutedTransport(
        {("OPTIONS", "/dav"): httpx.Response(503, request=httpx.Request("GET", "http://x"))}
    )
    backend = make_webdav()
    backend._client = httpx.AsyncClient(transport=transport)
    with pytest.raises(BackendUnavailable):
        asyncio.run(backend.connect("scratch"))


def test_webdav_connect_probe_error() -> None:
    transport = RoutedTransport(
        {("OPTIONS", "/dav"): httpx.Response(409, request=httpx.Request("GET", "http://x"))}
    )
    backend = make_webdav()
    backend._client = httpx.AsyncClient(transport=transport)
    with pytest.raises(ProtocolError):
        asyncio.run(backend.connect("scratch"))


# ------------------------------------------------------------------ onedrive


def _o_routes(overrides: dict | None = None) -> dict[tuple[str, str], httpx.Response]:
    routes: dict[tuple[str, str], httpx.Response] = {
        ("GET", "/v1.0/me/drive"): _resp(200, {"id": "d1", "driveType": "personal", "quota": {"total": 5, "remaining": 4}}),
        ("GET", "/v1.0/me/drive/root/children"): _resp(
            200,
            {
                "value": [
                    {"id": "i1", "name": "Work", "folder": {"childCount": 1}, "lastModifiedDateTime": "2026-09-14T10:00:00Z"},
                    {"id": "i2", "name": "notes.txt", "size": 42, "lastModifiedDateTime": "2026-09-14T11:00:00Z"},
                ]
            },
        ),
        ("GET", "/v1.0/me/drive/root:/Work/notes.txt:/content"): _resp(200, None),
        ("PUT", "/v1.0/me/drive/root:/Work/notes.txt:/content"): _resp(201, {"id": "n1"}),
        ("PATCH", "/v1.0/me/drive/root:/Work/notes.txt"): _resp(200, {"id": "n1"}),
        ("DELETE", "/v1.0/me/drive/root:/Work/notes.txt"): _resp(204, None),
        ("POST", "/v1.0/me/drive/root:/Work:/children"): _resp(201, {"id": "f1", "name": "Sub"}),
    }
    routes.update(overrides or {})
    return routes


def test_onedrive_full_backend_cycle() -> None:
    transport = RoutedTransport(_o_routes())
    backend = make_onedrive()
    backend._client = httpx.AsyncClient(transport=transport)

    async def run() -> None:
        await backend.connect("work")
        try:
            entries = await backend.list("work", "")
            assert entries == [
                NodeInfo(name="Work", is_dir=True, size=None, modified="2026-09-14T10:00:00Z"),
                NodeInfo(name="notes.txt", is_dir=False, size=42, modified="2026-09-14T11:00:00Z"),
            ]
            await backend.read("work", "Work/notes.txt")
            await backend.write("work", "Work/notes.txt", b"data\n")
            await backend.move("work", "Work/notes.txt", "Work/renamed.txt")
            await backend.trash("work", "Work/notes.txt")
            await backend.mkdir("work", "Work/Sub")
            caps = await backend.capabilities("work")
            assert caps["drive_id"] == "d1"
        finally:
            await backend.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, AuthError),
        (429, BackendUnavailable),
        (503, BackendUnavailable),
        (400, ProtocolError),
    ],
)
def test_onedrive_error_translation(status: int, expected: type) -> None:
    transport = RoutedTransport(
        _o_routes({("GET", "/v1.0/me/drive/root/children"): _resp(status, {"error": {"code": "x"}})})
    )
    backend = make_onedrive()
    backend._client = httpx.AsyncClient(transport=transport)

    async def run() -> None:
        await backend.connect("work")
        try:
            await backend.list("work", "")
        finally:
            await backend.close()

    with pytest.raises(expected):
        asyncio.run(run())


def test_onedrive_account_validation() -> None:
    with pytest.raises(ValueError):
        GraphAccount(name="x", tenant_id="t", client_id="c", token_env="", authority="https://ok")
    with pytest.raises(ValueError):
        GraphAccount(name="x", tenant_id="t", client_id="c", token_env="T", authority="ftp://bad")
    with pytest.raises(ValueError):
        GraphDriveBackend(accounts={}, token_provider=StaticTokenProvider())


def test_onedrive_unknown_account() -> None:
    backend = make_onedrive()
    with pytest.raises(ValueError):
        asyncio.run(backend.connect("nope"))


def test_onedrive_read_scopes_distinct_from_groupware() -> None:
    from data_broker.backends.onedrive import READ_SCOPES, WRITE_SCOPES

    assert "Files.Read" in READ_SCOPES
    assert "Files.ReadWrite" in WRITE_SCOPES
