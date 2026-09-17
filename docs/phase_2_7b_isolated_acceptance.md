# Phase 2.7B — isolated acceptance environment (proposal, not created)

Phase 2.7B exercises merchant knowledge, and merchant knowledge is the
merchant's own confidential text. No acceptance fixture is written into the
database the public API serves. This document is the exact configuration
proposed for owner approval; **nothing here has been created.**

## Why a separate database

The knowledge cases need sections that are deliberately wrong (a stale price, a
stale availability claim), deliberately deleted, and deliberately owned by
another tenant. Creating any of those next to real merchant knowledge is
unacceptable, and even reading real sections into a report risks exposing
confidential text. A temporary database removes the question entirely.

## Guard already implemented

`services/commerce_v2_phase_2_7b_environment.assert_isolated_acceptance_database`
runs before any write and fails closed unless **both** hold:

1. `NAHLA_P27B_ISOLATED_ACCEPTANCE` is `true` on the job, and
2. the target database contains no tenant and no customer, conversation,
   message, order, product or knowledge row outside the Phase 2.7B synthetic
   tenants.

Pointed at production, condition 2 fails on the first real tenant and the job
writes nothing (`phase_2_7b_env_database_contains_foreign_tenants`).

## Proposed Railway resources

| resource | proposal |
|---|---|
| database | new Postgres service `nahla-p27b-acceptance-db`, temporary, deleted after acceptance |
| job service | new service `nahla-p27b-acceptance`, restart policy **NEVER**, **no public domain**, no volume |
| source | the exact merged application commit, pinned via an `ops/p27b-pin-<sha>` branch |
| migrations | `alembic upgrade` applied **only** to the temporary database |
| lifecycle | `provision` → `create` → `run` → `status` → (review) → `cleanup`, then delete both services |

### Variables on the job

| variable | value |
|---|---|
| `DATABASE_URL` | `${{nahla-p27b-acceptance-db.DATABASE_URL}}` (reference only — never a literal) |
| `NAHLA_P27B_ISOLATED_ACCEPTANCE` | `true` |
| `NAHLA_COMMERCE_V2_INTERNAL_E2E_ENABLED` | `true` (this job only; never the public API) |
| `COMMERCE_AGENT_V2_ENABLED` / `_SHADOW_ONLY` / `_TENANT_IDS` | production-equivalent values for the synthetic tenant |
| `OPENAI_API_KEY`, `OPENAI_MODEL`, `NAHLA_MODEL_*`, `COMMERCE_AGENT_V2_SERVICE_TIER`, `_TIMEOUT_SECONDS`, `_RUN_DEADLINE_SECONDS`, `_MAX_MODEL_RETRIES` | Railway variable references to the same values production uses, so the model and provider behaviour match |
| `NAHLA_DISABLE_SCHEDULERS` | `true` |

### Variables deliberately absent

`WHATSAPP_TOKEN`, `WHATSAPP_VERIFY_TOKEN`, `META_*`, `D360_*`, `SALLA_*`,
`MOYASAR_*`, `STRIPE_*`, `SMTP_*`, `RESEND_API_KEY`, `NAHLA_CATALOG_MEDIA_R2_*`,
and the production `DATABASE_URL`. Without them the job has **no route** to a
real customer, a real merchant store, a payment provider or production data —
it can reach the model provider and its own temporary database, nothing else.

## Fixture identity contract

The acceptance turns are submitted through `submit_internal_customer_turn`,
whose `_find_fixture` owns the identity contract.  The provisioner therefore
builds its conversations from the same canonical helpers that resolver reads
back — `internal_e2e_customer_identity` and `internal_e2e_metadata` — with a
NULL `customer_id`, and adds the Phase 2.7B keys alongside that block rather
than in place of it.  The aliases come from the matrix: it names no per-case
thread and K11 is a bare follow-up, so all sixteen cases share one
INTERNAL_E2E conversation.

`verify_acceptance_fixtures` runs before the provisioning commit, again on a
repeat provision, and once more in the operator before the first case, so a
world the run could not resolve is never reported as provisioned.

## What the run does

1. `provision` — synthetic tenant, a neighbouring tenant (for the cross-tenant
   case), 4 products (including a discounted one and an out-of-stock one),
   10 knowledge sections covering every fixture the matrix names, one
   canonical INTERNAL_E2E conversation per matrix alias, 1 order with a
   shipment.
2. `create --commit <sha>` — writes the run record **before** the first case:
   contract version, matrix hash, commit, case order, `status: queued`.
3. `run --run-id <id>` — executes K01→K16 in order through the internal
   channel, scores each case, then stores the report and its machine digest.
4. `status` / `review` — readable and reviewable after INTERNAL_E2E is disabled;
   verdicts may only change review fields, and the digest is re-checked.
5. `cleanup` — deletes the synthetic world and reports the row counts removed.

## What still needs owner approval

Creating the two Railway services, applying migrations to the temporary
database, and running the job. None of it has been done.
