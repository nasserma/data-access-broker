"""Scratch WebDAV container (S4-3): the throwaway real-protocol target.

The suite's scratch-server pattern (Dovecot/Radicale/Dendrite/
scratch-HA): WSGIDAV bound to 127.0.0.1:8466, anonymous auth, a
throwaway share under /tmp. Bring up with
`./scratch_servers/webdav_scratch.py up`, run the marked integration
tests, tear down with `... down`. LAN-local only; no data leaves the
machine.
"""

# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import contextlib
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

HOST = "127.0.0.1"
PORT = 8466
SCRATCH_ROOT = Path("/tmp/dataBroker-scratch-webdav")
PID_FILE = Path("/tmp/dataBroker-scratch-webdav.pid")
LOG_FILE = Path("/tmp/dataBroker-scratch-webdav.log")
_READY_PROBES = 50


def _server_cmd() -> list[str]:
    return [
        "wsgidav",
        "--host", HOST,
        "--port", str(PORT),
        "--root", str(SCRATCH_ROOT),
        "--auth", "anonymous",
        "--server", "uvicorn",
        "--no-config",
    ]


def main() -> None:
    action = sys.argv[1] if len(sys.argv) > 1 else "up"
    if action == "up":
        up()
    elif action == "down":
        down()
    else:
        raise SystemExit(f"unknown action: {action}")


def up() -> None:
    """Start the scratch server (idempotent; writes PID + log)."""
    SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    (SCRATCH_ROOT / "Docs").mkdir(exist_ok=True)
    (SCRATCH_ROOT / "notes.txt").write_text("scratch seed\n")
    if PID_FILE.exists():
        return  # already up
    log = open(LOG_FILE, "w")  # noqa: SIM115 - server lifetime
    proc = subprocess.Popen(_server_cmd(), stdout=log, stderr=subprocess.STDOUT)
    PID_FILE.write_text(str(proc.pid))
    # readiness probe: up to ~5s for the socket to accept
    for _ in range(_READY_PROBES):
        try:
            with socket.create_connection((HOST, PORT), timeout=0.2):
                return
        except OSError:
            time.sleep(0.1)
    down()
    raise RuntimeError("scratch WebDAV did not become ready; see " + str(LOG_FILE))


def down() -> None:
    if not PID_FILE.exists():
        return
    pid = int(PID_FILE.read_text().strip())
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGTERM)
    PID_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
