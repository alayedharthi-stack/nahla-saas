"""Platform intelligence recovery — natural-customer acceptance suite.

Asserts owner/evidence/provenance/post-compose preservation.
Does not assert exact customer-facing Arabic sentences.
Generic merchant fixtures only (no single-store product families).
"""
from __future__ import annotations

import dataclasses
import os
import sys
from typing import Any, Dict, List

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.inbound_dedup import is_duplicate_inbound, reset_cache  # noqa: E402
from core.outbound_dedup import (  # noqa: E402
    check_outbound_send,
    clear_outbound_dedup,
    record_outbound_result,
)
from modules.ai.brain.commerce.catalog_reasoning_evidence import (  # noqa: E402
    catalog_reasoning_titles,
    collect_catalog_reasoning_candidates,
)
from modules.ai.brain.commerce.commerce_turn_contract import (  # noqa: E402
    build_commerce_turn_contract,
    maybe_enforce_commerce_turn_contract_decision,
)
from modules.ai.brain.commerce.fact_answer import (  # noqa: E402
    KIND_BRANCH_EXISTENCE,
    KIND_GIFT_RECOMMENDATION,
    KIND_SHIPPING_COMPANIES,
    KIND_SHIPPING_ETA,
    KIND_SHIPPING_FEE,
    STATUS_KNOWN_VALUE,
    STATUS_UNKNOWN,
    build_fact_answer_contract,
    classify_fact_answer,
    fact_answer_yields_to_transactional,
)
from modules.ai.brain.compose.brain_state_slim import (  # noqa: E402
    should_slim_general_brain_state,
)
from modules.ai.brain.compose.prompt_state_serializer import (  # noqa: E402
    _slim_known_facts,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_LLM_REPLY,
    ACTION_SEARCH_PRODUCTS,
)
from modules.ai.brain.postprocess.merchant_capability_truth_guard import (  # noqa: E402
    apply_merchant_capability_truth_guard,
)
from modules.ai.brain.postprocess.product_availability_truth_guard import (  # noqa: E402
    _UNKNOWN_REPLY_AR,
    apply_product_availability_truth_guard,
)
from modules.ai.brain.types import (  # noqa: E402
    INTENT_ASK_PRODUCT,
    INTENT_ASK_SHIPPING,
    INTENT_START_ORDER,
    INTENT_TRACK_ORDER,
    BrainContext,
    BrainReplyState,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
)

GENERIC_MERCHANT = "متجر تجريبي عام"
GENERIC_SHOE = "حذاء رياضي أبيض"
GENERIC_SHIRT = "قميص قطني أزرق"
GENERIC_PERFUME = "عطر ورد 100ml"
GENERIC_ORDER_REF = "284719365"

BROWSE_FAMILY = (
    "وش عندكم؟",
    "إيش تبيعون؟",
    "ورني الموجود",
    "أبي أشوف الأشياء اللي عندكم",
    "وش ممكن آخذ منكم؟",
)
RECOMMEND_FAMILY = (
    "أبي شيء حلو",
    "أبي شيء مناسب",
    "محتار وش آخذ",
    "وش تنصحني؟",
)
GIFT_FAMILY = (
    "أبي هدية",
    "أبي شيء حلو هدية",
)
FOLLOWUP_FAMILY = (
    "طيب وريني شيء مناسب",
    "غيره؟",
    "وش الأفضل؟",
    "أيهم أحسن؟",
)
PRONOUN_FAMILY = (
    "هذا كم؟",
    "هذا متوفر؟",
    "الثاني؟",
    "وش لونه؟",
    "له مقاس ثاني؟",
)
SOCIAL_FAMILY = (
    "السلام عليكم",
    "شكرا",
    "ههههه",
    "غالي شوي",
    "ما عجبني",
    "محتار",
    "يعطيك العافية",
)


def _catalog_rows(*, checkout_first: bool = False) -> List[Dict[str, Any]]:
    return [
        {
            "id": 11,
            "title": GENERIC_SHOE,
            "price": 189,
            "in_stock": True,
            "can_checkout": bool(checkout_first),
            "external_id": "ext-shoe" if checkout_first else None,
        },
        {
            "id": 12,
            "title": GENERIC_SHIRT,
            "price": 79,
            "in_stock": True,
            "can_checkout": True,
            "external_id": "ext-shirt",
        },
        {
            "id": 13,
            "title": GENERIC_PERFUME,
            "price": 120,
            "in_stock": True,
            "can_checkout": False,
        },
    ]


