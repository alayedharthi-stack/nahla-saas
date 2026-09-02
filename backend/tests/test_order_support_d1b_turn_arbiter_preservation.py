"""ORDER-SUPPORT-D1B — preserve authoritative Order Support through Turn Arbiter.

End-to-end: classifier-shaped Intent → DecisionEngine → understanding →
arbitration → maybe_enforce_turn_decision → compose known_facts / attestation.

Asserts structural ownership, not customer-facing prose.
"""
from __future__ import annotations

import os
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.commerce.customer_order_evidence import (  # noqa: E402
    collect_customer_order_evidence,
    customer_order_evidence_available,
)
from modules.ai.brain.commerce.order_support_ownership import (  # noqa: E402
    has_authoritative_order_support_ownership,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_HANDOFF,
    ACTION_LLM_REPLY,
    ACTION_ORDER_CONTEXT_UPDATE,
    ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_TRACK_ORDER,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.intent.classifier import (  # noqa: E402
    PROVENANCE_LAYER2_SEMANTIC_OVERRIDE,
)
from modules.ai.brain.state.state_relevance import StateRelevanceVerdict  # noqa: E402
from modules.ai.brain.truth_surface.model_payload_attestation import (  # noqa: E402
    assert_attestation_redacted,
    build_model_payload_attestation,
)
from modules.ai.brain.turn.contract import (  # noqa: E402
    OWNER_CHECKOUT,
    OWNER_ORDERING,
    OWNER_PAYMENT,
    OWNER_SUPPORT,
    OWNER_TRACKING,
)
from modules.ai.brain.turn.enforce import maybe_enforce_turn_decision  # noqa: E402
from modules.ai.brain.turn.legacy_owner import legacy_owner_from_decision  # noqa: E402
from modules.ai.brain.turn.owner_brief import build_owner_brief  # noqa: E402
from modules.ai.brain.turn.shadow import prepare_turn_arbitration  # noqa: E402
from modules.ai.brain.turn.understanding import synthesize_turn_understanding  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    INTENT_ASK_PAYMENT_INFO,
    INTENT_COMPLAINT_REFUND,
    INTENT_GENERAL,
    INTENT_LATEST_ORDER_SUMMARY,
    INTENT_START_ORDER,
    INTENT_TRACK_ORDER,
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)
from tests.commerce_scenario_fixtures import (  # noqa: E402
    DEFAULT_PHONE_E164,
    make_scenario_db,
    seed_conversation,
    seed_customer,
    seed_order,
    seed_tenant,
)

GENERIC_MERCHANT = "متجر تجريبي عام"
GENERIC_CUSTOMER = "أحمد سالم"
GENERIC_SHOE = "حذاء رياضي أبيض"
GENERIC_SHIRT = "قميص قطني أزرق"
GENERIC_PERFUME = "عطر ورد 100ml"
LATEST_REF = "284719628"
LATEST_REF_MASKED = "***628"

TURN2_FAMILY = (
    "ابي ارقامها",
    "عطني أرقامهم",
    "ابي ارقام الطلبات",
)
STAFF_CONTACT_FAMILY = (
    "أرقام خدمة العملاء",
    "وش رقم المسؤول؟",
)
CHECKOUT_TOPICS = (
    "checkout",
    "draft_order",
    "order_creation",
)

_DEFOCUS_TOPICS = frozenset({
    "persona_social",
    "persona_identity",
    "identity_collaboration",
    "support",
})


def _layer2_track_order(message: str) -> Intent:
    return Intent(
        name=INTENT_TRACK_ORDER,
        confidence=0.72,
        slots={
            "semantic_owner": "brain_classifier",
            "classification_provenance": PROVENANCE_LAYER2_SEMANTIC_OVERRIDE,
            "precedence_winner": "layer2",
            "layer2_result": INTENT_TRACK_ORDER,
            "semantic_relation": "authoritative_override",
        },
        raw_message=message,
        extraction_method="llm",
    )


def _noisy_track_order(message: str) -> Intent:
    return Intent(
        name=INTENT_TRACK_ORDER,
        confidence=0.72,
        slots={},
        raw_message=message,
        extraction_method="llm",
    )


