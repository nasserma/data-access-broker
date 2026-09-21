"""S5-1: the data broker's matrix approval-gateway adapter battery. The
adapter is an I/O shell over the core decision engine; the battery
drives it with a fake nio client (recorded shapes): wire shapes for the
transport, room event intake, sync-loop lifecycle, and start()/stop()
fail-closed. Pattern: communications broker's test_gateway_matrix.py.
"""

# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest
from access_broker_core.gateways import logic

from data_broker.gateways.matrix import MatrixGateway, MatrixTransport

ROOM_ID = "!approvals:example.org"


@dataclass
class FakeNioClient:
    """Recorded-shape nio client for the gateway adapter."""

    whoami_result: object = None
    join_result: object = None
    send_result: object = None
    sync_raises: bool = False
    calls: list = field(default_factory=list)

    async def whoami(self):
        self.calls.append("whoami")
        return self.whoami_result

    async def join(self, room_id):
        self.calls.append(("join", room_id))
        return self.join_result

    async def room_send(self, room_id=None, message_type=None, content=None):
        self.calls.append(("send", room_id, message_type, content))
        return self.send_result

    def add_event_callback(self, cb, types):
        self.calls.append(("cb", types))

    async def sync(self, timeout=None, sync_filter=None, since=None, full_state=None):
        if self.sync_raises:
            raise RuntimeError("sync blip")
        self.calls.append(("sync", sync_filter, since, full_state))
        await asyncio.sleep(0.01)
        return self.sync_result

    async def close(self):
        self.calls.append("close")


@dataclass
class OkWhoami:
    user_id: str = "@approvals:example.org"


@dataclass
class OkJoin:
    room_id: str = ROOM_ID


@dataclass
class OkSend:
    event_id: str = "$evt-1"


@dataclass
class NoIdSend:
    event_id: None = None


class RecordingCore:
    """Stands in for ApprovalGatewayCore; records canonical events."""

    def __init__(self) -> None:
        self.reactions: list[tuple[str, str, str]] = []
        self.replies: list[tuple[str, str]] = []

    async def handle_reaction(self, sender: str, event_id: str, emoji: str) -> None:
        self.reactions.append((sender, event_id, emoji))

    async def handle_reply(self, sender: str, text: str) -> None:
        self.replies.append((sender, text))


def _gateway(client=None, core=None) -> MatrixGateway:
    """Build the gateway and inject the fake client (bypassing nio
    AsyncClient construction, the comms pattern)."""
    gw = object.__new__(MatrixGateway)
    client = client or FakeNioClient(whoami_result=OkWhoami(), join_result=OkJoin())
    gw._core = core or RecordingCore()  # noqa: SLF001 - test wiring
    gw._room_id = ROOM_ID  # noqa: SLF001 - test wiring
    gw._transport = MatrixTransport(None, ROOM_ID)  # noqa: SLF001 - test wiring
    gw._transport._client = client  # noqa: SLF001 - test wiring
    gw._client = client  # noqa: SLF001 - test wiring
    gw._running = False  # noqa: SLF001 - test wiring
    gw.retry_delay = 0.01  # noqa: SLF001 - test wiring
    return gw


# ---------------------------------------------------------------- transport


async def test_transport_send_returns_event_id() -> None:
    client = FakeNioClient(send_result=OkSend())
    t = MatrixTransport(client, ROOM_ID)
    assert await t.send_message("approve #3") == "$evt-1"
    assert client.calls[0] == ("send", ROOM_ID, "m.room.message", {"msgtype": "m.text", "body": "approve #3"})


async def test_transport_send_no_event_id_raises() -> None:
    client = FakeNioClient(send_result=NoIdSend())
    t = MatrixTransport(client, ROOM_ID)
    with pytest.raises(logic.TransportError, match="send failed"):
        await t.send_message("x")