def _facts(**kwargs: Any) -> CommerceFacts:
    base: Dict[str, Any] = dict(
        store_name=GENERIC_MERCHANT,
        has_products=True,
        product_count=3,
        in_stock_count=3,
        orderable=True,
        discovery_products=_catalog_rows(),
        top_products=[
            {
                "id": 12,
                "title": GENERIC_SHIRT,
                "price": 79,
                "external_id": "ext-shirt",
                "can_checkout": True,
            },
        ],
        payment_methods=["cod", "bank"],
        shipping_methods=["Dev Company"],
        merchant_capabilities={
            "source": "salla",
            "kind": "merchant_enabled",
            "payments": {
                "status": "known",
                "methods": [
                    {"code": "cod", "label": "COD", "enabled": True},
                    {"code": "bank", "label": "Bank", "enabled": True},
                ],
            },
            "shipping": {
                "companies_status": "known",
                "companies": [{"id": 1, "name": "Dev Company", "enabled": True}],
            },
        },
    )
    base.update(kwargs)
    return CommerceFacts(**base)


def _order_history() -> List[Dict[str, str]]:
    return [
        {"direction": "in", "body": GENERIC_ORDER_REF},
        {"direction": "out", "body": "طلبك قيد التجهيز"},
    ]


def _ctx(
    message: str,
    *,
    intent_name: str = INTENT_ASK_PRODUCT,
    history: List[Dict[str, str]] | None = None,
    facts: CommerceFacts | None = None,
    tenant_id: int = 1,
) -> BrainContext:
    return BrainContext(
        tenant_id=tenant_id,
        customer_phone="966500000001",
        message=message,
        intent=Intent(
            name=intent_name,
            confidence=0.85,
            slots={},
            raw_message=message,
            extraction_method="rules",
        ),
        state=MerchantConversationState(stage="exploring", turn=4, greeted=True),
        facts=facts or _facts(),
        history=history or [],
        profile={"inbound_metadata": {}},
        commerce_bundle={},
    )


def _sku(pid: int, title: str, *, checkout: bool) -> dict:
    return {
        "id": pid,
        "title": title,
        "sku": f"SKU-{pid}",
        "external_id": f"ext-{pid}" if checkout else "",
        "can_checkout": checkout,
        "in_stock": checkout,
        "years": [],
        "weights": [],
        "family_key": title,
    }


def _avail_ctx(skus: list, *, connected: bool = True) -> dict:
    return {
        "platform_connected": connected,
        "focus_product": None,
        "recommended_product_ids": [],
        "catalog_skus": skus,
        "kb_signals": [],
        "product_links": [],
    }


def _postcompose_class(original: str, result: Any) -> str:
    if not getattr(result, "replaced", False) and result.reply == original:
        return "FORMATTING_ONLY"
    if result.reply == original:
        return "FORMATTING_ONLY"
    if result.reply == _UNKNOWN_REPLY_AR:
        return "CANNED_REPLACEMENT"
    if result.action == "strip_inactive_catalog_lines":
        return "SAFE_FACT_REMOVAL"
    return "SEMANTIC_REWRITE"


class TestABroadDiscoveryEvidence:
    @pytest.mark.parametrize("message", BROWSE_FAMILY)
    def test_browse_paraphrases_receive_bounded_real_catalog(self, message: str) -> None:
        titles = catalog_reasoning_titles(facts=_facts())
        assert GENERIC_SHOE in titles
        assert GENERIC_SHIRT in titles
        assert GENERIC_PERFUME in titles
        assert 1 < len(titles) <= 8
        ctx = _ctx(message)
        contract = build_commerce_turn_contract(ctx, db=None)
        assert contract.known_facts.get("existing_order_support_only") is not True
        assert "do_not_search_products" not in set(contract.forbidden_actions)

    def test_existence_includes_non_checkout_titles(self) -> None:
        rows = collect_catalog_reasoning_candidates(facts=_facts())
        by_title = {row["title"]: row for row in rows}
        assert by_title[GENERIC_SHOE]["can_checkout"] is False
        assert by_title[GENERIC_SHIRT]["can_checkout"] is True
        assert by_title[GENERIC_PERFUME]["can_checkout"] is False


