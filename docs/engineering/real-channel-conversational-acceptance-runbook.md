# Real-channel conversational acceptance runbook (post-ARCH-001)

**Status:** Preparation only (default-off). Do not execute until ARCH-001 preprod
synthetic signoff v2 (`ARCH-001-PREPROD-SYNTHETIC-SIGNOFF-v2`) HMAC bundle is approved.
Post-approval canonical shadow during limited allowlisted canary remains a separate gate.

## Scope

Mandatory post-shadow real-channel conversational acceptance program:

1. **Tenant 1** — intensive synthetic/test-store acceptance (50 scenarios)
2. **Tenant 33** — limited real-store acceptance on private allowlisted numbers only
   (16 scenarios), **only after Tenant 1 passes**

Poll/CI success alone is insufficient. Every defect becomes Eval/regression/engineering
work. No auto-merge of fixes during the acceptance window.

## Architecture trace (real channel path)

**Closed target path:** Meta Cloud API direct only (`meta_cloud_api_direct`).
**Target onboarding:** per-merchant Meta Embedded Signup → merchant-owned WABA, Phone
Number ID, and Access Token. **Meta Business Verification** is the current external blocker.

360dialog is **legacy/transition-only** and must not satisfy acceptance readiness.

```
Meta Cloud API direct → POST /webhook/whatsapp
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
  → Meta Graph API → customer device
```

**Direct-code probes** (`merchant_assistant_constitution_smoke.py`, internal
`_handle_merchant_message`) bypass HTTP webhook ingress. They are useful for
diagnostics but **must not** be labeled real-channel E2E evidence.

**Critical boundary:** a locally generated signed HTTP POST to
`/webhook/whatsapp*` is also **not** actual-channel E2E. It proves only ingress,
signature, normalization, and routing integration. Launch acceptance requires:

```
private allowlisted test phone/device
  → Meta Cloud API direct inbound delivery
  → Nahla webhook + AI
  → Meta outbound provider ID/status
  → receipt visible on the same private test device
```

The runner therefore classifies an otherwise valid-looking persisted webhook
row as `direct_signed_webhook_integration_probe` until a named reviewer records
the real-device send and receipt attestation. Database fixtures and direct code
markers can never be upgraded.

## Preconditions (readiness)

| Gate | Requirement |
|------|-------------|
| ARCH-001 preprod signoff | HMAC-signed `product_availability_preprod_synthetic_signoff_v2` artifact (`traffic_claim=synthetic_probes_only`) |
| Staging identity | `RAILWAY_PROJECT_NAME=desirable-growth`, `RAILWAY_ENVIRONMENT_NAME=staging` |
| Deploy pin | `NAHLA_REAL_CHANNEL_ACCEPTANCE_PINNED_REVISION=<SHA>` |
| Store AI mode | `store_ai_mode=test` on target tenant |
| Allowlist | Test phones in `ai_test_allowed_numbers` only |
| Master gate | `NAHLA_REAL_CHANNEL_ACCEPTANCE_ENABLED` unset or not truthy (default-off) |
| Execution confirm | `NAHLA_REAL_CHANNEL_ACCEPTANCE_CONFIRM=true` (human) |
| ARCH-001 signoff env | `NAHLA_ARCH001_SHADOW_SIGNOFF_CONFIRM=true` |
| Tenant 1 pass (T33 only) | HMAC-signed Tenant 1 PASS artifact with teardown verified |
| Deployment identity | Exact `RAILWAY_DEPLOYMENT_ID` plus pinned revision |
| Evidence identity | Keyed hashes via `NAHLA_REAL_CHANNEL_ACCEPTANCE_EVIDENCE_HMAC_KEY` |

## Credential / test-number env refs (names only)

