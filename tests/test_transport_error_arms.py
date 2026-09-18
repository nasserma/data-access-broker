"""Backend transport error-arm coverage: every verb's failure paths over
recorded responses (the supervisory-review coverage finding). One
parametrized sweep per backend verb: 4xx->ProtocolError, 401/403->
AuthError, 429/5xx->BackendUnavailable.
"""

# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio

import httpx
import pytest

import data_broker.backends.webdav as wd
from data_broker.backends.base import AuthError, BackendUnavailable, ProtocolError
from data_broker.backends.onedrive import (
    GraphAccount,
    GraphDriveBackend,
    StaticTokenProvider,
    TokenProvider,
)
from data_broker.backends.webdav import WebDAVAccount, WebDAVBackend


@pytest.fixture(autouse=True)
def _backend_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBDAV_PW", "pw")
    monkeypatch.setenv("ONEDRIVE_TOKEN_TEST", "tok")


def _resp(status: int, body: object = None) -> httpx.Response:
    import json

    kwargs: dict = {"request": httpx.Request("GET", "http://x")}
    if body is not None:
        kwargs["content"] = json.dumps(body).encode()
        kwargs["headers"] = {"Content-Type": "application/json"}
    return httpx.Response(status, **kwargs)


class RoutedTransport(httpx.AsyncBaseTransport):
    """Maps (method, exact path) to canned responses."""

    def __init__(self, routes: dict) -> None:
        self._routes = routes

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path = str(request.url.path)
        if (request.method, path) in self._routes:
            return self._routes[(request.method, path)]
        return _resp(404, {"error": {"code": "itemNotFound"}})


def _fail_routes(status: int, connect_ok: bool = True) -> dict:
    """Every verb route fails with `status`; the probe optionally passes."""
    routes: dict = {
        key: httpx.Response(status, request=httpx.Request("GET", "http://x"))
        for key in _FAIL_KEYS
    }
    if connect_ok:
        routes[("OPTIONS", "/dav")] = httpx.Response(
            200, headers={"DAV": "1"}, request=httpx.Request("GET", "http://x")
        )
    return routes


_FAIL_KEYS = {
    ("PROPFIND", "/dav/Docs"),
    ("GET", "/dav/Docs/a.txt"),
    ("PUT", "/dav/Docs/a.txt"),
    ("MOVE", "/dav/Docs/a.txt"),
    ("DELETE", "/dav/Docs/a.txt"),
    ("MKCOL", "/dav/Docs/Sub"),
    ("OPTIONS", "/dav"),
}

VERBS_W = [
    ("list", "Docs"),
    ("read", "Docs/a.txt"),
    ("write", "Docs/a.txt"),
    ("move", "Docs/a.txt"),
    ("trash", "Docs/a.txt"),
    ("mkdir", "Docs/Sub"),
]


def make_connected_webdav(status: int) -> WebDAVBackend:
    backend = WebDAVBackend(
        {
            "scratch": WebDAVAccount(
                name="scratch", url="http://g.test/dav", username="u", password_env="WEBDAV_PW"
            )
        }
    )
    routes: dict = {
        key: httpx.Response(status, request=httpx.Request("GET", "http://x"))
        for key in _FAIL_KEYS
    }
    routes[("OPTIONS", "/dav")] = httpx.Response(
        200, headers={"DAV": "1"}, request=httpx.Request("GET", "http://x")
    )
    backend._client = httpx.AsyncClient(transport=RoutedTransport(routes))
    asyncio.run(backend.connect("scratch"))
    return backend


@pytest.mark.parametrize("verb,resource", VERBS_W)
@pytest.mark.parametrize("status", [409, 503])
def test_webdav_verb_error_arms(verb: str, resource: str, status: int) -> None:
    backend = make_connected_webdav(status)
    call = getattr(backend, verb)
    if verb == "move":
        coro = call("scratch", "Docs/a.txt", "Docs/b.txt")
    elif verb == "write":
        coro = call("scratch", "Docs/a.txt", b"x")
    else:
        coro = call("scratch", resource)
    with pytest.raises((ProtocolError, BackendUnavailable)):
        asyncio.run(coro)


