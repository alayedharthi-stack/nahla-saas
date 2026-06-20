"""P0 — complaint misclassification, staff vCard gating, identity continuity."""
from __future__ import annotations

import os
import sys
import types as _types
from typing import Any, List, Optional

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.commerce.complaint_refund_topic_guard import (  # noqa: E402
    COMPLAINT_INTAKE_REPLY_AR,
    apply_complaint_refund_session_flags,
    classify_complaint_refund,
    try_complaint_refund_decision,
)
from modules.ai.brain.commerce.commerce_objective import (  # noqa: E402
    COMMERCE_OBJECTIVE_ORDERING,
    COMMERCE_OBJECTIVE_SUPPORT,
    get_commerce_objective,
)
from modules.ai.brain.commerce.contact_escalation import classify_store_arrival  # noqa: E402
from modules.ai.brain.state.stages import STAGE_ORDERING, STAGE_SUPPORT  # noqa: E402
from modules.ai.brain.commerce.staff_contact_suppression import (  # noqa: E402
    apply_staff_contact_session_flags,
    customer_allows_staff_vcard,
    is_staff_contact_suppressed,
    staff_vcard_delivery_blocked,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_LLM_REPLY,
    ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_SEARCH_PRODUCTS,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.intent import rules  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    INTENT_COMPLAINT_REFUND,
    INTENT_WHO_ARE_YOU,
    MerchantConversationState,
    OrderPreparationState,
)
from core.wa_draft_confirmation import compose_wa_order_flow_reply  # noqa: E402


def _ctx(
    msg: str,
    *,
    state: MerchantConversationState | None = None,
) -> BrainContext:
    intent = rules.match(msg)
    if intent is None:
        from modules.ai.brain.types import Intent

        intent = Intent(name="general", confidence=0.5, raw_message=msg)
    return BrainContext(
        tenant_id=1,
        customer_phone="+966500000000",
        message=msg,
        intent=intent,
        state=state or MerchantConversationState(greeted=True, stage="discovery"),
        facts=CommerceFacts(
            has_products=True,
            product_count=5,
            orderable=True,
            has_active_integration=True,
            store_name="test",
        ),
        history=[],
    )


class _StubKBSection:
    def __init__(
        self,
        *,
        section_id: int,
        kind: str,
        body: str,
        title: str = "",
        metadata: Optional[dict] = None,
    ) -> None:
        self.id = section_id
        self.kind = kind
        self.body = body
        self.title = title
        self.metadata = metadata or {}
        self.is_active = True
        self.priority = 100
        self.updated_at = section_id


class _StubDB:
    def __init__(self, sections: List[_StubKBSection]) -> None:
        self._sections = sections

    def query(self, _model: Any) -> "_Query":
        return _Query(self._sections)


class _Query:
    def __init__(self, sections: List[_StubKBSection]) -> None:
        self._sections = list(sections)

    def filter(self, *args: Any, **kwargs: Any) -> "_Query":
        return self

    def order_by(self, *args: Any, **kwargs: Any) -> "_Query":
        return self

    def limit(self, _n: int) -> "_Query":
        return self

    def all(self) -> List[_StubKBSection]:
        return self._sections

    def first(self) -> None:
        return None


def _install_staff_stubs(monkeypatch: pytest.MonkeyPatch) -> _StubDB:
    models_stub = _types.ModuleType("models")

    class _Col:
        def __init__(self, name: str) -> None:
            self.name = name

        def __eq__(self, other: Any) -> _types.SimpleNamespace:
            return _types.SimpleNamespace(col_name=self.name, value=other)

        def is_(self, other: Any) -> _types.SimpleNamespace:
            return _types.SimpleNamespace(col_name=self.name, value=other)

        def in_(self, values: Any) -> _types.SimpleNamespace:
            return _types.SimpleNamespace(col_name=self.name, _kinds=tuple(values))

        def asc(self) -> "_Col":
            return self

        def desc(self) -> "_Col":
            return self

    class _MksStub:
        tenant_id = _Col("tenant_id")
        kind = _Col("kind")
        is_active = _Col("is_active")
        priority = _Col("priority")
        updated_at = _Col("updated_at")
        deleted_at = _Col("deleted_at")

    class _TsStub:
        tenant_id = _Col("tenant_id")

    models_stub.MerchantKnowledgeSection = _MksStub  # type: ignore[attr-defined]
    models_stub.TenantSettings = _TsStub  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "models", models_stub)

    call_stub = _types.ModuleType("services.call_resolver")

    class _CallTarget:
        def __init__(self, **kwargs: Any) -> None:
            self.name = kwargs.get("name", "")
            self.wa_id = kwargs.get("wa_id", "")
            self.phone_display = kwargs.get("phone_display", "")
            self.raw_phone = kwargs.get("raw_phone", "")

    call_stub.CallTarget = _CallTarget  # type: ignore[attr-defined]
    call_stub._normalize_saudi_phone = lambda p: "966541690226"  # type: ignore[attr-defined]
    call_stub._pretty_phone = lambda w: w  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "services.call_resolver", call_stub)
    monkeypatch.setenv("STAFF_CONTACT_SAFETY_NET_ENABLED", "1")

    return _StubDB(
        [
            _StubKBSection(
                section_id=149,
                kind="escalation_rules",
                body="عند الوصول للمعرض تواصل مع بائع المعرض على الرقم المسجل.",
            ),
            _StubKBSection(
                section_id=5,
                kind="branches",
                body="أمين بائع المعرض: 0541690226",
            ),
        ]
    )


