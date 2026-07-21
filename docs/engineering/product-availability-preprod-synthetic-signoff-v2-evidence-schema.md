# ARCH-001 preprod synthetic signoff v2 — evidence schema

Initiative: `ARCH-001-PREPROD-SYNTHETIC-SIGNOFF-v2`  
Bundle schema: `product_availability_preprod_synthetic_signoff_v2`

## Purpose

Replace the pre-production **48-hour zero-traffic observation window** prerequisite
with a **phase/lifecycle-based synthetic matrix signoff**. Production bundles ingest
**externally generated runtime-bound phase artifacts** produced after real lifecycle
actions inside the isolated preprod shadow service. CI contract self-test produces
**ineligible** bundles only (`evidence_class=ci_contract_self_test`,
`eligible_for_signoff=false`).

| Claim | Allowed in v2 preprod signoff? |
|-------|-------------------------------|
| Synthetic 7/7 matrix PASS | Yes |
| Zero customer text mutation | Yes |
| Zero added LLM/provider calls | Yes |
| Organic / real-channel traffic observed | **No** (`traffic_claim=synthetic_probes_only`) |
| Post-approval canonical shadow canary | **No** — remains `pending` |
| Enforce eligibility | **No** — remains `pending` |
| Container restart / fresh redeploy | **Only** via runtime-bound phase artifacts with lifecycle attestation |

Legacy v1 bundles (`product_availability_shadow_staging_signoff_v1`) remain
**historically readable** but are **not sufficient** to unlock preprod gates.

## Evidence classes

| Class | `eligible_for_signoff` | Gate consumption |
|-------|------------------------|------------------|
| `production_signoff` | `true` | Unlocks preprod gates when identity-bound |
| `ci_contract_self_test` | `false` | **Rejected** by all gate consumers |

## Artifact location

- Operator output: `docs/engineering/staging-evidence/arch001-preprod-synthetic-signoff-v2-<date>.json`
- Gate env ref: `NAHLA_ARCH001_PREPROD_SYNTHETIC_SIGNOFF_V2_ARTIFACT`
- HMAC key env ref: `NAHLA_ARCH001_PREPROD_SYNTHETIC_SIGNOFF_V2_HMAC_KEY`

## Identity binding (operator-supplied — not hardcoded in source)

Gate consumers bind verification to the **current** pinned revision, manifest digest,
and isolated preprod shadow service identity supplied via env/config. Fail closed when
any expected identity field is absent or mismatched.

| Env var | Purpose |
|---------|---------|
| `NAHLA_ARCH001_PREPROD_PINNED_REVISION` | Pinned git SHA (also accepts `NAHLA_REAL_CHANNEL_ACCEPTANCE_PINNED_REVISION`) |
| `NAHLA_ARCH001_PREPROD_EXPECTED_MANIFEST_DIGEST` | 64-char SHA-256 over closed runtime artifact manifest |
| `NAHLA_ARCH001_PREPROD_ISOLATED_SERVICE_NAME` | Isolated shadow service name (e.g. `nahla-arch001-shadow`) |
| `NAHLA_ARCH001_PREPROD_ISOLATED_SERVICE_ID` | Isolated shadow service UUID |
| `NAHLA_ARCH001_PREPROD_ISOLATED_DEPLOYMENT_ID` | Post-redeploy deployment UUID (repeat matrices bind here) |

### `identity_binding` object

| Key | Requirement |
|-----|-------------|
| `pinned_target_revision` | 7–40 char git SHA |
| `manifest_digest` | 64-char SHA-256 over closed runtime artifact manifest |
| `service_role` | `isolated_preprod_shadow` for preprod bundle |
| `service_name` | Operator-supplied isolated service name |
| `service_id` | Operator-supplied UUID |
| `deployment_id` | Post-redeploy deployment UUID |
| `image_digest` | 64-char SHA-256 or `absent` |

**No environment-specific service UUID is hardcoded in source.** Canonical control
(`nahla-saas`, guard=off) is proven separately in the teardown artifact.

## Bundle shape (unsigned fields before `signature`)

| Field | Type | Description |
|-------|------|-------------|
| `bundle_schema_version` | string | `product_availability_preprod_synthetic_signoff_v2` |
| `initiative_id` | string | `ARCH-001-PREPROD-SYNTHETIC-SIGNOFF-v2` |
| `evidence_class` | string | `production_signoff` or `ci_contract_self_test` |
| `eligible_for_signoff` | boolean | `true` only for production bundles |
| `traffic_claim` | string | Must be exactly `synthetic_probes_only` |
| `identity_binding` | object | Pinned SHA, manifest digest, service/deployment/image binding |
| `lifecycle_phases` | array[6] | baseline → container_restart → fresh_pinned_redeploy → repeat_matrix_1..3 |
| `negative_controls` | object | Four runtime-bound BLOCK controls |
| `stable_counter_reference` | object | Absolute expected counters from closed matrix |
| `post_approval` | object | `canonical_shadow_canary=pending`, `enforce_eligibility=pending` |
| `superseded_invalid_windows` | array | **Required**, nonempty; retired 48h windows marked inactive |
| `teardown_proof` | object | Verified isolated + canonical teardown evidence |
| `signed_at_utc` | string | ISO-8601 UTC |
| `signature` | string | `hmac-sha256:<digest>` with domain prefix `ARCH001_PREPROD_V2\0` |

### Phase artifact (`arch001_preprod_phase_artifact_v1`)

Each lifecycle phase is a separate JSON file ingested at bundle assembly time.
Artifacts must prove:

- `execution_mode=in_container`
- `target_app_root=/app`
- Pinned revision + manifest digest match expected identity
- Service/deployment/image identity binding
- Matrix 7/7 with absolute stable counters (7 evaluated turns, 2 would_rewrite, four zero safety counters)
- Phase-specific lifecycle attestation:
  - `baseline`: initial deploy
  - `container_restart`: bounded restart evidence (may retain deployment ID)
  - `fresh_pinned_redeploy`: new deployment ID ≠ baseline, same revision/manifest
  - `repeat_matrix_*`: bind to post-redeploy identity; timestamps strictly ordered with ≥15m spacing

### Negative controls (must BLOCK, runtime-bound)

| Control ID | Expected code |
|------------|---------------|
| `wrong_manifest` | `artifact_manifest_mismatch` |
| `wrong_revision` | `runtime_revision_mismatch` |
| `outside_app` | `runtime_execution_required` |
| `enforce_enabled` | `enforce_mode_enabled` |

Each control artifact must bind to the same current revision/manifest/runtime identity.

### Teardown proof (`arch001_preprod_teardown_v1`)

Required fields (placeholder/unverified values **BLOCK**):

- Isolated service: `guard_mode=off`, `service_state` in `{stopped, down}`, verified timestamp, prior deployment binding
- Canonical control: `guard_mode=off`, `service_role=canonical_control`, verified timestamp
- `no_domains` / `no_provider_credentials` on isolated preprod shadow service

## Production evidence flow

1. Deploy isolated preprod shadow service (`nahla-arch001-shadow` role) with no domains and no provider credentials.
2. After each real lifecycle action, run the runtime-bound synthetic matrix inside `/app` and save a phase artifact JSON (`baseline.json`, `container_restart.json`, etc.).
3. Record runtime-bound negative-control artifacts and verified teardown proof.
4. Assemble and sign the production bundle:

```bash
export NAHLA_ARCH001_PREPROD_PINNED_REVISION=<SHA>
export NAHLA_ARCH001_PREPROD_EXPECTED_MANIFEST_DIGEST=<64-char-digest>
export NAHLA_ARCH001_PREPROD_ISOLATED_SERVICE_NAME=nahla-arch001-shadow
export NAHLA_ARCH001_PREPROD_ISOLATED_SERVICE_ID=<uuid>
export NAHLA_ARCH001_PREPROD_ISOLATED_DEPLOYMENT_ID=<post-redeploy-uuid>
export NAHLA_ARCH001_PREPROD_SYNTHETIC_SIGNOFF_V2_HMAC_KEY=<min-32-byte-secret>

python -m scripts.operators.product_availability_preprod_synthetic_signoff_v2 \
  assemble-bundle \
  --phase-dir ./phase-artifacts \
  --teardown ./teardown-proof.json \
  --negative-controls-dir ./negative-controls \
  --output docs/engineering/staging-evidence/arch001-preprod-synthetic-signoff-v2.json
```

5. Point gate env vars at the signed bundle and verify:

```bash
export NAHLA_ARCH001_PREPROD_SYNTHETIC_SIGNOFF_V2_ARTIFACT=docs/engineering/staging-evidence/arch001-preprod-synthetic-signoff-v2.json
python -m scripts.operators.product_availability_preprod_synthetic_signoff_v2 verify-artifact-env
```

## CI-only commands (ineligible for signoff)

```bash
# Contract self-test: validates logic, signs ineligible CI bundle
python -m scripts.operators.product_availability_preprod_synthetic_signoff_v2 contract-self-test

# Ingest/validate a single externally produced phase artifact
python -m scripts.operators.product_availability_preprod_synthetic_signoff_v2 \
  ingest-phase-artifact ./phase-artifacts/baseline.json

# Verify archived production artifact (requires identity env)
python -m scripts.operators.product_availability_preprod_synthetic_signoff_v2 \
  verify-bundle docs/engineering/staging-evidence/arch001-preprod-synthetic-signoff-v2.json

# Read legacy v1 (historical only)
python -m scripts.operators.product_availability_preprod_synthetic_signoff_v2 \
  verify-legacy-v1 docs/engineering/staging-evidence/product-availability-shadow-baseline-2026-07-18.json
```

**Removed:** `full-probe` and in-process multi-phase labeling — these do not prove lifecycle
evidence and must not be used for production signoff.

## HMAC requirements

- Minimum 32-byte key; known test/default keys rejected in production assembly/verification
- Domain-separated canonical payload: `ARCH001_PREPROD_V2\0` prefix
- `compare_digest` for signature verification
- Malformed/unreadable JSON returns bounded fail-closed report (no throw)

## Rollback / invalidation

1. **Supersede bundle:** add the window/bundle ID to `superseded_invalid_windows` with
   `active=false`, reason, and `superseded_at_utc`. Re-sign a replacement bundle.
2. **Invalidate gate consumption:** unset `NAHLA_ARCH001_PREPROD_SYNTHETIC_SIGNOFF_V2_ARTIFACT`
   from any downstream operator env. Real-channel acceptance and staging consolidation
   gates fail closed immediately.
3. **Do not rewrite history:** leave v1 JSON artifacts in `staging-evidence/`; mark them
   superseded in the replacement v2 bundle only.
4. **Post-approval path unchanged:** canonical shadow during limited allowlisted canary and
   enforce eligibility review remain separate operator workflows after v2 preprod signoff.

## Gate consumption

| Consumer | Verification helper | Identity binding |
|----------|---------------------|------------------|
| Real-channel acceptance | `verify_arch001_preprod_signoff_for_gate()` | Pinned revision + manifest + isolated service identity |
| Real-channel session | same | same |
| Staging config consolidation | `gate_arch001_teardown_proof` | same + teardown proof ref |

`NAHLA_ARCH001_SHADOW_SIGNOFF_CONFIRM=true` alone is **not sufficient**.

## CI

`backend/tests/test_product_availability_preprod_synthetic_signoff_v2_probe.py`