def _facts() -> CommerceFacts:
    return CommerceFacts(
        has_products=True,
        product_count=5,
        in_stock_count=5,
        orderable=True,
        store_name=GENERIC_MERCHANT,
    )


def _stale_ordering_state() -> MerchantConversationState:
    st = MerchantConversationState(turn=5, stage="ordering")
    st.order_prep = OrderPreparationState(
        product_id="jacket-1",
        missing_fields=["city", "address", "payment"],
    )
    st.current_product_focus = {"id": "jacket-1", "title": "جاكيت"}
    st.last_question_asked = "ما المدينة؟"
    st.last_question_answered = False
    return st


def _stale_relevance() -> StateRelevanceVerdict:
    return StateRelevanceVerdict(
        safe_to_resume_state=False,
        detected_topic_shift=True,
        active_workflows=("active_fulfillment",),
    )


def _ctx(
    message: str,
    *,
    intent: Intent | None = None,
    state: MerchantConversationState | None = None,
    history: list | None = None,
    tenant_id: int = 1,
    phone: str = DEFAULT_PHONE_E164,
    customer_id: int | None = None,
    state_relevance: StateRelevanceVerdict | None = None,
) -> BrainContext:
    return BrainContext(
        tenant_id=tenant_id,
        customer_phone=phone,
        customer_id=customer_id,
        message=message,
        raw_message=message,
        intent=intent or Intent(name=INTENT_GENERAL, confidence=0.5, raw_message=message),
        state=state or MerchantConversationState(),
        facts=_facts(),
        history=history or [],
        commerce_bundle={},
        profile={"inbound_metadata": {}},
        state_relevance=state_relevance,
    )


def _enable_enforce(monkeypatch) -> None:
    monkeypatch.setenv("TURN_ARBITER_ENFORCE_ENABLED", "true")
    monkeypatch.delenv("TURN_ARBITER_ENFORCE_TENANTS", raising=False)
    monkeypatch.setenv(
        "TURN_ARBITER_ENFORCE_MISMATCH_TYPES",
        "checkout_vs_support,checkout_vs_discovery,staff_vs_persona",
    )


def _run_engine_then_arbiter(ctx: BrainContext, monkeypatch):
    _enable_enforce(monkeypatch)
    prepare_turn_arbitration(ctx)
    engine_decision = DefaultDecisionEngine().decide(ctx)
    final, result = maybe_enforce_turn_decision(ctx, engine_decision)
    return engine_decision, final, result


def _known_facts_after_defocus(decision: Decision, evidence: dict | None) -> dict:
    """Mirror pipeline social/non-commerce defocus without changing pipeline policy."""
    known: dict = {}
    if evidence:
        known["customer_order_evidence"] = evidence
    args = decision.args or {}
    block = bool(args.get("block_commerce_escalation"))
    topic = str(args.get("topic") or "")
    if block or topic in _DEFOCUS_TOPICS:
        known.pop("customer_order_evidence", None)
    return known


def _mask_ref(ref: str) -> str:
    digits = str(ref or "").strip()
    if len(digits) < 3:
        return digits
    return f"***{digits[-3:]}"


@pytest.fixture()
def db():
    session, _engine = make_scenario_db()
    yield session
    session.close()