class TestComplaintRefundGuard:
    @pytest.mark.parametrize(
        "msg",
        (
            "ارجعوا لي فلوسي",
            "العسل ليس عسل",
            "لقد خدعت في جودة العسل",
        ),
    )
    def test_classifies_complaint(self, msg: str) -> None:
        assert classify_complaint_refund(msg)

    def test_refund_routes_support_not_order(self) -> None:
        ctx = _ctx("ارجعوا لي فلوسي")
        dec = DefaultDecisionEngine().decide(ctx)
        assert dec.action == ACTION_LLM_REPLY
        assert dec.args.get("topic") == "support_complaint_refund"
        assert dec.action != ACTION_PROPOSE_DRAFT_ORDER
        assert dec.action != ACTION_SEARCH_PRODUCTS

    def test_honey_fraud_not_checkout(self) -> None:
        intent = rules.match("العسل ليس عسل")
        assert intent is not None
        assert intent.name == INTENT_COMPLAINT_REFUND
        dec = DefaultDecisionEngine().decide(_ctx("العسل ليس عسل"))
        assert dec.args.get("topic") == "support_complaint_refund"

    def test_intake_reply_is_operational_not_refund_promise(self) -> None:
        dec = try_complaint_refund_decision(_ctx("ارجعوا لي فلوسي"))
        assert dec is not None
        assert "support_complaint_refund" in str(dec.args.get("topic"))
        assert "نعتذر" in COMPLAINT_INTAKE_REPLY_AR
        assert "رقم الطلب" in COMPLAINT_INTAKE_REPLY_AR

    def test_blocks_draft_order_injection(self) -> None:
        injected = compose_wa_order_flow_reply(
            order_prep={
                "line_items": [{"product_id": 1, "title": "عسل", "price": 100}],
            },
            brain_state={},
            cart_changed=True,
            customer_message="ارجعوا لي فلوسي",
        )
        assert injected is None


