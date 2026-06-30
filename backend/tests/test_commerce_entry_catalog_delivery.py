"""PR-CE2 — commerce entry catalog delivery ownership."""
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
    catalog_delivery_is_blocked,
    has_pending_catalog_send,
    is_catalog_send_confirmation,
    pin_pending_catalog_send,
    try_commerce_entry_catalog_decision,
)
from modules.ai.brain.commerce.payment_evidence_turn_route import (  # noqa: E402
    current_turn_has_payment_evidence,
)
from modules.ai.brain.commerce.non_catalog_availability_kb_route import (  # noqa: E402
    TOPIC_KB_AVAILABILITY_FACTS,
)
from modules.ai.brain.commerce.status_reply_product_context import (  # noqa: E402
    try_status_reply_product_decision,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_CLARIFY,
    ACTION_CATALOG_NAVIGATE,
    ACTION_LLM_REPLY,
    ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_SEARCH_PRODUCTS,
    ACTION_VARIANT_PRICING,
)
from modules.ai.brain.intent import rules as intent_rules  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)


class _StubProduct:
    def __init__(self, *, pid: int, title: str, price: float = 120.0) -> None:
        self.id = pid
        self.title = title
        self.price = price
        self.meta_retailer_id = f"ret-{pid}"


class _StubKBSection:
    def __init__(self, *, section_id: int, title: str, body: str, kind: str = "faq") -> None:
        self.id = section_id
        self.kind = kind
        self.title = title
        self.body = body
        self.priority = 10
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

    def first(self) -> Any:
        return self._rows[0] if self._rows else None

    def all(self) -> List[Any]:
        return list(self._rows)


class _StubDB:
    def __init__(
        self,
        *,
        products: Optional[List[_StubProduct]] = None,
        kb_sections: Optional[List[_StubKBSection]] = None,
    ) -> None:
        self._products = products or []
        self._kb_sections = kb_sections or []

    def query(self, model: Any) -> _QueryStub:
        name = getattr(model, "__name__", str(model))
        if name == "Product":
            return _QueryStub(self._products)
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


def _bee_kb() -> _StubKBSection:
    return _StubKBSection(
        section_id=7,
        kind="faq",
        title="طرود نحل",
        body="طرود النحل غير متوفرة حالياً.",
    )


