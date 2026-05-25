# ADR 0002 — Nahla Order Record is an internal commerce abstraction, not a platform-specific mirror

- Status: Accepted (architectural directive — May 2026, Wave 1 of the
  Payment / Receipt Integrity rollout)
- Date: 2026-05-25
- Deciders: Nahla product owner + platform team
- Scope: All future Order-record work. Locked **before** the Wave 1
  W1.5 commit ("Order Record Lifecycle") so no implementation drift
  toward a platform-specific shape can land first.
- Supersedes: Implicit assumptions inside
  `routers/ai_sales.py::create_order`, `services/store_sync.py`, and
  any `integrations/salla/*` flow that an `Order` row is a
  by-product of a Salla / Moyasar object.

## Context

Nahla now serves merchants whose order surface is **structurally
heterogeneous**:

1. Merchants whose live store is on **Salla**.
2. Merchants whose live store is on **Zid** (or any future platform).
3. Merchants who are **WhatsApp-only** — no e-commerce platform at
   all; orders are completed entirely inside the conversation +
   bank transfer / wallet receipt.
4. Merchants with one of (1)–(3) **and** WhatsApp who let customers
   pick a product inside a conversation, then route them either to
   the platform checkout or to a fully WhatsApp-native completion.
5. Merchants whose payment is itself out-of-band (manual bank
   transfer, digital wallet, generated payment link via Moyasar /
   another PSP).

The Wave 1 diagnostic (May 2026) confirmed three concrete
consequences in the current code:

- The `Order` SQLAlchemy model is created today only via
  `routers/ai_sales.py` (a checkout-API path), `services/store_sync.py`
  (Salla mirror), and `routers/webhooks.py` (Moyasar payment).
  None of those is reachable from a verified WhatsApp bank-transfer
  receipt — so a customer who paid in WhatsApp leaves no `Order`
  trail beyond the in-conversation `brain_state.order_prep`.
- There is no first-class concept of an **abandoned WhatsApp order**.
  The only abandoned-cart writers are the Salla sync flow and a few
  Salla webhook adapters; merchants without Salla can't see a
  funnel of dropped customers.
- A merchant with both Salla and WhatsApp can't tell, on the Nahla
  dashboard, which orders were completed inside WhatsApp vs which
  were Salla-native. The two surfaces share the `orders` table but
  do not share a contract.

If we let Wave 1 W1.5 ("Order Record Lifecycle") land as a thin
adapter that calls Salla / Moyasar, we will repeat the mistake:
new merchants on Zid (or no platform at all) will get either a
broken funnel or a silent fallback. That is unacceptable for a
merchant-agnostic product.

## Decision

**An Order record inside Nahla is an internal commerce abstraction.
It is not a 1:1 mirror of any specific platform's order object.**

The Order record MUST be the canonical representation of "a
commercial intent to buy" inside Nahla, and it MUST be valid
independently of whether any external platform integration exists.

### Required fields (contract)

Any Order row created or upserted by Wave 1 W1.5 (or any later
work) MUST carry — at minimum — these dimensions. Names are
illustrative; the migration in W1.5 will reconcile them with the
existing `Order` model:

| Field | Type | Why it matters |
|---|---|---|
| `source` | enum: `whatsapp` / `salla` / `zid` / `moyasar` / `manual` / `external` | Distinguishes WhatsApp-native orders from platform-native ones at query time. Replaces the implicit assumption that `Order.source` is always a Salla shorthand. |
| `external_order_id` | nullable string | When the order was generated on (or echoed to) a platform, this is the platform's id. NULL for WhatsApp-native. |
| `platform_source` | nullable enum: `salla` / `zid` / `moyasar` / `none` | Records the merchant's platform context at order creation. Note: a merchant who has Salla integration may STILL have WhatsApp-native orders — `source` and `platform_source` are independent. |
| `conversation_id` | nullable int (FK to `conversations`) | Mandatory for WhatsApp-sourced orders, optional otherwise. Powers replay + audit. |
| `customer_id` | int (FK to `customers`) | Required. |
| `payment_source` | enum: `bank_transfer` / `wallet` / `card` / `cod` / `payment_link` / `external` | First-class field, not derived from a freeform string. |
| `payment_verification_status` | enum (closed): `verified_match`, `probable_match`, `unclear_receipt`, `account_mismatch`, `fake_or_corrupted`, `text_claim_unverified`, `not_payment`, `not_required` | The verdict from W1.4's verification layer. `paid` flow is gated on `verified_match`. |
| `receipt_metadata` | JSON | Carries the structured `ReceiptFields` (W1.3). NEVER inlines secrets. |
| `fulfillment_status` | enum: `awaiting_payment` / `awaiting_address` / `awaiting_handoff` / `processing` / `shipped` / `delivered` / `cancelled` | Distinct from payment status. |
| `lifecycle_state` | enum: `draft` / `abandoned` / `paid` / `under_review` / `cancelled` / `closed` | The order's commercial state machine. `abandoned` is a first-class state. |

### Architectural invariants

These are non-negotiable for any Order-record code that lands in
Wave 1 (W1.5 onward) or later waves:

1. **No platform coupling at construction time.** A function that
   creates / upserts an Order row MUST accept the source as an
   explicit parameter and MUST work for `source=whatsapp` with
   `external_order_id=None` and `platform_source=None`.
