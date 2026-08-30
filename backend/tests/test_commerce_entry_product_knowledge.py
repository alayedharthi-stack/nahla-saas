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
from modules.ai.brain.commerce.commerce_focus_owner import set_product_focus  # noqa: E402
from modules.ai.brain.commerce.non_catalog_availability_kb_route import (  # noqa: E402
    TOPIC_KB_AVAILABILITY_FACTS,
    try_non_catalog_availability_kb_decision,
)
from modules.ai.brain.commerce.product_knowledge_or_comparison import (  # noqa: E402
    ProductKnowledgeKind,
    TOPIC_PRODUCT_KNOWLEDGE_FACTS,
    _retrieve_product_kb_sections,
    classify_product_knowledge_kind,
    extract_features_subject,
    get_product_knowledge_session,
    pin_product_knowledge_session,
    resolve_subject_product,
    try_product_knowledge_decision,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.commerce.status_reply_product_context import (  # noqa: E402
    try_status_reply_product_decision,
)
from modules.ai.brain.postprocess.product_claim_grounding_guard import (  # noqa: E402
    apply_product_claim_grounding_guard,
)
from modules.ai.brain.state.product_information_topic import (  # noqa: E402
    detect_product_information_topic_shift,
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
    Intent,
    INTENT_ASK_PRODUCT,
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
    def __init__(
        self,
        *,
        section_id: int,
        title: str,
        body: str,
        kind: str = "faq",
        product_ids: Optional[List[int]] = None,
    ) -> None:
        self.id = section_id
        self.kind = kind
        self.title = title
        self.body = body
        self.priority = 10
        self.updated_at = None
        self.is_active = True
        self.deleted_at = None
        self.tenant_id = 33
        self.product_links = [
            SimpleNamespace(product_id=product_id)
            for product_id in (product_ids or [])
        ]


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
    facts: Optional[CommerceFacts] = None,
    intent: Optional[Intent] = None,
) -> BrainContext:
    resolved_intent = intent or intent_rules.match(message) or Intent(
        name="general",
        confidence=0.5,
        raw_message=message,
    )
    ctx = BrainContext(
        tenant_id=33,
        customer_phone="966500000001",
        message=message,
        intent=resolved_intent,
        state=state or _state(),
        facts=facts or CommerceFacts(has_products=True, product_count=5, orderable=True),
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

    def test_features_kind_ma_hiya_mumayizat(self) -> None:
        assert classify_product_knowledge_kind(
            "ما هي مميزات عسل السدر القيضي؟"
        ) == ProductKnowledgeKind.FEATURES

    def test_features_kind_wesh_mumayizat(self) -> None:
        assert classify_product_knowledge_kind(
            "وش مميزات عسل السدر القيضي؟"
        ) == ProductKnowledgeKind.FEATURES

    def test_features_kind_khasais(self) -> None:
        assert classify_product_knowledge_kind(
            "ما هي خصائص عسل السدر القيضي؟"
        ) == ProductKnowledgeKind.FEATURES

    def test_features_kind_what_are_the_features(self) -> None:
        assert classify_product_knowledge_kind(
            "what are the features of white sports shoes?"
        ) == ProductKnowledgeKind.FEATURES

    def test_features_kind_wesh_yumayiz_product(self) -> None:
        assert classify_product_knowledge_kind(
            "وش يميز عسل السدر القيضي؟"
        ) == ProductKnowledgeKind.FEATURES

    def test_health_ma_hiya_fawaid_not_features(self) -> None:
        assert classify_product_knowledge_kind(
            "ما هي فوائد عسل السدر القيضي؟"
        ) == ProductKnowledgeKind.HEALTH

    def test_generic_mumayizat_without_product_not_features_subject(self) -> None:
        assert extract_features_subject("ما هي المميزات؟") == ""

    def test_price_query_not_knowledge(self) -> None:
        assert classify_product_knowledge_kind("كم سعرhe") is None

    def test_live_case_wesh_farq_without_al(self) -> None:
        assert classify_product_knowledge_kind("وش فرق عن السدر العادي؟") == (
            ProductKnowledgeKind.COMPARISON
        )

    def test_meaning_kind(self) -> None:
        assert classify_product_knowledge_kind("وش معنى قيضية؟") == ProductKnowledgeKind.MEANING
        assert classify_product_knowledge_kind("وش قصته؟") == ProductKnowledgeKind.MEANING

    def test_explicit_health_not_comparison(self) -> None:
        assert classify_product_knowledge_kind("وش فوائده الصحية؟") == ProductKnowledgeKind.HEALTH


class TestCommerceEntryProductKnowledge:
    def test_customer_action_knowledge(self) -> None:
        assert classify_customer_action("ايش يفرق عن السدر العادي؟") == CustomerAction.KNOWLEDGE

    def test_live_status_comparison_not_health_or_details_offer(self) -> None:
        state = _state()
        _status_focus(state)
        decision = try_product_knowledge_decision(
            _ctx("وش فرق عن السدر العادي؟", state=state, db=_StubDB()),
        )
        assert decision is not None
        assert decision.args.get("question_kind") == ProductKnowledgeKind.COMPARISON.value
        goal = str(decision.args.get("response_goal") or "")
        assert "do NOT pivot to health benefits" in goal
        assert "تبي أرسل لك تفاصيله" in goal
        assert detect_product_information_topic_shift("وش فرق عن السدر العادي؟") is False

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

    def test_comparison_continuation_naaam_arsil(self, monkeypatch: pytest.MonkeyPatch) -> None:
        kb = _StubKBSection(
            section_id=21,
            kind="product_info",
            title="سدرة قيضية",
            body="تختلف عن السدر العادي بندرة القطف.",
        )
        db = _install_kb_stubs(monkeypatch, [kb])
        state = _state()
        _status_focus(state)
        first = try_product_knowledge_decision(
            _ctx("وش فرق عن السدر العادي؟", state=state, db=db),
        )
        assert first is not None
        assert get_product_knowledge_session(state).get("active") is True

        follow = try_product_knowledge_decision(
            _ctx("نعم ارسل", state=state, db=db),
        )
        assert follow is not None
        assert follow.args.get("product_knowledge_continuation") is True
        assert follow.args.get("question_kind") == ProductKnowledgeKind.COMPARISON.value
        goal = str(follow.args.get("response_goal") or "")
        assert "deliver available" in goal
        assert "no price/availability fallback" in goal

    def test_explicit_health_routes_kb_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        kb = _StubKBSection(
            section_id=31,
            kind="product_benefit",
            title="عسل سدر",
            body="قد يفيد كمصدر طاقة طبيعي.",
        )
        db = _install_kb_stubs(monkeypatch, [kb])
        state = _state()
        _status_focus(state, title="عسل سدر", pid=9)
        decision = try_product_knowledge_decision(
            _ctx("وش فوائده الصحية؟", state=state, db=db),
        )
        assert decision is not None
        assert decision.args.get("question_kind") == ProductKnowledgeKind.HEALTH.value
        sections = (decision.args.get("allowed_facts") or {}).get("kb_sections") or []
        assert sections
        assert all(s.get("kind") == "product_benefit" for s in sections)

    def test_claim_grounding_skips_health_fallback_on_knowledge_turn(self) -> None:
        state = _state()
        pin_product_knowledge_session(
            state,
            question_kind=ProductKnowledgeKind.COMPARISON,
            subject_product={"title": "عسل سدرة"},
            anchor_message="وش فرق عن السدر العادي؟",
            comparison_reference="السدر العادي",
        )
        reply = "السدر أخف في الطعم ويفيد للمناعة."
        result = apply_product_claim_grounding_guard(
            reply=reply,
            order_state=state,
            inbound_metadata={"decision_topic": TOPIC_PRODUCT_KNOWLEDGE_FACTS},
        )
        assert result.action == "allowed_product_knowledge"
        assert result.replaced is False
        assert "فوائد صحية" not in result.reply


class TestCanonicalProductFactualFollowup:
    _SHOE = {
        "id": 501,
        "external_id": "shoe-white-501",
        "title": "حذاء رياضي أبيض",
        "description": "حذاء رياضي ببطانة شبكية قابلة للتنفس.",
        "price": 249,
    }
    _PERFUME = {
        "id": 502,
        "external_id": "perfume-rose-502",
        "title": "عطر ورد 100ml",
        "description": "عطر ورد مختلف عن الحذاء.",
        "price": 180,
    }

    def _bound_state(self) -> MerchantConversationState:
        state = _state(turn=3)
        set_product_focus(
            state,
            dict(self._SHOE),
            reason="test_catalog_confirmed_product",
            turn=2,
        )
        return state

    def _catalog_facts(self) -> CommerceFacts:
        return CommerceFacts(
            has_products=True,
            product_count=2,
            orderable=True,
            top_products=[dict(self._SHOE), dict(self._PERFUME)],
            discovery_products=[dict(self._SHOE), dict(self._PERFUME)],
        )

    def test_attribute_followup_routes_to_facts_with_product_scoped_kb(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db = _install_kb_stubs(monkeypatch, [
            _StubKBSection(
                section_id=501,
                kind="product_info",
                title="حذاء رياضي أبيض",
                body="الخامة شبكية قابلة للتنفس.",
                product_ids=[501],
            ),
            _StubKBSection(
                section_id=502,
                kind="product_info",
                title="عطر ورد 100ml",
                body="عطر مختلف ولا يخص الحذاء.",
                product_ids=[502],
            ),
        ])
        ctx = _ctx(
            "هل هذا مصنوع من مادة قابلة للتنفس؟",
            state=self._bound_state(),
            db=db,
            facts=self._catalog_facts(),
            intent=Intent(
                name=INTENT_ASK_PRODUCT,
                confidence=0.9,
                raw_message="هل هذا مصنوع من مادة قابلة للتنفس؟",
            ),
        )

        decision = try_product_knowledge_decision(ctx)

        assert decision is not None
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == TOPIC_PRODUCT_KNOWLEDGE_FACTS
        assert decision.args.get("question_kind") == ProductKnowledgeKind.ATTRIBUTE.value
        subject = decision.args.get("subject_product") or {}
        assert subject.get("id") == 501
        allowed = decision.args.get("allowed_facts") or {}
        assert "بطانة شبكية" in str(allowed.get("catalog_description") or "")
        assert [s["section_id"] for s in allowed.get("kb_sections") or []] == [501]

    def test_availability_followup_keeps_availability_owner(self) -> None:
        decision = try_product_knowledge_decision(
            _ctx(
                "هل هذا متوفر؟",
                state=self._bound_state(),
                facts=self._catalog_facts(),
                intent=Intent(
                    name=INTENT_ASK_PRODUCT,
                    confidence=0.9,
                    raw_message="هل هذا متوفر؟",
                ),
            ),
        )
        assert decision is None

    def test_broad_browse_keeps_catalog_browse_owner(self) -> None:
        decision = try_product_knowledge_decision(
            _ctx(
                "وش أنواع الأحذية عندكم؟",
                state=self._bound_state(),
                facts=self._catalog_facts(),
                intent=Intent(
                    name=INTENT_ASK_PRODUCT,
                    confidence=0.9,
                    raw_message="وش أنواع الأحذية عندكم؟",
                ),
            ),
        )
        assert decision is None

    def test_exact_structured_switch_does_not_trap_old_referent(self) -> None:
        ctx = _ctx(
            "هل هذا العطر مناسب للاستخدام اليومي؟",
            state=self._bound_state(),
            facts=self._catalog_facts(),
            intent=Intent(
                name=INTENT_ASK_PRODUCT,
                confidence=0.9,
                slots={"product_query": "عطر ورد 100ml", "product_id": 502},
                raw_message="هل هذا العطر مناسب للاستخدام اليومي؟",
            ),
        )
        assert try_product_knowledge_decision(ctx) is None
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_SEARCH_PRODUCTS
        assert decision.args.get("query") == "عطر ورد 100ml"

    def test_foreign_catalog_evidence_cannot_enrich_focused_product(self) -> None:
        foreign_only_facts = CommerceFacts(
            has_products=True,
            product_count=1,
            orderable=True,
            top_products=[dict(self._PERFUME)],
            discovery_products=[dict(self._PERFUME)],
        )
        decision = try_product_knowledge_decision(
            _ctx(
                "هل هذا مصنوع من مادة قابلة للتنفس؟",
                state=self._bound_state(),
                facts=foreign_only_facts,
                intent=Intent(
                    name=INTENT_ASK_PRODUCT,
                    confidence=0.9,
                    raw_message="هل هذا مصنوع من مادة قابلة للتنفس؟",
                ),
            ),
        )
        assert decision is None

    def test_short_deictic_continuation_has_nonempty_llm_owner(self) -> None:
        decision = try_product_knowledge_decision(
            _ctx(
                "هذا ينفع؟",
                state=self._bound_state(),
                facts=self._catalog_facts(),
                intent=Intent(
                    name=INTENT_ASK_PRODUCT,
                    confidence=0.9,
                    raw_message="هذا ينفع؟",
                ),
            ),
        )
        assert decision is not None
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == TOPIC_PRODUCT_KNOWLEDGE_FACTS


class TestProductKnowledgeFeaturesRouting:
    """Slice 0/1 — Arabic features questions route to CE4 with subject + KB facts."""

    def test_extract_features_subject_smoke_question(self) -> None:
        assert extract_features_subject("ما هي مميزات عسل السدر القيضي؟") == (
            "عسل السدر القيضي"
        )

    def test_extract_features_subject_wesh_variant(self) -> None:
        assert extract_features_subject("وش مميزات عسل السدر القيضي؟") == (
            "عسل السدر القيضي"
        )

    def test_extract_features_subject_khasais(self) -> None:
        subj = extract_features_subject("ما هي خصائص السدر القيضي؟")
        assert "السدر" in subj
        assert "القيضي" in subj

    def test_resolve_subject_without_focus_uses_message(self) -> None:
        ctx = _ctx("ما هي مميزات عسل السدر القيضي؟")
        subject = resolve_subject_product(ctx, ctx.message or "")
        assert subject.get("title_hint_from_message") == "عسل السدر القيضي"

    def test_generic_mumayizat_without_focus_has_no_subject(self) -> None:
        ctx = _ctx("ما هي المميزات؟")
        subject = resolve_subject_product(ctx, ctx.message or "")
        assert not subject.get("title")
        assert not subject.get("title_hint_from_message")

    def test_smoke_question_routes_to_ce4(self) -> None:
        message = "ما هي مميزات عسل السدر القيضي؟"
        ctx = _ctx(message)
        decision = try_product_knowledge_decision(ctx)
        assert decision is not None
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == TOPIC_PRODUCT_KNOWLEDGE_FACTS
        assert decision.args.get("question_kind") == ProductKnowledgeKind.FEATURES.value
        subject = decision.args.get("subject_product") or {}
        hint = str(subject.get("title_hint_from_message") or subject.get("title") or "")
        assert "القيضي" in hint
        assert "السدر" in hint

    def test_smoke_question_engine_decision_not_non_sales_ambiguous(self) -> None:
        message = "ما هي مميزات عسل السدر القيضي؟"
        ctx = _ctx(message)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == TOPIC_PRODUCT_KNOWLEDGE_FACTS
        assert decision.args.get("topic") != "non_sales_ambiguous"

    def test_wesh_mumayizat_still_routes_to_ce4(self) -> None:
        message = "وش مميزات عسل السدر القيضي؟"
        ctx = _ctx(message)
        decision = try_product_knowledge_decision(ctx)
        assert decision is not None
        assert decision.args.get("question_kind") == ProductKnowledgeKind.FEATURES.value

    def test_khasais_routes_to_ce4(self) -> None:
        message = "ما هي خصائص عسل السدر القيضي؟"
        ctx = _ctx(message)
        decision = try_product_knowledge_decision(ctx)
        assert decision is not None
        assert decision.args.get("topic") == TOPIC_PRODUCT_KNOWLEDGE_FACTS
        assert decision.args.get("question_kind") == ProductKnowledgeKind.FEATURES.value

    def test_kb_retrieval_returns_sidr_qaidhi_section(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        kb = _StubKBSection(
            section_id=501,
            kind="product_info",
            title="عسل السدر القيضي",
            body="مميزاته: ندرة القطف، طعم غني، من مصادر جبلية.",
        )
        db = _install_kb_stubs(monkeypatch, [kb])
        message = "ما هي مميزات عسل السدر القيضي؟"
        ctx = _ctx(message, db=db)
        sections = _retrieve_product_kb_sections(
            db,
            33,
            subject="عسل السدر القيضي",
            message=message,
        )
        assert len(sections) == 1
        assert sections[0]["section_id"] == 501
        assert sections[0]["title"] == "عسل السدر القيضي"
        assert sections[0]["match_score"] >= 0.35

        decision = try_product_knowledge_decision(ctx)
        assert decision is not None
        allowed = decision.args.get("allowed_facts") or {}
        kb_sections = allowed.get("kb_sections") or []
        assert len(kb_sections) == 1
        assert kb_sections[0]["section_id"] == 501
        assert kb_sections[0]["title"] == "عسل السدر القيضي"
        assert kb_sections[0]["body"]
        assert kb_sections[0]["kind"] == "product_info"
        assert kb_sections[0]["match_score"] >= 0.35
