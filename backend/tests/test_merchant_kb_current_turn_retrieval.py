"""LIVE-MERCHANT-KB-CURRENT-TURN-RETRIEVAL-D1 — Tests A–L.

Generic commerce fixtures only. No tenant-33, section-243, or honey hardcode.
"""
from __future__ import annotations

import asyncio
import inspect
import os
import sys
from types import SimpleNamespace
from typing import Any, List, Optional
from unittest.mock import AsyncMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
for _p in (_ROOT, _BACKEND, os.path.join(_ROOT, "database")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.commerce.commerce_focus_owner import set_product_focus  # noqa: E402
from modules.ai.brain.commerce.product_knowledge_or_comparison import (  # noqa: E402
    TOPIC_PRODUCT_KNOWLEDGE_FACTS,
    ProductKnowledgeKind,
    _combine_kb_relevance,
    _KB_RELEVANCE_THRESHOLD,
    _KB_SECTION_BODY_LIMIT,
    _KB_SECTION_RESULT_LIMIT,
    _score_kb_section,
    retrieve_catalog_candidate_kb_sections,
    try_product_knowledge_decision,
)
from modules.ai.brain.compose.responder import (  # noqa: E402
    DefaultComposer,
    attach_catalog_candidate_kb_to_decision_args,
    _catalog_candidate_ids_and_subject,
)
from modules.ai.brain.decision.actions import ACTION_LLM_REPLY, ACTION_SEARCH_PRODUCTS  # noqa: E402
from modules.ai.brain.persona.catalog_product_answer import (  # noqa: E402
    build_catalog_product_answer_emergency_outcome,
    build_catalog_product_answer_event_metadata,
    build_catalog_product_answer_facts_bundle,
)
from modules.ai.brain.persona.facts_bundle import PersonaComposeResult  # noqa: E402
from modules.ai.brain.persona.prompts import build_user_prompt  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    ActionResult,
    BrainContext,
    CommerceFacts,
    Decision,
    INTENT_ASK_PRODUCT,
    Intent,
    MerchantConversationState,
)