| Env var | Purpose |
|---------|---------|
| `NAHLA_REAL_CHANNEL_ACCEPTANCE_ENABLED` | Master execution gate (default off) |
| `NAHLA_REAL_CHANNEL_ACCEPTANCE_CONFIRM` | Human execution confirmation |
| `NAHLA_ARCH001_PREPROD_SYNTHETIC_SIGNOFF_V2_ARTIFACT` | Signed preprod v2 bundle path |
| `NAHLA_ARCH001_PREPROD_SYNTHETIC_SIGNOFF_V2_HMAC_KEY` | HMAC key for v2 bundle verification (min 32 bytes) |
| `NAHLA_ARCH001_PREPROD_PINNED_REVISION` | Pinned SHA bound at verification |
| `NAHLA_ARCH001_PREPROD_EXPECTED_MANIFEST_DIGEST` | Runtime manifest digest bound at verification |
| `NAHLA_ARCH001_PREPROD_ISOLATED_SERVICE_NAME` | Isolated shadow service name |
| `NAHLA_ARCH001_PREPROD_ISOLATED_SERVICE_ID` | Isolated shadow service UUID |
| `NAHLA_ARCH001_PREPROD_ISOLATED_DEPLOYMENT_ID` | Post-redeploy deployment UUID |
| `NAHLA_ARCH001_SHADOW_SIGNOFF_CONFIRM` | **Deprecated alone** — not sufficient without v2 artifact |
| `NAHLA_REAL_CHANNEL_ACCEPTANCE_TENANT_1_PASS_ARTIFACT` | Signed Tenant 1 PASS artifact path |
| `NAHLA_REAL_CHANNEL_ACCEPTANCE_PINNED_REVISION` | Deploy revision pin |
| `NAHLA_REAL_CHANNEL_ACCEPTANCE_EVIDENCE_HMAC_KEY` | Evidence signing/keyed-hash secret |
| `NAHLA_REAL_CHANNEL_ACCEPTANCE_REVIEWER_ID` | Secret reviewer identity reference |
| `NAHLA_REAL_CHANNEL_ACCEPTANCE_SESSION_DIR` | Optional local secure session directory |
| `RAILWAY_DEPLOYMENT_ID` | Exact deployed instance identity |
| `NAHLA_REAL_CHANNEL_ACCEPTANCE_TENANT_1_PHONE` | Tenant 1 test phone (secret) |
| `NAHLA_REAL_CHANNEL_ACCEPTANCE_TENANT_33_PHONE` | Tenant 33 test phone (secret) |
| `NAHLA_REAL_CHANNEL_ACCEPTANCE_ALLOWLIST_PHONES` | Comma-separated allowlist (secrets) |
| `NAHLA_REAL_CHANNEL_ACCEPTANCE_TENANT_1_CONVERSATION_ID` | Optional conversation pin |
| `NAHLA_REAL_CHANNEL_ACCEPTANCE_TENANT_33_CONVERSATION_ID` | Optional conversation pin |
| `DATABASE_URL` | Staging DB (postgres-staging.railway.internal) |
| `BACKEND_URL` | Public webhook target (**required for Meta readiness**) |
| `META_APP_SECRET` | Meta webhook signature verification (**required**) |
| `WHATSAPP_API_URL` | Graph API base |
| `WHATSAPP_TOKEN` | Outbound send token (**required**, secret) |
| `WHATSAPP_VERIFY_TOKEN` | Webhook verification (**required**) |
| `D360_API_BASE_URL` | 360dialog — **legacy observability only, not readiness** |
| `D360_PARTNER_HUB_BASE` | Partner hub (legacy observability) |
| `D360_PARTNER_API_KEY` | Partner hub auth (legacy observability, secret) |
| `D360_PARTNER_ID` | Partner ID (legacy observability) |
| `NAHLA_META_ACCEPTANCE_WEBHOOK_ATTESTATION_ARTIFACT` | Signed bounded external webhook attestation (path or inline JSON) |
| `NAHLA_META_ACCEPTANCE_WEBHOOK_ATTESTATION_HMAC_KEY` | HMAC key for attestation verification (min 32 bytes, operator-held) |

**Meta readiness terminology (fail-closed):**

| Signal | Meaning |
|--------|---------|
| `meta_config_present` | Required Meta env keys exist (`META_APP_SECRET`, `WHATSAPP_TOKEN`, `WHATSAPP_VERIFY_TOKEN`, `BACKEND_URL`). **Does not** unlock real-channel execution. |
| `actual_provider_channel_ready` | Meta config **plus** signed operator webhook observation attestation **plus** tenant-specific DB `whatsapp_connections` binding match. **Not** post-send provider proof. |
| `operator_attested_channel_ready` | Same as above — preflight readiness only (`channel_evidence_class=operator_observed_meta_webhook`). |

