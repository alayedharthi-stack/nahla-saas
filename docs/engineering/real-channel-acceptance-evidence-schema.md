# Real-channel acceptance evidence schema (v2)

Schema version: `real_channel_acceptance_evidence_v2`

## Purpose

Structured, sanitized evidence for post-ARCH-001-shadow real-channel conversational
acceptance. No raw phone numbers, tokens, webhook secrets, or customer PII.

## Record location

- Active session state: `.nahla-acceptance-sessions/<session-id>.json` (gitignored)
- Sanitized archived session evidence: `docs/engineering/staging-evidence/real-channel-acceptance-<session-id>.json`
- Poll log: `docs/engineering/staging-evidence/real-channel-acceptance-poll.jsonl`
- Defect bundles: `docs/engineering/staging-evidence/defect-bundles/<bundle-id>.json`

## Evidence record shape

| Field | Type | Description |
|-------|------|-------------|
| `evidence_schema_version` | string | `real_channel_acceptance_evidence_v2` |
| `scenario_id` | string | Closed manifest scenario ID |
| `correlation_id` | string | Per-turn/session correlation UUID |
| `evidence_channel` | enum | See closed evidence-channel enum below |
| `recorded_at_utc` | string | ISO-8601 UTC |
| `pass_fail` | string | `pass`, `fail`, `blocked`, `skipped` |
| `provenance_chain` | object | `compose_source`, `chosen_path`, `response_mode`, transforms |
| `state_evidence` | object | Deterministic state assertions (orders, routing, guards) |
| `test_phone_hmac` | string | Keyed HMAC; no raw phone |
| `device_attestation` | object | Reviewer-HMAC, provider, sent/received booleans |

## Closed evidence-channel enum

| Value | Meaning | Can satisfy launch acceptance? |
|-------|---------|--------------------------------|
| `actual_provider_channel` | Private test device → provider → Nahla → provider → same device, with machine evidence and reviewer attestation | Yes |
| `direct_signed_webhook_integration_probe` | Locally generated signed HTTP POST or un-attested webhook-shaped DB evidence | **No** |
| `direct_code_probe` | Internal handler/playground/fake harness | **No** |

A provider-shaped `wamid`, `message_origin=live_webhook`, and valid timestamps
are necessary but not sufficient: a party holding the webhook secret can
generate them locally. The runner initially classifies all such rows as
`direct_signed_webhook_integration_probe`; only real-device send and receipt
attestation may upgrade an otherwise eligible event. Explicit fixture,
synthetic, direct-code, stale, replayed, wrong-tenant, wrong-phone, or missing-ID
events are permanently rejected.

## Session evidence

Session state is local under `.nahla-acceptance-sessions/` (or
`NAHLA_REAL_CHANNEL_ACCEPTANCE_SESSION_DIR`) and excluded from git. It contains:

- exact deployment revision and keyed deployment-ID fingerprint
- tenant ID and keyed test-phone fingerprint
- start `MessageEvent` and `AIUsageEvent` cursors
- complete config fingerprint plus sanitized config snapshot
- scenario state machine, machine verdict, device attestation, human rubric
- provider/media ID HMACs, budgets, and provenance metadata

Raw phone numbers, message bodies, tokens, signatures, and customer PII are not
stored.

## Provenance chain (mandatory)

Trace: **decision → facts → compose → guards → sanitizer → dedup → wire**

Required fields per `AGENTS.md`:

- `compose_source`
- `response_mode`
- `chosen_path`
- `llm_candidate_present`
- `final_text_transformed`
- `final_transform_reasons`
- `fallback_reason` (when applicable)
- `fallback_action_type` (when applicable)

## Defect bundle shape

| Field | Description |
|-------|-------------|
| `bundle_id` | `{scenario_id}-{correlation_prefix}` |
| `failure_class` | Taxonomy-aligned failure code |
| `classification.severity` | `p0` / `p1` |
| `classification.auto_merge_fixes` | Always `false` during acceptance |
| `sanitized_evidence` | Hashes and structured metadata only |
| `defect_workflow` | Eval → regression → engineering via normal CI |

## Redaction rules

1. Phone numbers → keyed `hmac_identifier` only
2. Tokens/secrets → `present|absent` in preflight; never in evidence
3. Customer names → hash or omit
4. Outbound body → excerpt hash; no exact Arabic prose in archived evidence
5. Config snapshots → `ai_test_allowed_numbers_hash`, not raw list
6. Provider/media IDs → keyed HMAC in archived evidence
7. Reviewer identity → keyed HMAC from secret env reference
