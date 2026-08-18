"""Family 3 — catalog referent, structured action/media, Brain-owned wording.

Asserts identity, capability, and completion — not exact customer Arabic.
"""
from __future__ import annotations

import asyncio
import inspect
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
_REPO = os.path.abspath(os.path.join(_BACKEND, ".."))
for _p in (_BACKEND, os.path.join(_REPO, "database"), _REPO):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.commerce.assistant_presented_provenance import (  # noqa: E402
    apply_turn_catalog_referent_binding,
    stamp_structured_presented_products,
    structured_product_from_turn,
    structured_selected_referent,
)
from modules.ai.brain.commerce.catalog_body_policy import (  # noqa: E402
    MINIMAL_CATALOG_BODY,
    TECHNICAL_CATALOG_BODY,
    resolve_native_catalog_body_text,
)
from modules.ai.brain.commerce.commerce_focus_owner import (  # noqa: E402
    archive_current_product_focus,
    bind_structured_catalog_referent,
    canonical_product_referent,
    has_structured_catalog_identity,
    product_focus_identity,
    set_product_focus,
    should_preserve_focus_after_product_list_display,
)
from modules.ai.brain.commerce.visual_delivery_capability import (  # noqa: E402
    collect_visual_delivery_capability,
    try_visual_catalog_send_decision,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_CLARIFY,
    ACTION_LLM_REPLY,
    ACTION_SEARCH_PRODUCTS,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.intent.classifier import (  # noqa: E402
    DefaultIntentClassifier,
    PROVENANCE_COMPATIBLE_BROADER_EVIDENCE,
    PROVENANCE_LAYER2_SEMANTIC_OVERRIDE,
    PROVENANCE_RULE_CANDIDATE_CONFIRMED,
    PROVENANCE_RULE_FALLBACK_AFTER_NO_AUTHORITATIVE_LAYER2,
    RULES_ONLY_THRESHOLD,
    SEMANTIC_RELATION_AUTHORITATIVE_OVERRIDE,
    SEMANTIC_RELATION_COMPATIBLE_BROADER,
    SEMANTIC_RELATION_NON_AUTHORITATIVE,
    SEMANTIC_RELATION_NO_AUTHORITATIVE,
    _BRAIN_SEMANTIC_REQUIRED_INTENTS,
    is_authoritative_layer2_intent,
    layer2_is_compatible_broader_evidence,
)
from modules.ai.brain.intent import rules as intent_rules  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    INTENT_ASK_OWNER_CONTACT,
    INTENT_ASK_PRODUCT,
    INTENT_GENERAL,
    INTENT_PRODUCT_VISUAL_REQUEST,
    INTENT_START_ORDER,
    INTENT_TRACK_ORDER,
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
)
from modules.ai.media.customer_turn_completion import (  # noqa: E402
    COMPLETION_ORPHAN,
    COMPLETION_STRUCTURED_VISIBLE,
    native_catalog_send_completion,
)

GENERIC_MERCHANT = "متجر تجريبي عام"
SHOE = {
    "id": 501,
    "external_id": "sku-white-sneaker",
    "title": "حذاء رياضي أبيض",
    "price": 189,
    "currency": "SAR",
    "in_stock": True,
    "can_checkout": True,
    "image_url": "https://cdn.example.test/shoe.jpg",
    "product_url": "https://shop.example.test/p/shoe",
    "provenance": "catalog_db",
}
SHIRT = {
    "id": 502,
    "external_id": "sku-blue-shirt",
    "title": "قميص قطني أزرق",
    "price": 120,
    "currency": "SAR",
    "in_stock": True,
    "can_checkout": True,
    "image_url": "https://cdn.example.test/shirt.jpg",
    "provenance": "catalog_db",
}
PERFUME = {
    "id": 503,
    "external_id": "sku-rose-100",
    "title": "عطر ورد 100ml",
    "price": 95,
    "currency": "SAR",
    "in_stock": True,
    "can_checkout": True,
    "image_url": "https://cdn.example.test/perfume.jpg",
    "provenance": "catalog_db",
}


def _state(**kwargs) -> MerchantConversationState:
    payload = dict(stage="exploring", turn=4, greeted=True)
    payload.update(kwargs)
    return MerchantConversationState(**payload)


def _facts(*rows: dict) -> CommerceFacts:
    products = list(rows) or [dict(SHOE), dict(SHIRT)]
    return CommerceFacts(
        store_name=GENERIC_MERCHANT,
        has_products=True,
        product_count=len(products),
        discovery_products=products,
        top_products=products,
    )


def _ctx(
    *,
    intent_name: str,
    state: MerchantConversationState,
    facts: CommerceFacts | None = None,
    message: str = "follow-up",
    tenant_id: int = 9001,
) -> BrainContext:
    return BrainContext(
        tenant_id=tenant_id,
        customer_phone="966500000001",
        message=message,
        intent=Intent(name=intent_name, confidence=0.9, slots={}),
        state=state,
        facts=facts or _facts(),
        history=[],
        profile={"inbound_metadata": {}},
        commerce_bundle={},
    )