async def test_transport_reaction_wire_shape() -> None:
    client = FakeNioClient(send_result=OkSend())
    t = MatrixTransport(client, ROOM_ID)
    await t.add_reaction("$evt-9", "✅")
    kind, rid, mtype, content = client.calls[0]
    assert (rid, mtype) == (ROOM_ID, "m.reaction")
    assert content["m.relates_to"]["event_id"] == "$evt-9"
    assert content["m.relates_to"]["key"] == "✅"


async def test_transport_reaction_no_event_id_raises() -> None:
    client = FakeNioClient(send_result=NoIdSend())
    t = MatrixTransport(client, ROOM_ID)
    with pytest.raises(logic.TransportError, match="reaction failed"):
        await t.add_reaction("$e", "x")


# ------------------------------------------------------------------ intake


async def test_reply_reaches_core() -> None:
    core = RecordingCore()
    gw = _gateway(core=core)

    class Room:
        room_id = ROOM_ID

    class Ev:
        body = "approve 7"
        sender = "@owner:example.org"

    await gw._on_message(Room(), Ev())
    assert core.replies == [("@owner:example.org", "approve 7")]


async def test_foreign_room_filtered() -> None:
    core = RecordingCore()
    gw = _gateway(core=core)

    class Room:
        room_id = "!other:example.org"

    class Ev:
        body = "approve 7"
        sender = "@owner:example.org"

    await gw._on_message(Room(), Ev())
    await gw._on_reaction(Room(), Ev())
    assert core.replies == [] and core.reactions == []


async def test_reaction_reaches_core() -> None:
    core = RecordingCore()
    gw = _gateway(core=core)

    class Room:
        room_id = ROOM_ID

    class Ev:
        sender = "@owner:example.org"
        source = {"content": {"m.relates_to": {"key": "✅", "event_id": "$e1"}}}

    await gw._on_reaction(Room(), Ev())
    assert core.reactions == [("@owner:example.org", "$e1", "✅")]


async def test_reaction_without_key_or_target_no_core_call() -> None:
    """A reaction event missing key or target never reaches the core
    (the malformed-relation guard)."""
    core = RecordingCore()
    gw = _gateway(core=core)

    class Room:
        room_id = ROOM_ID

    class Ev:
        sender = "@owner:example.org"
        source = {"content": {"m.relates_to": {"key": "✅"}}}  # no event_id

    await gw._on_reaction(Room(), Ev())
    assert core.reactions == []


async def test_empty_body_no_core_call() -> None:
    core = RecordingCore()
    gw = _gateway(core=core)

    class Room:
        room_id = ROOM_ID

    class Ev:
        body = ""
        sender = "@owner:example.org"

    await gw._on_message(Room(), Ev())
    assert core.replies == []


# ------------------------------------------------------------- construction


def test_construction_wires_nio_client() -> None:
    """__init__ builds the nio AsyncClient, sets the token, and hands
    the client to the transport (recorded fake in place of nio)."""
    import data_broker.gateways.matrix as mod

    class FakeAsyncClient:
        def __init__(self, homeserver: str, user: str) -> None:
            self.homeserver = homeserver
            self.user = user
            self.access_token = None

    real_client = mod.AsyncClient
    try:
        mod.AsyncClient = FakeAsyncClient
        core = RecordingCore()
        gw = MatrixGateway(
            core=core,
            homeserver_url="https://matrix.example.org",
            user_id="@approvals:example.org",
            access_token="t" * 20,
            room_id=ROOM_ID,
        )
        assert isinstance(gw._client, FakeAsyncClient)  # noqa: SLF001 - wiring assertion
        assert gw._client.access_token == "t" * 20  # noqa: SLF001 - wiring assertion
        assert gw._transport._client is gw._client  # noqa: SLF001 - wiring assertion
        assert gw._room_id == ROOM_ID  # noqa: SLF001 - wiring assertion
        assert gw._running is False  # noqa: SLF001 - wiring assertion
    finally:
        mod.AsyncClient = real_client


