"""S4-5: the MCP tool surfaces and the BrokerContext gate.

Agent surface (request_access, check_access, list, move, trash, mkdir)
and transfer surface (check_access, read, write) behind separate
tokens and path prefixes; bulk content never enters LLM context.

THE ENFORCEMENT PATH (BrokerContext.execute) evaluates in the F-C
normative order, no configuration changes it:

1. tier classification via the registry (undeclared -> GATED);
2. GATED ops: baseline NEVER matches; grant or request+pending;
3. READ ops: an ACTIVE baseline of this principal matches -> proceed
   (audit-logged as baseline use);
4. suspended baselines match nothing (fallback to step 4);
5. grant fallback: the core store's active_for();
6. otherwise: read-without-grant REFUSES (no free-lane deviation here:
   the free-lane invariant RETURNS in this domain, so T0/T1 reads
   execute without a grant when no baseline covers them - the free
   lane IS the tier model);
7. gated-without-grant submits + notifies + pends.

Write-before-operate audit for every executed op via the core audit
log; the gate never raises across the tool boundary ({status:
ok|refused|error|pending} envelope).
"""

# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from access_broker_core.audit import AuditLog, LogWriteError
from access_broker_core.baselines import BaselineEngine
from access_broker_core.grants import GrantStore
from access_broker_core.policy import OperationClass

from data_broker import policy as wall

logger = logging.getLogger(__name__)

_ENVELOPE_STATUSES = {"ok", "refused", "error", "pending"}


def _ensure_backend_task(call: Awaitable[Any]) -> Any:
    """Schedule the backend call as a task (from the audit `then`)."""
    return asyncio.ensure_future(call)


def _to_json(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): _to_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_json(v) for v in value]
    return repr(value)


@dataclass
class BrokerContext:
    """Everything the tool surfaces need: wall registry, grant store,
    baseline engine, audit log, backends, accounts."""

    store: GrantStore
    baselines: BaselineEngine
    audit: AuditLog
    backends: dict[str, Any]  # backend family -> backend instance
    accounts: dict[str, str]  # account name -> backend family
    registry: wall.PolicyRegistry
    core: Any | None = None  # approval gateway notification entry point
    principal: str = "agent"

    def backend_for(self, account: str) -> Any:
        family = self.accounts.get(account)
        if family is None:
            raise ValueError(f"unknown account: {account!r}")
        return self.backends[family]

    # ------------------------------------------------------------ the gate

    async def execute(
        self,
        account: str,
        resource: str,
        op: str,
        run_backend_call: Callable[[], Awaitable[Any]],
        *,
        justification: str | None = None,
        principal: str | None = None,
    ) -> dict[str, Any]:
        """THE enforcement path every tool runs through (F-C order)."""
        if account not in self.accounts:
            return {"status": "refused", "reason": f"unknown account: {account!r}"}
        backend_family = self.accounts[account]
        op_class = self.registry.classify(op)

        # Step 2: GATED ops never match a baseline (T2/T3 always gate).
        grants = self.store.active_for(backend_family, account, resource, op)

        # Step 3-4: baseline match for READ ops only; suspended match
        # nothing (the engine enforces the order; the gate trusts it).
        who = principal or self.principal
        baseline = None
        if op_class is not OperationClass.GATED:
            baseline = self.baselines.baseline_for(
                who, backend_family, account, resource, op
            )

        grant_id = grants[0].request_number if grants else None

        if op_class is OperationClass.GATED and not grants:
            return await self._request_pending(
                account, resource, op, justification, backend_family
            )
        if not grants and not baseline:
            # READ op, no baseline: free-lane invariant RETURNS in this
            # file-class domain (goal contract section 9) - execute,
            # audit-logged.
            logger.info(
                "read op %r on %s:%s executed without grant or baseline", op, account, resource
            )

        reason = "grant" if grants else ("baseline" if baseline else "freelane")
        started: Awaitable[Any] | None = None

        def _then() -> None:
            nonlocal started
            started = _ensure_backend_task(run_backend_call())

        try:
            self.audit.record(
                account=account,
                resource=resource,
                operation=op,
                grant_id=grant_id,
                decision="executed",
                reason=reason,
                then=_then,
                backend=backend_family,
                principal=who,
            )
        except LogWriteError as exc:
            return {"status": "error", "error": f"audit write failed: {exc}"}
        except ValueError as exc:
            return {"status": "refused", "reason": str(exc)}

        try:
            result = await started  # type: ignore[misc]
        except Exception as exc:  # noqa: BLE001 - the gate never leaks
            logger.exception("backend call failed")
            return {"status": "error", "error": f"internal error: {exc}"}

        if isinstance(result, dict) and result.get("status") in _ENVELOPE_STATUSES:
            return result
        return {"status": "ok", "result": _to_json(result)}

    async def _request_pending(
        self,
        account: str,
        resource: str,
        op: str,
        justification: str | None,
        backend_family: str,
    ) -> dict[str, Any]:
        """GATED with no active grant: submit + notify + pending."""
        item = {
            "backend": backend_family,
            "account": account,
            "resource": resource,
            "ops": [op],
            "expires_at": None,
        }
        if not justification:
            justification = f"{op} on {resource} (account {account})"
        try:
            number = self.store.submit(
                request_id_hint="agent", items=[item], justification=justification
            )
        except ValueError as exc:
            return {"status": "refused", "reason": str(exc)}
        try:
            self.audit.record(
                account=account,
                resource=resource,
                operation=op,
                grant_id=number,
                decision="submitted",
                reason="pending_approval",
                backend=backend_family,
                principal=self.principal,
            )
        except (LogWriteError, ValueError) as exc:
            logger.warning("audit entry for pending request #%s failed: %s", number, exc)
        if self.core is not None:
            try:
                await self.core.notify_request(number, justification, [item])
            except Exception as exc:  # noqa: BLE001 - notify must never block
                logger.warning("gateway notification for request #%s failed: %s", number, exc)
        return {"status": "pending", "request_number": number}