class TestCommerceEntryCatalogDelivery:
    def test_send_catalog_explicit_request(self) -> None:
        decision = try_commerce_entry_catalog_decision(
            _ctx("أرسل الكتalog", db=_StubDB()),
        )
        assert decision is not None
        assert decision.action == ACTION_CATALOG_NAVIGATE
        assert decision.args.get("catalog_delivery_kind") == CatalogDeliveryKind.SEND_CATALOG.value

    def test_types_overview_sends_catalog(self) -> None:
        decision = try_commerce_entry_catalog_decision(
            _ctx("وش الأنواع المتوفرة؟", db=_StubDB()),
        )
        assert decision is not None
        assert decision.action == ACTION_CATALOG_NAVIGATE
        assert decision.args.get("catalog_delivery_kind") == CatalogDeliveryKind.SEND_CATALOG.value

    def test_show_category_browse_sends_catalog(self) -> None:
        db = _StubDB(products=[_StubProduct(pid=1, title="عسل سدر")])
        decision = try_commerce_entry_catalog_decision(
            _ctx("أبي أشوف العسل", db=db),
        )
        assert decision is not None
        assert decision.action in {ACTION_CATALOG_NAVIGATE, ACTION_SEARCH_PRODUCTS}
        kind = decision.args.get("catalog_delivery_kind")
        assert kind in {
            CatalogDeliveryKind.SEND_CATALOG.value,
            CatalogDeliveryKind.SEND_PRODUCT_CATALOG_ITEM.value,
        }

    def test_named_catalog_product_availability_not_kb(self) -> None:
        db = _StubDB(products=[_StubProduct(pid=5, title="عسل السمر")])
        decision = try_commerce_entry_catalog_decision(
            _ctx("هل عسل السمر متوفر؟", db=db),
        )
        assert decision is not None
        assert decision.action == ACTION_SEARCH_PRODUCTS
        assert decision.args.get("catalog_delivery_kind") == (
            CatalogDeliveryKind.SEND_PRODUCT_CATALOG_ITEM.value
        )
        assert decision.args.get("topic") != TOPIC_KB_AVAILABILITY_FACTS

    def test_non_catalog_kb_wins_over_catalog(self, monkeypatch: pytest.MonkeyPatch) -> None:
        db = _install_kb_stubs(monkeypatch, [_bee_kb()])
        state = _state()
        decision = try_commerce_entry_catalog_decision(
            _ctx("فيه عندك طرود نحل؟", state=state, db=db),
        )
        assert decision is not None
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == TOPIC_KB_AVAILABILITY_FACTS
        assert decision.args.get("catalog_delivery_kind") == (
            CatalogDeliveryKind.DELEGATE_KB_AVAILABILITY.value
        )
        assert catalog_delivery_is_blocked(_ctx("فيه عندك طرود نحل؟", state=state, db=db))

    def test_catalog_miss_complaint_blocks_repeat(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = _state()
        state.commerce_session = {"catalog_delivery_last_sent": True}
        db = _install_kb_stubs(monkeypatch, [_bee_kb()])
        decision = try_commerce_entry_catalog_decision(
            _ctx("مافيه إلا عسل", state=state, db=db),
        )
        assert decision is None or decision.args.get("catalog_delivery_kind") != (
            CatalogDeliveryKind.SEND_CATALOG.value
        )
        assert catalog_delivery_is_blocked(_ctx("مافيه إلا عسل", state=state, db=db))

    def test_correction_routes_kb_not_catalog(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = _state()
        state.commerce_session = {"catalog_delivery_last_sent": True}
        db = _install_kb_stubs(monkeypatch, [_bee_kb()])
        decision = try_commerce_entry_catalog_decision(
            _ctx("أقصد طرود أربيها", state=state, db=db),
        )
        if decision is not None:
            assert decision.args.get("catalog_delivery_kind") in {
                CatalogDeliveryKind.DELEGATE_KB_AVAILABILITY.value,
                None,
            }
            assert decision.action != ACTION_CATALOG_NAVIGATE
        assert catalog_delivery_is_blocked(
            _ctx("أقصد طرود أربيها", state=state, db=db),
        )

    def test_status_price_stays_ce1_not_catalog(self) -> None:
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
            "price": 120.0,
            "from_status_reply": True,
        }
        db = _StubDB(products=[_StubProduct(pid=9, title="عسل سدر بلدي")])
        catalog_dec = try_commerce_entry_catalog_decision(
            _ctx("كم سعرhe", state=state, db=db),
        )
        assert catalog_dec is None
        status_dec = try_status_reply_product_decision(
            _ctx("كم سعرhe", state=state, db=db),
        )
        assert status_dec is not None
        assert status_dec.action in {ACTION_VARIANT_PRICING, ACTION_CLARIFY}

    def test_status_send_link_sends_product_item(self) -> None:
        state = _state()
        state.current_product_focus = {
            "id": 9,
            "title": "عسل سدر بلدي",
            "from_status_reply": True,
        }
        db = _StubDB(products=[_StubProduct(pid=9, title="عسل سدر بلدي")])
        decision = try_commerce_entry_catalog_decision(
            _ctx("أرسل الرابط", state=state, db=db),
        )
        assert decision is not None
        assert decision.action == ACTION_SEARCH_PRODUCTS
        assert decision.args.get("catalog_delivery_kind") == (
            CatalogDeliveryKind.SEND_PRODUCT_CATALOG_ITEM.value
        )

    def test_product_knowledge_does_not_send_catalog(self) -> None:
        state = _state()
        decision = try_commerce_entry_catalog_decision(
            _ctx("وش الفرق عن السدر العادي؟", state=state, db=_StubDB()),
        )
        assert decision is None
        assert catalog_delivery_is_blocked(
            _ctx("وش الفرق عن السدر العادي؟", state=state, db=_StubDB()),
        )

    def test_order_prep_product_id_defers_catalog_to_draft_order(self) -> None:
        from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: PLC0415

        state = _state(
            stage="ordering",
            current_product_focus=None,
            order_prep=OrderPreparationState(
                product_id="ext-7",
                customer_first_name="نورة",
                city="جدة",
                short_address_code="JEDD9988",
            ),
        )
        state.last_search_candidates = [
            {
                "id": 7,
                "external_id": "ext-7",
                "title": "عسل السدر",
                "can_checkout": True,
                "orderable": True,
            },
        ]
        message = "تمام، أرسل الطلب"
        ctx = BrainContext(
            tenant_id=33,
            customer_phone="966500000001",
            message=message,
            intent=Intent(name="general", confidence=0.9, raw_message=message),
            state=state,
            facts=CommerceFacts(has_products=True, product_count=5, orderable=True),
        )
        catalog_dec = try_commerce_entry_catalog_decision(ctx)
        assert catalog_dec is None
        engine_dec = DefaultDecisionEngine().decide(ctx)
        assert engine_dec.action == ACTION_PROPOSE_DRAFT_ORDER
        assert engine_dec.args.get("source") == "order_prep_recovery"


class TestExplicitCatalogSendAndPendingConfirmation:
    def test_arabic_send_catalog_immediate(self) -> None:
        decision = try_commerce_entry_catalog_decision(
            _ctx("ارسل الكتalog", db=_StubDB()),
        )
        assert decision is not None
        assert decision.action == ACTION_CATALOG_NAVIGATE
        assert decision.args.get("catalog_delivery_kind") == CatalogDeliveryKind.SEND_CATALOG.value

    def test_arabic_katalog_variants_immediate(self) -> None:
        for message in (
            "ارسل الكتalog",
            "أرسل الكتalog",
            "الكتalog",
            "اعرض الكتalog",
            "ورني الكتalog",
        ):
            decision = try_commerce_entry_catalog_decision(_ctx(message, db=_StubDB()))
            assert decision is not None, message
            assert decision.action == ACTION_CATALOG_NAVIGATE, message
            assert decision.args.get("catalog_delivery_kind") == (
                CatalogDeliveryKind.SEND_CATALOG.value
            ), message

    def test_pending_catalog_confirmation_send(self) -> None:
        state = _state()
        pin_pending_catalog_send(state, source="catalog_confirmation")
        decision = try_commerce_entry_catalog_decision(
            _ctx("ارسل", state=state, db=_StubDB()),
        )
        assert decision is not None
        assert decision.action == ACTION_CATALOG_NAVIGATE
        assert decision.args.get("catalog_delivery_kind") == CatalogDeliveryKind.SEND_CATALOG.value
        assert not has_pending_catalog_send(state)

    def test_pending_catalog_confirmation_nam(self) -> None:
        state = _state()
        pin_pending_catalog_send(state, source="catalog_confirmation")
        decision = try_commerce_entry_catalog_decision(
            _ctx("نعم", state=state, db=_StubDB()),
        )
        assert decision is not None
        assert decision.action == ACTION_CATALOG_NAVIGATE

    def test_explicit_catalog_after_payment_receipt_prior_block(self) -> None:
        state = _state()
        state.commerce_session = {"catalog_delivery_blocked": "payment_evidence"}
        decision = try_commerce_entry_catalog_decision(
            _ctx("ارسل الكتalog", state=state, db=_StubDB()),
        )
        assert decision is not None
        assert decision.action == ACTION_CATALOG_NAVIGATE
        assert not catalog_delivery_is_blocked(_ctx("ارسل الكتalog", state=state, db=_StubDB()))

    def test_receipt_same_turn_blocks_catalog(self) -> None:
        meta = {
            "normalized_type": "document",
            "has_attached_media": True,
            "pdf_kind": "payment_receipt",
            "payment_evidence_status": "confirmed",
            "receipt_data": {"amount": 350.0, "beneficiary_name": "test"},
        }
        ctx = _ctx("ارسل الكتalog", inbound_metadata=meta, db=_StubDB())
        assert current_turn_has_payment_evidence(ctx) is True
        assert try_commerce_entry_catalog_decision(ctx) is None

    def test_bare_send_without_pending_not_product_item(self) -> None:
        state = _state(
            stage="checkout",
            current_product_focus={"id": 9, "title": "عسل سدر", "from_status_reply": True},
        )
        decision = try_commerce_entry_catalog_decision(
            _ctx("ارسل", state=state, db=_StubDB()),
        )
        assert decision is None
        assert is_catalog_send_confirmation("ارسل")

    def test_ce1_regression_status_product_buy_not_catalog(self) -> None:
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
            "price": 120.0,
            "from_status_reply": True,
        }
        db = _StubDB(products=[_StubProduct(pid=9, title="عسل سدر بلدي")])
        catalog_dec = try_commerce_entry_catalog_decision(_ctx("أبيه", state=state, db=db))
        assert catalog_dec is None
        status_dec = try_status_reply_product_decision(_ctx("أبيه", state=state, db=db))
        assert status_dec is not None
        assert status_dec.action in {ACTION_PROPOSE_DRAFT_ORDER, ACTION_VARIANT_PRICING, ACTION_CLARIFY}

    def test_grounding_guard_pins_pending_catalog_confirmation(self) -> None:
        from modules.ai.brain.postprocess.catalog_product_grounding_guard import (  # noqa: PLC0415
            apply_catalog_product_grounding_guard,
        )

        state = _state()
        result = apply_catalog_product_grounding_guard(
            reply="عندنا عسل القطف وعسل الشهد متوفر.",
            inbound_text="وش عندكم؟",
            executor_products=[{"title": "عسل سدر بلدي"}],
            order_state=state,
            chosen_path="llm_general",
        )
        assert result.replaced is True
        assert "الخيارات المؤكدة" in result.reply
        assert has_pending_catalog_send(state)

    def test_ce4_regression_product_knowledge_not_catalog(self) -> None:
        state = _state()
        decision = try_commerce_entry_catalog_decision(
            _ctx("وش الفرق عن السدر العادي؟", state=state, db=_StubDB()),
        )
        assert decision is None
        assert catalog_delivery_is_blocked(
            _ctx("وش الفرق عن السدر العادي؟", state=state, db=_StubDB()),
        )
        state = _state()
        decision = try_commerce_entry_catalog_decision(
            _ctx("وش الفرق عن السدر العادي؟", state=state, db=_StubDB()),
        )
        assert decision is None
        assert catalog_delivery_is_blocked(
            _ctx("وش الفرق عن السدر العادي؟", state=state, db=_StubDB()),
        )
