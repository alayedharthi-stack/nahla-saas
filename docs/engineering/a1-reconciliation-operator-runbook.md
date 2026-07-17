# A1 reconciliation operator runbook (G4 gate)

Tenant-scoped operator tooling for the **A1-Validate** rollout gate (G4):

1. **Read-only report** — proves coverage state without mutations.
2. **Write reconciliation** — bounded, audited backfill via `reconcile_*` service APIs (post-`0087` Expand only).

## Purpose

After **A1-Expand** (`0087`) is deployed, operators must review a deterministic reconciliation report per tenant before opening the separate **A1-Validate** PR (`0088`). The report:

- Enumerates external-profile and internal-customer subjects for **one tenant**
- Computes tuple-scoped linked / unmapped / mislinked counts from orders (read-only)
- Reads persisted coverage rows and watermark presence
- Surfaces platform capability state (`expand` vs `validated`)
- Emits `ready_for_validate` only when all evidence gates pass

The report **does not** approve migration `0088`, toggle capability state, or enable reconciliation consumers.

---

## Read-only report command

```bash
export DATABASE_URL='postgresql://…'   # established app configuration only

python backend/scripts/report_order_customer_identity_reconciliation.py \
  --tenant-id <TENANT_ID> \
  --pretty
```

Optional safety cap (default and maximum `1000` per subject kind):

```bash
python backend/scripts/report_order_customer_identity_reconciliation.py \
  --tenant-id <TENANT_ID> \
  --max-subjects-per-kind 1000
```

**Requirements**

- `--tenant-id` is mandatory (positive integer). There is no all-tenant mode.
- `DATABASE_URL` must be set; the tool does not accept raw connection strings on the CLI.
- Report mode is always read-only / dry-run.

Exit codes: `0` = report generated, `1` = configuration error, `2` = `access_status` not `ok` (tenant missing, capability unreadable, enumeration truncated, or degraded access).

## Output schema (`a1_reconciliation_report_v1`)

Machine-readable JSON. Closed top-level fields:

| Field | Meaning |
|-------|---------|
| `report_schema_version` | Always `a1_reconciliation_report_v1` |
| `tenant_id` | Scoped tenant |
| `dry_run` / `read_only` | Always `true` |
| `tenant_present` | Tenant row exists |
| `policy_eligibility_ready` | Always `false` (report never claims policy eligibility) |
| `coverage_scope_claims` | External / internal tuple scope labels |
| `capability` | `state`, `state_readable`, `reconciliation_consumer_ready` |
| `external_profiles` | Aggregate external subject rollup |
| `internal_customers` | Aggregate internal subject rollup |
| `aggregate` | Combined totals |
| `evidence_gates` | Boolean gate map |
| `ready_for_validate` | `true` only when every gate passes and no blockers remain |
| `readiness_blockers` | Closed blocker tokens (e.g. `watermark_missing`) |
| `access_status` | `ok` \| `tenant_missing` \| `capability_unreadable` \| `enumeration_truncated` \| `degraded` |
| `report_generated_at_utc` | ISO timestamp (audit only) |

**Privacy:** Output must not contain phone, email, name, order ID, customer ID, external reference, profile UUID, raw SQL, DB URLs, or exception text. Aggregates only.

`external_profiles.orphan_tuple_orders_total` is an aggregate-only gate for
external-provider orders that contain a complete external tuple but no linked
`ExternalCustomerProfile`. It deliberately exposes no tuple values, IDs, or refs.

## Interpreting `ready_for_validate`

`ready_for_validate: true` means the tenant’s enumerated subjects satisfy **all** evidence gates:

1. Tenant exists
2. Capability state readable and equals `expand` (pre-validate)
3. No subject enumeration truncation
4. At least one subject enumerated
5. Every enumerated subject has clean tuple linkage (no unmapped/mislinked orders in scope)
6. Every enumerated subject has a persisted coverage row and watermark
7. Zero unmapped / mislinked orders in aggregate scope
8. Zero external orphan tuple orders
9. At least one linked order in aggregate scope (a zero-order tenant cannot pass
   vacuously)

`ready_for_validate: false` is expected immediately after Expand merge — runtime completeness/health remain capability-capped to incomplete/degraded until `0088` sets `validated`.

