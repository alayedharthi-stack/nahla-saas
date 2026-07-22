# Staging acceptance config consolidation runbook

**Status:** Preparation only (default-off). Do **not** execute until ARCH-001 preprod
synthetic signoff v2 bundle is approved, shadow mode is torn down, and teardown proof is recorded.

## Problem

Two staging public app services (`nahla-saas` and `nahla-saas-staging`) create
ambiguity for real-channel acceptance routing. This operator consolidates
staging-only channel/config onto one **canonical** app without disturbing ARCH-001
shadow on `nahla-saas` during the observation window.

## Canonical recommendation

| Role | Service | Rationale |
|------|---------|-----------|
| **Canonical (destination)** | `nahla-saas` | ARCH-001 shadow runs here; all staging runbooks use `--service nahla-saas --environment staging` |
| **Legacy source** | `nahla-saas-staging` | Hypothetical/legacy duplicate; config may be copied **from** here only after ARCH-001 teardown |

**Do not** choose `nahla-saas-staging` as canonical while ARCH-001 is active on `nahla-saas`.

## Closed Railway allowlist

| Dimension | Allowlisted value |
|-----------|-------------------|
| Project name | `desirable-growth` |
| Environment name | `staging` |
| Project ID | `f0090862-0a40-4293-bd5d-e94df58762b5` |
| Environment ID | `b3d51523-7544-4d5c-b510-631b334cd8a7` |
| Canonical service | `nahla-saas` — `686b36c5-a926-4e58-912a-5e9d13fbc2e7` |
| Legacy source service | `nahla-saas-staging` — `d0282eea-05fe-49bf-bd58-e663e8585516` |

**Production rejection:** any `production` / `prod` / `live` marker in environment or
forbidden service names fails closed with exit code 2. The known production
environment ID `ede962ce-3042-4dae-94de-623837e83ed9` is explicitly denied.

These non-secret resource identifiers were verified on 2026-07-18 from authenticated,
read-only `railway list --json` inventory. Both app service IDs are members of the
staging environment. The canonical `nahla-saas` service also has a production
instance, so environment-ID matching remains mandatory for every operation.

## Migratable variable keys (closed allowlist)

Names only — **never log values**.

| Key | Notes |
|-----|-------|
| `BACKEND_URL` | Public webhook target |
| `D360_API_BASE_URL` | 360dialog channel API (**legacy/transition — not acceptance readiness**) |
| `D360_PARTNER_HUB_BASE` | Partner hub (optional) |
| `D360_PARTNER_API_KEY` | Secret |
| `D360_PARTNER_ID` | Partner ID |
| `D360_WEBHOOK_INTERNAL_SECRET` | Secret |
| `META_APP_SECRET` | Meta signature verification (**required for channel readiness**) |
| `META_WEBHOOK_ENFORCE_SIGNATURE` | Must not be weakened to `false` |
| `META_WEBHOOK_ALLOW_MISSING_SIGNATURE` | Must not be weakened to `true` |
| `WHATSAPP_API_URL` | Graph API base |
| `WHATSAPP_TOKEN` | Outbound send token (secret) |
| `WHATSAPP_VERIFY_TOKEN` | Webhook verification |
| `DATABASE_URL` | **Only** Railway reference to canonical staging Postgres |
| `REDIS_URL` | **Only** Railway reference to canonical staging Redis |

### Protected keys (never touched by apply)

- `NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE` (ARCH-001)
- `NAHLA_REAL_CHANNEL_ACCEPTANCE_ENABLED`
- `NAHLA_REAL_CHANNEL_ACCEPTANCE_CONFIRM`

## Execution gates

| Gate | Env var | Requirement |
|------|---------|-------------|
| Master enable | `NAHLA_STAGING_ACCEPTANCE_CONFIG_CONSOLIDATION_ENABLED` | Unset or not truthy (default-off) |
| Apply confirm | `NAHLA_STAGING_ACCEPTANCE_CONFIG_CONSOLIDATION_CONFIRM` | Exact token `consolidate-staging-acceptance-config` |
| ARCH-001 preprod signoff | `NAHLA_ARCH001_PREPROD_SYNTHETIC_SIGNOFF_V2_ARTIFACT` + HMAC key + identity binding env | Valid v2 production bundle bound to current revision/manifest/isolated service |
| ARCH-001 teardown proof | `NAHLA_ARCH001_SHADOW_TEARDOWN_PROOF` | Reference to approved teardown evidence artifact |
| Snapshot encryption | `NAHLA_STAGING_ACCEPTANCE_CONFIG_SNAPSHOT_KEY` | Operator-held key for reversible snapshot |
| Revision pin | `NAHLA_STAGING_ACCEPTANCE_CONFIG_PINNED_REVISION` | Git SHA for post-apply attestation |
| Staging identity | `RAILWAY_PROJECT_NAME=desirable-growth` | Fail-closed |
| Staging identity | `RAILWAY_ENVIRONMENT_NAME=staging` | Fail-closed |

**BLOCK apply** when `NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE=shadow` on canonical service.

## Operator commands (safe now — no Railway mutations)

