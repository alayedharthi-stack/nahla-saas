# Staging legacy migration 0030 → 0087 — operator runbook (A1 Expand)

Controlled, staging-gated operator surface for the legacy recovery chain **0031–0087** when staging advances through three bounded guarded stages. This runbook covers three bounded guarded stages only — not backup storage, deploy, production execution, bootstrap unfreeze, or migrations **0088 / 0089 / head**.

## Repository boundary (read before GO)

| Revision | Status in repository | Status in this operator slice |
|----------|----------------------|-------------------------------|
| **0087** | Merged (A1-Expand) | **Final target** for Stage C |
| **0088** | Deferred (`.a1-validate-deferred/`) | **Out of scope** — separate A1-Validate operator slice |
| **0089** | **Merged on `origin/main`** (PR #596, `0089_conversation_a1_subject_bindings.py`). Alembic repository head may be **0089** while staging runners stop at **0087**. | **Out of scope** — separate later operator slice for staging 0087→0089 |

Runners in this PR invoke exactly `alembic upgrade 0032`, `0083`, or `0087` — never `head`, never `0089`.

## Why this exists

- Staging controlled migration reached **`0030`** via the 0024→0030 slice; guarded Stage A advances staging to **`0032`**. Bootstrap remains frozen (`NAHLA_SKIP_DB_BOOTSTRAP=1`).
- App head expects schema through **0087** (A1-Expand) but staging must advance in **three bounded stages** with backup/restore between each.
- **0031 / 0032** contain customer duplicate data gates — Stage A preflight surfaces aggregate duplicate counts before GO.
- **0064** introduces variant backfill workload — Stage B uses a longer bounded timeout policy.
- **0087** adds A1-Expand objects with **NOT VALID** constraints — Stage C adds catalog audit + extension gates.

## Three bounded stages

| Stage | Runner | Base → Target | Confirmation token |
|-------|--------|---------------|-------------------|
| **A** | `staging_migration_0030_to_0032.py` | `0030` → `0032` | `NAHLA_STAGING_MIGRATION_0030_TO_0032_CONFIRM=RUN_STAGING_0030_TO_0032` |
| **B** | `staging_migration_0032_to_0083.py` | `0032` → `0083` | `NAHLA_STAGING_MIGRATION_0032_TO_0083_CONFIRM=RUN_STAGING_0032_TO_0083` |
| **C** | `staging_migration_0083_to_0087.py` | `0083` → `0087` | `NAHLA_STAGING_MIGRATION_0083_TO_0087_CONFIRM=RUN_STAGING_0083_TO_0087` |

Execute stages **in order**. Do not skip. Take an approved backup **before each stage** and compare preflight fingerprints.

## Prerequisites (all stages)

| Requirement | Value / check |
|-------------|----------------|
| Prior slice complete | Staging `alembic_version` exactly **`0030`** before Stage A; exactly **`0032`** before Stage B; exactly **`0083`** before Stage C |
| Branch merged & deployed to **staging** app container | Runners ship with app image |
| `RAILWAY_PROJECT_NAME` | `desirable-growth` |
| `RAILWAY_ENVIRONMENT_NAME` | `staging` |
| `ENVIRONMENT` | Must **not** be `production`, `prod`, or `live` |
| `NAHLA_SKIP_DB_BOOTSTRAP` | `1` (bootstrap frozen — required for every **`run`**, recommended for entire window) |
| `DATABASE_URL` | Staging app configuration only; parsed host must exactly equal `postgres-staging.railway.internal` |
| Approved backup | Taken **before each stage GO** with manifest using **`schema_fingerprint_version=nahla_public_tables_sha256_v1`** |

## Fingerprint contract

| Field | Specification |
|-------|----------------|
| `schema_fingerprint_version` | `nahla_public_tables_sha256_v1` |
| Algorithm | SHA-256 hex digest of comma-joined **sorted public base table names only** |
| Scope | **Table-set only** — does not encode columns, indexes, or constraints |
| Post-contract validation | Each stage `run` additionally validates required columns, indexes, and (Stage C) NOT VALID constraints via contract modules |

Preflight fingerprint mismatch vs backup manifest is a **hard stop**. Post-success schema metadata from the contract is the authoritative shape check after migration.

## Restore-first failure policy

| Situation | Operator action |
|-----------|-------------------|
| Preflight fingerprint mismatch vs approved backup manifest | **Restore from backup first** — do not GO |
| Timeout / `migration_nonzero_exit` during `run` | **Stop.** Keep bootstrap frozen. Do not retry with `head` or multi-revision downgrade. |
| `post_validation_failed` | **Stop.** Treat as failed migration. **Restore from pre-stage backup.** |
| `duplicate_preflight_failed` (Stage A or C) | **Stop.** Deduplicate offline; re-run stage `preflight`. |
| `catalog_audit_rejected` (Stage C) | **Stop.** A1 objects already present or wrong revision — investigate partial deploy. |
| Casual `alembic downgrade` across 0031–0087 | **Forbidden** — restore-first only. |

After restore: remain bootstrap-frozen, re-run stage `preflight`, compare manifests, only then consider another GO.

## Stage A — 0030 → 0032

### Hazards

- **0031**: duplicate `(tenant_id, phone)`, `(tenant_id, metadata salla_id)`, or **0031 backfill key** `COALESCE(metadata->>'salla_id', metadata->>'external_id')` blocks upgrade.
- **0032**: normalization collisions on `(tenant_id, normalized_phone)` block upgrade.

### Preflight duplicate counts (aggregate-only, no PII) — **hard gates**

| Key | Gate | Meaning |
|-----|------|---------|
| `duplicate_tenant_phone_groups` | **Hard stop** | Must be **0** |
| `duplicate_tenant_salla_metadata_groups` | **Hard stop** | Must be **0** |
| `duplicate_tenant_salla_backfill_groups` | **Hard stop** | 0031 `COALESCE(salla_id, external_id)` collision groups — must be **0** |
| `duplicate_tenant_normalized_phone_groups` | **Hard stop** | Must be **0** |

### Commands

```bash
export NAHLA_SKIP_DB_BOOTSTRAP=1

python scripts/operators/staging_migration_0030_to_0032.py preflight

export NAHLA_STAGING_MIGRATION_0030_TO_0032_CONFIRM=RUN_STAGING_0030_TO_0032
python scripts/operators/staging_migration_0030_to_0032.py run --timeout-sec 1800
```

Invokes exactly: `python -m alembic upgrade 0032` with `cwd=database/`.

### Stage A completion attestation (provenance)

After guarded Stage A `run` and post-validation pass, record live source evidence
before any Stage B backup/restore drill:

| Field | Attested value |
|-------|----------------|
| `alembic_revision` | `0032` |
| `public_table_count` | `96` |
| `schema_fingerprint_version` | `nahla_public_tables_sha256_v1` |
| `schema_fingerprint_sha256` | `1b9aca690e4eba0a0ffa1df8d59ecdd316d1a7f150e65bd2635d7fca4f5f54ae` |

Stage B DR canonical parity requires the versioned contract profile
`staging_pin_0032` (exact revision, table count, and fingerprint pin). See
`docs/engineering/staging-dr-canonical-parity-runbook.md`. Parity verification
fails closed when the source does not match a contracted profile.

## Stage B — 0032 → 0083

### Hazards

- Long chain including **0064** `product_variants` backfill (every product gets ≥1 variant row).
- Drift from `create_all` / forward-ORM partial schema.

### Preflight workload indicators — informational vs hard gates

| Key | Gate type | Meaning |
|-----|-----------|---------|
| `total_products_count` | **Informational** | Catalog size — informs timeout sizing |
| `products_with_metadata_variants_array` | **Informational** | 0064 multi-variant backfill workload estimate |
| `products_without_variant_rows` | **Informational** | Rows 0064 will INSERT on upgrade |
| `cross_merchant_signals_table_preexisting` | **Informational** | `1` if 0033 drift collision shape detected |
| `learned_sales_policies_table_preexisting` | **Informational** | `1` if 0034 drift collision shape detected |
| `product_variants_table_preexisting` | **Informational** | `1` if drift collision shape detected |
| `product_groups_table_preexisting` | **Informational** | `1` if catalog-intelligence table drift |
| `product_group_items_table_preexisting` | **Informational** | `1` if 0083 catalog join-table drift |
| `product_relations_table_preexisting` | **Informational** | `1` if catalog-intelligence table drift |
| `product_rankings_table_preexisting` | **Informational** | `1` if catalog-intelligence table drift |
| `products_has_variants_column_preexisting` | **Informational** | `1` if forward-ORM column drift |
| `products_default_variant_id_column_preexisting` | **Informational** | `1` if forward-ORM column drift |
| `stage_b_catalog_missing_index_count` | **Informational** | Count of contract-required indexes absent pre-GO |
| `stage_b_catalog_missing_unique_constraint_count` | **Informational** | Count of contract-required unique constraints absent pre-GO |

Stage B has **no duplicate hard-stop preflight** in this slice. Hard gates are identity, binding, bootstrap freeze, confirmation, exact start revision `0032`, post-success contract at `0083`, and DR canonical parity eligibility via `staging_pin_0032` on the pre-stage backup/restore drill.

### Stage B retry prerequisites (F16 drift guards on 0033–0049)

| Prerequisite | Check |
|--------------|-------|
| Migrations **0033–0049** merged with F16 inspector guards | Required for safe retry when `create_all` pre-created equivalent objects |
| Preflight `cross_merchant_signals_table_preexisting` / `learned_sales_policies_table_preexisting` | Informational — `1` means drift path; upgrade converges only when shapes are equivalent |
| Preflight `stage_b_catalog_missing_*_count` | Informational — non-zero means partial catalog drift; post-success contract is authoritative |
| High `stage_b_catalog_missing_unique_constraint_count` with tables present | Investigate before GO — may indicate non-equivalent partial schema outside guard boundary |

### Bounded timeout policy

| Parameter | Value |
|-----------|-------|
| Default | `3600` sec |
| Minimum | `600` sec |
| Maximum | `7200` sec |

### Commands

```bash
export NAHLA_SKIP_DB_BOOTSTRAP=1

python scripts/operators/staging_migration_0032_to_0083.py preflight

export NAHLA_STAGING_MIGRATION_0032_TO_0083_CONFIRM=RUN_STAGING_0032_TO_0083
python scripts/operators/staging_migration_0032_to_0083.py run --timeout-sec 3600
```

Invokes exactly: `python -m alembic upgrade 0083`.

### Stage B completion attestation (provenance)

After guarded Stage B `run` and post-validation pass (`post_validation.schema_ok=true`),
record live source evidence before any Stage C backup/restore drill:

| Field | Attested value |
|-------|----------------|
| `alembic_revision` | `0083` |
| `public_table_count` | `96` |
| `schema_fingerprint_version` | `nahla_public_tables_sha256_v1` |
| `schema_fingerprint_sha256` | `1b9aca690e4eba0a0ffa1df8d59ecdd316d1a7f150e65bd2635d7fca4f5f54ae` |

Stage C DR canonical parity requires the versioned contract profile
`staging_pin_0083` (exact revision, table count, and fingerprint pin). See
`docs/engineering/staging-dr-canonical-parity-runbook.md`. Parity verification
fails closed when the source does not match a contracted profile.

## Stage C — 0083 → 0087 (A1-Expand)

### Gates beyond standard staging identity

0. **DR canonical parity (hard gate)** — pre-stage backup/restore drill must match
   `staging_pin_0083` on the live source (exact revision `0083`, `96` public
   tables, fingerprint
   `1b9aca690e4eba0a0ffa1df8d59ecdd316d1a7f150e65bd2635d7fca4f5f54ae`). See
   `docs/engineering/staging-dr-canonical-parity-runbook.md`.

1. **Catalog audit (hard gate)** — no pre-existing A1-expand tables (`external_customer_profiles`, `order_customer_identity_capability_state`, `conversation_a1_subject_bindings`). Optional standalone audit:

   ```bash
   python scripts/operators/staging_catalog_readonly_audit.py
   ```

2. **0084 product identity duplicate preflight (hard gate)** — aggregate count of duplicate `(tenant_id, external_id)` groups where `external_id IS NOT NULL AND external_id != ''` must be **0** before GO. Migration **0084** creates `uq_products_tenant_external_id_nonempty`.

3. **Extension availability (hard gate)** — `gen_random_uuid()` probe must succeed (PG 13+ native or `pgcrypto`). Manifest records queried `pgcrypto_extension_available` (0/1) without overwrite.

### Preflight duplicate counts (Stage C) — **hard gates**

| Key | Gate | Meaning |
|-----|------|---------|
| `duplicate_tenant_product_external_id_groups` | **Hard stop** | Must be **0** (0084 hazard) |
| `forbidden_a1_objects_present` | **Hard stop** | Must be **0** (catalog audit) |

### Bounded timeout policy (Stage C)

| Parameter | Value |
|-----------|-------|
| Default | `1800` sec |
| Minimum | `300` sec |
| Maximum | `3600` sec |

### Post-success contract (0087 only)

- `alembic_version = 0087` (repository head may be `0089`; runners never target `0089` or `head`)
- `uq_products_tenant_external_id_nonempty` index on `products` exists and is **valid** (`indisvalid = true`)
- `order_customer_identity_capability_state.state = expand`
- Orders FK/CHECK constraints present and **NOT VALID** (`convalidated = false`)
- Deferred order indexes from **0088** must **not** exist

### Commands

```bash
export NAHLA_SKIP_DB_BOOTSTRAP=1

python scripts/operators/staging_migration_0083_to_0087.py preflight

export NAHLA_STAGING_MIGRATION_0083_TO_0087_CONFIRM=RUN_STAGING_0083_TO_0087
python scripts/operators/staging_migration_0083_to_0087.py run --timeout-sec 1800
```

Invokes exactly: `python -m alembic upgrade 0087`.

## Explicit exclusions

| Out of scope | Notes |
|--------------|-------|
| **0088** A1-Validate | Separate maintenance window; see `docs/engineering/a1-order-identity-migration-rollout.md` |
| **0089** conversation bindings | **Present in repository** (PR #596); **not executed** by these runners |
| `alembic upgrade head` | Forbidden for this window |
| Bootstrap unfreeze | `NAHLA_SKIP_DB_BOOTSTRAP` must remain `1` |
| DR executor / backup-runner changes | Separate operator slices |
| Staging execution in this PR | Implementation + tests only |

## Safety contract summary (all stages)

1. **Target locked** — literal stage target only (`0032`, `0083`, or `0087`).
2. **Staging identity and DB binding** — `desirable-growth` + `staging`, host exactly `postgres-staging.railway.internal`.
3. **Bootstrap freeze** — `NAHLA_SKIP_DB_BOOTSTRAP=1` required for `run`.
4. **Explicit confirm** — distinct token per stage (see table above).
5. **Start revision** — exact base per stage.
6. **Versioned fingerprint** — `nahla_public_tables_sha256_v1` captured at execution time.
7. **Bounded Alembic** — list-args subprocess, no shell, no `head`.
8. **Post validation** — revision + contract metadata from stage contract module.
9. **Safe failures** — `error_class` + `stage` only on stdout JSON.

## CI discoverability

| Artifact | Location |
|----------|----------|
| Unit tests (all three stages) | `backend/tests/test_staging_migration_0030_to_0087_operators.py` |
| Explicit CI gate | `Guarded staging migration operator tests` in `lint-and-test` job |
| Legacy PG chain tests (0030→0087) | `backend/tests/test_legacy_migration_drift_0030_0087_pg.py` (`a1-postgres-integration` job) |
| Stage contracts | `scripts/operators/staging_migration_0030_to_0032_contract.py`, `..._0032_to_0083_contract.py`, `..._0083_to_0087_contract.py` |
| Catalog audit | `scripts/operators/staging_catalog_readonly_audit.py` |

## Related

- 0024→0030 runner — `docs/engineering/staging-migration-0024-to-0030-runbook.md`
- DR canonical parity contract — `docs/engineering/staging-dr-canonical-parity-runbook.md`
- A1 identity rollout — `docs/engineering/a1-order-identity-migration-rollout.md`
