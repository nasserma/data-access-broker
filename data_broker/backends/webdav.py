"""Generic WebDAV backend (S4-3): the v1 reference implementation.

Covers Nextcloud, ownCloud, and any RFC 4918 DAV server (goal contract
section 3: the nextcloud-specific client is replaced by a generic one;
instance entries are named, following the groupware account model).

Talks raw WebDAV verbs over httpx (native async):

    OPTIONS  capability probing at boot (R1: degrade gracefully)
    PROPFIND depth-1  -> list
    GET      -> read
    PUT      -> write (direct where the store versions; the wall owns
               the versioning-dependent write policy)
    MOVE     -> move (with Overwrite:F so an existing destination is
               never clobbered silently)
    DELETE   -> trash (the server's trashbin handles the recoverable
               case where one exists; plain delete otherwise)
    MKCOL    -> mkdir (parent must exist per RFC 4918)

Fail-closed: auth failure -> AuthError; 429/5xx/connection failures ->
BackendUnavailable; everything else -> ProtocolError. No credential
ever appears in an error message.
"""

# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import quote

import httpx
from httpx import TransportError

from data_broker.backends.base import (
    AuthError,
    BackendError,
    BackendUnavailable,
    NodeInfo,
    ProtocolError,
    require_connected,
)

DEFAULT_TIMEOUT = 30.0
_SERVER_ERROR_FLOOR = 500
_CLIENT_ERROR_FLOOR = 400
_THROTTLED = 429
_READ_HEADERS = {"Accept": "*/*", "Depth": "1"}
_XML_PROPF = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<d:propfind xmlns:d="DAV:"><d:prop>'
    "<d:resourcetype/><d:getcontentlength/>"
    "<d:getlastmodified/></d:prop></d:propfind>"
)


class WebDAVAccount:
    """One validated WebDAV store configuration entry (per-store named
    accounts, following the groupware account model)."""

    def __init__(
        self,
        name: str,
        url: str,
        username: str,
        password_env: str,
        verify_ssl: bool = True,
    ) -> None:
        if not name:
            raise ValueError("account name must be nonempty")
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"account {name!r}: url must be an http(s) URL")
        if not password_env:
            raise ValueError(f"account {name!r}: password_env is required")
        self.name = name
        self.url = url.rstrip("/")
        self.username = username
        self.password_env = password_env
        self.verify_ssl = verify_ssl

    def resolve_password(self) -> str:
        """Resolve the app password from the env (fail-closed)."""
        value = os.environ.get(self.password_env, "")
        if not value:
            raise AuthError(f"environment variable {self.password_env!r} is not set")
        return value


def _dav_path(resource: str) -> str:
    """Normalized node components -> URL path segments (percent-encoded,
    no traversal: the components come from the wall's normalizer, but
    the backend re-derives the URL defensively)."""
    components = resource.split("/") if "/" in resource else [resource]
    return "".join("/" + quote(c, safe="") for c in components if c)