class TestF301StructuredIdentityBinding:
    def test_recommendation_binds_catalog_id_not_title_substring(self) -> None:
        state = _state()
        bound = bind_structured_catalog_referent(
            state,
            dict(SHOE),
            reason="structured_turn_product",
            turn=4,
        )
        assert bound is not None
        assert product_focus_identity(state.current_product_focus) == "sku-white-sneaker"
        assert state.current_product_focus["id"] == 501
        assert "حذاء" not in product_focus_identity(state.current_product_focus)

    def test_turn_binding_prefers_structured_product_over_reply_text(self) -> None:
        state = _state()
        apply_turn_catalog_referent_binding(
            state=state,
            reply="I like the other one actually",
            catalog_candidates=[dict(SHOE), dict(SHIRT)],
            turn=5,
            structured_product=dict(SHIRT),
        )
        assert canonical_product_referent(state)["id"] == 502
        assert canonical_product_referent(state)["external_id"] == "sku-blue-shirt"


class TestF302LaterMediaNeedResolvesSameReferent:
    def test_visual_decision_targets_bound_referent(self) -> None:
        state = _state()
        bind_structured_catalog_referent(state, dict(SHOE), reason="recommend", turn=4)
        ctx = _ctx(
            intent_name=INTENT_PRODUCT_VISUAL_REQUEST,
            state=state,
            facts=_facts(SHOE, SHIRT),
            message="follow-up media need",
        )
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_SEARCH_PRODUCTS
        replay = list(decision.args.get("replay_candidates") or [])
        assert replay[0]["id"] == 501
        assert replay[0]["external_id"] == "sku-white-sneaker"


class TestF303F304CheckoutSelectedOutranksDiscovery:
    def test_family_2_selected_not_overwritten_by_recommendation(self) -> None:
        state = _state()
        selected = dict(SHOE)
        selected["customer_selected"] = True
        selected["from_catalog_order"] = True
        selected["provenance"] = "catalog_order_selected"
        stamp_structured_presented_products(
            state,
            [selected],
            provenance="catalog_order_selected",
            customer_selected=True,
            turn=2,
        )
        bind_structured_catalog_referent(
            state,
            selected,
            reason="catalog_order_selected",
            turn=2,
            customer_selected=True,
        )
        bind_structured_catalog_referent(
            state,
            dict(SHIRT),
            reason="assistant_recommended_structured",
            turn=5,
        )
        ref = structured_selected_referent(state)
        canon = canonical_product_referent(state, checkout_active=True)
        assert ref["id"] == 501
        assert canon["id"] == 501
        assert product_focus_identity(state.current_product_focus) == "sku-white-sneaker"

    def test_product_list_display_does_not_erase_checkout_selected(self) -> None:
        focus = {
            "id": 501,
            "external_id": "sku-white-sneaker",
            "title": SHOE["title"],
            "customer_selected": True,
            "from_catalog_order": True,
            "provenance": "catalog_order_selected",
        }
        state = _state()
        state.current_product_focus = dict(focus)
        stamp_structured_presented_products(
            state,
            [focus],
            provenance="catalog_order_selected",
            customer_selected=True,
            turn=2,
        )
        assert should_preserve_focus_after_product_list_display(
            state.current_product_focus,
            [dict(SHIRT), dict(PERFUME)],
            state=state,
        )
        if not should_preserve_focus_after_product_list_display(
            state.current_product_focus,
            [dict(SHIRT), dict(PERFUME)],
            state=state,
        ):
            archive_current_product_focus(state, reason="product_list_display")
        assert product_focus_identity(state.current_product_focus) == "sku-white-sneaker"


class TestF305F306StructuredActionTargetsReferent:
    def test_does_not_send_first_imageable_candidate(self) -> None:
        state = _state()
        bind_structured_catalog_referent(state, dict(SHIRT), reason="recommend", turn=4)
        ctx = _ctx(
            intent_name=INTENT_PRODUCT_VISUAL_REQUEST,
            state=state,
            facts=_facts(SHOE, SHIRT),
        )
        decision = try_visual_catalog_send_decision(ctx)
        assert decision is not None
        replay = list(decision.args.get("replay_candidates") or [])
        assert len(replay) == 1
        assert replay[0]["id"] == 502
        assert replay[0]["id"] != SHOE["id"]

    def test_no_referent_does_not_execute_first_sku_card(self) -> None:
        state = _state()
        ctx = _ctx(
            intent_name=INTENT_PRODUCT_VISUAL_REQUEST,
            state=state,
            facts=_facts(SHOE, SHIRT),
        )
        assert try_visual_catalog_send_decision(ctx) is None

    def test_referent_plus_capability_emits_structured_visual_action(self) -> None:
        state = _state()
        bind_structured_catalog_referent(state, dict(SHOE), reason="recommend", turn=4)
        cap = collect_visual_delivery_capability(facts=_facts(SHOE, SHIRT), state=state)
        assert cap["available"] is True
        ctx = _ctx(intent_name=INTENT_PRODUCT_VISUAL_REQUEST, state=state, facts=_facts(SHOE, SHIRT))
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_SEARCH_PRODUCTS
        assert decision.args.get("force_product_card") is True
        assert decision.args.get("after_search") == "product_visual"