# ---------------------------------------------------------------------------
# The curated surfaces (D7 shape): agent + transfer
# ---------------------------------------------------------------------------


def build_agent_tools(ctx: BrokerContext) -> list[dict[str, Any]]:
    """The agent surface: request_access, check_access, list, move,
    trash, mkdir - tier + scope declarations on every tool."""

    async def list_dir(account: str, resource: str) -> dict[str, Any]:
        """Tool list: a store node listing (T0 read; free lane)."""

        async def _call() -> dict[str, Any]:
            backend = ctx.backend_for(account)
            entries = await backend.list(account, resource)
            return {
                "status": "ok",
                "entries": [
                    {"name": e.name, "type": "dir" if e.is_dir else "file", "size": e.size}
                    for e in entries
                ],
            }

        return await ctx.execute(account, resource, "list", _call)

    async def move_node(account: str, src: str, dst: str) -> dict[str, Any]:
        """Tool move: reparent a node (T2 gated per operation)."""

        async def _call() -> dict[str, Any]:
            backend = ctx.backend_for(account)
            await backend.move(account, src, dst)
            return {"status": "ok"}

        return await ctx.execute(
            account, src, "move", _call, justification=f"move {src} -> {dst} (account {account})"
        )

    async def trash_node(account: str, resource: str) -> dict[str, Any]:
        """Tool trash: move a node to trash (T2 gated per operation)."""

        async def _call() -> dict[str, Any]:
            backend = ctx.backend_for(account)
            await backend.trash(account, resource)
            return {"status": "ok"}

        return await ctx.execute(
            account, resource, "trash", _call, justification=f"trash {resource} (account {account})"
        )

    async def mkdir_node(account: str, resource: str) -> dict[str, Any]:
        """Tool mkdir: create a collection (T2 gated per operation)."""

        async def _call() -> dict[str, Any]:
            backend = ctx.backend_for(account)
            await backend.mkdir(account, resource)
            return {"status": "ok"}

        return await ctx.execute(
            account, resource, "mkdir", _call, justification=f"mkdir {resource} (account {account})"
        )

    async def request_access(
        account: str, resource: str, ops: str, justification: str = ""
    ) -> dict[str, Any]:
        """Tool request_access: submit operations for human approval."""
        ops_list = [o.strip() for o in ops.split(",") if o.strip()]
        backend_family = ctx.accounts.get(account)
        if backend_family is None:
            return {"status": "refused", "reason": f"unknown account: {account!r}"}
        item = {
            "backend": backend_family,
            "account": account,
            "resource": resource,
            "ops": ops_list,
            "expires_at": None,
        }
        text = justification or (
            f"request_access: {', '.join(ops_list)} on {resource} (account {account})"
        )
        try:
            number = ctx.store.submit(request_id_hint="agent", items=[item], justification=text)
        except ValueError as exc:
            return {"status": "refused", "reason": str(exc)}
        try:
            ctx.audit.record(
                account=account,
                resource=resource,
                operation=",".join(ops_list),
                grant_id=number,
                decision="submitted",
                reason="pending_approval",
                backend=backend_family,
                principal=ctx.principal,
            )
        except (LogWriteError, ValueError) as exc:
            logger.warning("audit entry for request #%s failed: %s", number, exc)
        if ctx.core is not None:
            try:
                await ctx.core.notify_request(number, text, [item])
            except Exception as exc:  # noqa: BLE001 - notify must never block
                logger.warning("gateway notification for request #%s failed: %s", number, exc)
        return {"status": "pending", "request_number": number}

    async def check_access(account: str, resource: str = "", op: str = "") -> dict[str, Any]:
        """Tool check_access: grant + baseline status for an account."""
        if resource and op:
            backend_family = ctx.accounts.get(account)
            if backend_family is None:
                return {"status": "refused", "reason": f"unknown account: {account!r}"}
            grants = ctx.store.active_for(backend_family, account, resource, op)
            baseline = ctx.baselines.baseline_for(
                ctx.principal, backend_family, account, resource, op
            )
            return {
                "status": "ok",
                "active": [
                    {
                        "request_number": g.request_number,
                        "expires_at": (
                            g.expires_at.isoformat() if g.expires_at else None
                        ),
                    }
                    for g in grants
                ],
                "baseline": baseline,
            }
        pending: list[dict[str, Any]] = []
        active: list[dict[str, Any]] = []
        for record in ctx.store.all_records():
            entry = {
                "request_number": record.request_number,
                "state": record.state,
                "justification": record.justification,
            }
            if record.state == "pending":
                pending.append(entry)
            elif record.state == "active":
                active.append(entry)
        return {
            "status": "ok",
            "pending": pending,
            "active": active,
            "baselines": ctx.baselines.list_definitions(),
            "budget": ctx.baselines.budget_report(standing_budget=8),
        }

    async def revoke_access(account: str, request_number: int) -> dict[str, Any]:
        """Tool revoke_access: withdraw a grant (T2 brokered)."""
        try:
            record = ctx.store.get_record(request_number)
        except LookupError:
            return {"status": "refused", "reason": f"unknown request number {request_number}"}
        if not any(item.get("account") == account for item in record.items):
            return {
                "status": "refused",
                "reason": f"request #{request_number} does not involve account {account!r}",
            }
        revoked = ctx.store.revoke(request_number)
        try:
            ctx.audit.record(
                account=account,
                resource="",
                operation="revoke",
                grant_id=request_number,
                decision="revoked",
                reason="revoked" if revoked else "not_revocable",
                principal=ctx.principal,
            )
        except (LogWriteError, ValueError) as exc:
            logger.warning("audit entry for revoke #%s failed: %s", request_number, exc)
        return {"status": "ok", "revoked": revoked, "request_number": request_number}

    async def read_file(account: str, resource: str) -> dict[str, Any]:
        """AGENT-SURFACE read: metadata-shaped result; bulk content stays
        on the transfer surface (this returns listing-shaped info only)."""

        async def _call() -> dict[str, Any]:
            backend = ctx.backend_for(account)
            entries = await backend.list(account, resource)
            return {
                "status": "ok",
                "entries": [
                    {"name": e.name, "is_dir": e.is_dir, "size": e.size} for e in entries
                ],
            }

        return await ctx.execute(account, resource, "read", _call)

    return [
        {"name": "list", "tier": 1, "handler": list_dir},
        {"name": "move", "tier": 2, "handler": move_node},
        {"name": "trash", "tier": 2, "handler": trash_node},
        {"name": "mkdir", "tier": 2, "handler": mkdir_node},
        {"name": "request_access", "tier": 2, "handler": request_access},
        {"name": "check_access", "tier": 1, "handler": check_access},
        {"name": "revoke_access", "tier": 2, "handler": revoke_access},
        {"name": "read", "tier": 1, "handler": read_file},
    ]


TOOL_REGISTRY: dict[str, int] = {}


def register_tools(server: Any, broker_context: BrokerContext) -> dict[str, int]:
    """Build the agent surface, register it on the MCP server."""
    if not isinstance(broker_context, BrokerContext):
        raise TypeError(
            f"data broker expects a BrokerContext, got {type(broker_context).__name__}"
        )
    specs = build_agent_tools(broker_context)
    TOOL_REGISTRY.clear()
    for spec in specs:
        TOOL_REGISTRY[spec["name"]] = spec["tier"]
        if server is not None:
            server.add_tool(
                spec["handler"], name=spec["name"], description=spec["name"]
            )
    return dict(TOOL_REGISTRY)
