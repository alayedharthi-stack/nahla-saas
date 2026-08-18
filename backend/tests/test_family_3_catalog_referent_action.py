"""Family 3 — catalog referent, structured action/media, Brain-owned wording.

Asserts identity, capability, and completion — not exact customer Arabic.
"""
from __future__ import annotations

import inspect
import os
import sys
from types import SimpleNamespace

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
from modules.ai.brain.types import (  # noqa: E402
    INTENT_PRODUCT_VISUAL_REQUEST,
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
