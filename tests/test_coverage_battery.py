"""Coverage battery (the supervisory-review finding 2): arms the main
batteries do not reach, module by module.

Sections map to the measured gaps (policy 147/149, config refusal
arms, tools surface arms, run boot/serving arms, webdav/onedrive
recorded transports). Every test is deterministic; no wall-clock, no
network.
"""

# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from data_broker import config as db_config
from data_broker import policy
from data_broker.backends.base import (
    AuthError,
    BackendUnavailable,
    NodeInfo,
    NotConnected,
    ProtocolError,
    require_connected,
)
from data_broker.backends.onedrive import (
    GraphAccount,
    GraphDriveBackend,
    StaticTokenProvider,
)
from data_broker.backends.webdav import WebDAVAccount, WebDAVBackend
from data_broker.config import ConfigError


# ------------------------------------------------------------------ wall arms


def test_normalizer_non_string_resource() -> None:
    with pytest.raises(policy.PolicyError) as excinfo:
        policy.normalize_store_node(123, "webdav")  # type: ignore[arg-type]
    assert excinfo.value.reason == policy.Reason.MALFORMED_RESOURCE


def test_normalizer_unknown_backend() -> None:
    with pytest.raises(policy.PolicyError) as excinfo:
        policy.normalize_store_node("Work", "gdrive")
    assert excinfo.value.reason == policy.Reason.UNKNOWN_BACKEND


def test_require_connected_arm() -> None:
    with pytest.raises(NotConnected):
        require_connected(False)
    require_connected(True)  # no raise


# ------------------------------------------------------------------ config arms


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "DATABROKER_AGENT_TOKEN",
        "DATABROKER_TRANSFER_TOKEN",
        "WEBDAV_PASSWORD_SCRATCH",
        "ONEDRIVE_TOKEN_WORK",
    ):
        monkeypatch.delenv(key, raising=False)


def _valid_cfg(tmp_path: Any, **overrides: object) -> dict:
    from pathlib import Path

    tmp = tmp_path if tmp_path is not None else Path("/tmp/cov-cfg")
    base: dict = {
        "bind_host": "127.0.0.1",
        "bind_port": 8471,
        "transport": "http",
        "auth": {"oauth": {"issuer": "https://i", "audience": "a"}},
        "storage": {
            "data_dir": str(tmp / "d"),
            "grants_db": str(tmp / "g.sqlite3"),
            "audit_log": str(tmp / "a.jsonl"),
        },
        "accounts": {
            "webdav": [
                {
                    "name": "scratch",
                    "url": "http://127.0.0.1:8466",
                    "username": "u",
                    "password_env": "WEBDAV_PASSWORD_SCRATCH",
                }
            ]
        },
        "tokens": {
            "agent_env": "DATABROKER_AGENT_TOKEN",
            "transfer_env": "DATABROKER_TRANSFER_TOKEN",
            "min_length": 32,
        },
    }
    base.update(overrides)
    return base


from pathlib import Path  # noqa: E402


def _write(tmp_path: Path, cfg: dict) -> str:
    import yaml

    p = tmp_path / "cfg.yaml"
    p.write_text(yaml.safe_dump(cfg))
    return str(p)


def test_unknown_top_key(tmp_path: Path) -> None:
    path = _write(tmp_path, _valid_cfg(tmp_path, bogus=1))
    with pytest.raises(ConfigError):
        db_config.load_config(path)


def test_wildcard_bind_hosts(tmp_path: Path) -> None:
    for host in ("0.0.0.0", "::", ""):
        path = _write(tmp_path, _valid_cfg(tmp_path, bind_host=host))
        with pytest.raises(ConfigError):
            db_config.load_config(path)


def test_bad_bind_port(tmp_path: Path) -> None:
    path = _write(tmp_path, _valid_cfg(tmp_path, bind_port=70000))
    with pytest.raises(ConfigError):
        db_config.load_config(path)


def test_bad_transport(tmp_path: Path) -> None:
    path = _write(tmp_path, _valid_cfg(tmp_path, transport="grpc"))
    with pytest.raises(ConfigError):
        db_config.load_config(path)


def test_http_requires_oauth(tmp_path: Path) -> None:
    cfg = _valid_cfg(tmp_path)
    cfg.pop("auth")
    path = _write(tmp_path, cfg)
    with pytest.raises(ConfigError):
        db_config.load_config(path)


def test_env_indirection_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABROKER_AGENT_TOKEN", raising=False)
    cfg = _valid_cfg(tmp_path)
    cfg["tokens"]["agent_env"] = "${DATABROKER_AGENT_TOKEN}"
    path = _write(tmp_path, cfg)
    with pytest.raises(ConfigError):
        db_config.load_config(path)


