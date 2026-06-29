"""Status/story reply product context ownership — platform-wide."""
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

from modules.ai.brain.commerce.status_reply_product_context import (  # noqa: E402
    TOPIC_STATUS_REPLY_PRODUCT_CONTEXT,
    apply_status_reply_product_context_to_state,
    extract_status_reply_quantity,
    is_status_reply_follow_up_message,
    resolve_status_reply_product_context,
    try_status_reply_product_decision,
)
from modules.ai.brain.decision.actions import ACTION_LLM_REPLY  # noqa: E402
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
from modules.ai.media.normalizer import _whatsapp_context_metadata  # noqa: E402


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


class TestNormalizerContextCapture:
    def test_text_message_whatsapp_context_metadata(self) -> None:
        meta = _whatsapp_context_metadata({
            "context": {
                "from": "966511111111",
                "id": "wamid.STATUS123",
                "referred_product": {
                    "catalog_id": "CAT1",
                    "product_retailer_id": "ret-9",
                },
            },
        })
        assert meta["is_status_or_reply_context"] is True
        assert meta["referred_wa_message_id"] == "wamid.STATUS123"
        assert meta["referred_product"]["product_retailer_id"] == "ret-9"


class TestStatusReplyProductContext:
    def test_quantity_linked_to_status_product_title(self) -> None:
        db = _StubDB(
            products=[_StubProduct(pid=9, title="عسل سدر بلدي")],
            outbound=_StubMessageEvent(body="عسل سدر بلدي — متوفر اليوم"),
        )
        inbound = {
            "is_status_or_reply_context": True,
            "referred_wa_message_id": "wamid.STATUS123",
        }
        state = _state()
        sr = apply_status_reply_product_context_to_state(
            db=db,
            tenant_id=33,
            message="نبغى كيلوين",
            state=state,
            inbound_metadata=inbound,
        )
        assert sr is not None
        assert sr.has_trusted_title is True
        assert state.current_product_focus is not None
        assert state.current_product_focus.get("title") == "عسل سدر بلدي"
        assert state.current_product_focus.get("from_status_reply") is True
        assert extract_status_reply_quantity("نبغى كيلوين")["quantity"] == 2

    def test_price_follow_up_uses_same_product_context(self) -> None:
        db = _StubDB(products=[_StubProduct(pid=9, title="عسل سدر بلدي")])
        state = _state()
        state.commerce_session = {
            "status_reply_product_context": {
                "active": True,
                "product_title": "عسل سدر بلدي",
                "product_id": 9,
                "has_trusted_title": True,
            },
        }
        state.current_product_focus = {
            "id": 9,
            "title": "عسل سدر بلدي",
            "from_status_reply": True,
        }
        decision = try_status_reply_product_decision(
            _ctx("كم سعره", state=state, db=db),
        )
        assert decision is not None
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == TOPIC_STATUS_REPLY_PRODUCT_CONTEXT
        assert state.current_product_focus.get("title") == "عسل سدر بلدي"

    def test_image_only_status_asks_clarify_not_availability_fallback(self) -> None:
        db = _StubDB(
            outbound=_StubMessageEvent(
                body="📎 صورة",
                extra_metadata={
                    "wa_message_id": "wamid.IMG1",
                    "normalized_inbound": {"source_type": "image", "mime_type": "image/jpeg"},
                },
            ),
        )
        inbound = {
            "is_status_or_reply_context": True,
            "referred_wa_message_id": "wamid.IMG1",
        }
        state = _state()
        decision = try_status_reply_product_decision(
            _ctx("نبغى كيلوين", state=state, db=db, inbound_metadata=inbound),
        )
        assert decision is not None
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == TOPIC_STATUS_REPLY_PRODUCT_CONTEXT
        goal = str(decision.args.get("response_goal") or "")
        assert "clarifying question" in goal
        fb = select_arabic_commerce_fallback(
            inbound_text="نبغى كيلوين",
            intent_name="general",
            primary_customer_goal="product_availability",
            inbound_metadata=inbound,
            state=state,
        )
        assert fb[0] != "التوفر قيد التحقق."

    def test_bare_price_without_context_asks_product_not_availability(self) -> None:
        fb, kind = select_arabic_commerce_fallback(
            inbound_text="كم سعرhe",
            intent_name="ask_price",
            primary_customer_goal="product_availability",
        )
        assert fb == "حدّد المنتج أو المقاس المطلوب."
        assert kind == "price_product_unresolved"
        assert "قيد التحقق" not in fb

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

    def test_referred_product_resolves_catalog_title(self) -> None:
        db = _StubDB(products=[_StubProduct(pid=11, title="عسل طلح")])
        hit = resolve_status_reply_product_context(
            db,
            33,
            {
                "is_status_or_reply_context": True,
                "referred_product": {"product_retailer_id": "ret-11"},
            },
        )
        assert hit is not None
        assert hit.has_trusted_title is True
        assert hit.product_title == "عسل طلح"

    def test_follow_up_message_detection(self) -> None:
        assert is_status_reply_follow_up_message("نبغى كيلوين")
        assert is_status_reply_follow_up_message("كم سعرhe")
        assert is_status_reply_follow_up_message("أبغاه")
        assert not is_status_reply_follow_up_message("صباح الخير")

    def test_status_context_does_not_hijack_kb_availability_inquiry(self) -> None:
        decision = try_status_reply_product_decision(
            _ctx("فيه عندك طرود نحل؟", inbound_metadata={}),
        )
        assert decision is None

    def test_status_context_inactive_without_reply_metadata(self) -> None:
        state = _state()
        sr = apply_status_reply_product_context_to_state(
            db=_StubDB(),
            tenant_id=33,
            message="فيه عندك طرود نحل؟",
            state=state,
            inbound_metadata={},
        )
        assert sr is None
        assert state.current_product_focus is None
