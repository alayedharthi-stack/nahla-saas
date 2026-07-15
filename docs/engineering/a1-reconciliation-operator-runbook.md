# A1 reconciliation operator runbook (G4 gate)

Tenant-scoped, read-only operator tooling for the **A1-Validate** rollout gate (G4). This report proves coverage state for authoritative order-identity subjects **without** mutating database rows or claiming policy eligibility.

## Purpose

After **A1-Expand** (`0087`) is deployed, operators must review a deterministic reconciliation report per tenant before opening the separate **A1-Validate** PR (`0088`). The report:

- Enumerates external-profile and internal-customer subjects for **one tenant**
- Computes tuple-scoped linked / unmapped / mislinked counts from orders (read-only)
- Reads persisted coverage rows and watermark presence
- Surfaces platform capability state (`expand` vs `validated`)
- Emits `ready_for_validate` only when all evidence gates pass

The report **does not** approve migration `0088`, toggle capability state, or enable reconciliation consumers.

## Command

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

## Non-goals (this tooling)

- Does **not** run `reconcile_*` write helpers or backfill coverage
- Does **not** apply migration `0088` or change `order_customer_identity_capability_state`
- Does **not** enable flags, dashboards, or reconciliation consumers
- Does **not** scan all tenants
- Does **not** produce customer-facing prose

## Read-only vs write reconciliation

| Path | Mutates coverage rows? | Used by report? |
|------|------------------------|-----------------|
| `reconcile_external_profile_coverage` / `reconcile_internal_customer_coverage` | **Yes** (counts + watermark + health) | **No** |
| `build_safe_*_proof` read contracts | No | Indirect (same cap rules) |
| `build_order_customer_identity_reconciliation_report` | No | **Yes** (operator CLI) |

Operators may run write reconciliation in a **separate** approved maintenance process before re-running this report. The operator report itself never calls write helpers.

Tuple classification cannot drift: the report and both write reconciliation
paths use the same pure `order_customer_identity_reconciliation_classification`
helper. The report only adds bounded reads and does not persist its result.

## Separate approval steps for `0088`

1. A1-Expand merged; `0087` applied in target environment
2. Per-tenant reconciliation report reviewed; `ready_for_validate: true` where required
3. Separate rollout approval granted (see `docs/engineering/a1-order-identity-migration-rollout.md`)
4. Maintenance window: `cd database && alembic upgrade 0088`
5. Verify constraints validated and indexes present
6. Only then enable reconciliation consumers / healthy runtime signals

Deferred Validate artifacts: `.a1-validate-deferred/` (not part of Expand branch).

## Generic merchant scenario

Use neutral tenants (e.g. `متجر تجريبي عام`) across categories — food, apparel, cosmetics — not a single production honey store. Rotate product/order fixtures in tests; assert behavior and aggregates, not Arabic phrasing.
