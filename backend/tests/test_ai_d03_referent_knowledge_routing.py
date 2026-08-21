"""AI-D03 — valid structured referent is a category-browse scope boundary.

Semantic contract: a follow-up still scoped to the current catalog referent
must not enter generic category browse, and the referent's catalog facts /
linked merchant knowledge must be projectable to Brain.

Do not assert exact customer-facing Arabic wording.
"""
from __future__ import annotations

import inspect
import os
import sys
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
_REPO = os.path.abspath(os.path.join(_BACKEND, ".."))
for _p in (_BACKEND, os.path.join(_REPO, "database"), _REPO):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.commerce.catalog_reasoning_evidence import (  # noqa: E402
    collect_catalog_reasoning_candidates,
    ensure_canonical_referent_catalog_projection,
    load_tenant_scoped_catalog_row,
    project_canonical_referent_catalog_facts,
)
from modules.ai.brain.commerce.commerce_focus_owner import (  # noqa: E402
    canonical_product_referent,
    has_structured_catalog_identity,
    set_product_focus,
)
from modules.ai.brain.commerce.product_breadth_policy import (  # noqa: E402
    resolve_kb_active_product_ids,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_LLM_REPLY,
    ACTION_SEARCH_PRODUCTS,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.product_discovery_gate import (  # noqa: E402
    preserve_canonical_referent_over_category_browse,
    try_category_price_browse_decision,
    try_referent_scoped_product_reply_decision,
    try_types_overview_decision,
)
from modules.ai.brain.state.stages import STAGE_DISCOVERY  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    INTENT_ASK_PRICE,
    INTENT_ASK_PRODUCT,
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
)

# Historical incident-shaped rows — fixtures only. Runtime must stay generic.
_SUMMER_HONEY_1KG = {
    "id": 154,
    "external_id": "sku-summer-honey-1kg",
    "title": "عسل صيفي 1 كيلو",
    "price": 320,
    "description": "عسل صيفي من أزهار موسم الصيف، مصدره المناحل المعتمدة.",
    "can_checkout": True,
    "in_stock": True,
}
_SUMMER_HONEY_250G = {
    "id": 141,
    "external_id": "sku-summer-honey-250g",
    "title": "عسل صيفي 250 جرام",
    "price": 95,
    "description": "العبوة الصغيرة من العسل الصيفي.",
    "can_checkout": True,
    "in_stock": True,
}
_UNRELATED_TALH_1KG = {
    "id": 146,
    "external_id": "sku-talh-1kg",
    "title": "عسل الطلح 1 كيلو",
    "price": 387,
    "description": "عسل طلج غير مرتبط بالمنتج الصيفي.",
    "can_checkout": True,
    "in_stock": True,
}
_LINKED_KB_BODY = (
    "مصدر العسل الصيفي من أزهار الموسم، ويختلف عن أصناف الطلح والسدر."
)

# Generic non-honey commerce — proves the contract is not a honey repair.
_SHOE = {
    "id": 501,
    "external_id": "sku-white-shoe",
    "title": "حذاء رياضي أبيض",
    "price": 249,
    "description": "حذاء رياضي أبيض بخامة شبكية للتنفس اليومي.",
    "can_checkout": True,
    "in_stock": True,
}
_SHOE_OTHER = {
    "id": 502,
    "external_id": "sku-black-boot",
    "title": "بوت جلدي أسود",
    "price": 310,
    "description": "بوت مختلف عن الحذاء الرياضي.",
    "can_checkout": True,
    "in_stock": True,
}
_PERFUME = {
    "id": 8801,
    "external_id": "sku-rose-perfume",
    "title": "عطر ورد 100ml",
    "price": 180,
    "description": "عطر ورد بتركيز 100 مل.",
    "can_checkout": True,
    "in_stock": True,
}

