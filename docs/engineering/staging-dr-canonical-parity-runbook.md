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
| Source eligibility | Source must match **one** `source_eligibility_profiles[]` entry on exact revision, table count, and full fingerprint |

## Source eligibility profiles (v1)

| `profile_id` | `alembic_revision` | `public_table_count` | Fingerprint pin |
|--------------|-------------------|----------------------|-----------------|
| `staging_pin_0024` | `0024` | `96` | `1b9aca…f5f54ae` (prior Phase A attestation) |
| `staging_pin_0030` | `0030` | `96` | `1b9aca…f5f54ae` (live source attestation during successful 0030 restore drill) |

Every profile requires a full 64-hex fingerprint pin. A historical `0016` hardcoded
gate is deliberately not retained as an eligibility profile: no full fingerprint
evidence was captured for it. Advancing staging requires a **contract bump** (new
`profile_id` with attested revision, count, and fingerprint; never an implicit
revision switch in shell).

## Why the PG migration test reports `+3`

The PostgreSQL migration test starts from a clean, ephemeral `0024` schema and
proves that migrations `0025–0029` add three public tables. That is a
**migration-chain behavior test**, not a measurement of live staging. The live
staging source attestation at revision `0030` measured `96` public tables and
fingerprint
`1b9aca690e4eba0a0ffa1df8d59ecdd316d1a7f150e65bd2635d7fca4f5f54ae`;
those measured values control `staging_pin_0030`. The difference may reflect
pre-existing schema/drift history and must not be converted into a `99` live
staging claim without a new source attestation.

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
| PostgreSQL proof | `backend/tests/test_staging_dr_canonical_parity_pg.py` (`a1-postgres-integration` job); proves the clean-chain `+3` migration delta separately from the live `0030` pin |
| Executor image build smoke | `staging-dr-executor-artifact` job in `.github/workflows/ci.yml` |

## Related (out of scope)

- Backup/restore scripts (`backup.sh`, `restore_verify.sh`) — separate deploy slice; not required for parity contract repair.
- Staging migration runners — see `docs/engineering/staging-migration-*-runbook.md`.
- Read-only single-endpoint attestation — separate `ops/staging_dr_attestation/` slice.