class TestF307F308NoPhraseRouterOrCannedClarify:
    def test_action_uses_intent_plus_referent_not_raw_arabic_match(self) -> None:
        state = _state()
        bind_structured_catalog_referent(state, dict(SHOE), reason="recommend", turn=4)
        ctx = _ctx(
            intent_name=INTENT_PRODUCT_VISUAL_REQUEST,
            state=state,
            facts=_facts(SHOE),
            message="please show that item",
        )
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_SEARCH_PRODUCTS
        assert decision.args["replay_candidates"][0]["id"] == 501

    def test_missing_referent_is_brain_owned_not_templates_clarify(self) -> None:
        state = _state()
        ctx = _ctx(intent_name=INTENT_PRODUCT_VISUAL_REQUEST, state=state, facts=_facts())
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_LLM_REPLY
        assert decision.action != ACTION_CLARIFY
        question = str((decision.args or {}).get("question") or "")
        assert "أي منتج" not in question
        assert decision.args.get("topic") == "product_visual"
        assert decision.args.get("missing_structured_referent") is True


class TestF3B1BrainOwnsProductVisualSemanticIntent:
    # Existing visual-need phrasings used only as classifier input.
    # Assertions are owner/provenance, not a specific customer sentence.
    _MEDIA_NEED_SAMPLES = ("وين الصوره", "ورني شكله")
    # Non-visual strong rule that still reaches Layer 2 (conf < 0.85).
    _NON_VISUAL_LAYER2_MESSAGE = "show me the product image"

    def _classify_with_layer2(self, message: str, layer2_slots: dict) -> Intent:
        slot_extract = AsyncMock(return_value=dict(layer2_slots))

        async def _run() -> Intent:
            with patch(
                "modules.ai.brain.intent.classifier._slot_mod.extract_slots",
                slot_extract,
            ):
                return await DefaultIntentClassifier().classify(
                    message,
                    [],
                    _state(),
                )

        intent = asyncio.run(_run())
        assert slot_extract.await_count == 1
        return intent

    def test_classifier_does_not_rules_only_short_circuit_visual(self) -> None:
        assert INTENT_PRODUCT_VISUAL_REQUEST in _BRAIN_SEMANTIC_REQUIRED_INTENTS
        for sample in self._MEDIA_NEED_SAMPLES:
            candidate = intent_rules.match(sample)
            assert candidate is not None
            assert candidate.name == INTENT_PRODUCT_VISUAL_REQUEST
            assert candidate.extraction_method == "rules"
            assert candidate.confidence >= RULES_ONLY_THRESHOLD

            intent = self._classify_with_layer2(sample, {"intent_hint": INTENT_GENERAL})
            assert intent.extraction_method != "rules"
            assert intent.extraction_method == "hybrid"
            assert intent.slots.get("semantic_owner") == "brain_classifier"
            assert intent.name == INTENT_PRODUCT_VISUAL_REQUEST

    def test_f3_b1_p1_authoritative_layer2_start_order_wins(self) -> None:
        sample = self._MEDIA_NEED_SAMPLES[0]
        candidate = intent_rules.match(sample)
        assert candidate is not None
        assert candidate.name == INTENT_PRODUCT_VISUAL_REQUEST
        intent = self._classify_with_layer2(
            sample, {"intent_hint": INTENT_START_ORDER}
        )
        assert intent.name == INTENT_START_ORDER
        assert intent.name != INTENT_PRODUCT_VISUAL_REQUEST
        assert intent.extraction_method == "llm"
        assert intent.slots.get("classification_provenance") == (
            PROVENANCE_LAYER2_SEMANTIC_OVERRIDE
        )
        assert intent.slots.get("precedence_winner") == "layer2"
        assert intent.slots.get("rule_candidate") == INTENT_PRODUCT_VISUAL_REQUEST
        assert intent.slots.get("layer2_result") == INTENT_START_ORDER

    def test_f3_b1_p2_authoritative_layer2_ask_owner_contact_wins(self) -> None:
        sample = self._MEDIA_NEED_SAMPLES[0]
        intent = self._classify_with_layer2(
            sample, {"intent_hint": INTENT_ASK_OWNER_CONTACT}
        )
        assert intent.name == INTENT_ASK_OWNER_CONTACT
        assert intent.name != INTENT_PRODUCT_VISUAL_REQUEST
        assert intent.extraction_method == "llm"
        assert intent.slots.get("classification_provenance") == (
            PROVENANCE_LAYER2_SEMANTIC_OVERRIDE
        )
        assert intent.slots.get("precedence_winner") == "layer2"

    def test_f3_b1_p3_general_does_not_erase_visual_need(self) -> None:
        sample = self._MEDIA_NEED_SAMPLES[1]
        intent = self._classify_with_layer2(
            sample, {"intent_hint": INTENT_GENERAL}
        )
        assert intent.name == INTENT_PRODUCT_VISUAL_REQUEST
        assert intent.extraction_method == "hybrid"
        assert intent.slots.get("classification_provenance") == (
            PROVENANCE_RULE_CANDIDATE_CONFIRMED
        )
        assert intent.slots.get("precedence_winner") == "rule_candidate"
        assert is_authoritative_layer2_intent(INTENT_GENERAL) is False

    def test_f3_b1_p4_empty_layer2_is_not_rules_only_owner(self) -> None:
        sample = self._MEDIA_NEED_SAMPLES[0]
        intent = self._classify_with_layer2(sample, {})
        assert intent.extraction_method != "rules"
        assert intent.extraction_method == "hybrid"
        assert intent.name == INTENT_PRODUCT_VISUAL_REQUEST
        assert intent.slots.get("semantic_owner") == "brain_classifier"
        assert intent.slots.get("classification_provenance") == (
            PROVENANCE_RULE_FALLBACK_AFTER_NO_AUTHORITATIVE_LAYER2
        )
        assert intent.slots.get("precedence_winner") == "rule_candidate"

    def test_f3_b1_p5_semantic_provenance_distinguishes_winner(self) -> None:
        sample = self._MEDIA_NEED_SAMPLES[0]
        confirmed = self._classify_with_layer2(
            sample, {"intent_hint": INTENT_GENERAL}
        )
        override = self._classify_with_layer2(
            sample, {"intent_hint": INTENT_START_ORDER}
        )
        fallback = self._classify_with_layer2(sample, {})
        assert confirmed.slots.get("classification_provenance") == (
            PROVENANCE_RULE_CANDIDATE_CONFIRMED
        )
        assert override.slots.get("classification_provenance") == (
            PROVENANCE_LAYER2_SEMANTIC_OVERRIDE
        )
        assert fallback.slots.get("classification_provenance") == (
            PROVENANCE_RULE_FALLBACK_AFTER_NO_AUTHORITATIVE_LAYER2
        )
        assert confirmed.extraction_method == "hybrid"
        assert override.extraction_method == "llm"
        assert fallback.extraction_method == "hybrid"

    def test_f3_b1_p6_no_phrase_regex_or_visual_operational_allowlist(self) -> None:
        from modules.ai.brain.intent import classifier as intent_classifier
        from modules.ai.brain.intent import rules as rules_mod

        classifier_src = inspect.getsource(intent_classifier)
        resolve_src = inspect.getsource(intent_classifier._resolve_layer2_rule_precedence)
        auth_src = inspect.getsource(intent_classifier.is_authoritative_layer2_intent)
        assert "re.compile" not in classifier_src
        assert r"(?:show|send)" not in classifier_src
        assert "_PRODUCT_VISUAL_LLM_OVERRIDE" not in classifier_src
        assert INTENT_START_ORDER not in resolve_src
        assert INTENT_ASK_OWNER_CONTACT not in resolve_src
        assert INTENT_START_ORDER not in auth_src
        assert INTENT_ASK_OWNER_CONTACT not in auth_src
        visual_rule = next(
            rs for rs, _compiled in rules_mod._RULES
            if rs.intent == INTENT_PRODUCT_VISUAL_REQUEST
        )
        assert len(visual_rule.patterns) == 8

    def test_f3_b1_p7_precedence_is_not_tenant_or_phone_specific(self) -> None:
        from modules.ai.brain.intent import classifier as intent_classifier

        classify_src = inspect.getsource(intent_classifier.DefaultIntentClassifier.classify)
        resolve_src = inspect.getsource(intent_classifier._resolve_layer2_rule_precedence)
        for src in (classify_src, resolve_src):
            assert "tenant_id" not in src
            assert "customer_phone" not in src
            assert "phone_number" not in src
            assert "merchant_id" not in src

    def test_f3_b1_p8_canonical_referent_structured_visual_still_targets_sku(self) -> None:
        sample = self._MEDIA_NEED_SAMPLES[0]
        intent = self._classify_with_layer2(sample, {"intent_hint": INTENT_GENERAL})
        state = _state()
        bind_structured_catalog_referent(state, dict(SHOE), reason="recommend", turn=4)
        ctx = _ctx(
            intent_name=intent.name,
            state=state,
            facts=_facts(SHOE, SHIRT),
            message=sample,
        )
        ctx.intent = intent
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_SEARCH_PRODUCTS
        assert decision.args["replay_candidates"][0]["id"] == 501
        assert decision.args["replay_candidates"][0]["id"] != SHIRT["id"]

    def test_f3_b1_case_e_same_precedence_for_non_visual_rule_candidate(self) -> None:
        message = self._NON_VISUAL_LAYER2_MESSAGE
        candidate = intent_rules.match(message)
        assert candidate is not None
        assert candidate.name == INTENT_ASK_PRODUCT
        assert candidate.name != INTENT_PRODUCT_VISUAL_REQUEST
        assert candidate.confidence < RULES_ONLY_THRESHOLD
        intent = self._classify_with_layer2(
            message, {"intent_hint": INTENT_TRACK_ORDER}
        )
        assert intent.name == INTENT_TRACK_ORDER
        assert intent.name != candidate.name
        assert intent.extraction_method == "llm"
        assert intent.slots.get("classification_provenance") == (
            PROVENANCE_LAYER2_SEMANTIC_OVERRIDE
        )
        assert intent.slots.get("precedence_winner") == "layer2"
        assert intent.slots.get("rule_candidate") == INTENT_ASK_PRODUCT

    def test_layer2_operational_override_is_brain_llm_not_regex(self) -> None:
        sample = self._MEDIA_NEED_SAMPLES[0]
        intent = self._classify_with_layer2(
            sample, {"intent_hint": "talk_to_human"}
        )
        assert intent.extraction_method == "llm"
        assert intent.name == "talk_to_human"
        assert intent.name != INTENT_PRODUCT_VISUAL_REQUEST
        assert intent.slots.get("classification_provenance") == (
            PROVENANCE_LAYER2_SEMANTIC_OVERRIDE
        )

    def test_empty_layer2_does_not_return_rules_only_visual(self) -> None:
        sample = self._MEDIA_NEED_SAMPLES[0]
        intent = self._classify_with_layer2(sample, {})
        assert intent.extraction_method != "rules"
        assert intent.extraction_method == "hybrid"
        assert intent.slots.get("semantic_owner") == "brain_classifier"
        assert intent.name == INTENT_PRODUCT_VISUAL_REQUEST


