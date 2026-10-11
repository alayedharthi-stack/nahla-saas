# Shopify catalog import — dormant, read-only, provider-scoped mirror

Status: **Draft PR, disabled by default, not deployed, no live configuration,
no live API validation.** Stacked on the Shopify connection foundation (PR
#1202) and the merchant connection UI (PR #1203). Nothing here is "Shopify
sync", and nothing here makes a Shopify catalog reach customers.

## What "imported" means here

**Imported = one committed, complete, Shopify-confirmed snapshot of a shop's
catalog in Nahlah's own Shopify mirror tables.** The mirror is *not* the
shared catalog hub (`products` / `product_variants`): no row of it is read by
the AI runtime, WhatsApp, Meta, campaigns, Google feeds or any channel, and
the import never writes the hub. Projecting the mirror into the hub — and any
Meta export or WhatsApp publication of those records — is a later slice that
needs explicit end-to-end approval and testing.

## Code map

| Path | Role |
|------|------|
| `backend/services/shopify_catalog/config.py` | import flag, pinned API version `2026-10`, scopes, fixed bounds and page sizes |
| `.../errors.py` | closed set of outcome codes (the only text that leaves the module) |
| `.../models.py` | `ShopifyCatalogBase` tables (not in `models.Base`, not in the foundation's `ShopifyBase`) |
| `.../graphql.py` | pinned, fenced, bounded GraphQL client |
| `.../queries.py` | GraphQL documents |
| `.../parse.py` | strict node validation (presence, types, enums, GIDs, money, images) |
| `.../traversal.py` | complete traversal with nested pagination and completeness proofs |
| `.../store.py` | admission, leases, fences, staging, single-transaction publish, read model, availability view |
| `.../importer.py` | one run end to end; background entry |
| `backend/routers/shopify_catalog_import.py` | `GET …/catalog-import/status`, `POST …/catalog-import/start` |
| `database/migrations/versions/0122_shopify_catalog_mirror.py` | explicit, fail-closed migration |
| `scripts/audit_shopify_catalog_collisions_readonly.py` | read-only collision and integrity audit |
| `dashboard/src/components/integrations/ShopifyCatalogImportStep.tsx` (+ `lib/shopifyConnection/catalogImport.ts`, `api/shopifyCatalogImport.ts`) | import step on the Shopify card, behind its own flag |

## Flags (all default off; nothing is configured by this PR)

| Flag | Effect when off |
|------|-----------------|
| `NAHLA_SHOPIFY_CONNECTION_ENABLED` (foundation) | every Shopify route 404 |
| `NAHLA_SHOPIFY_CATALOG_IMPORT_ENABLED` (new) | both import routes 404; no run is ever started |
| `VITE_SHOPIFY_CATALOG_IMPORT_UI` (new, dashboard) | the card keeps its static "not started" import step |

`start` additionally requires the foundation's availability gate (client
credentials, encryption key, …) because tokens are obtained and refreshed
through the foundation. Storage is checked per request: without revision
`0122` (or `0121`) both routes answer `storage_unavailable` and nothing runs.

## Identity and schema (revision 0122)

* Every mirrored resource is identified by **(connection, Shopify GID)**. The
  connection is the foundation's ownership row (one per shop, never
  transferred between tenants), so identity is provider + shop + resource.
  Nothing is keyed by a raw numeric id, nothing uses `external_id` or
  `salla_variant_id`, and no Salla / generic upsert can reach these tables —
  proven against the real Salla product writer with colliding raw ids.
* Composite foreign keys carry `tenant_id` through every parent link
  (connection → run / product / item / location; product → variant;
  item → variant and level; location → level; run → first/last seen), so a
  row can never reference another tenant's or shop's parent. 0122 adds the
  `(id, tenant_id)` key on `shopify_connections` that these need (additive,
  trivially satisfied).
* **Inventory is item-owned.** Since 2026-10 several variants may share one
  InventoryItem; levels are keyed by (item, location), never by variant, so
  shared stock is stored once, never counted twice and never rebound to a
  different variant.
* The InventoryLevel GID is the parameterised canonical form
  `gid://shopify/InventoryLevel/<n>?inventory_item_id=<m>`, validated in full
  (one parameter, no encoding, no fragment, `<m>` = the parent item) and
  kept as the level's identity.
* Lifecycle: rows are never hard-deleted. Products, variants and locations
  become `deleted` only after a complete traversal **and** Shopify's own
  `nodes(ids:)` answer that each one is gone; items and levels become
  `detached` when the snapshot no longer references them. Tombstones keep
  their ids, so local references survive.
* CHECKs: GID formats, closed status / policy / tracking sets, exact
  non-negative `NUMERIC` prices, ISO currency, image consistency, lifecycle
  consistency, run lease shape, and **commit evidence** (a committed run must
  carry version, currency, mode and counts; inventory counts are non-null
  exactly when inventory was read per location).
