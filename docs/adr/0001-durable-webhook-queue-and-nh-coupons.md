# ADR 0001 — Durable webhook queue, unified customer upsert, event-driven NH*** coupons

- Status: Accepted
- Date: 2026-04-16
- Deciders: Nahla platform team
- Supersedes: Ad-hoc webhook handling in `integrations/salla/webhooks/handlers.py`,
  legacy `NHL###` coupon generator logic, duplicated customer-creation
  paths in `routers/conversations.py` and `routers/ai_sales.py`.

## Context

Production symptoms reported in April 2026:

1. Automatic coupon generation was not producing the expected short-code
   format (`NH` + 3 alphanumeric chars, e.g. `NH7K2`). Legacy `NHL###`
   codes kept appearing.
2. New orders created in Salla were intermittently not registered in
   Nahla — either silently dropped or duplicated on concurrent webhooks.
3. Customer classification in Nahla did not always match the actual
   store data, because `routers/conversations.py` and `routers/ai_sales.py`
   each had a parallel, silently-failing `Customer` insertion path.

The root cause was structural, not a collection of shallow bugs:

- `POST /webhook/salla` returned `200 OK` *then* did business work in
  the request handler. Any exception was swallowed, so Salla never
  retried and we had no record the event failed.
- Customer creation existed in three places (`store_sync.py`,
  `conversations.py`, `ai_sales.py`) with slightly different phone
  normalisation, so the same customer ended up with duplicate rows
  keyed on different phone formats.
- Order idempotency was enforced in application code only. Two workers
  processing the same Salla `order.created` event raced and produced
  duplicate rows.
- The coupon generator used `secrets.choice` over `string.digits` with a
  `NHL` prefix — only 1,000 possible codes, and no collision handling or
  rollback on partial failure (Salla created, DB failed).

## Decision

We adopt the following architecture:

### 1. Durable webhook event queue

- New table `webhook_events` (migration 0023) — every inbound webhook
  (Salla, WhatsApp, Moyasar, Zid, ...) is *persisted first*, then
  processed asynchronously.
- `POST /webhook/salla` is now a thin receiver: verify signature,
  parse body, `persist_event()`, return `200 OK`. `200 OK` means
  *received and durably stored*, not *processed*.
- `core/webhook_dispatcher.py` is an async worker loop that claims
  batches via `SELECT ... FOR UPDATE SKIP LOCKED` (Postgres), invokes
  per-provider handlers, and advances rows through the FSM:
  `received → processing → processed | failed | dead_letter`.
- Transient failures retry with exponential backoff
  (1m, 5m, 15m, 1h, 6h), then land in `dead_letter`.
- `GET /admin/webhook-events` and `POST /admin/webhook-events/{id}/replay`
  (+ `replay-bulk`) let operators inspect and re-run any failed event.

### 2. Unified customer identity

- `CustomerIntelligenceService.upsert_customer_identity` is the single
  entry point for every path that needs to create-or-update a customer
  (webhook, AI sales, conversations).
- Silent fallbacks to direct `db.add(Customer(...))` were removed.
  Failure now raises or logs `CUSTOMER_UPSERT_FAILED` — never returns
  a silent half-valid row.

### 3. Event-driven NH*** coupon generation

- New code format: `NH` + 3 chars from `[A-Z0-9]` → 46,656 codes per
  tenant. Legacy `NHL###` is grandfathered (never re-issued, but
  recognised for reporting and collision avoidance).
- `_create_one_coupon` is the atomic unit: generate code → create in
  Salla → insert locally. On local IntegrityError it retries with a
  fresh code *and* deletes the orphaned Salla coupon. On hard DB
  failure it still compensates (Salla delete) so the two systems
  cannot drift.
- Coupons are generated both from the scheduled pool-fill AND on
  customer-status transitions via `generate_for_customer`. This is
  the trigger the previous architecture was missing.

### 4. Database-level idempotency

- Partial unique index `uq_orders_tenant_external_id ON orders
  (tenant_id, external_id) WHERE external_id IS NOT NULL AND != ''`
  — concurrent workers can no longer double-insert the same Salla
  order. `handle_order_webhook` catches the resulting IntegrityError
  and falls through to the UPDATE path.

### 5. Structured observability

- `core/obs.py::EVENTS` is the canonical event-name catalogue.
  Every business failure now logs a named event at ERROR level with
  full exception context. Silent `except Exception: pass` is banned
  (see CI lint below).

## Deprecations

The following modules are superseded by the durable queue + dispatcher.
They are kept only to avoid breaking import graphs but MUST NOT be
extended; new functionality belongs in `backend/core/` and
`backend/services/`.

- `integrations/salla/webhooks/handlers.py` — superseded by
  `backend/core/webhook_dispatcher.py::_dispatch_salla` and the
  `StoreSyncService` methods it invokes.
- `coupon-service/` (standalone microservice) — superseded by
  `backend/services/coupon_generator.py`. All coupon issuance now
  happens in-process, atomically with customer-intelligence updates.

Future work should delete these once the transition is verified in
production for at least one release cycle.

## Consequences

Positive:

- No more silent webhook drops. Every inbound event can be traced
  through the DLQ.
- Orders cannot be double-inserted — the database enforces it.
- Coupon pool size is ~47× bigger; collisions are statistically
  negligible, and the ones that do happen auto-recover with
  compensating Salla deletes.
- Customer classification stays in sync because there is only one
  create path.

Negative / trade-offs:

- `200 OK` from `/webhook/salla` no longer guarantees business
  processing succeeded — admins must monitor DLQ.
- Adds a background task (`run_dispatcher_loop`) that must stay
  healthy; a dead dispatcher means `received` rows accumulate.
- One new table (`webhook_events`) grows continuously; needs periodic
  archival of `status=processed` rows older than N days (follow-up work).

## CI guardrails

- `scripts/lint_no_silent_except.py` (invoked from `.github/workflows`)
  greps the backend for `except Exception:\n    pass` patterns and
  fails the build. The exceptions list lives in that script.
