# Staging legacy migration 0024 → 0030 — operator runbook

Controlled, staging-gated operator surface for the legacy recovery chain **0025–0030** when staging is pinned at **`0024`** after the successful 0016→0024 slice. This runbook covers the guarded runner and versioned preflight manifest only — not backup storage, deploy, production execution, or migrations **0031+**.

## Why this exists

- Staging controlled migration reached **`0024`**; bootstrap remains frozen (`NAHLA_SKIP_DB_BOOTSTRAP=1`).
- App head currently produces expected schema-lag errors for `tenants.is_platform_tenant` (0030) and later fields.
- **0025–0030** is additive schema with some non-idempotent `create_table` migrations, one data backfill (0027 `engine`), and forward ORM gap risk — hardened in this slice with F16 inspector guards.
- **0031 / 0032** contain customer duplicate data gates and are **explicitly out of scope** — do not use this runner beyond **`0030`**.

## Prerequisites

| Requirement | Value / check |
|-------------|----------------|
| Prior slice complete | Staging `alembic_version` exactly **`0024`** (via 0016→0024 runner or equivalent controlled path) |
| Branch merged & deployed to **staging** app container | Runner ships with app image |
| `RAILWAY_PROJECT_NAME` | `desirable-growth` |
| `RAILWAY_ENVIRONMENT_NAME` | `staging` |
| `ENVIRONMENT` | Must **not** be `production`, `prod`, or `live` |
| `NAHLA_SKIP_DB_BOOTSTRAP` | `1` (bootstrap frozen — required for **run**, recommended for entire window) |
| `DATABASE_URL` | Staging app configuration only (never log or paste into tickets); parsed host must exactly equal `postgres-staging.railway.internal` |
| Approved backup | Taken **after** 0024 baseline with manifest using **`schema_fingerprint_version=nahla_public_tables_sha256_v1`** |

## Post-0024 baseline

Before GO:

1. Confirm `alembic_version = 0024`.
2. Capture a **fresh preflight manifest** (below) — this records the execution-time table-set fingerprint. **Do not** compare against a pre-0024 fingerprint or hardcoded table counts.
3. Compare manifest fingerprint to the approved backup manifest (same `schema_fingerprint_version` only).
4. Review `destructive_preflight_counts` for drift indicators and 0027 backfill row counts.

## Fingerprint contract

| Field | Specification |
|-------|----------------|
| `schema_fingerprint_version` | `nahla_public_tables_sha256_v1` |
| Algorithm | SHA-256 hex digest of comma-joined **sorted** `public` base table names |
| `schema_fingerprint` | Full 64-char hex (authoritative compare) |
| `schema_fingerprint_display` | First 16 chars (display only) |
| `manifest_schema_version` | `staging_migration_manifest_v1` |

Source baseline after 0024 is captured at **execution time** via preflight — never hardcode prior table counts or fingerprints.

## Commands

All commands run **inside the staging app container** from repo root (`/app` in Railway).

### 1. Read-only preflight manifest

```bash
export NAHLA_SKIP_DB_BOOTSTRAP=1   # recommended for entire maintenance window

python scripts/operators/staging_migration_0024_to_0030.py preflight
```

Exit `0` → JSON manifest on stdout (`phase=preflight`).
Exit `1` → safe JSON error: `outcome`, `error_class`, `stage` only (no traceback, DSN, or row data).

### 2. Controlled migration (operator GO)

Requires every safety gate including explicit confirmation:

```bash
export NAHLA_SKIP_DB_BOOTSTRAP=1
export NAHLA_STAGING_MIGRATION_0024_TO_0030_CONFIRM=RUN_STAGING_0024_TO_0030

python scripts/operators/staging_migration_0024_to_0030.py run --timeout-sec 1800
```

The runner invokes exactly:

```text
python -m alembic upgrade 0030
```

with `cwd=database/`, bounded timeout, list-args subprocess (no shell). No parameter enables `head`, `0031+`, downgrade, or arbitrary revisions.

## Manifest fields (sanitized)

| Field | Meaning |
|-------|---------|
| `manifest_schema_version` | `staging_migration_manifest_v1` |
| `phase` | `preflight` \| `post_success` |
| `alembic_revision` | Current `alembic_version` |
| `public_table_count` | Count of public base tables at capture time |
| `schema_fingerprint` / `_display` | Versioned table-set digest |
| `destructive_preflight_counts` | Aggregate-only drift / data-sensitive indicators (see below) |
| `staging_identity_class` | `railway_staging_desirable_growth` |
| `bootstrap_freeze` | Whether `NAHLA_SKIP_DB_BOOTSTRAP=1` was observed |
| `timestamp_utc` | ISO-8601 UTC |
| `post_validation` | (post_success) `schema_ok`, missing table/column/index **names** only |
| `migration_outcome` | (post_success) `outcome`, `error_class`, `stage` |

### `destructive_preflight_counts` (0025–0030 window)

