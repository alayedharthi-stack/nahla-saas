# Real-channel acceptance evidence schema (v1)

Schema version: `real_channel_acceptance_evidence_v1`

## Purpose

Structured, sanitized evidence for post-ARCH-001-shadow real-channel conversational
acceptance. No raw phone numbers, tokens, webhook secrets, or customer PII.

## Record location

- Session evidence: `docs/engineering/staging-evidence/real-channel-acceptance-<session-id>.json`
- Poll log: `docs/engineering/staging-evidence/real-channel-acceptance-poll.jsonl`
- Defect bundles: `docs/engineering/staging-evidence/defect-bundles/<bundle-id>.json`

## Evidence record shape

| Field | Type | Description |
|-------|------|-------------|
| `evidence_schema_version` | string | `real_channel_acceptance_evidence_v1` |
| `scenario_id` | string | Closed manifest scenario ID |
| `correlation_id` | string | Per-turn/session correlation UUID |
| `execution_path` | string | `real_channel_webhook` or `direct_code_probe` |
| `execution_path_label` | string | Human label; direct-code probes are **not** real-channel |
| `recorded_at_utc` | string | ISO-8601 UTC |
| `pass_fail` | string | `pass`, `fail`, `blocked`, `skipped` |
| `provenance_chain` | object | `compose_source`, `chosen_path`, `response_mode`, transforms |
| `state_evidence` | object | Deterministic state assertions (orders, routing, guards) |
| `customer_text_excerpt_hash` | string | `sha256:<16>` fingerprint only |

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

1. Phone numbers → `mask_phone_tail` or `hash_identifier` only
2. Tokens/secrets → `present|absent` in preflight; never in evidence
3. Customer names → hash or omit
4. Outbound body → excerpt hash; no exact Arabic prose in archived evidence
5. Config snapshots → `ai_test_allowed_numbers_hash`, not raw list
