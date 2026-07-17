# Staging migration 0087 → 0088 runbook (A1-Validate)

Guarded operator for the **deferred validation** slice only. Targets exactly
`0087 → 0088`; never `head` or `0089`.

## Prerequisites

1. **Staging pinned at `0087`** — `alembic_version.version_num = '0087'`.
   Rejects `0088`, `0089`, and unknown revisions.
2. **Capability `expand` only** — `order_customer_identity_capability_state.state = expand`
   and `validation_revision IS NULL`.
3. **G4 evidence** — per-tenant read-only reconciliation report with
   `ready_for_validate: true` (tenant 1 on experimental staging as of Jul 2026).
4. **Fixture evidence** — if generic-commerce fixture rows (`A1G4FX-*`) were seeded
   for G4, keep them until validation outcome is recorded; remove only after
   validation succeeds or an explicit no-go decision.
5. **No `0089` objects** — `conversation_a1_subject_bindings` must not exist
   (sibling branch; apply `0089` in a separate later slice).
6. **Constraint preflight clean** — operator aggregate violation counts all zero.
7. **Separate rollout approval** granted (this PR merge ≠ production execute).
8. **DR canonical parity (hard gate)** — pre-maintenance backup/restore drill must
   match `staging_pin_0087` on the live source (exact revision `0087`, `101` public
   tables, fingerprint
   `2d3c6f4ffdd011517352efa5f1b1d881c30b66bf189e478197a6fad0777890db`). See
   `docs/engineering/staging-dr-canonical-parity-runbook.md`. Provenance: fresh
   read-only `postgres-staging` attestation collected Jul 2026 before the guarded
   `0087 → 0088` maintenance window (PR #612 merged; `0088` not yet executed).

## Operator commands

Read-only preflight (staging identity + DB allowlist; optional G4 when `--tenant-id` set):

```bash
export DATABASE_URL='postgresql://…@postgres-staging.railway.internal/…'
export RAILWAY_PROJECT_NAME='desirable-growth'
export RAILWAY_ENVIRONMENT_NAME='staging'

python scripts/operators/staging_migration_0087_to_0088.py preflight --tenant-id 1
```

Controlled execution (maintenance window):

```bash
export DATABASE_URL='postgresql://…@postgres-staging.railway.internal/…'
export RAILWAY_PROJECT_NAME='desirable-growth'
export RAILWAY_ENVIRONMENT_NAME='staging'
export NAHLA_SKIP_DB_BOOTSTRAP=1
export NAHLA_STAGING_MIGRATION_0087_TO_0088_CONFIRM='RUN_STAGING_0087_TO_0088'

python scripts/operators/staging_migration_0087_to_0088.py run \
  --tenant-id 1 \
  --timeout-sec 1800
```

Equivalent Alembic (only after operator preflight passes):

```bash
cd database && alembic upgrade 0088
```

**Never** run `alembic upgrade head` on staging for this slice — that may select
the `0089` sibling head.

## Preflight output

Manifest schema: `staging_migration_0087_to_0088_v1`.

Privacy-safe fields only:

- `constraint_violation_counts` — per-constraint aggregate counts + `violation_rows_total`
- `g4_evidence` — `ready_for_validate`, `access_status`, blocker count, linked-order aggregate
- No phone, email, name, order ID, customer ID, external ref, profile UUID, SQL, or URLs

## Post-validation contract

After successful `run`:

| Check | Expected |
|-------|----------|
| `alembic_version` | `0088` |
| Orders FK/CHECK (`0087` set) | `pg_constraint.convalidated = true` |
| Deferred indexes | `ix_orders_tenant_customer_id`, `ix_orders_tenant_external_tuple`, `ix_orders_tenant_order_source_kind` present and valid |
| Capability | `state = validated`, `validation_revision = '0088'` |
| `0089` table | absent |

## Failure policy (restore-first)

On **any** migration or post-validation failure:

1. **Stop** — do not retry in place.
2. **Restore** staging from the latest verified backup (DR restore path).
3. Re-run DR canonical parity against `staging_pin_0087` and G4 read-only report
   and operator preflight from restored `0087` state.
4. Root-cause constraint violations before a second attempt.

Downgrade `0088 → 0087` is for ephemeral test databases only, not staging recovery.

## Fixture cleanup (post-outcome)

After validation **success** or explicit **no-go**:

1. Re-run read-only G4 report to confirm final evidence state.
2. Remove `A1G4FX-{tenant_id}-*` fixture rows per
   `docs/engineering/a1-evidence-fixture-operator-runbook.md`.
3. Archive operator manifest JSON for audit.

## Non-goals

- Does **not** apply `0089` or alter the `0089` migration path
- Does **not** enable AI runtime, coupons, or reconciliation consumers
- Does **not** mutate orders/links (validation only)
- Does **not** call providers or change staging Railway service config
