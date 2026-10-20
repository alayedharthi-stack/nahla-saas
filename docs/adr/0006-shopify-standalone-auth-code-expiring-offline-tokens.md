# ADR 0006 — Shopify connection: standalone authorization code with expiring offline tokens

| Status      | Proposed (Draft PR; disabled by default; not deployed)                 |
|-------------|-------------------------------------------------------------------------|
| Scope       | Secure connection lifecycle only — no catalog import, no sync           |
| Successors  | Provider identity mapping + read-only catalog import; dashboard completion page; GDPR webhooks; key rotation |
| Owners      | Backend / Integrations                                                  |
| Docs checked| 2026-10-10 (see Sources)                                                |

## Context

Nahlah is a standalone product: its own React dashboard authenticates the
merchant (JWT carrying `tenant_id`, `user_id`, `role`, `jti`) and its FastAPI
backend serves that dashboard. There is no Shopify App Bridge surface and no
page rendered inside the Shopify admin.

The existing Shopify code is a placeholder (`integrations/shopify/main.py`).
The shared helpers it would naturally reuse are unsafe for this purpose and
are deliberately **not** reused or changed:

* `integrations/shared/base_oauth.py` keeps OAuth state in a process-local
  dict (lost across workers and restarts) and its `verify_hmac_signature`
  returns `True` when the secret is missing;
* `integrations/shared/tenant_resolver.upsert_tenant_and_integration` writes
  plaintext tokens into `Integration.config`, creates tenants from an OAuth
  callback and re-binds an existing store row without any authenticated
  tenant intent;
* `backend/store_integration/registry.py` resolves Salla only.

Catalog data (`Product.external_id` / `source`, `ProductVariant.salla_variant_id`)
is Salla-shaped, and `(tenant_id, external_id)` could collide between
providers. Provider identity mapping and catalog import are the **next**
slice; this ADR covers the connection only.

## Decision

**Standalone authorization-code grant, expiring offline tokens, read-only
catalog scope, behind a default-off flag.**

1. The merchant starts from Nahlah's own authenticated UI. `POST /start`
   (merchant JWT, DB-revalidated, no support impersonation) persists a hashed
   single-use state bound to tenant, user, JWT `jti`, canonical shop and the
   exact server-configured callback, and returns Shopify's authorize URL
   (`scope=read_products`, no `grant_options[]=per-user`).
2. Shopify redirects the browser to the exact callback. The callback verifies
   the query HMAC (hex, `hmac` removed, keys sorted; duplicates/arrays
   refused; missing secret fails closed), the timestamp window and the
   canonical shop, consumes the state once and stores the code AES-GCM
   encrypted. **It binds nothing.**
3. The dashboard's authenticated `POST /complete` (same tenant, user and JWT
   `jti`) exchanges the code with `expiring=1`, validates the grant
   (refresh token and expiries present, scopes ⊆ read-only), checks the shop
   identity with an authenticated GraphQL query, re-validates the live user
   and tenant rows under lock, and only then writes the ownership row.
4. Expiring offline access **and** refresh tokens are stored encrypted with a
   dedicated key and their expiries; refresh rotates them under a per-shop
   lease with compare-and-swap.
5. `app/uninstalled` is verified over the raw body (base64 HMAC), quarantines
   the connection and is resolved by asking Shopify with the current
   credential; a durable runner retries.

### Why standalone authorization code (and not embedded token exchange)

Shopify's documentation (checked 2026-10-10, see Sources) describes two
families:

* **Embedded apps** run inside the Shopify admin. With Shopify-managed
  installation (scopes declared in the app's TOML) and App Bridge, the
  frontend obtains a short-lived **ID token** (session token) and the backend
  exchanges it for an access token (token exchange). The embedded templates'
  `authenticate.admin` validates the ID token and performs the exchange; a
  custom embedded frontend has its own token-exchange path.
* **Standalone / API-only apps** run outside the admin with their own UI (or
  none). They cannot obtain ID tokens and use the **authorization code grant**:
  an exact, pre-registered redirect URI, a unique random `state`, validation
  of the callback, and an offline-token exchange.

Nahlah already owns an authenticated tenant UI and has no App Bridge
surface, so the authorization-code flow binds a Shopify shop to an
**already-authenticated Nahlah tenant actor** without a second identity
system. This is a product/architecture choice for a reversible first slice,
**not** a consequence of embedding currently being off in the Shopify shell.

What embedded would give us, and the future path:

* no redirect flicker, Shopify-managed install and scope updates, the
  merchant never leaves the admin, and App Store expectations for embedded
  apps;
* it requires an App Bridge frontend inside the admin, ID-token validation
  (signature, `aud` = client id, `dest`/`iss` shop, `exp`/`nbf`) and token
  exchange — and, crucially, a way to bind the Shopify-authenticated
  session to a Nahlah tenant (an explicit, authenticated linking step,
  never an email/domain guess).

The persistence, encryption, ownership, refresh, uninstall and recovery
layers in this slice are grant-agnostic: an embedded path can later feed the
same `_claim` with an ID-token-derived grant.

### Explicitly rejected

* **Client-credentials grant** — organization-only (stores of the app's own
  organization); unusable for unrelated merchants.
* **Non-expiring offline tokens** — public apps using the GraphQL Admin API
  must use expiring offline tokens (existing public apps must migrate by
  2027-01-01 per the coordinator-verified access-token page).
* Reusing `integrations/shared/*` OAuth helpers or `Integration.config` for
  tokens.
* Tenant creation, email/domain matching, a tenant id in the query, a bearer
  token in a URL, or any automatic cross-tenant transfer.

## Consequences

* New tables on a separate `ShopifyBase` metadata (never created by startup
  `create_all`); migration `0121` must be applied deliberately and refuses any
  pre-existing Shopify table.
* A shop belongs to at most one tenant forever (unconditional uniqueness;
  tombstone survives disconnect/uninstall). A transfer is a future,
  separately audited operation.
* The flow is not usable end to end until a dashboard completion page exists
  (later slice); with the flag off nothing is reachable anyway.
* Shopify keeps one current expiring offline token per app and store and a
  new acquisition retires older refresh tokens. **Only one backend may
  acquire tokens for a given store**: the existing Shopify shell
  (Partner 5246037 / app 434031788033) must not run a parallel OAuth or
  token-exchange flow for stores connected here. This slice does not touch
  that app's configuration.

## Production branch distinction (recorded 2026-10-10)

This ADR and its Draft PR are based on `main` @
`b7d2d6811911a80a597992e0de0c6a833c2c9e12`. The deployed catalog commit
`462ede1aff602cad84e9569be859cca0087b190d` is the tip of
`ops/tenant1-haiku-20260921-f9021b59`, **not** `main`; merge-base
`4b5793a27bbf591befeb3e00341d2b547bbdd4bb`. Divergence: GitHub compare
`b7d2d681...462ede1` reports ahead_by=8 / behind_by=100;
`git rev-list --left-right --count b7d2d681...462ede1` gives 100 / 8 once the
history is deepened (a shallow clone first reported 95 / 8, an undercount
caused by truncated history). Shared files this slice edits are byte-identical on both
for `core/log_redaction.py`, `core/middleware.py`, `core/observability_sentry.py`
and `scripts/operators/bootstrap_migration_contract.py`; `backend/main.py`,
the required-PG inventory, its runner self-test and runbook differ (the ops
side lacks the catalog review-environment guard and its proof suite). A
main-based Draft is not permission or proof to deploy onto the ops branch;
any deployment needs a separately reviewed, focused transfer.

## Sources (official; checked 2026-10-10)

Direct fetches of shopify.dev from this session's container were refused by
the egress proxy (HTTP 403); the content below was read and verified by the
coordinating reviewer through an authorized web tool on 2026-10-10, and
cross-checked against search-indexed copies of the same pages.

* Authenticate a standalone or API-only app —
  https://shopify.dev/docs/apps/build/authentication-authorization/authenticate-standalone-apps
* Authentication for apps built with Shopify CLI (embedded, managed install,
  `authenticate.admin`, token exchange) —
  https://shopify.dev/docs/apps/build/authentication-authorization/cli-app-authentication
* Access tokens (offline / online, expiring offline tokens, one current token
  per app and store, retirement on new acquisition, refresh rotation, 2027-01-01
  migration) — https://shopify.dev/docs/apps/build/authentication-authorization/access-tokens
  and #how-refresh-token-rotation-works
* Implement token exchange — send the refresh request —
  https://shopify.dev/docs/apps/build/authentication-authorization/implement-token-exchange#send-the-refresh-request
* Verify webhook deliveries (base64 HMAC-SHA256 over the raw body; delivery-id
  dedupe; five-second response limit) —
  https://shopify.dev/docs/apps/build/webhooks/verify-deliveries
* Implement authorization code grant manually (query HMAC, shop regex) —
  https://shopify.dev/docs/apps/build/authentication-authorization/access-tokens/authorization-code-grant