**Meta readiness required env:** `META_APP_SECRET`, `WHATSAPP_TOKEN`, `WHATSAPP_VERIFY_TOKEN`,
`BACKEND_URL`. D360 absence is never a blocker. D360-only environments **fail** readiness.

### Pre-verification acceptance — Tenant 1 temporary cutover (acceptance-only)

Until Business Verification unblocks Embedded Signup, Tenant 1's existing direct-Meta test
channel may be used as a **temporary reversible cutover** to canonical staging. Before any
cutover, snapshot and after acceptance restore:

1. Meta webhook target
2. Staging env secret fingerprints
3. Tenant 1 `whatsapp_connections` DB binding

Label: `acceptance_only_not_production`. Scope:
`tenant_1_preverification_direct_meta_test_channel`. **Must not** unlock production or
become a runtime abstraction. See
`staging-acceptance-config-consolidation-runbook.md` and:

```bash
python -m scripts.operators.staging_acceptance_config_consolidation acceptance-cutover-guidance
```

**Do not** perform cutover from this PR or CI.

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

Meta Cloud API direct credentials must be present. D360-only environments **BLOCK**.
D360 vars may be reported for legacy observability but never satisfy readiness.

**Route readiness is never inferred from constants.** Channel preflight requires a
bounded **operator observation attestation** (HMAC-signed, not provider-cryptographic
proof) with closed `observation_source`, `observer_id`, `observed_at_utc`,
`observation_evidence_digest`, observed callback route, rollback snapshot evidence
(tied to consolidation snapshot schema), plus deployment/revision binding.

For every acceptance tenant, session start also requires read-only DB evidence of an
enabled direct-Meta `whatsapp_connections` row with `sending_enabled=true`, non-empty
WABA + Phone Number ID (HMAC fingerprints only), tenant match, and consistency with
attestation fingerprints. D360 provider rows are rejected.

```bash
python -m scripts.operators.real_channel_conversational_acceptance preflight
```

**BLOCK** if `operator_attested_channel_ready` is false. `meta_config_present` alone is
insufficient. Post-send `actual_provider_channel` evidence is a separate gate after
real device/provider IDs — never conflated with preflight attestation.

## Execution (after ARCH-001 ends — not now)

1. Set secret env references, gates, exact revision, and deployment ID.
2. Run channel health probes; any non-zero result is **BLOCK**.
3. Start the Tenant 1 session (read-only DB snapshot and event/usage cursors).
4. For each scenario, obtain instructions, send from the real private WhatsApp
   test device, observe DB/provider evidence, and record device + human review.
5. Complete all scenarios and teardown. Teardown requires exact config
   fingerprint equality and emits a signed Tenant 1 PASS artifact only when all
   50 scenarios passed in a full `phase_acceptance` session. A
   `single_scenario_retest` session emits only bounded per-scenario evidence and
   never unlocks Tenant 33.
6. Point `NAHLA_REAL_CHANNEL_ACCEPTANCE_TENANT_1_PASS_ARTIFACT` at that signed
   artifact, then start Tenant 33.

### Session commands/state machine

```bash
# STARTED: snapshots exact config fingerprint and DB/event/AI-usage cursors.
python -m scripts.operators.real_channel_acceptance_session start-session --tenant 1

# Single-scenario retest (fail-closed): arms only the named closed manifest scenario.
# Does not mint Tenant 1 PASS or unlock Tenant 33.
python -m scripts.operators.real_channel_acceptance_session start-session --tenant 1 \
  --scenario-id t1_catalog_dress_ambiguous

# STARTED → AWAITING_DEVICE_SEND: prints the test input and manual/device boundary.
python -m scripts.operators.real_channel_acceptance_session next-scenario --session-id <SESSION_ID>

# Human or authorized existing test-device automation sends from WhatsApp now.
# No runner command sends a message.

# AWAITING_DEVICE_SEND → OBSERVED: polls persisted inbound/outbound/AI evidence.
python -m scripts.operators.real_channel_acceptance_session observe --session-id <SESSION_ID>
# `poll` is an alias for repeated one-shot observation while no inbound exists.
python -m scripts.operators.real_channel_acceptance_session poll --session-id <SESSION_ID>

# Attest the real device boundary; reviewer identity comes from secret env.
python -m scripts.operators.real_channel_acceptance_session record-device-attestation \
  --session-id <SESSION_ID> --provider 360dialog \
  --sent-from-private-device yes --outbound-received yes

# Separate naturalness/context/audio/truthfulness review.
python -m scripts.operators.real_channel_acceptance_session record-human-assessment \
  --session-id <SESSION_ID> --naturalness pass \
  --context-continuity pass --audio-quality not_applicable \
  --operational-truthfulness pass

# HUMAN_ASSESSED → SCENARIO_COMPLETED
python -m scripts.operators.real_channel_acceptance_session complete-scenario --session-id <SESSION_ID>
python -m scripts.operators.real_channel_acceptance_session session-status --session-id <SESSION_ID>
python -m scripts.operators.real_channel_acceptance_session emit-defect-bundle --session-id <SESSION_ID>
python -m scripts.operators.real_channel_acceptance_session teardown --session-id <SESSION_ID>
```