_INCIDENT_FOLLOWUP = "من أي أنواع العسل هذا العسل الصيفي"
_DEICTIC_TYPES_FOLLOWUP = "من أي أنواع هذا؟"
_GENERIC_SHOE_FOLLOWUP = "من أي أنواع هذا الحذاء الرياضي"
_SHOE_SIZE_FOLLOWUP_EN = "what sizes are available for this running shoe"
_SHOE_SIZE_FOLLOWUP_AR = "ما مقاسات هذا الحذاء الرياضي؟"
_HONEY_BROADEN = "وش أنواع العسل عندكم؟"
_HONEY_TYPES_BARE = "من أي أنواع العسل؟"
_SHOE_BROADEN = "وش أنواع الأحذية عندكم؟"
_HONEY_PRICE_BROWSE = "أسعار العسل"
_SHOE_PRICE_BROWSE = "أسعار الأحذية"
_SHOE_AVAIL_ON_HONEY_FOCUS = "هل هذه الأحذية متوفرة؟"


def _intent(message: str, name: str = INTENT_ASK_PRODUCT) -> Intent:
    return Intent(name=name, confidence=0.86, raw_message=message)


def _state(product: Dict[str, Any] | None = None) -> MerchantConversationState:
    state = MerchantConversationState(greeted=True, stage=STAGE_DISCOVERY, turn=3)
    if product is not None:
        set_product_focus(state, dict(product), reason="ai_d03_test_focus", turn=2)
    return state


def _facts(*products: Dict[str, Any]) -> CommerceFacts:
    rows = [dict(p) for p in products]
    return CommerceFacts(
        has_products=bool(rows),
        product_count=len(rows),
        in_stock_count=len(rows),
        orderable=True,
        store_name="متجر تجريبي عام",
        assistant_name="نحلة",
        top_products=list(rows),
        discovery_products=list(rows),
    )


def _ctx(
    message: str,
    *,
    state: MerchantConversationState | None = None,
    facts: CommerceFacts | None = None,
    tenant_id: int = 77,
    intent_name: str = INTENT_ASK_PRODUCT,
    merchant_context: Dict[str, Any] | None = None,
) -> BrainContext:
    return BrainContext(
        tenant_id=tenant_id,
        customer_phone="966500000001",
        message=message,
        intent=_intent(message, intent_name),
        state=state if state is not None else _state(),
        facts=facts if facts is not None else _facts(),
        profile={"inbound_metadata": {}},
        merchant_context=merchant_context if merchant_context is not None else {},
    )


def _decision_product_id(decision: Any) -> Any:
    args = getattr(decision, "args", None) or {}
    product = args.get("product") or {}
    if isinstance(product, dict):
        return product.get("id") or product.get("product_id")
    return None


def _runtime_sources() -> List[str]:
    return [
        inspect.getsource(
            sys.modules["modules.ai.brain.product_discovery_gate"],
        ),
        inspect.getsource(
            sys.modules["modules.ai.brain.commerce.catalog_reasoning_evidence"],
        ),
        inspect.getsource(
            sys.modules["modules.ai.brain.commerce.commerce_focus_owner"],
        ),
    ]


class TestEstablishedReferentFollowup:
    def test_incident_followup_does_not_enter_category_browse(self) -> None:
        state = _state(_SUMMER_HONEY_1KG)
        ctx = _ctx(
            _INCIDENT_FOLLOWUP,
            state=state,
            facts=_facts(_UNRELATED_TALH_1KG, _SUMMER_HONEY_1KG, _SUMMER_HONEY_250G),
        )
        assert preserve_canonical_referent_over_category_browse(state, _INCIDENT_FOLLOWUP)
        assert try_category_price_browse_decision(ctx) is None
        types_dec = try_types_overview_decision(ctx)
        assert types_dec is not None
        assert types_dec.action == ACTION_LLM_REPLY
        assert types_dec.args.get("source") != "category_browse"

    def test_variant_attribute_followup_keeps_shoe_referent(self) -> None:
        shoe = dict(_SHOE)
        shoe["title"] = "white running shoe"
        state = _state(shoe)
        facts = _facts(shoe, _SHOE_OTHER)
        ctx = _ctx(_SHOE_SIZE_FOLLOWUP_EN, state=state, facts=facts)
        assert preserve_canonical_referent_over_category_browse(
            state,
            _SHOE_SIZE_FOLLOWUP_EN,
            facts=facts,
        )
        browse = try_category_price_browse_decision(ctx)
        assert browse is None or browse.action != ACTION_SEARCH_PRODUCTS
        reply = try_referent_scoped_product_reply_decision(ctx)
        assert reply is not None
        assert reply.action == ACTION_LLM_REPLY
        assert _decision_product_id(reply) == 501
        assert str((reply.args or {}).get("query") or "") != "sizes"

    def test_arabic_size_followup_keeps_shoe_referent(self) -> None:
        state = _state(_SHOE)
        facts = _facts(_SHOE, _SHOE_OTHER)
        ctx = _ctx(_SHOE_SIZE_FOLLOWUP_AR, state=state, facts=facts)
        assert preserve_canonical_referent_over_category_browse(
            state,
            _SHOE_SIZE_FOLLOWUP_AR,
            facts=facts,
        )
        browse = try_category_price_browse_decision(ctx)
        assert browse is None or browse.action != ACTION_SEARCH_PRODUCTS
        reply = try_referent_scoped_product_reply_decision(ctx)
        assert reply is not None
        assert reply.action == ACTION_LLM_REPLY
        assert _decision_product_id(reply) == 501

    def test_generic_shoe_followup_does_not_enter_category_browse(self) -> None:
        state = _state(_SHOE)
        ctx = _ctx(
            _GENERIC_SHOE_FOLLOWUP,
            state=state,
            facts=_facts(_SHOE, _SHOE_OTHER),
        )
        assert preserve_canonical_referent_over_category_browse(state, _GENERIC_SHOE_FOLLOWUP)
        assert try_category_price_browse_decision(ctx) is None
        types_dec = try_types_overview_decision(ctx)
        assert types_dec is not None
        assert types_dec.action == ACTION_LLM_REPLY


