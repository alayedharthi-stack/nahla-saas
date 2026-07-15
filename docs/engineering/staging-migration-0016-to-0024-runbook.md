# Staging legacy migration 0016 → 0024 — operator runbook

Controlled, staging-gated operator surface for the legacy drift-recovery chain **0017–0024** when staging is pinned at **`0016`**. This runbook covers the guarded runner and versioned preflight manifest only — not backup storage, deploy, or production execution.

## Why this exists

- Staging must never use unbounded `alembic upgrade head` for this recovery window.
- Backup/DR scripts previously used inconsistent table fingerprints (e.g. MD5 in `.dr-staging-tmp/verify_all.py`). Operators must compare manifests using **`schema_fingerprint_version=nahla_public_tables_sha256_v1`** before GO.
- The platform owns deterministic gates, structured manifests, and safe failure tokens — not conversational prose.

## Prerequisites

| Requirement | Value / check |
|-------------|----------------|
| Branch merged & deployed to **staging** app container | Runner ships with app image |
| `RAILWAY_PROJECT_NAME` | `desirable-growth` |
| `RAILWAY_ENVIRONMENT_NAME` | `staging` |
| `ENVIRONMENT` | Must **not** be `production`, `prod`, or `live` |
| `NAHLA_SKIP_DB_BOOTSTRAP` | `1` (bootstrap frozen — required for **run**, recommended for entire window) |
| `DATABASE_URL` | Staging app configuration only (never log or paste into tickets); parsed host must exactly equal the allowlisted staging service `postgres-staging.railway.internal` |
| Alembic revision at start | Exactly `0016` |
| Salla duplicates | Clean — `scripts/verify_salla_no_duplicates.py` exit `0` |
| Approved backup | Taken with manifest using **same** `schema_fingerprint_version` |

## Fingerprint contract

| Field | Specification |
|-------|----------------|
| `schema_fingerprint_version` | `nahla_public_tables_sha256_v1` |
| Algorithm | SHA-256 hex digest of comma-joined **sorted** `public` base table names |
| `schema_fingerprint` | Full 64-char hex (authoritative compare) |
| `schema_fingerprint_display` | First 16 chars (display only) |
| `manifest_schema_version` | `staging_migration_manifest_v1` |

**Do not** compare against legacy MD5 fingerprints from ad-hoc DR scripts. Re-run preflight with this operator after merge so backup and source manifests share the versioned contract.

## Commands

All commands run **inside the staging app container** from repo root (`/app` in Railway).

### 1. Read-only preflight manifest

```bash
export NAHLA_SKIP_DB_BOOTSTRAP=1   # recommended for entire maintenance window

python scripts/operators/staging_migration_0016_to_0024.py preflight
```

Exit `0` → JSON manifest on stdout (`phase=preflight`).  
Exit `1` → safe JSON error: `outcome`, `error_class`, `stage` only (no traceback, DSN, or row data).

Capture stdout to a file for audit, e.g. `staging-0016-preflight-manifest.json`.

### 2. Controlled migration (operator GO)

Requires every safety gate including explicit confirmation:

```bash
export NAHLA_SKIP_DB_BOOTSTRAP=1
export NAHLA_STAGING_MIGRATION_CONFIRM=RUN_STAGING_0016_TO_0024

python scripts/operators/staging_migration_0016_to_0024.py run --timeout-sec 1800
```

The runner invokes exactly:

```text
python -m alembic upgrade 0024
```

with `cwd=database/`, bounded timeout, list-args subprocess (no shell). No parameter enables `head`, `0025+`, downgrade, or arbitrary revisions.

Exit `0` → post-success manifest (`phase=post_success`, revision `0024`, schema metadata checks).  
Exit `1` → safe failure token only.

## Manifest fields (sanitized)

| Field | Meaning |
|-------|---------|
| `manifest_schema_version` | `staging_migration_manifest_v1` |
| `phase` | `preflight` \| `post_success` |
| `alembic_revision` | Current `alembic_version` |
| `public_table_count` | Count of public base tables |
| `schema_fingerprint` / `_display` | Versioned table-set digest |
| `destructive_preflight_counts` | Aggregate-only: `waba_duplicate_groups`, `order_duplicate_groups`, `zombie_automation_rows` |
| `salla_preflight_outcome` | `pass` \| `fail` (no customer/store identifiers) |
| `staging_identity_class` | `railway_staging_desirable_growth` |
| `bootstrap_freeze` | Whether `NAHLA_SKIP_DB_BOOTSTRAP=1` was observed |
| `timestamp_utc` | ISO-8601 UTC |
| `post_validation` | (post_success) `schema_ok`, missing table/column/index **names** only |
| `migration_outcome` | (post_success) `outcome`, `error_class`, `stage` |