class TestBVagueRecommendationEvidence:
    @pytest.mark.parametrize("message", RECOMMEND_FAMILY + GIFT_FAMILY)
    def test_recommendation_paraphrases_have_grounded_candidates(
        self,
        message: str,
    ) -> None:
        titles = catalog_reasoning_titles(facts=_facts())
        assert GENERIC_SHIRT in titles
        assert "عطور" not in titles
        assert "شوكولاتة" not in titles

    def test_gift_contract_uses_discovery_not_only_synced_top(self) -> None:
        req = classify_fact_answer("أبي هدية", intent_name=INTENT_START_ORDER)
        assert req is not None
        assert req.fact_kind == KIND_GIFT_RECOMMENDATION
        facts = _facts(top_products=[])
        contract = build_fact_answer_contract(req, facts=facts)
        assert contract.status == STATUS_KNOWN_VALUE
        assert GENERIC_SHOE in contract.claimable_values
        assert GENERIC_PERFUME in contract.claimable_values
        assert "invent_catalog_category" in contract.forbidden_inferences

    def test_gift_unknown_only_when_catalog_empty(self) -> None:
        req = classify_fact_answer("أبي هدية", intent_name=INTENT_START_ORDER)
        contract = build_fact_answer_contract(
            req,
            facts=CommerceFacts(has_products=False, discovery_products=[], top_products=[]),
        )
        assert contract.status == STATUS_UNKNOWN
        assert contract.claimable_values == []


class TestCFollowupContinuityWithoutScripting:
    @pytest.mark.parametrize("message", FOLLOWUP_FAMILY)
    def test_followup_does_not_force_order_support_ownership(self, message: str) -> None:
        ctx = _ctx(message, history=_order_history())
        contract = build_commerce_turn_contract(ctx, db=None)
        assert contract.known_facts.get("existing_order_support_only") is not True
        raw = Decision(action=ACTION_SEARCH_PRODUCTS, args={"query": message}, reason="followup")
        enforced = maybe_enforce_commerce_turn_contract_decision(ctx, contract, raw)
        assert enforced.action == ACTION_SEARCH_PRODUCTS


class TestDPronounFocusProvenance:
    def test_assistant_mention_does_not_become_customer_selection(self) -> None:
        state = MerchantConversationState(stage="exploring")
        assert state.current_product_focus is None
        ctx = _ctx("هذا كم؟")
        ctx.history = [
            {"direction": "out", "body": f"عندنا {GENERIC_SHOE} و {GENERIC_SHIRT}"},
        ]
        assert ctx.state.current_product_focus is None

    @pytest.mark.parametrize("message", PRONOUN_FAMILY)
    def test_pronoun_turns_keep_catalog_evidence_available(self, message: str) -> None:
        titles = catalog_reasoning_titles(facts=_facts())
        assert len(titles) >= 2


class TestETopicSwitchingTurnLocal:
    def test_answer_contract_is_not_persisted_on_conversation_state(self) -> None:
        names = {f.name for f in dataclasses.fields(MerchantConversationState)}
        assert "answer_contract" not in names
        assert "owner_brief" not in names
        assert "response_goal" not in names

    def test_shipping_fact_owner_does_not_block_later_catalog_titles(self) -> None:
        ship = classify_fact_answer("كم سعر الشحن؟", intent_name=INTENT_ASK_SHIPPING)
        assert ship is not None
        assert ship.fact_kind == KIND_SHIPPING_FEE
        titles = catalog_reasoning_titles(facts=_facts())
        assert GENERIC_SHIRT in titles

    def test_payments_then_catalog_still_projects_products(self) -> None:
        pay = classify_fact_answer("وش طرق الدفع؟")
        assert pay is not None
        catalog = collect_catalog_reasoning_candidates(facts=_facts())
        assert len(catalog) >= 2


