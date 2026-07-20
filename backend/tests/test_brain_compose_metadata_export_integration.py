"""Integration regressions for Brain compose metadata export and live provenance."""
from __future__ import annotations

import asyncio
import os
import sys
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.compose.responder import DefaultComposer  # noqa: E402
from modules.ai.brain.catalog.navigation import PATH_TOP_FALLBACK  # noqa: E402
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_CATALOG_NAVIGATE,
    ACTION_LLM_REPLY,
    ACTION_SEARCH_PRODUCTS,
)
from modules.ai.brain.truth_surface.contract import (  # noqa: E402
    TrustedContextSnapshot,
    TrustedDomain,
    TrustedFact,
    TruthSource,
)
from modules.ai.brain.truth_surface.trusted_context import (  # noqa: E402
    clear_trusted_context,
    set_current_trusted_context,
)
from modules.ai.brain.types import (  # noqa: E402
    ActionResult,
    BrainContext,
    CommerceFacts,
    Decision,
    INTENT_GENERAL,
    Intent,
    MerchantConversationState,
)
from modules.ai.compose.reply_metadata_export import extract_reply_metadata_export  # noqa: E402
from modules.ai.orchestrator.types import AIReplyPayload  # noqa: E402
from services.internal_conversational_e2e_harness import _provenance_blockers  # noqa: E402
from services.merchant_brain_turn import (  # noqa: E402
    LiveMerchantBrainPreconditions,
    LiveMerchantBrainTurnInput,
    evaluate_live_merchant_brain_turn,
)
from tests.commerce_scenario_fixtures import (  # noqa: E402
    DEFAULT_PHONE_E164,
    make_scenario_db,
    seed_conversation,
    seed_customer,
    seed_tenant,
)
from tests.test_track_order_need_identifiers_compose import (  # noqa: E402
    _compose_need_identifiers,
    _ctx,
)
from tests.test_trusted_context_shadow_wireup import _run  # noqa: E402

_MERCHANT = "متجر تجريبي عام"
_PHONE = DEFAULT_PHONE_E164
_GENERAL_MESSAGE = "ما هي مدة التوصيل المتوقعة؟"
_GENERAL_REPLY = "مدة التوصيل تعتمد على مدينتك وتظهر لك عند إتمام الطلب."
_STORE_WIDE_MESSAGE = "عندكم عروض؟"
_PERSONA_REPLY = "في منتجات بأسعار مخفّضة حسب بيانات الكتالوج المؤكدة."
_TRACK_MESSAGE = "وين طلبي؟"


@pytest.fixture()
def db():
    session, _engine = make_scenario_db()
    yield session
    session.close()


@pytest.fixture()
def tenant_ctx(db):
    tenant = seed_tenant(db, name=_MERCHANT)
    customer = seed_customer(db, tenant.id, name="أحمد سالم")
    conv = seed_conversation(db, tenant.id, customer_id=customer.id)
    return SimpleNamespace(
        tenant_id=tenant.id,
        customer_id=customer.id,
        conversation_id=conv.id,
        phone=_PHONE,
    )


def _trace() -> SimpleNamespace:
    return SimpleNamespace(
        brain_called=False,
        brain_silent=False,
        response_goal="",
        response_mode="",
        reply_source="",
        fallback_source="",
        chosen_path="",
        handoff_triggered=False,
    )


def _convo() -> SimpleNamespace:
    return SimpleNamespace(
        id=91,
        extra_metadata={"brain_state": {"last_action": "ACTION_LLM_REPLY"}},
        status="active",
        is_human_handoff=False,
        needs_human=False,
        handoff_active=False,
    )


def _commerce_facts() -> CommerceFacts:
    return CommerceFacts(
        store_name=_MERCHANT,
        store_url="https://example.test",
        store_url_resolved=True,
        store_url_source="settings",
        has_products=True,
        product_count=3,
        in_stock_count=2,
        orderable=True,
        has_coupons=False,
    )


def _provider_payload(reply_text: str) -> AIReplyPayload:
    return AIReplyPayload(
        reply_text=reply_text,
        provider_used="openai_compatible",
        metadata={"model": "gpt-test"},
    )


