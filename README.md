# data-access-broker

Tiered, human-gated MCP access broker for file and data stores across
providers. Fifth broker in the access-broker suite, third built on
[access-broker-core](../accessBrokerCore). v2 lineage of
nextcloud-access-broker: it inherits that broker's design discipline
(the D5 dual-surface transfer model, content-addressed staging with
sha verification, the path-checker rule set) and receives the security
architecture (tier evaluation, grant lifecycle, hash-chained audit,
transport auth, approval-gateway substrate) through the core.

## What it does

An AI agent requests scoped, time-limited access to file stores; a
human approves or refuses each request through the approval gateway;
approved grants expire; every operation is written to a hash-chained
audit log before it runs. Bulk file content never enters the LLM
context: it moves through a separate transfer surface, addressed by a
CLI holding its own token, with content-addressed staging and sha
verification at both ends.

v1 backends:

- **WebDAV** (the generic reference implementation): Nextcloud,
  ownCloud, or any DAV server; per-store named accounts; class-1
  capability probing at connect (refuse-to-connect on non-DAV).
- **OneDrive**: MS Graph drives via the msal/httpx stack (raw Graph
  REST over httpx, MSAL token providers; the same token boundary
  family as the groupware broker); recorded-fixture testing with
  live-tenant wiring deferred to a deployment session.

## The wall

Scope objects are (backend, account, resource, operation). A resource
is a normalized store node (path-like components); an operation is a
declared operation from the broker's registered vocabulary (read,
write, list, move, trash, mkdir). Evaluation is tier-first and
normative: tier classification first, gated tiers always require a
grant, T0/T1 may be covered by an active baseline, grants are the
fallback, suspended baselines match nothing. The checker fails closed
on every malformed input.

## Baseline permissions

Standing read baselines (T0/T1 only, never T2/T3) for genuinely
constant access patterns, deliberately harder to create than a grant
and continuously reassessed: config-defined intervals, reconfirmation
requests that carry usage data, a grace period (default 72h) after
which silence suspends rather than revokes, restoration pinned to the
exact definition hash, and a visible standing budget. No usage-based
auto-renewal: the human is the only renewal source. Definitions live
in the store, never in config, and every mutation is itself gated.

## Layout

    data_broker/
      config.py        YAML + env-indirected secrets; fail-closed boot
      policy.py        the wall: cross-provider path scoping + tier table
      token_providers.py  MSAL provider boundary (S6-2b)
      backends/        base.py (interface), webdav.py, onedrive.py
      gateways/        matrix gateway adapter (core factory)
      tools.py         MCP tool surfaces (agent + transfer)
      server.py        MCPServer wiring, dual-surface routes
      run.py           boot ordering, refuse-to-start guards
    tests/             unit + integration + boot-path suites
    scratch_servers/   scratch WebDAV container

## Status

Stage 4 of the suite plan; executes without owner testing per the
standing instruction (2026-09-18) — owner deferred testing is the
end-of-line gate. The deployed nextcloud-access-broker is untouched
through its Gate 8 sequence; supersession is a separate migration
task, decided only after both are stable (see core MIGRATION_NOTES.md).

## License

GPL-3.0-or-later. Author and maintainer: Nasser Mohieddin Abukhdeir.