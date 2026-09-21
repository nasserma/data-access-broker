"""Entry point (S4-5): boot ordering and refuse-to-start guards.

Boot order (the suite contract, fail at the first failure; no backend
connection opens until everything validates):

    config -> bind-host guard -> TOKEN guards (missing/short/equal) ->
    AuditLog (core) -> GrantStore (core, registry-bound) ->
    BaselineEngine (core-backed facade) -> backends -> gateway (core
    factory) -> BrokerContext -> server.

The token guards are the D5 two-token model with teeth: refuse to boot
when the transfer token is missing, short, or equal to the agent token
(goal contract section 4). Validated in config.py at load; re-checked
here so a programmatic boot cannot bypass the loader.
"""

# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from datetime import UTC, datetime
from typing import Any

import uvicorn
from access_broker_core.audit import AuditLog
from access_broker_core.baselines import BaselineEngine
from access_broker_core.custody import CustodyClass, CustodyRegistry
from access_broker_core.grants import GrantStore

from data_broker import config, policy, token_providers, tools
from data_broker.backends.onedrive import GraphAccount, GraphDriveBackend, StaticTokenProvider
from data_broker.backends.webdav import WebDAVAccount, WebDAVBackend
from data_broker.config import Config, ConfigError
from data_broker.server import build_dual_app, build_server, validate_bind_host

logger = logging.getLogger(__name__)

_SWEEP_INTERVAL_SECONDS = 60


def _utc_clock() -> datetime:
    return datetime.now(UTC)


def load_and_validate(path: str) -> Config:
    """Load config and check the token section is present.

    All token guards (missing env, short transfer token, equal tokens,
    and the missing-section refusal) live in config._parse_tokens and
    are fail-closed at load — duplicated re-checks here would be
    unreachable dead code shadowed by config's guards (fresh-context
    review finding; the earlier tokens-None arm here was deleted for
    the same reason: load_config refuses a missing section before this
    function could ever see tokens=None). The tokens-None refusal that
    remains reachable is _boot's re-check for a Config constructed
    programmatically without a tokens section.
    """
    cfg = config.load_config(path)
    validate_bind_host(cfg.bind_host)
    return cfg


def _onedrive_provider(cfg: Config, entries: list[dict]) -> StaticTokenProvider:
    """Config-gated token-provider selection (S6-2b): any entry carrying
    ``token_provider: msal`` selects the production MsalTokenProvider
    (per the first such entry's client_id/authority; the provider is
    per-deployment); the default remains StaticTokenProvider (tests and
    scratch, no network, no tenant). No tenant contact happens here."""
    for entry in entries:
        provider = token_providers.provider_from_config(entry, cfg.storage["data_dir"])
        if provider is not None:
            return provider  # type: ignore[return-value] - same TokenProvider boundary
    return StaticTokenProvider()


def _build_backends(cfg: Config) -> tuple[dict[str, object], dict[str, str]]:
    backends: dict[str, object] = {}
    accounts_map: dict[str, str] = {}
    webdav_entries = cfg.accounts.get("webdav", [])
    if webdav_entries:
        accounts = {
            entry["name"]: WebDAVAccount(
                name=entry["name"],
                url=entry["url"],
                username=entry["username"],
                password_env=entry["password_env"],
                verify_ssl=bool(entry.get("verify_ssl", True)),
            )
            for entry in webdav_entries
        }
        backends["webdav"] = WebDAVBackend(accounts=accounts)
        for entry in webdav_entries:
            accounts_map[entry["name"]] = "webdav"
    onedrive_entries = cfg.accounts.get("onedrive", [])
    if onedrive_entries:
        graph_accounts = {
            entry["name"]: GraphAccount(
                name=entry["name"],
                tenant_id=entry["tenant_id"],
                client_id=entry["client_id"],
                token_env=entry["token_env"],
                allow_write=bool(entry.get("allow_write", False)),
            )
            for entry in onedrive_entries
        }
        backends["onedrive"] = GraphDriveBackend(
            accounts=graph_accounts, token_provider=_onedrive_provider(cfg, onedrive_entries)
        )
        for entry in onedrive_entries:
            accounts_map[entry["name"]] = "onedrive"
    if not backends:
        raise ConfigError("accounts: no backend could be constructed")
    return backends, accounts_map


def build_custody_registry() -> CustodyRegistry:
    """The data broker custody declarations (S6-2): the trust boundary
    of every backend family, declared at boot.

    webdav: app-password credential scoped to the store (SCOPED).
    onedrive: delegated Graph token under the app registration with
    minimal scopes (SCOPED) --- the closest to properly scoped
    credentials in the suite. The empty registry refuses to start
    (CustodyRegistry.validate), so an undeclared trust boundary can
    never serve.
    """
    registry = CustodyRegistry()
    registry.register("webdav", CustodyClass.SCOPED)
    registry.register("onedrive", CustodyClass.SCOPED)
    registry.validate()
    return registry