class TestReferentPreserved:
    def test_historical_141_then_154_does_not_select_unrelated_ranker_winner(self) -> None:
        state = _state(_SUMMER_HONEY_250G)
        set_product_focus(state, dict(_SUMMER_HONEY_1KG), reason="ai_d03_later_focus", turn=3)
        ctx = _ctx(
            _INCIDENT_FOLLOWUP,
            state=state,
            facts=_facts(_UNRELATED_TALH_1KG, _SUMMER_HONEY_1KG, _SUMMER_HONEY_250G),
        )
        decision = DefaultDecisionEngine().decide(ctx)
        referent = canonical_product_referent(state)
        assert referent["id"] == 154
        assert _decision_product_id(decision) == 154
        assert _decision_product_id(decision) != 146
        assert decision.action != ACTION_SEARCH_PRODUCTS
        assert 146 not in (
            resolve_kb_active_product_ids(state, _INCIDENT_FOLLOWUP) or set()
        )

    def test_incident_keeps_summer_honey_identity(self) -> None:
        state = _state(_SUMMER_HONEY_1KG)
        ctx = _ctx(
            _INCIDENT_FOLLOWUP,
            state=state,
            facts=_facts(_UNRELATED_TALH_1KG, _SUMMER_HONEY_1KG, _SUMMER_HONEY_250G),
        )
        decision = DefaultDecisionEngine().decide(ctx)
        referent = canonical_product_referent(state)
        assert has_structured_catalog_identity(referent)
        assert referent["id"] == 154
        assert _decision_product_id(decision) == 154
        assert decision.action != ACTION_SEARCH_PRODUCTS

    def test_generic_shoe_identity_is_preserved(self) -> None:
        state = _state(_SHOE)
        ctx = _ctx(
            _GENERIC_SHOE_FOLLOWUP,
            state=state,
            facts=_facts(_SHOE, _SHOE_OTHER),
        )
        decision = DefaultDecisionEngine().decide(ctx)
        assert canonical_product_referent(state)["id"] == 501
        assert _decision_product_id(decision) == 501
        assert decision.action != ACTION_SEARCH_PRODUCTS


class TestKnowledgeReachesBrain:
    def test_incident_description_and_linked_kb_scope(self) -> None:
        state = _state(_SUMMER_HONEY_1KG)
        facts = _facts(_UNRELATED_TALH_1KG, _SUMMER_HONEY_1KG, _SUMMER_HONEY_250G)
        rows = collect_catalog_reasoning_candidates(facts=facts, state=state)
        assert rows
        assert rows[0]["id"] == 154
        assert "أزهار موسم الصيف" in str(rows[0].get("description") or "")
        projected = project_canonical_referent_catalog_facts(state=state, facts=facts)
        assert projected is not None
        assert projected["id"] == 154
        assert "أزهار موسم الصيف" in str(projected.get("description") or "")
        active = resolve_kb_active_product_ids(state, _INCIDENT_FOLLOWUP)
        assert active is not None
        assert 154 in active
        assert 146 not in active

    def test_generic_perfume_description_projects(self) -> None:
        state = _state(_PERFUME)
        facts = _facts(_PERFUME, _SHOE)
        projected = project_canonical_referent_catalog_facts(state=state, facts=facts)
        assert projected is not None
        assert projected["id"] == 8801
        assert "100 مل" in str(projected.get("description") or "")