2. **Two source dimensions.** Whether a merchant has a Salla / Zid /
   any platform integration is captured in `platform_source`. The
   channel a SPECIFIC order was created on is captured in `source`.
   These are independent. A merchant with Salla integration can
   absolutely have `Order(source=whatsapp, platform_source=salla,
   external_order_id=NULL)` — that is the WhatsApp-native order
   for a Salla merchant, by design.
3. **No silent platform push.** Just because a merchant has a Salla
   / Zid integration does NOT mean every WhatsApp order must be
   pushed to that platform. Wave 1 explicitly defers any creation
   of carts / orders inside Salla / Zid to a later, separately
   gated phase.
4. **Channel-choice policy** (see "Channel-choice policy" below).
5. **Verified payment is the only state that flips an Order to
   `paid`.** No path may flip `lifecycle_state` to `paid` based on
   a text-only claim or a `probable_match`. `under_review` is the
   highest state allowed for those.
6. **Idempotency.** `(source, external_order_id)` MUST be unique
   when both are non-null. `(source=whatsapp, conversation_id, …)`
   MUST be deduplicated by an explicit deduplication key — never
   by checkout-time race.
7. **No tenant-specific carve-outs.** No code branch may check
   `tenant_id == X` to decide order behaviour. Tenant settings live
   on the merchant config, not in module-level conditionals.

## Channel-choice policy (interim)

When a merchant has an active Salla / Zid integration AND a
customer starts an order conversation in WhatsApp, the AI MUST ask
the customer **which channel they prefer to complete the order
in**. The decision is a customer choice — NOT a system default.

Policy:

- The Brain prompt overlay (added in a later Wave 1 commit) asks
  the customer politely; the wording is left to the LLM (no
  hardcoded canned text — consistent with the Conversational
  Commerce Architecture rollout).
- If the customer chooses **the store**: the AI sends the
  appropriate product / store / cart link. Wave 1 does NOT create
  a Salla / Zid cart automatically. That work is a separately
  scoped phase (post-Wave-1) with its own kill switch and
  regression tests, because cart push can fail / duplicate / miss
  inventory.
- If the customer chooses **WhatsApp**: the order is treated as
  WhatsApp-native. `Order(source=whatsapp, conversation_id=…,
  platform_source=<merchant's platform if any>,
  external_order_id=NULL)` and the rest of the WhatsApp commerce
  flow (slots / verified receipt / fulfilment) takes over.
- If the customer is ambiguous: stay with WhatsApp until the
  customer signals otherwise. Never silently push the customer
  off-platform.

## Implementation phasing (Wave 1)

The following phasing protects the invariants above:

- **W1.1 (this commit's parent)** — Contradiction Guard. No Order
  schema change.
- **W1.2** — Closed `ReceiptVerdict` enum (telemetry only).
- **W1.3** — Structured `ReceiptFields` extractor (telemetry only).
- **W1.4** — Hardened verification.
- **W1.5** — **Order Record Lifecycle** — the commit that lands
  this ADR's contract. MUST:
  - Add the `source` / `platform_source` / `payment_source` /
    `payment_verification_status` / `lifecycle_state` /
    `fulfillment_status` / `receipt_metadata` fields (or reconcile
    them with the existing `Order` model via a migration).
  - Provide a `core/order_record.py` module whose public function
    `upsert_order_for_verified_payment(...)` is platform-agnostic.
  - Behind `RECEIPT_ORDER_RECORD_UPSERT_ENABLED` (default OFF),
    rolled out staged.
  - Architectural test: no reference to `salla`, `zid`, `moyasar`
    inside `core/order_record.py`. The module is platform-blind.
- **W1.6** — Payment-path auto-pause guard.

The channel-choice policy has its own commit AFTER Wave 1 (it
touches the Brain prompt overlay, not the Order model).

## Consequences

Positive:

- New merchants on Zid (or WhatsApp-only) get a working order
  funnel from day one without per-platform code branches.
- Dashboard queries can filter by `source` / `lifecycle_state`
  consistently across all merchants.
- The `paid` state machine is gated on the verification verdict,
  closing the `verified_match` ↔ `paid` invariant the Wave 1
  diagnostic identified as the most-failing path.

Negative / costs:

- W1.5 will need a database migration (additive only; no
  destructive changes to existing rows). The migration must be
  back-fillable from `extra_metadata` for existing rows so we
  don't lose history.
- Existing call sites in `routers/ai_sales.py`,
  `services/store_sync.py`, `routers/webhooks.py` will need a
  light shim to set `source` correctly. Each shim is small and
  has its own regression test.
- The "interim" channel-choice policy means we WILL temporarily
  carry two surfaces (the existing Salla draft-order push for
  customers who chose the store + the new WhatsApp-native order
  for customers who chose WhatsApp). This is by design until the
  cart-push phase ships.

## Notes for future agents reading this ADR

- Do NOT remove this ADR or downgrade it to "informational"
  without a counter-ADR. The product owner pinned this contract
  before W1.5 explicitly to prevent drift.
- Wording inside the bot (the customer-facing channel-choice
  question) is generated by the Brain. Do NOT add hardcoded
  Arabic templates for it.
- If a future merchant requires automatic Salla / Zid order push,
  build that as a SEPARATE module with its own ADR — never as a
  side-effect of Order creation.
