"""
LIVE-KNOWLEDGE-LEAK-D1A — legacy import semantic boundary.

INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
PERSONA_CHANGED=NO
PHRASE_MAP_CHANGED=NO
KEYWORD_ROUTER_CHANGED=NO
CUSTOMER_REGEX_CHANGED=NO

The heading table remains a structural splitter hint. Write-time kind
comes from the existing KB-2 advisor / canonical classifier. Tests use
synthetic merchant-document authoring text only — they do not add
incident headings to production rules.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

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
    """Splitter hint is unchanged; it is no longer write-time authority."""
    from routers.knowledge import _classify_heading, _split_legacy_text

    assert _classify_heading("الفوائد") == "product_benefit"
    blocks = _split_legacy_text(
        "# الفوائد\nالحذاء الرياضي الأبيض خفيف للمشي اليومي.\n"
    )
    assert blocks[0]["kind"] == "product_benefit"


def test_behavioral_benefits_heading_does_not_become_global_product_fact() -> None:
    """Heading looks like product benefits; body is existing advisor guidance."""
    from routers.knowledge import _plan_legacy_import
    from services.knowledge_section_kinds import is_behavioral_kind

    blob = (
        "# الفوائد\n"
        "ممنوع ادعاء علاجي للعسل.\n"
        "لا تقل حبيبي للعملاء.\n"
    )
    planned = _plan_legacy_import(
        blob,
        classifier_fn=lambda _text: pytest.fail("advisor must resolve this without LLM"),
    )
    assert len(planned) == 1
    item = planned[0]
    assert item["heading_hint_kind"] == "product_benefit"
    assert is_behavioral_kind(item["kind"]) is True
    assert item["kind"] != "product_benefit"
    assert item["kind"] == "compliance_rules"
    assert item["classification_source"] == "repair_advisor_behavioral"
    assert item["ai_status"] == "approved"
    assert item["proven_product_ids"] == ()
    assert item["metadata_json"].get("unscoped_product_bound") is not True
    # Group-7 is overlay-visible, not a product_benefit fact with 0 links.
    assert not (
        item["kind"] == "product_benefit"
        and _ai_visible(item)
        and not item["proven_product_ids"]
    )


def test_instruction_style_body_uses_canonical_classifier_not_heading() -> None:
    """When the advisor is silent, the existing classifier owns the kind."""
    from routers.knowledge import _plan_legacy_import
    from services.knowledge_section_kinds import is_behavioral_kind

    blob = (
        "# الفوائد\n"
        "عند وصف المنتج ابق داخل إطار الاستخدام اليومي فقط.\n"
        "لا تتجاوز نطاق وصف المنتج.\n"
    )
    planned = _plan_legacy_import(
        blob,
        classifier_fn=lambda _text: _classifier_kind("compliance_rules"),
    )
    item = planned[0]
    assert item["heading_hint_kind"] == "product_benefit"
    assert item["classification_source"] == "canonical_classifier"
    assert is_behavioral_kind(item["kind"]) is True
    assert item["kind"] == "compliance_rules"
    assert not (
        item["kind"] == "product_benefit"
        and _ai_visible(item)
        and not item["proven_product_ids"]
    )


def test_unknown_product_bound_import_fails_safe_not_product_benefit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from modules.ai.knowledge import classifier as kbc
    from routers.knowledge import _plan_legacy_import

    monkeypatch.setattr(kbc, "_API_KEY", "")
    blob = (
        "# الفوائد\n"
        "الحذاء الرياضي الأبيض خفيف ومناسب للمشي اليومي.\n"
    )
    planned = _plan_legacy_import(blob)
    item = planned[0]
    assert item["heading_hint_kind"] == "product_benefit"
    assert item["kind"] == "quick_update"
    assert item["ai_status"] == "needs_review"
    assert item["classification_source"] == "fail_safe_unknown"
    assert _ai_visible(item) is False
    assert item["kind"] != "product_benefit"


def test_legitimate_product_benefit_import_with_scope_is_preserved() -> None:
    from routers.knowledge import _plan_legacy_import

    blob = (
        "# الفوائد\n"
        "الحذاء الرياضي الأبيض خفيف ومناسب للمشي اليومي في الجو الحار.\n"
    )
    planned = _plan_legacy_import(
        blob,
        classifier_fn=lambda _text: _classifier_kind("product_benefit"),
        proven_product_ids=(101,),
    )
    item = planned[0]
    assert item["kind"] == "product_benefit"
    assert item["ai_status"] == "approved"
    assert item["classification_source"] == "canonical_classifier"
    assert item["proven_product_ids"] == (101,)
    assert _ai_visible(item) is True


def test_unscoped_product_bound_classifier_result_is_held_for_review() -> None:
    from routers.knowledge import _plan_legacy_import

    blob = (
        "# الفوائد\n"
        "الحذاء الرياضي الأبيض خفيف ومناسب للمشي اليومي.\n"
    )
    planned = _plan_legacy_import(
        blob,
        classifier_fn=lambda _text: _classifier_kind("product_benefit"),
    )
    item = planned[0]
    assert item["kind"] == "product_benefit"
    assert item["ai_status"] == "needs_review"
    assert item["metadata_json"].get("unscoped_product_bound") is True
    assert item["proven_product_ids"] == ()
    assert _ai_visible(item) is False


def test_shipping_and_payment_heading_hints_are_preserved() -> None:
    from routers.knowledge import _plan_legacy_import

    blob = (
        "# الشحن\n"
        "نشحن بسمسا خلال 2-3 أيام عمل.\n"
        "الشحن المجاني للطلبات فوق 200 ريال.\n\n"
        "# الدفع\n"
        "نقبل مدى وفيزا والتحويل البنكي.\n"
    )
    planned = _plan_legacy_import(
        blob,
        classifier_fn=lambda _text: pytest.fail("non-product-bound hints must not call classifier"),
    )
    kinds = [item["kind"] for item in planned]
    assert "shipping_zones" in kinds
    assert "payment_method" in kinds
    shipping = next(item for item in planned if item["kind"] == "shipping_zones")
    payment = next(item for item in planned if item["kind"] == "payment_method")
    assert shipping["ai_status"] == "approved"
    assert payment["ai_status"] == "approved"
    assert shipping["classification_source"] == "heading_hint"
    assert _ai_visible(shipping) is True
    assert _ai_visible(payment) is True


def test_behavioral_group7_import_is_preserved() -> None:
    from routers.knowledge import _plan_legacy_import
    from services.knowledge_section_kinds import is_behavioral_kind

    blob = (
        "# أسلوب الرد\n"
        "استخدم لهجة خليجية مختصرة.\n"
    )
    planned = _plan_legacy_import(
        blob,
        classifier_fn=lambda _text: pytest.fail("advisor must resolve this without LLM"),
    )
    item = planned[0]
    assert item["heading_hint_kind"] == "reply_style"
    assert is_behavioral_kind(item["kind"]) is True
    assert item["kind"] == "response_tone"
    assert item["ai_status"] == "approved"
    assert item["classification_source"] == "repair_advisor_behavioral"


def test_product_usage_heading_without_scope_is_not_global_ai_fact() -> None:
    from routers.knowledge import _plan_legacy_import

    blob = (
        "# طريقة الاستخدام\n"
        "يُرتدى الحذاء الرياضي الأبيض للمشي اليومي.\n"
    )
    planned = _plan_legacy_import(
        blob,
        classifier_fn=lambda _text: _classifier_kind("product_usage"),
    )
    item = planned[0]
    assert item["kind"] == "product_usage"
    assert item["ai_status"] == "needs_review"
    assert _ai_visible(item) is False
