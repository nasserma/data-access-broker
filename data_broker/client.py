# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared client core for the data broker's client-side surfaces
(the transfer CLI and the local MCP fetcher).

Extracted verbatim from cli.py (S4-5, ported from
nextcloud-access-broker broker/cli.py, GPL-3.0-or-later) so the CLI and
the local fetcher server share ONE implementation of the transfer
path: token from env only, verify-then-stage, verify-then-write,
content-addressed staging with gc. File content NEVER transits the
LLM context — every consumer of this module hands the agent a local
staged path plus a sha256 manifest, never the bytes.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

CLIENT_VERSION = "0.2.0"

EXIT_OK = 0
EXIT_LOCAL = 1
EXIT_INFRA = 2
EXIT_REFUSED = 3
EXIT_VERIFICATION = 4
_HTTP_UNAUTHORIZED = 401

GC_MAX_AGE_DAYS = 7
GC_MAX_TOTAL_BYTES = 512 * 1024 * 1024

TOKEN_ENV = "DATABROKER_TRANSFER_TOKEN"
URL_ENV = "DATABROKER_URL"
DEFAULT_URL = "http://127.0.0.1:8471/transfer"


class ClientError(Exception):
    """Local/usage error (CLI exit 1 class)."""

    def __init__(self, message: str) -> None:
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

    Only what the client surfaces need: initialize (session capture),
    call_tool. Token from the env only; echoed loudly on --url override.
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
                raise ClientError(
                    "broker rejected the transfer token (401); check "
                    "DATABROKER_TRANSFER_TOKEN against the transfer surface"
                ) from exc
            raise ClientError(f"broker HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise ClientError(f"broker unreachable: {exc.reason}") from exc
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
                    "clientInfo": {"name": "data-broker-client", "version": CLIENT_VERSION},
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
                    raise ClientError("unparseable broker response (no JSON, no SSE data)")
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
                    raise ClientError("no JSON-RPC response in SSE stream")
        except json.JSONDecodeError as exc:
            raise ClientError(f"broker response is not valid JSON: {exc}") from exc
        if envelope.get("error"):
            raise ClientError(f"broker rpc error: {envelope['error']}")
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


def envelope_error(result: dict, where: str) -> ClientError | None:
    status = result.get("status")
    if status == "refused":
        return ClientError(f"REFUSED: {result.get('reason', 'no active grant')}")
    if status == "error":
        return ClientError(f"INFRA: broker error: {result.get('error', 'unknown')}")
    if status != "ok":
        return ClientError(f"INFRA: unexpected envelope status {status!r} ({where})")
    return None


# ---------------------------------------------------------------- fetch


def fetch(
    account: str,
    resource: str,
    url: str | None = None,
    token: str | None = None,
) -> tuple[int, dict]:
    """Verify-then-stage fetch. Returns (exit_code, manifest_or_error).

    The ONE transfer path shared by the CLI and the local MCP fetcher:
    read (content_b64) -> strict base64 decode -> sha verify against
    the server's sha -> verify-then-stage into content-addressed
    staging. NO staging write before every verification passes. The
    returned manifest carries the local staged path, never content.
    """
    token = token if token is not None else os.environ.get(TOKEN_ENV)
    if not token:
        return EXIT_LOCAL, {"error": f"{TOKEN_ENV} is not set"}
    client = TransferClient(url or os.environ.get(URL_ENV, DEFAULT_URL), token)
    root = staging_root(account)
    _ensure_private_dir(root)

    try:
        result = client.call_tool("read", {"account": account, "resource": resource})
    except ClientError as exc:
        return _error_exit(exc)

    err = envelope_error(result, "fetch")
    if err:
        return _error_exit(err)

    content_b64 = result.get("content_b64")
    server_sha = result.get("sha256")
    declared_size = result.get("size")
    if not isinstance(content_b64, str) or not isinstance(server_sha, str):
        return EXIT_VERIFICATION, {"error": "no content_b64/sha256 in envelope"}
    try:
        data = base64.b64decode(content_b64, validate=True)
    except Exception as exc:  # noqa: BLE001 - strict decode, the client contract
        return EXIT_VERIFICATION, {"error": f"content is not valid base64: {exc}"}

    digest = hashlib.sha256(data).hexdigest()
    if digest != server_sha:
        return EXIT_VERIFICATION, {
            "error": f"sha mismatch (server {server_sha}, local {digest})"
        }

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
        "account": account,
        "resource": resource,
        "sha256": digest,
        "size": len(data),
    }
    manifest_path = root / f"{digest}.json"
    tmp_manifest = root / f".{digest}.manifest.json.tmp"
    tmp_manifest.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    os.chmod(tmp_manifest, 0o600)
    os.replace(tmp_manifest, manifest_path)

    return EXIT_OK, {
        "staged": str(entry),
        "sha256": digest,
        "size": len(data),
        "declared_size": declared_size,
    }


def _error_exit(exc: ClientError) -> tuple[int, dict]:
    message = exc.message
    if "REFUSED" in message or "refused" in message:
        return EXIT_REFUSED, {"error": message}
    if "INFRA" in message or "unreachable" in message or "HTTP" in message:
        return EXIT_INFRA, {"error": message}
    return EXIT_LOCAL, {"error": message}


# ----------------------------------------------------------------- push


def push(
    account: str,
    resource: str,
    path: str,
    url: str | None = None,
    token: str | None = None,
) -> tuple[int, dict]:
    """Verify-then-write push. Returns (exit_code, manifest_or_error).

    Reads the local file, hashes it, and sends it with expected_sha256;
    the SERVER verifies before writing. The bytes still never appear in
    any tool result or stdout manifest.
    """
    token = token if token is not None else os.environ.get(TOKEN_ENV)
    if not token:
        return EXIT_LOCAL, {"error": f"{TOKEN_ENV} is not set"}
    src = Path(path)
    if not src.is_file():
        return EXIT_LOCAL, {"error": f"{path} is not a file"}
    try:
        data = src.read_bytes()
    except OSError as exc:
        return EXIT_LOCAL, {"error": str(exc)}

    digest = hashlib.sha256(data).hexdigest()
    client = TransferClient(url or os.environ.get(URL_ENV, DEFAULT_URL), token)
    try:
        result = client.call_tool(
            "write",
            {
                "account": account,
                "resource": resource,
                "content_b64": base64.b64encode(data).decode(),
                "expected_sha256": digest,
            },
        )
    except ClientError as exc:
        return _error_exit(exc)

    err = envelope_error(result, "push")
    if err:
        if "sha256 mismatch" in (result.get("reason") or ""):
            return EXIT_VERIFICATION, {"error": err.message}
        return _error_exit(err)

    return EXIT_OK, {"pushed": resource, "sha256": result.get("sha256", digest), "size": len(data)}