class WebDAVBackend:
    """Generic WebDAV adapter (the v1 reference implementation).

    One instance per process; one HTTP client per account. Capability
    probing at boot (OPTIONS) decides trash support (R1); an
    unversioned store is fail-closed for the versioning-dependent write
    policy, documented in DEPLOYMENT.md, not improvised.
    """

    def __init__(
        self,
        accounts: dict[str, WebDAVAccount],
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not accounts:
            raise ValueError("at least one WebDAV account is required")
        self._accounts = accounts
        self._connected: dict[str, httpx.AsyncClient] = {}
        self._client = http_client or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)

    async def connect(self, account: str) -> None:
        """Probe one account (OPTIONS; auth must resolve now - fail closed)."""
        if account not in self._accounts:
            raise ValueError(f"unknown account: {account!r}")
        entry = self._accounts[account]
        password = entry.resolve_password()
        try:
            resp = await self._client.request(
                "OPTIONS",
                entry.url,
                auth=(entry.username, password),
                headers={"Depth": "0"},
            )
        except TransportError as exc:
            raise BackendUnavailable(f"unreachable: {type(exc).__name__}") from exc
        if resp.status_code in (401, 403):
            raise AuthError("credentials refused")
        if resp.status_code >= _SERVER_ERROR_FLOOR:
            raise BackendUnavailable(f"server error {resp.status_code}")
        if resp.status_code >= _CLIENT_ERROR_FLOOR:
            raise ProtocolError(f"probe failed: HTTP {resp.status_code}")
        dav = resp.headers.get("DAV", "")
        if "1" not in dav.split(","):
            raise ProtocolError("server does not advertise class-1 WebDAV")
        self._connected[account] = self._client

    async def close(self) -> None:
        await self._client.aclose()

    def _client_for(self, account: str) -> tuple[WebDAVAccount, httpx.AsyncClient]:
        require_connected(account in self._connected)
        return self._accounts[account], self._connected[account]

    @staticmethod
    def _translate(resp: httpx.Response) -> BackendError | None:
        """Map one failed response to the typed hierarchy (None on success)."""
        if resp.is_success:
            return None
        if resp.status_code in (401, 403):
            return AuthError("credentials refused")
        if resp.status_code == _THROTTLED or resp.status_code >= _SERVER_ERROR_FLOOR:
            retry_after = resp.headers.get("Retry-After", "")
            detail = f"HTTP {resp.status_code}"
            if retry_after:
                detail += f" (Retry-After: {retry_after})"
            return BackendUnavailable(detail)
        return ProtocolError(f"HTTP {resp.status_code}")

    async def list(self, account: str, resource: str) -> list[NodeInfo]:
        entry, client = self._client_for(account)
        url = f"{entry.url}{_dav_path(resource)}"
        try:
            resp = await client.request("PROPFIND", url, headers=_READ_HEADERS, content=_XML_PROPF)
        except TransportError as exc:
            raise BackendUnavailable(f"unreachable: {type(exc).__name__}") from exc
        err = self._translate(resp)
        if err:
            raise err
        return _parse_propfind(resp.text)

    async def read(self, account: str, resource: str) -> bytes:
        entry, client = self._client_for(account)
        url = f"{entry.url}{_dav_path(resource)}"
        try:
            resp = await client.get(url)
        except TransportError as exc:
            raise BackendUnavailable(f"unreachable: {type(exc).__name__}") from exc
        err = self._translate(resp)
        if err:
            raise err
        return resp.content

    async def write(self, account: str, resource: str, content: bytes) -> None:
        entry, client = self._client_for(account)
        url = f"{entry.url}{_dav_path(resource)}"
        try:
            resp = await client.put(url, content=content)
        except TransportError as exc:
            raise BackendUnavailable(f"unreachable: {type(exc).__name__}") from exc
        err = self._translate(resp)
        if err:
            raise err

    async def move(self, account: str, src: str, dst: str) -> None:
        entry, client = self._client_for(account)
        url = f"{entry.url}{_dav_path(src)}"
        dest = f"{entry.url}{_dav_path(dst)}"
        try:
            resp = await client.request(
                "MOVE",
                url,
                headers={"Destination": _quote_url(dest), "Overwrite": "F"},
            )
        except TransportError as exc:
            raise BackendUnavailable(f"unreachable: {type(exc).__name__}") from exc
        err = self._translate(resp)
        if err:
            raise err

    async def trash(self, account: str, resource: str) -> None:
        entry, client = self._client_for(account)
        url = f"{entry.url}{_dav_path(resource)}"
        try:
            resp = await client.delete(url)
        except TransportError as exc:
            raise BackendUnavailable(f"unreachable: {type(exc).__name__}") from exc
        err = self._translate(resp)
        if err:
            raise err

    async def mkdir(self, account: str, resource: str) -> None:
        entry, client = self._client_for(account)
        url = f"{entry.url}{_dav_path(resource)}"
        try:
            resp = await client.request("MKCOL", url)
        except TransportError as exc:
            raise BackendUnavailable(f"unreachable: {type(exc).__name__}") from exc
        err = self._translate(resp)
        if err:
            raise err

    async def capabilities(self, account: str) -> dict[str, Any]:
        """Capability probing (R1): what this server supports; the boot
        guard decides the write policy from it."""
        entry, client = self._client_for(account)
        try:
            resp = await client.request("OPTIONS", entry.url, headers={"Depth": "0"})
        except TransportError as exc:
            raise BackendUnavailable(f"unreachable: {type(exc).__name__}") from exc
        err = self._translate(resp)
        if err:
            raise err
        dav = resp.headers.get("DAV", "")
        return {
            "dav_class1": "1" in dav.split(","),
            "dav_class2_locking": "2" in dav.split(","),
            "server": resp.headers.get("Server", ""),
        }


def _quote_url(url: str) -> str:
    """Percent-encode an absolute URL's path, preserving scheme/host."""
    scheme, sep, rest = url.partition("://")
    if not sep:
        return url
    host, _, path = rest.partition("/")
    return f"{scheme}://{host}{quote('/' + path, safe='/')}"


def _parse_propfind(xml: str) -> list[NodeInfo]:
    """Minimal multistatus parser: response hrefs + resourcetype/length/
    modified. stdlib re only; tolerant of namespace variants (R1)."""
    infos: list[NodeInfo] = []
    multistatus_re = r"<([A-Za-z0-9]+:)?response[ >].*?</([A-Za-z0-9]+:)?response>"
    for match in re.finditer(multistatus_re, xml, re.S):
        block = match.group(0)
        href_match = re.search(r"<([A-Za-z0-9]+:)?href>([^<]*)</", block)
        if href_match is None:
            continue
        href = href_match.group(2)
        name = href.rstrip("/").rsplit("/", 1)[-1]
        if not name:
            continue
        is_dir = bool(re.search(r"<([A-Za-z0-9]+:)?collection\s*/?>", block))
        size_match = re.search(r"<([A-Za-z0-9]+:)?getcontentlength>(\d+)<", block)
        mod_match = re.search(r"<([A-Za-z0-9]+:)?getlastmodified>([^<]*)<", block)
        infos.append(
            NodeInfo(
                name=name,
                is_dir=is_dir,
                size=int(size_match.group(2)) if size_match else None,
                modified=mod_match.group(2) if mod_match else None,
            )
        )
    return infos