class TestNoWrongStructuredAction:
    def test_unrelated_first_ranked_product_is_not_emitted(self) -> None:
        state = _state(_SUMMER_HONEY_1KG)
        ctx = _ctx(
            _INCIDENT_FOLLOWUP,
            state=state,
            facts=_facts(_UNRELATED_TALH_1KG, _SUMMER_HONEY_1KG),
            merchant_context={"products": [dict(_UNRELATED_TALH_1KG)]},
        )
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_LLM_REPLY
        assert (decision.args or {}).get("source") != "category_browse"
        assert _decision_product_id(decision) != 146
        candidates = collect_catalog_reasoning_candidates(
            facts=ctx.facts,
            merchant_context=ctx.merchant_context,
            state=state,
        )
        assert candidates[0]["id"] == 154


class TestLegitimateBroadening:
    def test_honey_types_overview_still_browses(self) -> None:
        state = _state(_SUMMER_HONEY_1KG)
        ctx = _ctx(
            _HONEY_BROADEN,
            state=state,
            facts=_facts(_UNRELATED_TALH_1KG, _SUMMER_HONEY_1KG),
        )
        assert preserve_canonical_referent_over_category_browse(state, _HONEY_BROADEN) is False
        types_dec = try_types_overview_decision(ctx)
        assert types_dec is not None
        assert types_dec.action == ACTION_SEARCH_PRODUCTS
        assert types_dec.args.get("source") == "category_browse"

    def test_generic_shoe_types_overview_still_browses(self) -> None:
        state = _state(_SHOE)
        ctx = _ctx(
            _SHOE_BROADEN,
            state=state,
            facts=_facts(_SHOE, _SHOE_OTHER),
        )
        assert preserve_canonical_referent_over_category_browse(state, _SHOE_BROADEN) is False
        types_dec = try_types_overview_decision(ctx)
        assert types_dec is not None
        assert types_dec.action == ACTION_SEARCH_PRODUCTS

    def test_bare_category_types_ask_is_authorized_broadening(self) -> None:
        state = _state(_SUMMER_HONEY_1KG)
        assert preserve_canonical_referent_over_category_browse(state, _HONEY_TYPES_BARE) is False
        ctx = _ctx(
            _HONEY_TYPES_BARE,
            state=state,
            facts=_facts(_UNRELATED_TALH_1KG, _SUMMER_HONEY_1KG),
        )
        types_dec = try_types_overview_decision(ctx)
        assert types_dec is not None
        assert types_dec.action == ACTION_SEARCH_PRODUCTS

    def test_deictic_other_category_does_not_keep_old_sku(self) -> None:
        state = _state(_SUMMER_HONEY_1KG)
        assert preserve_canonical_referent_over_category_browse(
            state,
            _SHOE_AVAIL_ON_HONEY_FOCUS,
        ) is False

    def test_fresh_deictic_types_followup_keeps_referent(self) -> None:
        state = _state(_SUMMER_HONEY_1KG)
        assert preserve_canonical_referent_over_category_browse(state, _DEICTIC_TYPES_FOLLOWUP)
        ctx = _ctx(
            _DEICTIC_TYPES_FOLLOWUP,
            state=state,
            facts=_facts(_UNRELATED_TALH_1KG, _SUMMER_HONEY_1KG),
        )
        types_dec = try_types_overview_decision(ctx)
        assert types_dec is not None
        assert types_dec.action == ACTION_LLM_REPLY
        assert _decision_product_id(types_dec) == 154