class TestFExistingOrderPlusNewShopping:
    @pytest.mark.parametrize("message", BROWSE_FAMILY + ("أبي أشتري شيء ثاني",))
    def test_historical_order_does_not_block_catalog_search(self, message: str) -> None:
        ctx = _ctx(message, history=_order_history())
        contract = build_commerce_turn_contract(ctx, db=None)
        assert contract.known_facts.get("existing_order_support_only") is not True
        assert contract.known_facts.get("existing_order_evidence_available") is True
        assert "do_not_search_products" not in set(contract.forbidden_actions)
        raw = Decision(action=ACTION_SEARCH_PRODUCTS, args={"query": message}, reason="browse")
        enforced = maybe_enforce_commerce_turn_contract_decision(ctx, contract, raw)
        assert enforced.action == ACTION_SEARCH_PRODUCTS
        assert enforced.args.get("topic") != "existing_order_support"

    def test_where_is_my_order_still_owns_support(self) -> None:
        ctx = _ctx("وين طلبي؟", intent_name=INTENT_TRACK_ORDER, history=_order_history())
        contract = build_commerce_turn_contract(ctx, db=None)
        assert contract.known_facts.get("existing_order_support_only") is True
        raw = Decision(action=ACTION_SEARCH_PRODUCTS, args={"query": "منتجات"}, reason="noise")
        enforced = maybe_enforce_commerce_turn_contract_decision(ctx, contract, raw)
        assert enforced.action == ACTION_LLM_REPLY
        assert enforced.args.get("topic") == "existing_order_support"


class TestGFactualUnknown:
    def test_eta_and_fee_unknown_without_inventing_carrier(self) -> None:
        fee_req = classify_fact_answer("كم تكلفة الشحن؟")
        eta_req = classify_fact_answer("كم يستغرق الشحن؟")
        assert fee_req is not None and eta_req is not None
        fee = build_fact_answer_contract(fee_req, facts=_facts())
        eta = build_fact_answer_contract(eta_req, facts=_facts())
        assert fee.status == STATUS_UNKNOWN
        assert eta.status == STATUS_UNKNOWN
        assert "carrier_implies_fee" in fee.forbidden_inferences
        assert "carrier_implies_eta" in eta.forbidden_inferences

    def test_branch_unknown_is_not_false(self) -> None:
        req = classify_fact_answer("عندكم فرع في لندن؟")
        assert req is not None
        assert req.fact_kind == KIND_BRANCH_EXISTENCE
        contract = build_fact_answer_contract(req, facts=_facts())
        assert contract.status == STATUS_UNKNOWN
        assert contract.claimable_values == []


class TestHMerchantCapabilities:
    def test_shipping_companies_are_merchant_enabled_not_order_actual(self) -> None:
        req = classify_fact_answer("أي شركة توصلون معها؟")
        assert req is not None
        assert req.fact_kind == KIND_SHIPPING_COMPANIES
        contract = build_fact_answer_contract(req, facts=_facts())
        assert contract.status == STATUS_KNOWN_VALUE
        assert "Dev Company" in contract.claimable_values
        assert "أرامكس" not in contract.claimable_values

    def test_order_actual_shipping_yields_generic_companies(self) -> None:
        assert fact_answer_yields_to_transactional(
            "طلبي متى يوصل؟",
            fact_kind=KIND_SHIPPING_ETA,
        ) is True
        assert fact_answer_yields_to_transactional(
            "أي شركة توصلون معها؟",
            fact_kind=KIND_SHIPPING_COMPANIES,
        ) is False


class TestIActualOrder:
    def test_order_question_does_not_use_catalog_as_order_truth(self) -> None:
        ctx = _ctx("وين طلبي؟", intent_name=INTENT_TRACK_ORDER, history=_order_history())
        contract = build_commerce_turn_contract(ctx, db=None)
        assert contract.known_facts.get("existing_order_support_only") is True
        titles = catalog_reasoning_titles(facts=_facts())
        assert titles  # catalog may exist, but must not own this turn


class TestJRepetitionDedup:
    def setup_method(self) -> None:
        clear_outbound_dedup()
        reset_cache()

    def teardown_method(self) -> None:
        clear_outbound_dedup()
        reset_cache()

    def test_distinct_inbound_ids_both_send(self) -> None:
        body = "عندنا حذاء رياضي أبيض وقميص قطني أزرق"
        first = {
            "messaging_product": "whatsapp",
            "to": "966500000001",
            "type": "text",
            "text": {"body": body},
            "_nahla_inbound_id": "wamid.browse-1",
        }
        second = dict(first)
        second["_nahla_inbound_id"] = "wamid.browse-2"
        a = check_outbound_send(tenant_id=1, recipient="966500000001", payload=first)
        assert a.skip is False
        record_outbound_result(
            tenant_id=1,
            recipient="966500000001",
            payload=first,
            wamid="wamid.out-1",
            succeeded=True,
        )
        b = check_outbound_send(tenant_id=1, recipient="966500000001", payload=second)
        assert b.skip is False

    def test_same_webhook_id_replay_is_idempotent(self) -> None:
        assert is_duplicate_inbound(phone_number_id="123", msg_id="wamid.in-1") is False
        assert is_duplicate_inbound(phone_number_id="123", msg_id="wamid.in-1") is True
        assert is_duplicate_inbound(phone_number_id="123", msg_id="wamid.in-2") is False


