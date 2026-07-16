# Staging DR canonical parity — operator runbook

Controlled, staging-gated verification that a **restore target** matches the
**live staging source** on revision, public table count, and
`nahla_public_tables_sha256_v1` fingerprint — and that the source is an
**explicitly contracted** staging pin (not an implicit head).

## Repository boundary

| Artifact | Tracked path | Deploy target |
|----------|--------------|---------------|
| DR executor image | `ops/staging_dr_executor/` | Railway service `nahla-stg-dr-job` (staging) |
| Parity script | `ops/staging_dr_executor/scripts/verify_canonical_parity.sh` | `/dr/scripts/verify_canonical_parity.sh` |
| Versioned contract | `ops/staging_dr_executor/contracts/canonical_parity.json` | `/dr/contracts/canonical_parity.json` |
| Contract source | `scripts/operators/staging_dr_canonical_parity_contract.py` | Regenerate JSON via `python -m scripts.operators.staging_dr_canonical_parity emit-contract` |
| Deterministic harness | `scripts/operators/staging_dr_canonical_parity.py` | Local/CI only |

**Prior state:** `verify_canonical_parity.sh` lived only under `.dr-staging-tmp/backup-runner/` (untracked) and hardcoded `alembic_version=0016` + `public_table_count=96`, which blocked contract completion after staging advanced to `0030`.

## Fingerprint contract

| Field | Specification |
|-------|----------------|
| `schema_fingerprint_version` | `nahla_public_tables_sha256_v1` |
| Algorithm | SHA-256 hex digest of comma-joined **sorted public base table names** |
| Parity scope | Source and restore must match on revision, table count, and full fingerprint |
| Source eligibility | Source must match **one** `source_eligibility_profiles[]` entry in the baked contract |

## Source eligibility profiles (v1)

| `profile_id` | `alembic_revision` | `public_table_count` | Fingerprint pin |
|--------------|-------------------|----------------------|-----------------|
| `staging_pin_0016` | `0016` | `96` | none |
| `staging_pin_0024` | `0024` | `96` | pinned (Phase A attestation) |
| `staging_pin_0030` | `0030` | `99` | none (parity + PG test) |

Advancing staging requires a **contract bump** (new `profile_id`, never an implicit revision switch in shell).

## Operator invocation (post-deploy)

From an operator workstation with Railway access:

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
- `matched_source_profile_id=staging_pin_0030` (when staging is pinned at 0030)

Fail-closed stderr tokens include `canonical_manifest_parity=false`, `source_contract_eligible=false`, `contract_missing=false`, and `destructive_aggregate_parity=false`.

## Regenerating the baked contract

After editing `staging_dr_canonical_parity_contract.py`:

```bash
python -m scripts.operators.staging_dr_canonical_parity emit-contract
```

Commit both the Python contract and `ops/staging_dr_executor/contracts/canonical_parity.json`.

## CI discoverability

| Artifact | Location |
|----------|----------|
| Unit tests (gates + script guard) | `backend/tests/test_staging_dr_canonical_parity.py` |
| PostgreSQL profile proof | `backend/tests/test_staging_dr_canonical_parity_pg.py` (`a1-postgres-integration` job) |
| Executor image build smoke | `staging-dr-executor-artifact` job in `.github/workflows/ci.yml` |

## Related (out of scope)

- Backup/restore scripts (`backup.sh`, `restore_verify.sh`) — separate deploy slice; not required for parity contract repair.
- Staging migration runners — see `docs/engineering/staging-migration-*-runbook.md`.
- Read-only single-endpoint attestation — separate `ops/staging_dr_attestation/` slice.
