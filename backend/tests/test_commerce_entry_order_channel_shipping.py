"""PR-CE3 — order channel choice and cold shipping inquiry ownership."""
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
from modules.ai.brain.commerce.commerce_order_channel_owner import (  # noqa: E402
    OrderChannelRouteKind,
    TOPIC_COLD_SHIPPING_INQUIRY,
    TOPIC_STOREFRONT_SELF_CHECKOUT,
    get_preferred_order_channel,
    has_storefront_channel_committed,
    is_cold_shipping_inquiry,
    is_storefront_self_checkout_request,
    pin_storefront_self_checkout,
    try_commerce_order_channel_decision,
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
)


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


class _Col:
    def __init__(self, name: str) -> None:
        self.name = name

    def in_(self, values: Any) -> "_Col":
        return self

    def asc(self) -> "_Col":
        return self

    def desc(self) -> "_Col":
        return self


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
        name = getattr(model, "__name__", str(model))
        if name == "MerchantKnowledgeSection":
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


def _state(**kwargs: Any) -> MerchantConversationState:
    st = MerchantConversationState(greeted=True)
    for k, v in kwargs.items():
        setattr(st, k, v)
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
        state=state or _state(),
        facts=CommerceFacts(has_products=True, product_count=5, orderable=True),
        profile={"inbound_metadata": dict(inbound_metadata or {})},
    )
    if db is not None:
        ctx._db = db  # type: ignore[attr-defined]
    return ctx


def _status_state() -> MerchantConversationState:
    state = _state()
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


