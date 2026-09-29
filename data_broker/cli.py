# SPDX-License-Identifier: GPL-3.0-or-later
"""S4-5 transfer CLI: `data-broker fetch` / `data-broker push`.

Ported from the same author's nextcloud-access-broker broker/cli.py
(GPL-3.0-or-later) per the goal contract S4-5. The only sanctioned path
for bulk content between the broker and the AI host: this CLI speaks
MCP directly against the broker's transfer surface, stages files
locally (content-addressed), and hands the agent a local path plus a
manifest. File content NEVER transits the LLM context.

As of v0.2.0 the transport, verification, and staging live in
data_broker.client (the shared client core); this module is the argv
wrapper over it with the identical flags, exit codes, and single-line
stdout contract. The local MCP fetcher (data_broker.client_mcp) rides
the same core.

Design (nextcloud pattern retained):

- Token from env DATABROKER_TRANSFER_TOKEN only (never argv, never a
  config value); broker URL pinned or from --url (echoed to stderr).
- Fetch: read (content_b64) -> strict base64 decode -> sha verify
  against the server's sha -> size check -> temp + fsync + atomic
  rename into staging. NO staging write before every verification
  passes.
- Push: read the local file -> sha -> write with expected_sha256 ->
  the server verifies before writing (verify-then-write); on mismatch
  the server refuses (exit 4).
- Staging: ~/.local/state/data-broker/staging/<account>/<sha256>
  (0700/0600); gc sweep per invocation (7 days / 512 MB).
- Exit codes (the contract agents branch on):
    0 ok; 1 local/usage; 2 infrastructure (retry later);
    3 wall-refused (request access, never retry as-is);
    4 verification failure (treat as corrupt).
- stdout carries exactly one machine-parsable line; diagnostics to
  stderr.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from data_broker import client as _client
from data_broker.client import EXIT_OK

# Back-compat surface: everything the CLI exported at v0.1.x now lives
# in data_broker.client (the shared client core). Re-exported here so
# the 0.1.x import surface (tests, scripts, `data_broker.cli.X`) is
# unchanged.
for _name in (
    "CLI_VERSION",
    "EXIT_OK",
    "EXIT_LOCAL",
    "EXIT_INFRA",
    "EXIT_REFUSED",
    "EXIT_VERIFICATION",
    "GC_MAX_AGE_DAYS",
    "GC_MAX_TOTAL_BYTES",
    "TOKEN_ENV",
    "ClientError",
    "staging_root",
    "gc_staging",
    "TransferClient",
):
    globals()[_name] = "0.2.0" if _name == "CLI_VERSION" else getattr(_client, _name)

CliError = globals()["ClientError"]  # the v0.1.x exception name, same class

# The v0.1.x private helpers the transport battery asserts on: thin
# delegates to the client core (same messages, same exit mapping).
_client_error = _client._error_exit  # noqa: SLF001 - back-compat delegate target
# Back-compat alias: the v0.1.x transport battery asserts on cli._envelope_error
# (deliberately NOT auto-removed; ruff F401 is suppressed because the test
# suite and any 0.1.x caller import it from this module).
from data_broker.client import envelope_error as _envelope_error  # noqa: E402, F401


def _cli_error_exit(exc: Any) -> int:  # noqa: ARG001 - back-compat delegate
    rc, _payload = _client_error(exc)  # the v0.1.x exit mapping, unchanged
    return rc


def cmd_fetch(args: argparse.Namespace) -> int:
    rc, payload = _client.fetch(account=args.account, resource=args.resource, url=args.url)
    if rc != EXIT_OK:
        print(f"data-broker: {payload.get('error', 'unknown error')}", file=sys.stderr)
        return rc
    print(json.dumps(payload))
    return EXIT_OK


def cmd_push(args: argparse.Namespace) -> int:
    rc, payload = _client.push(
        account=args.account, resource=args.resource, path=args.path, url=args.url
    )
    if rc != EXIT_OK:
        print(f"data-broker: {payload.get('error', 'unknown error')}", file=sys.stderr)
        return rc
    print(json.dumps(payload))
    return EXIT_OK


def main() -> None:
    parser = argparse.ArgumentParser(prog="data-broker", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    fetch = sub.add_parser("fetch", help="fetch a file into content-addressed staging")
    fetch.add_argument("account")
    fetch.add_argument("resource")
    fetch.add_argument("--url", default=os.environ.get("DATABROKER_URL", "http://127.0.0.1:8471/transfer"))
    fetch.set_defaults(func=cmd_fetch)

    push = sub.add_parser("push", help="push a local file to a resource")
    push.add_argument("account")
    push.add_argument("resource")
    push.add_argument("path")
    push.add_argument("--url", default=os.environ.get("DATABROKER_URL", "http://127.0.0.1:8471/transfer"))
    push.set_defaults(func=cmd_push)

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
