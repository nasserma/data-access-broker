"""OneDrive backend (S4-4): MS Graph drives over httpx + StaticTokenProvider.

Per the goal contract section 3: MS Graph drives via the msal/msgraph
stack validated in the groupware build; Graph drive resources here are
a DISTINCT family from the groupware broker's Graph mail/calendar
family (no shared token scope between brokers). Live-tenant wiring is
a deployment-session task; production installs an msal-backed token
provider at config time (the groupware TokenProvider boundary), tests
substitute the static provider with no network.

Talks raw Graph REST over httpx (native async):

    GET    /me/drive/root/children            -> list (root)
    GET    /me/drive/root:/<path>:/children   -> list (subfolder)
    GET    /me/drive/root:/<path>:/content    -> read
    PUT    /me/drive/root:/<path>:/content    -> write
    PATCH  /me/drive/root:/<path>             -> move (parentReference +
             name; OneDrive move = reparent)
    DELETE /me/drive/root:/<path>             -> trash (recycle bin
               preserves the recoverable case)
    POST   /me/drive/root/children            -> mkdir (folder facet)
    GET    /me/drive                          -> capabilities (probe)

Error translation (the groupware pattern): 401/403 -> AuthError;
429 -> BackendUnavailable (Retry-After); 5xx/connection ->
BackendUnavailable; everything else -> ProtocolError. No token or
credential ever appears in an error message.
"""

# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import os
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

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
DEFAULT_TIMEOUT = 30.0
_SERVER_ERROR_FLOOR = 500
_THROTTLED = 429

#: Drive resource scope family (distinct from the groupware broker's
#: Graph mail/calendar scopes; no shared token between brokers).
READ_SCOPES = ("Files.Read", "User.Read")
WRITE_SCOPES = ("Files.ReadWrite",)


class GraphAccount:
    """One validated MS Graph account configuration entry.

    ``tenant_id``/``client_id`` select the Entra app registration.
    ``allow_write`` gates the write scope set (read-only is the v1
    default posture, the groupware R6 scope-allowlist shape).
    """

    def __init__(
        self,
        name: str,
        tenant_id: str,
        client_id: str,
        token_env: str,
        authority: str = "https://login.microsoftonline.com",
        allow_write: bool = False,
    ) -> None:
        if not tenant_id:
            raise ValueError(f"account {name!r}: tenant_id is required")
        if not client_id:
            raise ValueError(f"account {name!r}: client_id is required")
        if not token_env:
            raise ValueError(f"account {name!r}: token_env is required")
        if not authority.startswith("https://"):
            raise ValueError(f"account {name!r}: authority must be an https URL")
        self.name = name
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.token_env = token_env
        self.authority = authority
        self.allow_write = allow_write

    @property
    def scopes(self) -> tuple[str, ...]:
        """Scope allowlist: read set, plus write set only when gated."""
        return READ_SCOPES + (WRITE_SCOPES if self.allow_write else ())


class TokenProvider:
    """Thin injectable boundary around MSAL token acquisition.

    Production wiring (a deployment-session task) installs a provider
    that drives the MSAL device-code flow with a per-account token
    cache; the backend itself never imports msal, so tests substitute
    the static provider with no network.
    """

    def get_token(self, account_name: str) -> str:
        raise NotImplementedError("install a production provider at config time")


class StaticTokenProvider(TokenProvider):
    """Returns a fixed token (tests and scratch; no network, no msal)."""

    def __init__(self, token: str = "test-token") -> None:
        self.token = token
        self.calls: list[str] = []

    def get_token(self, account_name: str) -> str:
        self.calls.append(account_name)
        return self.token