def test_builder_constructs_gateway() -> None:
    """build_matrix_adapter: (core, fields, approver) -> (adapter,
    surface_id) with the fields threaded (recorded fake in place of nio)."""
    import data_broker.gateways.matrix as mod
    from data_broker.gateways import build_matrix_adapter

    class FakeAsyncClient:
        def __init__(self, homeserver: str, user: str) -> None:
            self.access_token = None

    real_client = mod.AsyncClient
    try:
        mod.AsyncClient = FakeAsyncClient
        core = RecordingCore()
        fields = {
            "homeserver_url": "https://matrix.example.org",
            "user_id": "@approvals:example.org",
            "access_token": "t" * 20,
            "room_id": ROOM_ID,
        }
        adapter, surface = build_matrix_adapter(core, fields, "@owner:example.org")
        assert surface == ROOM_ID
        assert adapter._core is core  # noqa: SLF001 - wiring assertion
        assert adapter._transport._client.access_token == "t" * 20  # noqa: SLF001
    finally:
        mod.AsyncClient = real_client


# --------------------------------------------------------------- lifecycle


async def test_start_registers_callbacks_and_enters_sync_loop() -> None:
    client = FakeNioClient(whoami_result=OkWhoami(), join_result=OkJoin())
    gw = _gateway(client=client)
    await gw.start()
    assert ("cb", ()) in client.calls or any(c[0] == "cb" for c in client.calls)
    await asyncio.sleep(0.02)
    await gw.stop()
    assert "close" in client.calls


async def test_start_fail_closed_bad_auth() -> None:
    client = FakeNioClient(whoami_result=object(), join_result=OkJoin())
    gw = _gateway(client=client)
    with pytest.raises(RuntimeError, match="auth failed"):
        await gw.start()


async def test_start_fail_closed_unjoinable_room() -> None:
    @dataclass
    class ErrJoin:
        status_code: int = 403
        message: str = "forbidden"

    client = FakeNioClient(whoami_result=OkWhoami(), join_result=ErrJoin())
    gw = _gateway(client=client)
    with pytest.raises(RuntimeError, match="cannot join"):
        await gw.start()


async def test_start_fail_closed_unjoinable_room_no_room_attr() -> None:
    """A join response with NO room_id and NO error marker is also the
    refusal (the second duck-typed arm: the verdict is undecidable ->
    fail closed)."""

    @dataclass
    class BareJoin:
        pass  # neither room_id nor status_code

    client = FakeNioClient(whoami_result=OkWhoami(), join_result=BareJoin())
    gw = _gateway(client=client)
    with pytest.raises(RuntimeError, match="cannot join"):
        await gw.start()


async def test_stop_before_start_closes_cleanly() -> None:
    """stop() with no sync task (never started) still closes the client
    (the task-is-None arm)."""
    client = FakeNioClient(whoami_result=OkWhoami(), join_result=OkJoin())
    gw = _gateway(client=client)
    await gw.stop()
    assert "close" in client.calls
    assert not gw._running


async def test_sync_loop_exits_on_stop_mid_sync() -> None:
    """stop() during an in-flight sync: the loop exits after the sync
    returns (the while-loop exit branch), then closes."""
    client = FakeNioClient(whoami_result=OkWhoami(), join_result=OkJoin())
    gw = _gateway(client=client)
    await gw.start()
    # let one sync complete, then flip _running via stop() while the
    # loop is in its sleep-retry path is already covered; here the
    # loop's clean exit (running False -> loop ends) is asserted.
    await asyncio.sleep(0.02)
    await gw.stop()
    await asyncio.sleep(0.02)
    assert not gw._running
    assert "close" in client.calls


