"""S6-2b battery: MsalTokenProvider on the TokenProvider boundary.

The agent-side OneDrive prerequisite (no tenant, no network in tests):
- the provider implements the existing TokenProvider boundary;
- silent acquisition (cache hit) returns the access token;
- anything demanding interaction raises InteractionRequiredError
  (fail-closed: no static fallback, no unauthenticated proceed);
- provider selection is CONFIG-GATED in run.py: ``token_provider:
  msal`` selects the production provider, the default remains
  StaticTokenProvider;
- per-account cache files persist under the storage data dir.
"""

# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from pathlib import Path
from typing import Any

import msal
import pytest
from test_boot_path import booted  # noqa: F401

from data_broker import run as run_mod
from data_broker.backends.onedrive import StaticTokenProvider, TokenProvider
from data_broker.token_providers import (
    InteractionRequiredError,
    MsalTokenProvider,
    cache_path_for,
    provider_from_config,
)


def make_provider(tmp_path: Path) -> MsalTokenProvider:
    return MsalTokenProvider(
        client_id="test-client-id",
        authority="https://login.microsoftonline.com/test-tenant",
        cache_dir=tmp_path,
    )


@pytest.fixture()
def no_tenant_discovery(monkeypatch):
    """Stub MSAL's tenant discovery so the fake authority never hits the
    network (tests are no-network by contract)."""
    monkeypatch.setattr(
        msal.authority,
        "tenant_discovery",
        lambda endpoint, http_client, **kw: {
            "authorization_endpoint": "https://login.microsoftonline.com/test-tenant/oauth2/v2.0/authorize",
            "token_endpoint": "https://login.microsoftonline.com/test-tenant/oauth2/v2.0/token",
            "issuer": "https://login.microsoftonline.com/test-tenant/v2.0",
        },
    )


# ------------------------------------------------- boundary conformance


def test_implements_token_provider_boundary(tmp_path):
    provider = make_provider(tmp_path)
    assert isinstance(provider, TokenProvider)


def test_backend_never_imports_msal():
    """The S4-4 contract holds: the backend module stays msal-free; only
    the provider module consumes msal."""
    from data_broker.backends import onedrive

    source = Path(onedrive.__file__).read_text()
    assert "import msal" not in source


# ------------------------------------------------- fail-closed behavior


def test_no_cache_raises_interaction_required(tmp_path, monkeypatch, no_tenant_discovery):
    """No cached account: the provider initiates the device flow and
    raises InteractionRequiredError naming the account (the interactive
    step is owner-executed)."""
    provider = make_provider(tmp_path)

    def fake_initiate(self, scopes):
        return {"verification_uri": "https://microsoft.com/devicelogin", "user_code": "ABCD-EFGH"}

    monkeypatch.setattr(msal.PublicClientApplication, "initiate_device_flow", fake_initiate)
    with pytest.raises(InteractionRequiredError, match="interactive device-code"):
        provider.get_token("work")


def test_device_flow_initiation_failure_raises(tmp_path, monkeypatch, no_tenant_discovery):
    provider = make_provider(tmp_path)

    def fake_initiate(self, scopes):
        return {"error": "invalid_client", "error_description": "bad registration"}

    monkeypatch.setattr(msal.PublicClientApplication, "initiate_device_flow", fake_initiate)
    with pytest.raises(InteractionRequiredError, match="device-flow initiation failed"):
        provider.get_token("work")


def test_silent_hit_returns_token_and_persists_cache(tmp_path, monkeypatch):
    """A cached account with a valid token acquires silently; the cache
    state lands on disk under the data dir."""
    provider = make_provider(tmp_path)

    class FakeApp:
        def __init__(self, *a, **kw):
            self.token_cache = msal.SerializableTokenCache()

        def get_accounts(self, username):
            return [{"username": username, "home_account_id": "h1"}]

        def acquire_token_silent(self, scopes, account):
            return {"access_token": "cached-token"}

    monkeypatch.setattr(msal, "PublicClientApplication", FakeApp)
    token = provider.get_token("work")
    assert token == "cached-token"
    assert cache_path_for(tmp_path, "work").name == "msal_cache_work.json"


# ------------------------------------------------- config-gated selection


def test_provider_from_config_gates_on_key(tmp_path):
    assert provider_from_config({"name": "a"}, tmp_path) is None
    assert provider_from_config({"name": "a", "token_provider": "static"}, tmp_path) is None


def test_provider_from_config_requires_ids(tmp_path):
    with pytest.raises(ValueError, match="client_id and authority"):
        provider_from_config({"name": "a", "token_provider": "msal"}, tmp_path)


