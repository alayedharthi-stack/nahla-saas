"""
LIVE-KNOWLEDGE-LEAK-D1A — legacy import semantic boundary.

INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
PERSONA_CHANGED=NO
PHRASE_MAP_CHANGED=NO
KEYWORD_ROUTER_CHANGED=NO
CUSTOMER_REGEX_CHANGED=NO

Repair-advisor hits are suspicion/review metadata only. Write-time
approval requires the existing canonical classifier at the few-shot
confidence floor. Tests use synthetic merchant-document authoring
text only — they do not add incident headings to production rules.
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


def _classifier_kind(kind: str, *, confidence: float = 0.9) -> Dict[str, Any]:
    return {
        "fallback_used": False,
        "confidence": confidence,
        "proposed_ops": [{"op": "create", "kind": kind}],
    }


def test_heading_table_does_not_learn_incident_phrases() -> None:
    """Production heading keywords must not grow incident-specific rules."""
    from routers.knowledge import _HEADING_KEYWORDS

    joined = " ".join(kw for kw, _kind in _HEADING_KEYWORDS)
    for banned in ("لا يبالغ", "عند الحديث", "جرثومة المعدة"):
        assert banned not in joined


def test_heading_hint_still_maps_benefits_substring() -> None:
    """Splitter hint is unchanged; it is not write-time authority."""
    from routers.knowledge import _classify_heading, _split_legacy_text

    assert _classify_heading("الفوائد") == "product_benefit"
    blocks = _split_legacy_text(
        "# الفوائد\nالحذاء الرياضي الأبيض خفيف للمشي اليومي.\n"
    )
    assert blocks[0]["kind"] == "product_benefit"


def test_confidence_floor_comes_from_classifier_fewshots() -> None:
    from modules.ai.knowledge.classifier import _FEW_SHOT_EXAMPLES
    from routers.knowledge import _legacy_classifier_confidence_floor

    fewshot_min = min(float(ex["expected"]["confidence"]) for ex in _FEW_SHOT_EXAMPLES)
    assert _legacy_classifier_confidence_floor() == fewshot_min
    assert fewshot_min == 0.90


def test_repair_advisor_alone_cannot_auto_approve_final_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case 1: advisor suspicion is metadata only, never approved kind."""
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
    assert item["heading_hint_kind"] == "product_benefit"
    assert item["metadata_json"]["repair_advisor_suspicion"] is True
    assert item["metadata_json"]["repair_advisor_suggested_kind"]
    assert item["classification_source"] != "repair_advisor_behavioral"
    assert not (
        is_behavioral_kind(item["kind"]) and item["ai_status"] == "approved"
    )
    assert item["ai_status"] == "needs_review"
    assert _ai_visible(item) is False
    assert "ممنوع ادعاء علاجي للعسل" in item["body"]
    assert item["proven_product_ids"] == ()


def test_high_confidence_behavioral_classifier_may_approve() -> None:
    """Case 2: canonical high-confidence behavioral kind may be approved."""
    from routers.knowledge import _plan_legacy_import
    from services.knowledge_section_kinds import is_behavioral_kind

    blob = (
        "# الفوائد\n"
        "عند وصف المنتج ابق داخل إطار الاستخدام اليومي فقط.\n"
        "لا تتجاوز نطاق وصف المنتج.\n"
    )
    planned = _plan_legacy_import(
        blob,
        classifier_fn=lambda _text: _classifier_kind("compliance_rules", confidence=0.95),
    )
    item = planned[0]
    assert item["kind"] == "compliance_rules"
    assert is_behavioral_kind(item["kind"]) is True
    assert item["ai_status"] == "approved"
    assert item["classification_source"] == "canonical_classifier"
    assert _ai_visible(item) is True
    assert "إطار الاستخدام اليومي" in item["body"]


