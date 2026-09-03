"""LIVE-T33-BALADI-BEE-KNOWLEDGE-D1B — product-KB current-question relevance.

Asserts retrieval/state, not exact customer-facing prose.
Phrases below are fixture evidence only — not runtime rules.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, List, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")):
    if p not in sys.path:
        sys.path.insert(0, p)

from core.knowledge import kb_row_is_ai_visible  # noqa: E402
from modules.ai.brain.commerce.product_knowledge_or_comparison import (  # noqa: E402
    ProductKnowledgeKind,
    _PRODUCT_KB_KINDS,
    _retrieve_product_kb_sections,
    gather_product_knowledge_facts,
)
from modules.ai.brain.persona.kb_product_answer import (  # noqa: E402
    build_kb_product_answer_facts_bundle,
)
from modules.ai.brain.persona.prompts import build_user_prompt  # noqa: E402

TENANT_A = 33
TENANT_B = 99
PRODUCT_A = 154
PRODUCT_B = 9001

FLORAL_TITLE = "1 كيلو العسل الصيفي أزهار جبلية من جنوب الطائف"
SHOE_TITLE = "حذاء رياضي أبيض مقاس 42"
LINEAGE_TITLE = "سلالة النحل المنتجة لعسل مناحل آل عايد"
LINEAGE_BODY = (
    "العسل المنتج من مناحل آل عايد ينتجه نحل بلدي "
    "من سلالة آل عايد البلدية الأصيلة."
)
LINING_TITLE = "بطانة الحذاء الرياضي"
LINING_BODY = "الحذاء الرياضي الأبيض يحتوي على بطانة جلدية داخلية."
NECTAR_TITLE = "مميزات العسل الصيفي من جنوب الطائف"
NECTAR_BODY = (
    "عسل الصيفي من جنوب الطائف يتميز بجودة عالية. "
    "مصدر الرحيق أزهار جبلية صيفية متنوعة."
)
UNRELATED_TITLE = "مواقف المعرض"
UNRELATED_BODY = "مواقف السيارات مجانية للعملاء أمام المعرض خلال ساعات العمل."
PACKAGE_BODY = (
    "الطرود تكون خلايا حديثة تحتوي على براويز مملوءة بالنحل "
    "مع ملكة حديثة من سلالة آل عايد البلدية الأصيلة."
)
STYLE_BODY = (
    "حالياً الطرود غير متوفرة بشكل دائم. "
    "والطرود تكون خلايا حديثة مع ملكة من سلالة آل عايد البلدية الأصيلة."
)
BEE_QUESTION = "هل العسل من نحل بلدي"
LINING_QUESTION = "هل فيه بطانة جلدية؟"
SHORT_FEATURES_QUESTION = "المميزات؟"
HEALTH_QUESTION = "وش فوائده الصحية"


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
    def __init__(self, kb_sections: List[Any]) -> None:
        self._kb_sections = kb_sections
        self.visibility_filter_calls = 0

    def query(self, model: Any) -> _QueryStub:
        name = getattr(model, "__name__", str(model))
        if name == "MerchantKnowledgeSection":
            return _QueryStub(self._kb_sections)
        return _QueryStub([])


class _StubKBSection:
    def __init__(
        self,
        *,
        section_id: int,
        title: str,
        body: str,
        kind: str = "custom",
        product_ids: Optional[List[int]] = None,
        tenant_id: int = TENANT_A,
        is_active: bool = True,
        ai_status: str = "approved",
        deleted_at: Any = None,
    ) -> None:
        self.id = section_id
        self.kind = kind
        self.title = title
        self.body = body
        self.priority = 10
        self.updated_at = None
        self.is_active = is_active
        self.ai_status = ai_status
        self.deleted_at = deleted_at
        self.tenant_id = tenant_id
        self.product_links = [
            SimpleNamespace(product_id=pid) for pid in (product_ids or [])
        ]


def _install_kb_stubs(
    monkeypatch: pytest.MonkeyPatch,
    sections: List[_StubKBSection],
) -> _StubDB:
    db = _StubDB(sections)
    models_stub = ModuleType("models")
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

    def _apply_visible(query: Any) -> Any:
        db.visibility_filter_calls += 1
        return query

    knowledge_stub = ModuleType("core.knowledge")
    knowledge_stub.apply_ai_visible_kb_query_filters = _apply_visible  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "core.knowledge", knowledge_stub)
    return db


def _ids(sections: List[dict[str, Any]]) -> list[int]:
    return [int(s["section_id"]) for s in sections]


def test_kinds_filter_not_widened() -> None:
    assert "custom" in _PRODUCT_KB_KINDS
    assert "shipping_zones" not in _PRODUCT_KB_KINDS
    assert "reply_style" not in _PRODUCT_KB_KINDS


def test_current_question_retrieves_unlinked_custom_fact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lineage = _StubKBSection(
        section_id=243,
        kind="custom",
        title=LINEAGE_TITLE,
        body=LINEAGE_BODY,
    )
    db = _install_kb_stubs(monkeypatch, [lineage])
    assert kb_row_is_ai_visible(lineage) is True
    sections = _retrieve_product_kb_sections(
        db,
        TENANT_A,
        subject=FLORAL_TITLE,
        message=BEE_QUESTION,
        product_id=PRODUCT_A,
    )
    assert db.visibility_filter_calls == 1
    assert _ids(sections) == [243]
    assert sections[0]["question_score"] >= 0.35
    assert sections[0]["subject_score"] < 0.35


def test_generic_commerce_question_retrieves_unlinked_custom_fact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lining = _StubKBSection(
        section_id=701,
        kind="custom",
        title=LINING_TITLE,
        body=LINING_BODY,
        tenant_id=TENANT_A,
    )
    db = _install_kb_stubs(monkeypatch, [lining])
    sections = _retrieve_product_kb_sections(
        db,
        TENANT_A,
        subject=SHOE_TITLE,
        message=LINING_QUESTION,
        product_id=8801,
    )
    assert _ids(sections) == [701]


def test_subject_title_retrieval_preserved_for_short_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nectar = _StubKBSection(
        section_id=212,
        kind="product_benefit",
        title=NECTAR_TITLE,
        body=NECTAR_BODY,
        product_ids=[PRODUCT_A],
    )
    db = _install_kb_stubs(monkeypatch, [nectar])
    sections = _retrieve_product_kb_sections(
        db,
        TENANT_A,
        subject=FLORAL_TITLE,
        message=SHORT_FEATURES_QUESTION,
        product_id=PRODUCT_A,
    )
    assert _ids(sections) == [212]
    assert sections[0]["subject_score"] >= 0.35


def test_mismatched_product_link_excluded_despite_question_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    linked_other = _StubKBSection(
        section_id=702,
        kind="custom",
        title=LINING_TITLE,
        body=LINING_BODY,
        product_ids=[PRODUCT_B],
    )
    db = _install_kb_stubs(monkeypatch, [linked_other])
    sections = _retrieve_product_kb_sections(
        db,
        TENANT_A,
        subject=SHOE_TITLE,
        message=LINING_QUESTION,
        product_id=8801,
    )
    assert sections == []


def test_unrelated_global_custom_not_retrieved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unrelated = _StubKBSection(
        section_id=703,
        kind="custom",
        title=UNRELATED_TITLE,
        body=UNRELATED_BODY,
    )
    db = _install_kb_stubs(monkeypatch, [unrelated])
    sections = _retrieve_product_kb_sections(
        db,
        TENANT_A,
        subject=FLORAL_TITLE,
        message=BEE_QUESTION,
        product_id=PRODUCT_A,
    )
    assert sections == []


def test_wrong_kind_shipping_zones_excluded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row_84 = _StubKBSection(
        section_id=84,
        kind="shipping_zones",
        title="طرود النحل",
        body=PACKAGE_BODY,
    )
    db = _install_kb_stubs(monkeypatch, [row_84])
    sections = _retrieve_product_kb_sections(
        db,
        TENANT_A,
        subject=FLORAL_TITLE,
        message=BEE_QUESTION,
        product_id=PRODUCT_A,
    )
    assert _ids(sections) == []


def test_wrong_kind_reply_style_excluded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row_85 = _StubKBSection(
        section_id=85,
        kind="reply_style",
        title="فالرد يكون بأسلوب طبيعي مثل",
        body=STYLE_BODY,
    )
    db = _install_kb_stubs(monkeypatch, [row_85])
    sections = _retrieve_product_kb_sections(
        db,
        TENANT_A,
        subject=FLORAL_TITLE,
        message=BEE_QUESTION,
        product_id=PRODUCT_A,
    )
    assert _ids(sections) == []


def test_health_path_keeps_product_benefit_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    custom = _StubKBSection(
        section_id=243,
        kind="custom",
        title=LINEAGE_TITLE,
        body=LINEAGE_BODY,
    )
    benefit = _StubKBSection(
        section_id=212,
        kind="product_benefit",
        title="فوائد عسل الصيفي من جنوب الطائف",
        body=(
            "فوائده الصحية من رحيق أزهار جبلية صيفية متنوعة "
            "من جنوب الطائف ضمن الغذاء اليومي."
        ),
        product_ids=[PRODUCT_A],
    )
    db = _install_kb_stubs(monkeypatch, [custom, benefit])
    ctx = SimpleNamespace(_db=db, tenant_id=TENANT_A)
    bundle = gather_product_knowledge_facts(
        ctx,
        subject_product={"id": PRODUCT_A, "title": FLORAL_TITLE},
        question_kind=ProductKnowledgeKind.HEALTH,
        message=HEALTH_QUESTION,
    )
    ids = _ids(list(bundle.allowed_facts.get("kb_sections") or []))
    assert 212 in ids
    assert 243 not in ids


def test_strong_question_fact_survives_result_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    title_rows = [
        _StubKBSection(
            section_id=800 + i,
            kind="product_info",
            title=f"{FLORAL_TITLE} وصف {i}",
            body=f"أزهار جبلية صيفية من جنوب الطائف للعسل الصيفي رقم {i}.",
        )
        for i in range(5)
    ]
    question_row = _StubKBSection(
        section_id=243,
        kind="custom",
        title=LINEAGE_TITLE,
        body=LINEAGE_BODY,
    )
    db = _install_kb_stubs(monkeypatch, title_rows + [question_row])
    sections = _retrieve_product_kb_sections(
        db,
        TENANT_A,
        subject=FLORAL_TITLE,
        message=BEE_QUESTION,
        product_id=PRODUCT_A,
        limit=4,
    )
    assert len(sections) == 4
    assert 243 in _ids(sections)
    assert sections[0]["section_id"] == 243


def test_tenant_isolation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    foreign = _StubKBSection(
        section_id=243,
        kind="custom",
        title=LINEAGE_TITLE,
        body=LINEAGE_BODY,
        tenant_id=TENANT_B,
    )
    db = _install_kb_stubs(monkeypatch, [foreign])
    sections = _retrieve_product_kb_sections(
        db,
        TENANT_A,
        subject=FLORAL_TITLE,
        message=BEE_QUESTION,
        product_id=PRODUCT_A,
    )
    assert sections == []


def test_section_243_shaped_replay_reaches_known_facts_and_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nectar = _StubKBSection(
        section_id=212,
        kind="product_benefit",
        title=NECTAR_TITLE,
        body=NECTAR_BODY,
        product_ids=[PRODUCT_A],
    )
    lineage = _StubKBSection(
        section_id=243,
        kind="custom",
        title=LINEAGE_TITLE,
        body=LINEAGE_BODY,
    )
    db = _install_kb_stubs(monkeypatch, [nectar, lineage])
    ctx = SimpleNamespace(_db=db, tenant_id=TENANT_A)
    subject_product = {
        "id": PRODUCT_A,
        "title": FLORAL_TITLE,
        "description": NECTAR_BODY,
        "price": 180,
    }
    bundle = gather_product_knowledge_facts(
        ctx,
        subject_product=subject_product,
        question_kind=ProductKnowledgeKind.ATTRIBUTE,
        message=BEE_QUESTION,
    )
    kb_sections = list(bundle.allowed_facts.get("kb_sections") or [])
    ids = _ids(kb_sections)
    assert 243 in ids
    assert 212 in ids
    persona = build_kb_product_answer_facts_bundle(
        inbound_text=BEE_QUESTION,
        tenant_id=TENANT_A,
        question_kind="attribute",
        allowed_facts=dict(bundle.allowed_facts),
        missing_facts=list(bundle.missing_facts),
        subject_product=subject_product,
    )
    payload = build_user_prompt(persona)
    assert LINEAGE_BODY in payload
    assert 243 in (persona.verified_facts.get("kb_section_ids") or [])