_SAME_TITLE = "Classic Canvas Item"
ITEM_A = {
    "id": 801,
    "external_id": "sku-canvas-a",
    "title": _SAME_TITLE,
    "price": 80,
    "currency": "SAR",
    "in_stock": True,
    "can_checkout": True,
    "image_url": "https://cdn.example.test/canvas-a.jpg",
    "product_url": "https://shop.example.test/p/canvas-a",
    "provenance": "catalog_db",
}
ITEM_B = {
    "id": 802,
    "external_id": "sku-canvas-b",
    "title": _SAME_TITLE,
    "price": 80,
    "currency": "SAR",
    "in_stock": True,
    "can_checkout": True,
    "image_url": "https://cdn.example.test/canvas-b.jpg",
    "product_url": "https://shop.example.test/p/canvas-b",
    "provenance": "catalog_db",
}
ITEM_C = {
    "id": 803,
    "external_id": "sku-canvas-c",
    "title": _SAME_TITLE,
    "price": 80,
    "currency": "SAR",
    "in_stock": True,
    "can_checkout": True,
    "image_url": "https://cdn.example.test/canvas-c.jpg",
    "product_url": "https://shop.example.test/p/canvas-c",
    "provenance": "catalog_db",
}


class TestF3R1ClassifierCanonicalPrecedence:
    """R1: specific product-media need stays compatible with broader Layer 2."""

    # Existing visual-need phrasing used only as classifier input.
    # Contract does not depend on the live production sentence.
    _MEDIA_NEED = "وين الصوره"

    def _classify_with_layer2(self, message: str, layer2_slots: dict) -> Intent:
        slot_extract = AsyncMock(return_value=dict(layer2_slots))

        async def _run() -> Intent:
            with patch(
                "modules.ai.brain.intent.classifier._slot_mod.extract_slots",
                slot_extract,
            ):
                return await DefaultIntentClassifier().classify(
                    message,
                    [],
                    _state(),
                )

        intent = asyncio.run(_run())
        assert slot_extract.await_count == 1
        return intent

    def _bound_visual_decision(self, *, tenant_id: int, bound: dict, others: list[dict], message: str):
        intent = self._classify_with_layer2(message, {"intent_hint": INTENT_ASK_PRODUCT})
        state = _state()
        bind_structured_catalog_referent(state, dict(bound), reason="assistant_recommended", turn=4)
        bound_id = bound["id"]
        ctx = _ctx(
            intent_name=intent.name,
            state=state,
            facts=_facts(bound, *others),
            message=message,
            tenant_id=tenant_id,
        )
        ctx.intent = intent
        decision = DefaultDecisionEngine().decide(ctx)
        return intent, state, decision, bound_id

    def test_r1_01_broader_layer2_product_domain_keeps_specific_media_need(self) -> None:
        from modules.ai.brain.intent.classifier import _resolve_layer2_rule_precedence

        synthetic = Intent(name=INTENT_PRODUCT_VISUAL_REQUEST, confidence=0.93, slots={})
        name, _conf, method, provenance, winner, relation = _resolve_layer2_rule_precedence(
            rule_intent=synthetic,
            llm_hint=INTENT_ASK_PRODUCT,
            base_conf=0.93,
        )
        assert name == INTENT_PRODUCT_VISUAL_REQUEST
        assert name != INTENT_ASK_PRODUCT
        assert method == "hybrid"
        assert provenance == PROVENANCE_COMPATIBLE_BROADER_EVIDENCE
        assert winner == "rule_candidate"
        assert relation == SEMANTIC_RELATION_COMPATIBLE_BROADER

        candidate = intent_rules.match(self._MEDIA_NEED)
        assert candidate is not None
        assert candidate.name == INTENT_PRODUCT_VISUAL_REQUEST
        intent = self._classify_with_layer2(
            self._MEDIA_NEED, {"intent_hint": INTENT_ASK_PRODUCT}
        )
        assert intent.name == INTENT_PRODUCT_VISUAL_REQUEST
        assert intent.name != INTENT_ASK_PRODUCT
        assert intent.extraction_method == "hybrid"
        assert intent.extraction_method != "rules"
        assert intent.slots.get("classification_provenance") == (
            PROVENANCE_COMPATIBLE_BROADER_EVIDENCE
        )
        assert intent.slots.get("semantic_relation") == SEMANTIC_RELATION_COMPATIBLE_BROADER
        assert intent.slots.get("precedence_winner") == "rule_candidate"
        assert intent.slots.get("rule_candidate") == INTENT_PRODUCT_VISUAL_REQUEST
        assert intent.slots.get("layer2_result") == INTENT_ASK_PRODUCT

    def test_r1_02_same_title_siblings_resolve_bound_structured_id(self) -> None:
        intent, state, decision, bound_id = self._bound_visual_decision(
            tenant_id=9101,
            bound=ITEM_A,
            others=[ITEM_B, ITEM_C],
            message=self._MEDIA_NEED,
        )
        assert intent.name == INTENT_PRODUCT_VISUAL_REQUEST
        referent = canonical_product_referent(state)
        assert referent["id"] == bound_id
        assert referent["id"] != ITEM_B["id"]
        assert referent["id"] != ITEM_C["id"]
        assert referent["title"] == ITEM_B["title"] == ITEM_C["title"]
        replay = list(decision.args.get("replay_candidates") or [])
        assert replay[0]["id"] == bound_id
        assert replay[0]["id"] != ITEM_B["id"]

    def test_r1_03_ask_product_search_not_selected_when_bound_referent_satisfies(self) -> None:
        intent, _state_obj, decision, bound_id = self._bound_visual_decision(
            tenant_id=9101,
            bound=ITEM_A,
            others=[ITEM_B, ITEM_C],
            message=self._MEDIA_NEED,
        )
        assert intent.name != INTENT_ASK_PRODUCT
        assert decision.reason == "product visual — canonical referent is imageable"
        assert decision.args.get("after_search") == "product_visual"
        assert list(decision.args.get("replay_candidates") or [])[0]["id"] == bound_id
        visual = try_visual_catalog_send_decision(
            _ctx(
                intent_name=INTENT_PRODUCT_VISUAL_REQUEST,
                state=_state_obj,
                facts=_facts(ITEM_A, ITEM_B, ITEM_C),
                message=self._MEDIA_NEED,
                tenant_id=9101,
            )
        )
        assert visual is not None
        assert visual.reason == decision.reason

    def test_r1_04_canonical_focus_not_rebound_by_same_title_search(self) -> None:
        intent, state, decision, bound_id = self._bound_visual_decision(
            tenant_id=9101,
            bound=ITEM_A,
            others=[ITEM_B, ITEM_C],
            message=self._MEDIA_NEED,
        )
        assert intent.name == INTENT_PRODUCT_VISUAL_REQUEST
        assert canonical_product_referent(state)["id"] == bound_id
        assert canonical_product_referent(state)["external_id"] == ITEM_A["external_id"]
        replay_ids = [row["id"] for row in (decision.args.get("replay_candidates") or [])]
        assert replay_ids == [bound_id]
        assert ITEM_B["id"] not in replay_ids
        assert ITEM_C["id"] not in replay_ids

    def test_r1_05_start_order_still_authoritative_override(self) -> None:
        intent = self._classify_with_layer2(
            self._MEDIA_NEED, {"intent_hint": INTENT_START_ORDER}
        )
        assert intent.name == INTENT_START_ORDER
        assert intent.name != INTENT_PRODUCT_VISUAL_REQUEST
        assert intent.extraction_method == "llm"
        assert intent.slots.get("classification_provenance") == (
            PROVENANCE_LAYER2_SEMANTIC_OVERRIDE
        )
        assert intent.slots.get("semantic_relation") == (
            SEMANTIC_RELATION_AUTHORITATIVE_OVERRIDE
        )
        assert intent.slots.get("precedence_winner") == "layer2"

    def test_r1_06_ask_owner_contact_still_authoritative_override(self) -> None:
        intent = self._classify_with_layer2(
            self._MEDIA_NEED, {"intent_hint": INTENT_ASK_OWNER_CONTACT}
        )
        assert intent.name == INTENT_ASK_OWNER_CONTACT
        assert intent.slots.get("classification_provenance") == (
            PROVENANCE_LAYER2_SEMANTIC_OVERRIDE
        )
        assert intent.slots.get("semantic_relation") == (
            SEMANTIC_RELATION_AUTHORITATIVE_OVERRIDE
        )

    def test_r1_07_general_does_not_erase_supported_specific_candidate(self) -> None:
        intent = self._classify_with_layer2(
            self._MEDIA_NEED, {"intent_hint": INTENT_GENERAL}
        )
        assert intent.name == INTENT_PRODUCT_VISUAL_REQUEST
        assert intent.extraction_method == "hybrid"
        assert intent.slots.get("classification_provenance") == (
            PROVENANCE_RULE_CANDIDATE_CONFIRMED
        )
        assert intent.slots.get("semantic_relation") == SEMANTIC_RELATION_NON_AUTHORITATIVE
        assert is_authoritative_layer2_intent(INTENT_GENERAL) is False

    def test_r1_08_empty_layer2_is_not_rules_only_owner(self) -> None:
        intent = self._classify_with_layer2(self._MEDIA_NEED, {})
        assert intent.extraction_method != "rules"
        assert intent.extraction_method == "hybrid"
        assert intent.name == INTENT_PRODUCT_VISUAL_REQUEST
        assert intent.slots.get("classification_provenance") == (
            PROVENANCE_RULE_FALLBACK_AFTER_NO_AUTHORITATIVE_LAYER2
        )
        assert intent.slots.get("semantic_relation") == SEMANTIC_RELATION_NO_AUTHORITATIVE

    def test_r1_09_provenance_identifies_winner_and_canonical_product_id(self) -> None:
        intent, state, decision, bound_id = self._bound_visual_decision(
            tenant_id=9101,
            bound=ITEM_A,
            others=[ITEM_B, ITEM_C],
            message=self._MEDIA_NEED,
        )
        assert intent.slots.get("rule_candidate") == INTENT_PRODUCT_VISUAL_REQUEST
        assert intent.slots.get("layer2_result") == INTENT_ASK_PRODUCT
        assert intent.slots.get("precedence_winner") == "rule_candidate"
        assert intent.slots.get("semantic_relation") == SEMANTIC_RELATION_COMPATIBLE_BROADER
        assert intent.slots.get("classification_provenance") == (
            PROVENANCE_COMPATIBLE_BROADER_EVIDENCE
        )
        assert intent.name == INTENT_PRODUCT_VISUAL_REQUEST
        assert canonical_product_referent(state)["id"] == bound_id
        assert decision.args["replay_candidates"][0]["id"] == bound_id

    def test_r1_10_generic_second_tenant_category(self) -> None:
        intent, state, decision, bound_id = self._bound_visual_decision(
            tenant_id=9202,
            bound=PERFUME,
            others=[SHIRT],
            message=self._MEDIA_NEED,
        )
        assert intent.name == INTENT_PRODUCT_VISUAL_REQUEST
        assert canonical_product_referent(state)["id"] == PERFUME["id"]
        assert canonical_product_referent(state)["id"] == bound_id
        assert decision.args["replay_candidates"][0]["id"] == PERFUME["id"]
        assert decision.args["replay_candidates"][0]["id"] != SHIRT["id"]

    def test_r1_11_no_phrase_regex_or_intent_pair_allowlist(self) -> None:
        from modules.ai.brain.intent import classifier as intent_classifier

        classifier_src = inspect.getsource(intent_classifier)
        resolve_src = inspect.getsource(intent_classifier._resolve_layer2_rule_precedence)
        compat_src = inspect.getsource(intent_classifier.layer2_is_compatible_broader_evidence)
        assert "re.compile" not in classifier_src
        assert "ورني شكله" not in classifier_src
        assert "ورني" not in classifier_src
        assert "شكله" not in classifier_src
        assert "_PRODUCT_VISUAL_LLM_OVERRIDE" not in classifier_src
        assert "product_visual_request" not in compat_src
        assert "ask_product" not in compat_src
        assert INTENT_START_ORDER not in resolve_src
        assert INTENT_ASK_OWNER_CONTACT not in resolve_src
        assert INTENT_START_ORDER not in compat_src
        assert INTENT_ASK_OWNER_CONTACT not in compat_src
        assert "if layer2" not in compat_src
        assert layer2_is_compatible_broader_evidence(
            INTENT_PRODUCT_VISUAL_REQUEST, INTENT_ASK_PRODUCT
        ) is True
        assert layer2_is_compatible_broader_evidence(
            INTENT_PRODUCT_VISUAL_REQUEST, "ask_price"
        ) is True
        assert layer2_is_compatible_broader_evidence(
            INTENT_PRODUCT_VISUAL_REQUEST, INTENT_START_ORDER
        ) is False
        assert layer2_is_compatible_broader_evidence(
            INTENT_ASK_PRODUCT, INTENT_TRACK_ORDER
        ) is False

    def test_r1_12_no_duplicate_intent_or_referent_resolver(self) -> None:
        from modules.ai.brain.intent import classifier as intent_classifier

        classifier_src = inspect.getsource(intent_classifier)
        assert classifier_src.count("class ") == 1
        assert "def _resolve_layer2_rule_precedence" in classifier_src
        assert "def canonical_product_referent" not in classifier_src
        assert "def resolve_trusted_focus" not in classifier_src
        assert "visual_product_dispatch" not in classifier_src
        assert "product_presentation_selection" not in classifier_src
        assert "pick_best_candidate_title" not in classifier_src

    def test_r1_layer2_vocab_mirrors_prompt_and_excludes_visual(self) -> None:
        import re

        from modules.ai.brain.intent.slot_extractor import (
            LAYER2_INTENT_HINT_VOCABULARY,
            LAYER2_PRODUCT_DOMAIN_HINTS,
            _SYSTEM,
        )

        match = re.search(r"intent_hint:.*?:\s*([a-z_|]+)", _SYSTEM)
        assert match is not None
        prompt_labels = set(match.group(1).split("|"))
        assert prompt_labels == set(LAYER2_INTENT_HINT_VOCABULARY)
        assert INTENT_PRODUCT_VISUAL_REQUEST not in LAYER2_INTENT_HINT_VOCABULARY
        assert INTENT_PRODUCT_VISUAL_REQUEST not in _SYSTEM
        assert LAYER2_PRODUCT_DOMAIN_HINTS <= LAYER2_INTENT_HINT_VOCABULARY
        assert INTENT_ASK_PRODUCT in LAYER2_PRODUCT_DOMAIN_HINTS


