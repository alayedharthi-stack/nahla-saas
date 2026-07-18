# Real-channel conversational acceptance runbook (post-ARCH-001)

**Status:** Preparation only (default-off). Do not execute until ARCH-001 48h shadow
window completes and receives signoff.

## Scope

Mandatory post-shadow real-channel conversational acceptance program:

1. **Tenant 1** — intensive synthetic/test-store acceptance (49 scenarios)
2. **Tenant 33** — limited real-store acceptance on private allowlisted numbers only
   (16 scenarios), **only after Tenant 1 passes**

Poll/CI success alone is insufficient. Every defect becomes Eval/regression/engineering
work. No auto-merge of fixes during the acceptance window.

## Architecture trace (real channel path)

```
Meta/360dialog → POST /webhook/whatsapp[/360dialog]
  → signature/replay verification (webhook_security)
  → idempotency/dedup guards
  → message persistence (message_events)
  → media/audio normalizer
  → tenant routing (whatsapp_connections.phone_number_id)
  → store_ai_mode / allowlist gate (ai_disabled_gate)
  → pause/handoff/blocklist/subscription guards
  → brain.process / legacy webhook path
  → compose (persona_llm) + guards + sanitizer + dedup
  → outbound adapter (provider_send_message)
  → provider API → customer device
```

**Direct-code probes** (`merchant_assistant_constitution_smoke.py`, internal
`_handle_merchant_message`) bypass HTTP webhook ingress. They are useful for
diagnostics but **must not** be labeled real-channel E2E evidence.

## Preconditions (readiness)

| Gate | Requirement |
|------|-------------|
| ARCH-001 shadow | 48h synthetic signoff artifact approved |
| Staging identity | `RAILWAY_PROJECT_NAME=desirable-growth`, `RAILWAY_ENVIRONMENT_NAME=staging` |
| Deploy pin | `NAHLA_REAL_CHANNEL_ACCEPTANCE_PINNED_REVISION=<SHA>` |
| Store AI mode | `store_ai_mode=test` on target tenant |
| Allowlist | Test phones in `ai_test_allowed_numbers` only |
| Master gate | `NAHLA_REAL_CHANNEL_ACCEPTANCE_ENABLED` unset or not truthy (default-off) |
| Execution confirm | `NAHLA_REAL_CHANNEL_ACCEPTANCE_CONFIRM=true` (human) |
| ARCH-001 signoff env | `NAHLA_ARCH001_SHADOW_SIGNOFF_CONFIRM=true` |
| Tenant 1 pass (T33 only) | `NAHLA_REAL_CHANNEL_ACCEPTANCE_TENANT_1_PASS_CONFIRM=true` |

## Credential / test-number env refs (names only)

| Env var | Purpose |
|---------|---------|
| `NAHLA_REAL_CHANNEL_ACCEPTANCE_ENABLED` | Master execution gate (default off) |
| `NAHLA_REAL_CHANNEL_ACCEPTANCE_CONFIRM` | Human execution confirmation |
| `NAHLA_ARCH001_SHADOW_SIGNOFF_CONFIRM` | ARCH-001 shadow signoff attestation |
| `NAHLA_REAL_CHANNEL_ACCEPTANCE_TENANT_1_PASS_CONFIRM` | Tenant 1 pass gate for Tenant 33 |
| `NAHLA_REAL_CHANNEL_ACCEPTANCE_PINNED_REVISION` | Deploy revision pin |
| `NAHLA_REAL_CHANNEL_ACCEPTANCE_TENANT_1_PHONE` | Tenant 1 test phone (secret) |
| `NAHLA_REAL_CHANNEL_ACCEPTANCE_TENANT_33_PHONE` | Tenant 33 test phone (secret) |
| `NAHLA_REAL_CHANNEL_ACCEPTANCE_ALLOWLIST_PHONES` | Comma-separated allowlist (secrets) |
| `NAHLA_REAL_CHANNEL_ACCEPTANCE_TENANT_1_CONVERSATION_ID` | Optional conversation pin |
| `NAHLA_REAL_CHANNEL_ACCEPTANCE_TENANT_33_CONVERSATION_ID` | Optional conversation pin |
| `DATABASE_URL` | Staging DB (postgres-staging.railway.internal) |
| `BACKEND_URL` | Public webhook target |
| `D360_API_BASE_URL` | 360dialog channel API |
| `D360_PARTNER_HUB_BASE` | Partner hub (optional) |
| `D360_PARTNER_API_KEY` | Partner hub auth (secret) |
| `D360_PARTNER_ID` | Partner ID |
| `META_APP_SECRET` | Meta webhook signature verification |
| `WHATSAPP_API_URL` | Graph API base |
| `WHATSAPP_TOKEN` | Outbound send token (secret) |
| `WHATSAPP_VERIFY_TOKEN` | Webhook verification |