@pytest.fixture()
def world(db):
    tenant = seed_tenant(db, name=GENERIC_MERCHANT)
    customer = seed_customer(db, tenant.id, name=GENERIC_CUSTOMER)
    conv = seed_conversation(db, tenant.id, customer_id=customer.id)
    base = datetime(2026, 8, 1, tzinfo=timezone.utc)
    visible_statuses = ("in_progress", "in_progress", "in_progress", "delivered")
    visible_refs = (LATEST_REF, "284719293", "284719477", "284719659")
    for idx, (status, ref) in enumerate(zip(visible_statuses, visible_refs)):
        row = seed_order(
            db,
            tenant.id,
            status=status,
            source="salla",
            external_id=ref,
            external_order_number=ref,
            customer_info={"phone": DEFAULT_PHONE_E164, "mobile": DEFAULT_PHONE_E164},
            line_items=[{"title": GENERIC_SHOE if idx == 0 else GENERIC_SHIRT, "quantity": 1}],
            extra_metadata={"created_at": (base + timedelta(days=30 - idx)).isoformat()},
        )
        row.customer_id = None
    for idx, ref in enumerate(("284719315", "284719976", "284719245")):
        row = seed_order(
            db,
            tenant.id,
            status="cancelled",
            source="salla",
            external_id=ref,
            external_order_number=ref,
            customer_info={"phone": DEFAULT_PHONE_E164},
            line_items=[{"title": GENERIC_PERFUME, "quantity": 1}],
            extra_metadata={"created_at": (base + timedelta(days=idx)).isoformat()},
        )
        row.customer_id = None
    abandoned = seed_order(
        db,
        tenant.id,
        status="abandoned",
        source="salla",
        external_id="284719136",
        external_order_number="284719136",
        customer_info={"phone": DEFAULT_PHONE_E164},
        extra_metadata={"created_at": base.isoformat()},
    )
    abandoned.customer_id = None
    db.commit()
    other = seed_tenant(db, name="متجر تجريبي آخر")
    seed_order(
        db,
        other.id,
        status="delivered",
        source="salla",
        external_id="999888777",
        external_order_number="999888777",
        customer_info={"phone": DEFAULT_PHONE_E164},
        extra_metadata={"created_at": base.isoformat()},
    )
    return SimpleNamespace(
        db=db,
        tenant_id=tenant.id,
        other_tenant_id=other.id,
        customer_id=customer.id,
        conversation_id=conv.id,
        phone=DEFAULT_PHONE_E164,
    )


class TestLiveTurn2ArbiterPreservation:
    @pytest.mark.parametrize("message", TURN2_FAMILY)
    def test_authoritative_os_survives_arbiter_as_order_history(
        self,
        world,
        monkeypatch,
        message: str,
    ) -> None:
        intent = _layer2_track_order(message)
        assert has_authoritative_order_support_ownership(intent) is True
        ctx = _ctx(
            message,
            intent=intent,
            state=_stale_ordering_state(),
            tenant_id=world.tenant_id,
            phone=world.phone,
            customer_id=world.customer_id,
            state_relevance=_stale_relevance(),
        )
        understanding = synthesize_turn_understanding(ctx)
        assert understanding.current_intent == "track_order"
        assert understanding.current_intent != "reach_staff"

        engine, final, result = _run_engine_then_arbiter(ctx, monkeypatch)
        assert (engine.args or {}).get("topic") == "order_history"
        assert result.enforced is False
        assert final.action == ACTION_LLM_REPLY
        assert (final.args or {}).get("topic") == "order_history"
        assert bool((final.args or {}).get("block_commerce_escalation")) is False
        assert legacy_owner_from_decision(engine) == OWNER_TRACKING
        assert ctx.turn_arbitration_shadow.turn_owner == OWNER_TRACKING

        evidence = collect_customer_order_evidence(
            db=world.db,
            tenant_id=world.tenant_id,
            phone=world.phone,
            customer_id=world.customer_id,
            conversation_id=world.conversation_id,
        )
        known = _known_facts_after_defocus(final, evidence)
        assert customer_order_evidence_available(known.get("customer_order_evidence")) is True
        payload = known["customer_order_evidence"]
        assert int(payload.get("order_count") or 0) == 7
        orders = list(payload.get("orders") or [])
        assert len(orders) == 7
        latest_ref = str((payload.get("latest_order") or {}).get("display_reference") or "")
        assert latest_ref.endswith("628")
        assert _mask_ref(latest_ref) == LATEST_REF_MASKED

        attestation = build_model_payload_attestation(
            stage="compose",
            known_facts=known,
            decision_action=str(final.action or ""),
        )
        assert_attestation_redacted(attestation)
        compose_keys = attestation["facts_reaching_compose"]["known_facts_keys"]
        assert "customer_order_evidence" in compose_keys
        refs = [str(row.get("display_reference") or "") for row in orders]
        assert len(refs) == 7
        assert LATEST_REF in refs or any(item.endswith("628") for item in refs)