class TestF309NativeCatalogCompletion:
    def test_successful_send_is_structured_visible_action(self) -> None:
        payload = native_catalog_send_completion(sent=True, has_brain_text=False)
        completion = payload["customer_turn_completion"]
        assert completion["completion_class"] == COMPLETION_STRUCTURED_VISIBLE
        assert completion["customer_visible"] is True
        assert completion["completion_class"] != COMPLETION_ORPHAN

    def test_protocol_body_is_minimum_not_conversational_script(self) -> None:
        body = resolve_native_catalog_body_text(
            context_reply="",
            inbound_customer_message="browse catalog",
        )
        assert body == MINIMAL_CATALOG_BODY
        assert body != TECHNICAL_CATALOG_BODY


class TestF310ProvenanceNoInferredFacts:
    def test_normalize_does_not_invent_media_or_price(self) -> None:
        from modules.ai.brain.commerce.commerce_focus_owner import (
            normalize_structured_product_referent,
        )

        row = normalize_structured_product_referent(
            {"id": 77, "external_id": "sku-x", "title": SHOE["title"]},
        )
        assert row is not None
        assert "image_url" not in row
        assert "price" not in row
        assert row["provenance"] == "structured_catalog"
        assert has_structured_catalog_identity(row)


class TestF311GenericSecondTenantCategory:
    def test_perfume_tenant_binds_and_targets_own_referent(self) -> None:
        state = _state()
        bind_structured_catalog_referent(state, dict(PERFUME), reason="recommend", turn=3)
        ctx = _ctx(
            intent_name=INTENT_PRODUCT_VISUAL_REQUEST,
            state=state,
            facts=_facts(PERFUME, SHIRT),
            tenant_id=9002,
        )
        decision = try_visual_catalog_send_decision(ctx)
        assert decision is not None
        assert decision.args["replay_candidates"][0]["id"] == 503
        assert decision.args["replay_candidates"][0]["title"] == PERFUME["title"]


