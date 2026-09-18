# Changelog

All notable changes to this project are documented in this file.
Format based on Keep a Changelog; versioning is SemVer.

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