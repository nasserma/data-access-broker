# Changelog

All notable changes to this project are documented in this file.
Format based on Keep a Changelog; versioning is SemVer.

## [0.2.0] — 2026-09-28

- Client-side MCP fetcher (`data_broker.client_mcp`): a minimal stdio
  MCP server exposing `fetch` and `push` over the transfer path, for
  AI harnesses that gate shell commands (approval prompts, sandboxing)
  — the CLI deadlocked in unattended sessions when its terminal
  approval timed out (found live 2026-09-28). Tool results carry the
  staged path + sha256 manifest only, never content; the D5 invariant
  holds on the client surface too. No policy, grants, or audit live
  client side: the remote broker enforces the wall unchanged.
- Shared client core (`data_broker.client`): the transport, SHA-256
  verify-then-stage/verify-then-write logic, and staging gc extracted
  from the CLI. One implementation, two doors (CLI argv, MCP stdio).
  The CLI is now a thin wrapper; identical flags, exit codes 0-4, and
  single-line stdout contract. The full v0.1.x import surface is
  re-exported from `data_broker.cli` for back-compat.
- New test batteries: client MCP tool surface (manifest-only results,
  refusal/verification arms, live stdio subprocess handshake) and
  client transport error arms (real HTTP against a stub server).
- Test repairs (pre-existing, root-caused): the dual-mount lifespan
  fake never delivered shutdown and hung the suite (the recorded
  known-hanger; the fake was at fault, not the dispatch), and the
  boot-wiring test depended on a live WebDAV store at 127.0.0.1:8466
  (the connect probe is now stubbed in that wiring test).

## [0.1.1] — 2026-09-20

- Account-name uniqueness enforced at load: duplicate names within a
  backend family or across sibling families (webdav/onedrive) now
  refuse to start with a named ConfigError (the H2 invariant; the
  boot's accounts maps previously kept the last entry silently).
- `list_accounts` agent-surface tool (Tier 1): account names + backend
  families only, never URLs, usernames, or credential material (the
  D6.5 discovery discipline, ported from nextcloud-access-broker).
  Multi-instance deployments configure multiple named accounts; agents
  can now discover them.

## [0.1.0] — 2026-09-18

Initial release (Stage 4 of the access-broker suite plan):

- Cross-provider policy wall: normalized store-node resources,
  declared-operation vocabulary, tier table, fail-closed stage order,
  hypothesis fuzz on the normalizer.
- Generic WebDAV backend v1 (the reference implementation; covers
  Nextcloud, ownCloud, and any DAV server) with per-store named
  accounts and capability probing at boot.
- OneDrive backend v1: MS Graph drives via the msal/msgraph stack,
  recorded fixtures; live-tenant wiring is a deployment-session task.
- Dual-surface MCP server: agent surface and transfer surface behind
  separate tokens; refuse-to-start equal-token and missing-token
  guards; content-addressed staging with sha verification.
- Baseline permissions engine (consumed from access-broker-core):
  standing T0/T1 read baselines, reassessment cycle, suspension
  semantics with reconfirm grace, gated mutation, budget report.
- Hash-chained audit log with the baseline event classes
  (baseline.create/modify/remove/suspend/restore/reconfirm-requested).