* The migration refuses to run without the 0121 foundation tables, and
  refuses any pre-existing catalog table or connection key (complete,
  partial or malformed) without changing anything. Downgrade drops only
  what it created. Normal bootstrap (pinned to `0093`) never applies it;
  `REPOSITORY_ALEMBIC_HEADS` moves from `0121` to `0122` (0121 remains an
  accepted checkout and explicit target).

## The run

```
admission (actor → connection → run locks; actor re-checked under lock)
  → preflight: shop identity, Shopify-reported scopes, EXACT productsCount ≤ 25,000, currency
  → [locations]            (only when read_inventory is granted and reported)
  → products, 5 per page, each with every nested page of media, variants and variant media
  → [inventory levels]     (distinct items, 20 per query, every nested page)
  → product count + shop currency re-verified
  → Shopify confirms every absence (nodes(ids:) → null)
  → one-transaction publish under the fence
```

### Completeness — proven, not assumed

Shopify pagination is not an atomic snapshot. A run publishes nothing unless:

* every connection reached `hasNextPage = false`; a repeated or missing
  cursor, or an empty page that promises more, is `pagination_loop`;
* every product's EXACT `variantsCount` equals the variants read **and**
  every follow-up variants page reports that same count. Any drift — a count
  change between pages, a vanished owner — discards the **whole unit** (the
  products page with all its nested pages) and reads it again, so product
  metadata, options and variant list always come from one consistent
  reading; after 2 restarts the run fails `source_changed_during_import`;
* Shopify's EXACT `productsCount` equals the products read, after one
  bounded re-read, and the shop currency is unchanged (mixed currencies are
  refused, never labelled with one);
* every queried field is **present** (an omitted key is `invalid_response`
  even when nullable; an explicit `null` is kept) — a valid-JSON partial
  payload can never clear a stored value;
* identities are unique where Shopify says so (duplicate products, variants,
  locations, levels, media within one connection → `duplicate_identity`);
  a file may be shared by several products and their variants (Shopify file
  references), but a variant's or featured media must be one of its own
  product's media, with one payload per media GID per run;
* the catalog is within bounds: ≤ 25,000 products (Shopify's pagination and
  exact-count cap), ≤ 2,048 variants per product, ≤ 200,000 variants,
  ≤ 5,000 locations, ≤ 1,000,000 levels — beyond that `catalog_too_large`.

### Publication

One transaction under the fence. Upserts keyed by identity; a row whose
stored Shopify `updatedAt` is newer than the staged one is **not**
overwritten (`stale_skipped`) and keeps its previous `last_seen_run_id`
(seen is not refreshed). Its relationships survive with it: an inventory
item is detached only when no present variant references it, and absent
levels are detached only for items this run actually traversed. A failed,
cancelled, interrupted, partial or inconsistent run publishes nothing; the
last committed mirror stays exactly as it was and staged rows are deleted.

### Fences and concurrency

* At most one running run per connection (partial unique index).
* **Admission** takes the merchant actor's user and tenant rows `FOR SHARE`
  through the foundation's `recheck_actor_locked` (unchanged), then the
  connection `FOR SHARE`, then inserts the run. A demotion, deactivation,
  re-assignment or tenant deactivation committed first refuses the start
  (`actor_revoked`); one arriving after waits for the admitting commit.
  Support impersonation and platform staff can never start a run.
* Every runner write goes through `fence`: locks **connection → run** (the
  global order), and only then reads the write-time clock. A runner that
  waited on a lock past its lease's expiry is refused, never renewed. The
  fence checks the run's full identity (id, lease, tenant, connection,
  generation, shop), that the connection is still `active` with the run's
  generation and shop, and (for data writes) the credential version the
  data was fetched with. `publish` re-checks the lease immediately before
  commit.
* The GraphQL client calls the fence **before every attempt** (including
  each retry after a throttle or backoff wait) and **after every response**,
  before parsing: a disconnect, uninstall (quarantine), re-authorization,
  reinstall (generation bump) or lost lease stops the run without another
  call with the captured token. A credential rotation (another worker's
  refresh) discards the page and fetches it again with the current
  credential; a token is only ever paired with the version it came from.
* Disconnect / uninstall / quarantine → `cancelled connection_unavailable`;
  reinstall → `cancelled connection_changed`; an expired lease without a
  result is `interrupted` (closed by the next start, never by a status read).
* Bounded waits: cost-aware pacing from `throttleStatus`, `THROTTLED`
  retries (8), HTTP retries for 429 (`Retry-After`, capped at 30 s) / 5xx /
  timeouts (5), each attempt as a whole (connect, send, complete streamed
  body) bounded by 60 s and by the run's remaining 6-hour budget, response
  size 8 MB, every single wait ≤ 30 s.

## Inventory, scopes and availability

**Current foundation: `read_products` only** (requested and accepted). With
it the import reads products, variants, options, exact prices, images,
inventory policy and InventoryItem `tracked` / `updatedAt`, and records the
run's `inventory_mode = scope_missing`: locations and levels are not read and
their counts are **null, not 0**, in the run, the status API and the UI.