def test_low_confidence_behavioral_classifier_is_not_ai_visible() -> None:
    """Case 3: valid kind + fallback_used=false + low confidence → review."""
    from routers.knowledge import (
        _legacy_classifier_confidence_floor,
        _plan_legacy_import,
    )
    from services.knowledge_section_kinds import is_behavioral_kind

    floor = _legacy_classifier_confidence_floor()
    blob = (
        "# أسلوب الرد\n"
        "استخدم لهجة خليجية مختصرة.\n"
    )
    planned = _plan_legacy_import(
        blob,
        classifier_fn=lambda _text: _classifier_kind(
            "response_tone", confidence=floor - 0.4,
        ),
    )
    item = planned[0]
    assert item["kind"] == "response_tone"
    assert is_behavioral_kind(item["kind"]) is True
    assert item["ai_status"] == "needs_review"
    assert item["classification_source"] == "canonical_classifier_low_confidence"
    assert _ai_visible(item) is False
    assert item["classification_confidence"] < floor


def test_heading_shipping_payment_without_classifier_is_not_approved() -> None:
    """Case 4: heading substring is not sole authority for an AI-visible fact."""
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
        assert item["classification_source"] != "heading_hint"
    shipping = next(i for i in planned if i["heading_hint_kind"] == "shipping_zones")
    payment = next(i for i in planned if i["heading_hint_kind"] == "payment_method")
    assert shipping_body in shipping["body"]
    assert payment_body in payment["body"]
    assert shipping["proven_product_ids"] == ()
    assert payment["proven_product_ids"] == ()


def test_unscoped_product_bound_classifier_result_is_held_for_review() -> None:
    """Case 5: product-bound classified kind with zero product scope is not AI-visible."""
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


def test_classifier_unavailable_fails_to_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case 6: classifier unavailable → safe review, text kept, no product links."""
    from modules.ai.knowledge import classifier as kbc
    from routers.knowledge import _plan_legacy_import

    monkeypatch.setattr(kbc, "_API_KEY", "")
    body = "الحذاء الرياضي الأبيض خفيف ومناسب للمشي اليومي."
    blob = f"# الفوائد\n{body}\n"
    planned = _plan_legacy_import(blob)
    item = planned[0]
    assert item["kind"] == "quick_update"
    assert item["ai_status"] == "needs_review"
    assert item["classification_source"] == "fail_safe_unknown"
    assert _ai_visible(item) is False
    assert body in item["body"]
    assert item["proven_product_ids"] == ()


def test_merchant_text_preserved_and_no_product_links_fabricated() -> None:
    """Cases 7–8: original authoring text is kept; no product ids are invented."""
    from routers.knowledge import _plan_legacy_import

    heading = "الفوائد"
    body = "الحذاء الرياضي الأبيض خفيف ومناسب للمشي اليومي في الجو الحار."
    blob = f"# {heading}\n{body}\n"
    planned = _plan_legacy_import(
        blob,
        classifier_fn=lambda _text: _classifier_kind("product_benefit", confidence=0.95),
    )
    item = planned[0]
    assert item["title"] == heading
    assert item["body"] == body
    assert item["proven_product_ids"] == ()
    assert "product_id" not in item["metadata_json"]
    assert item["metadata_json"].get("unscoped_product_bound") is True


def test_high_confidence_scoped_product_benefit_may_be_approved() -> None:
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
    assert item["ai_status"] == "approved"
    assert item["classification_source"] == "canonical_classifier"
    assert item["proven_product_ids"] == (101,)
    assert _ai_visible(item) is True


def test_classifier_call_limit_fails_remaining_candidates_to_review() -> None:
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
    assert sum(1 for item in planned if item["classifier_attempted"]) == 1
    unconfirmed = [item for item in planned if not item["classifier_attempted"]]
    assert len(unconfirmed) == 1
    leftover = unconfirmed[0]
    assert leftover["ai_status"] == "needs_review"
    assert _ai_visible(leftover) is False
    assert leftover["kind"] == "quick_update"
    assert leftover["metadata_json"].get("fail_safe_reason") == "classifier_call_limit"


def test_confirmed_shipping_kind_may_be_approved() -> None:
    from routers.knowledge import _plan_legacy_import

    blob = "# الشحن\nنشحن بسمسا خلال 2-3 أيام عمل.\n"
    planned = _plan_legacy_import(
        blob,
        classifier_fn=lambda _text: _classifier_kind("shipping_zones", confidence=0.95),
    )
    item = planned[0]
    assert item["kind"] == "shipping_zones"
    assert item["ai_status"] == "approved"
    assert item["classification_source"] == "canonical_classifier"
    assert "سمسا" in item["body"]