def _base_brain_stack(brain, *, intent: Intent, decision: Decision, state: MerchantConversationState):
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
        patch(
            "core.store_knowledge.build_merchant_context",
            return_value={},
        )
    )
    stack.enter_context(patch.object(brain._classifier, "classify", return_value=intent))
    stack.enter_context(patch.object(brain._decision_engine, "decide", return_value=decision))
    stack.enter_context(patch.object(brain._policy_gate, "gate", side_effect=lambda d, _ctx: d))
    stack.enter_context(patch.object(brain._state_store, "load", return_value=state))
    stack.enter_context(patch.object(brain._state_store, "save"))
    stack.enter_context(patch.object(brain._facts_loader, "load", return_value=_commerce_facts()))
    stack.enter_context(patch.object(brain._memory_updater, "update"))
    return stack


def _provenance_dict_from_turn(result) -> dict:
    provenance = result.provenance
    return {
        "compose_source": provenance.compose_source,
        "response_mode": provenance.response_mode,
        "chosen_path": provenance.chosen_path,
        "llm_candidate_present": provenance.llm_candidate_present,
        "final_text_transformed": provenance.final_text_transformed,
        "final_transform_reasons": list(provenance.final_transform_reasons),
        "fallback_reason": provenance.fallback_reason,
        "fallback_action_type": provenance.fallback_action_type,
    }


_TENANT_48_CASES = (
    {
        "case": "search_miss",
        "message": "كم سعر قميص قطني أزرق؟",
        "intent": "ask_price",
        "decision": Decision(
            action=ACTION_SEARCH_PRODUCTS,
            args={"query": "قميص قطني أزرق"},
        ),
        "action_result": ActionResult(
            success=False,
            error="no matching products",
            data={
                "message": "no_matching_products",
                "query": "قميص قطني أزرق",
            },
        ),
        "provider_reply": "ما ظهر تطابق مؤكد لقميص قطني أزرق في الكتالوج حالياً.",
        "chosen_path": "catalog_miss_resolved_subject",
    },
    {
        "case": "zero_eligible_catalog_qa",
        "message": "كم سعر عطر ورد 100ml؟",
        "intent": "ask_price",
        "decision": Decision(
            action=ACTION_SEARCH_PRODUCTS,
            args={"query": "عطر ورد 100ml"},
        ),
        "action_result": ActionResult(
            success=True,
            data={
                "query": "عطر ورد 100ml",
                "products": [
                    {
                        "id": 4802,
                        "title": "عطر ورد 100ml",
                        "category": "عطور",
                        "price": 189,
                        "can_checkout": False,
                    },
                ],
            },
        ),
        "provider_reply": "المنتج موجود في بيانات الكتالوج، وهو غير متاح للطلب حالياً.",
        "chosen_path": "fact_bound_persona_compose",
    },
    {
        "case": "zero_fact_navigation",
        "message": "وش عندكم؟",
        "intent": "ask_product",
        "decision": Decision(
            action=ACTION_CATALOG_NAVIGATE,
            args={
                "chosen_path": PATH_TOP_FALLBACK,
                "navigator_step": "top_products_fallback",
            },
        ),
        "action_result": ActionResult(
            success=True,
            data={
                "chosen_path": PATH_TOP_FALLBACK,
                "discovery_output_kind": "products",
                "products": [],
                "navigator_no_groups_fallback": True,
                "turn_owner": "catalog_navigation",
                "owner_locked": True,
            },
        ),
        "provider_reply": "ما ظهرت منتجات مؤكدة في الكتالوج حالياً.",
        "chosen_path": PATH_TOP_FALLBACK,
    },
)


