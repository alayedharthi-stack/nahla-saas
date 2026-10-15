# Shopify per-shop `app/uninstalled` readiness gate

Status: **Draft source only.** No deploy, no migration, no live token, no
store install, no live Shopify mutation, no credential was used. Live
permission and live uninstall-delivery verification remain **PENDING**.

Base: deployed baseline `59216725836bdb0d104e5b8f5107369999efad63`
(`codex/shopify-install-first-20261013`). `main` does not contain the
Shopify connection code at all (it diverged at `4b5793a2`), so this change
cannot be built on `main`.

## Why

Shopify confirmed that app-scoped (TOML) webhook subscriptions are not
available to an app with `use_legacy_install_flow = true`. The app keeps the
legacy flow, `read_products` only, the existing authorization-code flow and
the configured Admin API version (default `2026-07`, `SHOPIFY_ADMIN_API_VERSION`).
So each shop's `APP_UNINSTALLED` subscription is created per shop through the
Admin GraphQL API with that shop's offline token, and nothing may be ready
before it exists.

## Contract

* Endpoint: `https://<API host of the configured OAuth callback>/webhooks/shopify/app-uninstalled`
  (production: `https://api.nahlah.ai/webhooks/shopify/app-uninstalled`), format `JSON`.
* Exact match: topic `APP_UNINSTALLED`, `uri` equal, format `JSON`, and no
  `includeFields` / `filter` (either could strip or suppress the delivery the
  handler parses). Anything else is not proof; it is never deleted or changed.
* One bounded attempt (`uninstall_webhook.ensure`): list (≤ 4 pages of 50,
  `topics: [APP_UNINSTALLED]`) → exact found: proof, nothing created → else
  create **once** → **always** read back (also after a timeout, 5xx, 429,
  `THROTTLED`, `ACCESS_DENIED` or `userErrors`). Only an exact subscription
  in the read-back is proof. A create whose outcome is unknown is never
  repeated in the attempt; the next attempt starts with a list again.
* Every call runs under the per-shop lease and its deadline (`_guarded_call`).
* Proof = audit row `uninstall_webhook_verified` bound to `connection_id`,
  `tenant_id`, `shop_domain` and the connection **generation** (subscription
  id, uri, format, API version in `detail`; no secret). A reinstall's new
  generation needs its own proof; an older generation's proof never counts.
* `lifecycle.is_ready(conn)` = `active` **and** proven for the current
  generation. It is the one predicate for credential use
  (`active_access_token`, which the catalog import uses), install-first
  hand-offs (`callback` ready paths, `redeem`) and sessions (`validate_session`).

## Ordering (no `active`, no `ready` before proof)

1. Offline grant exchanged and shop identity verified (unchanged).
2. **Store, not ready** (one transaction, shop lease kept): encrypted grant,
   generation bump, tenant/user/owner (install-first), OAuth state completed,
   status `quarantined` with reason `uninstall_webhook_pending`, durable retry
   due after the lease could expire. The code is spent here — it is never
   exchanged again.
3. **Register** under the same lease with the fresh token.
4. **Promote** in the lease's final transaction, only if the lease is still
   held, the generation is unchanged, the row is still in this hold and no
   reconciliation was requested meanwhile (an uninstall delivery or possible
   retirement needs its own probe): write the proof, set `active`, then (and
   only then) issue the install-first hand-off. Otherwise: failure audit, not
   ready, legacy completion answers `503 uninstall_webhook_pending` (never
   "connected"), install-first answers `install_pending` (router: refused).

## Readiness paths reviewed

| Path | Before | Now |
|---|---|---|
| legacy `_claim` (reconnect, lifecycle ~695) | `active` at claim | hold → register → promote |
| legacy `_claim` (first connect) | `active` at claim | same |
| install-first offline exchange (install.py ~274-325) | `active` + `ready` | hold → register → promote → `ready` |
| install-first online phase, connection active | `ready` | `ready` only if `is_ready` |
| install-first online phase, grant held for subscription | offline authorize (new exchange) | no new authorization; registration made due; `install_pending` |
| install-first offline pre-check (concurrent first install) | `ready` if `active` | `ready` only if `is_ready` |
| `redeem`, `validate_session` | `active` + generation | `is_ready` + generation |
| `reconcile` / `_cas_retain` (lifecycle ~1248) | quarantine → `active` | identity proven, then proof required; missing → one registration under the shop lease, else `webhook_pending` with backoff |
| possible-retirement quarantine | forced refresh → `active` | forced refresh, then proof required |
| `active` row without proof (pre-gate) | usable | not ready everywhere; runner claims it, holds it, registers |
| `active_access_token` | `active` | `is_ready` |
| catalog `begin_run` / status views | `active` | unchanged (`active` now implies proof except pre-gate rows, whose credential use is refused) |

## Quarantine semantics (reused, reviewed)

`quarantined` already means "credentials kept, never used" (`CREDENTIAL_STATUSES`,
`ck_shopify_connections_credentials`), is drained by the durable recovery
runner with backoff (30 s · 2ⁿ, cap 1 h), and is lifted only by
`_cas_retain`. The new reason `uninstall_webhook_pending` reuses exactly that:
the grant is preserved (no tombstone, no re-authorization), uninstall
deliveries and possible retirements still override/extend it, disconnect
still tombstones it, a refused credential still tombstones it as
`uninstalled`, and `_cas_retain` now also requires the proof. A stored grant
on an exchanging install flow (`connection_id` set) is no longer treated as an
uncertain exchange by `sweep_expired`.

## Schema

No schema change. The status/reason columns, the credential check constraint
and the audit table (with `connection_id` and `generation`) already express
the hold and the generation-bound proof. The shop lease's `purpose` check
allows only `exchange` / `refresh`; runner registrations use `refresh`.

## Shopify documentation check (what could and could not be verified)

Direct fetches of shopify.dev were not possible from this environment (DNS
failure in the fetch tool; direct HTTP not permitted), so only search-result
excerpts of the official pages were available:

* `webhookSubscriptions` (latest): paginated list of subscriptions "created
  using the API for the current app and shop"; arguments include `first`,
  `after`, `topics`, `uri`, `format`, `query`, `sortKey`; `callbackUrl` deprecated.
* `WebhookSubscription` (latest): `uri`, `format`, `topic`, `includeFields`,
  `filter`; `WebhookSubscriptionInput`: `uri`, `format`, `filter`,
  `callbackUrl` deprecated. The 2026-07 pages were **not** read directly.
* Per-topic access scopes: the subscription guide says each topic needs its
  corresponding scope; the scope for `APP_UNINSTALLED` could **not** be
  confirmed. Therefore `read_products` is **not** assumed sufficient: an
  `ACCESS_DENIED` answer fails closed (grant kept, not ready, retried with backoff).
* Duplicate create: "Address for this topic has already been taken" is
  documented for the Pub/Sub variant and reported by developers for HTTP; the
  code never relies on it (list first, create once, read back).

## PENDING (owner-authorized, live)

* Live verification that `webhookSubscriptions` / `webhookSubscriptionCreate`
  succeed at the configured API version with `read_products` only.
* Live verification that an uninstall of a development store delivers
  `app/uninstalled` to the production endpoint and the existing handler
  quarantines → reconciles → tombstones.
