"""S4-5 transfer CLI: `data-broker fetch` / `data-broker push`.

Ported from the same author's nextcloud-access-broker broker/cli.py
(GPL-3.0-or-later) per the goal contract S4-5. The only sanctioned path
for bulk content between the broker and the AI host: this CLI speaks
MCP directly against the broker's transfer surface, stages files
locally (content-addressed), and hands the agent a local path plus a
manifest. File content NEVER transits the LLM context.

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

# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

CLI_VERSION = "0.1.1"

EXIT_OK = 0
EXIT_LOCAL = 1
EXIT_INFRA = 2
EXIT_REFUSED = 3
EXIT_VERIFICATION = 4
_HTTP_UNAUTHORIZED = 401

GC_MAX_AGE_DAYS = 7
GC_MAX_TOTAL_BYTES = 512 * 1024 * 1024

TOKEN_ENV = "DATABROKER_TRANSFER_TOKEN"


class CliError(Exception):
    """Local/usage error -> exit 1."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


# ------------------------------------------------------------- staging


def staging_root(account: str) -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    return base / "data-broker" / "staging" / account


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def gc_staging(account: str) -> list[str]:
    """Delete staged entries older than GC_MAX_AGE_DAYS; called on every
    invocation (the nextcloud pattern)."""
    root = staging_root(account).parent
    if not root.exists():
        return []
    removed = []

    cutoff = time.time() - GC_MAX_AGE_DAYS * 86400
    for entry in root.rglob("*"):
        try:
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                entry.unlink(missing_ok=True)
                removed.append(str(entry))
        except OSError:
            continue
    return removed


# ------------------------------------------------------------ transport


class TransferClient:
    """Minimal streamable-HTTP MCP client (JSON-RPC over POST).

    Only what the CLI needs: initialize (session capture), call_tool.
    Token from the env only; echoed loudly on --url override.
    """

    def __init__(self, url: str, token: str, timeout: float = 60.0) -> None:
        self.url = url
        self.token = token
        self.timeout = timeout
        self._id = 0
        self._session_id: str | None = None

    def _post(self, payload: dict, expect_session: bool = False) -> str:
        """One JSON-RPC POST; returns the body text. Captures the MCP
        session header when expect_session (the initialize call)."""
        data = json.dumps(payload).encode()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self.token}",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        req = urllib.request.Request(self.url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode()
                if expect_session and self._session_id is None:
                    self._session_id = resp.headers.get("Mcp-Session-Id")
        except urllib.error.HTTPError as exc:
            if exc.code == _HTTP_UNAUTHORIZED:
                raise CliError(
                    "broker rejected the transfer token (401); check "
                    "DATABROKER_TRANSFER_TOKEN against the transfer surface"
                ) from exc
            raise CliError(f"broker HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise CliError(f"broker unreachable: {exc.reason}") from exc
        return body

    def _initialize(self) -> None:
        """Streamable HTTP is session-stateful: a bare tools/call is
        rejected with 400 'Missing session ID' (S5-4 dry-run finding).
        Initialize once, capture the session header, echo it on every
        call (the nextcloud CLI's contract, ported)."""
        self._id += 1
        self._post(
            {
                "jsonrpc": "2.0",
                "id": self._id,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2026-07-28",
                    "capabilities": {},
                    "clientInfo": {"name": "data-broker-cli", "version": CLI_VERSION},
                },
            },
            expect_session=True,
        )

    def call_tool(self, name: str, args: dict) -> dict:
        if self._session_id is None:
            self._initialize()
        self._id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._id,
            "method": "tools/call",
            "params": {"name": name, "arguments": args},
        }
        return self._parse_response(self._post(payload))

    @staticmethod
    def _parse_response(body: str) -> dict:
        """Accept plain JSON-RPC or SSE bodies whose data: lines carry it."""
        text = body.strip()
        try:
            if text.startswith("{"):
                envelope = json.loads(text)
            else:
                data_lines = [
                    line[5:].strip()
                    for line in text.splitlines()
                    if line.startswith("data:")
                ]
                if not data_lines:
                    raise CliError("unparseable broker response (no JSON, no SSE data)")
                envelope = None
                for line in reversed(data_lines):
                    try:
                        cand = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(cand, dict) and ("id" in cand or "result" in cand):
                        envelope = cand
                        break
                if envelope is None:
                    raise CliError("no JSON-RPC response in SSE stream")
        except json.JSONDecodeError as exc:
            raise CliError(f"broker response is not valid JSON: {exc}") from exc
        if envelope.get("error"):
            raise CliError(f"broker rpc error: {envelope['error']}")
        result = envelope.get("result", {})
        content = result.get("content") if isinstance(result, dict) else None
        if isinstance(content, list) and content:
            inner = content[0].get("text")
            if inner:
                try:
                    return json.loads(inner)
                except json.JSONDecodeError:
                    return {"raw": inner}
        return result if isinstance(result, dict) else {}


