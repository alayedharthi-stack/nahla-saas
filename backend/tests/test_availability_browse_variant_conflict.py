"""PR1 — availability variants, browse breadth, no canned conflict rewrite."""
from __future__ import annotations

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.product_entity_resolution import (  # noqa: E402
    direct_product_availability_ask,
    family_key_from_title,
    resolve_availability_entity,
)
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
    EVIDENCE_RESOLVED_AVAILABLE,
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


class TestDirectFamilyAvailabilityAsk:
    """PR #88 follow-up — «هل X متوفر؟» must not conflict on variant families."""

    def test_direct_ask_detected(self):
        assert direct_product_availability_ask("هل السمر متوفر؟")
        assert direct_product_availability_ask("Edition Series متوفر؟")
        assert not direct_product_availability_ask("وش أنواع السمر عندكم؟")

    def test_single_token_inbound_resolves_inbound_family(self):
        skus = [
            _sku(1, "Edition Series legacy harvest", checkout=True, family="a|b|legacy"),
            _sku(2, "Edition Series current harvest", checkout=False, family="a|b|current"),
        ]
        entity = resolve_availability_entity(
            focus_product=None,
            recommended_product_ids=[],
            inbound_text="هل Edition Series متوفر؟",
            catalog_skus=skus,
        )
        assert entity.resolution_mode == "inbound_family"
        assert len(entity.candidate_product_ids) == 2

    def test_arabic_definite_article_inbound_resolves_inbound_family(self):
        """«هل السمر متوفر؟» must match catalog titles tokenized as «سمر»."""
        inbound = "\u0647\u0644 \u0627\u0644\u0633\u0645\u0631 \u0645\u062a\u0648\u0641\u0631\u061f"
        skus = [
            _sku(11, "\u0639\u0633\u0644 \u0633\u0645\u0631 \u0627\u0644\u062d\u062c\u0627\u0632 \u0625\u0646\u062a\u0627\u062c \u0642\u062f\u064a\u0645", checkout=True, family="a|b"),
            _sku(12, "\u0639\u0633\u0644 \u0633\u0645\u0631 \u0627\u0644\u062d\u062c\u0627\u0632 \u0627\u0646\u062a\u0627\u062c 1446", checkout=False, family="c|d"),
        ]
        entity = resolve_availability_entity(
            focus_product=None,
            recommended_product_ids=[],
            inbound_text=inbound,
            catalog_skus=skus,
        )
        assert entity.resolution_mode == "inbound_family"
        assert len(entity.candidate_product_ids) == 2

        ctx = _family_ctx(*skus, inbound=inbound)
        ctx["kb_signals"] = [{
            "section_id": 185,
            "kind": "quick_update",
            "avail_polarity": "positive",
            "primary_year": "1447",
            "linked_product_ids": [],
        }]
        ev = evaluate_product_availability_evidence(
            availability_context=ctx,
            inbound_text=inbound,
        )
        assert ev.evidence_state == EVIDENCE_VARIANT_OPTIONS
        assert ev.conflict_type is None

        prev = os.environ.get("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE")
        os.environ["NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE"] = "enforce"
        try:
            result = apply_product_availability_truth_guard(
                reply="\u0646\u0639\u0645\u060c \u0645\u062a\u0648\u0641\u0631 \u062d\u0627\u0644\u064a\u0627\u064b.",
                availability_context=ctx,
                inbound_text=inbound,
                tenant_id=1,
            )
            assert result.replaced is False
            assert _CONFLICT_REPLY_AR not in result.reply
        finally:
            if prev is None:
                os.environ.pop("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE", None)
            else:
                os.environ["NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE"] = prev

    def test_direct_ask_mixed_variants_not_conflict_canned(self):
        skus = [
            _sku(1, "Edition Series legacy harvest", checkout=True, family="a|b|legacy"),
            _sku(2, "Edition Series current harvest", checkout=False, family="a|b|current"),
        ]
        ctx = _family_ctx(*skus, inbound="هل Edition Series متوفر؟")
        ctx["kb_signals"] = [{
            "section_id": 99,
            "kind": "quick_update",
            "avail_polarity": "positive",
            "primary_year": "2099",
            "linked_product_ids": [],
        }]
        ev = evaluate_product_availability_evidence(
            availability_context=ctx,
            inbound_text="هل Edition Series متوفر؟",
        )
        assert ev.evidence_state == EVIDENCE_VARIANT_OPTIONS

        prev = os.environ.get("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE")
        os.environ["NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE"] = "enforce"
        try:
            reply = "نعم، متوفر حالياً بأكثر من نسخة."
            result = apply_product_availability_truth_guard(
                reply=reply,
                availability_context=ctx,
                inbound_text="هل Edition Series متوفر؟",
                tenant_id=1,
            )
            assert result.replaced is False
            assert _CONFLICT_REPLY_AR not in result.reply
        finally:
            if prev is None:
                os.environ.pop("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE", None)
            else:
                os.environ["NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE"] = prev

    def test_types_ask_still_lists_variants(self):
        skus = [
            _sku(1, "Edition Series legacy harvest", checkout=True, family="edition|series"),
            _sku(2, "Edition Series current harvest", checkout=False, family="edition|series"),
        ]
        ev = evaluate_product_availability_evidence(
            availability_context=_family_ctx(*skus, inbound="وش أنواع Edition Series عندكم؟"),
            inbound_text="وش أنواع Edition Series عندكم؟",
        )
        assert ev.evidence_state in {EVIDENCE_VARIANT_OPTIONS, EVIDENCE_RESOLVED_AVAILABLE}


class TestRepeatAvailabilityDedupBypass:
    def test_repeat_after_guard_rewrite_unlocks(self):
        from modules.ai.brain.commerce.dedup_operational_delta import (  # noqa: E402
            should_bypass_hard_dedup_repeat_availability,
        )
        from modules.ai.brain.postprocess.product_availability_truth_guard import (  # noqa: E402
            _CONFLICT_REPLY_AR,
        )

        assert should_bypass_hard_dedup_repeat_availability(
            "هل Edition Series متوفر؟",
            _CONFLICT_REPLY_AR,
        )
        assert not should_bypass_hard_dedup_repeat_availability(
            "هل Edition Series متوفر؟",
            "Edition Series متوفر بصنفين مختلفين.",
        )
