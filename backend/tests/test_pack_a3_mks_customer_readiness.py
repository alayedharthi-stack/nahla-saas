"""
Pack A3 — MKS customer-readiness completeness contract.

Shared pure detector + existence/retrieval coherence.
Does not mutate rows; FAQ customer exposure remains deferred.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, List, Optional
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    s = str(_p)
    if s not in sys.path:
        sys.path.insert(0, s)

from services.merchant_document_retrieval import (  # noqa: E402
    retrieve_merchant_documents,
)
from services.merchant_knowledge_customer_readiness import (  # noqa: E402
    INCOMPLETE_AUTHORING_TEMPLATE,
    READY,
    assess_mks_customer_readiness,
    mks_section_customer_ready,
)
from services.merchant_policy_existence import (  # noqa: E402
    build_policy_existence_map,
)
from tests.commerce_scenario_fixtures import (  # noqa: E402
    make_scenario_db,
    seed_knowledge_section,
    seed_tenant,
)

# Canonical unfinished authoring shape (mks:122-shaped; fixture only — no row mutation).
_CANONICAL_PLACEHOLDER = (
    "نقبل الاسترجاع خلال [أضف المدة — مثلاً 14 يوماً] من تاريخ الاستلام."
)
_COMPLETE_RETURN = (
    "يمكن الاسترجاع أو الاستبدال خلال 7 أيام من تاريخ الاستلام "
    "بشرط أن يكون المنتج بحالته الأصلية."
)
_COMPLETE_SHIPPING = (
    "الشحن متاح لجميع مدن المملكة خلال 2–5 أيام عمل، "
    "وتُحسب رسوم التوصيل عند إتمام الطلب."
)
_COMPLETE_STORY = (
    "بدأ متجرنا عام 2018 بهدف تقديم منتجات عالية الجودة "
    "وخدمة عملاء واضحة وشفافة."
)


def _fake_section(
    *,
    section_id: int,
    tenant_id: int,
    kind: str,
    body: str,
    title: str = "",
    source: str = "manual",
    priority: int = 10,
) -> MagicMock:
    row = MagicMock()
    row.id = section_id
    row.tenant_id = tenant_id
    row.kind = kind
    row.body = body
    row.title = title or kind
    row.source = source
    row.priority = priority
    row.updated_at = None
    row.metadata_json = {"content_hash": f"h{section_id}"}
    row.product_links = []
    row.deleted_at = None
    row.is_active = True
    return row


def _map_with_rows(tenant_id: int, rows: List[Any]):
    db = MagicMock()
    db.query.return_value.filter.return_value.order_by.return_value.all.return_value = (
        rows
    )
    with patch(
        "core.knowledge.apply_ai_visible_kb_query_filters",
        side_effect=lambda query: query,
    ):
        return build_policy_existence_map(db, tenant_id)


# ── Detector unit matrix ─────────────────────────────────────────────────────


class TestReadinessDetector:
    def test_exact_placeholder_shape_incomplete(self):
        v = assess_mks_customer_readiness(_CANONICAL_PLACEHOLDER)
        assert v.status == INCOMPLETE_AUTHORING_TEMPLATE
        assert v.is_ready is False
        assert v.reason_code in {
            "bracket_add_instruction",
            "unfinished_authoring_instruction",
        }

    def test_todo_template_incomplete(self):
        for body in (
            "سياسة الاسترجاع: TODO أكمل النص",
            "Return policy PLACEHOLDER text",
            "نص السياسة [TEMPLATE]",
            "الشروط: FIXME",
        ):
            v = assess_mks_customer_readiness(body)
            assert v.is_ready is False, body
            assert v.status == INCOMPLETE_AUTHORING_TEMPLATE, body

    def test_normal_complete_ready(self):
        v = assess_mks_customer_readiness(_COMPLETE_RETURN)
        assert v.status == READY
        assert v.is_ready is True
        assert v.reason_code is None

    def test_legitimate_brackets_ready(self):
        for body in (
            "الضمان يشمل [اسم المنتج] حسب الفاتورة.",
            "يمكن التواصل معنا عبر قسم [خدمة العملاء].",
            "المنتج متوفر بمقاسات [S/M/L].",
        ):
            v = assess_mks_customer_readiness(body)
            assert v.is_ready is True, body

    def test_ordinary_example_word_not_auto_incomplete(self):
        body = (
            "يمكن الاسترجاع خلال 14 يوماً مثلاً عند بقاء المنتج بحالته الأصلية. "
            "مثال: إذا استلمت الطلب يوم الأحد يمكنك الإرجاع حتى الأحد التالي."
        )
        v = assess_mks_customer_readiness(body)
        assert v.is_ready is True

    def test_add_verb_outside_brackets_not_auto_incomplete(self):
        body = "يمكنك أن تضيف ملاحظاتك عند تقديم طلب الاسترجاع عبر الواتساب."
        # "تضيف" is conjugation; ensure bare أضف outside brackets does not trip.
        body2 = "ننصح العميل: أضف الملاحظات في رسالة الطلب عند الحاجة."
        assert assess_mks_customer_readiness(body).is_ready is True
        assert assess_mks_customer_readiness(body2).is_ready is True

    def test_brackets_alone_not_rejected(self):
        body = "الشحن إلى المنطقة [الشمالية] خلال 3 أيام."
        assert assess_mks_customer_readiness(body).is_ready is True

    def test_source_agnostic_same_verdict(self):
        # Detector ignores source; call sites must not special-case.
        body = _CANONICAL_PLACEHOLDER
        for source in ("ai_classified", "manual", "import", "unknown"):
            row = _fake_section(
                section_id=122,
                tenant_id=33,
                kind="return_policy",
                body=body,
                source=source,
            )
            assert mks_section_customer_ready(row).is_ready is False, source


# ── Existence ────────────────────────────────────────────────────────────────


class TestPolicyExistenceReadiness:
    def test_incomplete_only_unknown(self):
        row = _fake_section(
            section_id=122,
            tenant_id=33,
            kind="return_policy",
            body=_CANONICAL_PLACEHOLDER,
            source="ai_classified",
        )
        m = _map_with_rows(33, [row])
        assert m["return_policy"]["status"] == "UNKNOWN"
        assert m["return_policy"]["doc_ref"] is None
        assert m["return_policy"]["status"] != "KNOWN_ABSENT"

    def test_complete_known_present(self):
        row = _fake_section(
            section_id=50,
            tenant_id=1,
            kind="return_policy",
            body=_COMPLETE_RETURN,
        )
        m = _map_with_rows(1, [row])
        assert m["return_policy"]["status"] == "KNOWN_PRESENT"
        assert m["return_policy"]["doc_ref"] == "mks:50"

    def test_mixed_incomplete_plus_complete_present_on_complete(self):
        incomplete = _fake_section(
            section_id=1,
            tenant_id=7,
            kind="return_policy",
            body=_CANONICAL_PLACEHOLDER,
            priority=5,
        )
        complete = _fake_section(
            section_id=2,
            tenant_id=7,
            kind="return_policy",
            body=_COMPLETE_RETURN,
            priority=10,
        )
        m = _map_with_rows(7, [incomplete, complete])
        assert m["return_policy"]["status"] == "KNOWN_PRESENT"
        assert m["return_policy"]["doc_ref"] == "mks:2"

    def test_never_known_absent(self):
        m = _map_with_rows(1, [])
        for kind, payload in m.items():
            assert payload["status"] in {"KNOWN_PRESENT", "UNKNOWN"}, kind
            assert payload["status"] != "KNOWN_ABSENT", kind

    def test_shipping_policy_incomplete_unknown(self):
        row = _fake_section(
            section_id=9,
            tenant_id=1,
            kind="shipping_policy",
            body="سياسة الشحن: [أضف تفاصيل الشحن — مثلاً 3 أيام]",
        )
        m = _map_with_rows(1, [row])
        assert m["shipping_policy"]["status"] == "UNKNOWN"
        assert m["shipping_policy"]["doc_ref"] is None


# ── Retrieval + coherence ────────────────────────────────────────────────────


class TestRetrievalReadiness:
    def test_incomplete_excluded(self):
        db, _ = make_scenario_db()
        tenant = seed_tenant(db, name="متجر تجريبي عام")
        seed_knowledge_section(
            db,
            tenant.id,
            kind="return_policy",
            title="سياسة الاسترجاع",
            body=_CANONICAL_PLACEHOLDER,
        )
        result = retrieve_merchant_documents(db, tenant.id, "وش سياسة الاسترجاع؟")
        assert len(result.sections) == 0
        assert result.sections_skipped_incomplete >= 1
        assert any(r.startswith("mks:") for r in result.doc_refs_skipped)

    def test_complete_included(self):
        db, _ = make_scenario_db()
        tenant = seed_tenant(db, name="متجر تجريبي عام")
        seed_knowledge_section(
            db,
            tenant.id,
            kind="return_policy",
            title="سياسة الاسترجاع",
            body=_COMPLETE_RETURN,
        )
        result = retrieve_merchant_documents(db, tenant.id, "وش سياسة الاسترجاع؟")
        assert len(result.sections) >= 1
        assert "7 أيام" in result.sections[0].body
        assert result.sections_skipped_incomplete == 0

    def test_mixed_returns_complete_only(self):
        db, _ = make_scenario_db()
        tenant = seed_tenant(db, name="متجر تجريبي عام")
        bad = seed_knowledge_section(
            db,
            tenant.id,
            kind="return_policy",
            title="مسودة",
            body=_CANONICAL_PLACEHOLDER,
            priority=1,
        )
        good = seed_knowledge_section(
            db,
            tenant.id,
            kind="return_policy",
            title="نهائية",
            body=_COMPLETE_RETURN,
            priority=2,
        )
        result = retrieve_merchant_documents(db, tenant.id, "وش سياسة الاسترجاع؟")
        assert len(result.sections) == 1
        assert result.sections[0].section_id == good.id
        assert f"mks:{bad.id}" in result.doc_refs_skipped
        assert f"mks:{good.id}" == result.sections[0].provenance.get("doc_ref")
        assert _CANONICAL_PLACEHOLDER not in result.sections[0].body

    def test_store_story_incomplete_not_retrieved(self):
        db, _ = make_scenario_db()
        tenant = seed_tenant(db, name="متجر تجريبي عام")
        seed_knowledge_section(
            db,
            tenant.id,
            kind="store_story",
            title="قصة المتجر",
            body="قصتنا: [اكتب هنا قصة المتجر]",
        )
        result = retrieve_merchant_documents(db, tenant.id, "وش قصة المتجر؟")
        assert len(result.sections) == 0
        assert result.sections_skipped_incomplete >= 1

    def test_shipping_policy_incomplete_not_retrieved(self):
        db, _ = make_scenario_db()
        tenant = seed_tenant(db, name="متجر تجريبي عام")
        seed_knowledge_section(
            db,
            tenant.id,
            kind="shipping_policy",
            title="سياسة الشحن",
            body="الشحن: [أضف مدة التوصيل — مثلاً 3 أيام]",
        )
        result = retrieve_merchant_documents(db, tenant.id, "وش سياسة الشحن؟")
        assert len(result.sections) == 0

    def test_placeholder_body_does_not_reach_compose_format(self):
        from services.merchant_document_retrieval import (
            format_retrieved_documents_for_prompt,
        )

        db, _ = make_scenario_db()
        tenant = seed_tenant(db, name="متجر تجريبي عام")
        seed_knowledge_section(
            db,
            tenant.id,
            kind="return_policy",
            body=_CANONICAL_PLACEHOLDER,
        )
        result = retrieve_merchant_documents(db, tenant.id, "وش سياسة الاسترجاع؟")
        prompt = format_retrieved_documents_for_prompt(result)
        assert prompt == ""
        assert "[أضف المدة" not in prompt


class TestExistenceRetrievalCoherence:
    def test_incomplete_only_unknown_and_zero_retrieval(self):
        db, _ = make_scenario_db()
        tenant = seed_tenant(db, name="متجر تجريبي عام")
        seed_knowledge_section(
            db,
            tenant.id,
            kind="return_policy",
            body=_CANONICAL_PLACEHOLDER,
        )
        existence = build_policy_existence_map(db, tenant.id)
        retrieval = retrieve_merchant_documents(db, tenant.id, "وش سياسة الاسترجاع؟")
        assert existence["return_policy"]["status"] == "UNKNOWN"
        assert existence["return_policy"]["doc_ref"] is None
        assert len(retrieval.sections) == 0
        # No PRESENT + zero eligible docs.
        assert not (
            existence["return_policy"]["status"] == "KNOWN_PRESENT"
            and len(retrieval.sections) == 0
        )

    def test_complete_present_and_retrieval_nonzero(self):
        db, _ = make_scenario_db()
        tenant = seed_tenant(db, name="متجر تجريبي عام")
        section = seed_knowledge_section(
            db,
            tenant.id,
            kind="return_policy",
            body=_COMPLETE_RETURN,
        )
        existence = build_policy_existence_map(db, tenant.id)
        retrieval = retrieve_merchant_documents(db, tenant.id, "وش سياسة الاسترجاع؟")
        assert existence["return_policy"]["status"] == "KNOWN_PRESENT"
        assert existence["return_policy"]["doc_ref"] == f"mks:{section.id}"
        assert len(retrieval.sections) >= 1
        assert retrieval.sections[0].provenance.get("doc_ref") == f"mks:{section.id}"

    def test_mixed_present_retrieval_complete_only(self):
        db, _ = make_scenario_db()
        tenant = seed_tenant(db, name="متجر تجريبي عام")
        bad = seed_knowledge_section(
            db,
            tenant.id,
            kind="return_policy",
            body=_CANONICAL_PLACEHOLDER,
            priority=1,
        )
        good = seed_knowledge_section(
            db,
            tenant.id,
            kind="return_policy",
            body=_COMPLETE_RETURN,
            priority=2,
        )
        existence = build_policy_existence_map(db, tenant.id)
        retrieval = retrieve_merchant_documents(db, tenant.id, "وش سياسة الاسترجاع؟")
        assert existence["return_policy"]["status"] == "KNOWN_PRESENT"
        assert existence["return_policy"]["doc_ref"] == f"mks:{good.id}"
        assert len(retrieval.sections) == 1
        assert retrieval.sections[0].section_id == good.id
        assert f"mks:{bad.id}" not in [
            s.provenance.get("doc_ref") for s in retrieval.sections
        ]


class TestDualTenantIsolation:
    def test_incomplete_tenant_a_does_not_rescue_from_tenant_b(self):
        db, _ = make_scenario_db()
        tenant_a = seed_tenant(db, name="متجر أ ناقص")
        tenant_b = seed_tenant(db, name="متجر ب مكتمل")
        seed_knowledge_section(
            db,
            tenant_a.id,
            kind="return_policy",
            body=_CANONICAL_PLACEHOLDER,
        )
        seed_knowledge_section(
            db,
            tenant_b.id,
            kind="return_policy",
            body=_COMPLETE_RETURN,
        )
        map_a = build_policy_existence_map(db, tenant_a.id)
        map_b = build_policy_existence_map(db, tenant_b.id)
        ret_a = retrieve_merchant_documents(db, tenant_a.id, "وش سياسة الاسترجاع؟")
        ret_b = retrieve_merchant_documents(db, tenant_b.id, "وش سياسة الاسترجاع؟")
        assert map_a["return_policy"]["status"] == "UNKNOWN"
        assert len(ret_a.sections) == 0
        assert map_b["return_policy"]["status"] == "KNOWN_PRESENT"
        assert len(ret_b.sections) >= 1
        assert "7 أيام" in ret_b.sections[0].body


class TestFaqRemainsDeferred:
    def test_faq_not_customer_retrieved_even_when_complete(self):
        db, _ = make_scenario_db()
        tenant = seed_tenant(db, name="متجر تجريبي عام")
        seed_knowledge_section(
            db,
            tenant.id,
            kind="faq",
            title="أسئلة شائعة",
            body="س: هل الشحن مجاني؟ ج: يعتمد على المدينة والطلب.",
        )
        result = retrieve_merchant_documents(db, tenant.id, "أسئلة شائعة؟")
        assert result.matched_intent == ""
        assert len(result.sections) == 0


class TestKnownRowShapeFixture:
    """mks:122-shaped content — fixture only; do not mutate production row."""

    def test_mks_122_shaped_not_customer_eligible(self):
        body = _CANONICAL_PLACEHOLDER
        verdict = assess_mks_customer_readiness(body)
        assert verdict.is_ready is False
        assert verdict.status == INCOMPLETE_AUTHORING_TEMPLATE
        row = _fake_section(
            section_id=122,
            tenant_id=33,
            kind="return_policy",
            body=body,
            source="ai_classified",
        )
        assert mks_section_customer_ready(row).is_ready is False
        m = _map_with_rows(33, [row])
        assert m["return_policy"]["status"] == "UNKNOWN"
        assert m["return_policy"]["doc_ref"] is None
