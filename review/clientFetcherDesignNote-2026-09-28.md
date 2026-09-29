# Client-Side Transfer Fetcher — Design Note (2026-09-28)

Status: IMPLEMENTED (v0.2.0, in the working tree, uncommitted) —
awaiting owner supervisory review, then live test. CHANGELOG.md,
README.md, DEPLOYMENT.md section 9, and SECURITY.md invariant 3 carry
the shipped documentation. One recorded deviation from this note: the
shipped fetcher exposes plain `fetch`/`push` tools over the shared
client core (the CLI's own code path); the one-time capability-token
manifest variant described below was NOT implemented — the token
exposure question it addressed is unchanged (the fetcher holds the
same transfer token the CLI already holds on the same host), so the
simpler shape was built first. The capability variant remains a
recorded future option if token discipline ever demands it.
Owner: Nasser. Implementer: dev profile (coding-assistant peer owns all coding work,
per the 2026-09-22 ownership ruling). Research profile relays and verifies.

## Problem

The only sanctioned path for bulk file content is the transfer CLI
(`python -m data_broker.cli fetch`). On the AI host, invoking it means a
shell command; Hermes routes shell commands with network egress through the
terminal approval gate; unattended sessions time out and the command never
runs (observed live 2026-09-28, research profile, NE deck build: two
approval-prompt timeouts, fetch never executed).

Root cause: the invocation layer, not the security model. The D5 invariant
(bulk content never transits the LLM context) is correct and must be
preserved. What is wrong is that the only transport for that invariant is a
process-spawn that host-level agent harnesses gate or prohibit.

## Proposed revision (v0.1.1 → v0.2.0)

Ship a second, client-side MCP endpoint in the same package: a minimal
local fetcher the harness starts as a helper process and calls as a normal
tool. It reuses the CLI's exact code path. Content still never enters the
LLM context: the tool returns a local staged path + sha256 manifest only.

### What changes

1. `data_broker/client.py` (NEW) — shared client core. The transport
   (TransferClient), verify-then-stage fetch, verify-then-write push, and
   staging gc extracted from cli.py verbatim. One implementation.

2. `data_broker/client_mcp.py` (NEW) — minimal local MCP server (stdio)
   exposing exactly two tools:
   - `fetch(account, resource)` — identical to CLI fetch; returns the
     one-line manifest JSON: staged path, sha256, size. Never content.
   - `push(account, resource, path)` — identical to CLI push.
   Runs on the agent host. Token from `DATABROKER_TRANSFER_TOKEN` env
   only (never argv, never config); broker URL from `DATABROKER_URL` env
   (default `http://127.0.0.1:8471/transfer`). No policy, no grants, no
   audit — the remote broker enforces all of it, unchanged.

3. `data_broker/cli.py` — becomes a thin wrapper over client.py.
   Same flags, same exit codes 0-4, same single-line stdout contract.
   Zero behavior change for existing CLI users.

4. `DEPLOYMENT.md` — new section: "Client-side fetcher for AI harnesses."
   Install the package (or the two client modules) on the agent host, set
   the two env vars, add a stdio MCP entry to the harness config
   (`python -m data_broker.client_mcp`). Includes a worked Hermes example.

5. `SECURITY.md` — addendum to invariant 3: bulk content still never
   enters LLM context; the fetcher's tool result is metadata (path +
   hash) only. Transfer-token exposure surface = the client host, the
   same exposure the CLI has today — no wider. The fetcher cannot serve
   content, only stage it.

6. Tests — new batteries: client core (verify-then-stage ordering, sha
   mismatch refusal, gc, exit-code mapping), client MCP tool surface
   (ok / wall-refused / verification-failure paths), plus the existing
   CLI suite staying green. Ruff clean. Coverage gate stays at the
   recorded 93 baseline with a per-module note for the new files.

7. Version bump to 0.2.0 (new feature, no wire-contract change).

### What does NOT change

`server.py`, `tools.py`, `policy.py`, backends, gateways, the core
submodule — untouched. The `/mcp` and `/transfer` wire contracts are
unchanged; the client speaks the same MCP calls the CLI already makes.
The CLI remains fully supported as the manual/fallback path.

### Why this preserves the security model

- Read grant still required — the fetcher calls the same grant-scoped
  transfer-surface `read`; no grant → REFUSED, same as today.
- Two-token separation unchanged — the fetcher holds only the transfer
  token, never the agent token.
- sha256 verify-then-stage unchanged — no staging write before every
  verification passes.
- Audit unchanged — every fetch is audited by the remote broker.
- The only removed friction is the host-side shell-command approval
  prompt, which was never a broker control.

### Other brokers in the suite

Not applicable. The data broker is the only suite member with bulk
content; comms/automation/groupware payloads are small and ride the
agent-surface tool calls, which harnesses do not gate as shell commands.
No client-side piece is proposed for them.

## Deployment sequence (after build + tests)

1. Dev tree: implement, full battery green, ruff clean.
2. Server side (nahome): rsync data_broker + restart — core untouched,
   so no venv core-copy step.
3. Client side (AI host): install the updated package (or client modules)
   in the profile venv; owner adds `DATABROKER_TRANSFER_TOKEN` to the
   profile `.env` (his edit, per standing rule); owner adds the stdio MCP
   entry to the profile config (his edit) + gateway restart.
4. Live test (owner): agent fetches a granted file with no approval
   prompt; staged path returned; sha verifies; un-granted fetch REFUSED.

## Owner test checklist

- [ ] Fetch with an active grant: no approval prompt, manifest returned,
      staged file sha256 matches, permissions 0600 under staging dir.
- [ ] Fetch with no grant: REFUSED envelope, exit/reason semantics match
      CLI exit code 3 behavior.
- [ ] Corrupted transfer (simulated sha mismatch): nothing staged,
      verification-failure result.
- [ ] CLI fetch still works unchanged (regression).