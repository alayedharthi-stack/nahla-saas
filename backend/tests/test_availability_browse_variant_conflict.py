"""PR1 — availability variants, browse breadth, no canned conflict rewrite."""
from __future__ import annotations

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.product_entity_resolution import family_key_from_title  # noqa: E402
from modules.ai.brain.commerce.product_breadth_policy import (  # noqa: E402
    global_availability_browse_requested,
    resolve_product_breadth,
)
from modules.ai.brain.postprocess.availability_guard_policy import (  # noqa: E402
    browse_alternatives_requested,
    inbound_exempt_from_availability_rewrite,
)
from modules.ai.brain.postprocess.product_availability_evidence import (  # noqa: E402
    EVIDENCE_CONFLICT,
    EVIDENCE_VARIANT_OPTIONS,
    evaluate_product_availability_evidence,
)
from modules.ai.brain.postprocess.product_availability_truth_guard import (  # noqa: E402
    _CONFLICT_REPLY_AR,
    apply_product_availability_truth_guard,
)


def _sku(pid: int, title: str, *, checkout: bool, family: str = "") -> dict:
    return {
        "id": pid,
        "title": title,
        "sku": f"SKU-{pid}",
        "external_id": f"ext-{pid}",
        "can_checkout": checkout,
        "in_stock": checkout,
        "years": [],
        "weights": [],
        "family_key": family or family_key_from_title(title),
    }


def _family_ctx(*skus, inbound: str = ""):
    return {
        "platform_connected": True,
        "focus_product": None,
        "recommended_product_ids": [],
        "catalog_skus": list(skus),
        "kb_signals": [],
        "product_links": [],
    }


class TestVariantFamilyNotConflict:
    def test_mixed_checkout_family_is_variant_options(self):
        fam = "edition|series"
        ev = evaluate_product_availability_evidence(
            availability_context=_family_ctx(
                _sku(1, "Edition Series legacy", checkout=True, family=fam),
                _sku(2, "Edition Series current", checkout=False, family=fam),
            ),
            inbound_text="Edition Series legacy",
        )
        assert ev.evidence_state == EVIDENCE_VARIANT_OPTIONS

    def test_enforce_does_not_emit_conflict_canned_for_variants(self):
        fam = "edition|series"
        prev = os.environ.get("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE")
        os.environ["NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE"] = "enforce"
        try:
            reply = "المنتج متوفر حالياً بأكثر من نسخة."
            result = apply_product_availability_truth_guard(
                reply=reply,
                availability_context=_family_ctx(
                    _sku(1, "Edition Series legacy", checkout=True, family=fam),
                    _sku(2, "Edition Series current", checkout=False, family=fam),
                ),
                inbound_text="Edition Series legacy",
                tenant_id=1,
            )
            assert result.replaced is False
            assert result.reply == reply
            assert _CONFLICT_REPLY_AR not in result.reply
        finally:
            if prev is None:
                os.environ.pop("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE", None)
            else:
                os.environ["NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE"] = prev

    def test_true_kb_catalog_conflict_still_conflict(self):
        ev = evaluate_product_availability_evidence(
            availability_context={
                "platform_connected": True,
                "focus_product": {"id": 3, "title": "Line Item 2025"},
                "recommended_product_ids": [],
                "catalog_skus": [_sku(3, "Line Item 2025", checkout=False)],
                "kb_signals": [{
                    "section_id": 10,
                    "kind": "quick_update",
                    "avail_polarity": "positive",
                    "primary_year": "2025",
                    "linked_product_ids": [3],
                }],
                "product_links": [{
                    "section_id": 10,
                    "product_id": 3,
                    "source": "manual",
                    "confidence": None,
                }],
            },
        )
        assert ev.evidence_state == EVIDENCE_CONFLICT


class TestBrowseBreadth:
    def test_global_types_question_uses_multi_display(self):
        breadth = resolve_product_breadth(
            message="وش الأنواع المتوفرة؟",
            intent_name="general",
            intent_confidence=0.55,
            source="top_products",
            query="",
            stage="discovery",
            is_first_recommendation=True,
            total_available=5,
        )
        assert breadth.display_limit >= 2
        assert breadth.mode in {"broad", "browse"}

    def test_wesh_ghayriha_is_alternative_browse(self):
        assert browse_alternatives_requested("وش غيرها؟")
        breadth = resolve_product_breadth(
            message="وش غيرها",
            intent_name="general",
            intent_confidence=0.6,
            source="show_more",
            query="",
            stage="discovery",
            is_first_recommendation=False,
            total_available=4,
        )
        assert breadth.display_limit >= 2


class TestGuardInboundExempt:
    def test_delivery_question_exempt(self):
        assert inbound_exempt_from_availability_rewrite("كيف التوصيل للمدينة؟")

    def test_global_browse_exempt(self):
        assert inbound_exempt_from_availability_rewrite("وش الأنواع المتوفرة؟")

    def test_delivery_reply_not_replaced_with_conflict_canned(self):
        prev = os.environ.get("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE")
        os.environ["NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE"] = "enforce"
        try:
            reply = "نعم التوصيل متوفر لمعظم المدن."
            result = apply_product_availability_truth_guard(
                reply=reply,
                availability_context=_family_ctx(
                    _sku(1, "Widget A", checkout=True),
                ),
                inbound_text="كيف التوصيل للمدينة؟",
                tenant_id=1,
            )
            assert result.replaced is False
            assert _CONFLICT_REPLY_AR not in result.reply
        finally:
            if prev is None:
                os.environ.pop("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE", None)
            else:
                os.environ["NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE"] = prev


class TestClarifyRecovery:
    def test_global_browse_recovery_decision(self):
        from modules.ai.brain.product_discovery_gate import clarify_instead_of_top_products  # noqa: E402
        from modules.ai.brain.types import BrainContext, CommerceFacts, Intent, MerchantConversationState  # noqa: E402
        from modules.ai.brain.decision.actions import ACTION_SEARCH_PRODUCTS  # noqa: E402

        ctx = BrainContext(
            tenant_id=1,
            customer_phone="966500000001",
            message="وش الأنواع المتوفرة؟",
            intent=Intent(name="general", confidence=0.5, raw_message="وش الأنواع المتوفرة؟"),
            state=MerchantConversationState(turn=3),
            facts=CommerceFacts(has_products=True),
        )
        decision = clarify_instead_of_top_products(ctx, reason="weak_or_unknown_intent")
        assert decision.action == ACTION_SEARCH_PRODUCTS
        assert decision.args.get("source") == "global_browse_recovery"