def _boot(cfg: Config) -> tuple:
    """Synchronous boot: construct everything in the contract order.

    Returns (server, ctx, bind, gateway); any failure raises
    (refuse-to-start). gateway is the (core, adapter) tuple when a
    ``gateway:`` section is configured, else None.
    """
    validate_bind_host(cfg.bind_host)
    if cfg.tokens is None:
        raise ConfigError("tokens: section is required (two-token model)")

    audit = AuditLog(
        cfg.storage["audit_log"],
        clock=_utc_clock,
        secrets=[
            # Secret scrubbing (Stage 8 F-2 fix): refuse to record any
            # credential-shaped value the broker holds — the two MCP
            # surface bearer tokens plus every WebDAV account password.
            cfg.tokens.agent_token,
            cfg.tokens.transfer_token,
            *[
                value
                for entry in cfg.accounts.get("webdav", [])
                if (value := os.environ.get(entry.get("password_env", ""), ""))
            ],
        ],
    )
    backends, accounts_map = _build_backends(cfg)
    registry = policy.build_registry()
    registry.validate()  # refuse-to-start: an unregistered wall must not serve
    store = GrantStore(cfg.storage["grants_db"], clock=_utc_clock, registry=registry)
    baselines = BaselineEngine(store, clock=_utc_clock)

    gateway = None
    if cfg.gateway is not None:
        from data_broker.gateways import register_gateway_adapters  # noqa: PLC0415

        register_gateway_adapters()
        from access_broker_core.gateways import build_gateway  # noqa: PLC0415

        gateway = build_gateway(cfg.gateway, store, _utc_clock, audit=audit)

    ctx = tools.BrokerContext(
        store=store,
        baselines=baselines,
        audit=audit,
        backends=backends,
        accounts=accounts_map,
        registry=registry,
        custody=build_custody_registry(),
        core=(gateway[0] if gateway is not None else None),
    )

    server = build_server(cfg, ctx)
    return server, ctx, (cfg.bind_host, cfg.bind_port), gateway


def _sweep_loop(core) -> Any:
    """Housekeeping loop; failures logged, never fatal."""

    async def _loop() -> None:
        while True:
            await asyncio.sleep(_SWEEP_INTERVAL_SECONDS)
            try:
                await core.sweep()
            except Exception:  # noqa: BLE001 - housekeeping never kills serving
                logger.exception("gateway sweep failed")

    return _loop()


async def _shutdown(background: list, adapter, backends: dict) -> None:
    for task in background:
        task.cancel()
    if background:
        await asyncio.gather(*background, return_exceptions=True)
    if adapter is not None:
        with contextlib.suppress(Exception):
            await adapter.stop()
    for backend in backends.values():
        with contextlib.suppress(Exception):
            await backend.close()


async def _serve(
    server,
    transport: str,
    bind: tuple[str, int],
    adapter=None,
    core=None,
    dual_app=None,
    backends: dict | None = None,
    accounts_map: dict | None = None,
) -> None:
    """Serve MCP; run the gateway + sweep loop alongside; clean shutdown.

    HTTP transport: serve the DUAL app (agent /mcp + transfer /transfer,
    each behind its bearer token) with uvicorn; stdio: the agent surface
    only (no transfer surface exists without HTTP)."""
    host, port = bind
    # F1-parity fix (found live 2026-09-20 on production): backends were
    # constructed at boot but never connect()ed — every operation died on
    # 'backend not connected; call connect() first' while the S5-4
    # dry-run's manual connect() masked it. Connect every configured
    # account BEFORE serving (fail closed: an unreachable store refuses
    # the boot, never serves half-wired).
    if backends and accounts_map:
        for account, family in accounts_map.items():
            await backends[family].connect(account)
    background: list[asyncio.Task] = []
    if adapter is not None:
        background.append(asyncio.ensure_future(adapter.start()))
    if core is not None:
        background.append(asyncio.ensure_future(_sweep_loop(core)))
    try:
        if transport == "stdio":
            await server.run_stdio_async()
        elif dual_app is not None:
            config = uvicorn.Config(dual_app, host=host, port=port, log_level="info")
            u_server = uvicorn.Server(config)
            await u_server.serve()
        else:
            await server.run_streamable_http_async(host=host, port=port)
    finally:
        await _shutdown(background, adapter, {})


def main() -> None:
    """Boot the broker; raises (refuse-to-start) on any validation failure."""
    path = os.environ.get("DATABROKER_CONFIG", "config.yaml")
    cfg = load_and_validate(path)
    server, ctx, bind, gateway = _boot(cfg)
    core = gateway[0] if gateway is not None else None
    adapter = gateway[1] if gateway is not None else None
    dual = (
        build_dual_app(cfg, ctx, cfg.tokens)
        if cfg.transport != "stdio" and cfg.tokens is not None
        else None
    )
    asyncio.run(
        _serve(
            server,
            cfg.transport,
            bind,
            adapter=adapter,
            core=core,
            dual_app=dual,
            backends=ctx.backends,
            accounts_map=ctx.accounts,
        )
    )


def boot(config_path: str) -> tuple[object, object]:
    """Programmatic boot used by tests/wrappers."""
    cfg = load_and_validate(config_path)
    server, ctx, _bind, _gateway = _boot(cfg)
    return server, ctx


if __name__ == "__main__":
    main()