Per-location inventory is read only when Shopify's own
`currentAppInstallation.accessScopes` **and** the stored grant both include
`read_inventory`. The PG proofs exercise that path with a mocked wider grant;
**this does not mean any current live connection can import inventory**.

### Scope proposal (documentation only — nothing requested or granted here)

Coordinator-verified against the official 2026-10 reference (2026-10-11):
ProductVariant needs `read_products`; InventoryItem `read_inventory` or
`read_products`; InventoryLevel requires `read_inventory`; Location is
readable with `read_inventory`, `read_locations` or `read_markets_home`.

* **Proposed minimum for per-location inventory: `read_products` +
  `read_inventory`.** `read_locations` is not needed (`read_inventory`
  suffices for the Location fields used) and is not proposed.
* Requires a later, approved slice that widens the foundation's
  `REQUESTED_SCOPES` / `ALLOWED_GRANTED_SCOPES` and re-authorizes every
  connected store. Until then the import honestly reports `scope_missing`.

### Availability view (`store.variant_availability`) — consumed by nothing yet

Read-only, derived, and `unknown` whenever the evidence does not support a
number: connection not active, published run from a previous installation,
deleted variant, bundle (`requiresComponents`), detached item, any item /
variant / level not refreshed by the committed run (stale), or inventory not
read. Otherwise `untracked` (with the inventory policy), or `tracked` with
`online_fulfillable_available` = the sum of `available` over present levels at
locations that are **active and fulfil online orders** only, plus the number
of excluded locations and of variants sharing the item (the figure is per
item: never add it across variants). `ProductVariant.inventoryQuantity` is
not read: it totals locations that cannot fulfil online orders.

## Merchant state

`GET /merchant/integrations/shopify/catalog-import/status` — per connection:
`connection_status`, and `import.state` for the **current installation**:

| state | meaning |
|-------|---------|
| `not_started` | no run for this installation (also after a disconnect or reinstall) |
| `importing` | a run holds a live lease (connection active) |
| `imported` | the latest run of this installation committed |
| `error` | the latest run failed / was cancelled / was interrupted, or the connection is no longer active — with a fixed `reason` |

`last_success` is the latest committed run's evidence (time, API version,
counts, removals, inventory mode and counts — null when not read) with
`current_installation` true/false. The dashboard shows "complete" only for
server-committed evidence of the current installation; reasons map to fixed
copy; support impersonation sees the state but cannot start.

## Not supported in this slice (explicit boundaries)

* Projection into `products` / `product_variants`, AI / WhatsApp / Meta /
  campaign use, Meta export, WhatsApp publication.
* Catalogs over 25,000 products (needs a bulk-operation strategy; a bulk
  result's `partialDataUrl` is a partial failure, never a full snapshot).
* Webhooks and subscriptions of any kind (a partial variant webhook would only
  ever be a reconciliation hint, never a snapshot); scheduled re-imports.
* Markets / contextual prices and currencies (base price in the shop's
  default currency only); bundles (flagged, availability unknown);
  subscriptions (`requiresSellingPlan` flagged); combined listings (role
  recorded only); gift cards (flagged); descriptions, tags, collections,
  metafields, translations, SEO.
* Scope widening / re-authorization; orders, customers, payments.
* Live validation against a real store and the 2026-10 schema (the fields
  used are coordinator-verified; this environment's fetch of shopify.dev was
  refused by the network policy, HTTP 403).
* A dedicated worker: the run executes as an in-process background task; a
  production activation needs a durable worker and a statement timeout that
  fits the publish transaction (the default session sets 15 s).

## Read-only collision audit

```bash
DATABASE_URL=postgresql://… python scripts/audit_shopify_catalog_collisions_readonly.py
```

One `READ ONLY` transaction, counts only. Reports schema presence (0121,
0122, the connection key), legacy hub rows with `source = 'shopify'` (WARN —
never adopted), Shopify GIDs stored in `products.external_id` or
`product_variants.salla_variant_id` (FAIL), same-tenant raw-id overlap
between the mirror and Salla/generic rows (INFO — harmless by construction),
and mirror integrity (FAIL on present variants of deleted products or
detached items, present levels at deleted locations or of detached items,
staged rows of finished runs; WARN on running runs with an expired lease).
Exit 0 / 1 (FAIL) / 2 (not run). Not executed against any live database.

## Tests

```bash
python -m pytest -q tests/test_shopify_catalog_import.py           # DB-free, in the default root suite
NAHLA_RELIABILITY_REQUIRE_PG=1 NAHLA_RELIABILITY_PG_ADMIN_DSN=postgresql://…/postgres \
  python -m pytest -q backend/tests/test_shopify_catalog_import_pg.py   # inventoried: shopify_catalog_import
cd dashboard && npm run check:shopify-catalog-import-ui
```
