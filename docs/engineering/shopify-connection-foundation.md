# Shopify secure connection foundation (dormant)

Status: Draft PR, **disabled by default**, not deployed, no live configuration.
Decision record: `docs/adr/0006-shopify-standalone-auth-code-expiring-offline-tokens.md`.

This slice connects a Shopify store to an authenticated Nahlah tenant and
keeps the credential lifecycle correct. It does **not** import a catalog,
sync anything, register a store adapter, or touch the AI runtime, prompts,
persona, model selection, Salla, Meta, WhatsApp or Moyasar. Nothing here
should be described to merchants as "Shopify sync".

## Code map

| Path | Role |
|------|------|
| `backend/services/shopify_connection/config.py` | flag, exact callback / dashboard targets, fixed scopes, key checks |
| `.../shop_domain.py` | canonical single-label `<name>.myshopify.com` |
| `.../crypto.py` | AES-256-GCM, AAD = purpose / tenant / shop / generation (or state) |
| `.../oauth.py` | callback query HMAC, authorize URL, exchange / refresh, GraphQL identity |
| `.../webhooks.py` | raw-body webhook HMAC, signed uninstall identity |
| `.../models.py` | `ShopifyBase` tables (not in startup `create_all`) |
| `.../actor.py` | DB revalidation of the merchant actor |
| `.../lifecycle.py` | state, claim, shop lease, refresh, disconnect, uninstall, reconcile |
| `.../recovery.py` | durable, flag-gated reconciliation runner |
| `backend/routers/shopify_connection.py` | routes (all 404 while the flag is off) |
| `database/migrations/versions/0121_shopify_connection_foundation.py` | explicit migration |

## Routes

| Route | Auth | Purpose |
|-------|------|---------|
| `GET /merchant/integrations/shopify/status` | JWT (platform staff refused) | secret-free connection list |
| `POST /merchant/integrations/shopify/start` `{shop}` | merchant actor | authorize URL |
| `GET /merchant/integrations/shopify/callback` | public (exact path) | verify + consume state; binds nothing |
| `POST /merchant/integrations/shopify/complete` `{handle}` | merchant actor, same session | exchange, identity, claim |
| `POST /merchant/integrations/shopify/disconnect` `{shop}` | merchant actor | erase credentials, keep tombstone |
| `POST /webhooks/shopify/app-uninstalled` | raw-body HMAC | persist, dedupe, quarantine, ack |

"Merchant actor" = JWT role `merchant`, no `impersonation`, not a platform
role, a `jti`; then the `users` row must exist, be active, belong to the JWT
tenant and still have role `merchant`, and the tenant must be active.
`core.auth.require_merchant_scope` admits support impersonation; these
mutations refuse it explicitly.

## Lifecycle

```
start ──► state (hashed, single use, 10 min) ──► Shopify consent
      ──► callback: query HMAC + timestamp + shop + consume state
           └─ code stored AES-GCM, completion handle (hashed, 5 min) → dashboard fragment
      ──► complete (same tenant/user/jti):
           shop lease ─► recheck ownership ─► exchange (expiring=1) ─► GraphQL identity
           ─► lock user+tenant, revalidate ─► claim (insert or same-tenant reinstall) ─► release lease
```

* **Ownership** is written only at the end of `complete`. Starting, failing
  or expiring an authorization writes only a state row and reserves nothing.
  `shop_domain` and `shop_gid` are unconditionally unique; disconnect and
  uninstall keep the row (tokens erased) as the ownership tombstone. The same
  tenant may reinstall; another tenant gets `shop_unavailable`.
* **Holding a state URL binds nothing**: the callback only produces a handle,
  and only the tenant's authenticated `complete` with the same `jti` uses it.
* **Per-shop lease** (`shopify_shop_leases`): at most one credential-producing
  Shopify call (code exchange or refresh) per shop across workers. A busy lease
  answers `exchange_in_progress` and spends nothing. Stale leases are taken
  over. The result is stored only if the lease is still held, in the same
  transaction that deletes it. No row lock is held across HTTP.
* **Call deadlines**: each Shopify call admitted by a lease is preceded by a
  lease check and must finish `LEASE_SAFETY_SECONDS` (10 s) before the lease
  could expire (and within 20 s).
* **Possibly retired credential**: if an exchange may have produced a grant
  that was not stored (late answer after a lease takeover, timeout, refused
  claim, failed identity check), the shop's stored credential is quarantined
  as `possible_retired_credential`. Only a successful forced refresh clears
  it; a refusal ends in `reauth_required` (ownership kept).
* **Refresh** (lease + compare-and-swap on generation, credential version and,
  for the runner, its lease): a permanent refusal erases the pair
  (`reauth_required`); a transient failure or timeout keeps the stored pair
  (Shopify accepts a retry of the held refresh token until its replacement is
  used) and defers.

## Uninstall: what is proven and what is not

The webhook HMAC (base64 HMAC-SHA256 of the raw body with the client secret)
authenticates the **body only**. `X-Shopify-Shop-Domain`, `X-Shopify-Topic`,
`X-Shopify-Webhook-Id`, `X-Shopify-Event-Id` and `X-Shopify-Triggered-At` are
unsigned. A captured signed body can be replayed later with any headers,
including after the merchant reinstalled. Therefore:

1. Shop identity comes from the signed body (`id`, `myshopify_domain`);
   headers must merely agree.
