# Shopify deployed-compatible port — staging runbook (Draft PR #1209)

Status: **runbook only.** Nothing in it has been executed against a live or
staging environment. Every step is a later, separately approved action by the
owner or an operator. The port itself ships dormant: all Shopify capability,
UI, worker and destructive flags default OFF, and no secret, grant, webhook,
redirect or subscription is configured by the code.

The known `main` versus deployed-catalog divergence remains a release
blocker for the Shopify stack as a whole; this runbook does not propose a
merge or a deployment.

## 0. Preconditions (owner decisions, out of band)

* An explicitly chosen **test tenant** on staging with a real merchant user
  (role `merchant`, no impersonation). The tenant is never inferred from
  another audit or session.
* Owned HTTPS origins: the API origin that will serve
  `https://<api host>/merchant/integrations/shopify/callback`, and the
  dashboard origin (`DASHBOARD_URL`) that will serve
  `<origin>/integrations/shopify/complete`.
* The Shopify app shell is configured by the owner, not by code: redirect URI
  byte-exactly as above, `app/uninstalled` subscription to
  `https://<api host>/webhooks/shopify/app-uninstalled`, the three compliance
  topics to the plural `https://<api host>/webhooks/shopify/privacy/...`
  URIs, and the access scope `read_products` **only** — requested only after
  this configuration is explicitly authorized. `read_products` is the
  implemented scope contract, not a live grant.
* Secrets are entered out of band, never through this repository or a chat:
  `SHOPIFY_CLIENT_ID`, `SHOPIFY_CLIENT_SECRET`, and a **dedicated**
  `SHOPIFY_TOKEN_ENC_KEY` (32-byte url-safe base64) that differs from
  `WA_TOKEN_ENC_KEY`, `TOTP_ENC_KEY` and `JWT_SECRET` (the gate refuses a
  reused key). `SHOPIFY_ADMIN_API_VERSION` may be left at the code default;
  the catalog import pins Admin API `2026-10` independently.

## 1. Staging schema preflight (read-only first)

Live Alembic / schema state is **unverified** by this port. Before any
migration:

1. Record the staging `alembic_version` rows and confirm the deployed bootstrap
   target is still `0093` (normal bootstrap never applies 0121–0123).
2. Run the read-only audits with `DATABASE_URL` set for staging only, in a
   session that cannot write:
   * `python scripts/audit_shopify_catalog_collisions_readonly.py`
   * `python scripts/audit_shopify_privacy_readonly.py`
   Both run one `READ ONLY` transaction and print counts only. Any
   pre-existing `shopify_*` table, partial schema or legacy Shopify footprint
   is a stop: migrations 0121–0123 refuse pre-existing objects and change
   nothing in that case.
3. Confirm `database/migrations/versions/0120_*` on staging equals the
   repository blob (it is identical on the deployed base and `main`).

## 2. Migrations 0121 → 0122 → 0123 (explicit, owner-approved, in order)

Each is an explicit `alembic upgrade <rev>` from `database/`, never `head`,
never from bootstrap:

| Step | Command | Refuses when | Downgrade |
|---|---|---|---|
| foundation | `alembic upgrade 0121` | any `shopify_connections` / leases / oauth states / webhook events / audit table already exists (full, partial or malformed) | `alembic downgrade 0120` drops only the five tables |
| catalog mirror | `alembic upgrade 0122` | 0121 missing, or any catalog table / the `(id, tenant_id)` connection key already exists | `alembic downgrade 0121` drops only the seven tables and the key |
| privacy | `alembic upgrade 0123` | 0121 / 0122 missing, the connection key missing, or any privacy table already exists | `alembic downgrade 0122` drops only the three tables |

Evidence that these behave as stated on disposable databases (fresh,
existing, drift, refusal, upgrade and downgrade on the real chain
`0120 → 0121 → 0122 → 0123` and back) is the migration cases of the three
Shopify proof suites recorded in the validation evidence of this PR. That
evidence is for disposable databases; the staging run must be observed
separately, with the `alembic_version` rows recorded before and after.

