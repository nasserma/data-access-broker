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

from access_broker_core.audit import AuditLog
from access_broker_core.baselines import BaselineEngine
from access_broker_core.grants import GrantStore

from data_broker import config, policy, tools
from data_broker.backends.onedrive import GraphAccount, GraphDriveBackend, StaticTokenProvider
from data_broker.backends.webdav import WebDAVAccount, WebDAVBackend
from data_broker.config import Config, ConfigError
from data_broker.server import build_server, validate_bind_host

logger = logging.getLogger(__name__)

_SWEEP_INTERVAL_SECONDS = 60


def _utc_clock() -> datetime:
    return datetime.now(UTC)


def load_and_validate(path: str) -> Config:
    """Load config and re-check the token guards (defense in depth)."""
    cfg = config.load_config(path)
    if cfg.tokens is None:
        raise ConfigError("tokens: section is required (two-token model)")
    validate_bind_host(cfg.bind_host)
    if len(cfg.tokens.transfer_token) < cfg.tokens.min_length:
        raise ConfigError("tokens: transfer token shorter than min_length")
    if cfg.tokens.agent_token == cfg.tokens.transfer_token:
        raise ConfigError("tokens: transfer token must differ from the agent token")
    return cfg


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
            accounts=graph_accounts, token_provider=StaticTokenProvider()
        )
        for entry in onedrive_entries:
            accounts_map[entry["name"]] = "onedrive"
    if not backends:
        raise ConfigError("accounts: no backend could be constructed")
    return backends, accounts_map


def _boot(cfg: Config) -> tuple:
    """Synchronous boot: construct everything in the contract order.

    Returns (server, ctx, transport_params); any failure raises
    (refuse-to-start).
    """
    validate_bind_host(cfg.bind_host)
    if cfg.tokens is None:
        raise ConfigError("tokens: section is required (two-token model)")

    audit = AuditLog(cfg.storage["audit_log"], clock=_utc_clock)
    backends, accounts_map = _build_backends(cfg)
    registry = policy.build_registry()
    registry.validate()  # refuse-to-start: an unregistered wall must not serve
    store = GrantStore(cfg.storage["grants_db"], clock=_utc_clock, registry=registry)
    baselines = BaselineEngine(store, clock=_utc_clock)

    gateway = None
    if cfg.gateway is not None:
        from access_broker_core.gateways import build_gateway  # noqa: PLC0415

        gateway = build_gateway(cfg.gateway, store, _utc_clock, audit=audit)

    ctx = tools.BrokerContext(
        store=store,
        baselines=baselines,
        audit=audit,
        backends=backends,
        accounts=accounts_map,
        registry=registry,
        core=(gateway[0] if gateway is not None else None),
    )

    server = build_server(cfg, ctx)
    return server, ctx, (cfg.bind_host, cfg.bind_port)


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
) -> None:
    """Serve MCP; run the gateway + sweep loop alongside; clean shutdown."""
    host, port = bind
    background: list[asyncio.Task] = []
    if adapter is not None:
        background.append(asyncio.ensure_future(adapter.start()))
    if core is not None:
        background.append(asyncio.ensure_future(_sweep_loop(core)))
    try:
        if transport == "stdio":
            await server.run_stdio_async()
        else:
            await server.run_streamable_http_async(host=host, port=port)
    finally:
        await _shutdown(background, adapter, {})


def main() -> None:
    """Boot the broker; raises (refuse-to-start) on any validation failure."""
    path = os.environ.get("DATABROKER_CONFIG", "config.yaml")
    cfg = load_and_validate(path)
    server, _ctx, bind = _boot(cfg)
    asyncio.run(_serve(server, cfg.transport, bind))


def boot(config_path: str) -> tuple[object, object]:
    """Programmatic boot used by tests/wrappers."""
    cfg = load_and_validate(config_path)
    server, ctx, _bind = _boot(cfg)
    return server, ctx
