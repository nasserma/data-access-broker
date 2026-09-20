# Deployment Guide (data-access-broker)

Status: S4-6 readiness. No live deployment performed (goal contract);
this guide is the scratch-to-production checklist for the owner's
deferred testing and deployment session.

## 1. Prerequisites

1. One or more file stores:
   - **WebDAV** (the generic v1 reference implementation): Nextcloud,
     ownCloud, or any RFC 4918 DAV server, reachable over HTTP(S).
   - **OneDrive**: an Entra app registration (tenant_id + client_id);
     the msal-backed token provider installs at config time (a
     deployment-session task; the backend never imports msal - tests
     and scratch use the static provider).
2. Store credentials scoped to a **dedicated broker identity**: a
   WebDAV app password or Entra registration scoped to the stores the
   broker serves. Human-account credential reuse would make the audit
   trail's principal meaningless (SECURITY.md prerequisites).
3. access-broker-core adjacent to this repo (editable path dependency;
   GitHub publication is the owner's separate task).
4. Approval gateway (when wired): a dedicated broker bot account on
   the gateway platform; the approval-separation invariant holds
   structurally (the broker must not broker its own approval account).

## 2. Configuration

Copy `config.yaml.example`, walk every key. The loader is strict:
unknown keys are `ConfigError` (a typo stops the boot). Secrets are
never in YAML: every `password_env` / `token_env` names an environment
variable that MUST be set at boot (fail closed).

The two-token model is refuse-to-start enforced at load AND at boot:

- `tokens.agent_env` — the MCP client's surface token
- `tokens.transfer_env` — the transfer surface token (CLI only);
  MUST be at least `min_length` characters AND different from the
  agent token, or the boot refuses

Environment variables (from the example):
- `WEBDAV_PASSWORD_SCRATCH` — the WebDAV app password per store
- `ONEDRIVE_TOKEN_PERSONAL` — the Graph access token per account
- `DATABROKER_AGENT_TOKEN` / `DATABROKER_TRANSFER_TOKEN` — the surface
  tokens

### OneDrive token provider selection (S6-2b)

An `accounts.onedrive` entry selects its token provider by config:

- default (no `token_provider` key): `StaticTokenProvider` from
  `token_env` — tests, scratch, and any deployment holding a token out
  of band. No network.
- `token_provider: msal` (requires `client_id` and `authority`, e.g.
  `https://login.microsoftonline.com/<tenant>`): the production
  `MsalTokenProvider` (`data_broker/token_providers.py`) — device-code
  flow, per-account cache at `<data_dir>/msal_cache_<account>.json`.
  The backend never imports msal (asserted in the battery).

Fail-closed contract: when MSAL demands interaction (no cache, expired
refresh, consent), the provider raises `InteractionRequiredError`
naming the account and the verification URI/code — the broker refuses
rather than proceeding unauthenticated. The interactive device-code
acquisition is OWNER-executed in his live-tenant stage (ruled
2026-09-19): run the broker once with `token_provider: msal`, read the
verification URI + code from the error, complete it on the tenant, and
the cached token makes subsequent boots silent. The Entra app
registration and tenant setup are also owner steps.

## 3. Boot and verification

```
uv sync --dev
uv run pytest -q                 # full suite (64 passed expected)
uv run python -m data_broker.run # transport from config
```

Verify in order:
1. Boot completes (refuse-to-start on any validation failure,
   including token guards).
2. `check_access` answers ok through the MCP surface.
3. A read/list executes free (the free-lane invariant returns in this
   file-class domain), audit-logged.
4. A trash/move with no grant submits a pending request; when a
   gateway is configured it posts to the approval room (with no
   gateway configured the request still pends — notification is
   skipped silently by design; the S5-4 dry-run note).
5. Approve via the gateway; the operation executes; the audit chain
   verifies (`verify_chain`). Without a live gateway, drive the same
   decision seam the gateway core calls (store approve), as the
   S5-4 dry-run does.
6. Transfer-surface CLI: fetch a file, sha256 verifies, bulk content
   confirmed absent from the LLM context. The CLI performs the MCP
   initialize handshake once and echoes the session id (S5-4 repair:
   a bare tools/call is rejected 400 by the session-stateful
   transport). The transfer surface is served at /transfer behind
   the transfer token on the same port as /mcp (the dual mount).

## 4. Versioning-dependent write policy

Direct writes where the backend versions: OneDrive keeps its own
version history; WebDAV over a versioning store (Nextcloud versions)
likewise. A backend WITHOUT versioning would require the
per-operation approval discipline the smart home broker introduces;
that case is documented here, not improvised. Capability probing at
boot decides; unversioned stores are fail-closed for the write policy.

## 5. Baseline rollout notes

- The reassessment cycle is the primary mechanism; baseline
  accumulation is a cost to be managed, not a feature to be celebrated
  (ruling 5). The budget report surfaces accumulation; the standing
  budget is owner-set.
- Discovery migration (F-J) is owner-visible and NOT behavior-neutral:
  enrolling existing discovery-enabled instances generates
  reconfirmation traffic. The choice (grandfather as policy-exempt, or
  enroll in the reassessment cycle) is recorded at rollout, never
  improvised. The nextcloud broker's live discovery flags are NOT
  migrated in this stage.
- The supervisory-AI principal is designed but NOT recommended
  (ruling 3): leave it off; the documented escape for owner
  unavailability is suspended-baseline + deferred grants queueing for
  the human's return.

## 6. Scratch-to-production checklist

- Scratch WebDAV container (scratch_servers/webdav_scratch.py) first;
  the marked integration tests against it are the bring-up proof.
  Record (2026-09-20 closing sweep, supervisory-review finding 2): the
  2 marked tests were SKIPPED at the Stage 6 sweep because the scratch
  server was down; the skip is recorded, not counted. Run them live
  (`uv run python scratch_servers/webdav_scratch.py up`, then
  `uv run pytest -m integration`) before any rotation/publication
  work resumes.
- Broker identity created BEFORE production wiring (§1).
- Token rotation discipline: the transfer token is rotated like any
  suite credential; rotation precedes publication for every repo.
- Wildcard-bind and token-guard refusals tested
  (test_server_dual_surface.py).

## 7. Rollback

Container rebuild to the previous image tag; grants and audit survive
restart (SQLite + append-only JSONL). Baseline definitions persist in
the store; a rollback never promotes suspended or pending baselines
(restart-never-promotes, F-E).