| Key | Meaning |
|-----|---------|
| `preexisting_product_interests_table` | `1` if table exists before upgrade (create_all drift) |
| `preexisting_promotions_table` | `1` if `promotions` exists before upgrade |
| `preexisting_offer_decisions_table` | `1` if `offer_decisions` exists before upgrade |
| `products_stock_columns_preexisting` | `1` if both `stock_quantity` and `in_stock` already on `products` |
| `orders_dashboard_columns_preexisting` | Count (0–3) of dashboard columns already on `orders` |
| `platform_tenant_column_preexisting` | `1` if `tenants.is_platform_tenant` already exists |
| `smart_automation_engine_backfill_rows` | Rows 0027 will UPDATE via `ENGINE_BY_TYPE` |

No PII, tenant/customer/order IDs, phone, email, raw SQL errors, or DSNs.

## Stop conditions (do not proceed)

Stop and investigate — **do not run** `run`:

| `error_class` / gate | Action |
|----------------------|--------|
| `identity_rejected` | Wrong project/environment or production marker |
| `database_binding_rejected` | Missing/malformed URL, non-PostgreSQL URL, localhost/unknown host, prod/live-marked host, or host other than allowlisted staging service |
| `bootstrap_freeze_missing` | Set `NAHLA_SKIP_DB_BOOTSTRAP=1`, redeploy if needed |
| `confirmation_missing` | Set `NAHLA_STAGING_MIGRATION_0024_TO_0030_CONFIRM=RUN_STAGING_0024_TO_0030` deliberately |
| `wrong_revision` | DB must be exactly `0024` |
| Preflight fingerprint mismatch vs approved backup manifest | **Restore from backup first** — do not GO |
| Unexpected drift counts vs operator expectations | Review partial `create_all` collision shapes before GO |

## Rollback / failure during `run`

| Situation | Operator action |
|-----------|-------------------|
| Timeout / `migration_nonzero_exit` | **Stop.** Keep `NAHLA_SKIP_DB_BOOTSTRAP=1`. Do not retry with `head` or multi-revision downgrade. |
| `post_validation_failed` | **Stop.** Schema incomplete at `0030` — treat as failed migration. |
| Destructive outcome not matching preflight expectations | **Restore from approved backup** (manifest fingerprint must match pre-GO preflight). |
| Casual `alembic downgrade` across 0025–0030 | **Forbidden** for this window — risk of dropping additive tables/columns still referenced by app head. |

After restore: remain bootstrap-frozen, re-run `preflight`, compare manifests, only then consider another GO.

The PostgreSQL suite includes a bounded 0030→0029 downgrade coherence test
to validate migration DDL on an ephemeral database. It is not an operational
rollback procedure: staging failure recovery remains restore-first.

## Explicit boundary at 0030

| In scope (this runbook) | Out of scope (future slice) |
|-------------------------|----------------------------|
| Revisions **0025–0030** | **0031+** customer duplicate gates |
| `tenants.is_platform_tenant` | **0087 / 0088** A1 identity |
| `product_interests`, `promotions`, `offer_decisions` | `alembic upgrade head` wrappers |
| 0027 `smart_automations.engine` backfill | Bootstrap unfreeze |
| Order dashboard columns (0026) | DR canonical parity contract — see `docs/engineering/staging-dr-canonical-parity-runbook.md` |

Do **not** proceed to 0031 until a separate approved operator slice exists.

## Safety contract summary

1. **Target locked** — literal `0030` only.
2. **Staging identity and DB binding** — `desirable-growth` + `staging`, plus PostgreSQL `DATABASE_URL` host exactly `postgres-staging.railway.internal`.
3. **Bootstrap freeze** — `NAHLA_SKIP_DB_BOOTSTRAP=1` required for `run`.
4. **Explicit confirm** — `NAHLA_STAGING_MIGRATION_0024_TO_0030_CONFIRM=RUN_STAGING_0024_TO_0030` (distinct from 0016→0024 token).
5. **Start revision** — exactly `0024`.
6. **Versioned fingerprint** — `nahla_public_tables_sha256_v1` captured at execution time.
7. **Bounded Alembic** — `python -m alembic upgrade 0030`, no shell, no `head`.
8. **Post validation** — revision `0030` + expected schema metadata from `staging_migration_0024_to_0030_contract.py`.
9. **Safe failures** — `error_class` + `stage` only on stdout JSON.

## CI discoverability

| Artifact | Location |
|----------|----------|
| Unit tests (all gates) | `backend/tests/test_staging_migration_0024_to_0030_operator.py` |
| Explicit CI gate | `Guarded staging migration operator tests` in `lint-and-test` job |
| Legacy PG chain tests (0024→0030 behavior) | `backend/tests/test_legacy_migration_drift_0025_0030_pg.py` (`a1-postgres-integration` job) |
| Contract constants | `scripts/operators/staging_migration_0024_to_0030_contract.py` |
| Fingerprint helper | `scripts/operators/schema_fingerprint.py` |

## Related (out of scope for this PR)

- Migrations **0031+** — separate future slice.
- 0016→0024 runner — unchanged; see `docs/engineering/staging-migration-0016-to-0024-runbook.md`.
- `0087` / `0088` A1 identity rollout — see `docs/engineering/a1-order-identity-migration-rollout.md`.