class TestNoReferentBrowseUnchanged:
    def test_honey_price_browse_without_focus(self) -> None:
        ctx = _ctx(
            _HONEY_PRICE_BROWSE,
            state=_state(),
            facts=_facts(_UNRELATED_TALH_1KG, _SUMMER_HONEY_1KG),
            intent_name=INTENT_ASK_PRICE,
        )
        assert preserve_canonical_referent_over_category_browse(ctx.state, _HONEY_PRICE_BROWSE) is False
        decision = try_category_price_browse_decision(ctx)
        assert decision is not None
        assert decision.action == ACTION_SEARCH_PRODUCTS
        assert decision.args.get("source") == "category_browse"

    def test_generic_shoe_price_browse_without_focus(self) -> None:
        ctx = _ctx(
            _SHOE_PRICE_BROWSE,
            state=_state(),
            facts=_facts(_SHOE, _SHOE_OTHER),
            intent_name=INTENT_ASK_PRICE,
        )
        decision = try_category_price_browse_decision(ctx)
        assert decision is not None
        assert decision.action == ACTION_SEARCH_PRODUCTS


class TestStaleInvalidReferent:
    def test_established_honey_focus_does_not_trap_unrelated_shoe_browse(self) -> None:
        state = _state(_SUMMER_HONEY_1KG)
        ctx = _ctx(
            _SHOE_PRICE_BROWSE,
            state=state,
            facts=_facts(_SHOE, _SHOE_OTHER, _SUMMER_HONEY_1KG),
            intent_name=INTENT_ASK_PRICE,
        )
        assert preserve_canonical_referent_over_category_browse(state, _SHOE_PRICE_BROWSE) is False
        decision = try_category_price_browse_decision(ctx)
        assert decision is not None
        assert decision.action == ACTION_SEARCH_PRODUCTS

    def test_title_only_focus_does_not_trap_browse(self) -> None:
        state = MerchantConversationState(greeted=True, stage=STAGE_DISCOVERY)
        state.current_product_focus = {"title": "عسل صيفي 1 كيلو"}
        assert has_structured_catalog_identity(state.current_product_focus) is False
        ctx = _ctx(
            _HONEY_BROADEN,
            state=state,
            facts=_facts(_UNRELATED_TALH_1KG, _SUMMER_HONEY_1KG),
        )
        assert preserve_canonical_referent_over_category_browse(state, _HONEY_BROADEN) is False
        types_dec = try_types_overview_decision(ctx)
        assert types_dec is not None
        assert types_dec.action == ACTION_SEARCH_PRODUCTS

    def test_deleted_id_does_not_trap_when_live_catalog_moved_on(self) -> None:
        stale = {
            "id": 999,
            "external_id": "deleted-sku",
            "title": "white running shoe",
        }
        state = _state(stale)
        facts = _facts(_SHOE_OTHER)
        message = "what types is this white running shoe"
        merchant_context = {"products": [dict(_SHOE_OTHER)]}
        assert preserve_canonical_referent_over_category_browse(
            state,
            message,
            facts=facts,
            merchant_context=merchant_context,
        ) is False
        ctx = _ctx(
            message,
            state=state,
            facts=facts,
            merchant_context=merchant_context,
        )
        types_dec = try_types_overview_decision(ctx)
        reply = try_referent_scoped_product_reply_decision(ctx)
        assert reply is None
        assert _decision_product_id(types_dec) != 999
        projected = ensure_canonical_referent_catalog_projection(
            state=state,
            facts=facts,
            merchant_context=merchant_context,
            bind_to_merchant_context=True,
        )
        assert projected is None
        assert merchant_context["products"][0]["id"] == 502

    def test_empty_focus_does_not_trap_price_browse(self) -> None:
        ctx = _ctx(
            _SHOE_PRICE_BROWSE,
            state=_state(),
            facts=_facts(_SHOE, _SHOE_OTHER),
            intent_name=INTENT_ASK_PRICE,
        )
        assert try_category_price_browse_decision(ctx) is not None