@pytest.mark.parametrize("case", _TENANT_48_CASES, ids=lambda case: case["case"])
def test_tenant48_pr687_paths_export_live_provenance(
    db,
    tenant_ctx,
    case,
) -> None:
    """Exercise #687 ownership through responder, pipeline, live boundary and blockers."""
    from modules.ai.brain.pipeline import get_brain  # noqa: PLC0415

    brain = get_brain()
    state = MerchantConversationState(stage="exploring", greeted=True)
    stack = _base_brain_stack(
        brain,
        intent=Intent(name=case["intent"], confidence=0.95, slots={}),
        decision=case["decision"],
        state=state,
    )
    stack.enter_context(
        patch.object(
            brain._executor,
            "execute",
            new=AsyncMock(return_value=case["action_result"]),
        )
    )
    openai_call = stack.enter_context(
        patch(
            "modules.ai.orchestrator.providers.openai_compatible_provider."
            "OpenAICompatibleProvider.call",
            return_value={
                "reply_text": case["provider_reply"],
                "model": "tenant48-integration",
            },
        )
    )
    anthropic_call = stack.enter_context(
        patch(
            "modules.ai.orchestrator.providers.anthropic_provider."
            "AnthropicProvider.call",
            return_value={
                "reply_text": case["provider_reply"],
                "model": "tenant48-integration",
            },
        )
    )
    stack.enter_context(
        patch(
            "modules.ai.orchestrator.providers.openai_compatible_provider."
            "OpenAICompatibleProvider.is_configured",
            return_value=True,
        )
    )
    stack.enter_context(
        patch(
            "modules.ai.orchestrator.providers.anthropic_provider."
            "AnthropicProvider.is_configured",
            return_value=True,
        )
    )

    async def invoke():
        with stack:
            return await evaluate_live_merchant_brain_turn(
                db=db,
                tenant_id=tenant_ctx.tenant_id,
                phone_id="PH-TENANT48",
                turn_input=LiveMerchantBrainTurnInput(
                    customer_phone=tenant_ctx.phone,
                    text=case["message"],
                    conversation_id=tenant_ctx.conversation_id,
                    history=[],
                    preconditions=LiveMerchantBrainPreconditions(),
                    profile={
                        "id": tenant_ctx.customer_id,
                        "name": "Generic Customer",
                        "preferred_language": "ar",
                    },
                ),
                convo=_convo(),
                trace=_trace(),
                persona_ownership=MagicMock(),
                brain_factory=lambda: brain,
                brain_active=True,
            )

    turn_result = asyncio.run(invoke())
    provider_calls = openai_call.call_count + anthropic_call.call_count
    assert turn_result.status == "evaluated"
    assert provider_calls == 1
    assert turn_result.brain_result is not None
    assert turn_result.brain_result["chosen_path"] == case["chosen_path"]
    assert turn_result.provenance.chosen_path == case["chosen_path"]
    assert turn_result.provenance.compose_source == "persona_llm", turn_result.brain_result
    assert turn_result.provenance.response_mode == "grounded_persona_compose"
    assert turn_result.provenance.llm_candidate_present is True
    assert turn_result.provenance.final_text_transformed is False
    assert turn_result.provenance.final_transform_reasons == []
    assert turn_result.reply_text == turn_result.brain_reply_candidate
    assert "catalog_deterministic_fallback" not in str(turn_result.brain_result)
    if case["case"] in {"search_miss", "zero_fact_navigation"}:
        assert turn_result.provenance.persona_route is not None
        assert turn_result.provenance.persona_route.compose_attempt == "provider_call"
    assert _provenance_blockers(
        _provenance_dict_from_turn(turn_result),
        evaluated_customer_text=True,
    ) == []


def test_general_llm_compose_stamps_metadata_with_provider_only_mock(db, tenant_ctx) -> None:
    ctx = _ctx(tenant_ctx, _GENERAL_MESSAGE, db=db)
    decision = Decision(action=ACTION_LLM_REPLY, args={})
    result = SimpleNamespace(data={})
    composer = DefaultComposer()

    async def _run_compose():
        with patch(
            "modules.ai.orchestrator.adapter.generate_ai_reply",
            return_value=_provider_payload(_GENERAL_REPLY),
        ):
            return await composer._llm_compose(ctx, result, decision=decision)

    reply = asyncio.run(_run_compose())
    assert reply == _GENERAL_REPLY
    assert result.data["compose_source"] == "llm"
    assert result.data["llm_candidate_present"] is True
    assert result.data["final_customer_text_source"] == "llm"