class TestOrderSupportTopicOwners:
    def test_order_history_is_tracking_not_checkout(self) -> None:
        decision = Decision(
            action=ACTION_LLM_REPLY,
            args={"topic": "order_history"},
            reason="order_history",
        )
        assert legacy_owner_from_decision(decision) == OWNER_TRACKING

    def test_latest_order_summary_is_tracking(self) -> None:
        decision = Decision(
            action=ACTION_LLM_REPLY,
            args={"topic": "latest_order_summary"},
            reason="latest",
        )
        assert legacy_owner_from_decision(decision) == OWNER_TRACKING

    def test_existing_order_support_is_tracking_not_support(self) -> None:
        decision = Decision(
            action=ACTION_LLM_REPLY,
            args={"topic": "existing_order_support"},
            reason="existing",
        )
        assert legacy_owner_from_decision(decision) == OWNER_TRACKING

    @pytest.mark.parametrize("topic", CHECKOUT_TOPICS)
    def test_purchase_topics_remain_checkout(self, topic: str) -> None:
        decision = Decision(
            action=ACTION_LLM_REPLY,
            args={"topic": topic},
            reason="checkout",
        )
        assert legacy_owner_from_decision(decision) == OWNER_CHECKOUT

    def test_propose_draft_order_remains_ordering(self) -> None:
        decision = Decision(action=ACTION_PROPOSE_DRAFT_ORDER, reason="start_order")
        assert legacy_owner_from_decision(decision) == OWNER_ORDERING


class TestLatestAndExistingSurviveArbiter:
    def test_latest_order_summary_survives_arbiter(self, monkeypatch) -> None:
        message = "رقم آخر طلب"
        intent = Intent(
            name=INTENT_LATEST_ORDER_SUMMARY,
            confidence=0.94,
            raw_message=message,
        )
        ctx = _ctx(message, intent=intent)
        engine, final, result = _run_engine_then_arbiter(ctx, monkeypatch)
        assert (engine.args or {}).get("topic") == "latest_order_summary"
        assert result.enforced is False
        assert (final.args or {}).get("topic") == "latest_order_summary"
        assert bool((final.args or {}).get("block_commerce_escalation")) is False
        assert legacy_owner_from_decision(engine) == OWNER_TRACKING

    def test_latest_order_summary_survives_arbiter_against_stale_checkout(
        self,
        monkeypatch,
    ) -> None:
        message = "رقم آخر طلب"
        intent = Intent(
            name=INTENT_LATEST_ORDER_SUMMARY,
            confidence=0.94,
            raw_message=message,
        )
        ctx = _ctx(
            message,
            intent=intent,
            state=_stale_ordering_state(),
            state_relevance=_stale_relevance(),
        )
        _enable_enforce(monkeypatch)
        prepare_turn_arbitration(ctx)
        engine = Decision(
            action=ACTION_LLM_REPLY,
            args={
                "topic": "latest_order_summary",
                "ledger_topic": INTENT_LATEST_ORDER_SUMMARY,
            },
            reason="latest order summary — evidence compose",
        )
        final, result = maybe_enforce_turn_decision(ctx, engine)
        assert result.enforced is False
        assert (final.args or {}).get("topic") == "latest_order_summary"
        assert bool((final.args or {}).get("block_commerce_escalation")) is False
        assert legacy_owner_from_decision(engine) == OWNER_TRACKING

    def test_existing_order_support_survives_arbiter(self, monkeypatch) -> None:
        message = "الطلب متأخر والشحن ما وصل"
        intent = _layer2_track_order(message)
        ctx = _ctx(
            message,
            intent=intent,
            history=[{"direction": "in", "body": LATEST_REF}],
            state=_stale_ordering_state(),
            state_relevance=_stale_relevance(),
        )
        _enable_enforce(monkeypatch)
        prepare_turn_arbitration(ctx)
        engine = Decision(
            action=ACTION_LLM_REPLY,
            args={"topic": "existing_order_support"},
            reason="explicit_intent_suppressed:existing_order_support",
        )
        final, result = maybe_enforce_turn_decision(ctx, engine)
        assert result.enforced is False
        assert (final.args or {}).get("topic") == "existing_order_support"
        assert bool((final.args or {}).get("block_commerce_escalation")) is False
        assert legacy_owner_from_decision(engine) == OWNER_TRACKING


