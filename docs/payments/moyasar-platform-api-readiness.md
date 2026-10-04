# Nahlah AI Payments — Moyasar Platform API readiness (status 2026-10-03)

This is the readiness record for merchant payments on Nahlah AI (نحلة). It
supersedes the "Marketplace Agreement" framing in `marketplace-foundation.md`
wherever the two disagree. It is a design and gating document, not a contract.

## What Moyasar told Nahlah AI on 2026-10-03

| Topic | Provider statement (paraphrased) | Design consequence |
|-------|----------------------------------|--------------------|
| Marketplace split | Splitting one payment across merchants (Marketplace model) is **not supported** at present. | Nahlah AI must not design, store or display a per-payment provider split. Allocations stay a *provisional quote*. |
| Alternative | A **Platform API** to manage multiple merchants: one independent account per merchant, with Test/Live keys issued after that merchant is approved. | Profiles, fee policies, activation and credentials are per tenant **and** per environment. One merchant's keys never serve another. |
| Onboarding | Merchant registration and KYC/KYB can be integrated through a private Platform API, with webhooks for registration and activation status. | Onboarding status is a closed state machine with an audit trail; the provider's status words are stored verbatim and mapped only once the contract defines them. |
| Platform API vs Payout | Two separate services. | Two separate dormant provider boundaries. Payout is never treated as the settlement rail. |
| Platform fees | Can be customised and settled separately under a later agreement. | Fee percentage is a per-tenant policy with no default; fee *settlement* is not modelled until terms exist. |
| Documentation and sandbox | Provided only after contracting, setup fees and a signed agreement. | No request/response schema, scope, header or webhook format is assumed anywhere in code. |
| Wallet | None. | Nahlah AI exposes no balance, stored value or withdrawal concept. |

## Readiness closed in this repository (dormant, route-free)

All of the following live under `backend/payments/` and are created only by
migration `0116` (which requires `0115` to be present and is never part of
normal bootstrap). No route, worker, WhatsApp, AI, cart, catalog or billing
path imports them.

- **Merchant model and activation state** — `onboarding.py` applies only the
  closed transition table (`not_started → pending → approved/rejected`,
  `approved → suspended`, `suspended → approved/rejected`, `rejected →
  pending`); approval requires a provider merchant reference and an evidence
  reference; every transition is appended to
  `merchant_payment_onboarding_events`. `activation.py` keeps a separate
  per-tenant, per-environment switch (`merchant_payment_activations`): a
  provider approval never turns payments on, `payment_acceptance_enabled` is
  false unless the profile is approved, a fee policy is effective, both secret
  *references* are registered and an owner evidence reference enabled it.
  `secret_refs.py` refuses anything that looks like a raw key.
- **Dormant provider boundaries** — `provider.py` separates
  `PlatformOnboardingProvider`, `PaymentProvider` / `SettlementLinesProvider`
  and `PayoutProvider`. Every `Dormant*` implementation raises
  `ProviderContractPending`; `MOYASAR_CAPABILITIES_2026_10_03` records the
  table above in code so tests can assert that marketplace split and wallet
  are unsupported and that no surface is contracted.
- **Generic webhook delivery ledger** — `webhook_ledger.py` stores every
  inbound delivery before anything else (`merchant_payment_webhook_deliveries`),
  collapses identical redeliveries by digest, redacts card, secret, token and
  bank identifiers, records authentication state through a pluggable
  authenticator (shared secret or HMAC-SHA256; the secret is never stored),
  admits a delivery to exactly one tenant only when authenticated, refuses
  re-attribution of a provider event reference to another tenant, and records a
  terminal outcome with a reason. Onboarding events for not-yet-approved
  merchants can be recorded here, which PR #1191's `admit_provider_event`
  (approved profiles only) cannot do; the two compose, they do not overlap.
- **Settlement evidence and reconciliation** — `settlements.py` records
  provider-reported settlements idempotently (amount, currency and recipient
  may never change; a status change is a newer observation) and settlement
  *lines*, linking a `payment` line to an observed payment only when that
  payment belongs to the same tenant. `reconciliation.py` labels every figure
  by its evidence: `settled_gross` counts only payments named by a provider
  settlement line; everything else is `awaiting_settlement_gross`,
  `provider_reported_settlement_total`, `unmatched_line_total` or
  `provisional_*`. `evidence_state` becomes `inconsistent` when the figures
  disagree, so no dashboard can present a number as final by accident. There is
  no balance field.

Tests: `tests/test_merchant_payments_readiness.py` (two unrelated generic
merchants on one provider environment; tenant isolation, replay/dedup,
database-enforced evidence, migration on a disposable SQLite database) and the
existing `tests/test_merchant_payments_foundation.py`.

## Still pending on Moyasar or on a business decision

Do not implement these from guesses:

1. **Agreement, setup fee and signature** — prerequisite for documentation and
   sandbox. Until then no network client exists in `backend/payments/`.
2. **Platform API schema** — registration request fields, KYB document
   handoff, status vocabulary, authoritative merchant identifier, key issuance
   flow, scopes/permissions of platform credentials.
3. **Webhook contract** — endpoint model (platform-level or per merchant),
   authentication mechanism and header/body format, event names, retry and
   ordering guarantees. The ledger's authenticator is pluggable for this reason.
4. **Platform fee terms** — percentage/fixed, VAT treatment, whether Moyasar
   collects and settles Nahlah AI's fee separately or Nahlah AI invoices the
   merchant. Allocation quotes remain provisional until then.
5. **Settlement terms** — cadence, recipient mapping per merchant account,
   availability of settlement and settlement-line reads to the platform
   credential, refund/chargeback allocation between merchant and fee.
6. **Payout** — whether Nahlah AI contracts the separate Payout service at all.
   The boundary exists; it has no implementation and no use case yet.
7. **Order linkage** — a tenant-safe foreign key from an observed payment to
   an internal order (`orders` is not declared unique as `(tenant_id, id)`).
8. **Production activation** — explicit review and application of `0115` then
   `0116` in the target database, credential storage in the existing secret
   manager, operator runbook and rollback. None of this is part of any PR.

## Operating rules while dormant

- Migrations `0115` and `0116` are applied explicitly, never by bootstrap or
  `alembic upgrade head`, and never in production under this readiness work.
- No production data, keys, identities or bank accounts are used in tests.
- Human-facing name: **Nahlah AI** in English, **نحلة** in Arabic.
