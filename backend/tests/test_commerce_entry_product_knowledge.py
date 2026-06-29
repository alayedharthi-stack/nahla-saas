"""PR-CE4 — product knowledge / comparison ownership."""
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
    try_commerce_entry_catalog_decision,
)
from modules.ai.brain.commerce.commerce_entry_orchestrator import (  # noqa: E402
    CustomerAction,
    classify_customer_action,
)
from modules.ai.brain.commerce.non_catalog_availability_kb_route import (  # noqa: E402
    TOPIC_KB_AVAILABILITY_FACTS,
    try_non_catalog_availability_kb_decision,
)
from modules.ai.brain.commerce.product_knowledge_or_comparison import (  # noqa: E402
    ProductKnowledgeKind,
    TOPIC_PRODUCT_KNOWLEDGE_FACTS,
    classify_product_knowledge_kind,
    try_product_knowledge_decision,
)
from modules.ai.brain.commerce.status_reply_product_context import (  # noqa: E402
    try_status_reply_product_decision,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_CATALOG_NAVIGATE,
    ACTION_LLM_REPLY,
    ACTION_SEARCH_PRODUCTS,
    ACTION_VARIANT_PRICING,
)
from modules.ai.brain.intent import rules as intent_rules  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    MerchantConversationState,
)


class _StubProduct:
    def __init__(
        self,
        *,
        pid: int,
        title: str,
        price: float = 120.0,
        description: str = "",
    ) -> None:
        self.id = pid
        self.title = title
        self.price = price
        self.description = description
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


def _status_focus(
    state: MerchantConversationState,
    *,
    title: str = "عسل سدرة قيضية نادرة جدًا",
    pid: int = 42,
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
        "price": 250.0,
        "from_status_reply": True,
    }


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


class TestProductKnowledgeClassification:
    def test_comparison_kind(self) -> None:
        assert classify_product_knowledge_kind("ايش يفرق عن السدر العادي؟") == (
            ProductKnowledgeKind.COMPARISON
        )

    def test_batch_kind(self) -> None:
        assert classify_product_knowledge_kind("هو نفس الإنتاج اللي قبل سنة؟") == (
            ProductKnowledgeKind.BATCH
        )

    def test_value_kind(self) -> None:
        assert classify_product_knowledge_kind("ليش أغلى؟") == ProductKnowledgeKind.VALUE

    def test_features_kind(self) -> None:
        assert classify_product_knowledge_kind("وش يميزه؟") == ProductKnowledgeKind.FEATURES

    def test_price_query_not_knowledge(self) -> None:
        assert classify_product_knowledge_kind("كم سعرhe") is None

    def test_customer_action_knowledge(self) -> None:
        assert classify_customer_action("ايش يفرق عن السدر العادي؟") == CustomerAction.KNOWLEDGE


