"""Final coverage battery (the supervisory-review coverage finding):
TransportError arms per verb on both backends, credential-validation
arms, and parser skip arms. The stored client is the source of truth
after connect(), so dead-transport swaps replace _connected[account].
"""

# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio

import httpx
import pytest

import data_broker.backends.webdav as wd
from data_broker.backends.base import AuthError, BackendUnavailable
from data_broker.backends.onedrive import GraphAccount, GraphDriveBackend, StaticTokenProvider
from data_broker.backends.webdav import WebDAVAccount, WebDAVBackend


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBDAV_PW", "pw")
    monkeypatch.setenv("ONEDRIVE_TOKEN_TEST", "tok")


class _DeadTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.TransportError("dead")


def make_webdav() -> WebDAVBackend:
    backend = WebDAVBackend(
        {
            "scratch": WebDAVAccount(
                name="scratch", url="http://g.test/dav", username="u", password_env="WEBDAV_PW"
            )
        }
    )
    probe = httpx.AsyncClient(
        transport=_FakeTransport({("OPTIONS", "/dav"): httpx.Response(200, headers={"DAV": "1"}, request=httpx.Request("GET", "http://x"))})
    )
    backend._client = probe
    asyncio.run(backend.connect("scratch"))
    asyncio.run(probe.aclose())
    backend._connected["scratch"] = httpx.AsyncClient(transport=_DeadTransport())
    return backend


class _FakeTransport(httpx.AsyncBaseTransport):
    def __init__(self, routes: dict) -> None:
        self._routes = routes

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        key = (request.method, str(request.url.path))
        if key in self._routes:
            return self._routes[key]
        return httpx.Response(404, request=httpx.Request("GET", "http://x"))


def _close(backend: WebDAVBackend) -> None:
    stored = backend._connected.pop("scratch", None)
    if stored is not None:
        asyncio.run(stored.aclose())


VERBS_W = [
    ("list", ("scratch", "Docs")),
    ("read", ("scratch", "Docs/a.txt")),
    ("write", ("scratch", "Docs/a.txt", b"x")),
    ("move", ("scratch", "Docs/a.txt", "Docs/b.txt")),
    ("trash", ("scratch", "Docs/a.txt")),
    ("mkdir", ("scratch", "Docs/Sub")),
    ("capabilities", ("scratch",)),
]


@pytest.mark.parametrize("verb,args", VERBS_W)
def test_webdav_transport_error_per_verb(verb: str, args: tuple) -> None:
    backend = make_webdav()
    try:
        with pytest.raises(BackendUnavailable):
            asyncio.run(getattr(backend, verb)(*args))
    finally:
        _close(backend)


def test_webdav_account_validation() -> None:
    with pytest.raises(ValueError):
        WebDAVAccount(name="", url="http://h", username="u", password_env="P")


def test_webdav_quote_url_passthrough() -> None:
    from data_broker.backends.webdav import _quote_url

    assert _quote_url("not-a-url") == "not-a-url"


def test_webdav_parser_skips() -> None:
    """The multistatus parser skips entries with no final path segment."""
    xml = (
        '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">'
        "<d:response><d:href></d:href></d:response>"  # no name -> skip
        "<d:response><d:href>/dav/Docs/</d:href>"
        "<d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype>"
        "</d:prop></d:propstat></d:response>"
        "</d:multistatus>"
    )
    infos = wd._parse_propfind(xml)
    assert [i.name for i in infos] == ["Docs"]
    assert infos[0].is_dir is True


def test_webdav_probe_class1_missing() -> None:
    """A probe that advertises no class-1 WebDAV raises ProtocolError."""
    backend = WebDAVBackend(
        {"scratch": WebDAVAccount(name="s", url="http://g.test/dav", username="u", password_env="WEBDAV_PW")}
    )
    backend._client = httpx.AsyncClient(
        transport=_FakeTransport({("OPTIONS", "/dav"): httpx.Response(200, headers={"DAV": ""}, request=httpx.Request("GET", "http://x"))})
    )
    from data_broker.backends.base import ProtocolError

    with pytest.raises(ProtocolError):
        asyncio.run(backend.connect("scratch"))