def test_provider_from_config_builds(tmp_path):
    provider = provider_from_config(
        {
            "name": "a",
            "token_provider": "msal",
            "client_id": "cid",
            "authority": "https://login.microsoftonline.com/t",
        },
        tmp_path,
    )
    assert isinstance(provider, MsalTokenProvider)


def test_boot_default_remains_static(booted):  # noqa: F811 - fixture reuse
    """No ``token_provider:`` key in the config: the boot wires the
    static provider (tests and scratch keep no-network behavior)."""
    from data_broker.backends.onedrive import GraphDriveBackend

    backend = booted.backends.get("onedrive")
    if backend is None:
        pytest.skip("boot fixture has no onedrive entries")
    assert isinstance(backend, GraphDriveBackend)
    assert isinstance(backend._token_provider, StaticTokenProvider)


def test__onedrive_provider_gates(tmp_path):
    """The boot's selection helper: static by default, msal when gated."""
    from data_broker.token_providers import MsalTokenProvider as M

    cfg: Any = type("C", (), {"storage": {"data_dir": str(tmp_path)}})()
    got = run_mod._onedrive_provider(cfg, [{"name": "a"}])
    assert isinstance(got, StaticTokenProvider)
    got2 = run_mod._onedrive_provider(
        cfg,
        [
            {
                "name": "a",
                "token_provider": "msal",
                "client_id": "cid",
                "authority": "https://login.microsoftonline.com/t",
            }
        ],
    )
    assert isinstance(got2, M)


# ------------------------------------------------- cache persistence arms


def test_cache_loaded_when_present(tmp_path, monkeypatch, no_tenant_discovery):
    """An existing cache file is deserialized at app construction (the
    per-account cache round-trips); a silent hit then persists it."""
    provider = make_provider(tmp_path)
    cache_path = cache_path_for(tmp_path, "work")
    cache_path.write_text('{"accounts": []}', encoding="utf-8")
    deserialized = {}

    class FakeCache(msal.SerializableTokenCache):
        def __init__(self, *a, **kw):
            self._changed = False
            self._lock = __import__("threading").RLock()
            self._cache = {}

        def deserialize(self, state):
            deserialized["state"] = state
            import json

            self._cache = json.loads(state) if state else {}
            self._changed = False

        def serialize(self):
            return '{"accounts": ["persisted"]}'

        @property
        def has_state_changed(self):  # type: ignore[override]
            return self._changed

        @has_state_changed.setter
        def has_state_changed(self, value):
            self._changed = value

    class FakeApp:
        def __init__(self, *a, token_cache=None, **kw):
            self.token_cache = token_cache

        def get_accounts(self, username):
            return [{"username": username}]

        def acquire_token_silent(self, scopes, account):
            self.token_cache._changed = True  # a real acquisition flips the flag
            return {"access_token": "tok"}

    monkeypatch.setattr(msal, "SerializableTokenCache", FakeCache)
    monkeypatch.setattr(msal, "PublicClientApplication", FakeApp)
    token = provider.get_token("work")
    assert token == "tok"
    assert deserialized["state"] == '{"accounts": []}'
    assert cache_path.read_text(encoding="utf-8") == '{"accounts": ["persisted"]}'


def test_silent_miss_after_cached_account_falls_to_device_flow(
    tmp_path, monkeypatch, no_tenant_discovery
):
    """A cached account whose silent acquisition fails (expired) still
    fails closed to InteractionRequired -- never a static fallback."""
    provider = make_provider(tmp_path)

    class FakeApp:
        def __init__(self, *a, **kw):
            self.token_cache = msal.SerializableTokenCache()

        def get_accounts(self, username):
            return [{"username": username}]

        def acquire_token_silent(self, scopes, account):
            return None

        def initiate_device_flow(self, scopes):
            return {"verification_uri": "u", "user_code": "c"}

    monkeypatch.setattr(msal, "PublicClientApplication", FakeApp)
    with pytest.raises(InteractionRequiredError, match="work"):
        provider.get_token("work")


def test_persist_skips_unchanged_cache(tmp_path, monkeypatch, no_tenant_discovery):
    """A cache with no state change is not rewritten (no churn)."""
    provider = make_provider(tmp_path)

    class FakeApp:
        def __init__(self, *a, **kw):
            self.token_cache = msal.SerializableTokenCache()

        def get_accounts(self, username):
            return [{"username": username}]

        def acquire_token_silent(self, scopes, account):
            return {"access_token": "tok"}

    monkeypatch.setattr(msal, "PublicClientApplication", FakeApp)
    cache_file = cache_path_for(tmp_path, "work")
    token = provider.get_token("work")
    assert token == "tok"
    assert not cache_file.exists()  # has_state_changed False -> no write