def test_pipeline_exports_general_llm_metadata_from_result_data(db, tenant_ctx) -> None:
    from modules.ai.brain.pipeline import get_brain  # noqa: PLC0415

    brain = get_brain()
    intent = Intent(name=INTENT_GENERAL, confidence=0.9, slots={})
    decision = Decision(action=ACTION_LLM_REPLY, args={})
    state = MerchantConversationState(stage="browsing", greeted=True)

    stack = _base_brain_stack(brain, intent=intent, decision=decision, state=state)
    stack.enter_context(
        patch(
            "modules.ai.orchestrator.adapter.generate_ai_reply",
            return_value=_provider_payload(_GENERAL_REPLY),
        )
    )
    with stack:
        result = _run(
            brain.process(
                db=db,
                tenant_id=tenant_ctx.tenant_id,
                customer_phone=tenant_ctx.phone,
                message=_GENERAL_MESSAGE,
                history=[],
                profile={"preferred_language": "ar", "id": tenant_ctx.customer_id},
                conversation_id=tenant_ctx.conversation_id,
            )
        )

    assert result.get("compose_source") == "llm"
    assert result.get("response_mode") == "llm"
    assert result.get("chosen_path") == "llm"
    assert result.get("llm_candidate_present") is True
    assert result.get("final_text_transformed") is False
    assert result.get("final_transform_reasons") == []
    assert result.get("final_customer_text_source") == "llm"
    assert (result.get("reply") or "").strip() == _GENERAL_REPLY


def test_live_turn_general_llm_provenance_passes_blockers(db, tenant_ctx) -> None:
    from modules.ai.brain.pipeline import get_brain  # noqa: E402

    brain = get_brain()
    intent = Intent(name=INTENT_GENERAL, confidence=0.9, slots={})
    decision = Decision(action=ACTION_LLM_REPLY, args={})
    state = MerchantConversationState(stage="browsing", greeted=True)

    stack = _base_brain_stack(brain, intent=intent, decision=decision, state=state)
    stack.enter_context(
        patch(
            "modules.ai.orchestrator.adapter.generate_ai_reply",
            return_value=_provider_payload(_GENERAL_REPLY),
        )
    )

    async def invoke():
        with stack:
            return await evaluate_live_merchant_brain_turn(
                db=db,
                tenant_id=tenant_ctx.tenant_id,
                phone_id="PH-META",
                turn_input=LiveMerchantBrainTurnInput(
                    customer_phone=tenant_ctx.phone,
                    text=_GENERAL_MESSAGE,
                    conversation_id=tenant_ctx.conversation_id,
                    history=[],
                    preconditions=LiveMerchantBrainPreconditions(),
                    profile={"id": tenant_ctx.customer_id, "name": "Generic Customer"},
                ),
                convo=_convo(),
                trace=_trace(),
                persona_ownership=MagicMock(),
                brain_factory=lambda: brain,
                brain_active=True,
            )

    turn_result = asyncio.run(invoke())
    assert turn_result.status == "evaluated"
    assert turn_result.provenance.compose_source == "llm"
    assert turn_result.provenance.llm_candidate_present is True
    assert turn_result.provenance.final_text_transformed is False
    assert _provenance_blockers(
        _provenance_dict_from_turn(turn_result),
        evaluated_customer_text=True,
    ) == []


def _sale_snapshot() -> TrustedContextSnapshot:
    snap = TrustedContextSnapshot(
        tenant_id=88002,
        facts=[
            TrustedFact(
                domain=TrustedDomain.CATALOG,
                key="catalog:product_sale_offer",
                value={
                    "domain": TrustedDomain.CATALOG.value,
                    "bundle_namespace": "product_sale_offer",
                    "question_kind": "store_wide",
                    "product_sale_availability": "active_sale_present",
                    "verified_on_sale_product_count": 1,
                    "sample_products": [
                        {
                            "title": "حذاء رياضي أبيض",
                            "sale_price": "80",
                            "regular_price": "100",
                        },
                    ],
                    "allow_price_mention": True,
                },
                source=TruthSource.PRODUCTS_TABLE,
                path="products_table.on_sale_count",
            )
        ],
    )
    snap.ensure_snapshot_id()
    return snap