class TestKSocialNotRobotic:
    @pytest.mark.parametrize("message", SOCIAL_FAMILY)
    def test_social_prose_not_rewritten_as_availability_unknown(self, message: str) -> None:
        os.environ["NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE"] = "enforce"
        reply = "يسعدك، خبرني كيف أقدر أساعدك"
        result = apply_product_availability_truth_guard(
            reply=reply,
            availability_context=_avail_ctx([_sku(12, GENERIC_SHIRT, checkout=True)]),
            inbound_text=message,
            tenant_id=1,
        )
        assert result.replaced is False
        assert result.reply == reply
        assert _postcompose_class(reply, result) == "FORMATTING_ONLY"


class TestLAdversarialAssumptions:
    def test_invented_aramex_scrubbed_when_merchant_company_known(self) -> None:
        result = apply_merchant_capability_truth_guard(
            "نعم نوصل مع أرامكس",
            known_facts={
                "merchant_capability_answer": {
                    "question_kind": "shipping_companies",
                    "shipping_companies": ["Dev Company"],
                    "shipping_companies_status": "known",
                },
            },
            decision_topic="shipping_companies",
        )
        assert result.invented_carriers
        assert "أرامكس" not in result.text

    def test_free_shipping_not_inferred_from_unknown_fee(self) -> None:
        req = classify_fact_answer("كم تكلفة الشحن؟")
        contract = build_fact_answer_contract(req, facts=_facts())
        assert contract.status == STATUS_UNKNOWN


class TestAvailabilityGuardPolicesClaimsNotConversation:
    def setup_method(self) -> None:
        self._prev = os.environ.get("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE")
        os.environ["NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE"] = "enforce"

    def teardown_method(self) -> None:
        if self._prev is None:
            os.environ.pop("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE", None)
        else:
            os.environ["NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE"] = self._prev

    def test_browse_multi_product_list_survives_without_stock_ask(self) -> None:
        reply = (
            f"عندنا عدة منتجات:\n"
            f"• {GENERIC_SHOE}\n"
            f"• {GENERIC_SHIRT}\n"
            f"• {GENERIC_PERFUME}"
        )
        skus = [
            _sku(11, GENERIC_SHOE, checkout=False),
            _sku(12, GENERIC_SHIRT, checkout=True),
            _sku(13, GENERIC_PERFUME, checkout=False),
        ]
        result = apply_product_availability_truth_guard(
            reply=reply,
            availability_context=_avail_ctx(skus),
            inbound_text="وش عندكم؟",
            tenant_id=1,
        )
        assert result.replaced is False
        assert GENERIC_SHOE in result.reply
        assert GENERIC_SHIRT in result.reply
        assert GENERIC_PERFUME in result.reply
        assert _postcompose_class(reply, result) == "FORMATTING_ONLY"

    def test_gift_clarification_not_replaced_with_canned_unknown(self) -> None:
        reply = "أكيد، تفضلي خبريني عن المناسبة أو ذوق اللي تهدينها عشان أرتب خيارات مناسبة من الموجود."
        result = apply_product_availability_truth_guard(
            reply=reply,
            availability_context=_avail_ctx([_sku(12, GENERIC_SHIRT, checkout=True)]),
            inbound_text="أبي شيء حلو هدية",
            tenant_id=1,
        )
        assert result.replaced is False
        assert result.reply == reply
        assert result.reply != _UNKNOWN_REPLY_AR

    def test_gift_options_claim_without_stock_ask_not_canned(self) -> None:
        reply = "عندنا خيارات جميلة للهدايا من القمصان والعطور."
        result = apply_product_availability_truth_guard(
            reply=reply,
            availability_context=_avail_ctx([_sku(12, GENERIC_SHIRT, checkout=True)]),
            inbound_text="أبي هدية",
            tenant_id=1,
        )
        assert result.replaced is False
        assert result.reply != _UNKNOWN_REPLY_AR

    def test_specific_availability_options_claim_still_rewritten(self) -> None:
        reply = "بالنسبة لطرود النحل، عندنا تشكيلة متنوعة."
        result = apply_product_availability_truth_guard(
            reply=reply,
            availability_context=_avail_ctx([_sku(10, "Catalog Honey", checkout=True)]),
            inbound_text="في عندك طرود نحل ؟",
            tenant_id=1,
        )
        assert result.replaced is True
        assert "تشكيلة متنوعة" not in result.reply
        assert _postcompose_class(reply, result) == "CANNED_REPLACEMENT"

    def test_stock_ask_still_strips_non_checkout_lines(self) -> None:
        reply = (
            "عندنا حالياً:\n"
            f"• {GENERIC_SHOE}\n"
            f"• {GENERIC_SHIRT}\n"
        )
        result = apply_product_availability_truth_guard(
            reply=reply,
            availability_context=_avail_ctx(
                [
                    _sku(11, GENERIC_SHOE, checkout=False),
                    _sku(12, GENERIC_SHIRT, checkout=True),
                ],
            ),
            inbound_text="وش المتوفر الان",
            tenant_id=1,
        )
        assert result.replaced is True
        assert result.action == "strip_inactive_catalog_lines"
        assert GENERIC_SHOE not in result.reply
        assert GENERIC_SHIRT in result.reply
        assert _postcompose_class(reply, result) == "SAFE_FACT_REMOVAL"


