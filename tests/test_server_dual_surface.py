"""S4-5 battery: the dual-surface server (TDD-first).

The D5 two-token surface separation carries over (goal contract
section 4): agent surface (request_access, check_access, list, move,
trash, mkdir) and transfer surface (check_access, read, write) behind
separate tokens and path prefixes, keeping bulk content out of LLM
context. Guards with teeth:

- refuse to boot when the transfer token is missing, short, or equal
  to the agent token;
- write-before-operate audit for every executed op.

Environment hygiene (the supervisory-review finding): every env
mutation goes through monkeypatch so the battery passes in a CLEAN
environment - no pre-existing WEBDAV_DUMMY or token vars may be
required. The boot-path battery (test_boot_path.py) carries the
production-wiring coverage.
"""

# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from data_broker import config, run
from data_broker.config import ConfigError

T0 = datetime(2026, 9, 13, 12, 0, 0)

TOKEN_KEYS = ("DATABROKER_AGENT_TOKEN", "DATABROKER_TRANSFER_TOKEN", "WEBDAV_DUMMY")


@pytest.fixture(autouse=True)
def clean_token_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate the token env: every battery test starts from a clean
    slate (no pre-existing WEBDAV_DUMMY or token vars from the host
    session - the review's repro: the committed suite was red without
    WEBDAV_DUMMY exported)."""
    for key in TOKEN_KEYS:
        monkeypatch.delenv(key, raising=False)


def make_config(tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "bind_host": "127.0.0.1",
        "bind_port": 8471,
        "transport": "http",
        "auth": {"oauth": {"issuer": "https://issuer", "audience": "data-access-broker"}},
        "storage": {
            "data_dir": str(tmp_path / "data"),
            "grants_db": str(tmp_path / "grants.sqlite3"),
            "audit_log": str(tmp_path / "audit.jsonl"),
        },
        "accounts": {
            "webdav": [
                {
                    "name": "scratch",
                    "url": "http://127.0.0.1:8466",
                    "username": "anonymous",
                    "password_env": "WEBDAV_DUMMY",
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


def write_config(tmp_path: Path, **overrides: Any) -> str:
    import yaml

    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(make_config(tmp_path, **overrides)))
    return str(path)


# ------------------------------------------------------------------ tokens


def test_missing_transfer_token_refuses_boot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WEBDAV_DUMMY", "pw")
    monkeypatch.setenv("DATABROKER_AGENT_TOKEN", "a" * 40)
    monkeypatch.delenv("DATABROKER_TRANSFER_TOKEN", raising=False)
    path = write_config(tmp_path)
    with pytest.raises(ConfigError):
        run.load_and_validate(path)


def test_equal_tokens_refuse_boot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WEBDAV_DUMMY", "pw")
    monkeypatch.setenv("DATABROKER_AGENT_TOKEN", "same-token-value")
    monkeypatch.setenv("DATABROKER_TRANSFER_TOKEN", "same-token-value")
    path = write_config(tmp_path)
    with pytest.raises(ConfigError):
        run.load_and_validate(path)


def test_short_transfer_token_refuses_boot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WEBDAV_DUMMY", "pw")
    monkeypatch.setenv("DATABROKER_AGENT_TOKEN", "a" * 32)
    monkeypatch.setenv("DATABROKER_TRANSFER_TOKEN", "short")
    path = write_config(tmp_path)
    with pytest.raises(ConfigError):
        config.load_config(path)


def test_missing_agent_token_refuses_boot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WEBDAV_DUMMY", "pw")
    monkeypatch.delenv("DATABROKER_AGENT_TOKEN", raising=False)
    path = write_config(tmp_path)
    with pytest.raises(ConfigError):
        config.load_config(path)


def test_valid_tokens_boot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBDAV_DUMMY", "pw")
    monkeypatch.setenv("DATABROKER_AGENT_TOKEN", "a" * 40)
    monkeypatch.setenv("DATABROKER_TRANSFER_TOKEN", "t" * 40)
    path = write_config(tmp_path)
    cfg = config.load_config(path)
    assert cfg.bind_host == "127.0.0.1"