class TestCommerceOrderChannelShipping:
    def test_storefront_self_checkout_phrase_detection(self) -> None:
        assert is_storefront_self_checkout_request("أنا بدخل السلة إن شاء الله")
        assert is_storefront_self_checkout_request("بطلب من السلة")
        assert is_cold_shipping_inquiry("مبرد التوصيل")

    def test_storefront_self_checkout_route(self) -> None:
        decision = try_commerce_order_channel_decision(_ctx("أنا بدخل السلة إن شاء الله"))
        assert decision is not None
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == TOPIC_STOREFRONT_SELF_CHECKOUT
        assert decision.args.get("order_channel_route_kind") == (
            OrderChannelRouteKind.STOREFRONT_SELF_CHECKOUT.value
        )
        assert decision.args.get("block_whatsapp_quick_order") is True
        assert "whatsapp_quick_order_start" in (decision.args.get("forbidden_claims") or [])

    def test_cart_phrase_storefront(self) -> None:
        decision = try_commerce_order_channel_decision(_ctx("بطلب من السلة"))
        assert decision is not None
        assert decision.args.get("order_channel_route_kind") == (
            OrderChannelRouteKind.STOREFRONT_SELF_CHECKOUT.value
        )

    def test_after_link_then_storefront_pins_channel(self) -> None:
        state = _state()
        pin_storefront_self_checkout(state, source="follow_up")
        assert get_preferred_order_channel(state) == (
            OrderChannelRouteKind.STOREFRONT_SELF_CHECKOUT.value
        )
        assert has_storefront_channel_committed(state)

    def test_cold_shipping_no_product_prompt(self) -> None:
        ctx = _ctx("مبرد التوصيل")
        decision = try_commerce_order_channel_decision(ctx)
        assert decision is not None
        assert decision.args.get("topic") == TOPIC_COLD_SHIPPING_INQUIRY
        goal = str(decision.args.get("response_goal") or "")
        assert "do NOT ask which product" in goal
        prompt = build_product_ordering_prompt(ctx)
        assert "وش المنتج" not in prompt

    def test_cold_shipping_delivery_question(self) -> None:
        decision = try_commerce_order_channel_decision(_ctx("التوصيل مبرد؟"))
        assert decision is not None
        assert decision.args.get("order_channel_route_kind") == (
            OrderChannelRouteKind.COLD_SHIPPING_INQUIRY.value
        )
        goal = str(decision.args.get("response_goal") or "")
        assert "ask_delivery_city_only=true" in goal or "needs_city=" in goal

    def test_storefront_plus_cold_shipping_same_turn(self) -> None:
        decision = try_commerce_order_channel_decision(
            _ctx("أنا بدخل السلة إن شاء الله، التوصيل مبرد؟"),
        )
        assert decision is not None
        assert decision.args.get("topic") == TOPIC_STOREFRONT_SELF_CHECKOUT
        goal = str(decision.args.get("response_goal") or "")
        assert "same_turn_also_asks_cold_shipping=true" in goal

    def test_ce1_status_quantity_not_broken(self) -> None:
        state = _status_state()
        ctx = _ctx("نبغى كيلوين", state=state)
        channel_dec = try_commerce_order_channel_decision(ctx)
        assert channel_dec is None
        status_dec = try_status_reply_product_decision(ctx)
        assert status_dec is not None
        assert status_dec.action == ACTION_PROPOSE_DRAFT_ORDER

    def test_ce2_catalog_not_broken(self) -> None:
        ctx = _ctx("أرسل الكتalog")
        assert try_commerce_order_channel_decision(ctx) is None
        ce2 = try_commerce_entry_catalog_decision(ctx)
        assert ce2 is not None
        assert ce2.action == ACTION_CATALOG_NAVIGATE
        assert ce2.args.get("catalog_delivery_kind") == CatalogDeliveryKind.SEND_CATALOG.value

    def test_ce4_knowledge_not_broken(self) -> None:
        ctx = _ctx("وش الفرق عن السدر العادي؟")
        assert try_commerce_order_channel_decision(ctx) is None
        ce4 = try_product_knowledge_decision(ctx)
        assert ce4 is None or ce4.action == ACTION_LLM_REPLY

    def test_payment_receipt_not_broken(self, monkeypatch: pytest.MonkeyPatch) -> None:
        meta = {
            "normalized_type": "document",
            "has_attached_media": True,
            "pdf_kind": "payment_receipt",
            "payment_evidence_status": "confirmed",
            "receipt_data": {"amount": 350.0},
        }
        ctx = _ctx("", inbound_metadata=meta)
        assert try_commerce_order_channel_decision(ctx) is None
        assert try_payment_evidence_turn_decision(ctx) is not None

    def test_physical_location_not_storefront(self) -> None:
        decision = try_commerce_order_channel_decision(_ctx("وين موقعكم؟"))
        assert decision is None

    def test_showroom_visit_not_storefront(self) -> None:
        decision = try_commerce_order_channel_decision(_ctx("بمر المعرض"))
        assert decision is None

    def test_storefront_beats_ce2_false_positive(self) -> None:
        ctx = _ctx("أطلب من المتجر")
        ce3 = try_commerce_order_channel_decision(ctx)
        assert ce3 is not None
        assert ce3.args.get("topic") == TOPIC_STOREFRONT_SELF_CHECKOUT
        assert try_commerce_entry_catalog_decision(ctx) is not None

    def test_cold_shipping_kb_attached_when_present(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db = _install_kb_stubs(monkeypatch, [
            _StubKBSection(
                section_id=11,
                kind="cold_shipping",
                title="الشحن المبرد",
                body="الشحن المبرد متاح لمدن محددة حسب المدينة.",
            ),
        ])
        decision = try_commerce_order_channel_decision(_ctx("مبرد التوصيل", db=db))
        assert decision is not None
        allowed = decision.args.get("allowed_facts") or {}
        assert allowed.get("kb_section_kind") == "cold_shipping"

    def test_engine_routes_storefront_before_whatsapp_order(self) -> None:
        decision = DefaultDecisionEngine().decide(_ctx("أنا بدخل السلة إن شاء الله"))
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == TOPIC_STOREFRONT_SELF_CHECKOUT