**Idempotent re-runs:** Re-run the same command after backfill/reconcile writes (separate maintenance job) until gates pass. Identical database state yields identical aggregates (timestamp excluded).

## Operational limits and cost

- `--max-subjects-per-kind` accepts `1..1000`; default and maximum is `1000`.
- Each subject reads at most `1001` matching orders. The extra row detects an
  order-history limit breach; it does not become part of reported totals.
- Internal subject ID reads are tenant-filtered and bounded to `1001` rows from
  each source (orders and coverage), rather than materializing an unbounded
  tenant ID set.
- External orphan tuples use one tenant-filtered aggregate count query.
- Any subject or order limit breach emits a truncation blocker and forces
  `ready_for_validate: false`; it never hides incomplete evidence.
- Operators should run one tenant at a time in a maintenance window and use the
  default cap unless a smaller diagnostic cap is required. A smaller cap is
  expected to fail closed if the tenant exceeds it.

## Non-goals (read-only report)

- Does **not** run `reconcile_*` write helpers or backfill coverage
- Does **not** apply migration `0088` or change `order_customer_identity_capability_state`
- Does **not** enable flags, dashboards, or reconciliation consumers
- Does **not** scan all tenants
- Does **not** produce customer-facing prose

## Read-only vs write reconciliation

| Path | Mutates coverage rows? | Operator CLI |
|------|------------------------|--------------|
| `reconcile_external_profile_coverage` / `reconcile_internal_customer_coverage` | **Yes** (counts + watermark + health) | `reconcile_order_customer_identity_coverage.py --write` |
| `build_safe_*_proof` read contracts | No | — |
| `build_order_customer_identity_reconciliation_report` | No | `report_order_customer_identity_reconciliation.py` |

The read-only report never calls write helpers. Use the write operator (below) in an approved maintenance window, then re-run the report until `ready_for_validate: true`.

Tuple classification cannot drift: the report, write operator, and both `reconcile_*`
paths use the same pure `order_customer_identity_reconciliation_classification`
helper.

---

## Write reconciliation command (post-0087 Expand)

Official bounded operator for tenant-scoped coverage backfill. **Dry-run is the default**; persistence requires explicit confirmation and staging gates.

### G4 / 0088 preconditions (write operator)

Before any `--write` run:

1. **0087 Expand applied exactly** — Alembic revision must be exactly `0087`. This pre-0088 validation maintenance path rejects `0088`, `0089`, and unknown revisions (`revision_not_exactly_0087`); a missing revision row is rejected as `alembic_version_missing`.
2. **Capability `expand` only** — `order_customer_identity_capability_state.state = expand` and `validation_revision IS NULL`. Rejects `validated`, missing/unknown capability, or `validation_revision` already set (pre-0088 label).
3. **Staging identity** — `RAILWAY_PROJECT_NAME=desirable-growth`, `RAILWAY_ENVIRONMENT_NAME=staging`. Production markers rejected.
4. **Database allowlist** — `DATABASE_URL` host must be `postgres-staging.railway.internal` (no production hosts).
5. **Confirmation token** — `NAHLA_A1_RECONCILE_WRITE_CONFIRM=RUN_A1_RECONCILE_WRITE` (exact match).
6. **Tenant scope** — explicit `--tenant-id` (positive integer). No all-tenant mode.
7. **Tuple linkage clean** — write backfill updates coverage counts/watermarks; it does **not** repair mislinked/unmapped orders. Run the read-only report first; resolve linkage blockers separately.
8. **After writes** — re-run the read-only report; proceed to deferred `0088` only when `ready_for_validate: true` and separate rollout approval is granted.

Dry-run preview (`default`) skips staging identity and confirmation but still enforces capability/revision/tenant/batch gates against the connected database.

### Commands

Dry-run preview (no mutations):

```bash
export DATABASE_URL='postgresql://…'

python backend/scripts/reconcile_order_customer_identity_coverage.py \
  --tenant-id <TENANT_ID> \
  --pretty
```

Persist coverage reconciliation (staging maintenance window):

```bash
export DATABASE_URL='postgresql://…@postgres-staging.railway.internal/…'
export RAILWAY_PROJECT_NAME='desirable-growth'
export RAILWAY_ENVIRONMENT_NAME='staging'
export NAHLA_A1_RECONCILE_WRITE_CONFIRM='RUN_A1_RECONCILE_WRITE'

python backend/scripts/reconcile_order_customer_identity_coverage.py \
  --tenant-id <TENANT_ID> \
  --write \
  --pretty
```

