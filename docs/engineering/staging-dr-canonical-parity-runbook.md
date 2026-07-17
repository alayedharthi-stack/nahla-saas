# Staging DR executor — operator runbook

Single source-controlled image for Railway service `nahla-stg-dr-job` (staging).
The executor runs a long-lived idle entrypoint so operators can `railway ssh`
into the container and invoke backup, restore, preflight, or canonical parity
scripts without public HTTP exposure.

## Repository boundary

| Artifact | Tracked path | Container path |
|----------|--------------|----------------|
| DR executor image | `ops/staging_dr_executor/` | Railway service `nahla-stg-dr-job` (staging) |
| Backup | `ops/staging_dr_executor/scripts/backup.sh` | `/dr/scripts/backup.sh` |
| Restore + verify | `ops/staging_dr_executor/scripts/restore_verify.sh` | `/dr/scripts/restore_verify.sh` |
| Target preflight | `ops/staging_dr_executor/scripts/target_preflight.sh` | `/dr/scripts/target_preflight.sh` |
| Shared guards | `ops/staging_dr_executor/scripts/common.sh` | `/dr/scripts/common.sh` |
| SSH idle entrypoint | `ops/staging_dr_executor/scripts/idle.sh` | `/dr/scripts/idle.sh` (`exec sleep infinity`) |
| Parity script | `ops/staging_dr_executor/scripts/verify_canonical_parity.sh` | `/dr/scripts/verify_canonical_parity.sh` |
| Versioned contract | `ops/staging_dr_executor/contracts/canonical_parity.json` | `/dr/contracts/canonical_parity.json` |
| Contract source | `scripts/operators/staging_dr_canonical_parity_contract.py` | Regenerate JSON via `python -m scripts.operators.staging_dr_canonical_parity emit-contract` |
| Deterministic harness | `scripts/operators/staging_dr_canonical_parity.py` | Local/CI only |

**Migration note:** Operational scripts previously lived only under
`.dr-staging-tmp/backup-runner/` (untracked deploy artifact). PR #605 added the
versioned canonical parity contract to `ops/staging_dr_executor/` but did not
track backup/restore/preflight. This executor image unifies both slices without
changing script behavior or secret/env contracts.

## Staging identity and secret handling

Every operational script sources `common.sh` and calls `require_staging_identity`:

- `RAILWAY_PROJECT_NAME` must equal `desirable-growth`
- `RAILWAY_ENVIRONMENT_NAME` must equal `staging`

Mismatch exits `2` with `BLOCK:` stderr — fail-closed before any backup, restore,
or parity work. Secrets are read from Railway service variables at runtime
(`NAHLA_STG_DR_*`, `SOURCE_PG*`, `TARGET_PG*`). No secrets are baked into the
image. Backup objects land in the private Railway bucket (`nahla-stg-dr-vault`);
the service has no public domain.

## Backup / restore operational contract

### Backup (`backup.sh`)

Requires: `NAHLA_STG_DR_ENCRYPT_KEY`, bucket/S3 credentials, `SOURCE_PG*`.

Pipeline: `pg_dump` (custom format) → `openssl enc -aes-256-cbc -pbkdf2` →
`aws s3 cp` to `staging/postgres-staging/<timestamp>/nahla-staging-logical.enc`.

```bash
railway ssh --service nahla-stg-dr-job -e staging -- bash /dr/scripts/backup.sh
```

### Restore + verify (`restore_verify.sh`)

Requires: encryption key, bucket/S3 credentials, `NAHLA_STG_DR_OBJECT_KEY`,
`TARGET_PG*`.

Pipeline: `aws s3 cp` → decrypt → `pg_restore`, then emits revision/table/row
attestation markers.

```bash
railway ssh --service nahla-stg-dr-job -e staging -- bash /dr/scripts/restore_verify.sh
```

### Target preflight (`target_preflight.sh`)

Fails closed (`exit 2`, `target_empty=false`) when the target database is not
empty. Use before restore drills on a fresh target.

```bash
railway ssh --service nahla-stg-dr-job -e staging -- bash /dr/scripts/target_preflight.sh
```

## Canonical parity contract

Controlled verification that a **restore target** matches the **live staging
source** on revision, public table count, and `nahla_public_tables_sha256_v1`
fingerprint — and that the source is an **explicitly contracted** staging pin
(not an implicit head).

| Field | Specification |
|-------|----------------|
| `schema_fingerprint_version` | `nahla_public_tables_sha256_v1` |
| Algorithm | SHA-256 hex digest of comma-joined **sorted public base table names** |
| Parity scope | Source and restore must match on revision, table count, and full fingerprint |
| Source eligibility | Source must match **one** `source_eligibility_profiles[]` entry on exact revision, table count, and full fingerprint |

### Source eligibility profiles (v1)

