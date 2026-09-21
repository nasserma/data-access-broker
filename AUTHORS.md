# Authors and provenance

**data-access-broker** — Nasser Mohieddin Abukhdeir
(Nasser Mohieddin Abukhdeir, 2026).

Maintainer and author: Nasser Mohieddin Abukhdeir.

## Provenance chain

This broker is the fifth member of the access-broker suite. The design
lineage, in order:

1. `nextcloud-access-broker` — the reference implementation: request →
   approval → scoped expiring grant → audited transfer; D5 dual-surface
   CLI with two-token separation; content-addressed staging with sha
   verification; discovery mode. The data broker's transfer-surface and
   path-checker patterns are ported from it (GPL-3.0-or-later).
2. `groupware-access-broker` — the first generalization:
   transport-agnostic core patterns, the msal/msgraph stack validated
   here and reused by the OneDrive backend.
3. `access-broker-core` — the shared security architecture (tier
   evaluation, grant lifecycle, hash-chained audit, transport auth,
   approval-gateway substrate). The data broker consumes it as an
   editable path dependency; the grant store it consumes carries the
   "resource + operation" generalization and the baseline engine,
   landed in the core during Stage 4 (the December 2026 generalization
   window's vehicle, pulled forward on the owner's instruction).
4. `automation-access-broker` — per-operation tier gating discipline
   (T1 comfort free, T2 physical gated, T3 admin gated), boot-path
   regression suite pattern.
5. `communications-access-broker` — gate porting pattern (read-
   without-grant refuses; gated-without-grant submits and pends),
   backend fixture-recording pattern.

All ported patterns retain their GPL-3.0-or-later licensing; see
AUTHORS.md and the module docstrings in the source repos for
line-by-line provenance.