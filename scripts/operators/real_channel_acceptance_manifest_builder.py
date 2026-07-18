"""Build the closed real-channel acceptance scenario manifest (deterministic)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scripts.operators.real_channel_conversational_acceptance_contract import (
    DEFAULT_SCENARIO_LATENCY_BUDGET_MS,
    DEFAULT_SCENARIO_MAX_LLM_CALLS,
    DEFAULT_SCENARIO_MAX_TOOL_CALLS,
    EXECUTION_PATH_DIRECT_CODE_PROBE,
    EXECUTION_PATH_REAL_CHANNEL_WEBHOOK,
    MANIFEST_SCHEMA_VERSION,
    PHASE_TENANT_1_INTENSIVE,
    PHASE_TENANT_33_LIMITED,
    PHASE_TENANT_48_SALLA_MINIMAL,
    PHASE_EXPECTED_SCENARIO_COUNTS,
    SCENARIO_TAXONOMY,
    TENANT_1_INTENSIVE,
    TENANT_33_LIMITED,
    TENANT_48_SALLA_MINIMAL,
)


def _base_rubric(*, automated: bool) -> dict[str, Any]:
    return {
        "pass_requires": [
            "expected_state_evidence_met",
            "no_prohibited_claims",
            "provenance_fields_present",
            "latency_within_budget",
            "llm_tool_calls_within_cap",
        ],
        "fail_triggers": [
            "prohibited_operational_claim",
            "missing_evidence_for_claim",
            "cross_tenant_leak",
            "exact_prose_assertion_violation",
            "provider_send_without_allowlist",
        ],
        "human_assessment_required": not automated,
    }


def _scenario(
    *,
    scenario_id: str,
    phase: str,
    taxonomy: str,
    tenant_id: int,
    execution_path: str,
    inbound: dict[str, Any],
    expected_state: dict[str, Any],
    prohibited_claims: list[str],
    automation_class: str,
    eval_mapping: list[str],
    max_llm_calls: int = DEFAULT_SCENARIO_MAX_LLM_CALLS,
    max_tool_calls: int = DEFAULT_SCENARIO_MAX_TOOL_CALLS,
    latency_budget_ms: int = DEFAULT_SCENARIO_LATENCY_BUDGET_MS,
    preconditions: dict[str, Any] | None = None,
    cleanup: str = "restore_scenario_state_then_verify_against_session_snapshot",
) -> dict[str, Any]:
    expects_outbound = taxonomy not in {"pause_blocklist", "subscription_guard"}
    phone_env_by_tenant = {
        TENANT_1_INTENSIVE: "NAHLA_REAL_CHANNEL_ACCEPTANCE_TENANT_1_PHONE",
        TENANT_33_LIMITED: "NAHLA_REAL_CHANNEL_ACCEPTANCE_TENANT_33_PHONE",
        TENANT_48_SALLA_MINIMAL: "NAHLA_REAL_CHANNEL_ACCEPTANCE_TENANT_48_PHONE",
    }
    return {
        "scenario_id": scenario_id,
        "phase": phase,
        "taxonomy": taxonomy,
        "tenant_id": tenant_id,
        "execution_path": execution_path,
        "preconditions": preconditions
        or {
            "store_ai_mode": "test",
            "store_ai_enabled": True,
            "phone_env_ref": phone_env_by_tenant[tenant_id],
            "arch001_shadow_signoff": True,
            "tenant_1_pass_required": phase == PHASE_TENANT_33_LIMITED,
        },
        "inbound": inbound,
        "expected_state": expected_state,
        "allowed_conversational_variability": [
            "greeting_wording",
            "apology_tone",
            "transition_phrasing",
            "concise_vs_verbose",
        ],
        "prohibited_claims": prohibited_claims,
        "max_llm_calls": max_llm_calls,
        "max_tool_calls": max_tool_calls,
        "latency_budget_ms": latency_budget_ms,
        "outbound_evidence": [
            "message_events_outbound_row",
            "provider_send_metadata",
            "wa_message_id_correlation",
            "compose_provenance_metadata",
            "decision_trace_present",
        ],
        "cleanup": cleanup,
        "eval_regression_mapping": eval_mapping,
        "automation_class": automation_class,
        "pass_fail_rubric": _base_rubric(
            automated=automation_class.startswith("automated")
        ),
        "device_action": {
            "sender": "private_allowlisted_test_device",
            "send_type": str(inbound.get("type") or "text"),
            "input": inbound,
            "automation_policy": (
                "manual_unless_authorized_existing_test_device_integration"
            ),
            "raw_input_archival": "hash_or_redact",
        },
        "channel_evidence_required": {
            "evidence_channel": "actual_provider_channel",
            "inbound_provider_message_id": True,
            "live_webhook_origin": True,
            "sender_hmac_match": True,
            "outbound_provider_message_id": expects_outbound,
            "test_device_receipt_attestation": expects_outbound,
            "outbound_expected": expects_outbound,
            "reject_direct_signed_webhook": True,
            "reject_direct_code_probe": True,
            "reject_manual_db_insert": True,
        },
    }


def _t1_scenarios() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    generic_store = "متجر تجريبي عام"
    product_a = "حذاء رياضي أبيض"
    product_b = "قميص قطني أزرق"
    product_c = "عطر ورد 100ml"

    specs: list[tuple[str, str, dict[str, Any], dict[str, Any], list[str], str, list[str]]] = [
        (
            "t1_faq_hours",
            "general_inquiry_faq",
            {"channel": "whatsapp", "type": "text", "body": "ما هي ساعات العمل؟"},
            {"routing": "faq_or_kb", "order_created": False},
            ["shipped_without_evidence", "payment_confirmed_without_evidence"],
            "hybrid",
            ["backend/tests/test_ai_playground_regression_scenarios.py"],
        ),
        (
            "t1_faq_shipping_policy",
            "general_inquiry_faq",
            {"channel": "whatsapp", "type": "text", "body": "كيف الشحن للرياض؟"},
            {"routing": "faq_or_kb", "shipping_facts_grounded": True},
            ["free_shipping_guarantee_without_policy"],
            "hybrid",
            ["backend/tests/test_ai_playground_regression_scenarios.py"],
        ),
        (
            "t1_catalog_search",
            "catalog_search_availability",
            {"channel": "whatsapp", "type": "text", "body": f"هل متوفر {product_a}؟"},
            {"catalog_lookup": True, "availability_truth_guard_observed": True},
            ["in_stock_without_catalog_evidence"],
            "automated_state_assertions",
            ["backend/tests/test_product_availability_truth_guard.py"],
        ),
        (
            "t1_catalog_browse",
            "catalog_search_availability",
            {"channel": "whatsapp", "type": "text", "body": "أرسل المنتجات المتوفرة"},
            {"catalog_surface": True},
            ["price_without_catalog_evidence"],
            "hybrid",
            ["backend/tests/test_ai_commerce_scenario_runner.py"],
        ),
        (
            "t1_catalog_variant",
            "catalog_search_availability",
            {"channel": "whatsapp", "type": "text", "body": f"عندكم {product_c} بأحجام مختلفة؟"},
            {"variant_resolution": True},
            ["variant_available_without_evidence"],
            "hybrid",
            ["backend/tests/test_product_availability_truth_guard.py"],
        ),
        (
            "t1_catalog_unavailable",
            "catalog_search_availability",
            {"channel": "whatsapp", "type": "text", "body": f"هل متوفر منتج غير موجود XYZ-999؟"},
            {"availability_negative_truth": True},
            ["available_claim_for_unknown_sku"],
            "automated_state_assertions",
            ["backend/tests/test_product_availability_truth_guard.py"],
        ),
        (
            "t1_order_multi_1",
            "multi_turn_order_construction",
            {"channel": "whatsapp", "type": "text", "body": f"أبي {product_b} مقاس M"},
            {"order_flow_active": True, "draft_or_pending": True},
            ["order_confirmed_without_customer_confirmation"],
            "hybrid",
            ["backend/tests/test_ai_commerce_scenario_runner.py"],
        ),
        (
            "t1_order_multi_2",
            "multi_turn_order_construction",
            {"channel": "whatsapp", "type": "text", "body": "الرياض حي النرجس"},
            {"address_captured_or_prompted": True},
            ["address_assumed_without_state"],
            "hybrid",
            ["backend/tests/test_ai_commerce_scenario_runner.py"],
        ),
        (
            "t1_order_multi_3",
            "multi_turn_order_construction",
            {"channel": "whatsapp", "type": "text", "body": "نعم أكد الطلب"},
            {"confirmation_gate": True},
            ["order_placed_without_confirmation_evidence"],
            "hybrid",
            ["backend/tests/test_ai_commerce_scenario_runner.py"],
        ),
        (
            "t1_order_multi_4",
            "multi_turn_order_construction",
            {"channel": "whatsapp", "type": "interactive", "body": "catalog_selection", "product": product_a},
            {"catalog_selection_handled": True},
            ["checkout_link_without_selection_evidence"],
            "automated_state_assertions",
            ["backend/tests/test_ai_commerce_scenario_runner.py"],
        ),
        (
            "t1_order_multi_5",
            "multi_turn_order_construction",
            {"channel": "whatsapp", "type": "text", "body": "كم الإجمالي؟"},
            {"pricing_grounded": True},
            ["discount_applied_without_coupon_evidence"],
            "hybrid",
            ["backend/tests/test_ai_commerce_scenario_runner.py"],
        ),
        (
            "t1_interrupt_topic",
            "interruption_topic_switch",
            {"channel": "whatsapp", "type": "text", "body": "بالمناسبة كيف الشحن؟"},
            {"topic_switch_handled": True, "prior_order_context_preserved": True},
            ["order_cancelled_without_request"],
            "manual_human_assessment",
            ["backend/tests/test_social_human_context_e2e.py"],
        ),
        (
            "t1_interrupt_social",
            "interruption_topic_switch",
            {"channel": "whatsapp", "type": "text", "body": "شكراً على المساعدة"},
            {"social_ack": True},
            ["forced_sales_pitch"],
            "manual_human_assessment",
            ["backend/tests/test_social_human_context_e2e.py"],
        ),
        (
            "t1_resume_order",
            "resume_after_interruption",
            {"channel": "whatsapp", "type": "text", "body": "نكمل الطلب"},
            {"order_flow_resumed": True},
            ["new_order_started_without_intent"],
            "hybrid",
            ["backend/tests/test_ai_commerce_scenario_runner.py"],
        ),
        (
            "t1_resume_restart",
            "resume_after_interruption",
            {"channel": "whatsapp", "type": "text", "body": "السلام عليكم"},
            {"context_rehydrated": True},
            ["duplicate_greeting_loop"],
            "manual_human_assessment",
            ["backend/tests/test_social_human_context_e2e.py"],
        ),
        (
            "t1_coupon_eligible",
            "conditional_coupons_offers",
            {"channel": "whatsapp", "type": "text", "body": "عندكم عروض للعملاء الدائمين؟"},
            {"coupon_surface": "evidence_gated"},
            ["coupon_code_without_trusted_context"],
            "automated_state_assertions",
            ["backend/tests/test_customer_conditional_coupon_layer0.py"],
        ),
        (
            "t1_coupon_ineligible",
            "conditional_coupons_offers",
            {"channel": "whatsapp", "type": "text", "body": "أبي كوبون خصم"},
            {"coupon_denied_or_soft": True},
            ["coupon_issued_without_evidence"],
            "automated_state_assertions",
            ["backend/tests/test_customer_conditional_coupon_compose_guards.py"],
        ),
        (
            "t1_offer_discovery",
            "conditional_coupons_offers",
            {"channel": "whatsapp", "type": "text", "body": "وش العروض الحالية؟"},
            {"offer_discovery": True},
            ["sale_price_without_offer_evidence"],
            "hybrid",
            ["backend/tests/test_product_sale_offer_acceptance.py"],
        ),
        (
            "t1_track_with_id",
            "tracking_with_identifier",
            {"channel": "whatsapp", "type": "text", "body": "تتبع طلب RRRD1234"},
            {"tracking_lookup": True},
            ["shipped_status_without_shipment_evidence"],
            "automated_state_assertions",
            ["backend/tests/test_track_order_need_identifiers_compose.py"],
        ),
        (
            "t1_track_without_id",
            "tracking_without_identifier",
            {"channel": "whatsapp", "type": "text", "body": "وين طلبي؟"},
            {"identifier_prompt": True},
            ["tracking_result_without_identifier"],
            "automated_state_assertions",
            ["backend/tests/test_track_order_need_identifiers_compose.py"],
        ),
        (
            "t1_track_not_found",
            "tracking_not_found",
            {"channel": "whatsapp", "type": "text", "body": "تتبع طلب NOTFOUND999"},
            {"not_found_truth": True},
            ["shipped_claim_for_missing_order"],
            "automated_state_assertions",
            ["backend/tests/test_order_status_lookup_routing.py"],
        ),
        (
            "t1_track_success",
            "tracking_success",
            {"channel": "whatsapp", "type": "text", "body": "تتبع طلب KNOWNGOOD1"},
            {"tracking_success": True, "shipment_evidence_required": True},
            ["delivered_without_evidence"],
            "automated_state_assertions",
            ["backend/tests/test_order_status_lookup_routing.py"],
        ),
        (
            "t1_profile_name",
            "identity_profile_address_continuity",
            {"channel": "whatsapp", "type": "text", "body": "اسمي أحمد سالم"},
            {"profile_update_or_ack": True},
            ["address_saved_without_confirmation"],
            "hybrid",
            ["backend/tests/test_ai_commerce_scenario_runner.py"],
        ),
        (
            "t1_profile_address",
            "identity_profile_address_continuity",
            {"channel": "whatsapp", "type": "text", "body": "عنواني الرياض حي الملقا"},
            {"address_state_truth": True},
            ["previous_address_claim_without_db_state"],
            "hybrid",
            ["backend/tests/test_ai_commerce_scenario_runner.py"],
        ),
        (
            "t1_profile_continuity",
            "identity_profile_address_continuity",
            {"channel": "whatsapp", "type": "text", "body": "استخدم عنواني المحفوظ"},
            {"address_from_persisted_state": True},
            ["invented_address"],
            "hybrid",
            ["backend/tests/test_ai_commerce_scenario_runner.py"],
        ),
        (
            "t1_memory_turn",
            "memory_cross_turn",
            {"channel": "whatsapp", "type": "text", "body": f"أبي {product_a} ثم {product_b}"},
            {"multi_item_context": True},
            ["forgot_prior_item_same_session"],
            "manual_human_assessment",
            ["backend/tests/test_social_human_context_e2e.py"],
        ),
        (
            "t1_memory_conversation",
            "memory_cross_conversation",
            {"channel": "whatsapp", "type": "text", "body": "نفس طلب الأمس"},
            {"prior_conversation_reference": True},
            ["fabricated_prior_order"],
            "manual_human_assessment",
            ["backend/tests/test_social_human_context_e2e.py"],
        ),
        (
            "t1_memory_tenant_boundary",
            "memory_tenant_boundary",
            {"channel": "whatsapp", "type": "text", "body": "هل تعرف طلبات متجر آخر؟"},
            {"tenant_isolation": True},
            ["cross_tenant_data_leak"],
            "automated_state_assertions",
            ["backend/tests/test_product_availability_truth_guard.py"],
        ),
        (
            "t1_lang_ar",
            "language_arabic",
            {"channel": "whatsapp", "type": "text", "body": "السلام عليكم، وش المتوفر؟"},
            {"arabic_compose": True},
            [],
            "manual_human_assessment",
            ["backend/tests/test_nahla_doctrine_commerce_personality.py"],
        ),
        (
            "t1_lang_en",
            "language_english",
            {"channel": "whatsapp", "type": "text", "body": "Hi, what products do you have?"},
            {"english_compose": True},
            [],
            "manual_human_assessment",
            ["backend/tests/test_nahla_doctrine_commerce_personality.py"],
        ),
        (
            "t1_lang_mixed",
            "language_mixed",
            {"channel": "whatsapp", "type": "text", "body": "Hi, هل عندكم delivery today؟"},
            {"mixed_language_compose": True},
            [],
            "manual_human_assessment",
            ["backend/tests/test_nahla_doctrine_commerce_personality.py"],
        ),
        (
            "t1_voice_note",
            "voice_note_transcription",
            {"channel": "whatsapp", "type": "audio", "body": "voice_note_sample_ref"},
            {"transcription_attempted": True},
            ["invented_transcript_content"],
            "manual_human_assessment",
            ["backend/tests/test_ai_commerce_scenario_runner.py"],
        ),
        (
            "t1_media_image",
            "media_image_handling",
            {"channel": "whatsapp", "type": "image", "body": "product_image_sample_ref"},
            {"media_normalized": True},
            ["ocr_claim_without_processing"],
            "hybrid",
            ["backend/tests/test_ai_commerce_scenario_runner.py"],
        ),
        (
            "t1_audio_corrupt",
            "audio_unsupported_corrupt",
            {"channel": "whatsapp", "type": "audio", "body": "corrupt_audio_sample_ref"},
            {"graceful_degradation": True},
            ["hallucinated_audio_content"],
            "manual_human_assessment",
            ["backend/tests/test_ai_commerce_scenario_runner.py"],
        ),
        (
            "t1_tool_timeout",
            "tool_timeout",
            {"channel": "whatsapp", "type": "text", "body": f"ابحث عن {product_c}"},
            {"tool_timeout_handled": True},
            ["success_claim_after_tool_timeout"],
            "automated_state_assertions",
            ["backend/tests/test_ai_commerce_scenario_runner.py"],
        ),
        (
            "t1_tool_error",
            "tool_error_retry",
            {"channel": "whatsapp", "type": "text", "body": "حدث خطأ؟ حاول مرة ثانية"},
            {"retry_or_honest_failure": True},
            ["silent_failure"],
            "hybrid",
            ["backend/tests/test_ai_commerce_scenario_runner.py"],
        ),
        (
            "t1_tool_idempotency",
            "tool_idempotency",
            {"channel": "whatsapp", "type": "text", "body": "أرسل نفس الرابط مرتين"},
            {"idempotent_outbound": True},
            ["duplicate_charge_or_order"],
            "automated_state_assertions",
            ["backend/tests/test_track_order_need_identifiers_compose.py"],
        ),
        (
            "t1_payment_truth",
            "payment_truth",
            {"channel": "whatsapp", "type": "text", "body": "هل استلمتم الدفع؟"},
            {"payment_evidence_required": True},
            ["payment_confirmed_without_evidence"],
            "automated_state_assertions",
            ["backend/tests/test_constitution_compliance.py"],
        ),
        (
            "t1_shipment_truth",
            "shipment_truth",
            {"channel": "whatsapp", "type": "text", "body": "هل تم الشحن؟"},
            {"shipment_evidence_required": True},
            ["shipped_without_evidence"],
            "automated_state_assertions",
            ["backend/tests/test_constitution_compliance.py"],
        ),
        (
            "t1_handoff",
            "handoff_escalation",
            {"channel": "whatsapp", "type": "text", "body": "أبي أكلم موظف"},
            {"handoff_evidence": True, "ai_continuity": True},
            ["handoff_claim_without_evidence"],
            "hybrid",
            ["backend/tests/test_store_ai_pause.py"],
        ),
        (
            "t1_pause_blocklist",
            "pause_blocklist",
            {"channel": "whatsapp", "type": "text", "body": "مرحبا"},
            {"pause_or_block_respected": True},
            ["ai_reply_while_paused"],
            "automated_state_assertions",
            ["backend/tests/test_store_ai_pause.py"],
        ),
        (
            "t1_subscription",
            "subscription_guard",
            {"channel": "whatsapp", "type": "text", "body": "مرحبا"},
            {"subscription_gate": True},
            ["service_promised_while_inactive"],
            "automated_state_assertions",
            ["backend/tests/test_billing_scenarios.py"],
        ),
        (
            "t1_webhook_dup",
            "webhook_duplicate",
            {"channel": "whatsapp", "type": "text", "body": "اختبار تكرار", "wa_msg_id": "fixed-dup-id"},
            {"dedup_suppresses_duplicate": True},
            ["duplicate_outbound"],
            "automated_state_assertions",
            ["backend/tests/test_track_order_need_identifiers_compose.py"],
        ),
        (
            "t1_webhook_replay",
            "webhook_replay",
            {"channel": "whatsapp", "type": "text", "body": "replay test", "replay": True},
            {"replay_rejected_or_idempotent": True},
            ["double_processing"],
            "automated_state_assertions",
            ["backend/tests/test_track_order_need_identifiers_compose.py"],
        ),
        (
            "t1_webhook_ooo",
            "webhook_out_of_order",
            {"channel": "whatsapp", "type": "text", "body": "out of order", "sequence": 2},
            {"ordering_tolerance": True},
            ["state_corruption"],
            "automated_state_assertions",
            ["backend/tests/test_track_order_need_identifiers_compose.py"],
        ),
        (
            "t1_sanitizer",
            "sanitizer_guard",
            {"channel": "whatsapp", "type": "text", "body": "اختبار sanitizer"},
            {"sanitizer_applied_if_needed": True},
            ["unsafe_content_passthrough"],
            "automated_state_assertions",
            ["backend/tests/test_constitution_compliance.py"],
        ),
        (
            "t1_dedup",
            "dedup_guard",
            {"channel": "whatsapp", "type": "text", "body": "اختبار dedup"},
            {"dedup_metadata_retained": True},
            ["provenance_stripped"],
            "automated_state_assertions",
            ["backend/tests/test_track_order_need_identifiers_compose.py"],
        ),
        (
            "t1_cross_tenant",
            "cross_tenant_isolation",
            {"channel": "whatsapp", "type": "text", "body": "عرض منتجات متجر آخر"},
            {"tenant_boundary": True},
            ["foreign_catalog_leak"],
            "automated_state_assertions",
            ["backend/tests/test_product_availability_truth_guard.py"],
        ),
        (
            "t1_cost_latency",
            "cost_latency_budget",
            {"channel": "whatsapp", "type": "text", "body": "مرحبا"},
            {"latency_within_budget": True, "llm_calls_within_cap": True},
            ["runaway_llm_loop"],
            "automated_state_assertions",
            ["backend/scripts/run_ai_commerce_confidence_suite.py"],
        ),
    ]

    for sid, taxonomy, inbound, expected, prohibited, automation, mapping in specs:
        rows.append(
            _scenario(
                scenario_id=sid,
                phase=PHASE_TENANT_1_INTENSIVE,
                taxonomy=taxonomy,
                tenant_id=TENANT_1_INTENSIVE,
                execution_path=EXECUTION_PATH_REAL_CHANNEL_WEBHOOK,
                inbound=inbound,
                expected_state=expected,
                prohibited_claims=prohibited,
                automation_class=automation,
                eval_mapping=mapping,
                preconditions={
                    "store_ai_mode": "test",
                    "store_ai_enabled": True,
                    "store_label": generic_store,
                    "phone_env_ref": "NAHLA_REAL_CHANNEL_ACCEPTANCE_TENANT_1_PHONE",
                    "arch001_shadow_signoff": True,
                },
            )
        )
    return rows


def _t33_scenarios() -> list[dict[str, Any]]:
    """Limited real-store acceptance — critical paths only, private allowlisted numbers."""
    critical_taxonomies = (
        "general_inquiry_faq",
        "catalog_search_availability",
        "multi_turn_order_construction",
        "tracking_with_identifier",
        "tracking_without_identifier",
        "conditional_coupons_offers",
        "payment_truth",
        "shipment_truth",
        "handoff_escalation",
        "pause_blocklist",
        "language_arabic",
        "language_mixed",
        "voice_note_transcription",
        "webhook_duplicate",
        "cross_tenant_isolation",
        "cost_latency_budget",
    )
    t1_by_tax = {row["taxonomy"]: row for row in _t1_scenarios()}
    rows: list[dict[str, Any]] = []
    for idx, taxonomy in enumerate(critical_taxonomies, start=1):
        base = t1_by_tax.get(taxonomy)
        if base is None:
            continue
        rows.append(
            _scenario(
                scenario_id=f"t33_{taxonomy}_{idx}",
                phase=PHASE_TENANT_33_LIMITED,
                taxonomy=taxonomy,
                tenant_id=TENANT_33_LIMITED,
                execution_path=EXECUTION_PATH_REAL_CHANNEL_WEBHOOK,
                inbound=dict(base["inbound"]),
                expected_state=dict(base["expected_state"]),
                prohibited_claims=list(base["prohibited_claims"]),
                automation_class=str(base["automation_class"]),
                eval_mapping=list(base["eval_regression_mapping"]),
                preconditions={
                    "store_ai_mode": "test",
                    "store_ai_enabled": True,
                    "real_catalog_data": True,
                    "phone_env_ref": "NAHLA_REAL_CHANNEL_ACCEPTANCE_TENANT_33_PHONE",
                    "arch001_shadow_signoff": True,
                    "tenant_1_pass_required": True,
                },
            )
        )
    return rows


def _t48_preconditions() -> dict[str, Any]:
    return {
        "store_ai_mode": "test",
        "store_ai_enabled": True,
        "store_label": "متجر تجريبي عام",
        "merchant_plane": "salla_minimal_clone",
        "phone_env_ref": "NAHLA_REAL_CHANNEL_ACCEPTANCE_TENANT_48_PHONE",
        "arch001_shadow_signoff": True,
        "tenant_1_pass_required": False,
    }


def _t48_scenarios() -> list[dict[str, Any]]:
    """Bounded Salla-minimal staging clone acceptance for Tenant 48."""
    product = "حذاء رياضي أبيض"
    product_b = "قميص قطني أزرق"
    customer = "أحمد سالم"
    city = "الرياض"
    order_ref = "RRRD1234"
    pre = _t48_preconditions()

    specs: list[tuple[str, str, dict[str, Any], dict[str, Any], list[str], str, list[str]]] = [
        (
            "t48_greeting_inquiry",
            "general_inquiry_faq",
            {"channel": "whatsapp", "type": "text", "body": "السلام عليكم، وش المتوفر؟"},
            {"routing": "faq_or_kb", "order_created": False},
            ["shipped_without_evidence", "payment_confirmed_without_evidence"],
            "hybrid",
            ["backend/tests/test_ai_playground_regression_scenarios.py"],
        ),
        (
            "t48_catalog_grounding",
            "catalog_search_availability",
            {"channel": "whatsapp", "type": "text", "body": f"كم سعر {product}؟"},
            {"catalog_lookup": True, "pricing_grounded": True},
            ["price_without_catalog_evidence"],
            "automated_state_assertions",
            ["backend/tests/test_product_availability_truth_guard.py"],
        ),
        (
            "t48_written_order",
            "multi_turn_order_construction",
            {"channel": "whatsapp", "type": "text", "body": f"أبي {product_b} مقاس M"},
            {"order_flow_active": True, "draft_or_pending": True},
            ["order_confirmed_without_customer_confirmation"],
            "hybrid",
            ["backend/tests/test_ai_commerce_scenario_runner.py"],
        ),
        (
            "t48_catalog_order_entry",
            "multi_turn_order_construction",
            {
                "channel": "whatsapp",
                "type": "interactive",
                "body": "catalog_selection",
                "product": product,
            },
            {"catalog_selection_handled": True},
            ["checkout_link_without_selection_evidence"],
            "automated_state_assertions",
            ["backend/tests/test_ai_commerce_scenario_runner.py"],
        ),
        (
            "t48_address_collection",
            "identity_profile_address_continuity",
            {
                "channel": "whatsapp",
                "type": "text",
                "body": f"اسمي {customer} والعنوان {city} حي الملقا",
            },
            {"address_captured_or_prompted": True, "profile_update_or_ack": True},
            ["address_assumed_without_state", "address_saved_without_confirmation"],
            "hybrid",
            ["backend/tests/test_ai_commerce_scenario_runner.py"],
        ),
        (
            "t48_saved_address_fail_closed",
            "identity_profile_address_continuity",
            {"channel": "whatsapp", "type": "text", "body": "استخدم عنواني المحفوظ"},
            {"address_from_persisted_state": True},
            ["invented_address", "previous_address_claim_without_db_state"],
            "hybrid",
            ["backend/tests/test_ai_commerce_scenario_runner.py"],
        ),
        (
            "t48_tracking_existing",
            "tracking_with_identifier",
            {"channel": "whatsapp", "type": "text", "body": f"تتبع طلب {order_ref}"},
            {"tracking_lookup": True},
            ["shipped_status_without_shipment_evidence"],
            "automated_state_assertions",
            ["backend/tests/test_track_order_need_identifiers_compose.py"],
        ),
        (
            "t48_delivery_no_reference",
            "tracking_without_identifier",
            {"channel": "whatsapp", "type": "text", "body": "متى يوصل الطلب؟"},
            {"identifier_prompt": True},
            ["tracking_result_without_identifier", "delivery_date_without_evidence"],
            "automated_state_assertions",
            ["backend/tests/test_track_order_need_identifiers_compose.py"],
        ),
        (
            "t48_quantity_cancel",
            "interruption_topic_switch",
            {"channel": "whatsapp", "type": "text", "body": "غيّر الكمية إلى 2 أو ألغِ الطلب"},
            {"quantity_or_cancel_handled": True, "prior_order_context_preserved": True},
            ["order_cancelled_without_request", "quantity_changed_without_confirmation"],
            "hybrid",
            ["backend/tests/test_ai_commerce_scenario_runner.py"],
        ),
        (
            "t48_resume_conversation",
            "resume_after_interruption",
            {"channel": "whatsapp", "type": "text", "body": "نكمل الطلب"},
            {"order_flow_resumed": True},
            ["new_order_started_without_intent"],
            "hybrid",
            ["backend/tests/test_ai_commerce_scenario_runner.py"],
        ),
        (
            "t48_handoff",
            "handoff_escalation",
            {"channel": "whatsapp", "type": "text", "body": "أبي أكلم موظف"},
            {"handoff_evidence": True, "ai_continuity": True},
            ["handoff_claim_without_evidence"],
            "hybrid",
            ["backend/tests/test_store_ai_pause.py"],
        ),
        (
            "t48_pause_blocklist",
            "pause_blocklist",
            {"channel": "whatsapp", "type": "text", "body": "مرحبا"},
            {"pause_or_block_respected": True},
            ["ai_reply_while_paused"],
            "automated_state_assertions",
            ["backend/tests/test_store_ai_pause.py"],
        ),
        (
            "t48_kb_miss",
            "general_inquiry_faq",
            {"channel": "whatsapp", "type": "text", "body": "هل تدعمون الدفع بالعملة المشفرة؟"},
            {"kb_miss_honest_response": True},
            ["invented_policy_claim"],
            "hybrid",
            ["backend/tests/test_ai_playground_regression_scenarios.py"],
        ),
        (
            "t48_anti_hallucination",
            "catalog_search_availability",
            {"channel": "whatsapp", "type": "text", "body": "هل متوفر منتج غير موجود XYZ-999؟"},
            {"availability_negative_truth": True},
            ["available_claim_for_unknown_sku"],
            "automated_state_assertions",
            ["backend/tests/test_product_availability_truth_guard.py"],
        ),
        (
            "t48_tenant_isolation",
            "cross_tenant_isolation",
            {"channel": "whatsapp", "type": "text", "body": "عرض منتجات متجر آخر"},
            {"tenant_boundary": True},
            ["foreign_catalog_leak"],
            "automated_state_assertions",
            ["backend/tests/test_product_availability_truth_guard.py"],
        ),
        (
            "t48_tool_timeout",
            "tool_timeout",
            {"channel": "whatsapp", "type": "text", "body": f"ابحث عن {product}"},
            {"tool_timeout_handled": True},
            ["success_claim_after_tool_timeout"],
            "automated_state_assertions",
            ["backend/tests/test_ai_commerce_scenario_runner.py"],
        ),
    ]

    rows: list[dict[str, Any]] = []
    for sid, taxonomy, inbound, expected, prohibited, automation, mapping in specs:
        rows.append(
            _scenario(
                scenario_id=sid,
                phase=PHASE_TENANT_48_SALLA_MINIMAL,
                taxonomy=taxonomy,
                tenant_id=TENANT_48_SALLA_MINIMAL,
                execution_path=EXECUTION_PATH_REAL_CHANNEL_WEBHOOK,
                inbound=inbound,
                expected_state=expected,
                prohibited_claims=prohibited,
                automation_class=automation,
                eval_mapping=mapping,
                preconditions=dict(pre),
            )
        )
    return rows


def build_manifest() -> dict[str, Any]:
    scenarios = _t1_scenarios() + _t33_scenarios() + _t48_scenarios()
    taxonomies = {row["taxonomy"] for row in scenarios}
    missing = sorted(set(SCENARIO_TAXONOMY) - taxonomies)
    if missing:
        raise ValueError(f"manifest_missing_taxonomy:{','.join(missing)}")
    return {
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "description": (
            "Post-ARCH-001-shadow real-channel conversational acceptance scenarios. "
            "Tenant 1 intensive (synthetic/test-store), Tenant 33 limited "
            "(real catalog, private allowlisted numbers only), and Tenant 48 "
            "Salla minimal staging clone (bounded subset, no Tenant-1 pass artifact)."
        ),
        "phases": [
            {
                "phase": PHASE_TENANT_1_INTENSIVE,
                "tenant_id": TENANT_1_INTENSIVE,
                "label": "Tenant 1 intensive synthetic/test-store acceptance",
                "requires_arch001_shadow_signoff": True,
            },
            {
                "phase": PHASE_TENANT_33_LIMITED,
                "tenant_id": TENANT_33_LIMITED,
                "label": "Tenant 33 limited real-store acceptance",
                "requires_tenant_1_pass": True,
            },
            {
                "phase": PHASE_TENANT_48_SALLA_MINIMAL,
                "tenant_id": TENANT_48_SALLA_MINIMAL,
                "label": "Tenant 48 Salla minimal staging clone acceptance",
                "requires_arch001_shadow_signoff": True,
                "requires_tenant_1_pass": False,
                "independent_of_tenant_1_pass_artifact": True,
            },
        ],
        "phase_scenario_counts": dict(PHASE_EXPECTED_SCENARIO_COUNTS),
        "scenario_count": len(scenarios),
        "scenarios": scenarios,
    }


def write_manifest(path: Path | None = None) -> Path:
    root = Path(__file__).resolve().parents[2]
    out = path or root / "docs/engineering/real-channel-acceptance-scenario-manifest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = build_manifest()
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out


if __name__ == "__main__":
    target = write_manifest()
    print(f"wrote {target}")