class GraphDriveBackend:
    """MS Graph drives adapter over the same store backend Protocol.

    One instance per process; one HTTP client shared across accounts
    (the groupware pattern). Drive node paths travel as
    ``/me/drive/root:/<encoded path>:/`` URL shapes; the wall's
    normalized components are re-encoded defensively here.
    """

    def __init__(
        self,
        accounts: dict[str, GraphAccount],
        token_provider: TokenProvider,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not accounts:
            raise ValueError("at least one Graph account is required")
        self._accounts = accounts
        self._token_provider = token_provider
        self._client = http_client or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
        self._connected: set[str] = set()

    async def connect(self, account: str) -> None:
        """Resolve the token now (fail-closed); a probe verifies the API."""
        if account not in self._accounts:
            raise ValueError(f"unknown account: {account!r}")
        entry = self._accounts[account]
        if not os.environ.get(entry.token_env):
            raise AuthError(f"environment variable {entry.token_env!r} is not set")
        try:
            resp = await self._client.get(
                f"{GRAPH_BASE}/me/drive",
                headers=self._headers(account),
            )
        except TransportError as exc:
            raise BackendUnavailable(f"unreachable: {type(exc).__name__}") from exc
        err = _translate(resp)
        if err:
            raise err
        self._connected.add(account)

    async def close(self) -> None:
        await self._client.aclose()

    def _headers(self, account: str) -> dict[str, str]:
        token = self._token_provider.get_token(account)
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }

    def _client_for(self, account: str) -> GraphAccount:
        require_connected(account in self._connected)
        return self._accounts[account]

    async def list(self, account: str, resource: str) -> list[NodeInfo]:
        self._client_for(account)
        url = _children_url(resource)
        try:
            resp = await self._client.get(url, headers=self._headers(account))
        except TransportError as exc:
            raise BackendUnavailable(f"unreachable: {type(exc).__name__}") from exc
        err = _translate(resp)
        if err:
            raise err
        return _parse_children(resp.json())

    async def read(self, account: str, resource: str) -> bytes:
        self._client_for(account)
        url = f"{GRAPH_BASE}/me/drive/root:/{_enc(resource)}:/content"
        try:
            resp = await self._client.get(url, headers=self._headers(account))
        except TransportError as exc:
            raise BackendUnavailable(f"unreachable: {type(exc).__name__}") from exc
        err = _translate(resp)
        if err:
            raise err
        return resp.content

    async def write(self, account: str, resource: str, content: bytes) -> None:
        self._client_for(account)
        url = f"{GRAPH_BASE}/me/drive/root:/{_enc(resource)}:/content"
        try:
            resp = await self._client.put(url, headers=self._headers(account), content=content)
        except TransportError as exc:
            raise BackendUnavailable(f"unreachable: {type(exc).__name__}") from exc
        err = _translate(resp)
        if err:
            raise err

    async def move(self, account: str, src: str, dst: str) -> None:
        self._client_for(account)
        parent, _, name = dst.rpartition("/")
        body: dict[str, object] = {"name": name}
        if parent:
            body["parentReference"] = {"path": f"/drive/root:/{_enc(parent)}"}
        url = f"{GRAPH_BASE}/me/drive/root:/{_enc(src)}"
        try:
            resp = await self._client.patch(url, headers=self._headers(account), json=body)
        except TransportError as exc:
            raise BackendUnavailable(f"unreachable: {type(exc).__name__}") from exc
        err = _translate(resp)
        if err:
            raise err

    async def trash(self, account: str, resource: str) -> None:
        self._client_for(account)
        url = f"{GRAPH_BASE}/me/drive/root:/{_enc(resource)}"
        try:
            resp = await self._client.delete(url, headers=self._headers(account))
        except TransportError as exc:
            raise BackendUnavailable(f"unreachable: {type(exc).__name__}") from exc
        err = _translate(resp)
        if err:
            raise err

    async def mkdir(self, account: str, resource: str) -> None:
        self._client_for(account)
        parent, _, name = resource.rpartition("/")
        url = f"{GRAPH_BASE}/me/drive/root/children"
        body: dict[str, object] = {
            "name": name,
            "folder": {},
            "@microsoft.graph.conflictBehavior": "fail",
        }
        if parent:
            url = f"{GRAPH_BASE}/me/drive/root:/{_enc(parent)}:/children"
        try:
            resp = await self._client.post(url, headers=self._headers(account), json=body)
        except TransportError as exc:
            raise BackendUnavailable(f"unreachable: {type(exc).__name__}") from exc
        err = _translate(resp)
        if err:
            raise err

    async def capabilities(self, account: str) -> dict[str, object]:
        """Capability probe: the drive entry names the API and quota state."""
        self._client_for(account)
        try:
            resp = await self._client.get(f"{GRAPH_BASE}/me/drive", headers=self._headers(account))
        except TransportError as exc:
            raise BackendUnavailable(f"unreachable: {type(exc).__name__}") from exc
        err = _translate(resp)
        if err:
            raise err
        body = resp.json()
        quota = body.get("quota") or {}
        return {
            "drive_id": body.get("id"),
            "drive_type": body.get("driveType"),
            "quota_total": quota.get("total"),
            "remaining": quota.get("remaining"),
        }


def _enc(resource: str) -> str:
    """Percent-encode a node path for a Graph URL (defensive re-derivation;
    the components come from the wall's normalizer)."""
    return "/".join(quote(c, safe="") for c in resource.split("/") if c)


def _children_url(resource: str) -> str:
    if not resource:
        return f"{GRAPH_BASE}/me/drive/root/children"
    return f"{GRAPH_BASE}/me/drive/root:/{_enc(resource)}:/children"


def _translate(resp: httpx.Response) -> BackendError | None:
    """Map one failed Graph response to the typed hierarchy (None on
    success); error bodies never surface their message text."""
    if resp.is_success:
        return None
    if resp.status_code in (401, 403):
        return AuthError("credentials refused")
    if resp.status_code == _THROTTLED or resp.status_code >= _SERVER_ERROR_FLOOR:
        retry_after = resp.headers.get("Retry-After", "")
        detail = f"Graph {resp.status_code}"
        if retry_after:
            detail += f" (Retry-After: {retry_after})"
        return BackendUnavailable(detail)
    return ProtocolError(f"Graph {resp.status_code}")


_SERVER_ERROR_FLOOR = 500


def _parse_children(body: dict) -> list[NodeInfo]:
    """Graph driveItem list -> node infos (domain mapping only here)."""
    infos: list[NodeInfo] = []
    for item in body.get("value") or []:
        name = str(item.get("name") or "")
        if not name:
            continue
        is_dir = "folder" in item
        infos.append(
            NodeInfo(
                name=name,
                is_dir=is_dir,
                size=item.get("size"),
                modified=item.get("lastModifiedDateTime"),
            )
        )
    return infos
