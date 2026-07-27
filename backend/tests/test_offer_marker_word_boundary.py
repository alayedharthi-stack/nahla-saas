"""The discount guard must not fire on verb forms that merely contain «عرض».

Production case: «من الكتالوج نقدر نعرض لك الأنسب» was rejected as
``invented_offer`` because «عرض» was matched as a bare substring inside the
verb «نعرض». Genuine discount wording must still be rejected.
"""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
for _p in (_BACKEND, os.path.join(_BACKEND, ".."), os.path.join(_BACKEND, "..", "database")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.persona.catalog_product_answer import (  # noqa: E402
    _build_catalog_navigation_bundle,
    build_catalog_product_answer_facts_bundle,
)
from modules.ai.brain.persona.compose_guards import (  # noqa: E402
    apply_persona_compose_guards,
)

_BROWSE = "وش المنتجات المتوفرة طيب؟"
_PRODUCTS = [
    {"id": 1, "title": "قميص قطني أزرق", "category": "ملابس", "price": 129,
     "can_checkout": True},
    {"id": 2, "title": "حذاء رياضي أبيض", "category": "أحذية", "price": 199,
     "can_checkout": True},
]


def _navigation_bundle():
    bundle, _rows = _build_catalog_navigation_bundle(
        tenant_id=9,
        customer_phone="966500000000",
        inbound_text=_BROWSE,
        products=[dict(p) for p in _PRODUCTS],
        navigator_no_groups_fallback=False,
        decision_args={},
        settings={},
    )
    return bundle


def _product_answer_bundle():
    return build_catalog_product_answer_facts_bundle(
        inbound_text=_BROWSE,
        tenant_id=9,
        products=[dict(p) for p in _PRODUCTS],
        question_kind="browse",
    )


_BUNDLES = {
    "navigation": _navigation_bundle,
    "product_answer": _product_answer_bundle,
}

# Verb/noun forms that merely contain the «عرض» letters and carry no offer.
_NOT_AN_OFFER = (
    "من الكتالوج نقدر نعرض لك الأنسب",
    "نقدر نستعرض لك الخيارات",
    "تعرض المنتجات في القائمة",
    "زوروا معرضنا",
    "يعرض الكتالوج كل الأصناف",
)

# Genuine discount/offer wording that must keep being rejected.
_IS_AN_OFFER = (
    "عندنا عرض خاص اليوم",
    "عندنا عروض قوية",
    "العرض ساري لليوم",
    "فيه خصم 20%",
    "عندنا تخفيض على المجموعة",
)


@pytest.mark.parametrize("surface", sorted(_BUNDLES))
@pytest.mark.parametrize("text", _NOT_AN_OFFER)
def test_verb_forms_containing_ard_are_not_invented_offer(surface: str, text: str) -> None:
    guard = apply_persona_compose_guards(text, _BUNDLES[surface]())
    assert guard.failed_reason != "invented_offer", (
        f"{surface}: «{text}» must not be treated as a discount claim"
    )


@pytest.mark.parametrize("surface", sorted(_BUNDLES))
@pytest.mark.parametrize("text", _IS_AN_OFFER)
def test_genuine_offer_wording_still_rejected(surface: str, text: str) -> None:
    guard = apply_persona_compose_guards(text, _BUNDLES[surface]())
    assert guard.passed is False
    assert guard.failed_reason == "invented_offer", (
        f"{surface}: «{text}» must still be rejected as an unsupported offer"
    )
