# Shopify roadmap — slices, permissions and activation gates

Status: **planning document only.** It implements nothing, requests no
permission and activates nothing. It does not replace any existing document;
the per-slice design documents remain the source of truth for what each
Draft actually contains. The companion test plan is
[`shopify-regression-acceptance.md`](shopify-regression-acceptance.md).

## Release blocker

The known divergence between `main` and the deployed catalog release is a
**release blocker** for the whole Shopify stack. This `main`-based stack must
never be deployed over the working catalog. Nothing here proposes a merge or
a deployment, and every slice requires an independent review before any
merge or release.

## Owner acceptance rules (every slice)

* Protect existing code and every previously working feature. Keep each
  change small and scoped; never weaken a test and never silently replace an
  existing implementation.
* Run the existing Salla, Meta catalog, tenant isolation, billing and agent
  non-interference regression coverage next to the new tests. Record the
  affected paths and the **baseline versus candidate** results with exact
  SHAs, commands, counts, skips and failures (see the acceptance document).
* No AI agent behaviour, runtime, prompt, persona, dialect or model change:
  every GOV-001 / GOV-002 change flag stays `NO`.
* Keep the evidence classes apart: **implemented** (code with automated tests
  on mocks or disposable PostgreSQL), **mocked** (behaviour asserted only
  against an in-process fake), **real development store** (observed on a
  Shopify development store). One is never reported as another.

## Slices

### Implemented as dormant Drafts (flags off, not deployed)

| Slice | Draft | What exists | Evidence class |
|---|---|---|---|
| Secure connection foundation | [#1202](https://github.com/alayedharthi-stack/nahla-saas/pull/1202) | standalone authorization code, expiring offline tokens, `read_products` only, uninstall quarantine and reconciliation | implemented; Shopify mocked |
| Merchant connection UI | [#1203](https://github.com/alayedharthi-stack/nahla-saas/pull/1203) | connection card and completion page behind a UI flag | implemented; browser harness against a local backend |
| Read-only catalog import | [#1204](https://github.com/alayedharthi-stack/nahla-saas/pull/1204) | provider-scoped mirror, Admin API 2026-10, no shared-hub projection | implemented; Shopify mocked |
| Privacy compliance webhooks | [#1205](https://github.com/alayedharthi-stack/nahla-saas/pull/1205) | durable intake of `customers/data_request`, `customers/redact`, `shop/redact`; bounded assessments; blocked-policy proposals; fixture-only cleanup | implemented; Shopify mocked; no subscription registered |

None of them has been exercised against a real Shopify development store.

### Planned (not started; each a separate isolated Draft)

| # | Slice | Permissions it would need | Gate before any activation |
|---|---|---|---|
| L1 | Orders read model (provider-scoped mirror) | `read_orders`; `read_all_orders` **only** if history older than 60 days is genuinely needed | owner approval of scope widening and of re-authorizing every connected store |
| L2 | Buyer fields (name, email, phone, address) | Shopify **protected customer data** approval, field by field for a public app | least privilege: no field without an approved use and an approved retention |
| L3 | Shipping / fulfillment read model | `read_fulfillments`; FulfillmentOrder scopes **only** for responsibilities the app actually has | evidence-based shipment state only |
| L4 | Tracking updates | fulfillment webhook topics declared in the app configuration | duplicates, delays and out-of-order events proven; no "delivered" without carrier evidence |
| L5 | COD order confirmation | approved message templates and consent (WhatsApp policy) | buyer confirmation is its own fact |
| L6 | Delivery confirmation | carrier / fulfillment evidence | delivery never implies payment |
| L7 | Payment collection (COD collected) | an authoritative collection source chosen by the owner | verified collection only; any consequential write needs explicit merchant authority |
| L8 | Buyer and merchant notifications | approved channels, templates, test recipients, consent | no unapproved channel fallback; Shopify native notifications accounted for |

## Permission and design gates

* Request `read_orders` / `read_fulfillments` only when a slice needs them,
  and `read_all_orders` only when history older than 60 days is required.
* Buyer names, phone numbers and addresses require Shopify protected customer
  data approval, including field-level access for a public app.
* FulfillmentOrder scopes only for fulfillment responsibilities the app
  really holds.
* **Never request a write scope silently.** Every write permission is an
  explicit owner decision recorded before it is requested.
* `orderMarkAsPaid` can create a sale or capture an authorization; it is a
  consequential financial write, not a harmless status toggle, and it is
  never inferred from tracking, delivery or a manual payment method.

### Cash on delivery: three separate facts

Cash on delivery is modelled as three independent facts, each with its own
evidence and state:

1. **Buyer acceptance** of the COD order (the buyer's own response, recorded
   with its message id). Shopify's `Order.confirmed` is an inventory
   reservation, not buyer acceptance.
2. **Shipment delivery** (carrier / fulfillment evidence). One delivered
   parcel never marks a whole multi-parcel order delivered.
3. **Payment collection** (an authoritative, owner-chosen collection source).
   A manual payment gateway alone is not COD proof; delivery never implies
   paid.

Refunds follow the same rule: `refunds/create` does not prove money was
returned — the refund's transactions must be verified before a refund is
reported as successful.

## Owner decisions required before any live activation

* The authoritative COD collection source and the carrier delivery source.
* Which Shopify writes, if any, Nahlah may perform, and under whose
  authority.
* Channels, templates, languages, test recipients and reminder deadlines
  for every notification.
* Retention of every mirror, receipt and notification record.
* The privacy-slice decisions listed in the #1205 design document.

## Real development-store journeys (launch gate)

Required before any launch, and **not run**: no real-store journey is
authorized now. Each is recorded as `NOT RUN — pending permissions and owner
approval`, never as passed.

* paid order;
* COD: confirmation, shipping, delivery and verified collection;
* rejection and cancellation;
* partial shipments;
* partial and failed refunds;
* retries and recovery;
* two-store isolation;
* a re-run of the existing regressions.

## Official references

Parent-verified; this environment's own fetch of shopify.dev is blocked by
the network policy and was not bypassed.

* Protected customer data — <https://shopify.dev/docs/apps/launch/protected-customer-data>
* Webhook subscription topics — <https://shopify.dev/docs/api/admin-graphql/latest/enums/WebhookSubscriptionTopic>
* Order — <https://shopify.dev/docs/api/admin-graphql/latest/objects/Order>
* Refund — <https://shopify.dev/docs/api/admin-graphql/latest/objects/Refund>
* Fulfillment — <https://shopify.dev/docs/api/admin-graphql/latest/objects/Fulfillment>
* `fulfillmentTrackingInfoUpdate` — <https://shopify.dev/docs/api/admin-graphql/latest/mutations/fulfillmentTrackingInfoUpdate>
* `orderMarkAsPaid` — <https://shopify.dev/docs/api/admin-graphql/latest/mutations/orderMarkAsPaid>
* Webhooks — <https://shopify.dev/docs/apps/build/webhooks>
* Verifying webhook deliveries — <https://shopify.dev/docs/apps/build/webhooks/verify-deliveries>
* WhatsApp Business policy — <https://whatsappbusiness.com/policy/>