class TestTenantIsolation:
    def test_catalog_row_load_is_tenant_scoped(self) -> None:
        row = MagicMock()
        row.id = 501
        row.external_id = "sku-white-shoe"
        row.sku = "sku-white-shoe"
        row.title = "حذاء رياضي أبيض"
        row.description = "حذاء رياضي أبيض بخامة شبكية للتنفس اليومي."
        row.price = 249

        class _Query:
            def __init__(self, match: bool) -> None:
                self._match = match

            def filter(self, *args: Any, **kwargs: Any) -> "_Query":
                return self

            def first(self) -> Any:
                return row if self._match else None

        class _Db:
            def __init__(self, tenant_id: int) -> None:
                self.tenant_id = tenant_id

            def query(self, _model: Any) -> _Query:
                return _Query(self.tenant_id == 77)

        owned = load_tenant_scoped_catalog_row(_Db(77), 77, 501)
        foreign = load_tenant_scoped_catalog_row(_Db(99), 99, 501)
        assert owned is not None
        assert owned.get("id") == 501
        assert foreign is None

    def test_kb_active_ids_stay_on_current_tenant_referent(self) -> None:
        state = _state(_SHOE)
        active = resolve_kb_active_product_ids(state, _GENERIC_SHOE_FOLLOWUP)
        assert active == {501}


class TestGenericFixNoRuntimeConstants:
    def test_runtime_has_no_incident_constants(self) -> None:
        blob = "\n".join(_runtime_sources())
        forbidden = (
            "tenant_id=33",
            "Tenant 33",
            "KB 212",
            "section 212",
            "sku-talh-1kg",
            "sku-summer-honey",
            "عسل صيفي",
            "عسل الطلح",
            _INCIDENT_FOLLOWUP,
            "product 146",
            "product 154",
            "product 141",
            "product 162",
            "section_id=212",
        )
        for token in forbidden:
            assert token not in blob, token


class TestAiConfigUntouched:
    def test_referent_reply_routes_to_brain_not_a_model_change(self) -> None:
        from modules.ai.brain.product_discovery_gate import (  # noqa: PLC0415
            try_referent_scoped_product_reply_decision,
        )

        src = inspect.getsource(try_referent_scoped_product_reply_decision)
        assert "ACTION_LLM_REPLY" in src
        assert "openai" not in src.lower()
        assert "anthropic" not in src.lower()
        assert "model=" not in src