_TENANT_A = 9001
_TENANT_B = 9002
_ATTR_QUESTION = "هل هذا مصنوع من مادة قابلة للتنفس؟"
_BROWSE_QUESTION = "وش عندكم؟"
_SHOE_42 = {
    "id": 801,
    "title": "حذاء رياضي أبيض مقاس 42",
    "price": 249,
    "can_checkout": True,
}
_SHOE_43 = {
    "id": 802,
    "title": "حذاء رياضي أبيض مقاس 43",
    "price": 249,
    "can_checkout": True,
}
_SHIRT = {
    "id": 803,
    "title": "قميص قطني أزرق",
    "price": 95,
    "can_checkout": True,
}
_GLOBAL_MATERIAL = {
    "section_id": 8011,
    "kind": "custom",
    "title": "خامة المنتجات الرياضية المعتمدة",
    "body": "منتجات هذا المتجر التجريبية تُصنع من خامة شبكية قابلة للتنفس.",
}
_LINKED_SHOE = {
    "section_id": 8012,
    "kind": "product_info",
    "title": "بطانة الحذاء الرياضي الأبيض",
    "body": "البطانة الداخلية شبكية قابلة للتنفس ومناسبة للاستخدام اليومي.",
    "product_ids": [801],
}
_LINKED_SHIRT = {
    "section_id": 8013,
    "kind": "product_info",
    "title": "خامة القميص القطني",
    "body": "القميص مصنوع من قطن ممشط ولا يخص الحذاء الرياضي.",
    "product_ids": [803],
}
_IRRELEVANT_STYLE = {
    "section_id": 8099,
    "kind": "custom",
    "title": "مكتبة إيموجيات الترحيب التسويقية",
    "body": "استخدم رموز الترحيب بلطف في رسائل الافتتاح فقط دون ذكر الخامات.",
}
_VARIANT_ID_COLLISION_PRODUCT = 9001
_LINKED_VARIANT_COLLISION = {
    "section_id": 8014,
    "kind": "product_info",
    "title": "خامة الحقيبة الشبكية القابلة للتنفس",
    "body": "حقيبة السفر التجريبية تُصنع من خامة شبكية قابلة للتنفس.",
    "product_ids": [_VARIANT_ID_COLLISION_PRODUCT],
}
_OPERATIONAL_KB_FLAGS = (
    "kb_retrieval_attempted",
    "kb_retrieval_succeeded",
    "kb_retrieval_ran",
    "kb_retrieval_failed",
    "kb_fact_absent",
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
    def __init__(
        self,
        *,
        section_id: int,
        title: str,
        body: str,
        kind: str = "custom",
        tenant_id: int = _TENANT_A,
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
        self.tenant_id = tenant_id
        self.product_links = [
            SimpleNamespace(product_id=pid) for pid in (product_ids or [])
        ]


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


def _install_kb_stubs(
    monkeypatch: pytest.MonkeyPatch,
    sections: List[_StubKBSection],
) -> _StubDB:
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


def _section(**spec: Any) -> _StubKBSection:
    return _StubKBSection(
        section_id=int(spec["section_id"]),
        title=str(spec["title"]),
        body=str(spec["body"]),
        kind=str(spec.get("kind") or "custom"),
        tenant_id=int(spec.get("tenant_id") or _TENANT_A),
        product_ids=list(spec.get("product_ids") or []),
    )


def _payload(db: Any, *, message: str, products: List[dict], tenant_id: int = _TENANT_A) -> dict:
    return retrieve_catalog_candidate_kb_sections(
        db,
        tenant_id,
        subject=" ".join(str(p.get("title") or "") for p in products),
        message=message,
        product_ids=[p.get("id") for p in products],
    )


def _bundle_from_payload(payload: dict, *, products: List[dict], inbound: str):
    return build_catalog_product_answer_facts_bundle(
        inbound_text=inbound,
        tenant_id=_TENANT_A,
        customer_phone="966500000001",
        products=products,
        catalog_search_query="حذاء",
        search_result_count=len(products),
        question_kind="browse",
        decision_args=dict(payload),
    )


class TestAGlobalKbWithCatalog:
    def test_global_fact_reaches_verified_facts(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db = _install_kb_stubs(monkeypatch, [_section(**_GLOBAL_MATERIAL)])
        payload = _payload(db, message=_ATTR_QUESTION, products=[_SHOE_42, _SHOE_43])
        assert payload["kb_retrieval_attempted"] is True
        assert payload["kb_retrieval_succeeded"] is True
        assert payload["kb_retrieval_ran"] is True
        assert payload["kb_retrieval_failed"] is False
        assert payload["kb_fact_absent"] is False
        assert payload["has_kb_sections"] is True
        assert payload["knowledge_source"] == "tenant_knowledge_base"
        assert 8011 in payload["kb_section_ids"]
        bundle = _bundle_from_payload(
            payload, products=[_SHOE_42, _SHOE_43], inbound=_ATTR_QUESTION,
        )
        facts = bundle.verified_facts
        assert facts["kb_retrieval_ran"] is True
        assert facts["has_kb_sections"] is True
        assert 8011 in facts["kb_section_ids"]
        prompt = build_user_prompt(bundle)
        assert "kb_section:" in prompt
        assert "قابلة للتنفس" in prompt
        assert "use only supplied catalog products and supplied authorized kb_sections" in prompt


class TestBProductLinkedKb:
    def test_linked_section_requires_candidate_intersection(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db = _install_kb_stubs(
            monkeypatch,
            [_section(**_LINKED_SHOE), _section(**_LINKED_SHIRT)],
        )
        shoe_payload = _payload(db, message=_ATTR_QUESTION, products=[_SHOE_42])
        assert 8012 in shoe_payload["kb_section_ids"]
        assert 8013 not in shoe_payload["kb_section_ids"]
        shirt_payload = _payload(
            db,
            message="هل هذا القميص من قطن ممشط؟",
            products=[_SHIRT],
        )
        assert 8013 in shirt_payload["kb_section_ids"]
        assert 8012 not in shirt_payload["kb_section_ids"]


class TestCIrrelevantKbExcluded:
    def test_unrelated_style_section_stays_out(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db = _install_kb_stubs(
            monkeypatch,
            [_section(**_GLOBAL_MATERIAL), _section(**_IRRELEVANT_STYLE)],
        )
        payload = _payload(db, message=_ATTR_QUESTION, products=[_SHOE_42])
        assert 8011 in payload["kb_section_ids"]
        assert 8099 not in payload["kb_section_ids"]
        prompt = build_user_prompt(
            _bundle_from_payload(payload, products=[_SHOE_42], inbound=_ATTR_QUESTION),
        )
        assert "إيموجيات" not in prompt


class TestDTenantIsolation:
    def test_other_tenant_global_section_excluded(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        foreign = dict(_GLOBAL_MATERIAL)
        foreign["section_id"] = 9011
        foreign["tenant_id"] = _TENANT_B
        db = _install_kb_stubs(
            monkeypatch,
            [_section(**_GLOBAL_MATERIAL), _section(**foreign)],
        )
        payload = _payload(db, message=_ATTR_QUESTION, products=[_SHOE_42], tenant_id=_TENANT_A)
        assert payload["kb_section_ids"] == [8011]
        assert 9011 not in payload["kb_section_ids"]


class TestECatalogBehaviorPreserved:
    def test_genuine_browse_stays_search_products(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db = _install_kb_stubs(monkeypatch, [_section(**_GLOBAL_MATERIAL)])
        calls = {"n": 0}

        def _count(*args: Any, **kwargs: Any) -> dict:
            calls["n"] += 1
            return retrieve_catalog_candidate_kb_sections(*args, **kwargs)

        ctx = BrainContext(
            tenant_id=_TENANT_A,
            customer_phone="966500000001",
            message=_BROWSE_QUESTION,
            intent=Intent(name=INTENT_ASK_PRODUCT, confidence=0.9, raw_message=_BROWSE_QUESTION),
            state=MerchantConversationState(greeted=True),
            facts=CommerceFacts(has_products=True, product_count=2, orderable=True),
        )
        ctx._db = db  # type: ignore[attr-defined]
        with patch(
            "modules.ai.brain.commerce.product_knowledge_or_comparison.retrieve_catalog_candidate_kb_sections",
            side_effect=_count,
        ):
            merged = attach_catalog_candidate_kb_to_decision_args(
                ctx,
                compose_products=[_SHOE_42, _SHOE_43],
                decision_args={"query": "حذاء"},
            )
        assert merged["kb_retrieval_ran"] is True
        assert ACTION_SEARCH_PRODUCTS == "search_products"
        assert merged.get("topic") != TOPIC_PRODUCT_KNOWLEDGE_FACTS
        bundle = _bundle_from_payload(
            merged, products=[_SHOE_42, _SHOE_43], inbound=_BROWSE_QUESTION,
        )
        assert bundle.verified_facts["has_catalog_products"] is True
        assert [row["id"] for row in bundle.verified_facts["catalog_products"]] == [801, 802]
        prompt = build_user_prompt(bundle)
        if not bundle.verified_facts.get("has_kb_sections"):
            assert "and supplied authorized kb_sections" not in prompt


class TestFFactAbsence:
    def test_uncertainty_only_after_authorized_empty_retrieval(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db = _install_kb_stubs(monkeypatch, [_section(**_IRRELEVANT_STYLE)])
        payload = _payload(db, message=_ATTR_QUESTION, products=[_SHOE_42])
        assert payload["kb_retrieval_attempted"] is True
        assert payload["kb_retrieval_succeeded"] is True
        assert payload["kb_retrieval_ran"] is True
        assert payload["kb_retrieval_failed"] is False
        assert payload["kb_fact_absent"] is True
        assert payload["knowledge_source"] == "missing_kb"
        assert payload["kb_section_ids"] == []
        never_ran = build_catalog_product_answer_facts_bundle(
            inbound_text=_ATTR_QUESTION,
            tenant_id=_TENANT_A,
            products=[_SHOE_42],
            question_kind="browse",
            decision_args={},
        ).verified_facts
        assert "kb_retrieval_ran" not in never_ran
        assert never_ran.get("knowledge_source") is None


class TestGGlobalFactWithMultipleCandidates:
    def test_global_fact_available_for_two_variants(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db = _install_kb_stubs(monkeypatch, [_section(**_GLOBAL_MATERIAL)])
        payload = _payload(db, message=_ATTR_QUESTION, products=[_SHOE_42, _SHOE_43])
        assert 8011 in payload["kb_section_ids"]
        assert payload["has_kb_sections"] is True


class TestHModelFreedom:
    def test_asserts_facts_not_arabic_wording(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db = _install_kb_stubs(monkeypatch, [_section(**_GLOBAL_MATERIAL)])
        payload = _payload(db, message=_ATTR_QUESTION, products=[_SHOE_42])
        facts = _bundle_from_payload(
            payload, products=[_SHOE_42], inbound=_ATTR_QUESTION,
        ).verified_facts
        assert facts["knowledge_source"] == "tenant_knowledge_base"
        assert facts["kb_section_ids"]
        assert facts["has_kb_sections"] is True


class TestICurrentScorer:
    def test_question_relevance_includes_without_threshold_change(self) -> None:
        title = _GLOBAL_MATERIAL["title"]
        body = _GLOBAL_MATERIAL["body"]
        subject_score = _score_kb_section(
            title=title, body=body, subject=_SHOE_42["title"],
        )
        question_score = _score_kb_section(
            title=title, body=body, subject=_ATTR_QUESTION,
        )
        combined = _combine_kb_relevance(
            subject_relevance=subject_score,
            question_relevance=question_score,
        )
        assert _KB_RELEVANCE_THRESHOLD == 0.35
        assert combined == max(subject_score, question_score)
        assert question_score >= _KB_RELEVANCE_THRESHOLD
        assert combined >= _KB_RELEVANCE_THRESHOLD


class TestJExistingSingleProductPath:
    def test_bound_referent_knowledge_path_unchanged(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db = _install_kb_stubs(monkeypatch, [_section(**_LINKED_SHOE)])
        state = MerchantConversationState(greeted=True, turn=3)
        set_product_focus(
            state,
            {
                "id": 801,
                "external_id": "shoe-white-801",
                "title": "حذاء رياضي أبيض",
                "description": "حذاء رياضي ببطانة شبكية قابلة للتنفس.",
                "price": 249,
            },
            reason="test_catalog_confirmed_product",
            turn=2,
        )
        ctx = BrainContext(
            tenant_id=_TENANT_A,
            customer_phone="966500000001",
            message=_ATTR_QUESTION,
            intent=Intent(
                name=INTENT_ASK_PRODUCT,
                confidence=0.9,
                raw_message=_ATTR_QUESTION,
            ),
            state=state,
            facts=CommerceFacts(
                has_products=True,
                product_count=1,
                orderable=True,
                top_products=[{"id": 801, "title": "حذاء رياضي أبيض"}],
            ),
        )
        ctx._db = db  # type: ignore[attr-defined]
        decision = try_product_knowledge_decision(ctx)
        assert decision is not None
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == TOPIC_PRODUCT_KNOWLEDGE_FACTS
        assert decision.args.get("question_kind") == ProductKnowledgeKind.ATTRIBUTE.value
        allowed = decision.args.get("allowed_facts") or {}
        assert [row["section_id"] for row in allowed.get("kb_sections") or []] == [8012]


class TestKRetrievalCalledOnce:
    def test_attach_and_compose_retrieve_once(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db = _install_kb_stubs(monkeypatch, [_section(**_GLOBAL_MATERIAL)])
        calls: list[tuple] = []
        real = retrieve_catalog_candidate_kb_sections

        def _spy(*args: Any, **kwargs: Any) -> dict:
            calls.append((args, kwargs))
            return real(*args, **kwargs)

        ctx = BrainContext(
            tenant_id=_TENANT_A,
            customer_phone="966500000001",
            message=_ATTR_QUESTION,
            intent=Intent(name=INTENT_ASK_PRODUCT, confidence=0.9, raw_message=_ATTR_QUESTION),
            state=MerchantConversationState(greeted=True),
            facts=CommerceFacts(has_products=True, product_count=2, orderable=True),
        )
        ctx._db = db  # type: ignore[attr-defined]
        dupes = [_SHOE_42, dict(_SHOE_42), _SHOE_43]
        with patch(
            "modules.ai.brain.commerce.product_knowledge_or_comparison.retrieve_catalog_candidate_kb_sections",
            side_effect=_spy,
        ):
            merged = attach_catalog_candidate_kb_to_decision_args(
                ctx,
                compose_products=dupes,
                decision_args={"query": "حذاء"},
            )
        assert len(calls) == 1
        sent_ids = list(calls[0][1].get("product_ids") or [])
        assert sent_ids == [801, 802]
        assert merged["kb_section_ids"].count(8011) == 1

        compose_calls: list[int] = []

        async def _fake_compose(**kwargs: Any):
            compose_calls.append(1)
            args = dict(kwargs.get("decision_args") or {})
            event = {
                "compose_source": "persona_llm",
                "kb_section_ids": list(args.get("kb_section_ids") or []),
                "kb_retrieval_ran": True,
            }
            result = SimpleNamespace(source="persona_llm", text="الخامة شبكية حسب الوقائع.", guard_passed=True)
            return "الخامة شبكية حسب الوقائع.", result, event

        async def _run() -> str:
            with patch(
                "modules.ai.brain.persona.catalog_product_answer.try_compose_catalog_product_answer",
                new=AsyncMock(side_effect=_fake_compose),
            ), patch(
                "modules.ai.brain.commerce.product_knowledge_or_comparison.retrieve_catalog_candidate_kb_sections",
                side_effect=_spy,
            ):
                composer = DefaultComposer()
                decision = Decision(
                    action=ACTION_SEARCH_PRODUCTS,
                    args={"query": "حذاء"},
                    reason="test",
                )
                result = ActionResult(
                    success=True,
                    data={"products": [_SHOE_42, _SHOE_43], "query": "حذاء"},
                )
                return await composer._compose_impl(decision, result, ctx)

        asyncio.run(_run())
        assert len(compose_calls) == 1
        assert len(calls) == 2  # attach unit + one live compose path


class TestLNoDbInPersona:
    def test_catalog_persona_consumes_precomputed_facts_only(self) -> None:
        src = inspect.getsource(
            sys.modules["modules.ai.brain.persona.catalog_product_answer"],
        )
        assert "MerchantKnowledgeSection" not in src
        assert "retrieve_catalog_candidate_kb_sections" not in src
        assert "apply_ai_visible_kb_query_filters" not in src

    def test_emergency_fallback_keeps_safe_provenance(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db = _install_kb_stubs(monkeypatch, [_section(**_GLOBAL_MATERIAL)])
        payload = _payload(db, message=_ATTR_QUESTION, products=[_SHOE_42])
        text, result, event = build_catalog_product_answer_emergency_outcome(
            tenant_id=_TENANT_A,
            customer_phone="966500000001",
            inbound_text=_ATTR_QUESTION,
            products=[_SHOE_42],
            catalog_search_query="حذاء",
            question_kind="browse",
            decision_args=payload,
            reason="compose_unavailable",
        )
        assert result.source == "fallback_deterministic"
        assert event["kb_retrieval_ran"] is True
        assert event["kb_section_ids"] == [8011]
        assert "kb_sections" not in event
        assert _GLOBAL_MATERIAL["body"] not in (text or "")
        assert "إيموجيات" not in str(event)


class TestBoundsAndDump:
    def test_section_count_and_body_limit(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        rows = []
        for idx in range(8):
            rows.append(
                _section(
                    section_id=8100 + idx,
                    kind="custom",
                    title=f"خامة معتمدة {idx} قابلة للتنفس",
                    body=("خامة شبكية قابلة للتنفس. " * 80),
                ),
            )
        db = _install_kb_stubs(monkeypatch, rows)
        payload = _payload(db, message=_ATTR_QUESTION, products=[_SHOE_42])
        assert len(payload["kb_sections"]) <= _KB_SECTION_RESULT_LIMIT
        assert all(
            len(str(row.get("body") or "")) <= _KB_SECTION_BODY_LIMIT
            for row in payload["kb_sections"]
        )

    def test_event_metadata_omits_section_bodies(self) -> None:
        facts = {
            "question_kind": "browse",
            "kb_retrieval_ran": True,
            "kb_fact_absent": False,
            "has_kb_sections": True,
            "kb_section_ids": [8011],
            "knowledge_source": "tenant_knowledge_base",
            "kb_sections": [
                {
                    "section_id": 8011,
                    "title": _GLOBAL_MATERIAL["title"],
                    "body": _GLOBAL_MATERIAL["body"],
                },
            ],
        }
        event = build_catalog_product_answer_event_metadata(
            PersonaComposeResult(
                text="ok",
                source="persona_llm",
                surface="catalog_product_answer",
                facts_hash="x",
                guard_passed=True,
            ),
            tenant_id=_TENANT_A,
            allowlist_result="allowed",
            catalog_facts=facts,
        )
        assert event["kb_section_ids"] == [8011]
        assert "kb_sections" not in event
        dumped = str(event)
        assert _GLOBAL_MATERIAL["body"] not in dumped


def _assert_operational_failure(payload: dict) -> None:
    assert payload["kb_retrieval_attempted"] is True
    assert payload["kb_retrieval_succeeded"] is False
    assert payload["kb_retrieval_ran"] is False
    assert payload["kb_retrieval_failed"] is True
    assert payload["kb_fact_absent"] is False
    assert payload.get("knowledge_source") != "missing_kb"
    assert "knowledge_source" not in payload or not payload.get("knowledge_source")
    assert payload["kb_section_ids"] == []
    assert payload["has_kb_sections"] is False


class TestMFailureIsNotAbsence:
    def test_missing_db_is_failure_not_absence(self) -> None:
        payload = retrieve_catalog_candidate_kb_sections(
            None,
            _TENANT_A,
            subject=_SHOE_42["title"],
            message=_ATTR_QUESTION,
            product_ids=[801],
        )
        _assert_operational_failure(payload)

    def test_query_exception_is_failure_not_absence(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_kb_stubs(monkeypatch, [_section(**_GLOBAL_MATERIAL)])

        class _BoomDB:
            def query(self, model: Any) -> Any:
                raise RuntimeError("kb query unavailable")

        payload = retrieve_catalog_candidate_kb_sections(
            _BoomDB(),
            _TENANT_A,
            subject=_SHOE_42["title"],
            message=_ATTR_QUESTION,
            product_ids=[801],
        )
        _assert_operational_failure(payload)

    def test_wrapper_exception_is_failure_not_absence(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db = _install_kb_stubs(monkeypatch, [_section(**_GLOBAL_MATERIAL)])
        ctx = BrainContext(
            tenant_id=_TENANT_A,
            customer_phone="966500000001",
            message=_ATTR_QUESTION,
            intent=Intent(name=INTENT_ASK_PRODUCT, confidence=0.9, raw_message=_ATTR_QUESTION),
            state=MerchantConversationState(greeted=True),
            facts=CommerceFacts(has_products=True, product_count=1, orderable=True),
        )
        ctx._db = db  # type: ignore[attr-defined]
        with patch(
            "modules.ai.brain.commerce.product_knowledge_or_comparison.retrieve_catalog_candidate_kb_sections",
            side_effect=RuntimeError("catalog kb wrapper failed"),
        ):
            merged = attach_catalog_candidate_kb_to_decision_args(
                ctx,
                compose_products=[_SHOE_42],
                decision_args={"query": "حذاء"},
            )
        _assert_operational_failure(merged)
        facts = _bundle_from_payload(
            merged, products=[_SHOE_42], inbound=_ATTR_QUESTION,
        ).verified_facts
        assert facts["kb_retrieval_failed"] is True
        assert facts["kb_fact_absent"] is False
        assert facts.get("knowledge_source") != "missing_kb"
        text, result, event = build_catalog_product_answer_emergency_outcome(
            tenant_id=_TENANT_A,
            customer_phone="966500000001",
            inbound_text=_ATTR_QUESTION,
            products=[_SHOE_42],
            catalog_search_query="حذاء",
            question_kind="browse",
            decision_args=merged,
            reason="compose_unavailable",
        )
        assert result.source == "fallback_deterministic"
        assert event["kb_retrieval_failed"] is True
        assert event["kb_fact_absent"] is False
        assert event.get("knowledge_source") != "missing_kb"
        assert _GLOBAL_MATERIAL["body"] not in (text or "")

    def test_successful_empty_and_successful_facts(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        empty_db = _install_kb_stubs(monkeypatch, [_section(**_IRRELEVANT_STYLE)])
        empty = _payload(empty_db, message=_ATTR_QUESTION, products=[_SHOE_42])
        assert empty["kb_retrieval_succeeded"] is True
        assert empty["kb_retrieval_failed"] is False
        assert empty["kb_fact_absent"] is True
        assert empty["knowledge_source"] == "missing_kb"
        facts_db = _install_kb_stubs(monkeypatch, [_section(**_GLOBAL_MATERIAL)])
        found = _payload(facts_db, message=_ATTR_QUESTION, products=[_SHOE_42])
        assert found["kb_retrieval_succeeded"] is True
        assert found["kb_retrieval_failed"] is False
        assert found["kb_fact_absent"] is False
        assert found["knowledge_source"] == "tenant_knowledge_base"


class TestNOperationalFlagsNotInModelPrompt:
    def test_flag_names_absent_on_empty_and_failed(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        empty_db = _install_kb_stubs(monkeypatch, [_section(**_IRRELEVANT_STYLE)])
        empty_prompt = build_user_prompt(
            _bundle_from_payload(
                _payload(empty_db, message=_ATTR_QUESTION, products=[_SHOE_42]),
                products=[_SHOE_42],
                inbound=_ATTR_QUESTION,
            ),
        )
        failed = retrieve_catalog_candidate_kb_sections(
            None,
            _TENANT_A,
            subject=_SHOE_42["title"],
            message=_ATTR_QUESTION,
            product_ids=[801],
        )
        failed_prompt = build_user_prompt(
            _bundle_from_payload(failed, products=[_SHOE_42], inbound=_ATTR_QUESTION),
        )
        for prompt in (empty_prompt, failed_prompt):
            for flag in _OPERATIONAL_KB_FLAGS:
                assert flag not in prompt
        facts_db = _install_kb_stubs(monkeypatch, [_section(**_GLOBAL_MATERIAL)])
        facts_prompt = build_user_prompt(
            _bundle_from_payload(
                _payload(facts_db, message=_ATTR_QUESTION, products=[_SHOE_42]),
                products=[_SHOE_42],
                inbound=_ATTR_QUESTION,
            ),
        )
        assert "kb_section:" in facts_prompt
        for flag in _OPERATIONAL_KB_FLAGS:
            assert flag not in facts_prompt


class TestOCanonicalProductVsVariantLinkIdentity:
    def test_parent_product_link_not_variant_id(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db = _install_kb_stubs(
            monkeypatch,
            [_section(**_LINKED_SHOE), _section(**_LINKED_VARIANT_COLLISION)],
        )
        candidate = {
            "id": 801,
            "product_id": 801,
            "variant_id": _VARIANT_ID_COLLISION_PRODUCT,
            "title": _SHOE_42["title"],
            "price": 249,
            "can_checkout": True,
        }
        ids, _subject = _catalog_candidate_ids_and_subject([candidate])
        assert ids == [801]
        confused = {
            "id": _VARIANT_ID_COLLISION_PRODUCT,
            "product_id": 801,
            "variant_id": _VARIANT_ID_COLLISION_PRODUCT,
            "title": _SHOE_42["title"],
            "price": 249,
            "can_checkout": True,
        }
        confused_ids, _ = _catalog_candidate_ids_and_subject([confused])
        assert confused_ids == [801]
        payload = retrieve_catalog_candidate_kb_sections(
            db,
            _TENANT_A,
            subject=_SHOE_42["title"],
            message=_ATTR_QUESTION,
            product_ids=ids,
        )
        assert 8012 in payload["kb_section_ids"]
        assert 8014 not in payload["kb_section_ids"]
        simple = _payload(db, message=_ATTR_QUESTION, products=[_SHOE_42])
        assert 8012 in simple["kb_section_ids"]
        assert 8014 not in simple["kb_section_ids"]
        simple_ids, _ = _catalog_candidate_ids_and_subject([_SHOE_42])
        assert simple_ids == [801]