State sequence:

```
started → awaiting_device_send → observed → human_assessed
        → scenario_completed → ... → completed → torn_down
```

No Web/WhatsApp automation is introduced. If an authorized test-device
integration is not already available, sending and receipt confirmation remain
manual and reviewer-attested.

### Evidence channel enum

| Value | Launch acceptance |
|-------|-------------------|
| `actual_provider_channel` | Eligible, only after provider-shaped evidence and device attestation |
| `direct_signed_webhook_integration_probe` | Never |
| `direct_code_probe` | Never |

`merchant_assistant_constitution_smoke.py` is explicitly non-use for this
program. Its hardcoded phones were removed; it now reads secret env refs and
labels output `direct_code_probe`. Its separate refs are
`NAHLA_CONSTITUTION_SMOKE_PHONE`,
`NAHLA_CONSTITUTION_SMOKE_ALLOWED_PHONES`, and
`NAHLA_CONSTITUTION_SMOKE_HASH_KEY`.

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
- New inbound event is after the session cursor and opening time
- Sender HMAC matches the secret allowlisted test phone
- Inbound and outbound provider IDs are present and reject synthetic markers
- Direct signed HTTP or DB-only evidence cannot produce actual-channel PASS

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
# Unset NAHLA_REAL_CHANNEL_ACCEPTANCE_ENABLED and
# NAHLA_REAL_CHANNEL_ACCEPTANCE_CONFIRM before session teardown.
python -m scripts.operators.real_channel_acceptance_session teardown --session-id <SESSION_ID>
```

Restore tenant `ai_settings` from config snapshot. Verify allowlist unchanged.
Archive evidence to `docs/engineering/staging-evidence/`.

The runner performs no tenant mutations. Teardown compares the complete current
`ai_settings` HMAC with the start snapshot and fails with `config_drift` if
anything changed. The operator must restore the external configuration exactly
before teardown can pass; no raw allowlist is written to disk.

## Artifacts

| Artifact | Path |
|----------|------|
| Scenario manifest | `docs/engineering/real-channel-acceptance-scenario-manifest.json` |
| Evidence schema | `docs/engineering/real-channel-acceptance-evidence-schema.md` |
| Operator contract | `scripts/operators/real_channel_conversational_acceptance_contract.py` |
| Operator | `scripts/operators/real_channel_conversational_acceptance.py` |
| Session runner | `scripts/operators/real_channel_acceptance_session.py` |
| CI probe tests | `backend/tests/test_real_channel_conversational_acceptance_probe.py` |
| Session/PG refusal tests | `backend/tests/test_real_channel_acceptance_session.py`, `backend/tests/test_real_channel_acceptance_session_pg.py` |

`run_ai_commerce_confidence_suite.py` was evaluated but not added as a second
full CI invocation: its constituent tests already run in the repository-wide
unit job. The launch-critical bounded subset is the acceptance contract/session
tests above, including PostgreSQL fixture refusal.

## GO / BLOCK

| Condition | Recommendation |
|-----------|----------------|
| Now (ARCH-001 window active) | **BLOCK** execution |
| After ARCH-001 signoff + preflight green | **GO** Tenant 1 |
| After Tenant 1 pass | **GO** Tenant 33 limited |
| Provider credentials absent | **BLOCK** (no simulation) |
| `store_ai_mode` not `test` | **BLOCK** |
| Phone not allowlisted | **BLOCK** |