class TestEnforcePreservationGate:
    def test_weaker_contact_mismatch_does_not_replace_os(self, monkeypatch) -> None:
        message = "ابي ارقامها"
        intent = _layer2_track_order(message)
        ctx = _ctx(
            message,
            intent=intent,
            state=_stale_ordering_state(),
            state_relevance=_stale_relevance(),
        )
        _enable_enforce(monkeypatch)
        monkeypatch.setenv(
            "TURN_ARBITER_ENFORCE_MISMATCH_TYPES",
            "checkout_vs_support,checkout_vs_discovery,staff_vs_persona,unknown_mismatch",
        )
        prepare_turn_arbitration(ctx)
        understanding = ctx.turn_understanding_shadow
        brief = build_owner_brief(OWNER_SUPPORT, understanding, ctx)
        ctx.turn_arbitration_shadow = replace(
            ctx.turn_arbitration_shadow,
            turn_owner=OWNER_SUPPORT,
            reason="forced_false_contact",
            owner_brief=brief,
        )
        engine = Decision(
            action=ACTION_LLM_REPLY,
            args={"topic": "order_history"},
            reason="order question without status-rule match",
        )
        final, result = maybe_enforce_turn_decision(ctx, engine)
        assert result.enforced is False
        assert final is engine
        assert (final.args or {}).get("topic") == "order_history"
        assert bool((final.args or {}).get("block_commerce_escalation")) is False

    def test_true_complaint_still_enforced_over_stale_checkout(self, monkeypatch) -> None:
        ctx = _ctx(
            "العسل خفيف ومو مثل أول",
            intent=Intent(name=INTENT_COMPLAINT_REFUND, confidence=0.92),
            state=_stale_ordering_state(),
            state_relevance=_stale_relevance(),
        )
        _enable_enforce(monkeypatch)
        prepare_turn_arbitration(ctx)
        legacy = Decision(action=ACTION_ORDER_CONTEXT_UPDATE, reason="ask_city")
        final, result = maybe_enforce_turn_decision(ctx, legacy)
        assert result.enforced is True
        assert (final.args or {}).get("topic") == "support"
        assert bool((final.args or {}).get("block_commerce_escalation")) is True


class TestStaffContactBoundaries:
    @pytest.mark.parametrize("message", STAFF_CONTACT_FAMILY)
    def test_real_staff_contact_without_os_stays_staff(self, monkeypatch, message: str) -> None:
        intent = Intent(name=INTENT_GENERAL, confidence=0.6, raw_message=message)
        assert has_authoritative_order_support_ownership(intent) is False
        ctx = _ctx(message, intent=intent, state=_stale_ordering_state())
        understanding = synthesize_turn_understanding(ctx)
        assert understanding.current_intent == "reach_staff"
        engine, final, result = _run_engine_then_arbiter(ctx, monkeypatch)
        assert engine.action != ACTION_TRACK_ORDER
        assert (final.args or {}).get("topic") != "order_history"
        assert legacy_owner_from_decision(engine) != OWNER_TRACKING
        if engine.action == ACTION_HANDOFF:
            assert result.enforced is False or (final.args or {}).get("topic") != "order_history"

    def test_noisy_track_order_without_layer2_does_not_gain_os(self, monkeypatch) -> None:
        message = "وش رقم المسؤول؟"
        intent = _noisy_track_order(message)
        assert has_authoritative_order_support_ownership(intent) is False
        ctx = _ctx(
            message,
            intent=intent,
            state=_stale_ordering_state(),
            state_relevance=_stale_relevance(),
        )
        understanding = synthesize_turn_understanding(ctx)
        assert understanding.current_intent == "reach_staff"
        engine, final, _result = _run_engine_then_arbiter(ctx, monkeypatch)
        assert (engine.args or {}).get("topic") != "order_history"
        assert (final.args or {}).get("topic") != "order_history"
        assert legacy_owner_from_decision(engine) != OWNER_TRACKING


