"""PR-CE1 — status product price / quantity / buy ownership."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, List, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")):
    if p not in sys.path:
        sys.path.insert(0, p)

from modules.ai.brain.commerce.commerce_entry_orchestrator import (  # noqa: E402
    CustomerAction,
    classify_customer_action,
    resolve_status_entry,
)
from modules.ai.brain.commerce.status_reply_product_context import (  # noqa: E402
    StatusReplyProductContext,
    apply_status_reply_product_context_to_state,
    extract_status_reply_quantity,
    try_status_reply_product_decision,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_CLARIFY,
    ACTION_LLM_REPLY,
    ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_VARIANT_PRICING,
)
from modules.ai.brain.intent import rules as intent_rules  # noqa: E402
from modules.ai.brain.postprocess.commerce_reply_quality_guard import (  # noqa: E402
    apply_commerce_reply_quality_guard,
    select_arabic_commerce_fallback,
)
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    MerchantConversationState,
)


class _StubProduct:
    def __init__(self, *, pid: int, title: str, price: float = 120.0) -> None:
        self.id = pid
        self.title = title
        self.price = price
        self.external_id = f"ext-{pid}"
        self.sku = f"SKU-{pid}"
        self.meta_retailer_id = f"ret-{pid}"


class _StubMessageEvent:
    def __init__(self, *, body: str, extra_metadata: Optional[dict] = None) -> None:
        self.body = body
        self.extra_metadata = extra_metadata or {}


class _QueryStub:
    def __init__(self, rows: List[Any]) -> None:
        self._rows = rows

    def filter(self, *args: Any, **kwargs: Any) -> "_QueryStub":
        return self

    def order_by(self, *args: Any, **kwargs: Any) -> "_QueryStub":
        return self

    def limit(self, n: int) -> "_QueryStub":
        return self

    def first(self) -> Any:
        return self._rows[0] if self._rows else None

    def all(self) -> List[Any]:
        return list(self._rows)


class _StubDB:
    def __init__(
        self,
        *,
        products: Optional[List[_StubProduct]] = None,
        outbound: Optional[_StubMessageEvent] = None,
    ) -> None:
        self._products = products or []
        self._outbound = outbound

    def query(self, model: Any) -> _QueryStub:
        name = getattr(model, "__name__", str(model))
        if name == "Product":
            return _QueryStub(self._products)
        if name == "MessageEvent":
            return _QueryStub([self._outbound] if self._outbound else [])
        return _QueryStub([])


def _state() -> MerchantConversationState:
    return MerchantConversationState(greeted=True)


def _ctx(
    message: str,
    *,
    state: Optional[MerchantConversationState] = None,
    db: Any = None,
    inbound_metadata: Optional[dict] = None,
) -> BrainContext:
    intent = intent_rules.match(message)
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


def _status_session(
    state: MerchantConversationState,
    *,
    title: str = "عسل سدر بلدي",
    pid: int = 9,
) -> None:
    state.commerce_session = {
        "status_reply_product_context": {
            "active": True,
            "product_title": title,
            "product_id": pid,
            "has_trusted_title": True,
        },
    }
    state.current_product_focus = {
        "id": pid,
        "title": title,
        "price": 120.0,
        "from_status_reply": True,
    }


def _sr(**kwargs: Any) -> StatusReplyProductContext:
    defaults = {
        "source": "test",
        "product_title": "عسل سدر بلدي",
        "product_id": 9,
        "has_trusted_title": True,
    }
    defaults.update(kwargs)
    return StatusReplyProductContext(**defaults)


class TestCommerceEntryStatusPriceQuantity:
    def test_status_product_quantity_pins_focus_and_starts_order(self) -> None:
        db = _StubDB(
            products=[_StubProduct(pid=9, title="عسل سدر بلدي", price=120.0)],
            outbound=_StubMessageEvent(body="عسل سدر بلدي — متوفر اليوم"),
        )
        inbound = {
            "is_status_or_reply_context": True,
            "referred_wa_message_id": "wamid.STATUS123",
        }
        state = _state()
        decision = try_status_reply_product_decision(
            _ctx("نبغى كيلوين", state=state, db=db, inbound_metadata=inbound),
        )
        assert decision is not None
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER
        assert state.current_product_focus is not None
        assert state.current_product_focus.get("title") == "عسل سدر بلدي"
        assert extract_status_reply_quantity("نبغى كيلوين")["quantity"] == 2
        assert state.order_prep.quantity == 2
        goal = str(decision.args.get("response_goal") or "")
        assert "أي نوع" not in goal

    def test_status_product_price_routes_to_variant_pricing(self) -> None:
        db = _StubDB(products=[_StubProduct(pid=9, title="عسل سدر بلدي", price=120.0)])
        state = _state()
        _status_session(state)
        decision = try_status_reply_product_decision(
            _ctx("كم سعرhe", state=state, db=db),
        )
        assert decision is not None
        assert decision.action in {ACTION_VARIANT_PRICING, ACTION_CLARIFY}
        assert decision.action != ACTION_LLM_REPLY
        fb, _kind = select_arabic_commerce_fallback(
            inbound_text="كم سعرhe",
            intent_name="ask_price",
            primary_customer_goal="product_availability",
            state=state,
            inbound_metadata={"status_reply_product_context": {"active": True}},
        )
        assert fb != "التوفر قيد التحقق."

    def test_status_product_unit_price_uses_focus(self) -> None:
        state = _state()
        _status_session(state, title="عسل طلح", pid=11)
        db = _StubDB(products=[_StubProduct(pid=11, title="عسل طلح", price=95.0)])
        decision = try_status_reply_product_decision(
            _ctx("بكم الكيلo", state=state, db=db),
        )
        assert decision is not None
        assert decision.action in {ACTION_VARIANT_PRICING, ACTION_CLARIFY}
        goal = str(decision.args.get("response_goal") or "")
        assert "أي منتج" not in goal

    def test_status_product_buy_starts_draft_order(self) -> None:
        state = _state()
        _status_session(state)
        db = _StubDB(products=[_StubProduct(pid=9, title="عسل سدر بلدي")])
        decision = try_status_reply_product_decision(
            _ctx("أبيه", state=state, db=db),
        )
        assert decision is not None
        assert decision.action in {
            ACTION_PROPOSE_DRAFT_ORDER,
            ACTION_CLARIFY,
        }
        assert decision.action != ACTION_LLM_REPLY

    def test_image_only_status_single_clarify_not_invented_product(self) -> None:
        db = _StubDB(
            outbound=_StubMessageEvent(
                body="📎 صورة",
                extra_metadata={
                    "wa_message_id": "wamid.IMG1",
                    "normalized_inbound": {
                        "source_type": "image",
                        "mime_type": "image/jpeg",
                    },
                },
            ),
        )
        inbound = {
            "is_status_or_reply_context": True,
            "referred_wa_message_id": "wamid.IMG1",
        }
        state = _state()
        decision = try_status_reply_product_decision(
            _ctx("نبغى كيلo", state=state, db=db, inbound_metadata=inbound),
        )
        assert decision is not None
        assert decision.action == ACTION_LLM_REPLY
        goal = str(decision.args.get("response_goal") or "")
        assert "clarifying question" in goal
        assert state.current_product_focus is None

    def test_no_status_context_price_asks_product_not_availability(self) -> None:
        fb, kind = select_arabic_commerce_fallback(
            inbound_text="كم سعرhe",
            intent_name="ask_price",
            primary_customer_goal="product_availability",
        )
        assert fb == "حدّد المنتج أو المقاس المطلوب."
        assert kind == "price_product_unresolved"
        assert "قيد التحقق" not in fb

    def test_kb_availability_not_hijacked_by_ce1(self) -> None:
        decision = try_status_reply_product_decision(
            _ctx("فيه عندك طرود نحل؟", inbound_metadata={}),
        )
        assert decision is None

    def test_classify_quantity_action_with_focus(self) -> None:
        action = classify_customer_action(
            "نبغى كيلوين",
            quantity_hint=extract_status_reply_quantity("نبغى كيلوين"),
            has_product_focus=True,
        )
        assert action == CustomerAction.QUANTITY

    def test_quality_guard_price_after_status_no_pending_verification(self) -> None:
        state = _state()
        state.current_product_focus = {
            "title": "عسل سدر بلدي",
            "from_status_reply": True,
        }
        result = apply_commerce_reply_quality_guard(
            "",
            inbound_text="كم سعرhe",
            intent_name="ask_price",
            primary_customer_goal="product_availability",
            state=state,
            inbound_metadata={"status_reply_product_context": {"active": True}},
        )
        assert "قيد التحقق" not in result.reply

    def test_multi_variant_buy_asks_size_only(self) -> None:
        state = _state()
        state.current_product_focus = {
            "id": 9,
            "title": "عسل سدر",
            "from_status_reply": True,
            "variants": [
                {"id": "v1", "option_summary": "250g", "price": 50, "in_stock": True},
                {"id": "v2", "option_summary": "500g", "price": 90, "in_stock": True},
                {"id": "v3", "option_summary": "1kg", "price": 160, "in_stock": True},
            ],
        }
        decision = resolve_status_entry(
            _ctx("أبيه", state=state),
            _sr(),
        )
        assert decision is not None
        assert decision.action == ACTION_CLARIFY
        assert "أي خيار" in str(decision.args.get("question") or "")