## 3. Configuration gates (names only; values entered out of band)

Backend flags count as on for `1`, `true`, `yes`, `on`; dashboard flags are
build-time Vite variables. All default OFF.

| Variable | Enables | Notes |
|---|---|---|
| `NAHLA_SHOPIFY_CONNECTION_ENABLED` | connection routes (otherwise 404) and the uninstall recovery runner | the runner also needs the full gate below |
| `SHOPIFY_CLIENT_ID`, `SHOPIFY_CLIENT_SECRET` | OAuth and webhook HMAC | missing secret → every webhook 401 / 503 by design |
| `SHOPIFY_OAUTH_REDIRECT_URI` | must equal `https://<api host>/merchant/integrations/shopify/callback` byte-exactly | |
| `DASHBOARD_URL` | https origin of the completion page | |
| `SHOPIFY_TOKEN_ENC_KEY` | encrypted expiring offline tokens | dedicated key, see §0 |
| `VITE_SHOPIFY_CONNECTION_UI` | connection card and completion page | build-time |
| `NAHLA_SHOPIFY_CATALOG_IMPORT_ENABLED` + `VITE_SHOPIFY_CATALOG_IMPORT_UI` | read-only import routes and UI | needs the connection flag too |
| `NAHLA_SHOPIFY_PRIVACY_INTAKE_ENABLED` | durable privacy receipts (plural `/webhooks/shopify/privacy/...`) | a receipt is **not** a completed deletion or a compliance claim |
| `NAHLA_SHOPIFY_PRIVACY_WORKER_ENABLED` | evidence worker | stays OFF in this slice |
| `NAHLA_SHOPIFY_PRIVACY_DESTRUCTIVE_ENABLED` | policy-gated executor | stays OFF; `APPROVED_POLICY` is `None` |

Readiness probe before any install: `GET /merchant/integrations/shopify/status`
(authenticated merchant) reports the gate's reason code
(`client_credentials_missing`, redirect / dashboard / encryption-key
reasons) until every item is present.

## 4. One development-store journey (evidence class R; not run)

Record each step with timestamps and the observed state, never a credential:

1. **Authenticated bind:** the test tenant's merchant user starts the
   connection (`POST /start`), is redirected to Shopify, and completes
   (`POST /complete`) in the same session (same tenant, user and JWT id;
   anything else is `session_mismatch`). Expected: one ownership row for the
   shop, scope exactly `read_products` (`scope_excess` refused).
2. **Signed callback, replay and cross-tenant:** a replayed callback is
   refused (single-use state); a second tenant attempting the same shop is
   refused (`shop_unavailable`); a callback with a bad HMAC or stale
   timestamp is refused.
3. **Read-only product / variant import:** one import run against the
   development store. Expected: products and variants in the provider-scoped
   mirror only; `inventory_mode = scope_missing`, location and inventory
   counts `null` (not 0), variant availability `unknown`; nothing written to
   `products` / `product_variants`. Production gap to note: the import runs
   as an in-process FastAPI `BackgroundTask`; a durable worker and a
   statement timeout sized for the publish transaction are needed before any
   production activation.
4. **Signed uninstall → quarantine → tombstone → reinstall:** uninstall the
   app from the development store; the signed `app/uninstalled` delivery is
   verified on the raw body, the connection is quarantined, reconciliation
   confirms the refusal and tombstones it (`uninstalled`, generation + 1,
   tokens erased); reinstalling creates a new generation and the old
   tombstone remains the ownership record.
5. **Privacy intake (flag on, worker OFF):** Shopify's compliance test
   deliveries are HMAC-verified and stored as receipts with 200; requests
   stay `received`; no deletion happens and none is claimed.

## 5. Out of scope until separately approved

* `read_inventory` (separate reviewed code/scope/reauthorization slice;
  every connected store would need re-authorization).
* Orders, customers, fulfillment, shipping, payment, COD and notification
  scopes and flows.
* Any destructive privacy processing or an approved policy object.
* Production activation of any flag.