**Never commit values.** Report `present|absent` in preflight only.

## Operator commands (preparation — safe now)

```bash
# Default-off gate (CI-safe)
python -m scripts.operators.real_channel_conversational_acceptance default-off

# Readiness preflight (no messages)
python -m scripts.operators.real_channel_conversational_acceptance preflight

# Full preflight + revision attestation + scenario plans
python -m scripts.operators.real_channel_conversational_acceptance full-preflight <PINNED_SHA>

# Validate closed scenario manifest
python -m scripts.operators.real_channel_conversational_acceptance manifest-validate

# Dry-run scenario plans (no execution)
python -m scripts.operators.real_channel_conversational_acceptance tenant-1-plan
python -m scripts.operators.real_channel_conversational_acceptance tenant-33-plan

# Teardown checklist
python -m scripts.operators.real_channel_conversational_acceptance teardown

# Defect bundle template
python -m scripts.operators.real_channel_conversational_acceptance defect-bundle template t1_faq_hours
```

## Channel health preflight (Phase 0 — before any messages)

```bash
python scripts/probe_d360_forwarding.py --tenant 1
python scripts/probe_d360_forwarding.py --tenant 33
```

**BLOCK** if provider credentials absent. Do not simulate and call it real E2E.

## Execution (after ARCH-001 ends — not now)

1. Set env gates and pinned revision on staging operator shell only.
2. Capture config snapshot (`config_snapshot` phase output).
3. Run channel health probes; exit non-zero → BLOCK.
4. Execute Tenant 1 scenarios sequentially; record evidence per scenario.
5. On Tenant 1 pass, set `NAHLA_REAL_CHANNEL_ACCEPTANCE_TENANT_1_PASS_CONFIRM=true`.
6. Execute Tenant 33 limited scenarios.
7. Archive evidence; run teardown.

Real inbound must use signed HTTP POST to `/webhook/whatsapp` or
`/webhook/whatsapp/360dialog` — not internal handler shortcuts.

## Rate / cost caps (per session)

| Cap | Value |
|-----|-------|
| Max scenarios | 60 |
| Max inbound messages | 120 |
| Max outbound provider calls | 120 |
| Max LLM calls | 240 |
| Max session cost | $25 USD |
| Default latency budget / scenario | 30s |

## Pass/fail rubric

### Automated (provider sandbox / state assertions)

- Expected deterministic state/evidence met
- No prohibited operational claims without evidence
- Provenance fields present in message metadata
- Latency and LLM/tool calls within cap
- Cross-tenant isolation holds

### Manual (human assessment)

- Naturalness of Arabic/English/mixed responses
- Context continuity across interruptions
- Audio transcription quality / graceful degradation
- Social warmth without template rigidity

**Do not assert exact Arabic prose** except protocol/legal/security text.

## Defect workflow

1. Operator emits sanitized defect bundle (`defect-bundle` command).
2. File issue with `bundle_id` and `scenario_id`.
3. Map to `eval_regression_mapping` in scenario manifest.
4. Fix via normal PR + CI + constitution-compliance.
5. **No auto-merge** during acceptance execution window.

## Teardown

```bash
python -m scripts.operators.real_channel_conversational_acceptance teardown
python -m scripts.operators.product_availability_truth_guard_shadow_observation teardown
```

Restore tenant `ai_settings` from config snapshot. Verify allowlist unchanged.
Archive evidence to `docs/engineering/staging-evidence/`.

## Artifacts

| Artifact | Path |
|----------|------|
| Scenario manifest | `docs/engineering/real-channel-acceptance-scenario-manifest.json` |
| Evidence schema | `docs/engineering/real-channel-acceptance-evidence-schema.md` |
| Operator contract | `scripts/operators/real_channel_conversational_acceptance_contract.py` |
| Operator | `scripts/operators/real_channel_conversational_acceptance.py` |
| CI probe tests | `backend/tests/test_real_channel_conversational_acceptance_probe.py` |

## GO / BLOCK

| Condition | Recommendation |
|-----------|----------------|
| Now (ARCH-001 window active) | **BLOCK** execution |
| After ARCH-001 signoff + preflight green | **GO** Tenant 1 |
| After Tenant 1 pass | **GO** Tenant 33 limited |
| Provider credentials absent | **BLOCK** (no simulation) |
| `store_ai_mode` not `test` | **BLOCK** |
| Phone not allowlisted | **BLOCK** |
