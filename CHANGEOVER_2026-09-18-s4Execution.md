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
  env); (2) coverage gate 58% — batteries added; the transfer surface
  build added new modules so the honest metric is PER-MODULE: wall
  (policy.py) 100%, server 100%, backends 96-98%, tools 94%, run 94%,
  config 87%, cli 79% (the fresh-context review caught the earlier
  claim omitting cli.py at 49% — since closed by the real-HTTP
  transport battery; the remaining cli gap is main()/argparse wiring
  and arms already covered at flow level); (3) TRANSFER SURFACE —
  BUILT AND COMMITTED (4ddf1ce): second MCP surface
  (check_access/read/write) behind its own token, verify-then-write,
  content-addressed staging CLI with exit codes 0-4, and the recorded
  deliberate deviation (transfer-surface reads/writes grant-scoped,
  never free — the nextcloud whole-file gating posture); (4)
  baselines.py docstring — FIXED in the core after the fresh-context
  pass found the docstring-vs-code mismatch untouched: the module now
  states that no max_suspension mechanism is implemented (an
  unimplemented knob would be worse than none) and that the budget
  report is the SOLE zombie countermeasure (last_used_at absent by
  design; usage lives in baseline_usage per F-B).
- Fresh-context supervisory validation pass (2026-09-18, second
  judge): PASS WITH FINDINGS — headline state fully verified (commits,
  clean tree, five closing-sweep counts reproduced, ruff clean,
  scratch-WebDAV integration tests really run and green). Its new
  findings, now closed: per-module coverage claim omitted cli.py
  (fixed by the transport battery + this record); gc_staging
  deletion path untested (now tested in test_cli_transport.py);
  run.py:54-56 dead re-checks shadowed by config guards (DELETED —
  the suite rule under a 100% branch gate is delete, not freeze;
  load_and_validate keeps only the reachable tokens-None arm,
  docstring records why); prior finding 4 docstring (fixed above);
  pytest.mark.integration unregistered (cosmetic, noted).
- Closing sweep (fresh-context judge re-ran it): core 477 / groupware
  633+1skip / smarthome 294 / comms 272 / data 241 green. Repo
  dc6262c..(this commit) clean tree.
- Also remaining: (a) owner deferred end-of-line testing (this repo's
  DEPLOYMENT.md verification order — including the transfer CLI
  fetch/push cycle); (b) publication (owner; rotation precedes
  publication for every repo); (c) nextcloud supersession migration
  AFTER Gate 8 + rotation (core MIGRATION_NOTES.md).

## Stage 5 (next session's opener — Nasser, 2026-09-18: "prepare for
## stage 5. After reset.")

- Next session starts by drafting the Stage 5 goal contract (LaTeX →
  PDF via latex_build.py, the suite pattern: goal contract first, then
  implementation on Nasser's gate).
- Candidate scope items the draft must resolve (present candidates
  with a recommended cut; DO NOT pick unilaterally):
  1. Approval-gateway adapter wiring per broker (register_adapter is
     core; adapters are per-broker; the data broker has none wired).
  2. OneDrive live-tenant wiring (deployment-session task deferred in
     S4-4: msal-backed token provider installs at config time).
  3. Marked scratch-integration stragglers: scratch-Dendrite test for
     comms, scratch-HA test for smarthome (bring up scratch_servers/,
     run marked tests).
  4. Owner end-of-line testing support (DEPLOYMENT.md verification
     orders across the suite, including this repo's transfer CLI
     fetch/push cycle).
  5. Possibly: the nextcloud supersession decision (NOT the migration
     itself — that stays after Gate 8 + rotation per the Stage 1g
     finding; only the decision surface could belong here).
- Nothing is committed for Stage 5 yet; no contract exists.
- Fresh-context supervisory validation pass was dispatched at reset
  (background); its verdict lands in the record and gates Stage 5
  execution.
- The nextcloud supersession migration is NOT in this stage (after
  Gate 8 + rotation; core MIGRATION_NOTES.md holds the migration cost
  record).
- Supervisory AI review of all completed stages follows the stage
  closure, on the full stage record.