class TestProjectionBind:
    def test_ensure_binds_referent_ahead_of_unrelated_search_hit(self) -> None:
        state = _state(_SUMMER_HONEY_1KG)
        merchant_context = {"products": [dict(_UNRELATED_TALH_1KG)]}
        projected = ensure_canonical_referent_catalog_projection(
            state=state,
            facts=_facts(_UNRELATED_TALH_1KG, _SUMMER_HONEY_1KG),
            merchant_context=merchant_context,
            bind_to_merchant_context=True,
        )
        assert projected is not None
        assert projected["id"] == 154
        assert merchant_context["products"][0]["id"] == 154
        assert merchant_context["conversation"]["selected_product"]["id"] == 154
        assert "أزهار موسم الصيف" in str(projected.get("description") or "")

    def test_catalog_row_overwrites_stale_focus_price(self) -> None:
        stale = dict(_SHOE)
        stale["price"] = 999
        stale.pop("description", None)
        state = _state(stale)
        projected = project_canonical_referent_catalog_facts(
            state=state,
            catalog_row={
                "id": 501,
                "external_id": "sku-white-shoe",
                "title": "حذاء رياضي أبيض",
                "price": 249,
                "description": "حذاء رياضي أبيض بخامة شبكية للتنفس اليومي.",
                "can_checkout": True,
                "in_stock": True,
            },
        )
        assert projected is not None
        assert projected["id"] == 501
        assert projected["price"] == 249
        assert "شبكية" in str(projected.get("description") or "")

    def test_cached_projection_overwrites_stale_focus_at_decision_time(self) -> None:
        stale = dict(_SHOE)
        stale["price"] = 999
        stale.pop("description", None)
        state = _state(stale)
        merchant_context = {
            "_canonical_referent_catalog_facts": {
                "id": 501,
                "external_id": "sku-white-shoe",
                "title": "حذاء رياضي أبيض",
                "price": 249,
                "description": "حذاء رياضي أبيض بخامة شبكية للتنفس اليومي.",
                "can_checkout": True,
                "in_stock": True,
            }
        }
        projected = project_canonical_referent_catalog_facts(
            state=state,
            merchant_context=merchant_context,
        )
        assert projected is not None
        assert projected["price"] == 249
        assert "شبكية" in str(projected.get("description") or "")

    def test_numeric_id_does_not_merge_foreign_external_id(self) -> None:
        state = _state({"id": 154, "title": "عسل صيفي 1 كيلو", "price": 320})
        projected = project_canonical_referent_catalog_facts(
            state=state,
            facts=_facts(
                {
                    "id": 8801,
                    "external_id": "154",
                    "title": "عطر ورد 100ml",
                    "price": 180,
                    "description": "must-not-merge",
                },
                _SUMMER_HONEY_1KG,
            ),
        )
        assert projected is not None
        assert projected["id"] == 154
        assert "must-not-merge" not in str(projected.get("description") or "")

    def test_external_id_matches_numeric_id_row(self) -> None:
        state = MerchantConversationState(greeted=True, stage=STAGE_DISCOVERY, turn=3)
        set_product_focus(
            state,
            {"external_id": "sku-white-shoe", "title": "حذاء رياضي أبيض"},
            reason="ai_d03_test_focus",
            turn=2,
        )
        projected = project_canonical_referent_catalog_facts(
            state=state,
            facts=_facts(_SHOE),
        )
        assert projected is not None
        assert projected.get("id") == 501
        assert "شبكية" in str(projected.get("description") or "")

    def test_sku_only_does_not_merge_foreign_external_id(self) -> None:
        state = MerchantConversationState(greeted=True, stage=STAGE_DISCOVERY, turn=3)
        set_product_focus(
            state,
            {"sku": "shared-key", "title": "حذاء رياضي أبيض"},
            reason="ai_d03_test_focus",
            turn=2,
        )
        projected = project_canonical_referent_catalog_facts(
            state=state,
            facts=_facts(
                {
                    "id": 8801,
                    "external_id": "shared-key",
                    "title": "عطر ورد 100ml",
                    "price": 180,
                    "description": "WRONG PRODUCT FACT",
                }
            ),
        )
        assert projected is not None
        assert projected.get("id") != 8801
        assert "WRONG PRODUCT FACT" not in str(projected.get("description") or "")

    def test_shared_sku_does_not_merge_different_internal_ids(self) -> None:
        state = _state({"id": 501, "sku": "shared-sku", "title": "حذاء رياضي أبيض", "price": 249})
        projected = project_canonical_referent_catalog_facts(
            state=state,
            facts=_facts(
                {
                    "id": 8801,
                    "sku": "shared-sku",
                    "title": "عطر ورد 100ml",
                    "price": 180,
                    "description": "WRONG PRODUCT FACT",
                }
            ),
        )
        assert projected is not None
        assert projected["id"] == 501
        assert "WRONG PRODUCT FACT" not in str(projected.get("description") or "")


class TestLinkedMerchantKnowledgeOverlay:
    def test_product_scoped_section_kept_for_preserved_referent(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from modules.ai.prompts.tenant_overlay import build_structured_facts_block

        class _Link:
            def __init__(self, product_id: int) -> None:
                self.product_id = product_id

        class _Section:
            def __init__(self) -> None:
                self.id = 9001
                self.kind = "product_info"
                self.title = "مصدر المنتج"
                self.body = _LINKED_KB_BODY
                self.priority = 10
                self.product_links = [_Link(154)]
                from datetime import datetime, timezone

                self.updated_at = datetime.now(timezone.utc)

        class _Query:
            def __init__(self, rows: List[Any]) -> None:
                self._rows = rows

            def filter(self, *args: Any, **kwargs: Any) -> "_Query":
                return self

            def order_by(self, *args: Any, **kwargs: Any) -> "_Query":
                return self

            def all(self) -> List[Any]:
                return list(self._rows)

        class _Session:
            def query(self, _model: Any) -> _Query:
                return _Query([_Section()])

        state = _state(_SUMMER_HONEY_1KG)
        active = resolve_kb_active_product_ids(state, _INCIDENT_FOLLOWUP)
        monkeypatch.setattr(
            "core.knowledge.section_has_catalog_active_product",
            lambda *_a, **_k: True,
        )
        kept = build_structured_facts_block(
            _Session(),
            tenant_id=77,
            active_product_ids=active,
        )
        dropped = build_structured_facts_block(
            _Session(),
            tenant_id=77,
            active_product_ids={146},
        )
        assert _LINKED_KB_BODY in kept
        assert _LINKED_KB_BODY not in dropped
