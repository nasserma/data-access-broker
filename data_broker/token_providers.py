"""Production MSAL token provider (S6-2b): the agent-side prerequisite.

Implements the existing ``TokenProvider`` boundary
(``data_broker.backends.onedrive.TokenProvider``) with the MSAL
device-code flow and a per-account token cache under the storage data
dir. The backend never imports msal; this module is the only msal
consumer in the package.

Contract (Stage 6 goal contract S6-2b):

- Provider selection is CONFIG-GATED in ``run.py``: a
  ``token_provider: msal`` key on an ``accounts.onedrive`` entry
  selects this provider; the default remains StaticTokenProvider
  (tests and scratch, no network).
- Device-code flow: ``acquire_token_interactive``-free; the flow is
  ``initiate_device_flow`` + ``acquire_token_by_device_flow``. The
  FIRST acquisition for an account requires the interactive device-code
  step, which is an OWNER action (the live-tenant go is never the
  agent's to give).
- Fail-closed on interaction-required: when MSAL demands interaction
  (no cached token, expired refresh, consent required), the provider
  raises ``InteractionRequiredError`` -- the boot/connect path refuses
  rather than falling back to a static token or proceeding
  unauthenticated.
- Per-account cache: MSAL's ``SerializableTokenCache`` persisted under
  ``<data_dir>/msal_cache_<account>.json``, loaded at construction and
  written after every acquisition.
- No tenant contact happens at import or boot: the first provider
  network call is the first ``get_token`` after config selects it.

The Entra app registration, tenant setup, device-code acquisition, and
all live-tenant verification are owner-executed in his own final stage
(ruled 2026-09-19); this module is the prerequisite that stage installs
against.
"""

# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from pathlib import Path

import msal

from data_broker.backends.onedrive import TokenProvider

GRAPH_SCOPE = "https://graph.microsoft.com/.default"


class InteractionRequiredError(Exception):
    """MSAL demands an interactive step (device code, consent, reauth).

    Fail-closed: the caller refuses the operation; no static-token
    fallback, no unauthenticated proceed. The interactive step is an
    owner action on his own tenant.
    """


class MsalTokenProvider(TokenProvider):
    """MSAL device-code provider on the TokenProvider boundary.

    One instance per process at config time; one authority/client pair
    (the Entra registration is per-deployment); one cache file per
    account name.
    """

    def __init__(self, client_id: str, authority: str, cache_dir: Path | str) -> None:
        """client_id + authority come from the config entry (the Entra
        app registration); cache_dir is the storage data dir."""
        self._client_id = client_id
        self._authority = authority
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._apps: dict[str, msal.PublicClientApplication] = {}

    def _app(self, account_name: str) -> msal.PublicClientApplication:
        """The per-account MSAL app: a SerializableTokenCache persisted
        under the data dir, loaded once and rewritten on acquisition."""
        app = self._apps.get(account_name)
        if app is not None:
            return app
        cache = msal.SerializableTokenCache()
        cache_path = self._cache_dir / f"msal_cache_{account_name}.json"
        if cache_path.exists():
            cache.deserialize(cache_path.read_text(encoding="utf-8"))
        app = msal.PublicClientApplication(
            self._client_id, authority=self._authority, token_cache=cache
        )
        app._cache_path = cache_path  # type: ignore[attr-defined] - carry for _persist
        self._apps[account_name] = app
        return app

    @staticmethod
    def _persist(app: msal.PublicClientApplication) -> None:
        cache_path = getattr(app, "_cache_path", None)
        if cache_path is not None and app.token_cache.has_state_changed:  # type: ignore[attr-defined]
            cache_path.write_text(app.token_cache.serialize(), encoding="utf-8")  # type: ignore[attr-defined]

    def get_token(self, account_name: str) -> str:
        """Acquire an access token for the account, silently first.

        Silent acquisition (cache hit) succeeds without interaction.
        Anything that demands interaction raises
        InteractionRequiredError: the flow is fail-closed, and the
        interactive device-code step belongs to the owner's live
        session.
        """
        app = self._app(account_name)
        accounts = app.get_accounts(username=account_name)
        if accounts:
            result = app.acquire_token_silent(
                [GRAPH_SCOPE], account=accounts[0]
            )
            self._persist(app)
            if result and "access_token" in result:
                return str(result["access_token"])

        # No cache (or silent failed): initiate the device-code flow and
        # FAIL CLOSED with the flow data -- the owner completes it in his
        # live-tenant session; the broker never proceeds unauthenticated.
        flow = app.initiate_device_flow(scopes=[GRAPH_SCOPE])
        if "error" in flow:
            raise InteractionRequiredError(
                f"msal device-flow initiation failed for {account_name!r}: "
                f"{flow.get('error')}: {flow.get('error_description')}"
            )
        raise InteractionRequiredError(
            f"interactive device-code acquisition required for {account_name!r} "
            f"(owner-executed live step): verification uri {flow.get('verification_uri')} "
            f"code {flow.get('user_code')}"
        )


def provider_from_config(entry: dict, data_dir: Path | str) -> MsalTokenProvider | None:
    """Config-gated construction: an entry carrying
    ``token_provider: msal`` builds the provider from the entry's
    ``client_id`` / ``authority``; anything else returns None (the
    caller keeps its StaticTokenProvider default)."""
    if entry.get("token_provider") != "msal":
        return None
    client_id = entry.get("client_id")
    authority = entry.get("authority")
    if not client_id or not authority:
        raise ValueError(
            "accounts.onedrive: token_provider msal requires client_id and authority"
        )
    return MsalTokenProvider(
        client_id=str(client_id), authority=str(authority), cache_dir=data_dir
    )


def cache_path_for(data_dir: Path | str, account_name: str) -> Path:
    """The cache file path for an account (exposed for the test battery
    and DEPLOYMENT.md)."""
    return Path(data_dir) / f"msal_cache_{account_name}.json"