Optional batch cap (default and maximum `1000` per subject kind):

```bash
python backend/scripts/reconcile_order_customer_identity_coverage.py \
  --tenant-id <TENANT_ID> \
  --max-subjects-per-kind 500
```

**Exit codes:** `0` = dry-run or write `outcome=success`; `1` = configuration / gate rejection; `2` = `access_status` not `ok` or `outcome=partial|failed`.

### Output schema (`a1_reconciliation_write_v1`)

Machine-readable JSON. Closed top-level fields:

| Field | Meaning |
|-------|---------|
| `write_schema_version` | Always `a1_reconciliation_write_v1` |
| `tenant_id` | Scoped tenant |
| `dry_run` / `read_only` | `true` unless `--write` |
| `outcome` | `success` \| `partial` \| `failed` \| `aborted` |
| `access_status` | `ok` \| `gate_rejected` \| `tenant_missing` \| `enumeration_truncated` \| `degraded` |
| `gate_stage` / `gate_error_class` | Set when preflight gates reject (writes only for env gates) |
| `tenant_present` | Tenant row exists |
| `capability` | `state`, `state_readable`, `validation_revision`, `alembic_revision`, `alembic_revision_is_0087` |
| `batch` | `max_subjects_per_kind`, selected counts, `enumeration_truncated` |
| `execution` | `subjects_attempted/succeeded/failed`, `coverage_rows_created/updated`, `committed` |
| `aggregate` | `linked/unmapped/mislinked_orders_in_scope_total` |
| `failure_categories` | `subject_exception`, `cross_tenant_rejected` (aggregate only) |
| `write_generated_at_utc` | ISO timestamp (audit only) |

**Privacy:** Same as read-only report — no phone, email, name, order ID, customer ID, external reference, profile UUID, raw SQL, DB URLs, or exception text.

### Write behavior and failure semantics

- **Deterministic ordering** — external profiles by `(created_at, id)`; internal customers by sorted `customer_id` union (same bounds as report).
- **Batch bounding** — `enumeration_truncated: true` fails closed with no writes.
- **Per-subject failures** — independent subjects; operator continues and commits successful subjects. `outcome=partial` when any subject fails; `failure_categories` report counts honestly (no hidden partial success).
- **Commit failure** — full rollback; `committed=false`, `outcome=failed`.
- **Idempotent re-runs** — identical DB state yields identical aggregates (timestamp excluded); second write reports `coverage_rows_created=0`.
- **Cross-tenant guard** — subjects whose `tenant_id` ≠ `--tenant-id` are skipped (`cross_tenant_rejected`).

### Write operator non-goals

- Does **not** apply migration `0088` or change capability state
- Does **not** enable flags, dashboards, or reconciliation consumers
- Does **not** scan all tenants
- Does **not** call providers or mutate orders/links (coverage rows only)

---

## Separate approval steps for `0088`

1. A1-Expand merged; `0087` applied in target environment (staging pinned at `0087`)
2. Per-tenant **write reconciliation** (if coverage/watermarks missing) via `reconcile_order_customer_identity_coverage.py`
3. Per-tenant **read-only report** reviewed; `ready_for_validate: true` where required (tenant 1 on experimental staging)
4. **Fixture evidence retained** until validation outcome — see `docs/engineering/a1-evidence-fixture-operator-runbook.md`
5. Separate rollout approval granted (see `docs/engineering/a1-order-identity-migration-rollout.md`)
6. Maintenance window: operator `scripts/operators/staging_migration_0087_to_0088.py run --tenant-id <ID>` (never `head`)
7. Verify constraints validated, indexes present, capability `validated`
8. **Fixture cleanup** after success or no-go decision
9. Only then enable reconciliation consumers / healthy runtime signals

Tracked operator + runbook: `docs/engineering/staging-migration-0087-to-0088-runbook.md`.

Deferred source archive: `.a1-validate-deferred/` (promoted into this PR as revision `0088`).

## Generic merchant scenario

Use neutral tenants (e.g. `متجر تجريبي عام`) across categories — food, apparel, cosmetics — not a single production honey store. Rotate product/order fixtures in tests; assert behavior and aggregates, not Arabic phrasing.