class TestF312NoTenant33Runtime:
    def test_this_module_has_no_tenant_33_branch(self) -> None:
        from modules.ai.brain.commerce import commerce_focus_owner, visual_delivery_capability

        for mod in (commerce_focus_owner, visual_delivery_capability):
            src = inspect.getsource(mod)
            assert "tenant_id == 33" not in src
            assert "tenant_id=33" not in src


class TestF313NoPromptModelChange:
    def test_family_3_helpers_do_not_set_model_or_response_goal(self) -> None:
        from modules.ai.brain.commerce import commerce_focus_owner, visual_delivery_capability

        for mod in (commerce_focus_owner, visual_delivery_capability):
            src = inspect.getsource(mod)
            assert "gpt-" not in src
            assert "openai" not in src.lower()
            assert "response_goal" not in src
            assert "temperature" not in src


class TestF314F315F316Owners:
    def test_no_duplicate_referent_owner(self) -> None:
        state = _state()
        set_product_focus(state, dict(SHOE), reason="control", turn=1)
        bound = bind_structured_catalog_referent(state, dict(SHOE), reason="same", turn=2)
        assert bound is not None
        assert product_focus_identity(state.current_product_focus) == product_focus_identity(SHOE)
        assert canonical_product_referent(state)["id"] == 501

    def test_structured_product_from_turn_uses_identity(self) -> None:
        decision = Decision(
            action=ACTION_SEARCH_PRODUCTS,
            args={"recommended_product": dict(SHOE), "force_product_card": True},
        )
        product = structured_product_from_turn(decision, SimpleNamespace(data={}))
        assert product["id"] == 501
        assert has_structured_catalog_identity(product)

    def test_visual_send_stays_on_existing_capability_helper(self) -> None:
        src = inspect.getsource(try_visual_catalog_send_decision)
        assert "canonical_product_referent" in src
        assert "ACTION_SEARCH_PRODUCTS" in src


class TestF317ShippingUntouched:
    def test_family_3_owners_do_not_import_shipping_truth(self) -> None:
        from modules.ai.brain.commerce import (
            assistant_presented_provenance,
            catalog_body_policy,
            commerce_focus_owner,
            visual_delivery_capability,
        )

        for mod in (
            assistant_presented_provenance,
            catalog_body_policy,
            commerce_focus_owner,
            visual_delivery_capability,
        ):
            src = inspect.getsource(mod)
            assert "checkout_shipping_policy" not in src
            assert "shipping_cost_truth_guard" not in src
            assert "تكلفة الشحن: 189" not in src
