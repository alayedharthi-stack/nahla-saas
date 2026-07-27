"""Shadow-mode detector tests for ungrounded catalog product titles (#710)."""
from __future__ import annotations

import os
import sys
from typing import Any, Callable

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
    detect_ungrounded_product_titles,
)

BundleBuilder = Callable[[list[dict[str, Any]]], Any]

LEGITIMATE_REPLIES: tuple[str, ...] = (
    "عندنا عدة منتجات متوفرة",
    "لدينا تشكيلة رائعة من الملابس",
    "عندنا خيارات كثيرة تناسبك",
    "من الكتالوج نقدر نعرض لك الأنسب",
    "عندنا أقسام متنوعة",
    "لدينا منتجات مناسبة لك، تحب أرشح لك شي؟",
    "عندنا قميص قطني أزرق وحذاء رياضي أبيض",
    "لدينا قميص قطني أزرق بجودة ممتازة",
    "متوفر عندنا قميص قطني أزرق",
    "عندنا قميص قطني ازرق",
    "أبشر، عندنا حذاء رياضي أبيض جاهز للطلب",
    "من الكتالوج:\n• قميص قطني أزرق\n• حذاء رياضي أبيض",
)

FABRICATED_REPLIES: tuple[str, ...] = (
    "عندنا ساعة ذكية فاخرة",
    "لدينا نظارة شمسية كلاسيكية",
    "متوفر عندنا حقيبة سفر جلدية",
    "عندنا ساعة ذكية موديل زد-٩٩٩",
    "لدينا ثلاجة سامسونج الجديدة",
)

# EM review round 3: a service verb must never disable product-title grounding.
ADVERSARIAL_SERVICE_VERB_REPLIES: tuple[str, ...] = (
    "عندنا نقدر نوفر لك ساعة",
    "لدينا نقدر نعرض لك ثلاجة",
    "عندنا أقدر أرشح لك مكيف",
    "من الكتالوج نوفر لك نظارة",
)


def _product(*, product_id: int, title: str, price: int = 150) -> dict[str, Any]:
    return {
        "id": product_id,
        "external_id": f"ext-{product_id}",
        "title": title,
        "category": "ملابس",
        "price": price,
        "can_checkout": True,
        "orderable": True,
        "in_stock": True,
    }


def _generic_products() -> list[dict[str, Any]]:
    return [
        _product(product_id=5001, title="قميص قطني أزرق", price=120),
        _product(product_id=5002, title="حذاء رياضي أبيض", price=180),
        _product(product_id=5003, title="عطر ورد 100ml", price=220),
    ]


def _build_product_answer_bundle(products: list[dict[str, Any]]) -> Any:
    return build_catalog_product_answer_facts_bundle(
        inbound_text="وش المنتجات المتوفرة طيب؟",
        tenant_id=71,
        products=products,
        search_result_count=len(products),
    )


def _build_navigation_bundle(products: list[dict[str, Any]]) -> Any:
    bundle, _rows = _build_catalog_navigation_bundle(
        tenant_id=71,
        customer_phone="966500000071",
        inbound_text="وش المنتجات المتوفرة طيب؟",
        products=products,
        navigator_no_groups_fallback=False,
        decision_args={},
        settings={},
    )
    return bundle


BUNDLE_BUILDERS: list[tuple[str, BundleBuilder]] = [
    ("product_answer", _build_product_answer_bundle),
    ("navigation", _build_navigation_bundle),
]


def _assert_shadow_contract(reply: str, bundle: Any) -> None:
    guard = apply_persona_compose_guards(reply, bundle)
    assert guard.text == reply
    assert guard.failed_reason != "invented_product_title_shadow"


def _assert_shadow_unchanged(reply: str, bundle: Any, *, expect_pass: bool = True) -> None:
    guard = apply_persona_compose_guards(reply, bundle)
    assert guard.passed is expect_pass
    assert guard.text == reply
    assert guard.failed_reason != "invented_product_title_shadow"


