# Credential Rotation Runbook — data-access-broker

Every live credential this broker holds, its custody class (see
SECURITY.md "Declared custody classes"), and its rotation procedure.
Rotation precedes publication for every repo in the suite.

## webdav — SCOPED

- **What:** the per-store app password (`*_password_env` per webdav
  entry).
- **Why scoped:** an app password is scoped to the store account it
  belongs to and can be revoked independently of the human password.
- **Rotate:** revoke the app password at the server (Nextcloud:
  Settings → Security → Devices & sessions; generic WebDAV: the
  server's user-management surface), create a new one, update the env
  var, restart (passwords resolve at boot; a stale one fails closed at
  connect).
- **Compromise procedure:** revoke the app password immediately
  (server-side, instant), review the audit chain for operations the
  record does not explain, create the replacement, restart. The store
  should be versioning-capable so any writes are after-the-fact
  reviewable.

## onedrive — SCOPED

- **What:** the delegated Graph token (`token_env` on the entry, or
  the MSAL per-account cache when `token_provider: msal` is selected).
- **Why scoped:** delegated Graph under the app registration with
  minimal file scopes — the closest to properly scoped credentials in
  the suite.
- **Rotate (static token):** issue a fresh token out of band, update
  the env var, restart.
- **Rotate (msal provider):** delete the per-account MSAL token cache
  (`<data_dir>/msal_cache_<account>.json`), restart — the next
  acquisition raises InteractionRequiredError with the device-code URI,
  completed in the owner's live session (owner-executed per the
  2026-09-19 ruling; see DEPLOYMENT.md §2).
- **Compromise procedure:** revoke the account's Graph sessions (Entra
  admin or My Sign-Ins), delete the cache, rotate, restart. The Entra
  app registration's client secret (if any, for confidential flows)
  rotates in the Entra portal; this broker uses the public
  device-code flow and holds no client secret.

## transfer surface token — n/a (operator-held)

`tokens.transfer_env` authenticates the CLI TO the broker; the
operator rotates it like any API token (update env, restart). The
two-token model is refuse-to-start enforced (agent token != transfer
token).

## Never-rotated-here

Scratch WebDAV tokens (`scratch_servers/`) are throwaway and die with
the container. No other live credentials are held.