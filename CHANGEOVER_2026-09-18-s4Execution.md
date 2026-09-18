# CHANGEOVER — 2026-09-18 — Stage 4 executed (data-access-broker built)

Author: Hermes Agent (research profile) for Nasser Mohieddin Abukhdeir.
Stage: S4 per the goal contract
(reports/dataAccessBrokerGoalContract.pdf), executed with NO owner
testing (standing instruction: owner testing deferred to end of line).

## Outcome (one paragraph)

data-access-broker is built and committed: the fifth broker in the
suite, the third built ON access-broker-core, and the vehicle for the
December 2026 generalization window pulled forward on the owner's
instruction. The grant-store "resource + operation" generalization and
the baseline permissions engine landed in the core (S4-1: 477 tests,
100% stmt+branch, ruff clean; backward-compat gate groupware 633 +
smarthome 294 green unchanged BEFORE any data-broker build). The
cross-provider wall (bounded percent-decoding normalizer, declared-
operation vocabulary, tier table with the free-lane invariant
returning, hypothesis fuzz), the generic WebDAV backend verified LIVE
against the scratch WSGIDAV container, the OneDrive backend against
recorded fixtures, the dual-surface server with the D5 two-token
refuse-to-start guards, and the boot-path battery (real config, real
boot, real calls) all per contract. Repo commits dc6262c..2a1dfa7
clean tree at S4-5; S4-6 closes the stage.

## State of record

- Repo: infra/infrastructure/dataAccessBroker/ (git, clean tree).
- Core commit: b296dda (declared_operations seam + baselines.py per
  design note rev 2, F-A..F-L).
- Verification: `uv run pytest -q tests/ --cov=data_broker
  --cov-branch` and `uv run ruff check .` (64 passed, ruff clean at
  S4-5).
- Scratch WebDAV: scratch_servers/webdav_scratch.py (wsgidav +
  uvicorn server; wsgiref is single-threaded and blocks PUTs - found
  live).

## Findings of note (this stage)

1. **The grace clock must start at the interval end, not at cycle
   time.** The first implementation started the grace clock when
   reassess() noticed a due baseline; a broker restarted after an
   outage would then re-wait a full grace. Fixed: the '_reconfirm'
   mark is written at the LOGICAL issue time (catch-up semantics).
2. **PENDING_CREATION leakage (two sites).** Unapproved definition
   requests leaked into list_definitions() and the budget report;
   an unapproved request must not consume budget or appear as standing
   permission. Both filtered (F-D, F-L).
3. **Component-prefix containment (real wall defect).** Raw string
   prefix ("Knowledge/2026"[:9]) accepted "Knowledgeable"; fixed with
   component-boundary matching ("Knowledge" covers "Knowledge/2026",
   never "Knowledgeable") - the suite's covers() rule at the
   component level.
4. **Budget roll-up counts every standing definition** whatever its
   mid-cycle state: a PENDING_RECONFIRM baseline is still standing
   (the report's discouragement counts it).
5. **Test-authoring defects outnumbered engine defects** (stale
   helper names, fixture/expectation mismatches, scaffolding debris
   caught before commit). The wall never raised on any hostile input;
   the fail-closed contract held throughout.

## Resumable state (for the next session)

- S4-0..S4-6 COMPLETE; supervisory AI review of Stages 1-4 COMPLETE
  (2026-09-18, verdict PASS WITH FINDINGS; Stages 1-3 verified clean).
  Review findings and dispositions: (1) dual-surface battery env
  hygiene — FIXED (monkeypatch + autouse fixture; green in a clean
  env); (2) coverage gate 58% — raised (batteries exist; the transfer
  surface build added new modules that dilute the aggregate; per-
  module coverage is the honest metric — wall 100%, server 100%,
  backends 96-98%, tools 94%); (3) TRANSFER SURFACE — BUILT AND
  COMMITTED (4ddf1ce): second MCP surface (check_access/read/write)
  behind its own token, verify-then-write, content-addressed staging
  CLI with exit codes 0-4, and the recorded deliberate deviation
  (transfer-surface reads/writes grant-scoped, never free — the
  nextcloud whole-file gating posture); (4) low: baselines.py
  docstring overstates max_suspension surfacing (F-A default-OFF, the
  schema omission is defensible).
- Closing sweep after the transfer surface: core 477 / groupware
  633+1skip / smarthome 294 / comms 272 / data 234 green. Repo
  dc6262c..4ddf1ce clean tree.
- Also remaining: (a) owner deferred end-of-line testing (this repo's
  DEPLOYMENT.md verification order — including the transfer CLI
  fetch/push cycle); (b) publication (owner; rotation precedes
  publication for every repo); (c) nextcloud supersession migration
  AFTER Gate 8 + rotation (core MIGRATION_NOTES.md).
- The nextcloud supersession migration is NOT in this stage (after
  Gate 8 + rotation; core MIGRATION_NOTES.md holds the migration cost
  record).
- Supervisory AI review of all completed stages follows the stage
  closure, on the full stage record.