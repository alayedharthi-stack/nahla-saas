"""
LIVE-KNOWLEDGE-LEAK-D1A — legacy import semantic boundary.

INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
GOV003_ACTIVE=YES
MODEL_EXPRESSION_POLICY=MODEL_CHOOSES_ITS_OWN_WORDS
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
PERSONA_CHANGED=NO
PHRASE_MAP_CHANGED=NO
KEYWORD_ROUTER_CHANGED=NO
CUSTOMER_REGEX_CHANGED=NO
EXACT_REPLY_TEMPLATE_CHANGED=NO
MANDATORY_PHRASE_CHANGED=NO
FORBIDDEN_WORD_LIST_CHANGED=NO
PREFERRED_WORD_LIST_CHANGED=NO
POSTPROCESS_STYLE_REWRITE_CHANGED=NO

Classifier output is a structural suggestion only. Imported rows stay
needs_review until a merchant action. Tests use synthetic merchant
document input only.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")


def _ai_visible(item: Dict[str, Any]) -> bool:
    from core.knowledge import kb_row_is_ai_visible

    return kb_row_is_ai_visible(
        SimpleNamespace(
            deleted_at=None,
            is_active=item.get("is_active", True),
            ai_status=item.get("ai_status"),
        )
    )


def _classifier_kind(kind: str, *, confidence: float = 0.95) -> Dict[str, Any]:
    return {
        "fallback_used": False,
        "confidence": confidence,
        "proposed_ops": [{"op": "create", "kind": kind}],
    }


def test_heading_table_does_not_learn_incident_phrases() -> None:
    from routers.knowledge import _HEADING_KEYWORDS

    joined = " ".join(kw for kw, _kind in _HEADING_KEYWORDS)
    for banned in ("لا يبالغ", "عند الحديث", "جرثومة المعدة"):
        assert banned not in joined


def test_heading_hint_still_maps_benefits_substring() -> None:
    from routers.knowledge import _classify_heading, _split_legacy_text

    assert _classify_heading("الفوائد") == "product_benefit"
    blocks = _split_legacy_text(
        "# الفوائد\nالحذاء الرياضي الأبيض خفيف للمشي اليومي.\n"
    )
    assert blocks[0]["kind"] == "product_benefit"


def test_high_confidence_behavioral_classification_stays_in_review() -> None:
    from routers.knowledge import _plan_legacy_import
    from services.knowledge_section_kinds import is_behavioral_kind

    blob = (
        "# الفوائد\n"
        "عند وصف المنتج ابق داخل إطار الاستخدام اليومي فقط.\n"
        "لا تتجاوز نطاق وصف المنتج.\n"
    )
    planned = _plan_legacy_import(
        blob,
        classifier_fn=lambda _text: _classifier_kind("compliance_rules", confidence=0.97),
    )
    item = planned[0]
    assert item["kind"] == "compliance_rules"
    assert is_behavioral_kind(item["kind"]) is True
    assert item["ai_status"] == "needs_review"
    assert item["classification_source"] == "canonical_classifier"
    assert _ai_visible(item) is False
    assert "إطار الاستخدام اليومي" in item["body"]


def test_high_confidence_commerce_classification_stays_in_review() -> None:
    from routers.knowledge import _plan_legacy_import

    blob = "# الشحن\nنشحن بسمسا خلال 2-3 أيام عمل.\n"
    planned = _plan_legacy_import(
        blob,
        classifier_fn=lambda _text: _classifier_kind("shipping_zones", confidence=0.95),
    )
    item = planned[0]
    assert item["kind"] == "shipping_zones"
    assert item["ai_status"] == "needs_review"
    assert _ai_visible(item) is False
    assert "سمسا" in item["body"]


def test_low_confidence_classification_stays_in_review() -> None:
    from routers.knowledge import _plan_legacy_import
    from services.knowledge_section_kinds import is_behavioral_kind

    blob = (
        "# أسلوب الرد\n"
        "استخدم لهجة خليجية مختصرة.\n"
    )
    planned = _plan_legacy_import(
        blob,
        classifier_fn=lambda _text: _classifier_kind("response_tone", confidence=0.2),
    )
    item = planned[0]
    assert item["kind"] == "response_tone"
    assert is_behavioral_kind(item["kind"]) is True
    assert item["ai_status"] == "needs_review"
    assert _ai_visible(item) is False


def test_classifier_unavailable_stays_in_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from modules.ai.knowledge import classifier as kbc
    from routers.knowledge import _plan_legacy_import

    monkeypatch.setattr(kbc, "_API_KEY", "")
    body = "الحذاء الرياضي الأبيض خفيف ومناسب للمشي اليومي."
    blob = f"# الفوائد\n{body}\n"
    planned = _plan_legacy_import(blob)
    item = planned[0]
    assert item["kind"] == "quick_update"
    assert item["ai_status"] == "needs_review"
    assert _ai_visible(item) is False
    assert body in item["body"]
    assert item["proven_product_ids"] == ()


def test_heading_only_stays_in_review() -> None:
    from routers.knowledge import _plan_legacy_import

    shipping_body = "نشحن بسمسا خلال 2-3 أيام عمل.\nالشحن المجاني للطلبات فوق 200 ريال."
    payment_body = "نقبل مدى وفيزا والتحويل البنكي."
    blob = f"# الشحن\n{shipping_body}\n\n# الدفع\n{payment_body}\n"
    planned = _plan_legacy_import(blob, max_classifier_calls=0)
    assert len(planned) == 2
    for item in planned:
        assert item["ai_status"] == "needs_review"
        assert _ai_visible(item) is False
        assert item["kind"] == "quick_update"
    shipping = next(i for i in planned if i["heading_hint_kind"] == "shipping_zones")
    payment = next(i for i in planned if i["heading_hint_kind"] == "payment_method")
    assert shipping_body in shipping["body"]
    assert payment_body in payment["body"]


def test_advisor_only_stays_in_review(monkeypatch: pytest.MonkeyPatch) -> None:
    from modules.ai.knowledge import classifier as kbc
    from routers.knowledge import _plan_legacy_import
    from services.knowledge_section_kinds import is_behavioral_kind

    monkeypatch.setattr(kbc, "_API_KEY", "")
    blob = (
        "# الفوائد\n"
        "ممنوع ادعاء علاجي للعسل.\n"
        "لا تقل حبيبي للعملاء.\n"
    )
    planned = _plan_legacy_import(blob)
    item = planned[0]
    assert item["metadata_json"]["repair_advisor_suspicion"] is True
    assert item["metadata_json"]["repair_advisor_suggested_kind"]
    assert item["ai_status"] == "needs_review"
    assert _ai_visible(item) is False
    assert not (
        is_behavioral_kind(item["kind"]) and item["ai_status"] == "approved"
    )
    assert "ممنوع ادعاء علاجي للعسل" in item["body"]


def test_product_bound_without_scope_stays_in_review() -> None:
    from routers.knowledge import _plan_legacy_import

    blob = (
        "# الفوائد\n"
        "الحذاء الرياضي الأبيض خفيف ومناسب للمشي اليومي.\n"
    )
    planned = _plan_legacy_import(
        blob,
        classifier_fn=lambda _text: _classifier_kind("product_benefit", confidence=0.95),
    )
    item = planned[0]
    assert item["kind"] == "product_benefit"
    assert item["ai_status"] == "needs_review"
    assert item["metadata_json"].get("unscoped_product_bound") is True
    assert item["proven_product_ids"] == ()
    assert _ai_visible(item) is False


def test_scoped_product_bound_still_stays_in_review() -> None:
    from routers.knowledge import _plan_legacy_import

    blob = (
        "# الفوائد\n"
        "الحذاء الرياضي الأبيض خفيف ومناسب للمشي اليومي في الجو الحار.\n"
    )
    planned = _plan_legacy_import(
        blob,
        classifier_fn=lambda _text: _classifier_kind("product_benefit", confidence=0.95),
        proven_product_ids=(101,),
    )
    item = planned[0]
    assert item["kind"] == "product_benefit"
    assert item["ai_status"] == "needs_review"
    assert _ai_visible(item) is False


def test_classifier_prose_rewrite_is_not_stored_as_payload() -> None:
    from routers.knowledge import _plan_legacy_import

    heading = "الفوائد"
    body = "الحذاء الرياضي الأبيض خفيف ومناسب للمشي اليومي."
    rewritten_title = "عنوان معاد صياغته من المصنف"
    rewritten_body = "نص معاد صياغته من المصنف وليس النص الأصلي."

    def _clf(_text: str) -> Dict[str, Any]:
        return {
            "fallback_used": False,
            "confidence": 0.95,
            "proposed_ops": [{
                "op": "create",
                "kind": "product_benefit",
                "title": rewritten_title,
                "body": rewritten_body,
            }],
        }

    planned = _plan_legacy_import(
        f"# {heading}\n{body}\n",
        classifier_fn=_clf,
    )
    item = planned[0]
    assert item["kind"] == "product_benefit"
    assert item["title"] == heading
    assert item["body"] == body
    assert rewritten_title not in item["title"]
    assert rewritten_body not in item["body"]
    assert item["ai_status"] == "needs_review"
    assert _ai_visible(item) is False


def test_original_merchant_text_preserved() -> None:
    from routers.knowledge import _plan_legacy_import

    heading = "الفوائد"
    body = "الحذاء الرياضي الأبيض خفيف ومناسب للمشي اليومي في الجو الحار."
    planned = _plan_legacy_import(
        f"# {heading}\n{body}\n",
        classifier_fn=lambda _text: _classifier_kind("product_benefit"),
    )
    item = planned[0]
    assert item["title"] == heading
    assert item["body"] == body
    assert item["proven_product_ids"] == ()
    assert "product_id" not in item["metadata_json"]


def test_classifier_call_limit_does_not_create_activation_privilege() -> None:
    from routers.knowledge import _MAX_CLASSIFIER_CALLS_PER_IMPORT, _plan_legacy_import

    calls: List[str] = []

    def _clf(text: str) -> Dict[str, Any]:
        calls.append(text)
        return _classifier_kind("shipping_zones", confidence=0.95)

    blob = (
        "# الشحن\n"
        "نشحن بسمسا خلال 2-3 أيام عمل.\n\n"
        "# الدفع\n"
        "نقبل مدى وفيزا والتحويل البنكي.\n"
    )
    planned = _plan_legacy_import(blob, classifier_fn=_clf)
    assert _MAX_CLASSIFIER_CALLS_PER_IMPORT == 1
    assert len(calls) == 1
    assert all(item["ai_status"] == "needs_review" for item in planned)
    assert all(_ai_visible(item) is False for item in planned)


def test_existing_merchant_draft_approve_path_exists() -> None:
    """Existing merchant approval is the draft approve endpoint, not confidence."""
    from routers.knowledge import router

    paths = {getattr(r, "path", "") for r in router.routes}
    assert "/knowledge/drafts/{draft_id}/approve" in paths


def test_visibility_gate_requires_merchant_approved_status_not_confidence() -> None:
    from core.knowledge import kb_row_is_ai_visible

    review = SimpleNamespace(deleted_at=None, is_active=True, ai_status="needs_review")
    approved = SimpleNamespace(deleted_at=None, is_active=True, ai_status="approved")
    assert kb_row_is_ai_visible(review) is False
    assert kb_row_is_ai_visible(approved) is True