@pytest.mark.parametrize("builder_name,build_bundle", BUNDLE_BUILDERS)
class TestCatalogInventedProductTitleShadow:
    """Presenter-slot grounding detector stays shadow-only across bundle builders."""

    @pytest.mark.parametrize("reply", LEGITIMATE_REPLIES)
    def test_legitimate_replies_do_not_fire(
        self,
        builder_name: str,
        build_bundle: BundleBuilder,
        reply: str,
    ) -> None:
        bundle = build_bundle(_generic_products())
        assert detect_ungrounded_product_titles(reply, bundle.verified_facts) == []
        _assert_shadow_contract(reply, bundle)

    def test_exact_approver_generic_with_trailing_word(
        self,
        builder_name: str,
        build_bundle: BundleBuilder,
    ) -> None:
        bundle = build_bundle(_generic_products())
        reply = "عندنا عدة منتجات متوفرة"
        assert detect_ungrounded_product_titles(reply, bundle.verified_facts) == []
        _assert_shadow_unchanged(reply, bundle)

    @pytest.mark.parametrize("reply", FABRICATED_REPLIES)
    def test_fabricated_replies_fire(
        self,
        builder_name: str,
        build_bundle: BundleBuilder,
        reply: str,
    ) -> None:
        bundle = build_bundle(_generic_products())
        detections = detect_ungrounded_product_titles(reply, bundle.verified_facts)
        assert len(detections) == 1
        assert detections[0]["reason"] == "invented_product_title_shadow"
        assert detections[0]["phrase"]
        assert detections[0]["phrase"] is not None
        assert detections[0]["mixed"] is False
        assert detections[0]["catalog_empty"] is False
        assert detections[0]["would_reject_enforce"] is True
        _assert_shadow_unchanged(reply, bundle)

    @pytest.mark.parametrize("reply", ADVERSARIAL_SERVICE_VERB_REPLIES)
    def test_service_verb_does_not_disable_grounding(
        self,
        builder_name: str,
        build_bundle: BundleBuilder,
        reply: str,
    ) -> None:
        # EM review round 3: prefixing a fabricated product with a service verb must not bypass detection.
        bundle = build_bundle(_generic_products())
        detections = detect_ungrounded_product_titles(reply, bundle.verified_facts)
        assert len(detections) == 1
        assert detections[0]["reason"] == "invented_product_title_shadow"
        assert detections[0]["phrase"]
        _assert_shadow_contract(reply, bundle)

    def test_detection_dict_populates_phrase_field(
        self,
        builder_name: str,
        build_bundle: BundleBuilder,
    ) -> None:
        bundle = build_bundle(_generic_products())
        detections = detect_ungrounded_product_titles(
            "عندنا ساعة ذكية فاخرة",
            bundle.verified_facts,
        )
        assert len(detections) == 1
        detection = detections[0]
        assert detection["phrase"] == "ساعه ذكيه فاخره"
        assert detection["phrase"] is not None

    def test_mixed_real_and_fabricated_detects_only_fabricated_conjunct(
        self,
        builder_name: str,
        build_bundle: BundleBuilder,
    ) -> None:
        bundle = build_bundle(_generic_products())
        reply = "عندنا قميص قطني أزرق، حذاء رياضي أبيض، وساعة ذكية فاخرة"
        detections = detect_ungrounded_product_titles(reply, bundle.verified_facts)
        assert len(detections) == 1
        assert detections[0]["mixed"] is True
        assert detections[0]["phrase"]
        assert "ساعه" in detections[0]["phrase"]
        assert "قميص" not in detections[0]["phrase"]
        assert "حذاء" not in detections[0]["phrase"]
        _assert_shadow_unchanged(reply, bundle)

    def test_empty_catalog_generic_prose_no_detection(
        self,
        builder_name: str,
        build_bundle: BundleBuilder,
    ) -> None:
        bundle = build_bundle([])
        reply = "عندنا عدة منتجات متوفرة"
        assert detect_ungrounded_product_titles(reply, bundle.verified_facts) == []
        guard = apply_persona_compose_guards(reply, bundle)
        assert guard.text == reply
        assert guard.failed_reason == "invented_availability"

    def test_empty_catalog_named_claim_detected(
        self,
        builder_name: str,
        build_bundle: BundleBuilder,
    ) -> None:
        bundle = build_bundle([])
        reply = "عندنا قميص قطني أزرق"
        detections = detect_ungrounded_product_titles(reply, bundle.verified_facts)
        assert len(detections) == 1
        assert detections[0]["catalog_empty"] is True
        assert detections[0]["mixed"] is False
        assert detections[0]["phrase"]
        _assert_shadow_unchanged(reply, bundle)

    def test_real_title_with_fabricated_price_still_fails_invented_price(
        self,
        builder_name: str,
        build_bundle: BundleBuilder,
    ) -> None:
        bundle = build_bundle(_generic_products())
        reply = "عندنا قميص قطني أزرق بسعر 999 ريال"
        guard = apply_persona_compose_guards(reply, bundle)
        assert guard.passed is False
        assert guard.failed_reason == "invented_price"
        assert guard.text == reply
        assert detect_ungrounded_product_titles(reply, bundle.verified_facts) == []