def _envelope_error(result: dict, where: str) -> CliError | None:
    status = result.get("status")
    if status == "refused":
        return CliError(f"REFUSED: {result.get('reason', 'no active grant')}")
    if status == "error":
        return CliError(f"INFRA: broker error: {result.get('error', 'unknown')}")
    if status != "ok":
        return CliError(f"INFRA: unexpected envelope status {status!r} ({where})")
    return None


# ------------------------------------------------------------------ fetch


def cmd_fetch(args: argparse.Namespace) -> int:
    token = os.environ.get("DATABROKER_TRANSFER_TOKEN")
    if not token:
        print("data-broker: local: DATABROKER_TRANSFER_TOKEN is not set", file=sys.stderr)
        return EXIT_LOCAL
    client = TransferClient(args.url, token)
    root = staging_root(args.account)
    _ensure_private_dir(root)

    try:
        result = client.call_tool(
            "read", {"account": args.account, "resource": args.resource}
        )
    except CliError as exc:
        return _cli_error_exit(exc)

    err = _envelope_error(result, "fetch")
    if err:
        print(f"data-broker: {err.message}", file=sys.stderr)
        return EXIT_REFUSED if "refused" in err.message or "REFUSED" in err.message else EXIT_INFRA

    content_b64 = result.get("content_b64")
    server_sha = result.get("sha256")
    size = result.get("size")
    if not isinstance(content_b64, str) or not isinstance(server_sha, str):
        print("data-broker: VERIFY: no content_b64/sha256 in envelope", file=sys.stderr)
        return EXIT_VERIFICATION
    try:
        data = base64.b64decode(content_b64, validate=True)
    except Exception as exc:  # noqa: BLE001 - strict decode, the CLI contract
        print(f"data-broker: VERIFY: content is not valid base64: {exc}", file=sys.stderr)
        return EXIT_VERIFICATION

    digest = hashlib.sha256(data).hexdigest()
    if digest != server_sha:
        print(
            f"data-broker: VERIFY: sha mismatch (server {server_sha}, local {digest})",
            file=sys.stderr,
        )
        return EXIT_VERIFICATION

    # verify-then-stage: nothing touches staging until here
    entry = root / digest
    tmp = root / f".{digest}.tmp"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, entry)
    manifest = {
        "account": args.account,
        "resource": args.resource,
        "sha256": digest,
        "size": len(data),
    }
    manifest_path = root / f"{digest}.json"
    tmp_manifest = root / f".{digest}.manifest.json.tmp"
    tmp_manifest.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    os.chmod(tmp_manifest, 0o600)
    os.replace(tmp_manifest, manifest_path)

    print(
        json.dumps(
            {"staged": str(entry), "sha256": digest, "size": len(data), "declared_size": size}
        )
    )
    return EXIT_OK


def _cli_error_exit(exc: CliError) -> int:
    print(f"data-broker: {exc.message}", file=sys.stderr)
    if "REFUSED" in exc.message or "refused" in exc.message:
        return EXIT_REFUSED
    if "unreachable" in exc.message or "HTTP" in exc.message:
        return EXIT_INFRA
    return EXIT_LOCAL


# ------------------------------------------------------------------ push


def cmd_push(args: argparse.Namespace) -> int:
    token = os.environ.get("DATABROKER_TRANSFER_TOKEN")
    if not token:
        print("data-broker: local: DATABROKER_TRANSFER_TOKEN is not set", file=sys.stderr)
        return EXIT_LOCAL
    client = TransferClient(args.url, token)
    path = Path(args.path)
    if not path.is_file():
        print(f"data-broker: local: {path} is not a file", file=sys.stderr)
        return EXIT_LOCAL
    try:
        data = path.read_bytes()
    except OSError as exc:
        print(f"data-broker: local: {exc}", file=sys.stderr)
        return EXIT_LOCAL

    digest = hashlib.sha256(data).hexdigest()
    try:
        result = client.call_tool(
            "write",
            {
                "account": args.account,
                "resource": args.resource,
                "content_b64": base64.b64encode(data).decode(),
                "expected_sha256": digest,
            },
        )
    except CliError as exc:
        return _cli_error_exit(exc)

    err = _envelope_error(result, "push")
    if err:
        print(f"data-broker: {err.message}", file=sys.stderr)
        if "sha256 mismatch" in (result.get("reason") or ""):
            return EXIT_VERIFICATION
        return EXIT_REFUSED if "refused" in (result.get("reason") or "") else EXIT_INFRA

    print(
        json.dumps(
            {"pushed": args.resource, "sha256": result.get("sha256", digest), "size": len(data)}
        )
    )
    return EXIT_OK


# ------------------------------------------------------------------- main


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
