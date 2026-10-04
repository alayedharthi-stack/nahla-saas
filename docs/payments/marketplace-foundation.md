# Nahlah Payments — marketplace foundation

> **Status update 2026-10-03.** Moyasar has stated that Marketplace split
> payments are not currently supported; the offered alternative is a Platform
> API with one independent account per merchant. Platform API documentation,
> sandbox, platform-fee terms and settlement terms remain pending a signed
> agreement, and there is no wallet. Read this document together with
> `moyasar-platform-api-readiness.md`, which records that update, what the
> repository closed in response (revision `0116`), and what still depends on
> Moyasar or on a business decision. The "Marketplace Agreement" wording below
> is kept as history of the original design; it is no longer the target.

## Decision and boundary

Nahlah records provider-backed facts for one merchant tenant. Moyasar performs
regulated payment acceptance and settlement under a future Marketplace
Agreement. The tables and fee quote code in this PR do not charge a customer,
register a merchant, generate a link, process a webhook, move funds, or create a
wallet. No route imports this package. There is no default platform percentage;
an authorized fee policy must be set explicitly for each tenant and mode.

The shared conversation includes Moyasar Customer Care's reply: public APIs do
not automatically onboard sub-merchants; multi-merchant capabilities require
custom activation, compliance review and a Marketplace Agreement. Its wording
about a platform capturing funds does **not** authorize routing customer money
through Nahlah's own bank account. Contractual fund flow and merchant keys must
be confirmed directly with Moyasar before activating an adapter.

## Repository audit on `origin/main` (053a40c6)

- `database.models.Tenant` is the merchant tenant. `Order` is tenant-scoped but
  its `total` is a string and its `checkout_url` may be from a store adapter.
- `PaymentSession` is a legacy invoice/session record with `Float` amount and
  a simple order FK. It cannot be the marketplace accounting ledger.
- `backend/payment_gateways/moyasar.py`, `backend/routers/billing.py`, and
  `backend/routers/webhooks.py` handle invoices/subscriptions and existing
  merchant payment sessions. None is changed here.
- `webhook_events` already records general raw payloads, but does not prove
  the scoped, authenticated marketplace event and accounting linkage required
  here. The new event key/digest table is dormant, with no raw card data.
- Startup calls the application `Base.metadata.create_all`. Payments use a
  separate `PaymentBase`, so a code deployment cannot silently create these
  relations. Revision `0115` is an explicit sibling of dormant `0113`, both
  descending from `0112`; the repository has multiple historical heads. The
  closed migration topology contract accepts this explicit fourth head while
  normal bootstrap remains pinned to `0093`.

## Data rules

Every new table has `tenant_id`; payment, policy, event and settlement rows also
reference that tenant's provider/mode profile through composite foreign keys.
Provider references are unique across tenants
within `(provider, environment)`; test and live data do not collide. Allocation
uses a composite tenant/transaction/amount FK, and the database checks that its
two gross components sum exactly to that payment's amount. A quote is only a **provisional calculation**;
it does not imply a payment is earned, settled or payable. Rates and amounts
use `Numeric` / `Decimal`, and an allocation snapshots the applied rate.

The profile stores opaque provider merchant and bank references, with approval
evidence required to mark it approved. It stores no ID number, IBAN, document,
API key, or card details. The provider event table stores a digest and dedup
reference only. No event receiver is wired until authentication, tenant binding,
ordering and provider reconciliation are reviewed against the activated setup.

The `PaymentProvider` boundary currently defines merchant-scoped, verified
reads. An implementation must reject a response whose merchant reference does
not match the tenant's approved profile. Onboarding, checkout creation,
settlement reconciliation, refunds, fee/tax treatment and dashboard summaries
remain separate reviewable phases.

The dormant `observe_payment` service now performs one provider read outside
its write transaction, checks the approved tenant/provider/mode profile before
and after that read, and refuses merchant/reference/amount mismatches. It
records the observed provider status and an effective tenant-specific fee
policy snapshot atomically. Repeating an observation preserves the original
amount and fee quote; a provider outage writes nothing. This is a provisional
record even when the provider reports a paid status: the service does not mark
an order paid, calculate a net settlement, or expose a merchant balance.
The caller must supply an authenticated tenant scope; the service has no route.
Concurrent insert races are stopped by the database unique constraints; a
caller may retry after a conflict. Provider status updates, refund transitions
and chargebacks require a separate reconciler with documented state rules.

## Public Moyasar documentation verified 2026-09-29

- [Platforms and marketplaces](https://moyasar.com/en/solutions/platforms-and-marketplaces/)
  describes custom onboarding, fees, checkout and settlement capabilities.
- [Platform terms](https://moyasar.com/en/resources/platform-terms-and-conditions/)
  describe the platform, beneficiary and PSP roles and provider verification.
- [API authentication](https://docs.moyasar.com/api/authentication) uses secret
  keys and HTTP Basic, with separate test/live modes.
- [Create invoice](https://docs.moyasar.com/api/invoices/01-create-invoice)
  provides hosted payment pages, but does not itself specify marketplace
  sub-merchant allocation under Nahlah's prospective contract.
- [Webhook reference](https://docs.moyasar.com/api/other/webhooks/webhook-reference)
  documents event IDs and a `secret_token` in the webhook object. The existing
  repository also assumes an HMAC header; do not reuse that assumption without
  confirming the exact configured delivery format and authenticating it.
- [List settlements](https://docs.moyasar.com/api/settlements/01-list-settlements)
  returns transfers to a merchant bank account with recipient identifiers;
  [settlement lines](https://docs.moyasar.com/api/settlements/03-list-settlement-lines)
  include payments, fees, refunds and chargebacks. These are the basis for a
  future read-only reconciliation, subject to actual account permissions.
- [Payouts introduction](https://docs.moyasar.com/guides/payouts/introduction)
  describes a separate payout product funded by a bank account or digital
  wallet. Its public endpoint is not assumed to be marketplace settlement.

## Activation questions for Moyasar

1. Executed Marketplace Agreement, precise fund flow and whether Nahlah's
   account ever receives merchant proceeds or has any fee/chargeback liability.
2. Sub-merchant registration flow, approved status evidence, merchant ID,
   onboarding URL or private API, and how credentials are scoped.
3. Checkout payload for merchant allocation and platform fees, fee and VAT
   responsibility, partial refunds and chargebacks; test credentials and
   provider reconciliation examples.
4. Settlement recipient mapping, pending versus completed facts, webhook
   secret format, retries, event ordering, and accessible settlement/line APIs.
5. How an internal order is bound to an observed payment with a DB-enforced
   tenant-safe FK. The existing `orders` key is global but not declared unique
   as `(tenant_id, id)`; no speculative order relation is added here.

Before any migration, inspect the target's Alembic revision and actual schema;
apply `0115` explicitly only after approval. No `upgrade head`, startup change,
or production migration is part of this PR.