def test_pipeline_catalog_persona_exports_structured_metadata() -> None:
    from modules.ai.brain.pipeline import get_brain  # noqa: PLC0415

    clear_trusted_context()
    brain = get_brain()
    snap = _sale_snapshot()
    intent = Intent(name=INTENT_GENERAL, confidence=0.92, slots={})
    decision = Decision(
        action=ACTION_LLM_REPLY,
        args={"chosen_path": "general_offer_discovery_compose"},
    )
    state = MerchantConversationState(stage="browsing", greeted=True)

    stack = _base_brain_stack(brain, intent=intent, decision=decision, state=state)
    for target in (
        "modules.ai.brain.truth_surface.product_sale_offer_consumption_gate."
        "is_general_offer_discovery_compose_enabled",
        "modules.ai.brain.persona.general_offer_discovery_answer."
        "is_general_offer_discovery_compose_enabled",
        "modules.ai.brain.truth_surface.product_sale_offer_consumption_gate."
        "is_product_sale_offer_compose_enabled",
        "modules.ai.brain.persona.product_sale_offer_answer."
        "is_product_sale_offer_compose_enabled",
    ):
        stack.enter_context(patch(target, return_value=True))
    stack.enter_context(
        patch(
            "modules.ai.orchestrator.providers.openai_compatible_provider."
            "OpenAICompatibleProvider.call",
            return_value={"reply_text": _PERSONA_REPLY, "model": "gpt-test"},
        )
    )
    stack.enter_context(
        patch(
            "modules.ai.orchestrator.providers.openai_compatible_provider."
            "OpenAICompatibleProvider.is_configured",
            return_value=True,
        )
    )
    set_current_trusted_context(snap)
    with stack:
        result = _run(
            brain.process(
                db=MagicMock(),
                tenant_id=88002,
                customer_phone=_PHONE,
                message=_STORE_WIDE_MESSAGE,
                history=[],
                profile={"preferred_language": "ar"},
                conversation_id=44,
            )
        )

    try:
        assert result.get("compose_source") == "persona_llm"
        assert result.get("chosen_path") == "general_offer_discovery_compose"
        assert result.get("llm_candidate_present") is True
        assert result.get("final_text_transformed") is False
        assert result.get("final_transform_reasons") == []
        assert _PERSONA_REPLY in (result.get("reply") or "")
    finally:
        clear_trusted_context()


def test_track_order_fallback_compose_exports_metadata(db, tenant_ctx) -> None:
    _decision, result, reply, _ctx = asyncio.run(
        _compose_need_identifiers(
            db,
            tenant_ctx,
            _TRACK_MESSAGE,
            llm_reply=None,
            force_compose_fail=True,
        )
    )
    exported = extract_reply_metadata_export(
        result.data,
        chosen_path=str(result.data.get("chosen_path") or ""),
    )
    assert exported["compose_source"] == "fallback_deterministic"
    assert exported["fallback_reason"] == "compose_failed_or_empty"
    assert exported["fallback_action_type"] == "track_order_need_identifiers"
    assert exported["llm_candidate_present"] is False
    assert (reply or "").strip()


def test_live_turn_audited_fallback_provenance_passes_blockers(db, tenant_ctx) -> None:
    _decision, compose_result, reply, _ctx = asyncio.run(
        _compose_need_identifiers(
            db,
            tenant_ctx,
            _TRACK_MESSAGE,
            llm_reply=None,
            force_compose_fail=True,
        )
    )
    chosen_path = str(compose_result.data.get("chosen_path") or "")
    brain_result = {
        "reply": reply,
        "buttons": [],
        "handoff": False,
        "chosen_path": chosen_path,
        **extract_reply_metadata_export(compose_result.data, chosen_path=chosen_path),
        "track_order_need_identifiers_compose_active": True,
    }
    brain = MagicMock()
    brain.process = AsyncMock(return_value=brain_result)

    async def invoke():
        return await evaluate_live_merchant_brain_turn(
            db=db,
            tenant_id=tenant_ctx.tenant_id,
            phone_id="PH-FALLBACK",
            turn_input=LiveMerchantBrainTurnInput(
                customer_phone=tenant_ctx.phone,
                text=_TRACK_MESSAGE,
                conversation_id=tenant_ctx.conversation_id,
                history=[],
                preconditions=LiveMerchantBrainPreconditions(),
                profile={"id": tenant_ctx.customer_id, "name": "Generic Customer"},
            ),
            convo=_convo(),
            trace=_trace(),
            persona_ownership=MagicMock(),
            brain_factory=lambda: brain,
            brain_active=True,
        )

    turn_result = asyncio.run(invoke())
    assert turn_result.status == "evaluated"
    assert turn_result.provenance.compose_source == "fallback_deterministic"
    assert turn_result.provenance.llm_candidate_present is False
    assert turn_result.provenance.fallback_reason == "compose_failed_or_empty"
    assert turn_result.provenance.fallback_action_type == "track_order_need_identifiers"
    assert _provenance_blockers(
        _provenance_dict_from_turn(turn_result),
        evaluated_customer_text=True,
    ) == []