class TestCommerceEntryProductKnowledge:
    def test_status_product_comparison_routes_to_knowledge(self) -> None:
        state = _state()
        _status_focus(state)
        db = _StubDB(
            products=[_StubProduct(pid=42, title="عسل سدرة قيضية نادرة جدًا", price=250.0)],
        )
        message = "ايش يفرق عن السدر العادي؟"
        ctx = _ctx(message, state=state, db=db)

        catalog_dec = try_commerce_entry_catalog_decision(ctx)
        assert catalog_dec is None
        assert catalog_delivery_is_blocked(ctx)

        status_dec = try_status_reply_product_decision(ctx)
        assert status_dec is None

        decision = try_product_knowledge_decision(ctx)
        assert decision is not None
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == TOPIC_PRODUCT_KNOWLEDGE_FACTS
        assert decision.args.get("question_kind") == ProductKnowledgeKind.COMPARISON.value
        assert decision.args.get("customer_action") == "knowledge"
        assert decision.args.get("block_catalog_escalation") is True
        assert decision.args.get("block_staff_contact") is True
        assert decision.args.get("block_commerce_escalation") is True
        subject = decision.args.get("subject_product") or {}
        assert "سدرة" in str(subject.get("title") or "")
        goal = str(decision.args.get("response_goal") or "")
        assert "تبي رقمهم" in goal
        assert "no catalog browse push" in goal

    def test_comparison_with_kb_facts_in_response_goal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        kb = _StubKBSection(
            section_id=11,
            kind="product_info",
            title="سدرة قيضية",
            body="تختلف عن السدر العادي بندرة القطف ومصدر الجبال.",
        )
        db = _install_kb_stubs(monkeypatch, [kb])
        state = _state()
        _status_focus(state)
        decision = try_product_knowledge_decision(
            _ctx("ايش يفرق عن السدر العادي؟", state=state, db=db),
        )
        assert decision is not None
        allowed = decision.args.get("allowed_facts") or {}
        assert allowed.get("kb_sections")
        goal = str(decision.args.get("response_goal") or "")
        assert "KB sections are authoritative" in goal
        forbidden = decision.args.get("forbidden_claims") or []
        assert "invented_harvest_year" in forbidden
        assert "invented_medical_benefit" in forbidden

    def test_batch_without_facts_marks_missing(self) -> None:
        state = _state()
        _status_focus(state)
        decision = try_product_knowledge_decision(
            _ctx("هو نفس الإنتاج اللي قبل سنة؟", state=state, db=_StubDB()),
        )
        assert decision is not None
        assert decision.args.get("question_kind") == ProductKnowledgeKind.BATCH.value
        missing = decision.args.get("missing_facts") or []
        assert "batch_or_harvest_year" in missing
        goal = str(decision.args.get("response_goal") or "")
        assert "do not invent" in goal

    def test_value_price_reason_uses_facts_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        kb = _StubKBSection(
            section_id=12,
            kind="product_benefit",
            title="سدرة قيضية",
            body="السعر أعلى لندرة القطف وصعوبة الوصول.",
        )
        db = _install_kb_stubs(monkeypatch, [kb])
        state = _state()
        _status_focus(state)
        decision = try_product_knowledge_decision(
            _ctx("ليش أغلى؟", state=state, db=db),
        )
        assert decision is not None
        assert decision.args.get("question_kind") == ProductKnowledgeKind.VALUE.value
        goal = str(decision.args.get("response_goal") or "")
        assert "invented medical benefit" in goal
        assert (decision.args.get("allowed_facts") or {}).get("kb_sections")

    def test_no_product_focus_uses_comparison_hint(self) -> None:
        decision = try_product_knowledge_decision(
            _ctx("وش الفرق عن السدر العادي؟", db=_StubDB()),
        )
        assert decision is not None
        allowed = decision.args.get("allowed_facts") or {}
        assert "comparison_reference_text" in allowed
        assert "السدر" in str(allowed.get("comparison_reference_text") or "")
        missing = decision.args.get("missing_facts") or []
        assert "comparison_facts" in missing

    def test_knowledge_turn_blocks_catalog_send(self) -> None:
        state = _state()
        _status_focus(state)
        ctx = _ctx("وش يميزه؟", state=state, db=_StubDB())
        assert try_product_knowledge_decision(ctx) is not None
        assert try_commerce_entry_catalog_decision(ctx) is None
        assert catalog_delivery_is_blocked(ctx)

    def test_ce2_regression_send_catalog_still_works(self) -> None:
        decision = try_commerce_entry_catalog_decision(
            _ctx("أرسل الكتalog", db=_StubDB()),
        )
        assert decision is not None
        assert decision.action == ACTION_CATALOG_NAVIGATE
        assert decision.args.get("catalog_delivery_kind") == CatalogDeliveryKind.SEND_CATALOG.value

    def test_ce1_regression_status_price_not_knowledge(self) -> None:
        state = _state()
        _status_focus(state, title="عسل سدر بلدي", pid=9)
        db = _StubDB(products=[_StubProduct(pid=9, title="عسل سدر بلدي")])
        ctx = _ctx("كم سعرhe", state=state, db=db)
        assert try_product_knowledge_decision(ctx) is None
        status_dec = try_status_reply_product_decision(ctx)
        assert status_dec is not None
        assert status_dec.action in {ACTION_VARIANT_PRICING}

    def test_kb_non_catalog_availability_not_knowledge(self, monkeypatch: pytest.MonkeyPatch) -> None:
        db = _install_kb_stubs(
            monkeypatch,
            [
                _StubKBSection(
                    section_id=7,
                    kind="faq",
                    title="طرود نحل",
                    body="طرود النحل غير متوفرة حالياً.",
                ),
            ],
        )
        message = "فيه عندك طرود نحل؟"
        ctx = _ctx(message, db=db)
        assert try_product_knowledge_decision(ctx) is None
        kb_dec = try_non_catalog_availability_kb_decision(ctx)
        assert kb_dec is not None
        assert kb_dec.args.get("topic") == TOPIC_KB_AVAILABILITY_FACTS
        catalog_dec = try_commerce_entry_catalog_decision(ctx)
        assert catalog_dec is not None
        assert catalog_dec.args.get("topic") == TOPIC_KB_AVAILABILITY_FACTS
