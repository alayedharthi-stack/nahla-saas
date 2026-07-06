"""Platform-wide product claim grounding guard tests."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")):
    if p not in sys.path:
        sys.path.insert(0, p)

from modules.ai.brain.postprocess.product_claim_grounding_evidence import (  # noqa: E402
    ProductClaimGroundingEvidence,
    scan_recent_catalog_miss_signals,
)
from modules.ai.brain.postprocess.product_claim_grounding_guard import (  # noqa: E402
    _detect_violations,
    apply_product_claim_grounding_guard,
    product_claim_grounding_guard_mode,
)


def _evidence(**overrides: Any) -> ProductClaimGroundingEvidence:
    base = dict(
        grounded_prices=frozenset({300, 400}),
        grounded_text_corpus="",
        available_products=(
            {"id": 1, "title": "منتج أ — طلح", "can_checkout": True},
            {"id": 2, "title": "منتج ب — سمر", "can_checkout": True},
        ),
        unavailable_products=(
            {"id": 3, "title": "منتج ج — سدر", "can_checkout": False},
        ),
        catalog_products_this_turn=False,
        catalog_miss_this_turn=False,
        recent_catalog_miss=False,
        recent_no_synced=False,
        has_checkout_catalog=True,
        executor_product_ids=frozenset(),
        kb_section_ids=frozenset(),
    )
    base.update(overrides)
    return ProductClaimGroundingEvidence(**base)


class TestComparisonGrounding:
    def test_comparison_sweetness_blocked_without_source(self) -> None:
        reply = (
            "السدر أقل حلاوة من الطلح بكثير. "
            "إذا تفضل العسل الأقل حلاوة، السدر الخيار الأفضل."
        )
        violations = _detect_violations(reply, _evidence())
        kinds = {v[0] for v in violations}
        assert "ungrounded_comparison" in kinds

    def test_comparison_allowed_when_kb_corpus_contains_claim(self) -> None:
        reply = "حسب وصف المتجر، هذا المنتج أقل حلاوة من البديل."
        ev = _evidence(grounded_text_corpus="وصف المنتج اقل حلاوه من البديل")
        violations = _detect_violations(reply, ev)
        assert not any(v[0] == "ungrounded_comparison" for v in violations)


class TestUnavailableRecommendation:
    def test_unavailable_product_not_recommended(self) -> None:
        reply = "السدر الخيار الأفضل لك. تبي أرسل لك تفاصيل السدر؟"
        violations = _detect_violations(reply, _evidence())
        assert any(v[0] == "unavailable_promoted" for v in violations)

    def test_unavailable_mentioned_as_unavailable_without_recommend_ok(self) -> None:
        reply = "السدر غير متوفر حالياً. المتوفر الآن الطلح والسمر."
        violations = _detect_violations(reply, _evidence())
        assert not any(v[0] == "unavailable_promoted" for v in violations)


class TestPriceGrounding:
    def test_generated_prices_blocked_after_catalog_miss(self) -> None:
        reply = "عسل الطلح: 300 ريال (نص كيلو) و 400 ريال (كيلو)"
        ev = _evidence(
            recent_catalog_miss=True,
            catalog_products_this_turn=False,
        )
        violations = _detect_violations(reply, ev)
        kinds = {v[0] for v in violations}
        assert "ungrounded_price" in kinds
        assert "contradiction_after_catalog_miss" in kinds

    def test_prices_allowed_when_executor_returned_products_this_turn(self) -> None:
        reply = "• منتج أ — 300 ريال"
        ev = _evidence(
            recent_catalog_miss=True,
            catalog_products_this_turn=True,
            executor_product_ids=frozenset({1}),
        )
        violations = _detect_violations(reply, ev)
        assert not any(v[0] == "ungrounded_price" for v in violations)

    def test_price_not_invented_when_missing_from_catalog(self) -> None:
        reply = "سعر المنتج 999 ريال"
        ev = _evidence(grounded_prices=frozenset({300, 400}))
        violations = _detect_violations(reply, ev)
        assert any(v[0] == "ungrounded_price" for v in violations)


class TestContradictionGuard:
    def test_no_synced_then_prices_blocked(self) -> None:
        history = [
            {"direction": "outbound", "body": "لا توجد منتجات مزامنة الآن."},
        ]
        recent_miss, recent_no_sync = scan_recent_catalog_miss_signals(history)
        assert recent_no_sync is True
        reply = "عندنا منتج أ ب 300 ريال"
        ev = _evidence(
            recent_no_synced=True,
            has_checkout_catalog=False,
            grounded_prices=frozenset(),
        )
        violations = _detect_violations(reply, ev)
        assert any(v[0] == "contradiction_no_synced" for v in violations)


class TestMedicalClaims:
    def test_medical_claim_blocked_without_kb(self) -> None:
        reply = "الطلح مشهور للصحة العامة والنشاط والصدر."
        violations = _detect_violations(reply, _evidence())
        assert any(v[0] == "ungrounded_medical" for v in violations)

    def test_medical_claim_allowed_when_in_merchant_kb_corpus(self) -> None:
        reply = "حسب وصف المتجر، يُفضله بعض العملاء للاستخدام اليومي."
        ev = _evidence(grounded_text_corpus="مفيد للصحه العامه والنشاط")
        violations = _detect_violations(reply, ev)
        assert not any(v[0] == "ungrounded_medical" for v in violations)


class TestBestPick:
    def test_best_type_recommendation_requires_catalog_source(self) -> None:
        reply = "بالنسبة للنوع الأفضل، منتج أ يتميز بقوته."
        violations = _detect_violations(reply, _evidence())
        assert any(v[0] == "ungrounded_best_pick" for v in violations)


class TestVariantPriceGrounding:
    def test_variant_price_allowed_when_grounded_in_catalog_turn(self) -> None:
        reply = "• منتج أ — نصف كيلو: 400 ريال\n• منتج أ — كيلو: 300 ريال"
        ev = _evidence(
            grounded_prices=frozenset({300, 400}),
            catalog_products_this_turn=True,
            executor_product_ids=frozenset({1}),
        )
        violations = _detect_violations(reply, ev)
        assert not any(v[0] == "ungrounded_price" for v in violations)

    def test_executor_variant_prices_become_grounded_evidence(self) -> None:
        from modules.ai.brain.postprocess.product_claim_grounding_evidence import (  # noqa: PLC0415
            build_product_claim_grounding_evidence,
        )

        evidence = build_product_claim_grounding_evidence(
            None,
            1,
            availability_context={"catalog_skus": []},
            executor_products=[{
                "id": 10,
                "title": "SKU A",
                "variants": [
                    {"price": "400 SAR", "options": {"weight": "1 kg"}},
                    {"price": "300", "options": {"weight": "0.5 kg"}},
                ],
            }],
            chosen_path="variant_pricing",
        )
        assert evidence.grounded_prices == frozenset({300, 400})
        assert evidence.catalog_products_this_turn is True

    def test_apply_guard_keeps_grounded_variant_price_reply(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("NAHLA_PRODUCT_CLAIM_GROUNDING_GUARD_MODE", "enforce")

        def _fake_build(*_a: Any, **_k: Any) -> ProductClaimGroundingEvidence:
            return _evidence(
                grounded_prices=frozenset({400}),
                catalog_products_this_turn=True,
                executor_product_ids=frozenset({1}),
            )

        monkeypatch.setattr(
            "modules.ai.brain.postprocess.product_claim_grounding_guard.build_product_claim_grounding_evidence",
            _fake_build,
        )
        reply = "نصف كيلو بـ 400 ريال"
        result = apply_product_claim_grounding_guard(
            reply=reply,
            chosen_path="variant_pricing",
            tenant_id=1,
        )
        assert result.replaced is False
        assert result.reply == reply


class TestApplyGuard:
    def test_enforce_rewrites_ungrounded_comparison(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NAHLA_PRODUCT_CLAIM_GROUNDING_GUARD_MODE", "enforce")
        assert product_claim_grounding_guard_mode() == "enforce"

        def _fake_build(*_a: Any, **_k: Any) -> ProductClaimGroundingEvidence:
            return _evidence()

        monkeypatch.setattr(
            "modules.ai.brain.postprocess.product_claim_grounding_guard.build_product_claim_grounding_evidence",
            _fake_build,
        )
        result = apply_product_claim_grounding_guard(
            reply="المنتج أ أقل حلاوة من المنتج ب.",
            tenant_id=1,
        )
        assert result.replaced is True
        assert "يختلف الطعم" in result.reply

    def test_variant_pricing_path_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NAHLA_PRODUCT_CLAIM_GROUNDING_GUARD_MODE", "enforce")
        result = apply_product_claim_grounding_guard(
            reply="300 ريال — نصف كيلو",
            chosen_path="variant_pricing",
            tenant_id=1,
        )
        assert result.replaced is False
        assert result.action == "allowed"


class TestPriceNormalization:
    def test_parse_price_amount_arabic_formatted(self) -> None:
        from modules.ai.brain.postprocess.product_claim_grounding_evidence import (  # noqa: PLC0415
            parse_price_amount,
        )

        assert parse_price_amount("387") == 387
        assert parse_price_amount("387.00") == 387
        assert parse_price_amount("ر.س. ٣٨٧٫٠٠") == 387
        assert parse_price_amount("١٬٤٧٥٫٠٠") == 1475
        assert parse_price_amount("1,475.00") == 1475


class TestCatalogFactPriceGrounding:
    def test_catalog_fact_products_prices_in_evidence(self) -> None:
        from modules.ai.brain.postprocess.product_claim_grounding_evidence import (  # noqa: PLC0415
            build_product_claim_grounding_evidence,
        )

        evidence = build_product_claim_grounding_evidence(
            None,
            33,
            availability_context={"catalog_skus": []},
            executor_products=[],
            catalog_fact_products=[{
                "id": 109,
                "title": "عسل طلح نجد البري",
                "price": "ر.س. ٣٨٧٫٠٠",
                "can_checkout": False,
            }],
            inbound_metadata={
                "price_source": "catalog",
                "catalog_product_ids": [109],
            },
        )
        assert 387 in evidence.grounded_prices

    def test_talh_deterministic_fallback_not_rewritten(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("NAHLA_PRODUCT_CLAIM_GROUNDING_GUARD_MODE", "enforce")
        reply = (
            "من الكتالوج:\n"
            "• عسل طلح نجد البري إنتاج منحلنا  1 كيلو سعره 387 ريال، "
            "والمنتج غير متاح للطلب حالياً"
        )
        result = apply_product_claim_grounding_guard(
            reply=reply,
            tenant_id=33,
            chosen_path="fact_bound_persona_compose",
            catalog_fact_products=[{
                "id": 109,
                "title": "عسل طلح نجد البري",
                "price": "ر.س. ٣٨٧٫٠٠",
                "can_checkout": False,
            }],
            inbound_metadata={
                "question_kind": "price",
                "price_source": "catalog",
                "checkout_pressure_allowed": False,
                "catalog_product_ids": [109],
                "persona_compose": {
                    "surface": "catalog_product_answer",
                    "source": "catalog_deterministic_fallback",
                },
            },
        )
        assert result.replaced is False
        assert "387" in result.reply
        assert "ما ظهر عندي سعر مؤكد" not in result.reply

    def test_generic_ungrounded_price_still_rewrites(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("NAHLA_PRODUCT_CLAIM_GROUNDING_GUARD_MODE", "enforce")

        def _fake_build(*_a: Any, **_k: Any) -> ProductClaimGroundingEvidence:
            return _evidence(grounded_prices=frozenset({300, 400}))

        monkeypatch.setattr(
            "modules.ai.brain.postprocess.product_claim_grounding_guard.build_product_claim_grounding_evidence",
            _fake_build,
        )
        result = apply_product_claim_grounding_guard(
            reply="سعر المنتج 999 ريال",
            tenant_id=1,
            chosen_path="search_products",
        )
        assert result.replaced is True
        assert "ما ظهر عندي سعر مؤكد" in result.reply