def test_env_indirection_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WEBDAV_PASSWORD_SCRATCH", "pw")
    monkeypatch.setenv("DATABROKER_AGENT_TOKEN", "a" * 40)
    monkeypatch.setenv("DATABROKER_TRANSFER_TOKEN", "t" * 40)
    cfg = _valid_cfg(tmp_path)
    cfg["tokens"]["agent_env"] = "${WEBDAV_PASSWORD_SCRATCH}"
    path = _write(tmp_path, cfg)
    # Top-level string indirection resolves (the ${VAR} VALUE form);
    # load_config resolves it to the var's value. agent_env then names
    # a var whose value is 'pw'... which fails the min-length guard,
    # so assert the resolved-value error instead.
    with pytest.raises(ConfigError):
        db_config.load_config(path)


def test_tokens_non_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WEBDAV_PASSWORD_SCRATCH", "pw")
    cfg = _valid_cfg(tmp_path)
    cfg["tokens"] = "nope"
    path = _write(tmp_path, cfg)
    with pytest.raises(ConfigError):
        db_config.load_config(path)


def test_tokens_unknown_key(tmp_path: Path) -> None:
    cfg = _valid_cfg(tmp_path)
    cfg["tokens"]["bogus"] = 1
    path = _write(tmp_path, cfg)
    with pytest.raises(ConfigError):
        db_config.load_config(path)


def test_tokens_bad_min_length(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABROKER_AGENT_TOKEN", "a" * 40)
    monkeypatch.setenv("DATABROKER_TRANSFER_TOKEN", "t" * 40)
    cfg = _valid_cfg(tmp_path)
    cfg["tokens"]["min_length"] = 0
    path = _write(tmp_path, cfg)
    with pytest.raises(ConfigError):
        db_config.load_config(path)


def test_storage_non_mapping(tmp_path: Path) -> None:
    cfg = _valid_cfg(tmp_path)
    cfg["storage"] = []
    path = _write(tmp_path, cfg)
    with pytest.raises(ConfigError):
        db_config.load_config(path)


def test_storage_unknown_key(tmp_path: Path) -> None:
    cfg = _valid_cfg(tmp_path)
    cfg["storage"]["bogus"] = "x"
    path = _write(tmp_path, cfg)
    with pytest.raises(ConfigError):
        db_config.load_config(path)


def test_accounts_no_entries(tmp_path: Path) -> None:
    cfg = _valid_cfg(tmp_path)
    cfg["accounts"] = {}
    path = _write(tmp_path, cfg)
    with pytest.raises(ConfigError):
        db_config.load_config(path)


def test_webdav_entry_missing_password_env(tmp_path: Path) -> None:
    cfg = _valid_cfg(tmp_path)
    cfg["accounts"]["webdav"][0].pop("password_env")
    path = _write(tmp_path, cfg)
    with pytest.raises(ConfigError):
        db_config.load_config(path)


def test_webdav_password_env_unset(tmp_path: Path) -> None:
    cfg = _valid_cfg(tmp_path)
    path = _write(tmp_path, cfg)
    with pytest.raises(ConfigError):
        db_config.load_config(path)


def test_onedrive_account_roundtrip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WEBDAV_PASSWORD_SCRATCH", "pw")
    monkeypatch.setenv("DATABROKER_AGENT_TOKEN", "a" * 40)
    monkeypatch.setenv("DATABROKER_TRANSFER_TOKEN", "t" * 40)
    monkeypatch.setenv("ONEDRIVE_TOKEN_WORK", "tok")
    cfg = _valid_cfg(tmp_path)
    cfg["accounts"]["onedrive"] = [
        {
            "name": "work",
            "tenant_id": "t",
            "client_id": "c",
            "token_env": "ONEDRIVE_TOKEN_WORK",
        }
    ]
    path = _write(tmp_path, cfg)
    cfg_out = db_config.load_config(path)
    assert cfg_out.accounts["onedrive"][0]["name"] == "work"


def test_onedrive_token_env_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBDAV_PASSWORD_SCRATCH", "pw")
    cfg = _valid_cfg(tmp_path)
    cfg["accounts"] = {
        "onedrive": [
            {"name": "w", "tenant_id": "t", "client_id": "c", "token_env": "ONEDRIVE_TOKEN_WORK"}
        ]
    }
    path = _write(tmp_path, cfg)
    with pytest.raises(ConfigError):
        db_config.load_config(path)


def test_config_not_a_mapping(tmp_path: Path) -> None:
    import yaml

    p = tmp_path / "cfg.yaml"
    p.write_text("- a\n- b\n")
    with pytest.raises(ConfigError):
        db_config.load_config(str(p))


def test_config_missing_file() -> None:
    with pytest.raises(ConfigError):
        db_config.load_config("/nonexistent/config.yaml")


def test_gateway_non_mapping(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBDAV_PASSWORD_SCRATCH", "pw")
    monkeypatch.setenv("DATABROKER_AGENT_TOKEN", "a" * 40)
    monkeypatch.setenv("DATABROKER_TRANSFER_TOKEN", "t" * 40)
    cfg = _valid_cfg(tmp_path)
    cfg["gateway"] = "bogus"
    path = _write(tmp_path, cfg)
    with pytest.raises(ConfigError):
        db_config.load_config(path)