# Shopify privacy compliance webhooks — dormant intake, evidence and proposed actions

Status: **Draft PR, disabled by default, not deployed, no live configuration.**
Stacked on the Shopify catalog import (PR #1204, head `08428cb`), which is
stacked on the merchant connection UI (#1203) and the connection foundation
(#1202). Those branches are not modified by this slice.

What this slice is **not**:

* not a registration of any webhook subscription — the three mandatory
  compliance topics are app-configuration / Dev Dashboard work, not an Admin
  API call, and nothing here configures, registers or deploys anything;
* not an App Store review approval and not a statement of legal compliance;
* not an erasure of any real customer or shop data — destructive execution
  stays blocked until the owner decisions below are made and approved;
* not a response to a merchant or to Shopify — nothing here emails, exports
  or discloses anything to anyone.

"Implemented and tested" below means: the endpoints, storage and state
machine exist in this branch and pass the listed tests against mocks and
disposable PostgreSQL. It never means subscribed, reviewed or compliant.

## Official requirements (sources and fetch limitation)

Sources:

* <https://shopify.dev/docs/apps/build/compliance/privacy-law-compliance>
* <https://shopify.dev/docs/apps/build/webhooks/verify-deliveries>

This environment could **not** fetch shopify.dev (WebFetch:
`getaddrinfo ENOTFOUND shopify.dev`; curl through the agent proxy:
`CONNECT tunnel failed, response 403`). The facts below were verified
independently by the coordinator on 2026-10-11 and are used as given; this
slice does not claim its own fetch succeeded.

* App Store apps must implement **and subscribe to** `customers/data_request`,
  `customers/redact` and `shop/redact`, even when the app collects no personal
  data.
* Deliveries are JSON `POST`s. A delivery whose raw-body HMAC is invalid must
  be answered **401**. A received delivery is acknowledged with a **2xx**.
* The action deadline is **30 days** after receipt. A legal retention
  exception requires an owner/legal policy; this slice does not invent one.
* `shop/redact` is delivered about **48 hours after uninstall**.
* `X-Shopify-Hmac-Sha256` is the **base64 HMAC-SHA256 of the raw body** with
  the app's client secret. Headers — topic, shop domain, webhook id, event id,
  triggered-at — are **not** covered by the signature.
* HTTPS; the whole delivery must complete within **5 seconds**; failed
  deliveries are retried **8 times over 4 hours**.
* Mandatory compliance subscriptions are declared in the app configuration
  (Dev Dashboard / `shopify.app.toml`), not registered through the Admin API.
* Per the privacy documentation, the `customer` object of a customer request
  may carry only an email: a customer id is not required blindly.

## Plan

1. **Intake** (`backend/routers/shopify_privacy_webhooks.py`, flag
   `NAHLA_SHOPIFY_PRIVACY_INTAKE_ENABLED`, default off → 404). Plural paths
   only (`/webhooks/shopify/privacy/...`): the singular `/webhook/` prefix is
   rewritten to HTTP 200 by `core.middleware._safe_fallback_response`, which
   would turn a storage failure into a false acknowledgement.
   * `POST /webhooks/shopify/privacy/customers-data-request`
   * `POST /webhooks/shopify/privacy/customers-redact`
   * `POST /webhooks/shopify/privacy/shop-redact`
   * `POST /webhooks/shopify/privacy` (one URI for all three; topic from the
     header, still checked against the signed body)

   Order: bounded streamed body (413; the bound is the foundation's 64 KiB,
   because its verified HMAC helper is reused unchanged — about 4,500 order
   ids; a larger genuine delivery would be refused and redelivered, so
   raising it is a rollout prerequisite) → secret present, else **401** (fail
   closed, never "dev accept") → single `X-Shopify-Hmac-Sha256`, raw-body
   HMAC via the foundation's `verify_webhook_hmac` (401) → `Content-Type:
   application/json` (415) → topic header in the closed set and equal to the
   path's topic (400) → strict JSON (UTF-8 object, duplicate keys refused) →
   **the signed body is classified on its own** and must equal the claimed
   topic (400) → canonical signed shop identity (`shop_id`, exact canonical
   `shop_domain`), unsigned `X-Shopify-Shop-Domain` must merely agree (400) →
   durable receipt committed → **200**. Any storage problem → rollback →
   **503** (Shopify retries). Response bodies are fixed codes only.

   **Event loop and budget.** No database work runs on the event loop. The
   body must arrive within 1 s (else 408); HMAC and validation are in-memory
   on at most 64 KiB; the durable receipt — slot admission (non-blocking: a
   full pool answers 503 at once, no backlog), pool checkout, connect,
   schema inspection on the same bounded connection, receipt SQL and commit
   — runs on a dedicated 4-thread pool with its own small engine (shared
   `DATABASE_URL`, separate pool, 1 s checkout, 2 s connect, 2.5 s statement,
   1.5 s lock timeouts; the shared engine is unchanged) and is answered within
   3 s, so the answer comes within about 4 s of Shopify's 5 s. A thread
   cannot be cancelled: a receipt still running at the deadline keeps its slot
   and connection until its own timeouts end it, re-checks the deadline
   before the receipt SQL and before committing (rolling back when passed),
   and a commit already in flight may still complete after the 503 — the
   redelivery then attaches to the same open request (a duplicate count,
   never a lost or falsely acknowledged receipt). The HTTP timeout does
   **not** terminate a database thread: a thread stalled on the network is
   not ended by the server-side statement timeout and may stay blocked until
   the operating system abandons the connection. The four-slot bound and the
   dedicated pool stop such threads from multiplying (later deliveries get an
   immediate 503) — a mitigation, not cancellation or hard network
   termination.

2. **Signed-body topic classification** (no header ever decides an action):

   | topic | required | forbidden |
   |---|---|---|
   | `customers/data_request` | `shop_id`, `shop_domain`, `customer` object, `orders_requested` array (bounded, may be empty), `data_request` object with positive `id` | `orders_to_redact` |
   | `customers/redact` | `shop_id`, `shop_domain`, `customer` object, `orders_to_redact` array (bounded, may be empty) | `data_request`, `orders_requested` |
   | `shop/redact` | `shop_id`, `shop_domain` | `customer`, `orders_requested`, `orders_to_redact`, `data_request` |

   A body with markers of two topics or an incomplete set classifies as
   nothing and is refused. A retained signed `customers/data_request` body
   therefore can never become a redact or `shop/redact` request by changing
   the unsigned path or header, and no signed body is actionable under a
   topic it was not built for. A `customer` object may carry only an email
   (or only a phone); the id is optional and nothing of it is stored.

3. **Storage** — new `ShopifyPrivacyBase` metadata (not `models.Base`, not the
   foundation's or the mirror's metadata) and revision **0123** (after 0122),
   additive and fail-closed; startup / normal bootstrap never builds it:
   * `shopify_privacy_requests` — one row per logical request: topic, signed
     shop domain + shop GID, SHA-256 of the raw body, matched
     `(connection_id, tenant_id)` through the composite key
     `uq_shopify_connections_id_tenant` (NULL for unknown / conflicting
     identity — never attributed to a tenant by guess), receipt generation,
     state, fixed reason code, counts-only proposed action, audit version,
     eligible / closed generation, worker lease, attempts, backoff, the
     30-day deadline, completion evidence.
   * `shopify_privacy_deliveries` — capped per-request delivery log. The
     unsigned webhook id is kept only when it is a canonical UUID and the
     triggered-at only as a strictly parsed timestamp; anything else (an
     email- or token-shaped value, free text) is neither stored nor logged.
     Diagnostics only, never evidence.
   * `shopify_privacy_request_events` — append-only transitions (fixed codes
     and counts; no raw customer identifier or payload), **capped at 50 per
     request**: the first receipt and an execution are always recorded; past
     the cap one `event_log_capped` marker is written and further replays /
     evaluations only move the request's counters (`delivery_count`,
     `attempts`), which remain the authoritative fences.

   **Not stored:** the raw body, customer email, phone, customer id, order
   ids, headers or the HMAC. Only the customer reference *kind*
   (`id` / `email_only` / `phone_only` / `none`), the order count and the
   Shopify `data_request.id` are kept.

4. **Idempotency, replay and reinstall** (our state decides, never an
   unsigned timestamp or delivery id):
   * every state except `completed` is **open**; a delivery attaches to the
     open request with the same topic, signed shop identity and body digest
     (partial unique index; concurrent deliveries resolve to one row). No
     closed request ever absorbs a delivery, so nothing can be swallowed by
     an earlier outcome; an identical body after a completion opens a new
     request;
   * each delivery bumps `delivery_count` and records the connection
     generation observed at that moment (our own state). An assessed,
     blocked or reviewed request becomes due again; a worker evaluating it
     meanwhile is fenced by the count at write time and must start over, so
     old open/leased work can never close away a newer delivery;
   * a "do not erase" conclusion for an active installation needs a
     revalidation probe requested **after the latest delivery**; an earlier
     retained probe never covers a later delivery. A reinstall is detected by
     generation (the foundation bumps it on every claim and every tombstone).

5. **Worker** (flag `NAHLA_SHOPIFY_PRIVACY_WORKER_ENABLED`, default off; not
   queued at startup without it). Claims due requests with `FOR UPDATE SKIP
   LOCKED` and a short lease; every write re-checks the lease against the
   write-time clock; lock order is **connection → request** (the foundation
   and mirror order is connection → run); bounded exponential backoff.
   Decisions:
   * identity unknown → `manual_review` / `unknown_shop_identity` for every
     topic: the obligation stays open with its deadline and **no** "no data"
     outcome is recorded. A reconsideration pass makes such a request due
     again once a connection owns the signed domain or GID; the worker then
     re-resolves the exact identity (attribution only to that one ownership
     row). The identity is re-resolved at write time too, so a connect flow
     committing during an evaluation sends the request back to the queue;
   * identity conflicting → `manual_review` (no connection is touched,
     nothing is rebound);
   * any legacy `shopify` footprint (see the audit) or schema drift of the
     audited stores → `manual_review` — never a broad "no data" outcome;
   * customer requests on a matched shop with no legacy footprint →
     `assessed` with the bounded outcome `no_structured_customer_records_in_audited_scope`
     at audit version `shopify-product-only-schema-audit-v1`: the
     product-only integration schema stores no structured Shopify customer
     or order resource. Catalog free text (titles, options, SKUs, vendors,
     location names, media alt text) is **not** assessed for personal data,
     the generic-customer mapping is unresolved and no legal fulfilment is
     asserted — never "no customer data in the tenant or platform";
   * `shop/redact` while the connection is `active` / `quarantined` at the
     generation observed by the latest delivery → one foundation revalidation
     probe (existing durable reconcile path) per newer delivery, then
     `awaiting_evidence`; a retained probe → `manual_review` /
     `active_installation_retained`; a delivery observed before the current
     installation → `manual_review` / `installed_after_last_delivery`.
     Both stay **open** with the 30-day deadline visible: they prove that
     erasing the current installation is unsafe, not that the obligation is
     inapplicable or fulfilled (historic / retained shop data stays an owner
     decision). Only a Shopify-confirmed `uninstalled` connection whose
     tombstone generation matches the latest delivery's installation, with
     no in-flight authorization, shop lease or live import run, yields a
     counts-only proposed action in `blocked_policy`.

6. **Destructive scaffolding** — a fixture-tested provider-owned cleanup
   helper (mirror rows, staged rows and import runs of exactly one
   `(connection, tenant)`), and an operator-only executor that refuses unless
   the destructive flag **and** an approved policy object whose scope is
   exactly `{catalog_mirror}` are present (any extra scope refuses), holds the
   connection row `FOR NO KEY UPDATE` (blocks a reinstall claim, an uninstall
   and the catalog fence; does not block foreign-key audit inserts), and
   re-validates under that lock the audit version, the audited
   schema snapshot and the legacy footprint (any change refuses, deletes
   nothing and leaves the request open). The runtime's approved policy is
   `None`; no route or scheduler calls the executor. Completion evidence says
   `executed_scope = catalog_mirror_only`, `legal_fulfillment = not_asserted`
   and `retained_obligations = pending_owner_policy`. The connection ownership tombstone, audit rows, uninstall events,
   OAuth states and privacy receipts are never deleted by this slice.

7. **Observability** — `services.shopify_privacy` and
   `routers.shopify_privacy_webhooks` join `_WITHHELD_FRAME_MODULES`; on
   `/webhooks/shopify/` requests the body is scrubbed and the whole headers
   map (unsigned, caller-controlled names and values) is replaced by one
   fixed sentinel; the HMAC header is scrubbed everywhere. Logs carry fixed codes, request ids and tenant ids only.

8. **Audit script** — `scripts/audit_shopify_privacy_readonly.py`, one
   `READ ONLY` transaction, counts only (no raw rows, config or customer
   values): 0123 collision/presence (privacy tables not recorded by an
   `alembic_version` 0123 — absent table included — or with other columns
   fail), audited-schema drift, legacy Shopify footprint, and, only on a
   validated schema, requests by known topic/state under fixed labels
   (anything else counted under one fixed label, never printed), overdue
   requests and cap integrity.

9. **Tests** — DB-free root-suite tests (HMAC, content type, size, topic
   confusion, identity, middleware non-2xx, Sentry/log scrubbing) and a new
   strict PG suite `shopify_privacy_webhooks` appended to the inventory
   (the 755 baseline entries and rules stay byte-identical), run on PG16 and
   PG18 with TCP SCRAM.

## Data-flow audit (repository at `08428cb`)

Shopify-owned stores written by the dormant slices (audited, versioned as
`shopify-product-only-schema-audit-v1`; the worker compares the live columns
with this snapshot and routes any drift to manual review). The audit attests
the absence of **structured** Shopify customer / order resources only:
catalog text is free-form and may contain personal data entered by a
merchant.

| table | customer fields | shop data |
|---|---|---|
| `shopify_connections` | none | tenant ↔ shop ownership tombstone, encrypted credentials while active |
| `shopify_shop_leases`, `shopify_oauth_states` | none | shop domain, tenant, initiating user |
| `shopify_webhook_events` (uninstall) | none (body digest only) | shop domain / GID |
| `shopify_connection_audit` | none | shop domain, lifecycle actions |
| `shopify_catalog_*` (7 tables) | none (scope `read_products`; no orders / customers) | catalog, locations (name only), runs |

Legacy / generic storage that may hold Shopify-sourced data and therefore
**blocks** any "no data" conclusion while non-empty (counts only):

* `integrations` with `provider = 'shopify'` — `config` may hold
  `store_email`, `store_domain`, tokens; `integrations/shared/tenant_resolver.py`
  can create or rebind such rows;
* `webhook_events` with `provider = 'shopify'` — `raw_headers`, `raw_body`,
  `parsed_payload`, `store_id`;
* `products` with `source = 'shopify'`; `orders` with `source = 'shopify'`;
* `external_customer_profiles` linked to a `shopify` integration;
* `customer_name_provenance` with a `shopify*` source;
* `tenant_settings.store_settings` with non-empty `shopify_shop_domain` or
  `shopify_access_token`.

The current adapter registry selects Salla; legacy rows are nevertheless not
assumed absent. Generic Nahlah / WhatsApp customer data (customers,
conversations, orders of other sources) is separate and is **not** searched:
matching it to a Shopify customer is an account-wide data-mapping decision
(below), and this slice keeps no email or phone to search with.

## Owner / legal / business-policy decisions (open)

1. Retention of privacy receipts, deliveries and events (no period is
   invented here).
2. Retention, scrubbing or transfer of the shop ownership tombstone
   (`shopify_connections`) and its audit / uninstall-event rows after
   `shop/redact`.
3. Legal hold and the account-wide mapping of a Shopify customer to generic
   Nahlah customer data.
4. What evidence lets a shop request apply after a reinstall (this slice
   requires a Shopify-confirmed `uninstalled` connection at the same
   generation and treats everything else as not applicable or for review).
5. Approval and activation of irreversible erasure, and its scope beyond the
   provider-owned mirror (a mirror-only scrub is not claimed to fulfil every
   shop-data obligation).
6. Whether customer identifiers may be retained, encrypted and time-bound,
   for requests routed to manual review (this slice retains none).
7. How and by whom a `customers/data_request` is answered to the merchant
   (no response is produced here).

## Rollout prerequisites (none done here)

Owner decisions above; compliance topics declared in the app configuration
with the exact production URIs; client secret provisioned; revision 0123
applied as an explicit step; intake flag, then worker flag; monitoring of
fixed refusal codes (a signed but malformed body is refused and Shopify
redelivers, then stops); a durable worker; a reviewed policy object before
any destructive execution.
