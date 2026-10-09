# Primary catalog transfer

## Scope and source

This local patch targets deployed baseline `4b5793a27bbf591befeb3e00341d2b547bbdd4bb`.
It carries catalog runtime, dashboard, retirement schema and regression work from
PR #1193 (`4e06409dea520a91b0c41db8dfed8fd897d2a19c`) and catalog-only consent
additions from PR #1197 (merge `b7d2d6811911a80a597992e0de0c6a833c2c9e12`).
Shared files were patched against the baseline; unrelated intervening main-branch
changes were not copied. The review environment validator is the dependency from
`3f4c24a0384bdb8a491ceecd0b9aa3b8f0215d63`.

The transfer changes catalog operations only. AI agent/model/prompt/persona code
and plan entitlements are unchanged. Existing Starter catalog eligibility stays
under the existing strict entitlement lookup; consent never grants a feature.

## Primary mode remains opt-in

All consent functionality remains off by default. Primary mode requires both:

- `NAHLA_META_CATALOG_CONSENT_ENABLED=1`
- `NAHLA_META_CATALOG_CONSENT_PRIMARY_ENABLED=1`

`NAHLA_CATALOG_REVIEW_ENV` must be off. Both modes together fail closed.
Primary accepts only these exact configured URLs:

- `META_CATALOG_CONSENT_REDIRECT_URI=https://api.nahlah.ai/merchant/catalog/meta-consent/callback`
- `DASHBOARD_URL=https://app.nahlah.ai`

An operator must separately verify the existing app credentials, dedicated
Facebook Login for Business configuration, dedicated encryption key and
`META_CATALOG_CONSENT_APPROVED_ASSETS` (`tenant:catalog:business`). Do not place
credentials in this document or source control. The merchant must complete the
catalog-only consent flow. The flow requests `catalog_management` and
`business_management`, checks app/user identity, asset ownership and grants,
and stores an encrypted catalog-bound authorization. It does not create or
claim a WhatsApp connection.

Review mode still checks the isolated deployment identity, DB binding and
persisted review marker. Runtime consent access now performs the same full
availability validation because this baseline has no verified review boot hook.
Primary validation performs no review-marker DB query.

## Publishing and lifecycle

The existing bulk route `POST /merchant/catalog/whatsapp-sync` accepts the
existing eligible Salla/native catalog through the shared synchronization
engine. Individual native confirmation remains native-only. An explicit bulk
request can schedule work while the automatic switch is off. One background
run walks batches, attempting each product and deletion-ledger entry at most
once; failures and held leases do not busy-loop. Subsequent runs retain the
existing retry/backoff behavior.

Ordinary automatic Salla synchronization still requires
`NAHLA_WHATSAPP_CATALOG_AUTO_SYNC=1`. Existing tenant/product scope settings are
preserved and must be reviewed separately before enabling all intended products.
Consent-only tenants are included in automatic discovery even without a
WhatsApp row. Readiness, push, reconciliation and retirement use the same
verified consent resolver. Expired, revoked, disabled, unreadable or changed
consent fails closed without falling back to a WhatsApp or platform token.

Deletion still requires exact platform-publication evidence. A source/native
hard delete locks and refreshes its product rows and defers while a publisher
is syncing or publication outcome is unresolved. Salla uses the existing
bounded durable webhook retry/dead-letter path; native deletion returns a
retryable 409. Unresolved data and the source event remain available for
operator replay rather than being discarded. No new queue or retry loop exists. This guard covers the scheduled/UI
orchestrator path; direct operator push CLIs that bypass its syncing lease are
not serialized by this bounded fix and are not covered by that race proof.

No-WA reconciliation cadence is stored in the existing tenant settings metadata,
including tenants with zero products and a remaining deletion ledger. Consent
audit timestamps are not repurposed. Catalog publication never counts as proof
that a WhatsApp catalog is linked or visible.

## Schema and execution limits

Source identities were verified statically:

- `0118_catalog_channel_retirements`: parent `0112`
- `0120_meta_catalog_authorizations`: parent `0118`
- Normal bootstrap remains pinned to `0093`.

No live database was accessed, no migration was executed, and no source-control
write or deployment was performed while preparing this patch. Live schema
preflight, explicit migration approval/execution, real PostgreSQL concurrency
proofs, merchant consent and live publication verification remain release gates.
SQLite tests exercise state transitions and mocked provider calls; they cannot
prove PostgreSQL row-lock contention. Passing collection of the PostgreSQL
proof inventory is not a PostgreSQL proof run.

The exact test commands, results, dependency versions, changed-file manifest,
patch SHA-256 and no-AI-change verification are delivered alongside this patch.