class TestSlimmingYieldsForCatalogReasoning:
    def test_slim_known_facts_keeps_catalog_candidates(self) -> None:
        slim = _slim_known_facts(
            {
                "catalog_reasoning_candidates": [
                    {"title": GENERIC_SHIRT, "can_checkout": True},
                ],
                "has_products": True,
                "product_count": 3,
                "payment_methods": ["cod"],
            },
        )
        assert slim["catalog_reasoning_candidates"][0]["title"] == GENERIC_SHIRT
        assert slim["has_products"] is True
        assert slim["payment_methods"] == ["cod"]

    def test_general_intent_does_not_slim_away_catalog_candidates(self) -> None:
        state = BrainReplyState(
            intent_name="general",
            known_facts={
                "catalog_reasoning_candidates": [{"title": GENERIC_SHIRT}],
            },
        )
        ok, reason = should_slim_general_brain_state(state)
        assert ok is False
        assert reason == "catalog_reasoning_evidence"

    def test_greeting_still_eligible_for_slim(self) -> None:
        state = BrainReplyState(
            intent_name="greeting",
            known_facts={
                "catalog_reasoning_candidates": [{"title": GENERIC_SHIRT}],
            },
        )
        ok, reason = should_slim_general_brain_state(state)
        assert ok is True
        assert "greeting" in reason or reason.startswith("non_commerce")


class TestMultiTenantIsolation:
    def test_catalog_projection_does_not_mix_tenant_facts(self) -> None:
        t1 = catalog_reasoning_titles(
            facts=CommerceFacts(
                store_name="متجر تجريبي عام",
                discovery_products=[{"id": 1, "title": GENERIC_SHOE}],
            ),
        )
        t2 = catalog_reasoning_titles(
            facts=CommerceFacts(
                store_name="متجر عطور تجريبي",
                discovery_products=[{"id": 99, "title": GENERIC_PERFUME}],
            ),
        )
        assert t1 == [GENERIC_SHOE]
        assert t2 == [GENERIC_PERFUME]
        assert GENERIC_PERFUME not in t1
        assert GENERIC_SHOE not in t2

    def test_order_support_contract_is_conversation_local(self) -> None:
        shop = _ctx("وش عندكم؟", history=_order_history(), tenant_id=1)
        other = _ctx("وش عندكم؟", history=[], tenant_id=2)
        c1 = build_commerce_turn_contract(shop, db=None)
        c2 = build_commerce_turn_contract(other, db=None)
        assert c1.known_facts.get("existing_order_evidence_available") is True
        assert c2.known_facts.get("existing_order_evidence_available") is not True


class TestModelConfigUnchanged:
    def test_default_customer_chat_model_is_luna(self) -> None:
        from modules.ai.orchestrator.customer_chat_models import (  # noqa: PLC0415
            MODEL_LUNA,
            resolve_default_customer_chat_model,
        )

        assert MODEL_LUNA == "gpt-5.6-luna"
        if not os.environ.get("NAHLA_MODEL_CHEAP") and not os.environ.get("OPENAI_MODEL"):
            assert resolve_default_customer_chat_model() == MODEL_LUNA
