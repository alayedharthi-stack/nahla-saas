"""T2 selection — name+price candidate resolution and ordinal price guard."""
from __future__ import annotations

import asyncio
import os
import sys
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.commerce.commerce_objective import COMMERCE_OBJECTIVE_DISCOVERY
from modules.ai.brain.commerce.selection_context import (
    NAME_PRICE_CANDIDATE_MATCH,
    STRUCTURED_UNIQUE_SELECTION_KEY,
    CANDIDATE_SOURCE_LAST_SEARCH,
    _extract_ordinal_token,
    _normalize_ar,
    resolve_selection_context,
    stamp_selection_context_from_products,
    try_selection_context_decision,
)
from modules.ai.brain.decision.actions import (
    ACTION_CLARIFY,
    ACTION_LLM_REPLY,
    ACTION_NARROW,
    ACTION_PROPOSE_DRAFT_ORDER,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine
from modules.ai.brain.intent import rules
from modules.ai.brain.intent_priority.types import GOAL_PRICE_INQUIRY, IntentPriorityVerdict
from modules.ai.brain.state.state_relevance import StateRelevanceVerdict
from modules.ai.brain.turn.enforce import maybe_enforce_turn_decision
from modules.ai.brain.turn.mismatch import MISMATCH_CHECKOUT_VS_DISCOVERY
from modules.ai.brain.turn.shadow import prepare_turn_arbitration
from modules.ai.brain.types import (
    INTENT_ASK_PRICE,
    ActionResult,
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)
from tests.commerce_scenario_fixtures import (
    make_scenario_db,
    seed_conversation,
    seed_customer,
    seed_tenant,
)

DRESS_CANDIDATES = [
    {
        "id": "23",
        "external_id": "23",
        "title": "فستان سهرة",
        "display_label": "فستان سهرة",
        "price": 89,
        "can_checkout": True,
        "orderable": True,
    },
    {
        "id": "37",
        "external_id": "37",
        "title": "فستان كاجوال",
        "display_label": "فستان كاجوال",
        "price": 114,
        "can_checkout": True,
        "orderable": True,
    },
]

SHIRT_DUPLICATE_PRICE = [
    {
        "id": "501",
        "external_id": "501",
        "title": "قميص قطني أزرق",
        "display_label": "قميص قطني أزرق",
        "price": 114,
        "can_checkout": True,
        "orderable": True,
    },
    {
        "id": "502",
        "external_id": "502",
        "title": "قميص قطني أبيض",
        "display_label": "قميص قطني أبيض",
        "price": 114,
        "can_checkout": True,
        "orderable": True,
    },
]

MSG_NAME_PRICE = "أريد الفستان سعره 114 ريال"
MSG_EXPLORATORY = "كم سعر الفستان؟"
MSG_MISMATCH = "أريد الفستان سعره 200 ريال"
MSG_DUPLICATE = "أريد القميص سعره 114 ريال"
MSG_ORDINAL_FIRST = "أبغى الأول"
MSG_NAME_ONLY = "أبغى ألفا"


def _facts() -> CommerceFacts:
    return CommerceFacts(
        has_products=True,
        product_count=20,
        in_stock_count=20,
        has_active_integration=True,
        orderable=True,
        snapshot_fresh=True,
        store_name="متجر تجريبي عام",
    )


def _browse_state(
    *,
    candidates: list | None = None,
    selected_id: str = "",
    stale_checkout: bool = False,
) -> MerchantConversationState:
    rows = list(candidates or DRESS_CANDIDATES)
    state = MerchantConversationState(
        greeted=True,
        stage="checkout" if stale_checkout else "discovery",
        turn=5 if stale_checkout else 4,
        commerce_objective=COMMERCE_OBJECTIVE_DISCOVERY,
        last_browse_query="فساتين",
    )
    stamp_selection_context_from_products(
        state,
        products=rows,
        selected_collection="فساتين",
        discovery_mode="search",
    )
    state.last_search_candidates = list(rows)
    if selected_id:
        state.selected_product_id = selected_id
    if stale_checkout:
        state.order_prep = OrderPreparationState(
            product_id="p-stale",
            missing_fields=["city"],
        )
        state.last_question_asked = "ما المدينة التي سيصلها الطلب؟"
        state.last_question_answered = False
    return state


def _ctx(
    msg: str,
    *,
    state: MerchantConversationState | None = None,
    intent_name: str | None = None,
) -> BrainContext:
    intent = rules.match(msg)
    if intent is None:
        intent = Intent(
            name=intent_name or "general",
            confidence=0.9,
            raw_message=msg,
        )
    elif intent_name:
        intent = Intent(name=intent_name, confidence=0.9, raw_message=msg, slots=intent.slots)
    return BrainContext(
        tenant_id=7,
        customer_phone="966542980511",
        message=msg,
        raw_message=msg,
        intent=intent,
        state=state or _browse_state(),
        facts=_facts(),
    )


def _enable_enforce_platform_wide(monkeypatch) -> None:
    monkeypatch.setenv("TURN_ARBITER_ENFORCE_ENABLED", "true")
    monkeypatch.delenv("TURN_ARBITER_ENFORCE_TENANTS", raising=False)
    monkeypatch.setenv(
        "TURN_ARBITER_ENFORCE_MISMATCH_TYPES",
        "checkout_vs_support,checkout_vs_discovery,staff_vs_persona",
    )


def _arbiter_ctx(*, stale_checkout: bool = True) -> BrainContext:
    ctx = _ctx(
        MSG_NAME_PRICE,
        state=_browse_state(stale_checkout=stale_checkout),
        intent_name=INTENT_ASK_PRICE,
    )
    ctx.state_relevance = StateRelevanceVerdict(
        safe_to_resume_state=False,
        detected_topic_shift=True,
        active_workflows=("active_fulfillment",),
    )
    ctx.intent_priority = IntentPriorityVerdict(primary_customer_goal=GOAL_PRICE_INQUIRY)
    return ctx


def _legacy_name_price_decision(ctx: BrainContext):
    prepare_turn_arbitration(ctx)
    legacy = DefaultDecisionEngine().decide(ctx)
    assert legacy.action == ACTION_PROPOSE_DRAFT_ORDER
    assert legacy.args["product"]["id"] == "37"
    return legacy


class TestOrdinalPriceGuard:
    def test_114_never_maps_to_ordinal_one(self) -> None:
        assert _extract_ordinal_token(_normalize_ar("114")) is None

    def test_sair_114_never_maps_to_ordinal_one(self) -> None:
        assert _extract_ordinal_token(_normalize_ar("سعره 114")) is None

    def test_name_price_message_not_ordinal_select(self) -> None:
        resolution = resolve_selection_context(_ctx(MSG_NAME_PRICE))
        assert resolution is not None
        assert resolution.kind == "name_price_select"
        assert resolution.selected is not None
        assert resolution.selected["id"] == "37"


class TestNamePriceUniqueSelection:
    def test_exact_name_price_selects_candidate_37_not_23(self) -> None:
        decision = try_selection_context_decision(_ctx(MSG_NAME_PRICE))
        assert decision is not None
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER
        assert decision.args["product"]["id"] == "37"
        assert decision.args["forced_product"]["id"] == "37"
        marker = decision.args.get(STRUCTURED_UNIQUE_SELECTION_KEY) or {}
        assert marker.get("verified_unique") is True
        assert marker.get("candidate_id") == "37"
        assert marker.get("stated_price") == 114

    def test_duplicate_matching_candidates_no_checkout(self) -> None:
        """Product adjudication: clarify on duplicate matches — never draft/checkout."""
        state = _browse_state(candidates=SHIRT_DUPLICATE_PRICE)
        decision = try_selection_context_decision(_ctx(MSG_DUPLICATE, state=state))
        assert decision is not None
        assert decision.action == ACTION_CLARIFY
        assert decision.args.get("topic") == "product_price_ambiguity"
        products = list(decision.args.get("products") or [])
        assert len(products) >= 2
        assert "forced_product" not in (decision.args or {})
        assert decision.action != ACTION_PROPOSE_DRAFT_ORDER
        assert decision.action not in {
            ACTION_PROPOSE_DRAFT_ORDER,
            "create_order",
            "create_checkout",
            "send_payment_link",
        }

    def test_mismatched_price_no_checkout(self) -> None:
        """Product adjudication: narrow_choices on price mismatch — never draft/checkout."""
        decision = try_selection_context_decision(_ctx(MSG_MISMATCH))
        assert decision is not None
        assert decision.action == ACTION_NARROW
        assert decision.args.get("source") == "selection_context_price_no_match"
        assert "forced_product" not in (decision.args or {})
        assert decision.action != ACTION_PROPOSE_DRAFT_ORDER
        assert decision.action not in {
            ACTION_PROPOSE_DRAFT_ORDER,
            "create_order",
            "create_checkout",
            "send_payment_link",
        }

    def test_no_last_search_candidates_no_checkout(self) -> None:
        state = _browse_state()
        state.last_search_candidates = []
        decision = try_selection_context_decision(_ctx(MSG_NAME_PRICE, state=state))
        assert decision is None

    def test_exploratory_price_question_no_checkout(self) -> None:
        decision = try_selection_context_decision(_ctx(MSG_EXPLORATORY))
        assert decision is None


class TestExistingSelectionPathsPreserved:
    def test_ordinal_first_still_works(self) -> None:
        decision = try_selection_context_decision(_ctx(MSG_ORDINAL_FIRST))
        assert decision is not None
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER
        assert decision.args["product"]["id"] == "23"

    def test_numeric_list_pick_still_works(self) -> None:
        decision = DefaultDecisionEngine().decide(_ctx("2"))
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER
        assert decision.args.get("list_index") == 2
        assert (decision.args.get("forced_product") or decision.args.get("product"))["id"] == "37"


class TestTurnArbiterComponentIntegration:
    def test_enforcement_preserves_verified_name_price_selection(self, monkeypatch) -> None:
        _enable_enforce_platform_wide(monkeypatch)
        ctx = _arbiter_ctx()
        legacy = _legacy_name_price_decision(ctx)

        enforced, result = maybe_enforce_turn_decision(ctx, legacy)
        assert result.enforced is False
        assert enforced.action == ACTION_PROPOSE_DRAFT_ORDER
        assert enforced.args["product"]["id"] == "37"
        assert not (enforced.args or {}).get("block_order_flow")


class TestTurnArbiterForgedMarkerRejection:
    def test_forged_candidate_id_cannot_bypass_checkout_vs_discovery(self, monkeypatch) -> None:
        _enable_enforce_platform_wide(monkeypatch)
        ctx = _arbiter_ctx()
        legacy = _legacy_name_price_decision(ctx)
        marker = dict(legacy.args.get(STRUCTURED_UNIQUE_SELECTION_KEY) or {})
        marker["candidate_id"] = "99"
        marker["external_id"] = "99"
        legacy.args[STRUCTURED_UNIQUE_SELECTION_KEY] = marker

        enforced, result = maybe_enforce_turn_decision(ctx, legacy)
        assert result.enforced is True
        assert result.mismatch_type == MISMATCH_CHECKOUT_VS_DISCOVERY
        assert enforced.action == ACTION_LLM_REPLY
        assert enforced.args.get("block_order_flow") is True

    def test_stale_marker_without_candidates_cannot_bypass(self, monkeypatch) -> None:
        _enable_enforce_platform_wide(monkeypatch)
        ctx = _arbiter_ctx()
        legacy = _legacy_name_price_decision(ctx)
        ctx.state.last_search_candidates = []

        enforced, result = maybe_enforce_turn_decision(ctx, legacy)
        assert result.enforced is True
        assert result.mismatch_type == MISMATCH_CHECKOUT_VS_DISCOVERY
        assert enforced.args.get("block_order_flow") is True

    def test_forged_price_mismatch_cannot_bypass(self, monkeypatch) -> None:
        _enable_enforce_platform_wide(monkeypatch)
        ctx = _arbiter_ctx()
        legacy = _legacy_name_price_decision(ctx)
        marker = dict(legacy.args.get(STRUCTURED_UNIQUE_SELECTION_KEY) or {})
        marker["stated_price"] = 999
        legacy.args[STRUCTURED_UNIQUE_SELECTION_KEY] = marker

        enforced, result = maybe_enforce_turn_decision(ctx, legacy)
        assert result.enforced is True
        assert result.mismatch_type == MISMATCH_CHECKOUT_VS_DISCOVERY
        assert enforced.args.get("block_order_flow") is True

    def test_marker_flag_alone_without_state_match_cannot_bypass(self, monkeypatch) -> None:
        _enable_enforce_platform_wide(monkeypatch)
        ctx = _arbiter_ctx()
        legacy = _legacy_name_price_decision(ctx)
        legacy.args[STRUCTURED_UNIQUE_SELECTION_KEY] = {
            "kind": NAME_PRICE_CANDIDATE_MATCH,
            "candidate_source": CANDIDATE_SOURCE_LAST_SEARCH,
            "candidate_id": "37",
            "external_id": "37",
            "stated_price": 114,
            "name_reference": "فستان",
            "verified_unique": True,
        }
        ctx.state.last_search_candidates = [DRESS_CANDIDATES[0]]

        enforced, result = maybe_enforce_turn_decision(ctx, legacy)
        assert result.enforced is True
        assert result.mismatch_type == MISMATCH_CHECKOUT_VS_DISCOVERY


class TestPipelineNamePriceSelection:
    def test_pipeline_orchestration_preserves_name_price_without_block_order_flow(
        self,
        monkeypatch,
    ) -> None:
        """Exercise MerchantBrain.process() decide+enforce path without mocking decide."""
        from modules.ai.brain.pipeline import get_brain  # noqa: PLC0415

        _enable_enforce_platform_wide(monkeypatch)
        session, _engine = make_scenario_db()
        tenant = seed_tenant(session, name="متجر تجريبي عام")
        customer = seed_customer(session, tenant.id, name="نورة عبدالله")
        conversation = seed_conversation(session, tenant.id, customer_id=customer.id)

        brain = get_brain()
        state = _browse_state(stale_checkout=True)
        selected = DRESS_CANDIDATES[1]

        stack = ExitStack()
        stack.enter_context(patch("core.billing.has_billing_access", return_value=True))
        stack.enter_context(
            patch(
                "core.wa_usage.check_limit",
                return_value=SimpleNamespace(
                    allowed=True,
                    used_total=0,
                    limit=1000,
                    reason="",
                ),
            )
        )
        stack.enter_context(
            patch(
                "core.ai_disabled_gate.is_ai_disabled_for_conversation",
                return_value=SimpleNamespace(disabled=False, reason=None),
            )
        )
        stack.enter_context(
            patch("core.store_knowledge.build_merchant_context", return_value={})
        )
        stack.enter_context(
            patch("core.active_order_context.load_commerce_bundle_from_db", return_value={})
        )
        stack.enter_context(
            patch.object(
                brain._classifier,
                "classify",
                return_value=Intent(
                    name=INTENT_ASK_PRICE,
                    confidence=0.92,
                    raw_message=MSG_NAME_PRICE,
                ),
            )
        )
        stack.enter_context(
            patch.object(brain._policy_gate, "gate", side_effect=lambda d, _ctx: d)
        )
        stack.enter_context(patch.object(brain._state_store, "load", return_value=state))
        stack.enter_context(patch.object(brain._state_store, "save"))
        stack.enter_context(patch.object(brain._facts_loader, "load", return_value=_facts()))
        stack.enter_context(patch.object(brain._memory_updater, "update"))
        stack.enter_context(
            patch.object(
                brain._executor,
                "execute",
                new=AsyncMock(
                    return_value=ActionResult(
                        success=True,
                        data={"product": selected, "await_quantity": True},
                    )
                ),
            )
        )
        stack.enter_context(
            patch.object(brain._composer, "compose", new=AsyncMock(return_value=""))
        )

        async def _run() -> dict:
            with stack:
                return await brain.process(
                    db=session,
                    tenant_id=tenant.id,
                    customer_phone="966542980511",
                    message=MSG_NAME_PRICE,
                    history=[],
                    profile={"id": customer.id, "name": "نورة عبدالله"},
                    customer_id=customer.id,
                    conversation_id=conversation.id,
                )

        try:
            output = asyncio.run(_run())
        finally:
            session.close()

        assert output["decision_action"] == ACTION_PROPOSE_DRAFT_ORDER
        assert output["decision_args"]["product"]["id"] == "37"
        assert output["decision_args"].get(STRUCTURED_UNIQUE_SELECTION_KEY, {}).get(
            "candidate_id"
        ) == "37"
        assert not output["decision_args"].get("turn_arbiter_enforced")
        assert not output["decision_args"].get("block_order_flow")
