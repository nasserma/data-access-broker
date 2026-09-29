"""Regression battery for the Sep 28 2026 401 defect (transfer-CLI fetch
of a granted file -> 'internal error: credentials refused').

Root cause: WebDAVBackend threaded Basic auth through connect() only;
httpx does not cache per-request auth, so every operation verb issued an
unauthenticated request. The battery asserts via a header-recording
transport that EVERY request this backend makes carries credentials —
including the auth used on connect() itself, so the probe is not
special-cased. No network: canned responses only.
"""

# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import asyncio
import base64

import httpx
import pytest

from data_broker.backends.base import AuthError
from data_broker.backends.webdav import WebDAVAccount, WebDAVBackend

expected_basic_value = "Basic " + base64.b64encode(b"u:pw").decode()


@pytest.fixture(autouse=True)
def _backend_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBDAV_PW", "pw")


class HeaderRecordingTransport(httpx.AsyncBaseTransport):
    """Maps (method, exact path) to canned responses and records the
    Authorization header (or None) of every issued request."""

    def __init__(self, routes: dict[tuple[str, str], httpx.Response]) -> None:
        self._routes = routes
        self.auth_seen: list[str | None] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        key = (request.method, str(request.url.path))
        self.auth_seen.append(request.headers.get("Authorization"))
        if key in self._routes:
            return self._routes[key]
        return httpx.Response(404, text="not found", request=request)


def _w_routes() -> dict[tuple[str, str], httpx.Response]:
    return {
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


def _basic_value(auth: tuple[str, str] | None) -> str | None:
    if auth is None:
        return None
    return "Basic " + base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()


def make_webdav() -> WebDAVBackend:
    account = WebDAVAccount(
        name="scratch", url="http://g.test/dav", username="u", password_env="WEBDAV_PW"
    )
    return WebDAVBackend({"scratch": account})


def test_webdav_every_request_carries_auth() -> None:
    """Every HTTP request across a full verb cycle carries the account's
    Basic credentials — including the connect() probe. Regression for the
    401/credentials-refused transfer defect."""
    transport = HeaderRecordingTransport(_w_routes())
    backend = make_webdav()
    backend._client = httpx.AsyncClient(transport=transport)

    async def run() -> None:
        await backend.connect("scratch")
        try:
            await backend.list("scratch", "Docs")
            await backend.read("scratch", "Docs/a.txt")
            await backend.write("scratch", "Docs/a.txt", b"payload\n")
            await backend.move("scratch", "Docs/a.txt", "Docs/b.txt")
            await backend.trash("scratch", "Docs/a.txt")
            await backend.mkdir("scratch", "Docs/Sub")
            await backend.capabilities("scratch")
        finally:
            await backend.close()

    asyncio.run(run())

    assert transport.auth_seen, "no requests recorded"
    for method_path, seen in zip(
        [
            ("OPTIONS", "/dav"),
            ("PROPFIND", "/dav/Docs"),
            ("GET", "/dav/Docs/a.txt"),
            ("PUT", "/dav/Docs/a.txt"),
            ("MOVE", "/dav/Docs/a.txt"),
            ("DELETE", "/dav/Docs/a.txt"),
            ("MKCOL", "/dav/Docs/Sub"),
            ("OPTIONS", "/dav"),  # capabilities()
        ],
        transport.auth_seen,
        strict=True,
    ):
        assert seen == expected_basic_value, (
            f"request {method_path} sent Authorization={seen!r}; "
            "every backend request must be authenticated"
        )


def test_webdav_auth_env_missing_fails_closed_per_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """A verb whose password env vanishes mid-session fails closed with
    AuthError, not an unauthenticated request."""
    transport = HeaderRecordingTransport(_w_routes())
    backend = make_webdav()
    backend._client = httpx.AsyncClient(transport=transport)

    async def run() -> None:
        await backend.connect("scratch")
        try:
            monkeypatch.delenv("WEBDAV_PW")
            with pytest.raises(AuthError):
                await backend.read("scratch", "Docs/a.txt")
        finally:
            await backend.close()

    asyncio.run(run())
    # Exactly one request was made (the authed probe); the failed verb
    # must NOT have hit the wire unauthenticated.
    assert transport.auth_seen == [expected_basic_value]


def test_webdav_auth_env_rotation_picked_up_per_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-call credential resolution: rotating the env app password is
    picked up on the next verb without reconnect."""
    expected_rotated = "Basic " + base64.b64encode(b"u:pw2").decode()
    transport = HeaderRecordingTransport(_w_routes())
    backend = make_webdav()
    backend._client = httpx.AsyncClient(transport=transport)

    async def run() -> None:
        await backend.connect("scratch")
        try:
            await backend.read("scratch", "Docs/a.txt")
            monkeypatch.setenv("WEBDAV_PW", "pw2")
            await backend.read("scratch", "Docs/a.txt")
        finally:
            await backend.close()

    asyncio.run(run())
    # request 0 = connect() probe, request 1 = pre-rotation read,
    # request 2 = post-rotation read (pick-up without reconnect).
    assert transport.auth_seen == [expected_basic_value, expected_basic_value, expected_rotated]