```bash
# Default-off gate (CI-safe)
python -m scripts.operators.staging_acceptance_config_consolidation default-off

# Staging identity preflight (no Railway)
python -m scripts.operators.staging_acceptance_config_consolidation preflight

# Routing selection guidance (no auto-delete)
python -m scripts.operators.staging_acceptance_config_consolidation routing-selection

# Fixture-backed inventory (names/presence only)
python -m scripts.operators.staging_acceptance_config_consolidation inventory \
  --fixture docs/engineering/staging-evidence/staging-acceptance-config-fixture.json

# Dry-run migration plan + conflict detection (HMAC fingerprints, no values)
python -m scripts.operators.staging_acceptance_config_consolidation dry-run-plan \
  --fixture docs/engineering/staging-evidence/staging-acceptance-config-fixture.json

# Summary with READY/BLOCK status
python -m scripts.operators.staging_acceptance_config_consolidation summary \
  --fixture docs/engineering/staging-evidence/staging-acceptance-config-fixture.json
```

## Execution sequence (after ARCH-001 teardown — not now)

1. Verify ARCH-001 shadow torn down:
   ```bash
   python -m scripts.operators.product_availability_truth_guard_shadow_observation teardown
   railway variables --environment staging \
     --set "NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE=off" \
     --service nahla-saas
   ```
2. Set `NAHLA_ARCH001_SHADOW_SIGNOFF_CONFIRM=true` and teardown proof ref.
3. Export Railway observation JSON (names/structure only in repo; values stay in secure store).
4. Run `inventory` → `dry-run-plan` → resolve conflicts manually if any.
5. Take encrypted snapshot (`snapshot` with `NAHLA_STAGING_ACCEPTANCE_CONFIG_SNAPSHOT_KEY`).
6. Set master enable + confirmation token; run apply (one controlled deploy on canonical only).
7. Verify: `/version`, health/db, webhook route, tenant routing, signature mode, no accidental flags.
8. Route Meta Cloud API direct webhooks to canonical `BACKEND_URL` only (document; do not auto-delete legacy service/domains). 360dialog remains legacy/transition-only.

## Rollback

```bash
# Requires same snapshot key + confirmation token
python -m scripts.operators.staging_acceptance_config_consolidation rollback \
  --snapshot <snapshot_id>.json
```

Restores exact prior canonical variable references/values from encrypted snapshot and triggers one deploy.

## Channel readiness (Meta Cloud API direct only)

**Closed target path:** Meta Cloud API direct (`meta_cloud_api_direct`). **360dialog is
legacy/transition-only** and must **not** satisfy, block, or shape acceptance readiness.
D360 absence is never reported as a gap.

**Target onboarding (architecture):** per-merchant **Meta Embedded Signup** yielding
merchant-owned WABA, Phone Number ID, and Access Token. **Meta Business Verification**
is the current external blocker — not a Nahla runtime gate.

**No permanent staging WABA dependency.** Do not create or document a standing staging
WABA/channel that production would rely on.

### Required on merged canonical config (platform credentials)

- `META_APP_SECRET`
- `WHATSAPP_VERIFY_TOKEN`
- `WHATSAPP_TOKEN`
- `BACKEND_URL` (canonical staging public URL)
- Direct Meta webhook route `/webhook/whatsapp` on canonical app (inventory only — **not** readiness evidence)
- Signature verification must remain enforced (`META_WEBHOOK_ENFORCE_SIGNATURE` not weakened)
- `NAHLA_META_ACCEPTANCE_WEBHOOK_ATTESTATION_ARTIFACT` + HMAC key (operator-held, for acceptance readiness only)

**Readiness terminology:**

| Signal | Meaning |
|--------|---------|
| `meta_config_present` | Required Meta env keys exist. **Does not** unlock real-channel execution. |
| `actual_provider_channel_ready` | Meta config **plus** signed external webhook attestation **plus** (Tenant 1 cutover) DB `whatsapp_connections` binding match. |

Report `channel_readiness_gaps` with **names only**. Never weaken signature verification to compensate.

### Pre-verification acceptance only — Tenant 1 temporary cutover

Until Business Verification unblocks Embedded Signup, **Tenant 1's existing direct-Meta
test channel** may be used as a **temporary, reversible, acceptance-only** cutover to
canonical staging. This must:

| Requirement | Detail |
|-------------|--------|
| Label | `acceptance_only_not_production` |
| Scope | `tenant_1_preverification_direct_meta_test_channel` |
| Snapshot before cutover | Meta webhook target, staging env secret fingerprints, Tenant 1 `whatsapp_connections` binding |
| Rollback after window | Restore all three snapshot components |
| Forbidden | Production unlock, runtime abstraction, permanent staging WABA |

```bash
# Documentation-only guidance (no mutations)
python -m scripts.operators.staging_acceptance_config_consolidation acceptance-cutover-guidance
```

**Do not** perform cutover, webhook changes, secret writes, or DB binding updates from
this governance PR or CI.

## Routing selection (documented only)

| Traffic | Route to |
|---------|----------|
| Meta Cloud API direct webhooks | Canonical `nahla-saas` `BACKEND_URL` → `/webhook/whatsapp` |
| Real-channel acceptance | Canonical `nahla-saas` (Meta direct only) |
| 360dialog (legacy) | Transition observability only — **excluded from readiness** |
| Legacy `nahla-saas-staging` | Decommission after acceptance signoff — **manual**, no auto-delete |

## CI

`backend/tests/test_staging_acceptance_config_consolidation_probe.py`

## Related

- ARCH-001 shadow: `docs/engineering/product-availability-truth-guard-shadow-runbook.md`
- Real-channel acceptance: `docs/engineering/real-channel-conversational-acceptance-runbook.md`
