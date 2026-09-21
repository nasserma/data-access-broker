# data-access-broker

Tiered, human-gated MCP access broker for file and data stores across
providers, built on access-broker-core. Fifth broker in the suite,
third built on the core. v2 lineage of nextcloud-access-broker: it
inherits that broker's design discipline (the D5 dual-surface transfer
model, content-addressed staging with sha verification, the
path-checker rule set) and receives the security architecture through
the core.

## Why access brokers

The Model Context Protocol connects AI agents to real infrastructure:
file stores, chat platforms, mail servers, a home. Its permission model
is static. An MCP server declares a list of tools; the client shows the
list to the user once, at install time; the user approves; from then on,
every tool call runs with the server's full credential.

The model cannot express what matters once the infrastructure is real.

- A grant is all-or-nothing at install time. There is no scoped, expiring
  grant for one folder, one room, or one lock. Approving the server
  approves everything it can reach.
- There is no per-operation review. After install, a destructive write
  runs as freely as a harmless read.
- There is no audit of agent decisions. What the agent requested, what
  was refused, and who approved what is recorded nowhere the owner
  controls.
- There is no human gate for sensitive actions. The only approval
  happened between the agent and the client, before any concrete
  operation existed.
- The credential at the server carries everything the server can do.
  Tool descriptions that promise restraint are advisory; the token is
  not.

An access broker is an MCP server that closes these gaps for one domain.
It holds the real credential, exposes a deliberately small tool surface,
and turns access into an explicit object: a scoped, expiring, audited,
human-approved grant, evaluated per operation.

## The access broker model

The suite implements one model with seven properties. Every broker
carries all of them; the mechanics live in access-broker-core.

1. A tier model per domain. Every operation is classified before it can
   be requested. T0/T1 are free-lane reads and reversible actions that
   run without a grant. T2 operations are sensitive and require
   per-operation human approval through an approval gateway on a chat
   platform; the agent cannot self-approve, and batches do not exist.
   T3 operations are always gated and, where the action is irreversible,
   are never offered as tools at all.

2. Declared operations. Each tool names the operation it performs. The
   wall, the broker's scope checker, validates the resource and the
   operation against active grants before anything executes. The free
   lane is defined by the domain tier table, not by what happens to be
   exposed.

3. Write-before-operate, hash-chained audit. Every request, execution,
   refusal, and human decision is an audit-class event written to a
   hash-chained log before the operation runs. `verify_chain` detects
   tampering, including truncation. The chain, not memory, is the record
   of what the agent did and why.

4. Fail-closed everywhere. Any configuration or validation failure
   refuses the boot. Unknown or malformed input denies. A restart never
   widens capability and never promotes pending or suspended state.

5. Approval separation. The approval gateway account is never a brokered
   account. A broker that could approve its own requests is a design
   failure, and the configuration is refused at boot.

6. Custody declarations. The broker declares, machine-readably, what its
   backend credentials can reach. An undeclared trust boundary refuses
   to start, and the custody class rides every audit record.

7. The D5 two-surface model in the data domain. The agent surface
   handles metadata and reasoning; bulk bytes move through a separate
   transfer surface addressed only by a CLI holding its own token, with
   SHA-256 verify-then-write. File content never enters LLM context.

## The suite

