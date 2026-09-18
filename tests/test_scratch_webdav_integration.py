"""Marked real-protocol integration tests (S4-3): scratch WSGIDAV.

Skipped unless the scratch server is up (bring up with
`./scratch_servers/webdav_scratch.py up`): the suite's
scratch-Dendrite/scratch-HA pattern - marked, non-blocking, runnable
in a separate session.

Idempotency: every path used here is derived from a run-unique suffix,
so re-runs never collide with leftover state (RFC 4918 MKCOL refuses
an existing collection with 405 - correct server behavior, poison for
a naive test).
"""

# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import socket
import time

import pytest

from data_broker.backends.webdav import WebDAVAccount, WebDAVBackend

HOST = "127.0.0.1"
PORT = 8466
URL = f"http://{HOST}:{PORT}"

pytestmark = [pytest.mark.integration]


def scratch_up() -> bool:
    try:
        with socket.create_connection((HOST, PORT), timeout=0.3):
            return True
    except OSError:
        return False


requires_scratch = pytest.mark.skipif(not scratch_up(), reason="scratch WebDAV not up")


def make_backend() -> WebDAVBackend:
    account = WebDAVAccount(
        name="scratch", url=URL, username="anonymous", password_env="WEBDAV_SCRATCH_PASSWORD"
    )
    return WebDAVBackend({"scratch": account})


def _run_tag() -> str:
    return str(int(time.time() * 1000))


@requires_scratch
def test_probe_and_capabilities() -> None:
    import asyncio

    async def run() -> None:
        backend = make_backend()
        await backend.connect("scratch")
        try:
            caps = await backend.capabilities("scratch")
            assert caps["dav_class1"]
        finally:
            await backend.close()

    asyncio.run(run())


@requires_scratch
def test_full_content_cycle() -> None:
    """write -> list -> read -> move -> trash -> mkdir against the real
    server, under a run-unique tag (idempotent re-runs)."""
    import asyncio

    tag = _run_tag()
    work = f"Docs/run-{tag}"

    async def run() -> None:
        backend = make_backend()
        await backend.connect("scratch")
        try:
            await backend.mkdir("scratch", work)
            entries = await backend.list("scratch", "Docs")
            assert any(e.name == f"run-{tag}" and e.is_dir for e in entries)

            await backend.write("scratch", f"{work}/cycle.txt", b"hello scratch\n")
            entries = await backend.list("scratch", work)
            assert any(e.name == "cycle.txt" and not e.is_dir for e in entries)

            content = await backend.read("scratch", f"{work}/cycle.txt")
            assert content == b"hello scratch\n"

            await backend.move("scratch", f"{work}/cycle.txt", f"{work}/cycle-moved.txt")
            entries = await backend.list("scratch", work)
            names = {e.name for e in entries}
            assert "cycle-moved.txt" in names
            assert "cycle.txt" not in names

            await backend.trash("scratch", f"{work}/cycle-moved.txt")
            entries = await backend.list("scratch", work)
            assert all(e.name != "cycle-moved.txt" for e in entries)

            await backend.mkdir("scratch", f"{work}/sub")
            entries = await backend.list("scratch", work)
            assert any(e.name == "sub" and e.is_dir for e in entries)
        finally:
            await backend.close()

    asyncio.run(run())