2. No header — in particular no timestamp — retains, revives or transfers
   access. There is no ordering decision based on unsigned data.
3. An active connection is **quarantined** (credential use stops) and the
   request is made **durable** (`reconcile_next_at`, `reconcile_request_version`)
   before the 200 is returned.
4. **Reconciliation asks Shopify**: with the current generation's credential
   (refreshing a normally expired access token first, since a successful
   refresh is itself evidence the install is alive). Accepted → retained;
   refused → uninstalled (tokens erased, generation bumped, tombstone kept);
   transient → stays quarantined, retried with capped backoff.
5. Dedupe: the delivery id suppresses only redeliveries of the same body; a
   body digest already proven stale for the **current** generation downgrades
   to a non-suspending probe. Neither ever suppresses a later genuine
   uninstall whose signed body is byte-identical: every redelivery/duplicate
   still requests a probe while credentials exist.
6. Every write of a reconciliation is fenced: generation, credential version,
   request version (a newer request is never cleared by an older probe) and
   the runner's lease checked against the write-time clock.

**Residual ambiguity (documented, fail-safe):**

* Between Shopify's uninstall and token revocation there may be a short
  window in which the old credential still works; a probe in that window
  retains. The next refused call (probe, refresh) then resolves it. Retaining
  never grants access Shopify does not grant.
* A retired (not revoked) access token stays usable until it expires, so an
  access-token probe cannot prove a refresh token is current; that is why
  `possible_retired_credential` requires a forced refresh.
* A deadline bounds how long Nahlah waits; it cannot recall a request
  Shopify already received. A late remote grant after a lease takeover can
  still retire the stored refresh token; the quarantine above detects it on
  the next forced refresh, but the database cannot fence a remote side
  effect.
* Shopify's refresh-rotation grace (retrying the held token until the
  replacement is used, bounded at 30 days) is relied on for discarded
  rotations; past that bound the connection becomes `reauth_required`.

## Activation prerequisites (none are configured by this PR)

All of the following must hold before any route answers or the runner is
queued; until then everything is 404 / dormant:

1. `NAHLA_SHOPIFY_CONNECTION_ENABLED=1`.
2. `SHOPIFY_CLIENT_ID`, `SHOPIFY_CLIENT_SECRET` of a Shopify app whose
   distribution allows unrelated merchants, using expiring offline tokens.
3. `SHOPIFY_OAUTH_REDIRECT_URI` exactly
   `https://<api host>/merchant/integrations/shopify/callback`, registered as
   an allowed redirect in that app.
4. `DASHBOARD_URL` (https origin) and a dashboard page at
   `/integrations/shopify/complete` that reads `#shopify_handle=` and calls
   `POST /complete` with the merchant's Authorization header — **not built
   yet** (next slice).
5. `SHOPIFY_TOKEN_ENC_KEY`: dedicated 32-byte url-safe base64 key; refused
   when equal (by decoded bytes) to `WA_TOKEN_ENC_KEY`, `TOTP_ENC_KEY` or
   `JWT_SECRET`.
6. Migration `0121` applied deliberately after a read-only check (refuses any
   pre-existing Shopify table).
7. An `app/uninstalled` webhook subscription pointing at
   `https://<api host>/webhooks/shopify/app-uninstalled`.
8. Only this backend acquires tokens for the stores it connects (the
   existing Shopify shell must not run a parallel OAuth/token exchange).

**Blocked / not done here:** the Shopify shell (Partner 5246037, org
240110284, app 434031788033) is not altered; no secret, permission,
distribution, webhook or redirect is configured; nothing is deployed.

## Known limitations (later slices)

* No dashboard completion page; no merchant-facing UI or copy.
* Mandatory compliance webhooks (`customers/data_request`, `customers/redact`,
  `shop/redact`) are not implemented — required before App Store distribution.
* Shopify-initiated installs (App URL without a Nahlah session) are not
  handled; they must route the merchant to log in and start from Nahlah.
* No encryption key rotation (single `sgcm1:` key).
* No cleanup job for expired state / event rows.
* Disconnect is local: it erases Nahlah's credentials but does not uninstall
  the app from the store.
* A shop whose myshopify domain changes while its id stays the same is
  refused as `identity_conflict` (operator review).
* Inherited: `core.token_revocation` is a best-effort denylist (Redis with an
  in-process fallback that swallows errors). Its lookup is repeated at
  revalidation and at claim time, but it is **not** a fail-closed guarantee
  added by this slice; the guarantees that do not depend on it are the
  same-`jti` binding and the locked DB revalidation of user and tenant.
* `shopify_connections.tenant_id` has no `ON DELETE` action: deleting a tenant
  that ever owned a shop is refused by the database until a deliberate,
  audited ownership decision exists.

## Tests

* `tests/test_shopify_connection_foundation.py` — database-free (runs in the
  default `pytest` of `lint-and-test`).
* `backend/tests/test_shopify_connection_pg.py` — real PostgreSQL, inventoried
  as `shopify_connection_foundation` in `scripts/required_postgres_proofs.json`.

```bash
python -m pytest -q tests/test_shopify_connection_foundation.py
NAHLA_RELIABILITY_REQUIRE_PG=1 NAHLA_RELIABILITY_PG_ADMIN_DSN=postgresql://user:pass@127.0.0.1:5432/postgres \
  python -m pytest -q backend/tests/test_shopify_connection_pg.py
```
