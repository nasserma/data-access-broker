"""Config loader (S4-5): strict, fail-closed, on the suite pattern.

The two-token model is refuse-to-start enforced HERE: the transfer
token must be present, at least min_length characters, and different
from the agent token - a broker that boots without the guards cannot
serve. Secrets are env-indirected only (never config values).
"""

# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

import yaml

TOP_KEYS = {
    "bind_host",
    "bind_port",
    "transport",
    "auth",
    "storage",
    "accounts",
    "gateway",
    "tokens",
}
AUTH_KEYS = {"oauth", "stdio"}
STORAGE_KEYS = {"data_dir", "grants_db", "audit_log"}
TOKEN_KEYS = {"agent_env", "transfer_env", "min_length"}
WEBDAV_KEYS = {"name", "url", "username", "password_env", "verify_ssl"}
ONEDRIVE_KEYS = {
    "name",
    "tenant_id",
    "client_id",
    "token_env",
    "allow_write",
    "token_provider",
    "authority",
}
ENV_REF_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


class ConfigError(Exception):
    """Raised for any invalid or incomplete configuration (fail-closed)."""


def _reject_unknown_keys(raw: dict, allowed: set[str], where: str) -> None:
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {sorted(map(str, unknown))}")


def _require_str(raw: dict, key: str, where: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{where}: {key!r} must be a nonempty string")
    return value


def _validate_bind_host(host: str) -> str:
    """Wildcard binds are refuse-to-start (suite invariant)."""
    if host in {"0.0.0.0", "::", ""}:
        raise ConfigError(f"refusing wildcard bind host: {host!r}")
    return host


def _resolve_env_value(value: Any, where: str) -> Any:
    if isinstance(value, str):
        m = ENV_REF_RE.match(value)
        if m:
            resolved = os.environ.get(m.group(1))
            if not resolved:
                raise ConfigError(f"{where}: environment variable {m.group(1)!r} is not set")
            return resolved
    return value


@dataclass
class TokenConfig:
    agent_token: str
    transfer_token: str
    min_length: int = 32


@dataclass
class Config:
    bind_host: str
    bind_port: int
    transport: str
    auth: dict[str, Any] | None
    storage: dict[str, str]
    accounts: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    gateway: dict[str, Any] | None = None
    tokens: TokenConfig | None = None


_PORT_MAX = 65535
_DEFAULT_MIN_TOKEN_LENGTH = 32


def _parse_tokens(raw: Any) -> TokenConfig:
    if not isinstance(raw, dict):
        raise ConfigError("tokens: section must be a mapping")
    _reject_unknown_keys(raw, TOKEN_KEYS, "tokens")
    _require_str(raw, "agent_env", "tokens")
    transfer_env = _require_str(raw, "transfer_env", "tokens")
    min_length = raw.get("min_length", _DEFAULT_MIN_TOKEN_LENGTH)
    if not isinstance(min_length, int) or min_length <= 0:
        raise ConfigError("tokens: min_length must be a positive integer")
    agent = os.environ.get(agent_env_name := raw["agent_env"], "")
    if not agent_env_name:
        raise ConfigError("tokens: agent_env must be a nonempty string")
    if not agent:
        raise ConfigError(f"tokens: environment variable {agent_env_name!r} is not set")
    transfer = os.environ.get(transfer_env, "")
    if not transfer:
        raise ConfigError(f"tokens: environment variable {transfer_env!r} is not set")
    # The D5 guards, with teeth (goal contract section 4): missing ->
    # refused above; short -> refused; equal to the agent token ->
    # refused. A transfer token that equals the agent token collapses
    # the surface separation - the one guard the D5 model cannot
    # survive without.
    if len(transfer) < min_length:
        raise ConfigError(
            f"tokens: transfer token shorter than min_length={min_length}"
        )
    if agent == transfer:
        raise ConfigError("tokens: transfer token must differ from the agent token")
    return TokenConfig(
        agent_token=agent,
        transfer_token=transfer,
        min_length=min_length,
    )


def _parse_storage(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise ConfigError("storage: section must be a mapping")
    _reject_unknown_keys(raw, STORAGE_KEYS, "storage")
    return {
        k: _require_str(raw, k, f"storage[{k}]") for k in ("data_dir", "grants_db", "audit_log")
    }


def _parse_accounts(raw: Any) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(raw, dict):
        raise ConfigError("accounts: section must be a mapping")
    _reject_unknown_keys(raw, {"webdav", "onedrive"}, "accounts")
    out: dict[str, list[dict[str, Any]]] = {}
    for family, keys in (("webdav", WEBDAV_KEYS), ("onedrive", ONEDRIVE_KEYS)):
        entries = []
        for entry in raw.get(family) or []:
            if not isinstance(entry, dict):
                raise ConfigError(f"accounts.{family}: entries must be mappings")
            where = f"accounts.{family}[{entry.get('name', '?')}]"
            _reject_unknown_keys(entry, keys, where)
            name = _require_str(entry, "name", where)
            entry["name"] = name
            for key in keys - {"name", "verify_ssl", "allow_write", "token_provider", "authority"}:
                _require_str(entry, key, where)
            if entry.get("token_provider") is not None:
                tp = entry["token_provider"]
                if tp != "msal":
                    raise ConfigError(
                        f"{where}: token_provider must be 'msal' when present (the "
                        "default StaticTokenProvider is selected by omitting the key)"
                    )
            if entry.get("token_provider") == "msal":
                if not entry.get("authority"):
                    raise ConfigError(
                        f"{where}: token_provider msal requires 'authority' (the Entra "
                        "authority URL, e.g. https://login.microsoftonline.com/<tenant>)"
                    )
            elif entry.get("authority") is not None:
                raise ConfigError(
                    f"{where}: 'authority' is only valid with token_provider msal"
                )
            entries.append(entry)
        if entries:
            out[family] = entries
    if not out:
        raise ConfigError("accounts: at least one store account is required")
    # Cross-family AND within-family name uniqueness (the H2 invariant,
    # suite pattern): key membership and identity are different
    # invariants - two accounts with the same name both parse, and the
    # boot's accounts maps (dict comprehensions) keep the LAST one,
    # silently routing requests to the wrong instance or family. Fail
    # closed at load.
    seen: dict[str, str] = {}
    for family, entries in out.items():
        for entry in entries:
            name = entry["name"]
            if name in seen:
                raise ConfigError(
                    f"accounts: duplicate account name {name!r} "
                    + (
                        f"in {family} (and previously in {seen[name]})"
                        if seen[name] != family
                        else f"in {family}"
                    )
                    + " (account names must be unique across all backend families)"
                )
            seen[name] = family
    return out


def _parse_auth(raw: Any, transport: str) -> dict[str, Any] | None:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("auth: section must be a mapping")
    _reject_unknown_keys(raw, AUTH_KEYS, "auth")
    oauth = raw.get("oauth")
    if transport == "http" and oauth is None:
        raise ConfigError("transport http requires auth.oauth")
    return {"oauth": oauth}


def load_config(path: str) -> Config:
    """Load and validate configuration from a YAML file (fail-closed)."""
    try:
        with open(path, encoding="utf-8") as fh:  # noqa: PTH123 - small loader
            raw = yaml.safe_load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"config file {path} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("config must be a mapping")
    _reject_unknown_keys(raw, TOP_KEYS, "config")

    bind_host = _validate_bind_host(_require_str(raw, "bind_host", "config"))
    bind_port = raw.get("bind_port", 8471)
    if not isinstance(bind_port, int) or not (1 <= bind_port <= _PORT_MAX):
        raise ConfigError("config: bind_port must be an integer in 1..65535")
    transport = _require_str(raw, "transport", "config")
    if transport not in ("stdio", "http"):
        raise ConfigError(f"transport must be stdio|http, got {transport!r}")

    tokens = _parse_tokens(_resolve_env_value(raw.get("tokens"), "tokens"))
    storage = _parse_storage(_resolve_env_value(raw.get("storage"), "storage"))
    accounts = _parse_accounts(_resolve_env_value(raw.get("accounts"), "accounts"))
    # password/token envs resolve at boot (fail closed)
    for entry in accounts.get("webdav", []):
        if not os.environ.get(entry["password_env"]):
            raise ConfigError(
                f"accounts.webdav[{entry['name']}]: environment variable "
                f"{entry['password_env']!r} is not set"
            )
    for entry in accounts.get("onedrive", []):
        if not os.environ.get(entry["token_env"]):
            raise ConfigError(
                f"accounts.onedrive[{entry['name']}]: environment variable "
                f"{entry['token_env']!r} is not set"
            )

    gateway_raw = _resolve_env_value(raw.get("gateway"), "gateway")
    gateway = None
    if gateway_raw is not None:
        if not isinstance(gateway_raw, dict) or not gateway_raw:
            raise ConfigError("gateway: section must be a mapping with exactly one adapter key")
        gateway = gateway_raw

    return Config(
        bind_host=bind_host,
        bind_port=bind_port,
        transport=transport,
        auth=_parse_auth(_resolve_env_value(raw.get("auth"), "auth"), transport),
        storage=storage,
        accounts=accounts,
        gateway=gateway,
        tokens=tokens,
    )
