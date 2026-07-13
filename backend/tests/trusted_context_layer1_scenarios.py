"""Trusted Context Layer 1 mass-validation scenario catalog (data only)."""
from __future__ import annotations

from datetime import timedelta

from trusted_context_layer1_harness import Layer1Scenario, utcnow

_NOW = utcnow()


def _coupon_seed(**kwargs):
    return kwargs


def _promo_seed(**kwargs):
    return kwargs


SCENARIOS: list[Layer1Scenario] = [
    # A — relevance / lazy loading
    Layer1Scenario("A01", "A", "social_greeting_no_offer_loader", inbound_text="السلام عليكم", expected_lazy_load=False),
    Layer1Scenario("A02", "A", "social_thanks_no_offer_loader", inbound_text="شكراً جزيلاً", expected_lazy_load=False),
    Layer1Scenario("A03", "A", "social_smalltalk_no_offer_loader", inbound_text="كيف حالك؟", expected_lazy_load=False),
    Layer1Scenario("A04", "A", "unrelated_product_no_offer_loader", inbound_text="عندكم حذاء رياضي أبيض؟", expected_lazy_load=False),
    Layer1Scenario("A05", "A", "coupon_intent_triggers_loader", inbound_text="هل عندكم كوبون؟", expected_lazy_load=True),
    Layer1Scenario("A06", "A", "discount_code_intent_triggers_loader", inbound_text="أعطني discount code", expected_lazy_load=True),
    Layer1Scenario("A07", "A", "offer_intent_triggers_loader", inbound_text="هل يوجد عرض؟", expected_lazy_load=True),
    Layer1Scenario("A08", "A", "promotion_keyword_triggers_loader", inbound_text="هل يوجد promotion؟", expected_lazy_load=True),
    Layer1Scenario("A09", "A", "cart_discount_context_triggers_loader", inbound_text="أبغى خصم", brain_state={"catalog_checkout_total": 180.0, "line_items": [{"product_id": 9}]}, expected_lazy_load=True),
    Layer1Scenario("A10", "A", "metadata_coupon_code_triggers_loader", inbound_text="تمام", inbound_metadata={"coupon_code": "SAVE10"}, expected_lazy_load=True),
    # B — coupon eligibility
    Layer1Scenario("B01", "B", "coupon_active_eligible", eligibility_target="coupon", coupon_seed=_coupon_seed(), basket_total=250.0, expected_eligible=True, expected_verified=True),
    Layer1Scenario("B02", "B", "coupon_expired", eligibility_target="coupon", coupon_seed=_coupon_seed(expires_at=_NOW - timedelta(days=1)), expected_eligible=False, expected_reason="expired"),
    Layer1Scenario("B03", "B", "coupon_disabled", eligibility_target="coupon", coupon_seed=_coupon_seed(extra_metadata={"active": False}), expected_eligible=False, expected_reason="disabled"),
    Layer1Scenario("B04", "B", "coupon_usage_limit_reached", eligibility_target="coupon", coupon_seed=_coupon_seed(extra_metadata={"usage_count": 5, "usage_limit": 5}), expected_eligible=False, expected_reason="usage_limit_reached"),
    Layer1Scenario("B05", "B", "coupon_personal_correct_customer", eligibility_target="coupon", coupon_seed=_coupon_seed(extra_metadata={"customer_id": 11}), customer_id=11, expected_eligible=True),
    Layer1Scenario("B06", "B", "coupon_personal_wrong_customer", eligibility_target="coupon", coupon_seed=_coupon_seed(extra_metadata={"customer_id": 99}), customer_id=11, expected_eligible=False, expected_reason="customer_restriction"),
    Layer1Scenario("B07", "B", "coupon_personal_missing_customer", eligibility_target="coupon", coupon_seed=_coupon_seed(extra_metadata={"customer_id": 11}), customer_id=None, expected_eligible=None, expected_verified=False, expected_reason="customer_unverified"),
    Layer1Scenario("B08", "B", "coupon_minimum_basket_met", eligibility_target="coupon", coupon_seed=_coupon_seed(extra_metadata={"min_order_amount": 100}), basket_total=150.0, expected_eligible=True),
    Layer1Scenario("B09", "B", "coupon_minimum_basket_not_met", eligibility_target="coupon", coupon_seed=_coupon_seed(extra_metadata={"min_order_amount": 300}), basket_total=150.0, expected_eligible=False, expected_reason="minimum_basket_not_met"),
    Layer1Scenario("B10", "B", "coupon_minimum_basket_unknown", eligibility_target="coupon", coupon_seed=_coupon_seed(extra_metadata={"min_order_amount": 100}), basket_total=None, expected_eligible=None, expected_verified=False, expected_reason="minimum_basket_unverified"),
    Layer1Scenario("B11", "B", "coupon_product_restriction_met", eligibility_target="coupon", coupon_seed=_coupon_seed(extra_metadata={"product_ids": ["9"]}), basket_total=100.0, brain_state={"line_items": [{"product_id": 9}]}, expected_eligible=True),
    Layer1Scenario("B12", "B", "coupon_product_restriction_not_met", eligibility_target="coupon", coupon_seed=_coupon_seed(extra_metadata={"product_ids": ["9"]}), basket_total=100.0, brain_state={"line_items": [{"product_id": 77}]}, expected_eligible=False, expected_reason="product_restriction_not_met"),
    Layer1Scenario("B13", "B", "coupon_category_advisory_not_verified", eligibility_target="coupon", coupon_seed=_coupon_seed(extra_metadata={"applicable_categories": [3]}), basket_total=100.0, expected_eligible=None, expected_verified=False),
    Layer1Scenario("B14", "B", "coupon_already_applied", eligibility_target="coupon", coupon_seed=_coupon_seed(code="SAVE10"), applied_codes=("SAVE10",), expected_eligible=False, expected_reason="already_applied"),
    Layer1Scenario("B15", "B", "coupon_available_not_applied", eligibility_target="coupon", coupon_seed=_coupon_seed(code="SAVE10"), applied_codes=(), expected_eligible=True),
    Layer1Scenario("B16", "B", "coupon_malformed_metadata", eligibility_target="coupon", coupon_seed=_coupon_seed(extra_metadata={"disabled": "yes"}), expected_eligible=False, expected_reason="disabled"),
    Layer1Scenario("B17", "B", "coupon_timezone_boundary_active", eligibility_target="coupon", coupon_seed=_coupon_seed(expires_at=_NOW + timedelta(minutes=5)), expected_eligible=True),
    Layer1Scenario("B18", "B", "coupon_timezone_boundary_expired", eligibility_target="coupon", coupon_seed=_coupon_seed(expires_at=_NOW - timedelta(seconds=1)), expected_eligible=False, expected_reason="expired"),
    # C — promotion eligibility
    Layer1Scenario("C01", "C", "promotion_active_eligible", eligibility_target="promotion", promotion_seed=_promo_seed(), basket_total=200.0, expected_eligible=True, expected_verified=True),
    Layer1Scenario("C02", "C", "promotion_future_window", eligibility_target="promotion", promotion_seed=_promo_seed(starts_at=_NOW + timedelta(days=2)), expected_eligible=False, expected_reason="outside_active_window"),
    Layer1Scenario("C03", "C", "promotion_expired", eligibility_target="promotion", promotion_seed=_promo_seed(ends_at=_NOW - timedelta(hours=1)), expected_eligible=False, expected_reason="outside_active_window"),
    Layer1Scenario("C04", "C", "promotion_inactive_status", eligibility_target="promotion", promotion_seed=_promo_seed(status="inactive"), expected_eligible=False, expected_reason="outside_active_window"),
    Layer1Scenario("C05", "C", "promotion_usage_limit_reached", eligibility_target="promotion", promotion_seed=_promo_seed(usage_limit=2, usage_count=2), expected_eligible=False),
    Layer1Scenario("C06", "C", "promotion_segment_match", eligibility_target="promotion", promotion_seed=_promo_seed(conditions={"customer_segments": ["vip"]}), customer_profile={"segment": "vip", "customer_id": 11}, expected_eligible=True),
    Layer1Scenario("C07", "C", "promotion_segment_mismatch", eligibility_target="promotion", promotion_seed=_promo_seed(conditions={"customer_segments": ["vip"]}), customer_profile={"segment": "new", "customer_id": 11}, expected_eligible=False, expected_reason="segment_mismatch"),
    Layer1Scenario("C08", "C", "promotion_minimum_order_met", eligibility_target="promotion", promotion_seed=_promo_seed(conditions={"min_order_amount": 100}), basket_total=150.0, expected_eligible=True),
    Layer1Scenario("C09", "C", "promotion_minimum_order_unknown", eligibility_target="promotion", promotion_seed=_promo_seed(conditions={"min_order_amount": 100}), basket_total=None, expected_eligible=None, expected_verified=False, expected_reason="minimum_basket_unverified"),
    Layer1Scenario("C10", "C", "promotion_category_advisory", eligibility_target="promotion", promotion_seed=_promo_seed(conditions={"applicable_categories": [4]}), expected_eligible=None, expected_verified=False, expected_reason="advisory_conditions_unverified"),
    Layer1Scenario("C11", "C", "promotion_bxgy_unknown", eligibility_target="promotion", promotion_seed=_promo_seed(promotion_type="buy_x_get_y", conditions={"x_quantity": 2, "y_quantity": 1}), expected_eligible=None, expected_verified=False, expected_reason="advisory_conditions_unverified"),
    Layer1Scenario("C12", "C", "promotion_multiple_no_winner", eligibility_target="loader", force_offer_loader=True, inbound_text="في عروض؟", promotions=(_promo_seed(id=1), _promo_seed(id=2)), expected_domains_loaded=("promotions",)),
    # D — error contract (#580)
    Layer1Scenario("D01", "D", "coupon_loader_runtime_build_error", expected_status="build_error", expected_error_class="RuntimeError", loader_side_effect=RuntimeError("secret-value"), force_offer_loader=True, inbound_text="عندكم كوبون؟", privacy_secrets=("secret-value",)),
    Layer1Scenario("D02", "D", "coupon_loader_timeout_build_error", expected_status="build_error", expected_error_class="TimeoutError", loader_side_effect=TimeoutError("secret-value"), force_offer_loader=True, inbound_text="هل يوجد خصم؟", privacy_secrets=("secret-value",)),
    Layer1Scenario("D03", "D", "top_level_build_error_class_only", expected_status="build_error", expected_error_class="RuntimeError", loader_side_effect=RuntimeError("secret body"), force_offer_loader=True, inbound_text="كوبون", privacy_secrets=("secret body",)),
    Layer1Scenario("D04", "D", "missing_phone_still_fail_open", expected_status="success", customer_phone="", inbound_text="مرحبا", expected_domains_not_loaded=("coupons", "promotions")),
    Layer1Scenario("D05", "D", "missing_conversation_id_success", expected_status="success", conversation_id=0, inbound_text="مرحبا"),
    # E — tenant isolation with tenant-scoped query
    Layer1Scenario(
        "E01", "E", "tenant_scoped_coupon_query",
        eligibility_target="loader",
        tenant_id=201,
        coupons=(_coupon_seed(id=1, code="SHARED"),),
        tenant_b_coupons=(_coupon_seed(id=99, tenant_id=202, code="SHARED"),),
        force_offer_loader=True,
        inbound_text="هل عندكم كوبون؟",
    ),
    Layer1Scenario(
        "E02", "E", "tenant_b_isolated_coupon_query",
        eligibility_target="loader",
        tenant_id=202,
        coupons=(_coupon_seed(id=2, code="TWOONLY", tenant_id=202),),
        tenant_b_coupons=(_coupon_seed(id=1, code="SHARED"),),
        force_offer_loader=True,
        inbound_text="هل عندكم كوبون؟",
    ),
    Layer1Scenario("E03", "E", "tenant_mismatch_record_guard", eligibility_target="coupon", coupon_seed=_coupon_seed(tenant_id=202), tenant_id=201, expected_eligible=False, expected_reason="tenant_mismatch"),
    Layer1Scenario("E04", "E", "promotion_tenant_mismatch_guard", eligibility_target="promotion", promotion_seed=_promo_seed(tenant_id=202), tenant_id=201, expected_eligible=False, expected_reason="tenant_mismatch"),
    # F — lifecycle / ContextVar
    Layer1Scenario("F01", "F", "duplicate_call_same_turn", lifecycle_action="duplicate_same_turn", inbound_text="مرحبا"),
    Layer1Scenario("F02", "F", "new_turn_new_snapshot", lifecycle_action="new_turn_new_snapshot", inbound_text="مرحبا"),
    Layer1Scenario("F03", "F", "concurrent_contextvar_isolation", lifecycle_action="concurrent_isolation", inbound_text="مرحبا"),
    Layer1Scenario("F04", "F", "failure_then_success_cleanup", lifecycle_action="failure_then_success"),
    Layer1Scenario("F05", "F", "cleanup_after_shadow_success", expected_status="success", inbound_text="مرحبا"),
    # G — privacy / provenance
    Layer1Scenario(
        "G01", "G", "telemetry_masks_coupon_code",
        eligibility_target="loader",
        coupons=(_coupon_seed(code="SECRET_COUPON_ABC123"),),
        force_offer_loader=True,
        inbound_text="عندكم كوبون؟",
        privacy_secrets=("SECRET_COUPON_ABC123",),
    ),
    Layer1Scenario(
        "G02", "G", "snapshot_may_retain_internal_code_when_required",
        eligibility_target="loader",
        coupons=(_coupon_seed(code="SECRET_COUPON_ABC123"),),
        force_offer_loader=True,
        inbound_text="الكوبون SAVE10",
        inbound_metadata={"coupon_code": "SECRET_COUPON_ABC123"},
        allow_code_in_snapshot=True,
        privacy_secrets=(),
    ),
    Layer1Scenario(
        "G03", "G", "masked_code_format",
        eligibility_target="coupon",
        coupon_seed=_coupon_seed(code="SECRET_COUPON_ABC123"),
        basket_total=100.0,
        expected_eligible=True,
    ),
    # H — behavioral equivalence (shadow on/off)
    Layer1Scenario("H01", "H", "shadow_enabled_social_build", shadow_enabled=True, inbound_text="مرحبا", expected_domains_not_loaded=("coupons", "promotions"), equivalence_pair="social"),
    Layer1Scenario("H02", "H", "shadow_disabled_social_build", shadow_enabled=False, inbound_text="مرحبا", equivalence_pair="social"),
    Layer1Scenario("H03", "H", "shadow_enabled_coupon_build", shadow_enabled=True, force_offer_loader=True, inbound_text="هل يوجد كوبون؟", coupons=(_coupon_seed(),), expected_domains_loaded=("coupons",), equivalence_pair="coupon"),
    Layer1Scenario("H04", "H", "shadow_disabled_coupon_build", shadow_enabled=False, force_offer_loader=True, inbound_text="هل يوجد كوبون؟", coupons=(_coupon_seed(),), equivalence_pair="coupon"),
    # I — read-only loader path
    Layer1Scenario("I01", "I", "loader_read_only_no_writes", eligibility_target="loader", force_offer_loader=True, inbound_text="خصم", coupons=(_coupon_seed(),)),
    Layer1Scenario("I02", "I", "build_snapshot_read_only_no_writes", expected_status="success", inbound_text="مرحبا"),
    # Handler-path scenarios (24)
    Layer1Scenario("HP01", "HP", "handler_social_fail_open", handler_path=True, inbound_text="السلام عليكم", shadow_enabled=True),
    Layer1Scenario("HP02", "HP", "handler_coupon_question", handler_path=True, inbound_text="عندكم كوبون؟", force_offer_loader=True, coupons=(_coupon_seed(),)),
    Layer1Scenario("HP03", "HP", "handler_offer_question", handler_path=True, inbound_text="في عروض؟", force_offer_loader=True, promotions=(_promo_seed(),)),
    Layer1Scenario("HP04", "HP", "handler_coupon_loader_build_error", handler_path=True, inbound_text="عندكم كوبون؟", loader_side_effect=RuntimeError("secret-value"), expected_status="build_error", expected_error_class="RuntimeError", privacy_secrets=("secret-value",)),
    Layer1Scenario("HP05", "HP", "handler_shadow_disabled_equivalence", handler_path=True, inbound_text="مرحبا", shadow_enabled=False, equivalence_pair="handler_social"),
    Layer1Scenario("HP06", "HP", "handler_shadow_enabled_equivalence", handler_path=True, inbound_text="مرحبا", shadow_enabled=True, equivalence_pair="handler_social"),
    Layer1Scenario("HP07", "HP", "handler_no_projection_in_brain", handler_path=True, inbound_text="هل يوجد خصم؟", force_offer_loader=True),
    Layer1Scenario("HP08", "HP", "handler_cleanup_after_success", handler_path=True, inbound_text="تمام", shadow_enabled=True),
    Layer1Scenario("HP09", "HP", "handler_cleanup_after_build_error", handler_path=True, inbound_text="كوبون", loader_side_effect=RuntimeError("secret-value"), expected_status="build_error", expected_error_class="RuntimeError"),
    Layer1Scenario("HP10", "HP", "handler_unrelated_product", handler_path=True, inbound_text="عندكم قميص قطني؟"),
    Layer1Scenario("HP11", "HP", "handler_discount_english", handler_path=True, inbound_text="any discount code?", force_offer_loader=True),
    Layer1Scenario("HP12", "HP", "handler_promotion_english", handler_path=True, inbound_text="active promotion?", force_offer_loader=True, promotions=(_promo_seed(),)),
    Layer1Scenario("HP13", "HP", "handler_with_history", handler_path=True, inbound_text="والكوبون؟", history=({"role": "user", "content": "مرحبا"},), force_offer_loader=True),
    Layer1Scenario("HP14", "HP", "handler_metadata_coupon", handler_path=True, inbound_text="تم", inbound_metadata={"coupon_code": "SAVE10"}, force_offer_loader=True, coupons=(_coupon_seed(),)),
    Layer1Scenario("HP15", "HP", "handler_tenant_scoped_coupon", handler_path=True, tenant_id=201, inbound_text="كوبون", force_offer_loader=True, coupons=(_coupon_seed(code="SHARED"),), tenant_b_coupons=(_coupon_seed(id=88, tenant_id=202, code="SHARED"),)),
    Layer1Scenario("HP16", "HP", "handler_timeout_build_error", handler_path=True, inbound_text="خصم", loader_side_effect=TimeoutError("secret-value"), expected_status="build_error", expected_error_class="TimeoutError"),
    Layer1Scenario("HP17", "HP", "handler_second_conversation", handler_path=True, conversation_id=777, inbound_text="مرحبا"),
    Layer1Scenario("HP18", "HP", "handler_second_customer_phone", handler_path=True, customer_phone="966500000888", inbound_text="مرحبا"),
    Layer1Scenario("HP19", "HP", "handler_cart_discount", handler_path=True, inbound_text="أبغى خصم", brain_state={"catalog_checkout_total": 120.0, "line_items": [{"product_id": 3}]}),
    Layer1Scenario("HP20", "HP", "handler_no_trace_secret_on_success", handler_path=True, inbound_text="عندكم كوبون؟", force_offer_loader=True, coupons=(_coupon_seed(code="SECRET_COUPON_ABC123"),), privacy_secrets=("SECRET_COUPON_ABC123",)),
    Layer1Scenario("HP21", "HP", "handler_provider_mock_only", handler_path=True, inbound_text="مرحبا"),
    Layer1Scenario("HP22", "HP", "handler_brain_reply_unchanged", handler_path=True, inbound_text="كيف الحال"),
    Layer1Scenario("HP23", "HP", "handler_generic_tenant_202", handler_path=True, tenant_id=202, customer_phone="966500000202", inbound_text="مرحبا"),
    Layer1Scenario("HP24", "HP", "handler_build_error_trace_contract", handler_path=True, inbound_text="كوبون؟", loader_side_effect=RuntimeError("secret-value"), expected_status="build_error", expected_error_class="RuntimeError"),
]

DUPLICATE_EQUIVALENCE_GROUPS = {
    "social": ("H01", "H02"),
    "coupon": ("H03", "H04"),
    "handler_social": ("HP05", "HP06"),
}