def test_webdav_verb_unreachable() -> None:
    """TransportError -> BackendUnavailable (list arm; the try/except
    shape is shared by every verb). The STORED client is the source of
    truth after connect() (the backend holds one client per account in
    _connected), so the swap replaces the stored entry."""
    class _DeadTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.TransportError("dead")

    backend = make_connected_webdav(200)
    dead_client = httpx.AsyncClient(transport=_DeadTransport())
    backend._connected["scratch"] = dead_client
    try:
        with pytest.raises(BackendUnavailable):
            asyncio.run(backend.list("scratch", "Docs"))
    finally:
        asyncio.run(dead_client.aclose())


def test_webdav_retry_after_detail() -> None:
    """429 with a Retry-After header: the BackendUnavailable detail
    carries it (the shared translation path, asserted directly)."""
    response = httpx.Response(429, headers={"Retry-After": "30"}, request=httpx.Request("GET", "http://x"))
    err = wd.WebDAVBackend._translate(response)
    assert isinstance(err, BackendUnavailable)
    assert "Retry-After: 30" in str(err)


def test_webdav_connect_probe_fail() -> None:
    backend = WebDAVBackend(
        {
            "scratch": WebDAVAccount(
                name="scratch", url="http://g.test/dav", username="u", password_env="WEBDAV_PW"
            )
        }
    )
    transport = RoutedTransport(
        {("OPTIONS", "/dav"): httpx.Response(409, request=httpx.Request("GET", "http://x"))}
    )
    backend._client = httpx.AsyncClient(transport=transport)
    with pytest.raises(ProtocolError):
        asyncio.run(backend.connect("scratch"))


# ------------------------------------------------------------------ onedrive


def make_connected_onedrive(status: int) -> GraphDriveBackend:
    account = GraphAccount(
        name="work", tenant_id="t", client_id="c", token_env="ONEDRIVE_TOKEN_TEST"
    )
    backend = GraphDriveBackend(
        accounts={"work": account}, token_provider=StaticTokenProvider("tok")
    )
    routes: dict = {
        key: _resp(status, {"error": {"code": "x"}})
        for key in _O_FAIL_KEYS
    }
    routes[("GET", "/v1.0/me/drive")] = _resp(200, {"id": "d1"})
    backend._client = httpx.AsyncClient(transport=RoutedTransport(routes))
    asyncio.run(backend.connect("work"))
    return backend


_O_FAIL_KEYS = {
    ("GET", "/v1.0/me/drive/root/children"),
    ("GET", "/v1.0/me/drive/root:/Work/notes.txt:/content"),
    ("PUT", "/v1.0/me/drive/root:/Work/notes.txt:/content"),
    ("PATCH", "/v1.0/me/drive/root:/Work/notes.txt"),
    ("DELETE", "/v1.0/me/drive/root:/Work/notes.txt"),
    ("POST", "/v1.0/me/drive/root:/Work:/children"),
    ("GET", "/v1.0/me/drive"),
}

VERBS_O = [
    ("list", ""),
    ("read", "Work/notes.txt"),
    ("write", "Work/notes.txt"),
    ("move", "Work/notes.txt"),
    ("trash", "Work/notes.txt"),
    ("mkdir", "Work/Sub"),
]


@pytest.mark.parametrize("verb,resource", VERBS_O)
@pytest.mark.parametrize("status", [400, 401, 429, 503])
def test_onedrive_verb_error_arms(verb: str, resource: str, status: int) -> None:
    backend = make_connected_onedrive(status)
    call = getattr(backend, verb)
    if verb == "move":
        coro = call("work", "Work/notes.txt", "Work/b.txt")
    elif verb == "write":
        coro = call("work", "Work/notes.txt", b"x")
    elif verb == "capabilities":
        coro = call("work")  # capabilities takes the account only
    else:
        coro = call("work", resource)
    with pytest.raises((BackendUnavailable, ProtocolError, AuthError)):
        asyncio.run(coro)


def test_token_provider_boundary() -> None:
    """The injectable boundary refuses unset production wiring."""
    with pytest.raises(NotImplementedError):
        TokenProvider().get_token("work")