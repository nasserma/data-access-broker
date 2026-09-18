# Security Policy / Threat Model

Status: v1. The threat model follows the suite pattern (see
access-broker-core README invariants). The data domain's distinguishing
property: the adversary surface is whatever data landed in a store -
including content no human vetted before it arrived.

## Primary adversary: hostile file content

File names, listing entries, and (on the transfer surface) file
contents are LLM-reachable strings. An attacker who controls a
document dropped into a shared store can attempt prompt injection
against any MCP client connected to this broker. This is the
nextcloud-access-broker adversary class, inherited whole; the D5
dual-surface separation exists because of it.

Design consequences:

1. **Deny-by-default wall.** Every call passes tier classification +
   store-node scoping through the core mechanics; the normalizer
   percent-decodes bounded, refuses traversal, and fails closed on any
   malformed input. The wall never raises; it denies with a fixed
   reason.
2. **The declared-operation vocabulary is the validation set.** Undeclared
   operations classify GATED (fail closed). T2 operations (write, move,
   trash, mkdir) are gated per operation; baseline definitions whose
   scope carries a T2 operation are refused at creation - tier
   classification wins over scope matching.
3. **Bulk content never enters LLM context.** File contents move only
   through the transfer surface behind its own token, addressed by the
   CLI; the agent surface returns metadata shapes only. Base64-through-
   context is a design flaw, not an optimization.
4. **Two-token separation is refuse-to-start.** A missing, short, or
   agent-equal transfer token refuses the boot (config + boot re-check).
   A broker that boots without the guards cannot serve.
5. **Bounded approval context.** Approval summaries are
   template-controlled; raw file content is never what the human
   approves on.

## Baseline-engine security properties

The baseline engine lands its spec (design note rev 2, findings
F-A..F-L) in the core; this broker consumes it. The properties with
teeth:

- **No usage-based auto-renewal.** Usage data populates the
  reconfirmation request; the human is the only renewal source.
- **Suspension is recoverable, never destructive.** Silence after
  grace suspends (indefinite by default); restart never promotes a
  suspended or pending baseline to ACTIVE; restoration is pinned to
  the exact definition_hash.
- **Definitions are DB-only and mutation-gated.** Every definition
  change runs through the gated request path with
  config_change_request_id provenance; config.yaml stays owner-authored
  policy only. An agent that could edit definitions would hold a
  self-service privilege escalation with a review calendar attached.
- **Budget report.** Per-baseline age, usage, principals, state against
  the owner-set standing budget; approval fatigue is the failure mode
  this design erodes by, and the visible budget is the counter-pressure.
- **Supervisory-AI principal: designed, NOT recommended.** The engine
  supports the principal type (T1-capped, non-delegable, structurally
  separate from definition writes); the documented posture is to leave
  it off.

## Secondary threats

- **Compromised MCP client:** tier OAuth audience binding (RFC 8707)
  on HTTP; constant-time credential checks on stdio (from core).
- **Gateway account compromise:** sender allowlist, room pinning; no
  silent renewal exists.
- **Local attacker:** secrets in the environment only (env-indirected
  in config; secret scrubbing in audit); staging directories 0700/0600.
- **Network exposure:** wildcard bind host is refuse-to-start; the
  transfer surface is addressed by the CLI holding its own token only.

## Deployment prerequisites

1. Store credentials must be policy-restricted: a dedicated app
   password / Entra registration scoped to the stores the broker
   serves, not a human account (the data broker reads and writes; a
   human-credential reuse would make the audit trail's principal
   meaningless).
2. OneDrive token custody is a deployment-session task: the msal-backed
   provider installs at config time; the backend never imports msal.
3. Versioning-dependent write policy: direct writes where the backend
   versions (OneDrive, Nextcloud versions); unversioned stores are
   fail-closed for the write policy, documented in DEPLOYMENT.md, not
   improvised.

## Reporting

Personal-project alpha; report suspected vulnerabilities directly to
the author (see AUTHORS.md).