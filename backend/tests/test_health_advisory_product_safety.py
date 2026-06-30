"""PR-Health-Advisory — sensitive health/product-safety ownership."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")):
    if p not in sys.path:
        sys.path.insert(0, p)

from modules.ai.brain.commerce.commerce_entry_catalog_delivery import (  # noqa: E402
    CatalogDeliveryKind,
    try_commerce_entry_catalog_decision,
)
from modules.ai.brain.commerce.health_advisory_product_safety import (  # noqa: E402
    TOPIC_HEALTH_ADVISORY,
    classify_health_advisory,
    has_active_health_advisory_context,
    pin_health_advisory_context,
    try_health_advisory_product_safety_decision,
)
from modules.ai.brain.commerce.identity_collaboration_guard import (  # noqa: E402
    try_identity_collaboration_decision,
)
from modules.ai.brain.commerce.payment_evidence_turn_route import (  # noqa: E402
    try_payment_evidence_turn_decision,
)
from modules.ai.brain.commerce.product_knowledge_or_comparison import (  # noqa: E402
    try_product_knowledge_decision,
)
from modules.ai.brain.commerce.product_ordering_prompt import build_product_ordering_prompt  # noqa: E402
from modules.ai.brain.commerce.status_reply_product_context import (  # noqa: E402
    try_status_reply_product_decision,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_CATALOG_NAVIGATE,
    ACTION_LLM_REPLY,
    ACTION_PROPOSE_DRAFT_ORDER,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.intent import rules as intent_rules  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)

LONG_HEALTH = (
    "عندي توأم أطفال أعمارهم 5 سنوات ونصف وعندهم تأخر في النطق "
    "شخصوهم احتمال يكون طيف توحد "
    "واحتمال آخر أنه من الأمعاء يكون فيها معادن ثقيلة "
    "وقالوا ابحث عن عسل مناسب للأمعاء "
    "أنا أبغاك تدلني على النوع الأنسب للأمعاء وانا بطلب الكمية المناسبة لهم "
    "فاش تنصحني فيه حسب خبرتك"
)


class _Col:
    def __init__(self, name: str) -> None:
        self.name = name

    def in_(self, values: Any) -> "_Col":
        return self

    def asc(self) -> "_Col":
        return self

    def desc(self) -> "_Col":
        return self


class _StubKBSection:
    def __init__(self, *, section_id: int, title: str, body: str, kind: str) -> None:
        self.id = section_id
        self.title = title
        self.body = body
        self.kind = kind
        self.priority = 5
        self.updated_at = None
        self.is_active = True
        self.deleted_at = None
        self.tenant_id = 33


class _QueryStub:
    def __init__(self, rows: List[Any]) -> None:
        self._rows = rows

    def filter(self, *args: Any, **kwargs: Any) -> "_QueryStub":
        return self

    def order_by(self, *args: Any, **kwargs: Any) -> "_QueryStub":
        return self

    def limit(self, n: int) -> "_QueryStub":
        return self

    def all(self) -> List[Any]:
        return list(self._rows)


class _StubDB:
    def __init__(self, kb_sections: Optional[List[_StubKBSection]] = None) -> None:
        self._kb_sections = kb_sections or []

    def query(self, model: Any) -> _QueryStub:
        if getattr(model, "__name__", str(model)) == "MerchantKnowledgeSection":
            return _QueryStub(self._kb_sections)
        return _QueryStub([])


def _install_kb_stubs(monkeypatch: pytest.MonkeyPatch, sections: List[_StubKBSection]) -> _StubDB:
    import types as _types

    models_stub = _types.ModuleType("models")
    models_stub.MerchantKnowledgeSection = type(  # type: ignore[attr-defined]
        "MerchantKnowledgeSection",
        (),
        {
            "tenant_id": _Col("tenant_id"),
            "kind": _Col("kind"),
            "priority": _Col("priority"),
            "updated_at": _Col("updated_at"),
        },
    )
    monkeypatch.setitem(sys.modules, "models", models_stub)
    knowledge_stub = _types.ModuleType("core.knowledge")
    knowledge_stub.apply_ai_visible_kb_query_filters = lambda q: q  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "core.knowledge", knowledge_stub)
    return _StubDB(kb_sections=sections)


def _checkout_state() -> MerchantConversationState:
    st = MerchantConversationState(greeted=True, stage="checkout")
    st.order_prep = OrderPreparationState(
        product_id="cat-1",
        missing_fields=["city"],
        catalog_line_items_authoritative=True,
        catalog_checkout_total=2025.0,
        line_items=[
            {"product_name": "غذاء ملكات", "quantity": 4},
            {"product_name": "عسل سدر", "quantity": 1},
        ],
    )
    st.last_question_asked = "address_location"
    return st


def _ctx(
    message: str,
    *,
    state: Optional[MerchantConversationState] = None,
    db: Any = None,
    inbound_metadata: Optional[dict] = None,
) -> BrainContext:
    intent = intent_rules.match(message) or Intent(
        name="general", confidence=0.5, raw_message=message,
    )
    ctx = BrainContext(
        tenant_id=33,
        customer_phone="966500000001",
        message=message,
        intent=intent,
        state=state or _checkout_state(),
        facts=CommerceFacts(has_products=True, product_count=5, orderable=True),
        profile={"inbound_metadata": dict(inbound_metadata or {})},
    )
    if db is not None:
        ctx._db = db  # type: ignore[attr-defined]
    return ctx


def _status_state() -> MerchantConversationState:
    state = MerchantConversationState(greeted=True)
    state.commerce_session = {
        "status_reply_product_context": {
            "active": True,
            "product_title": "عسل سدر",
            "product_id": 9,
            "has_trusted_title": True,
        },
    }
    state.current_product_focus = {
        "id": 9,
        "title": "عسل سدر",
        "from_status_reply": True,
    }
    return state


class TestHealthAdvisoryProductSafety:
    def test_long_child_health_message_routes_health_owner(self) -> None:
        ctx = _ctx(LONG_HEALTH)
        decision = try_health_advisory_product_safety_decision(ctx)
        assert decision is not None
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == TOPIC_HEALTH_ADVISORY
        assert decision.args.get("pause_order_slot_collection") is True
        assert decision.args.get("block_staff_contact") is True
        assert decision.args.get("block_showroom_location") is True
        assert decision.args.get("block_catalog_push") is True
        forbidden = decision.args.get("forbidden_claims") or []
        assert "treats_autism" in forbidden
        assert "order_quantity_before_health_ack" in forbidden
        assert try_identity_collaboration_decision(ctx) is None
        prompt = build_product_ordering_prompt(ctx)
        assert "وش المنتج" not in prompt
        assert "الكمية" not in prompt

    def test_health_during_active_order_prep_pauses_slots(self) -> None:
        state = _checkout_state()
        ctx = _ctx(LONG_HEALTH, state=state)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.args.get("topic") == TOPIC_HEALTH_ADVISORY
        assert decision.args.get("pause_order_slot_collection") is True
        assert state.last_question_asked in {"", None} or not str(state.last_question_asked).strip()

    def test_gut_advice_for_children(self) -> None:
        decision = try_health_advisory_product_safety_decision(
            _ctx("وش تنصحني للأمعاء للأطفال؟"),
        )
        assert decision is not None
        assert decision.args.get("topic") == TOPIC_HEALTH_ADVISORY
        goal = str(decision.args.get("response_goal") or "")
        assert "Do not diagnose" in goal or "HEALTH_ADVISORY" in goal

    def test_autism_treatment_claim_rejected_by_forbidden_claims(self) -> None:
        decision = try_health_advisory_product_safety_decision(
            _ctx("هل العسل يعالج التوحد؟"),
        )
        assert decision is not None
        forbidden = decision.args.get("forbidden_claims") or []
        assert "treats_autism" in forbidden
        goal = str(decision.args.get("response_goal") or "")
        assert "doctor" in goal or "specialist" in goal

    def test_mix_royal_jelly_after_health_context(self) -> None:
        state = _checkout_state()
        ev = classify_health_advisory(LONG_HEALTH, state=state)
        pin_health_advisory_context(state, evidence=ev, source="test")
        decision = try_health_advisory_product_safety_decision(
            _ctx("اخلطه مع غذاء الملكات لانهم نصحوني فيها", state=state),
        )
        assert decision is not None
        assert decision.args.get("question_kind") == "therapy_mix_followup"
        goal = str(decision.args.get("response_goal") or "")
        assert "therapy_mix_followup=true" in goal
        assert decision.args.get("pause_order_slot_collection") is True

    def test_bot_challenge_after_poor_health_response(self) -> None:
        state = _checkout_state()
        ev = classify_health_advisory(LONG_HEALTH, state=state)
        pin_health_advisory_context(state, evidence=ev, source="test")
        decision = try_health_advisory_product_safety_decision(
            _ctx("والله مدري انت اللي يرد ولا رد آلي", state=state),
        )
        assert decision is not None
        assert decision.args.get("question_kind") == "bot_authenticity_challenge"
        goal = str(decision.args.get("response_goal") or "")
        assert "prior_reply_inadequate=true" in goal
        assert "no immediate quantity" in goal

    def test_ce4_product_knowledge_not_health(self) -> None:
        ctx = _ctx("وش الفرق عن السدر العادي؟")
        assert try_health_advisory_product_safety_decision(ctx) is None
        assert try_product_knowledge_decision(ctx) is None or (
            try_product_knowledge_decision(ctx).action == ACTION_LLM_REPLY
        )

    def test_ce1_buy_not_health(self) -> None:
        state = _status_state()
        ctx = _ctx("نبغى كيلوين", state=state)
        assert try_health_advisory_product_safety_decision(ctx) is None
        status_dec = try_status_reply_product_decision(ctx)
        assert status_dec is not None
        assert status_dec.action == ACTION_PROPOSE_DRAFT_ORDER

    def test_ce2_catalog_not_health(self) -> None:
        ctx = _ctx("أرسل الكتalog")
        assert try_health_advisory_product_safety_decision(ctx) is None
        ce2 = try_commerce_entry_catalog_decision(ctx)
        assert ce2 is not None
        assert ce2.action == ACTION_CATALOG_NAVIGATE
        assert ce2.args.get("catalog_delivery_kind") == CatalogDeliveryKind.SEND_CATALOG.value

    def test_payment_receipt_not_health(self) -> None:
        meta = {
            "normalized_type": "document",
            "has_attached_media": True,
            "pdf_kind": "payment_receipt",
            "payment_evidence_status": "confirmed",
            "receipt_data": {"amount": 350.0},
        }
        ctx = _ctx("", inbound_metadata=meta)
        assert try_health_advisory_product_safety_decision(ctx) is None
        assert try_payment_evidence_turn_decision(ctx) is not None

    def test_engine_beats_identity_on_long_health(self) -> None:
        decision = DefaultDecisionEngine().decide(_ctx(LONG_HEALTH))
        assert decision.args.get("topic") == TOPIC_HEALTH_ADVISORY
        assert decision.args.get("topic") != "identity_collaboration"

    def test_kb_sections_attached_when_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        db = _install_kb_stubs(monkeypatch, [
            _StubKBSection(
                section_id=3,
                kind="health_advisory",
                title="العسل والأمعاء",
                body="المنتجات الغذائية لا تُعد علاجًا طبيًا للأطفال.",
            ),
        ])
        decision = try_health_advisory_product_safety_decision(_ctx(LONG_HEALTH, db=db))
        assert decision is not None
        allowed = decision.args.get("allowed_facts") or {}
        assert allowed.get("kb_sections")