class TestStaffVcardEvidenceGate:
    def test_city_only_no_vcard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from modules.ai.postprocess.safety_nets import apply_staff_contact_safety_net

        db = _install_staff_stubs(monkeypatch)
        blocked, reason = staff_vcard_delivery_blocked(
            customer_msg="أنا في الطائف",
            customer_intent=False,
        )
        assert not blocked

        result = apply_staff_contact_safety_net(
            customer_msg="أنا في الطائف",
            reply_text="تقدر تتواصل مع أمين بائع المعرض",
            existing_call_targets=[],
            detected_call_markers=0,
            db=db,
            tenant_id=42,
        )
        assert result.fired is False
        assert result.skipped_reason == "no_staff_intent"

    def test_explicit_ameen_number_allowed(self) -> None:
        allowed, reason = customer_allows_staff_vcard(
            customer_msg="أرسل رقم أمين",
            customer_intent=True,
        )
        assert allowed
        assert reason == "customer_intent_evidence"

    def test_staff_rejection_suppresses_routing(self) -> None:
        state = MerchantConversationState(greeted=True)
        apply_staff_contact_session_flags(state, "ما أبغى أمين")
        assert is_staff_contact_suppressed(state)
        allowed, reason = customer_allows_staff_vcard(
            customer_msg="أنا جايكم",
            commerce_session=state.commerce_session,
        )
        assert not allowed
        assert reason == "staff_contact_suppressed"

    def test_arrival_with_policy_still_allowed(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from modules.ai.postprocess.safety_nets import apply_staff_contact_safety_net

        db = _install_staff_stubs(monkeypatch)
        result = apply_staff_contact_safety_net(
            customer_msg="أنا جايكم",
            reply_text="تواصل مع أمين عند الوصول للمعرض",
            existing_call_targets=[],
            detected_call_markers=0,
            db=db,
            tenant_id=42,
        )
        assert result.fired is True


class TestIdentityContinuity:
    @pytest.mark.parametrize(
        "msg",
        (
            "انت تركي",
            "انت انسان؟",
            "انت نحلة؟",
        ),
    )
    def test_identity_intent(self, msg: str) -> None:
        intent = rules.match(msg)
        assert intent is not None
        assert intent.name == INTENT_WHO_ARE_YOU

    @pytest.mark.parametrize(
        "msg",
        (
            "انت تركي",
            "انت انسان؟",
            "انت نحلة؟",
        ),
    )
    def test_identity_routes_persona(self, msg: str) -> None:
        dec = DefaultDecisionEngine().decide(_ctx(msg))
        assert dec.action == ACTION_LLM_REPLY
        assert dec.args.get("topic") == "persona_identity"


class TestComplaintAfterActiveOrderJourney:
    """Proof: ORDERING → SUPPORT when complaint arrives mid-funnel."""

    _COMPLAINT_MSG = "وصلني المنتج وهو مغشوش\nأبغى فلوسي"

    def _ordering_state(self) -> MerchantConversationState:
        state = MerchantConversationState(
            greeted=True,
            stage=STAGE_ORDERING,
            commerce_objective=COMMERCE_OBJECTIVE_ORDERING,
            commerce_objective_turn=2,
            current_product_focus={"title": "عسل طلح", "product_id": 101},
        )
        state.order_prep = OrderPreparationState.from_dict({
            "product_name": "عسل طلح",
            "quantity_label": "نصف كilo",
            "line_items": [{"product_id": 101, "title": "عسل طلح", "quantity": 0.5}],
            "city": "الرياض",
        })
        state.commerce_session = {
            "order_intent": True,
            "active_product": "عسل طلح",
            "stage": "variant_selected",
        }
        return state

    def test_complaint_overrides_active_ordering_funnel(self) -> None:
        state = self._ordering_state()
        objective_before = get_commerce_objective(state)
        assert objective_before == COMMERCE_OBJECTIVE_ORDERING

        intent = rules.match(self._COMPLAINT_MSG)
        assert intent is not None
        assert intent.name == INTENT_COMPLAINT_REFUND

        ctx = BrainContext(
            tenant_id=1,
            customer_phone="+966500000000",
            message=self._COMPLAINT_MSG,
            intent=intent,
            state=state,
            facts=CommerceFacts(
                has_products=True,
                product_count=5,
                orderable=True,
                has_active_integration=True,
                store_name="test",
            ),
            history=[
                {"direction": "in", "body": "أبغى عسل طلح"},
                {"direction": "out", "body": "عندنا عسل طلح"},
                {"direction": "in", "body": "نصف كilo"},
                {"direction": "out", "body": "تمام، سجلت لك الطلب"},
            ],
        )

        dec = DefaultDecisionEngine().decide(ctx)
        assert dec.action == ACTION_LLM_REPLY
        assert dec.args.get("topic") == "support_complaint_refund"
        assert dec.args.get("block_order_flow") is True
        assert "complaint" in (dec.reason or "").lower()

        apply_complaint_refund_session_flags(state, self._COMPLAINT_MSG, dec)
        objective_after = get_commerce_objective(state)
        assert objective_after == COMMERCE_OBJECTIVE_SUPPORT
        assert state.stage == STAGE_SUPPORT
        assert state.commerce_session.get("complaint_refund_active") is True

        draft = compose_wa_order_flow_reply(
            order_prep=state.order_prep,
            brain_state=state.to_dict(),
            cart_changed=True,
            customer_message=self._COMPLAINT_MSG,
        )
        assert draft is None

    def test_outbound_is_support_intake_not_order_prompt(self) -> None:
        """Operational intake only — no address/city/order continuation."""
        assert classify_complaint_refund(self._COMPLAINT_MSG)
        dec = try_complaint_refund_decision(_ctx(self._COMPLAINT_MSG))
        assert dec is not None
        assert dec.args.get("topic") == "support_complaint_refund"
        assert COMPLAINT_INTAKE_REPLY_AR == (
            "وصلتنا ملاحظتك ونعتذر عن التجربة.\n\n"
            "فضلاً أرسل رقم الطلب أو صورة المنتج أو الفاتورة حتى نراجع الحالة."
        )
        assert "المدينة" not in COMPLAINT_INTAKE_REPLY_AR
        assert "سجلت لك الطلب" not in COMPLAINT_INTAKE_REPLY_AR
        assert "رقم الطلب" in COMPLAINT_INTAKE_REPLY_AR


class TestCityVsArrivalDisambiguation:
    """Proof: city mention ≠ in-person arrival intent."""

    @pytest.mark.parametrize(
        "msg",
        (
            "أنا في الطائف",
            "الطائف",
            "توصيل للطائف",
        ),
    )
    def test_city_mentions_not_store_arrival(self, msg: str) -> None:
        assert classify_store_arrival(msg) is None

    @pytest.mark.parametrize(
        "msg",
        (
            "أنا عند باب المعرض",
            "وصلت للمعرض",
            "أنا جايكم",
            "عند الباب",
        ),
    )
    def test_physical_arrival_signals_detected(self, msg: str) -> None:
        assert classify_store_arrival(msg) is not None

    def test_city_only_no_vcard_even_with_staff_reply_offer(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from modules.ai.postprocess.safety_nets import apply_staff_contact_safety_net

        db = _install_staff_stubs(monkeypatch)
        for city_msg in ("أنا في الطائف", "أنا من جدة"):
            result = apply_staff_contact_safety_net(
                customer_msg=city_msg,
                reply_text="تقدر تتواصل مع بائع المعرض",
                existing_call_targets=[],
                detected_call_markers=0,
                db=db,
                tenant_id=42,
            )
            assert result.fired is False
            assert result.skipped_reason == "no_staff_intent"
