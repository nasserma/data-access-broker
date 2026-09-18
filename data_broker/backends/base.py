"""Backend interface: store/node/content facets behind one domain model.

Per the goal contract section 4: backends sit behind one domain model.
A store is one named account's file namespace; a node is a normalized
store node (the wall's path component tuple); content is bytes. Every
backend implements the same five content verbs (the nextcloud layer's
five verbs, generalized) plus capability probing at boot (R1: generic
WebDAV drift degrades gracefully, fail-closed on unversioned stores).

Errors are the typed backend hierarchy, never raw library exceptions:
BackendError base; NotConnected before connect(); AuthError on 401/403;
BackendUnavailable on 429/5xx/connection failures; ProtocolError on
anything else unexpected. No traceback crosses this boundary.
"""

# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


class BackendError(Exception):
    """Backend failure (connection, auth, protocol, timeout)."""


class NotConnected(BackendError):
    """An operation was attempted before connect()."""


class AuthError(BackendError):
    """Authentication/authorization failure (401/403)."""


class BackendUnavailable(BackendError):
    """The backend is unreachable or throttled (429/5xx/network)."""


class ProtocolError(BackendError):
    """An unmapped upstream failure (the safe default translation)."""


@dataclass(frozen=True)
class NodeInfo:
    """One listed node: name, kind, size, modified (backend-shaped)."""

    name: str
    is_dir: bool
    size: int | None
    modified: str | None


@runtime_checkable
class StoreBackend(Protocol):
    """The one domain model every adapter implements.

    All methods are native async (the wall judges before any of them
    runs; the gate owns ordering, the backend owns transport).
    """

    def connect(self, account: str) -> Awaitable[None]: ...

    async def close(self) -> None: ...

    async def list(self, account: str, resource: str) -> list[NodeInfo]: ...

    async def read(self, account: str, resource: str) -> bytes: ...

    async def write(self, account: str, resource: str, content: bytes) -> None: ...

    async def move(self, account: str, src: str, dst: str) -> None: ...

    async def trash(self, account: str, resource: str) -> None: ...

    async def mkdir(self, account: str, resource: str) -> None: ...

    async def capabilities(self, account: str) -> dict[str, Any]: ...


def require_connected(connected: bool) -> None:
    """Fail-closed guard: operations before connect() are refused."""
    if not connected:
        raise NotConnected("backend not connected; call connect() first")