def test_webdav_connect_auth_arm() -> None:
    """401 on the probe raises AuthError before any class-1 check."""
    backend = WebDAVBackend(
        {"scratch": WebDAVAccount(name="s", url="http://g.test/dav", username="u", password_env="WEBDAV_PW")}
    )
    backend._client = httpx.AsyncClient(
        transport=_FakeTransport({("OPTIONS", "/dav"): httpx.Response(401, request=httpx.Request("GET", "http://x"))})
    )
    with pytest.raises(AuthError):
        asyncio.run(backend.connect("scratch"))


# ------------------------------------------------------------------ onedrive


def _connected_onedrive() -> GraphDriveBackend:
    account = GraphAccount(name="work", tenant_id="t", client_id="c", token_env="ONEDRIVE_TOKEN_TEST")
    backend = GraphDriveBackend(accounts={"work": account}, token_provider=StaticTokenProvider("tok"))
    probe = httpx.AsyncClient(
        transport=_FakeTransport({("GET", "/v1.0/me/drive"): _json(200, {"id": "d1"})})
    )
    backend._client = probe
    asyncio.run(backend.connect("work"))
    backend._client = httpx.AsyncClient(transport=_FakeDead())
    return backend


def _json(status: int, body: object) -> httpx.Response:
    import json

    return httpx.Response(status, content=__import__("json").dumps(body).encode(), headers={"Content-Type": "application/json"}, request=httpx.Request("GET", "http://x"))


class _FakeTransport(httpx.AsyncBaseTransport):
    def __init__(self, routes: dict) -> None:
        self._routes = routes

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        key = (request.method, str(request.url.path))
        if key in self._routes:
            return self._routes[key]
        return httpx.Response(404, request=httpx.Request("GET", "http://x"))


def _json(status: int, body: object) -> httpx.Response:
    import json

    return httpx.Response(
        status,
        content=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        request=httpx.Request("GET", "http://x"),
    )


class _FakeDead(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.TransportError("dead")


def _close_o(backend: GraphDriveBackend) -> None:
    import contextlib

    with contextlib.suppress(Exception):
        asyncio.run(backend.close())


VERBS_O = [
    ("list", ("work", "")),
    ("read", ("work", "Work/notes.txt")),
    ("write", ("work", "Work/notes.txt", b"x")),
    ("move", ("work", "Work/notes.txt", "Work/b.txt")),
    ("trash", ("work", "Work/notes.txt")),
    ("mkdir", ("work", "Work/Sub")),
    ("capabilities", ("work",)),
]


@pytest.mark.parametrize("verb,args", VERBS_O)
def test_onedrive_transport_error_per_verb(verb: str, args: tuple) -> None:
    backend = _connected_onedrive()
    with pytest.raises(BackendUnavailable):
        asyncio.run(getattr(backend, verb)(*args))
    _close_o(backend)


def test_onedrive_connect_transport_error() -> None:
    account = GraphAccount(name="w", tenant_id="t", client_id="c", token_env="ONEDRIVE_TOKEN_TEST")
    backend = GraphDriveBackend(accounts={"w": account}, token_provider=StaticTokenProvider("tok"))
    backend._client = httpx.AsyncClient(transport=_FakeDead())
    with pytest.raises(BackendUnavailable):
        asyncio.run(backend.connect("w"))
    _close_o(backend)


def test_onedrive_children_url_shape() -> None:
    from data_broker.backends.onedrive import _children_url

    assert _children_url("") == "https://graph.microsoft.com/v1.0/me/drive/root/children"
    assert "/me/drive/root:/Work:/children" in _children_url("Work")


def test_onedrive_translate_retry_after() -> None:
    from data_broker.backends.onedrive import _translate

    resp = httpx.Response(429, headers={"Retry-After": "12"}, request=httpx.Request("GET", "http://x"))
    err = _translate(resp)
    assert isinstance(err, BackendUnavailable)
    assert "Retry-After: 12" in str(err)