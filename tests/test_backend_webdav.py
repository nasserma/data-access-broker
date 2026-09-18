"""S4-3 battery: the generic WebDAV backend.

Two layers per the suite pattern:

1. Unit battery over a fake httpx transport (recorded response shapes;
   no network): probe, typed error translation, URL shaping, the
   multistatus parser.
2. Real-protocol integration against the scratch WSGIDAV container
   (marked; requires scratch_servers/ up - the suite's
   scratch-Dendrite/scratch-HA pattern, non-blocking).
"""

# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from data_broker.backends.base import (
    AuthError,
    BackendUnavailable,
    NodeInfo,
    ProtocolError,
)
from data_broker.backends.webdav import (
    WebDAVAccount,
    WebDAVBackend,
    _dav_path,
    _parse_propfind,
    _quote_url,
)


def make_account() -> WebDAVAccount:
    return WebDAVAccount(
        name="scratch",
        url="http://127.0.0.1:8466/dav",
        username="agent",
        password_env="WEBDAV_SCRATCH_PASSWORD",
    )


class FakeTransport(httpx.AsyncBaseTransport):
    """Recorded-response transport: maps (method, path-prefix) to canned
    httpx.Response objects (the groupware fixture pattern)."""

    def __init__(self, responses: dict[tuple[str, str], httpx.Response]) -> None:
        self._responses = responses
        self.requests: list[tuple[str, str]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        key = (request.method, str(request.url))
        self.requests.append(key)
        if key in self._responses:
            return self._responses[key]
        return httpx.Response(404, text="not found")


def _resp(status: int, **kwargs: Any) -> httpx.Response:
    return httpx.Response(status, request=httpx.Request("GET", "http://x"), **kwargs)


# ------------------------------------------------------------------ account


def test_account_rejects_bad_url() -> None:
    with pytest.raises(ValueError):
        WebDAVAccount(name="x", url="ftp://host", username="u", password_env="P")


def test_account_requires_password_env() -> None:
    with pytest.raises(ValueError):
        WebDAVAccount(name="x", url="http://h", username="u", password_env="")


def test_password_resolution_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WEBDAV_SCRATCH_PASSWORD", raising=False)
    account = make_account()
    with pytest.raises(AuthError):
        account.resolve_password()


def test_backend_requires_accounts() -> None:
    with pytest.raises(ValueError):
        WebDAVBackend({})


# ------------------------------------------------------------------ probe


def test_probe_auth_failure() -> None:
    transport = FakeTransport({("OPTIONS", "http://127.0.0.1:8466/dav"): _resp(401)})
    backend = WebDAVBackend({"scratch": make_account()}, http_client=httpx.AsyncClient(transport=transport))
    with pytest.raises(AuthError):
        asyncio.run(backend.connect("scratch"))


def test_probe_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Connection failure -> BackendUnavailable. Targets a reserved
    closed loopback port (not the scratch server, which may be up)."""
    monkeypatch.setenv("WEBDAV_SCRATCH_PASSWORD", "pw")
    account = WebDAVAccount(
        name="closed", url="http://127.0.0.1:1", username="agent", password_env="WEBDAV_SCRATCH_PASSWORD"
    )
    backend = WebDAVBackend({"closed": account})
    with pytest.raises(BackendUnavailable):
        asyncio.run(backend.connect("closed"))


def test_probe_unknown_account() -> None:
    backend = WebDAVBackend({"scratch": make_account()})
    with pytest.raises(ValueError):
        asyncio.run(backend.connect("nope"))


def test_probe_requires_class1(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBDAV_SCRATCH_PASSWORD", "pw")
    response = _resp(200, headers={"DAV": ""})
    backend = WebDAVBackend({"scratch": make_account()})
    backend._client = httpx.AsyncClient(transport=_StaticAsyncTransport(responses=[response]))
    with pytest.raises(ProtocolError):
        asyncio.run(backend.connect("scratch"))


class _StaticTransport(httpx.AsyncBaseTransport):
    """Returns the next canned response for any request."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self._responses = responses

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return self._responses.pop(0)


class _StaticAsyncTransport(httpx.AsyncBaseTransport):
    """Returns the next canned response for any request (async)."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self._responses = responses

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return self._responses.pop(0)


# ------------------------------------------------------------------ helpers


def test_dav_path_encoding() -> None:
    assert _dav_path("Work") == "/Work"
    assert _dav_path("My Docs/2026") == "/My%20Docs/2026"


def test_quote_url() -> None:
    assert _quote_url("http://h/dav/a b") == "http://h/dav/a%20b"


_MULTISTATUS = """<?xml version="1.0" encoding="utf-8"?>
<d:multistatus xmlns:d="DAV:">
 <d:response>
  <d:href>/dav/Docs/</d:href>
  <d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype>
  <d:getlastmodified>Mon, 14 Sep 2026 10:00:00 GMT</d:getlastmodified>
  </d:prop></d:propstat>
 </d:response>
 <d:response>
  <d:href>/dav/notes.txt</d:href>
  <d:propstat><d:prop><d:resourcetype/>
  <d:getcontentlength>42</d:getcontentlength>
  <d:getlastmodified>Mon, 14 Sep 2026 11:00:00 GMT</d:getlastmodified>
  </d:prop></d:propstat>
 </d:response>
</d:multistatus>"""


def test_parse_propfind() -> None:
    infos = _parse_propfind(_MULTISTATUS)
    assert infos == [
        NodeInfo(name="Docs", is_dir=True, size=None, modified="Mon, 14 Sep 2026 10:00:00 GMT"),
        NodeInfo(name="notes.txt", is_dir=False, size=42, modified="Mon, 14 Sep 2026 11:00:00 GMT"),
    ]


def test_parse_propfind_empty() -> None:
    assert _parse_propfind("") == []
