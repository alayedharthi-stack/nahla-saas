"""LIVE-BARE-EXTERNAL-URL-STOLEN-BY-CHECKOUT-CONTINUATION-D4

Non-checkout turns must not receive stale checkout-execution facts.

INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
PERSONA_CHANGED=NO
PHRASE_MAP_CHANGED=NO
KEYWORD_ROUTER_CHANGED=NO
CUSTOMER_REGEX_CHANGED=NO

Customer phrases are TEST INPUT only. Assert facts, Decision, ownership,
and provenance — not exact customer-facing Arabic wording.
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
from dataclasses import asdict
from typing import Any, Optional
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
_REPO = os.path.abspath(os.path.join(_BACKEND, ".."))
for _p in (_REPO, _BACKEND, os.path.join(_REPO, "database")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.inbound_url_spans import (  # noqa: E402
    is_url_only_inbound,
    semantic_text_excluding_url_spans,
)
from core.order_flow import context_aware_dedup_fallback  # noqa: E402
from core.wa_cart_line_items import ITEM_STATUS_CONFIRMED  # noqa: E402
from core.wa_draft_confirmation import maybe_inject_draft_flow_reply  # noqa: E402
from modules.ai.brain.commerce.complaint_refund_topic_guard import (  # noqa: E402
    should_block_order_draft_injection,
)
from modules.ai.brain.compose.prompt_builder import (  # noqa: E402
    build_brain_reply_prompt,
)
from modules.ai.brain.compose.responder import DefaultComposer  # noqa: E402
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_LLM_REPLY,
    ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_TRACK_ORDER,
)
from modules.ai.brain.decision.checkout_continuation_evidence import (  # noqa: E402
    has_positive_checkout_ownership,
    select_last_question,
    select_pending_action,
    should_stamp_checkout_progress,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.intent import rules as intent_rules  # noqa: E402
from modules.ai.brain.pipeline import _build_reply_state  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    ActionResult,
    BrainContext,
    CommerceFacts,
    INTENT_ASK_PRODUCT,
    INTENT_GENERAL,
    INTENT_GREETING,
    INTENT_START_ORDER,
    INTENT_TRACK_ORDER,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
    SuggestionSnapshot,
)
from modules.ai.orchestrator.types import AIReplyPayload  # noqa: E402

GENERIC_MERCHANT = "متجر تجريبي عام"
GENERIC_CITY = "الرياض"
GENERIC_SHORT = "RRRD1234"
GENERIC_PRODUCT_ID = "sku-white-sneaker"
GENERIC_PRODUCT_TITLE = "حذاء رياضي أبيض"
GENERIC_URL = "https://example.net/share/video/abc123"
RESUME_PHRASE = "أبغى أكمل الطلب السابق"
MODEL_CANDIDATE = "model-owned unrelated-turn candidate"
PREVIOUS_CHECKOUT_QUESTION = "أرسل لي التفاصيل الناقصة لإكمال الطلب."
NUDGE_MARKER = "تأمر بشيء أكمّل لك فيه؟"
RULE_E_MARKER = "اختياراتك محفوظة في هذه المحادثة مبدئيًا"
SYNTH_PHONE = "966511000099"
SYNTH_ADDRESS = "حي النخيل شارع التجربة 12"
SYNTH_PAYMENT = "bank_transfer"
SESSION_PHONE = "966500000099"


def _line_item() -> dict[str, Any]:
    return {
        "product_id": GENERIC_PRODUCT_ID,
        "title": GENERIC_PRODUCT_TITLE,
        "quantity": 1,
        "unit_price": 199.0,
        "match_status": ITEM_STATUS_CONFIRMED,
        "last_updated_at": "2026-09-03T09:27:46.921840+00:00",
    }


def _active_checkout_state(
    *,
    pending: str = "collect_checkout_details",
    last_question: str = PREVIOUS_CHECKOUT_QUESTION,
) -> MerchantConversationState:
    prep = OrderPreparationState(
        product_id=GENERIC_PRODUCT_ID,
        quantity=1,
        customer_first_name="أحمد",
        customer_last_name="سالم",
        customer_phone=SYNTH_PHONE,
        city=GENERIC_CITY,
        short_address_code=GENERIC_SHORT,
        address_line=SYNTH_ADDRESS,
        payment_method=SYNTH_PAYMENT,
        missing_fields=["payment_method"],
        order_status="awaiting_address",
        line_items=[_line_item()],
        catalog_line_items_authoritative=True,
    )
    return MerchantConversationState(
        stage="ordering",
        greeted=True,
        current_product_focus={
            "id": GENERIC_PRODUCT_ID,
            "external_id": GENERIC_PRODUCT_ID,
            "title": GENERIC_PRODUCT_TITLE,
            "price": 199.0,
        },
        selected_product_id=GENERIC_PRODUCT_ID,
        checkout_url=None,
        draft_order_id="draft-9001",
        last_search_candidates=[],
        pending_action=pending,
        last_question_asked=last_question,
        last_question_answered=False,
        recommended_next_step=pending,
        cart_items=[_line_item()],
        turn=12,
        updated_at="2026-09-04T17:47:22+00:00",
        order_prep=prep,
        last_action="propose_draft_order",
        last_intent="start_order",
        payment_method=SYNTH_PAYMENT,
    )


def _classify(message: str) -> Intent:
    matched = intent_rules.match(message)
    if matched is not None:
        return matched
    return Intent(
        name=INTENT_GENERAL,
        confidence=0.5,
        raw_message=message,
        slots={},
        extraction_method="rules",
    )


def _ctx(
    message: str,
    *,
    intent: Optional[Intent] = None,
    slots: Optional[dict[str, Any]] = None,
    orderable: bool = True,
    state: Optional[MerchantConversationState] = None,
    tenant_id: int = 9001,
    phone: str = SESSION_PHONE,
) -> BrainContext:
    resolved = intent or _classify(message)
    if slots:
        resolved.slots = dict(slots)
    return BrainContext(
        tenant_id=tenant_id,
        customer_phone=phone,
        message=message,
        intent=resolved,
        state=state or _active_checkout_state(),
        facts=CommerceFacts(
            store_name=GENERIC_MERCHANT,
            has_products=True,
            product_count=4,
            in_stock_count=4,
            orderable=orderable,
            snapshot_fresh=True,
        ),
    )


def _snapshot(state: MerchantConversationState) -> dict[str, Any]:
    prep = getattr(state, "order_prep", None)
    items = list(getattr(prep, "line_items", None) or [])
    return {
        "stage": state.stage,
        "focus_id": (state.current_product_focus or {}).get("id"),
        "checkout_url": state.checkout_url,
        "draft_order_id": state.draft_order_id,
        "pending_action": state.pending_action,
        "last_question_asked": state.last_question_asked,
        "product_id": getattr(prep, "product_id", None),
        "quantity": getattr(prep, "quantity", None),
        "first_name": getattr(prep, "customer_first_name", None),
        "phone": getattr(prep, "customer_phone", None),
        "city": getattr(prep, "city", None),
        "short_code": getattr(prep, "short_address_code", None),
        "address": getattr(prep, "address_line", None),
        "payment_method": getattr(prep, "payment_method", None),
        "line_items": copy.deepcopy(items),
        "cart_items": copy.deepcopy(list(state.cart_items or [])),
        "line_item_updated_at": (items[0].get("last_updated_at") if items else None),
        "updated_at": state.updated_at,
    }


def _stale_brain_dict(state: MerchantConversationState) -> dict[str, Any]:
    return {
        "stage": state.stage,
        "current_product_focus": dict(state.current_product_focus or {}),
        "pending_action": state.pending_action,
        "cart_items": copy.deepcopy(list(state.cart_items or [])),
        "order_prep": state.order_prep.to_dict() if hasattr(state.order_prep, "to_dict") else {},
        "updated_at": state.updated_at,
    }


def _pipeline_post_compose_draft_inject(
    *,
    reply: str,
    order_prep: Any,
    brain_state: Any,
    decision: Any,
    customer_message: str = "",
    ctx: Any = None,
) -> str:
    if should_block_order_draft_injection(
        brain_state=brain_state,
        customer_message=customer_message or "",
        decision=decision,
        history=[],
        ctx=ctx,
    ):
        return reply or ""
    return maybe_inject_draft_flow_reply(
        reply=reply or "",
        order_prep=order_prep,
        brain_state=brain_state,
        cart_changed=False,
        customer_message=customer_message or "",
        history=[],
    )


def _dedup_with_state(
    *,
    inbound: str,
    decision: Any,
    default_fallback: str,
    state: MerchantConversationState,
) -> str:
    bs = _stale_brain_dict(state)
    import core.order_flow as of

    def _fake_load(_db, _tenant_id, _phone):
        return None, bs

    orig_load = of._load_brain_state
    try:
        of._load_brain_state = _fake_load
        return context_aware_dedup_fallback(
            object(),
            tenant_id=9001,
            phone=SESSION_PHONE,
            history=[],
            default_fallback=default_fallback,
            inbound_text=inbound,
            decision=decision,
            decision_action=str(getattr(decision, "action", "") or ""),
            decision_args=dict(getattr(decision, "args", None) or {}),
        )
    finally:
        of._load_brain_state = orig_load


def _prep_blob(value: Any) -> str:
    if isinstance(value, dict):
        nonempty = {
            key: item
            for key, item in value.items()
            if item not in (None, "", [], {}, 0, False)
        }
        return json.dumps(nonempty, ensure_ascii=False)
    return json.dumps(value, ensure_ascii=False) if value else ""


def _assert_no_checkout_execution_facts(reply_state: Any, prompt: str = "") -> None:
    facts = dict(getattr(reply_state, "known_facts", None) or {})
    prep = facts.get("checkout_preparation") or {}
    identity = facts.get("checkout_identity_shipping") or {}
    assert prep == {} or _prep_blob(prep) in {"{}", ""}
    assert not identity
    assert not facts.get("checkout_missing_fields")
    assert not facts.get("next_missing_field")
    assert not facts.get("resume_missing_fields")
    pending = str(getattr(reply_state, "explicit_pending_action", "") or "")
    assert pending not in {
        "collect_checkout_details",
        "complete_checkout",
        "complete_payment",
        "confirm_order_details",
        "collect_missing_detail",
    }
    assert not str(getattr(reply_state, "last_question_asked", "") or "")
    assert str(getattr(reply_state, "stage", "") or "") != "ordering"
    blob = " ".join(
        [
            json.dumps(facts, ensure_ascii=False),
            prompt or "",
            str(getattr(reply_state, "response_goal", "") or ""),
            str(getattr(reply_state, "recommended_next_step", "") or ""),
        ]
    )
    assert SYNTH_PHONE not in blob
    assert SYNTH_ADDRESS not in blob
    assert GENERIC_SHORT not in blob
    assert SYNTH_PAYMENT not in blob
    nav = facts.get("commerce_navigator") or {}
    assert str(nav.get("stage") or "") != "whatsapp_quick_order"
    assert str(nav.get("next_goal") or "") not in {
        "collect_payment_method_for_whatsapp_order",
        "collect_or_confirm_delivery_address",
        "confirm_customer_order_and_shipping_details_once",
        "continue_checkout",
    }


def _run_full_path(message: str, *, state: Optional[MerchantConversationState] = None) -> dict[str, Any]:
    ctx = _ctx(message, state=state or _active_checkout_state())
    before = _snapshot(ctx.state)
    decision = DefaultDecisionEngine().decide(ctx)
    reply_state = _build_reply_state(
        ctx=ctx,
        previous_state=ctx.state,
        current_state=ctx.state,
        suggestion=SuggestionSnapshot(),
        decision=decision,
        db=None,
    )
    ctx.reply_state = reply_state
    prompt = build_brain_reply_prompt(reply_state)
    captured: dict[str, Any] = {
        "compose_count": 0,
        "fetch_count": 0,
        "prompt": "",
        "brain_state": {},
    }

    def _fake_generate_ai_reply(**kwargs: Any) -> AIReplyPayload:
        captured["compose_count"] += 1
        overrides = dict(kwargs.get("prompt_overrides") or {})
        captured["prompt"] = str(overrides.get("__full_system_prompt") or "")
        meta = dict(kwargs.get("context_metadata") or {})
        captured["brain_state"] = dict(meta.get("brain_state") or {})
        return AIReplyPayload(reply_text=MODEL_CANDIDATE)

    async def _compose() -> str:
        result = ActionResult(success=True, data={})
        with patch(
            "modules.ai.orchestrator.adapter.generate_ai_reply",
            side_effect=_fake_generate_ai_reply,
        ), patch(
            "modules.ai.brain.persona.integration.try_enforce_phatic_llm_persona_compose",
            return_value=None,
        ), patch(
            "urllib.request.urlopen",
            side_effect=AssertionError("external fetch must not run"),
        ) as fetch:
            try:
                text = await DefaultComposer().compose(decision, result, ctx)
            finally:
                captured["fetch_count"] = int(getattr(fetch, "call_count", 0) or 0)
            captured["result_data"] = dict(result.data or {})
            return text

    composed = asyncio.run(_compose())
    injected = _pipeline_post_compose_draft_inject(
        reply=composed,
        order_prep=ctx.state.order_prep,
        brain_state=ctx.state,
        decision=decision,
        customer_message=message,
        ctx=ctx,
    )
    final = _dedup_with_state(
        inbound=message,
        decision=decision,
        default_fallback=injected,
        state=ctx.state,
    )
    pending = select_pending_action(
        previous=before["pending_action"],
        suggested=ctx.state.pending_action,
        decision=decision,
        ctx=ctx,
    )
    question, _answered = select_last_question(
        previous_question=before["last_question_asked"],
        previous_answered=False,
        asked_now="",
        suggested_next_step=ctx.state.pending_action,
        decision=decision,
        ctx=ctx,
    )
    return {
        "ctx": ctx,
        "decision": decision,
        "reply_state": reply_state,
        "prompt": captured["prompt"] or prompt,
        "builder_prompt": prompt,
        "brain_state": captured["brain_state"],
        "compose_count": captured["compose_count"],
        "fetch_count": captured["fetch_count"],
        "composed": composed,
        "injected": injected,
        "final": final,
        "before": before,
        "after": _snapshot(ctx.state),
        "pending": pending,
        "question": question,
        "stamp": should_stamp_checkout_progress(decision=decision, ctx=ctx),
        "owned": has_positive_checkout_ownership(decision=decision, ctx=ctx),
    }


class TestAExactLiveUrlOnlyState:
    def test_url_only_quarantines_checkout_execution_facts(self) -> None:
        state = _active_checkout_state()
        assert state.stage == "ordering"
        assert state.current_product_focus
        assert state.order_prep.line_items
        assert state.order_prep.order_status == "awaiting_address"
        assert is_url_only_inbound(GENERIC_URL) is True
        assert semantic_text_excluding_url_spans(GENERIC_URL) == ""
        out = _run_full_path(GENERIC_URL, state=state)
        assert out["decision"].action == ACTION_LLM_REPLY
        assert out["owned"] is False
        _assert_no_checkout_execution_facts(out["reply_state"], out["prompt"])


class TestBSerializedModelPayloadProof:
    def test_final_model_payload_has_no_checkout_keys(self) -> None:
        out = _run_full_path(GENERIC_URL)
        payload = json.dumps(out["brain_state"] or asdict(out["reply_state"]), ensure_ascii=False)
        prompt = out["prompt"] or out["builder_prompt"]
        blob = payload + "\n" + prompt
        assert "checkout_identity_shipping" not in blob or '"checkout_identity_shipping": {}' in blob or '"checkout_identity_shipping": null' in blob
        facts = dict((out["brain_state"] or asdict(out["reply_state"])).get("known_facts") or {})
        if not facts:
            facts = dict(out["reply_state"].known_facts or {})
        assert not (facts.get("checkout_preparation") or {})
        assert not (facts.get("checkout_identity_shipping") or {})
        assert SYNTH_PHONE not in blob
        assert SYNTH_ADDRESS not in blob
        assert GENERIC_SHORT not in blob
        assert SYNTH_PAYMENT not in blob
        assert "CHECKOUT_IDENTITY_SHIPPING_FACTS" not in prompt
        assert "CATALOG_ORDER_FACTS" not in prompt


class TestCModelOwnedNeutralResponse:
    def test_compose_once_no_injection_or_nudge(self) -> None:
        out = _run_full_path(GENERIC_URL)
        assert out["compose_count"] == 1
        assert out["final"] == MODEL_CANDIDATE
        assert out["composed"] == MODEL_CANDIDATE
        assert RULE_E_MARKER not in (out["injected"] or "")
        assert NUDGE_MARKER not in (out["final"] or "")
        assert out["final"] == out["composed"]


class TestDNoSensitiveDisclosure:
    def test_payload_and_final_hide_saved_checkout_pii(self) -> None:
        out = _run_full_path(GENERIC_URL)
        blob = "\n".join(
            [
                json.dumps(asdict(out["reply_state"]), ensure_ascii=False),
                out["prompt"] or out["builder_prompt"],
                out["final"],
            ]
        )
        assert SYNTH_PHONE not in blob
        assert SYNTH_ADDRESS not in blob
        assert GENERIC_SHORT not in blob
        assert SYNTH_PAYMENT not in blob


class TestEExplicitResumeControl:
    def test_resume_keeps_checkout_facts(self) -> None:
        ctx = _ctx(RESUME_PHRASE)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER
        assert has_positive_checkout_ownership(decision=decision, ctx=ctx) is True
        reply_state = _build_reply_state(
            ctx=ctx,
            previous_state=ctx.state,
            current_state=ctx.state,
            suggestion=SuggestionSnapshot(),
            decision=decision,
            db=None,
        )
        prep = dict((reply_state.known_facts or {}).get("checkout_preparation") or {})
        assert prep.get("customer_first_name") == "أحمد"
        assert prep.get("city") == GENERIC_CITY
        assert prep.get("short_address_code") == GENERIC_SHORT


class TestFAddressMapsControl:
    def test_maps_answer_keeps_checkout_ownership(self) -> None:
        maps = "https://maps.app.goo.gl/abc123"
        ctx = _ctx(maps, slots={"google_maps_url": maps})
        ctx.state.order_prep.missing_fields = ["google_maps_url", "delivery_address"]
        decision = DefaultDecisionEngine().decide(ctx)
        assert has_positive_checkout_ownership(decision=decision, ctx=ctx) is True
        reply_state = _build_reply_state(
            ctx=ctx,
            previous_state=ctx.state,
            current_state=ctx.state,
            suggestion=SuggestionSnapshot(),
            decision=decision,
            db=None,
        )
        prep = dict((reply_state.known_facts or {}).get("checkout_preparation") or {})
        assert prep.get("address_line") == SYNTH_ADDRESS


class TestGPaymentControl:
    def test_payment_answer_keeps_checkout_facts(self) -> None:
        ctx = _ctx("تحويل")
        ctx.state.order_prep.missing_fields = ["payment_method"]
        decision = DefaultDecisionEngine().decide(ctx)
        assert has_positive_checkout_ownership(decision=decision, ctx=ctx) is True
        reply_state = _build_reply_state(
            ctx=ctx,
            previous_state=ctx.state,
            current_state=ctx.state,
            suggestion=SuggestionSnapshot(),
            decision=decision,
            db=None,
        )
        prep = dict((reply_state.known_facts or {}).get("checkout_preparation") or {})
        assert prep.get("payment_method") == SYNTH_PAYMENT


class TestHQuantityVariantControl:
    def test_quantity_slot_keeps_checkout_ownership(self) -> None:
        ctx = _ctx("2", slots={"quantity": 2})
        ctx.state.order_prep.missing_fields = ["quantity"]
        decision = DefaultDecisionEngine().decide(ctx)
        assert has_positive_checkout_ownership(decision=decision, ctx=ctx) is True
        reply_state = _build_reply_state(
            ctx=ctx,
            previous_state=ctx.state,
            current_state=ctx.state,
            suggestion=SuggestionSnapshot(),
            decision=decision,
            db=None,
        )
        prep = dict((reply_state.known_facts or {}).get("checkout_preparation") or {})
        assert prep.get("product_id") == GENERIC_PRODUCT_ID


class TestIGenuineCheckoutDecision:
    def test_start_order_reaches_checkout_facts(self) -> None:
        ctx = _ctx("أبي أطلب", intent=_classify("أبي أطلب"))
        ctx.intent.name = INTENT_START_ORDER
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action in {
            ACTION_PROPOSE_DRAFT_ORDER,
            "send_payment_link",
            "payment_continuation_reply",
        }
        reply_state = _build_reply_state(
            ctx=ctx,
            previous_state=ctx.state,
            current_state=ctx.state,
            suggestion=SuggestionSnapshot(),
            decision=decision,
            db=None,
        )
        prep = dict((reply_state.known_facts or {}).get("checkout_preparation") or {})
        assert prep.get("line_items")


class TestJExplicitOrderSupport:
    def test_status_query_keeps_support_facts_without_checkout_identity(self) -> None:
        evidence = {
            "latest_order": {
                "display_reference": "ORD-9001",
                "status": "processing",
                "title": GENERIC_PRODUCT_TITLE,
            },
            "current_order": {
                "display_reference": "ORD-9001",
                "status": "processing",
            },
        }
        ctx = _ctx("وين طلبي")
        ctx.intent.name = INTENT_TRACK_ORDER
        with patch(
            "modules.ai.brain.commerce.customer_order_evidence.collect_customer_order_evidence",
            return_value=evidence,
        ):
            decision = DefaultDecisionEngine().decide(ctx)
            reply_state = _build_reply_state(
                ctx=ctx,
                previous_state=ctx.state,
                current_state=ctx.state,
                suggestion=SuggestionSnapshot(),
                decision=decision,
                db=None,
            )
        assert decision.action in {ACTION_TRACK_ORDER, ACTION_LLM_REPLY}
        facts = dict(reply_state.known_facts or {})
        assert facts.get("customer_order_evidence")
        assert not (facts.get("checkout_preparation") or {})
        assert not (facts.get("checkout_identity_shipping") or {})
        blob = json.dumps(facts, ensure_ascii=False)
        assert SYNTH_PHONE not in blob
        assert SYNTH_ADDRESS not in blob
        assert SYNTH_PAYMENT not in blob


class TestKProductQuestionDuringDraft:
    def test_product_question_keeps_catalog_not_checkout_confirm(self) -> None:
        ctx = _ctx("تعرض لي المنتجات بالصور؟")
        ctx.intent.name = INTENT_ASK_PRODUCT
        decision = DefaultDecisionEngine().decide(ctx)
        reply_state = _build_reply_state(
            ctx=ctx,
            previous_state=ctx.state,
            current_state=ctx.state,
            suggestion=SuggestionSnapshot(),
            decision=decision,
            db=None,
        )
        assert has_positive_checkout_ownership(decision=decision, ctx=ctx) is False
        _assert_no_checkout_execution_facts(reply_state, build_brain_reply_prompt(reply_state))
        selected = reply_state.selected_product or {}
        assert selected.get("title") == GENERIC_PRODUCT_TITLE or (
            reply_state.known_facts or {}
        ).get("catalog_reasoning_candidates") is not None or selected.get("id") == GENERIC_PRODUCT_ID


class TestLGreetingUnrelated:
    def test_greeting_does_not_resume_checkout(self) -> None:
        ctx = _ctx("hello")
        ctx.intent.name = INTENT_GREETING
        decision = DefaultDecisionEngine().decide(ctx)
        reply_state = _build_reply_state(
            ctx=ctx,
            previous_state=ctx.state,
            current_state=ctx.state,
            suggestion=SuggestionSnapshot(),
            decision=decision,
            db=None,
        )
        assert decision.action != ACTION_PROPOSE_DRAFT_ORDER
        _assert_no_checkout_execution_facts(reply_state)


class TestMDraftPersistence:
    def test_non_checkout_turn_does_not_mutate_stored_checkout(self) -> None:
        state = _active_checkout_state()
        before = _snapshot(state)
        out = _run_full_path(GENERIC_URL, state=state)
        after = out["after"]
        assert after["cart_items"] == before["cart_items"]
        assert after["line_items"] == before["line_items"]
        assert after["address"] == before["address"]
        assert after["payment_method"] == before["payment_method"]
        assert after["phone"] == before["phone"]
        assert after["checkout_url"] == before["checkout_url"]
        assert after["draft_order_id"] == before["draft_order_id"]
        assert out["stamp"] is False
        assert out["pending"] == before["pending_action"]
        assert out["question"] == before["last_question_asked"]
        assert after["line_item_updated_at"] == before["line_item_updated_at"]


class TestNTenantIsolation:
    def test_tenant_b_does_not_see_tenant_a_checkout_facts(self) -> None:
        state_a = _active_checkout_state()
        state_b = _active_checkout_state()
        state_b.order_prep.customer_phone = "966522000088"
        state_b.order_prep.address_line = "حي الياسمين شارع آخر 9"
        ctx_a = _ctx(GENERIC_URL, state=state_a, tenant_id=9001, phone="966500000001")
        ctx_b = _ctx(GENERIC_URL, state=state_b, tenant_id=9002, phone="966500000002")
        decision_b = DefaultDecisionEngine().decide(ctx_b)
        reply_b = _build_reply_state(
            ctx=ctx_b,
            previous_state=ctx_b.state,
            current_state=ctx_b.state,
            suggestion=SuggestionSnapshot(),
            decision=decision_b,
            db=None,
        )
        blob_b = json.dumps(asdict(reply_b), ensure_ascii=False)
        assert SYNTH_PHONE not in blob_b
        assert SYNTH_ADDRESS not in blob_b
        assert "966522000088" not in blob_b
        assert "حي الياسمين" not in blob_b
        assert ctx_a.tenant_id != ctx_b.tenant_id


class TestOComposeOnceAndNoFetch:
    def test_url_only_compose_once_no_fetch_no_cta(self) -> None:
        out = _run_full_path(GENERIC_URL)
        assert out["compose_count"] == 1
        assert out["fetch_count"] == 0
        args = dict(out["decision"].args or {})
        assert not args.get("cta_url")
        assert args.get("cta_url") != GENERIC_URL
        assert GENERIC_URL not in (out["final"] or "")


class TestPNonInterference:
    def test_helpers_are_structural_not_customer_language(self) -> None:
        from modules.ai.brain import pipeline as pipeline_mod  # noqa: PLC0415

        source = open(pipeline_mod.__file__, encoding="utf-8").read()
        start = source.find("_CHECKOUT_EXECUTION_COMPOSE_KEYS")
        end = source.find("def _build_reply_state")
        helpers = source[start:end]
        assert "أبغى" not in helpers
        assert "تمام" not in helpers
        assert "re.compile" not in helpers
        assert "phrase_map" not in helpers.lower()