| `profile_id` | `alembic_revision` | `public_table_count` | Fingerprint pin |
|--------------|-------------------|----------------------|-----------------|
| `staging_pin_0024` | `0024` | `96` | `1b9aca…f5f54ae` (prior Phase A attestation) |
| `staging_pin_0030` | `0030` | `96` | `1b9aca…f5f54ae` (live source attestation during successful 0030 restore drill) |
| `staging_pin_0032` | `0032` | `96` | `1b9aca…f5f54ae` (guarded Stage A post-validation attestation) |
| `staging_pin_0083` | `0083` | `96` | `1b9aca…f5f54ae` (guarded Stage B post-validation attestation) |
| `staging_pin_0087` | `0087` | `101` | `2d3c6f4…77890db` (guarded Stage C post-validation attestation) |
| `staging_pin_0088` | `0088` | `101` | `2d3c6f4…77890db` (guarded A1-Validate post-validation attestation) |

Every profile requires a full 64-hex fingerprint pin. Advancing staging requires
a **contract bump** (new `profile_id` with attested revision, count, and
fingerprint; never an implicit revision switch in shell).

### Operator invocation

```bash
railway ssh --service nahla-stg-dr-job -e staging -- \
  bash /dr/scripts/verify_canonical_parity.sh
```

Sanitized success markers:

- `parity_contract_version=staging-dr-canonical-parity-v1`
- `canonical_full_sha_parity=true`
- `revision_parity=true`
- `public_table_count_parity=true`
- `source_contract_eligible=true`
- `matched_source_profile_id=staging_pin_0088` (when staging is pinned at 0088 after guarded A1-Validate)
- `matched_source_profile_id=staging_pin_0087` (when staging is pinned at 0087 after guarded Stage C)
- `matched_source_profile_id=staging_pin_0083` (when staging is pinned at 0083 after guarded Stage B)
- `matched_source_profile_id=staging_pin_0032` (when staging is pinned at 0032 after guarded Stage A)
- `matched_source_profile_id=staging_pin_0030` (when staging is pinned at 0030)

Fail-closed stderr tokens include `canonical_manifest_parity=false`,
`source_contract_eligible=false`, `contract_missing=false`, and
`destructive_aggregate_parity=false`.

## Why the PG migration test reports `+3`

The PostgreSQL migration test starts from a clean, ephemeral `0024` schema and
proves that migrations `0025–0029` add three public tables. That is a
**migration-chain behavior test**, not a measurement of live staging. The live
staging source attestation at revision `0030` measured `96` public tables and
fingerprint
`1b9aca690e4eba0a0ffa1df8d59ecdd316d1a7f150e65bd2635d7fca4f5f54ae`;
those measured values control `staging_pin_0030`. After guarded Stage A
(0030→0032), live staging attestation measured the same `96` public tables and
fingerprint
`1b9aca690e4eba0a0ffa1df8d59ecdd316d1a7f150e65bd2635d7fca4f5f54ae` at revision
`0032`; those measured values control `staging_pin_0032`. After guarded Stage B
(0032→0083), live staging attestation measured the same `96` public tables and
fingerprint
`1b9aca690e4eba0a0ffa1df8d59ecdd316d1a7f150e65bd2635d7fca4f5f54ae` at revision
`0083`; those measured values control `staging_pin_0083`. After guarded Stage C
(0083→0087), live staging attestation measured `101` public tables and
fingerprint
`2d3c6f4ffdd011517352efa5f1b1d881c30b66bf189e478197a6fad0777890db` at revision
`0087`; those measured values control `staging_pin_0087`. After guarded A1-Validate
(0087→0088), live staging attestation measured `101` public tables and
fingerprint
`2d3c6f4ffdd011517352efa5f1b1d881c30b66bf189e478197a6fad0777890db` at revision
`0088`; those measured values control `staging_pin_0088`.

## Regenerating the baked contract

After editing `staging_dr_canonical_parity_contract.py`:

```bash
python -m scripts.operators.staging_dr_canonical_parity emit-contract
```

Commit both the Python contract and `ops/staging_dr_executor/contracts/canonical_parity.json`.

## CI discoverability

| Artifact | Location |
|----------|----------|
| Canonical parity unit tests | `backend/tests/test_staging_dr_canonical_parity.py` |
| Executor image contract tests | `backend/tests/test_staging_dr_executor_image.py` |
| PostgreSQL proof | `backend/tests/test_staging_dr_canonical_parity_pg.py` (`a1-postgres-integration` job) |
| Executor image build + script smoke | `staging-dr-executor-artifact` job in `.github/workflows/ci.yml` |

## Related (separate slices)

- Staging migration runners — see `docs/engineering/staging-migration-*-runbook.md`.
- Read-only single-endpoint attestation — `ops/staging_dr_attestation/`.
