"""Gateway adapters: platform shells over the core decision engine.

The core (access_broker_core.gateways) owns ALL decision semantics;
this package is the thin I/O shell for the data broker's approval
plane. Platform adapters register their builders with the core adapter
registry at boot time (register_gateway_adapters), on the
groupware/communications pattern.

The matrix adapter is the v1 approval surface: a DEDICATED approval
account's access token (never a brokered account's identity, and never
a D5 surface token).
"""

# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

try:  # matrix-nio is an optional runtime dep
    from nio import AsyncClient, JoinResponse, ReactionEvent, RoomMessageText
except ImportError:  # pragma: no cover - exercised only without the extra
    AsyncClient = None  # type: ignore[assignment]
    ReactionEvent = None  # type: ignore[assignment]
    RoomMessageText = None  # type: ignore[assignment]
    JoinResponse = None  # type: ignore[assignment]

from access_broker_core.gateways import register_adapter  # noqa: E402


def build_matrix_adapter(core, fields: dict, approver: str):
    """Builder registered under 'matrix': (core, fields, approver) ->
    (adapter, surface_id). fields carry the resolved env values
    (homeserver_url, user_id, access_token, room_id)."""
    from data_broker.gateways.matrix import MatrixGateway  # noqa: PLC0415

    adapter = MatrixGateway(
        core=core,
        homeserver_url=fields["homeserver_url"],
        user_id=fields["user_id"],
        access_token=fields["access_token"],
        room_id=fields["room_id"],
    )
    return adapter, fields["room_id"]


def register_gateway_adapters() -> None:
    """Register this broker's adapter builders with the core registry
    (idempotent: re-registration replaces, per the core contract)."""
    register_adapter("matrix", build_matrix_adapter)
