# Moyasar readiness gates

> **Conflict to resolve before any design relies on it (noted 2026-10-04).** The
> "checkout splits" item below comes from earlier public documentation. PR #1194
> (`docs/payments/moyasar-platform-api-readiness.md`) records a 2026-10-03
> statement from Moyasar that Marketplace split — one payment split across
> merchants — is not supported at present. Neither statement was re-verified for
> this note; until the account-specific agreement settles it, no per-payment
> provider split is designed, stored or displayed.

Nahlah Payments remains dormant until the account-specific Marketplace contract
and scopes are confirmed. Public Moyasar documentation currently supports the
following design facts:

- Platforms/marketplaces support merchant onboarding/KYB, flexible fees,
  checkout splits (disputed — see the note above), per-merchant settlements and
  optional payouts.
- Standard Payments API supports create/fetch/list/update, full or partial
  refunds, capture and void.
- Webhooks use an HTTPS endpoint and a shared secret; documented payment events
  include paid, failed, refunded, voided, authorized, captured and verified.
- Aggregation settlements can be queried by API, including settlement lines,
  and `balance_transferred` can notify a configured webhook.
- The published aggregation settlement cadence is Monday and Thursday, but the
  agreement may override it.

## Implemented and deliberately dormant

- provider-neutral merchant profile with opaque provider references only
- versioned Decimal platform-fee policy
- provider-confirmed payment observations and provisional allocations
- provider-reported settlement records
- tenant-scoped merchant read model
- authenticated/deduplicated provider-event ledger primitive
- no wallet, stored value, payout instruction, card storage, live route, or
  automatic production migration

## Contract-dependent blockers

Do not implement or activate these from guesses:

1. Nahlah Marketplace Agreement and exact platform account classification.
2. Sub-merchant onboarding handoff/API, required fields, status vocabulary and
   authoritative merchant identifier.
3. Marketplace credentials/scopes and sandbox entitlement.
4. Exact split/application-fee request schema and tax treatment for Nahlah's fee.
5. Settlement recipient mapping for each sub-merchant and whether Nahlah uses
   aggregation settlement APIs, platform-specific APIs, or another rail.
6. Refund/chargeback allocation rules between merchant and platform fee.
7. Whether optional Moyasar Payout is part of Nahlah's agreement.
8. Account-specific webhook routing/secret model for platform vs sub-merchants.

## Activation checklist

Before any live merchant is enabled:

- contract/scopes documented in-repo without secrets
- sandbox tenant mapped to provider merchant reference using provider evidence
- credentials stored in the existing secret manager/environment mechanism
- HTTPS webhook route added with documented authentication and event mapping
- payment creation/fetch verified end-to-end in sandbox
- full and partial refund tests completed
- settlement + settlement-lines reconciliation completed
- duplicate/out-of-order webhook tests completed
- cross-tenant access tests completed
- finance/operator runbook and incident rollback documented
- migration 0115 reviewed/applied explicitly in the target environment
- production activation remains separately approved
