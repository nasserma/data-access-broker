"""Matrix approval adapter for the data broker's approval plane.

The core (access_broker_core.gateways.logic) owns ALL decision
semantics; this module is the thin I/O shell: send messages, pre-place
reactions, and feed room events (message reactions + typed replies)
into core.handle_reaction / core.handle_reply.

Identity contract: the allowlisted approver is one Matrix user id;
handle_reaction/handle_reply receive the raw mxid and the core
allowlist-checks. Events from other rooms are filtered by room_id
before they ever reach the core. Fail-closed posture: any nio callback
exception is logged and swallowed (the room must not crash the server
process); decisions already committed to the store are never re-applied
by re-delivery because request numbers are one-time (store CAS).

This adapter runs on the DEDICATED APPROVAL ACCOUNT (its own access
token, its own identity) — never the brokered account, and never a
surface token of the D5 two-token model.
"""

# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import asyncio
import logging

from access_broker_core.gateways import logic
from access_broker_core.gateways.logic import ApprovalGatewayCore, GatewayTransport

try:  # matrix-nio is an optional runtime dep (imported lazily at use)
    from nio import AsyncClient, JoinResponse, ReactionEvent, RoomMessageText
except ImportError:  # pragma: no cover - exercised only without the extra
    AsyncClient = None  # type: ignore[assignment]
    ReactionEvent = None  # type: ignore[assignment]
    RoomMessageText = None  # type: ignore[assignment]
    JoinResponse = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


class MatrixTransport(GatewayTransport):
    """GatewayTransport over a matrix-nio AsyncClient (single room)."""

    def __init__(self, client, room_id: str) -> None:
        self._client = client
        self._room_id = room_id

    async def send_message(self, text: str) -> str:
        """Send one room message (m.text); returns the event id or
        raises TransportError on nio failure."""
        response = await self._client.room_send(
            room_id=self._room_id,
            message_type="m.room.message",
            content={"msgtype": "m.text", "body": text},
        )
        if getattr(response, "event_id", None) is None:
            raise logic.TransportError(f"Matrix send failed: {response}")
        return str(response.event_id)

    async def add_reaction(self, event_id: str, emoji: str) -> None:
        """Pre-place one m.reaction annotation on a posted message (best-effort)."""
        content = {
            "m.relates_to": {
                "rel_type": "m.annotation",
                "event_id": event_id,
                "key": emoji,
            }
        }
        response = await self._client.room_send(
            room_id=self._room_id, message_type="m.reaction", content=content
        )
        if getattr(response, "event_id", None) is None:
            raise logic.TransportError(f"Matrix reaction failed: {response}")


class MatrixGateway:
    """Adapter wiring matrix-nio callbacks to the shared core.

    start()/stop() manage the nio sync loop as an asyncio task in the
    server process (single-process architecture). Construction
    validates the nio import at call time: a deployment without
    matrix-nio installed fails at config validation, never mid-request.
    """

    def __init__(
        self,
        core: ApprovalGatewayCore,
        homeserver_url: str,
        user_id: str,
        access_token: str,
        room_id: str,
    ) -> None:
        if AsyncClient is None:  # pragma: no cover
            raise RuntimeError("matrix-nio is not installed (gateway dep)")
        self._core = core
        self._room_id = room_id
        self._transport = MatrixTransport(None, room_id)  # client set at start
        self._client = AsyncClient(homeserver_url, user_id)
        self._client.access_token = access_token
        self._transport._client = self._client  # noqa: SLF001 - construction wiring
        self._running = False
        self.retry_delay = 5.0

    async def start(self) -> None:
        """Verify the room is reachable, register callbacks, enter the
        sync loop (background task). Fail-closed: an unreachable
        homeserver or unjoinable room stops startup."""
        whoami = await self._client.whoami()
        if getattr(whoami, "user_id", None) is None:
            raise RuntimeError(f"Matrix auth failed: {whoami}")
        join = await self._client.join(self._room_id)
        # Duck-typed verdicts (nio success shapes carry room_id; error
        # shapes carry status_code/message): a join WITHOUT a room_id
        # and WITH an error marker is the unjoinable-room refusal.
        joined = getattr(join, "room_id", None)
        if not joined and getattr(join, "status_code", None) is not None:
            raise RuntimeError(f"cannot join approval room: {join}")
        if joined is None and not hasattr(join, "room_id"):
            raise RuntimeError(f"cannot join approval room: {join}")
        self._client.add_event_callback(self._on_message, (RoomMessageText,))
        self._client.add_event_callback(self._on_reaction, (ReactionEvent,))
        self._running = True
        self._task = asyncio.create_task(self._sync_forever())
        logger.info("matrix gateway started (room=%s)", self._room_id)

    async def stop(self) -> None:
        """Cancel the sync-loop task and close the nio client."""
        self._running = False
        task = getattr(self, "_task", None)
        if task is not None:
            task.cancel()
        await self._client.close()
        logger.info("matrix gateway stopped")

    async def _sync_forever(self) -> None:
        # _running is set True by start() before this task is created;
        # stop() flips it False and the loop exits after the in-flight
        # sync returns (the 141->exit branch).
        while self._running:
            try:
                await self._client.sync(timeout=30000)
            except Exception:  # noqa: BLE001 - the room must not kill the process
                logger.exception("matrix sync failed; retrying")
                await asyncio.sleep(self.retry_delay)

    async def _on_message(self, room, event) -> None:  # noqa: ARG002 - nio signature
        if room.room_id != self._room_id:
            return
        text = getattr(event, "body", "") or ""
        sender = getattr(event, "sender", "") or ""
        if text:
            await self._core.handle_reply(sender, text)

    async def _on_reaction(self, room, event) -> None:  # noqa: ARG002 - nio signature
        if room.room_id != self._room_id:
            return
        sender = getattr(event, "sender", "") or ""
        source = getattr(event, "source", None) or {}
        rel = (source.get("content") or {}).get("m.relates_to", {}) or {}
        key = rel.get("key") or ""
        target = rel.get("event_id") or ""
        if key and target:
            await self._core.handle_reaction(sender, target, key)