async def test_sync_loop_exits_when_stop_lands_mid_sync() -> None:
    """The strict 140->exit branch: stop() flips _running WHILE a slow
    sync is awaited; the loop then re-checks the condition and exits
    without another iteration."""
    import dataclasses

    @dataclasses.dataclass
    class SlowClient(FakeNioClient):
        release: asyncio.Event = dataclasses.field(default_factory=asyncio.Event)

        async def sync(self, timeout=None):
            # hold the loop inside one sync until stop() has flipped the flag
            await self.release.wait()

    client = SlowClient(whoami_result=OkWhoami(), join_result=OkJoin())
    gw = _gateway(client=client)
    await gw.start()
    await asyncio.sleep(0.02)  # the loop is now parked inside sync()
    stop_task = asyncio.ensure_future(gw.stop())  # flips _running, cancels task
    await asyncio.sleep(0.02)  # let stop() run while sync is in-flight
    client.release.set()  # the in-flight sync returns
    await stop_task
    await asyncio.sleep(0.02)
    assert not gw._running
    assert "close" in client.calls


async def test_sync_loop_exits_when_stopped() -> None:
    """The sync loop exits when _running flips False (the 140->exit
    branch: the loop observes the flag after the sync returns)."""
    client = FakeNioClient(whoami_result=OkWhoami(), join_result=OkJoin())
    gw = _gateway(client=client)
    await gw.start()
    task = gw._task
    gw._running = False  # the loop observes the flag after the sync returns
    await asyncio.sleep(0.2)
    assert task.done()


async def test_sync_failure_survives() -> None:
    """A sync exception is swallowed and retried; the loop stays alive."""
    client = FakeNioClient(whoami_result=OkWhoami(), join_result=OkJoin(), sync_raises=True)
    gw = _gateway(client=client)
    await gw.start()
    await asyncio.sleep(0.05)
    assert gw._running
    await gw.stop()
    assert not gw._running


# ------------------------------------------------------------- registration


def test_builder_registers_with_core_registry() -> None:
    """register_gateway_adapters() registers the matrix builder on the
    core registry; re-registration replaces (idempotent)."""
    import access_broker_core.gateways as gw_mod

    from data_broker.gateways import build_matrix_adapter, register_gateway_adapters

    register_gateway_adapters()
    assert "matrix" in gw_mod.supported_adapters()
    register_gateway_adapters()
    assert "matrix" in gw_mod.supported_adapters()
    # cleanup: remove so other suites in-process are unaffected
    gw_mod._ADAPTER_BUILDERS.pop("matrix", None)
    assert build_matrix_adapter is not None


@dataclass
class OkSync:
    next_batch: str = "s1"


# ------------------------------------------------- D6g-parity: no history replay


async def test_sync_loop_skips_history_then_tracks_since() -> None:
    """The first sync carries the timeline-limit-0 filter (history
    never dispatched); later syncs carry the since token (only
    post-start events). Found live 2026-09-20: production room history
    replayed into handle_reply on the first boot."""
    core = RecordingCore()
    gw = MatrixGateway(
        core=core,
        homeserver_url="https://matrix.example.org",
        user_id="@approvals:example.org",
        access_token="tok",
        room_id=ROOM_ID,
    )
    fake = FakeNioClient(whoami_result=OkWhoami(), join_result=OkJoin(),
                         send_result=OkSend())
    fake.sync_result = OkSync(next_batch="tok-1")
    gw._client = fake  # noqa: SLF001 - test wiring
    task = asyncio.ensure_future(gw.start())
    await asyncio.sleep(0.05)
    gw._running = False  # flip the flag directly (stop() cancels the task)
    await asyncio.wait_for(task, timeout=2)
    syncs = [c for c in fake.calls if isinstance(c, tuple) and c[0] == "sync"]
    assert len(syncs) >= 2
    first = syncs[0]
    assert first[1] is not None and first[1]["room"]["rooms"] == [ROOM_ID]  # the filter
    assert first[1]["room"]["timeline"]["limit"] == 0
    assert first[2] is None  # first sync: no since token
    second = syncs[1]
    assert second[1] is None  # no filter on later syncs
    assert second[2] == "tok-1"  # since token carried