[access-broker-core](https://github.com/nasserma/access-broker-core)
carries the mechanics: the grant store (conditional-SQL CAS state
machine), policy wall evaluation, the hash-chained audit log, the
approval-gateway substrate, the custody registry, the baseline engine,
and transport authentication. Each broker is a thin domain layer: a
scope-checker wall, backend adapters, a tier table, and tool surfaces.
One repo per broker; no monorepo.

| Broker | Domain | Repository |
|---|---|---|
| data-access-broker | WebDAV and OneDrive file stores | https://github.com/nasserma/data-access-broker |
| communications-access-broker | Matrix and Microsoft Teams chat | to be published |
| groupware-access-broker | Mail, calendar, contacts, tasks over IMAP/SMTP, CalDAV/CardDAV, MS Graph | to be published |
| automation-access-broker | Physical automation: Home Assistant entities and services, extendable to any actuated device | to be published |

This repo's distinguishing domain property is the D5 dual-surface
model, the seventh suite property above. It exists because of the
adversary: file content is LLM-reachable string data, and hostile
content dropped into a store is a prompt-injection vector against any
connected client. The data domain is where the two-surface separation
was born (see SECURITY.md).

## The two surfaces

### The agent surface (/mcp)

One MCP surface for reasoning over stores. Free-lane reads (T0/T1
list, read, check_access, list_accounts) and gated operations (T2
write, move, trash, mkdir) with metadata shapes only: names, sizes,
entry listings, envelope statuses. The agent requests access through
`request_access`, the human approves or refuses through the approval
gateway, and grants expire. `list_accounts` discovers the configured
stores, so a multi-instance deployment needs one agent connection.

### The transfer surface (/transfer) and the CLI

Bulk bytes never enter LLM context. The transfer surface is a second
MCP mount behind its own token, and the intended client for it is a
CLI, not an agent:

```
DATABROKER_TRANSFER_TOKEN=... python -m data_broker.cli fetch nextcloud-personal Documents/report.pdf
DATABROKER_TRANSFER_TOKEN=... python -m data_broker.cli push nextcloud-personal Documents/report.pdf ./report.pdf
```

The CLI stages content-addressed files and verifies SHA-256 at both
ends; a transfer-surface write refuses without an active grant
(request access first, never retry as-is) and refuses on any hash
mismatch before the backend call (verify-then-write). The transfer
surface's read is grant-scoped even though the agent surface's read is
free: file content is bulk-sensitive and does not ride the free lane.

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

## Backends

- **WebDAV** (the generic reference implementation): Nextcloud,
  ownCloud, or any DAV server; per-store named accounts; class-1
  capability probing at connect (refuse-to-connect on non-DAV).
- **OneDrive**: MS Graph drives via the msal/httpx stack (raw Graph
  REST over httpx, MSAL token providers; the same token boundary
  family as the groupware broker); recorded-fixture testing with
  live-tenant wiring deferred to a deployment session.

## Layout

    data_broker/
      config.py        YAML + env-indirected secrets; fail-closed boot
      policy.py        the wall: cross-provider path scoping + tier table
      token_providers.py  MSAL provider boundary (S6-2b)
      backends/        base.py (interface), webdav.py, onedrive.py
      gateways/        matrix gateway adapter (core factory)
      tools.py         MCP tool surfaces (agent + transfer)
      cli.py           the transfer CLI (fetch / push)
      server.py        MCPServer wiring, dual-surface routes
      run.py           boot ordering, refuse-to-start guards
    tests/             unit + integration + boot-path suites
    scratch_servers/   scratch WebDAV container

## Status

v0.1.1, deployed in production on a multi-instance Nextcloud
deployment (2026-09-20) and verified end-to-end live: boot, both MCP
surfaces, free-lane reads, the human-gated Tier-2 cycle (request →
approval room → execute), grant revocation, and SHA-verified
transfer-CLI round trips across three configured stores. Agents
discover configured accounts via the list_accounts tool; supervision
runs as a user-space systemd service (see DEPLOYMENT.md section 7 for
the proven unit, including the refuse-to-start-not-crash-loop
behavior). Supersession of the v1 nextcloud-access-broker is a
separate migration task (see core MIGRATION_NOTES.md).

## Security

SECURITY.md carries the domain threat model: the primary adversary is
hostile file content (prompt injection through store content), the
two-token separation is refuse-to-start enforced at load and at boot,
and bulk content never enters LLM context. The suite invariants are
stated once in the core README and inherited here.

## Provenance

Author and maintainer: Nasser Mohieddin Abukhdeir. The primary
implementation model was GLM (glm-5.3), with glm-5.3-flash as the
delegated sub-agent model. See AUTHORS.md for the full provenance
chain.

## License

GPL-3.0-or-later.