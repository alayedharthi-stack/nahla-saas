# Staging migration 0088 + 0089 attachment runbook (coupon shadow observation)

Guarded operator for attaching the **sibling** `0089` conversation A1-subject
bindings migration onto staging already at **validated `0088`**. Targets exactly
`alembic upgrade 0089`; never `head`, never `0087`/`expand`, and never re-runs
or reverts `0088` validation state.

## Topology

```
0087 (A1-Expand, common ancestor)
 ├── 0088 (A1-Validate — capability validated)
 └── 0089 (conversation_a1_subject_bindings)
```

After attachment, `alembic_version` holds **two rows**: `0088` and `0089`.

Normal application bootstrap pins to `0089` only (via
`scripts/operators/bootstrap_migration_contract.py`) so capability remains under
operator control. **Never** run `alembic upgrade head` on staging — that would
apply both sibling heads from `0087` in one step.

## Prerequisites

1. **Staging pinned at validated `0088`** — single `alembic_version` row `0088`.
   Rejects `0087`, `0089`-only, and unknown revisions.
2. **Capability validated** — `order_customer_identity_capability_state.state = validated`
   and `validation_revision = '0088'`.
3. **All `0088` invariants** — orders FK/CHECK validated, deferred indexes valid.
4. **No pre-attach `0089` objects** — `conversation_a1_subject_bindings` absent.
5. **DR restore profile `staging_pin_0088`** — must exist in
   `staging_dr_canonical_parity_contract.py` (contract bump after live 0088
   attestation). Operator preflight/run **fails closed** until this profile is
   added with evidence-backed fingerprint.
6. **Separate rollout approval** granted (PR merge ≠ production execute).

## Operator commands

Read-only preflight (staging identity + DB allowlist; DR profile gate):

```bash
export DATABASE_URL='postgresql://…@postgres-staging.railway.internal/…'
export RAILWAY_PROJECT_NAME='desirable-growth'
export RAILWAY_ENVIRONMENT_NAME='staging'

python scripts/operators/staging_migration_0088_to_0089.py preflight
```

Controlled execution (maintenance window):

```bash
export DATABASE_URL='postgresql://…@postgres-staging.railway.internal/…'
export RAILWAY_PROJECT_NAME='desirable-growth'
export RAILWAY_ENVIRONMENT_NAME='staging'
export NAHLA_SKIP_DB_BOOTSTRAP=1
export NAHLA_STAGING_MIGRATION_0088_ATTACH_0089_CONFIRM='RUN_STAGING_0088_ATTACH_0089'

python scripts/operators/staging_migration_0088_to_0089.py run --timeout-sec 1800
```

Equivalent Alembic (only after operator preflight passes):

```bash
cd database && alembic upgrade 0089
```

**Never** run `alembic upgrade head`.

## Preflight output

Manifest schema: `staging_migration_0088_attach_0089_v1`.

Privacy-safe fields only:

- `alembic_revisions_observed` — revision set before attach
- `expected_post_success_revisions` — `["0088", "0089"]`
- `dr_restore_profile_revision` — `0088`
- Schema fingerprint aggregates (no row identifiers)

## Post-validation contract

After successful `run`:

| Check | Expected |
|-------|----------|
| `alembic_version` rows | `0088` **and** `0089` |
| Orders FK/CHECK (`0087` set) | still `pg_constraint.convalidated = true` |
| Deferred indexes | still present and valid |
| Capability | still `validated`, `validation_revision = '0088'` |
| `conversation_a1_subject_bindings` | present with tenant FK, partial unique, checks |

## Failure policy (restore-first)

On **any** migration or post-validation failure:

1. **Stop** — do not retry in place.
2. **Restore** staging from the latest verified backup with DR profile
   `staging_pin_0088`.
3. Re-run operator preflight from restored validated `0088` state.
4. Root-cause before a second attempt.

Downgrade `0089` or `0088` in place is for ephemeral test databases only, not
staging recovery.

## Non-goals

- Does **not** enable AI runtime, coupon shadow activation, or reconciliation consumers
- Does **not** mutate orders/links or capability state beyond coexistence checks
- Does **not** deploy Railway services or change provider configuration
- Does **not** add `staging_pin_0088` DR profile (separate attestation PR)

## Future operator plan (coupon shadow observation)

1. Merge this PR (operator + tests + runbook only).
2. Execute guarded `0087 → 0088` operator on experimental staging (separate approval).
3. Attest `staging_pin_0088` DR profile + backup/restore drill.
4. Execute this `0088 + 0089` attachment operator after DR prerequisite is green.
5. Run coupon shadow **read-only** observation probes (no runtime flags).
6. Evaluate shadow readiness before any activation slice.