class TestCheckoutPaymentTrackingBoundaries:
    def test_real_checkout_continuation_stays_checkout(self, monkeypatch) -> None:
        from modules.ai.brain.interpret.semantic_turn_interpreter import (  # noqa: PLC0415
            SemanticTurnInterpretation,
        )
        from modules.ai.brain.turn.arbiter import arbitrate_turn  # noqa: PLC0415

        st = MerchantConversationState(turn=5, stage="checkout")
        st.order_prep = OrderPreparationState(product_id="p1", missing_fields=["city"])
        st.last_question_asked = "ما المدينة؟"
        st.last_question_answered = False
        ctx = _ctx(
            "أنا في الرياض",
            intent=Intent(name="social", confidence=0.85, slots={}),
            state=st,
            state_relevance=StateRelevanceVerdict(
                safe_to_resume_state=True,
                detected_topic_shift=False,
                fulfillment_state_relevant=True,
                active_workflows=("active_fulfillment",),
            ),
        )
        ctx.semantic_interpretation = SemanticTurnInterpretation(
            canonical_text="أنا في الرياض",
            interpreted_intent="fulfillment_location_update",
            context_anchor="active_order_context",
            confidence=0.88,
            commerce_frame="fulfillment",
        )
        _enable_enforce(monkeypatch)
        prepare_turn_arbitration(ctx)
        understanding = synthesize_turn_understanding(ctx)
        arbitration = arbitrate_turn(understanding, ctx)
        assert understanding.current_intent == "checkout_continuation"
        assert arbitration.turn_owner in {OWNER_CHECKOUT, OWNER_ORDERING}
        legacy = Decision(action=ACTION_ORDER_CONTEXT_UPDATE, reason="ask_city")
        final, result = maybe_enforce_turn_decision(ctx, legacy)
        assert result.enforced is False
        assert final.action == ACTION_ORDER_CONTEXT_UPDATE
        assert legacy_owner_from_decision(legacy) == OWNER_CHECKOUT

    def test_start_order_does_not_become_tracking(self, monkeypatch) -> None:
        ctx = _ctx(
            "أبي أطلب الحذاء الرياضي الأبيض",
            intent=Intent(name=INTENT_START_ORDER, confidence=0.93),
            state=MerchantConversationState(stage="deciding"),
        )
        engine, final, result = _run_engine_then_arbiter(ctx, monkeypatch)
        assert legacy_owner_from_decision(engine) != OWNER_TRACKING
        assert (final.args or {}).get("topic") != "order_history"
        assert result.enforced is False or (final.args or {}).get("topic") != "order_history"

    def test_where_is_my_order_stays_tracking(self, monkeypatch) -> None:
        message = "وين طلبي؟"
        ctx = _ctx(message, intent=Intent(name=INTENT_TRACK_ORDER, confidence=0.92))
        engine, final, result = _run_engine_then_arbiter(ctx, monkeypatch)
        assert engine.action == ACTION_TRACK_ORDER
        assert result.enforced is False
        assert final.action == ACTION_TRACK_ORDER
        assert legacy_owner_from_decision(engine) == OWNER_TRACKING

    def test_payment_account_numbers_do_not_become_tracking(self, monkeypatch) -> None:
        message = "أرقام الحساب البنكي"
        intent = Intent(name=INTENT_ASK_PAYMENT_INFO, confidence=0.9, raw_message=message)
        ctx = _ctx(message, intent=intent)
        engine, final, _result = _run_engine_then_arbiter(ctx, monkeypatch)
        assert (engine.args or {}).get("topic") != "order_history"
        assert (final.args or {}).get("topic") != "order_history"
        owner = legacy_owner_from_decision(engine)
        assert owner != OWNER_TRACKING
        if engine.action == ACTION_LLM_REPLY:
            topic = str((engine.args or {}).get("topic") or "")
            assert "payment" in topic or owner == OWNER_PAYMENT or owner != OWNER_TRACKING

    def test_payment_topic_stays_payment(self) -> None:
        decision = Decision(
            action=ACTION_LLM_REPLY,
            args={"topic": "payment_methods"},
            reason="payment",
        )
        assert legacy_owner_from_decision(decision) == OWNER_PAYMENT


class TestTenantIsolation:
    def test_same_phone_other_tenant_does_not_leak(self, world) -> None:
        payload = collect_customer_order_evidence(
            db=world.db,
            tenant_id=world.other_tenant_id,
            phone=world.phone,
            customer_id=None,
        )
        refs = {
            str(row.get("display_reference") or "")
            for row in ((payload or {}).get("orders") or [])
        }
        assert LATEST_REF not in refs
        assert "284719293" not in refs
        other_count = int((payload or {}).get("order_count") or 0)
        assert other_count == 1
        assert "999888777" in refs