No PII, tenant/customer/order IDs, phone, email, raw SQL errors, or DSNs.

## Stop conditions (do not proceed)

Stop and investigate — **do not run** `run`:

| `error_class` / gate | Action |
|----------------------|--------|
| `identity_rejected` | Wrong project/environment or production marker |
| `database_binding_rejected` | Missing/malformed URL, non-PostgreSQL URL, localhost/unknown host, a prod/live-marked host, or a host other than the allowlisted staging service |
| `bootstrap_freeze_missing` | Set `NAHLA_SKIP_DB_BOOTSTRAP=1`, redeploy if needed |
| `confirmation_missing` | Set `NAHLA_STAGING_MIGRATION_CONFIRM=RUN_STAGING_0016_TO_0024` deliberately |
| `wrong_revision` | DB must be exactly `0016`; reconcile drift separately |
| `salla_preflight_failed` | Run `python scripts/cleanup_salla_duplicates.py --execute`, re-verify |
| Preflight fingerprint mismatch vs approved backup manifest | Restore from backup or reconcile source — do not GO |
| Unexpected rise in `destructive_preflight_counts` | Review 0022/0023/0024 remediation impact before GO |

## Rollback / failure during `run`

| Situation | Operator action |
|-----------|-------------------|
| Timeout / `migration_nonzero_exit` | **Stop.** Keep `NAHLA_SKIP_DB_BOOTSTRAP=1`. Do not retry with `head` or multi-revision downgrade. |
| `post_validation_failed` | **Stop.** Schema incomplete at `0024` — treat as failed migration. |
| Destructive outcome not matching preflight expectations | **Restore from approved backup** (manifest fingerprint must match pre-GO preflight). |
| Casual `alembic downgrade` across 0017–0024 | **Forbidden** for this window — data loss risk (0024 zombie purge, 0023 order merge, 0022 WABA nulling). |

After restore: remain bootstrap-frozen, re-run `preflight`, compare manifests, only then consider another GO.

## Safety contract summary

1. **Target locked** — literal `0024` only.
2. **Staging identity and DB binding** — `desirable-growth` + `staging`, plus a parsed PostgreSQL `DATABASE_URL` whose host exactly matches `postgres-staging.railway.internal`; rejects localhost, unknown, and prod/live-marked hosts without logging the URL or host.
3. **Bootstrap freeze** — `NAHLA_SKIP_DB_BOOTSTRAP=1` required for `run`.
4. **Explicit confirm** — `NAHLA_STAGING_MIGRATION_CONFIRM=RUN_STAGING_0016_TO_0024`.
5. **Start revision** — exactly `0016`.
6. **Salla preflight** — `verify_salla_no_duplicates.py` must pass; outcome token only in manifest.
7. **Destructive aggregates** — pre-counts for WABA duplicates, order duplicates, zombie automations.
8. **Versioned fingerprint** — `nahla_public_tables_sha256_v1` in every manifest.
9. **Bounded Alembic** — `python -m alembic upgrade 0024`, no shell, no `head`.
10. **Post validation** — revision `0024` + expected indexes/columns from `staging_migration_contract.py`.
11. **Safe failures** — `error_class` + `stage` only on stderr/stdout JSON.

## CI discoverability

| Artifact | Location |
|----------|----------|
| Unit tests (all gates) | `backend/tests/test_staging_migration_0016_to_0024_operator.py` |
| Explicit CI gate | `Guarded staging migration operator tests` runs `python -m pytest backend/tests/test_staging_migration_0016_to_0024_operator.py -q --maxfail=1` in `lint-and-test`; missing file or a failing test fails CI |
| Legacy PG chain tests (0016→0024 behavior) | `backend/tests/test_legacy_migration_drift_0020_0024_pg.py` (`a1-postgres-integration` job) |
| Contract constants | `scripts/operators/staging_migration_contract.py` |
| Fingerprint helper | `scripts/operators/schema_fingerprint.py` |

## Related (out of scope for this PR)

- Migrations `0017`–`0024` themselves — unchanged.
- `0087` / `0088` A1 identity rollout — see `docs/engineering/a1-order-identity-migration-rollout.md`.
- Legacy MD5 DR scripts under `.dr-staging-tmp/` — superseded for fingerprint compare by this manifest version; do not commit that directory.
