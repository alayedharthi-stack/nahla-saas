"""Tests for product availability truth guard — platform-wide, synthetic fixtures."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")):
    if p not in sys.path:
        sys.path.insert(0, p)

from modules.ai.brain.postprocess.product_availability_evidence import (  # noqa: E402
    CONFLICT_FAMILY_MIXED,
    CONFLICT_KB_AVAILABLE_CATALOG_UNAVAILABLE,
    CONFLICT_MISSING_CATALOG_ENTITY,
    CONFLICT_YEAR_MISMATCH,
    EVIDENCE_CONFLICT,
    EVIDENCE_RESOLVED_AVAILABLE,
    EVIDENCE_RESOLVED_UNAVAILABLE,
    EVIDENCE_UNKNOWN,
    evaluate_product_availability_evidence,
)
from modules.ai.brain.postprocess.product_availability_truth_guard import (  # noqa: E402
    _LEGACY_CONFLICT_REPLY_AR,
    _UNKNOWN_REPLY_AR,
    apply_product_availability_truth_guard,
    build_friendly_availability_conflict_reply,
    customer_facing_availability_reply_is_clean,
    product_availability_guard_mode,
    reply_availability_polarity,
)
from modules.ai.brain.turn_owner_contract import TOPIC_SHIPPING  # noqa: E402


def _sku(
    pid: int,
    title: str,
    *,
    checkout: bool,
    years: list | None = None,
    family: str = "",
) -> dict:
    from core.product_entity_resolution import family_key_from_title  # noqa: E402

    return {
        "id": pid,
        "title": title,
        "sku": f"SKU-{pid}",
        "external_id": f"ext-{pid}",
        "can_checkout": checkout,
        "in_stock": checkout,
        "years": years or [],
        "weights": [],
        "family_key": family or family_key_from_title(title),
    }


def _ctx(
    *,
    skus: list,
    focus: dict | None = None,
    kb: list | None = None,
    links: list | None = None,
    connected: bool = True,
) -> dict:
    return {
        "platform_connected": connected,
        "focus_product": focus,
        "recommended_product_ids": [],
        "catalog_skus": skus,
        "kb_signals": kb or [],
        "product_links": links or [],
    }


class TestEvidenceStates:
    def test_resolved_available(self) -> None:
        ev = evaluate_product_availability_evidence(
            availability_context=_ctx(
                skus=[_sku(1, "Alpha Widget 2025 large", checkout=True, years=["2025"])],
                focus={"id": 1, "title": "Alpha Widget 2025 large"},
            ),
        )
        assert ev.evidence_state == EVIDENCE_RESOLVED_AVAILABLE
        assert ev.evidence_ok_for_positive is True

    def test_resolved_unavailable(self) -> None:
        ev = evaluate_product_availability_evidence(
            availability_context=_ctx(
                skus=[_sku(2, "Beta Unit 2024", checkout=False, years=["2024"])],
                focus={"id": 2, "title": "Beta Unit 2024"},
            ),
        )
        assert ev.evidence_state == EVIDENCE_RESOLVED_UNAVAILABLE
        assert ev.evidence_ok_for_negative is True

    def test_kb_available_catalog_unavailable_conflict(self) -> None:
        ev = evaluate_product_availability_evidence(
            availability_context=_ctx(
                skus=[_sku(3, "Gamma Line 2025", checkout=False, years=["2025"])],
                focus={"id": 3, "title": "Gamma Line 2025"},
                kb=[{
                    "section_id": 10,
                    "kind": "quick_update",
                    "avail_polarity": "positive",
                    "primary_year": "2025",
                    "linked_product_ids": [3],
                }],
                links=[{"section_id": 10, "product_id": 3, "source": "manual", "confidence": None}],
            ),
        )
        assert ev.evidence_state == EVIDENCE_CONFLICT
        assert ev.conflict_type == CONFLICT_KB_AVAILABLE_CATALOG_UNAVAILABLE

    def test_missing_catalog_entity_year(self) -> None:
        fam = "gamma|line"
        ev = evaluate_product_availability_evidence(
            availability_context=_ctx(
                skus=[
                    _sku(4, "Gamma Line 2024 edition", checkout=True, years=["2024"], family=fam),
                ],
                focus={"id": 4, "title": "Gamma Line 2024 edition"},
                kb=[{
                    "section_id": 11,
                    "kind": "quick_update",
                    "avail_polarity": "positive",
                    "primary_year": "2025",
                    "linked_product_ids": [4],
                }],
                links=[{"section_id": 11, "product_id": 4, "source": "ai_fuzzy_match", "confidence": 0.6}],
            ),
        )
        assert ev.evidence_state == EVIDENCE_CONFLICT
        assert ev.conflict_type in (CONFLICT_YEAR_MISMATCH, CONFLICT_MISSING_CATALOG_ENTITY)

    def test_family_mixed_availability_is_variant_options(self) -> None:
        from modules.ai.brain.postprocess.product_availability_evidence import (  # noqa: PLC0415
            EVIDENCE_VARIANT_OPTIONS,
        )

        fam = "delta|series"
        ev = evaluate_product_availability_evidence(
            availability_context=_ctx(
                skus=[
                    _sku(10, "Delta Series small", checkout=False, family=fam),
                    _sku(11, "Delta Series large", checkout=True, family=fam),
                ],
                focus=None,
                kb=[{
                    "section_id": 12,
                    "kind": "quick_update",
                    "avail_polarity": "positive",
                    "primary_year": None,
                    "linked_product_ids": [],
                }],
            ),
            inbound_text="Delta Series small",
        )
        assert ev.evidence_state == EVIDENCE_VARIANT_OPTIONS
        assert ev.reason == "family_variant_options"

    def test_unknown_no_catalog(self) -> None:
        ev = evaluate_product_availability_evidence(
            availability_context=_ctx(skus=[], connected=False),
        )
        assert ev.evidence_state == EVIDENCE_UNKNOWN

    def test_unknown_unresolved_entity(self) -> None:
        ev = evaluate_product_availability_evidence(
            availability_context=_ctx(
                skus=[_sku(20, "Epsilon Model A", checkout=True)],
            ),
            inbound_text="hello",
        )
        assert ev.evidence_state == EVIDENCE_UNKNOWN


class TestGuardShadowMode:
    def setup_method(self) -> None:
        self._prev = os.environ.get("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE")
        os.environ["NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE"] = "shadow"

    def teardown_method(self) -> None:
        if self._prev is None:
            os.environ.pop("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE", None)
        else:
            os.environ["NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE"] = self._prev

    def test_shadow_does_not_rewrite_conflict(self) -> None:
        reply = "\u063a\u064a\u0631 \u0645\u062a\u0648\u0641\u0631"
        result = apply_product_availability_truth_guard(
            reply=reply,
            availability_context=_ctx(
                skus=[_sku(30, "Zeta Product size A", checkout=False)],
                focus={"id": 30, "title": "Zeta Product size A"},
                kb=[{
                    "section_id": 20,
                    "kind": "quick_update",
                    "avail_polarity": "positive",
                    "primary_year": None,
                    "linked_product_ids": [30],
                }],
                links=[{"section_id": 20, "product_id": 30, "source": "manual", "confidence": None}],
            ),
            inbound_text="Zeta Product size A",
            tenant_id=99,
            conversation_id=1,
        )
        assert result.replaced is False
        assert result.reply == reply
        assert result.would_rewrite is True
        assert result.shadow_mode is True

    def test_shadow_logs_resolved_allowed(self) -> None:
        reply = "\u0645\u062a\u0648\u0641\u0631"
        result = apply_product_availability_truth_guard(
            reply=reply,
            availability_context=_ctx(
                skus=[_sku(40, "Eta Item 2025", checkout=True, years=["2025"])],
                focus={"id": 40, "title": "Eta Item 2025"},
            ),
            tenant_id=99,
        )
        assert result.replaced is False
        assert result.action == "allowed"


class TestGuardEnforceMode:
    def setup_method(self) -> None:
        self._prev = os.environ.get("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE")
        os.environ["NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE"] = "enforce"

    def teardown_method(self) -> None:
        if self._prev is None:
            os.environ.pop("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE", None)
        else:
            os.environ["NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE"] = self._prev

    def test_enforce_rewrites_conflict(self) -> None:
        reply = "\u063a\u064a\u0631 \u0645\u062a\u0648\u0641\u0631"
        ctx = _ctx(
            skus=[_sku(50, "Theta Model 2025", checkout=False, years=["2025"])],
            focus={"id": 50, "title": "Theta Model 2025"},
            kb=[{
                "section_id": 30,
                "kind": "quick_update",
                "avail_polarity": "positive",
                "primary_year": "2025",
                "linked_product_ids": [50],
            }],
            links=[{"section_id": 30, "product_id": 50, "source": "manual", "confidence": None}],
        )
        result = apply_product_availability_truth_guard(
            reply=reply,
            availability_context=ctx,
            tenant_id=99,
        )
        assert result.replaced is False
        assert result.reply == reply
        assert result.reason == "honest_negative_unresolved_preserved"

    def test_enforce_rewrites_unknown(self) -> None:
        reply = "\u0645\u062a\u0648\u0641\u0631"
        result = apply_product_availability_truth_guard(
            reply=reply,
            availability_context=_ctx(skus=[_sku(60, "Iota Product", checkout=True)], connected=True),
            inbound_text="\u0647\u0644 \u0627\u0644\u0645\u0646\u062a\u062c \u0645\u062a\u0648\u0641\u0631\u061f",
            tenant_id=99,
        )
        assert result.replaced is True
        assert result.reply == _UNKNOWN_REPLY_AR
        assert customer_facing_availability_reply_is_clean(result.reply)

    def test_enforce_does_not_rewrite_resolved_available_positive(self) -> None:
        reply = "\u0645\u062a\u0648\u0641\u0631"
        result = apply_product_availability_truth_guard(
            reply=reply,
            availability_context=_ctx(
                skus=[_sku(70, "Kappa Unit", checkout=True)],
                focus={"id": 70, "title": "Kappa Unit"},
            ),
            tenant_id=99,
        )
        assert result.replaced is False
        assert result.reply == reply


class TestPolarityDetection:
    def test_positive_and_negative_markers(self) -> None:
        assert reply_availability_polarity("\u0645\u062a\u0648\u0641\u0631 \u0627\u0644\u0622\u0646") == "positive"
        assert reply_availability_polarity("\u063a\u064a\u0631 \u0645\u062a\u0648\u0641\u0631") == "negative"
        assert reply_availability_polarity("\u0645\u0631\u062d\u0628\u0627") is None

    def test_guard_off_by_default(self) -> None:
        os.environ.pop("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE", None)
        os.environ.pop("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD", None)
        assert product_availability_guard_mode() == "off"


class TestAvailabilityContextBuilder:
    def test_module_compiles(self) -> None:
        import py_compile
        from pathlib import Path

        path = (
            Path(__file__).resolve().parents[1]
            / "modules/ai/brain/postprocess/availability_context_builder.py"
        )
        py_compile.compile(str(path), doraise=True)

    def test_can_checkout_respects_merchant_hidden(self) -> None:
        from datetime import datetime, timezone
        from types import SimpleNamespace

        from modules.ai.brain.postprocess.availability_context_builder import (  # noqa: E402
            _can_checkout_from_row,
        )

        hidden = SimpleNamespace(
            external_id="27449738824609642",
            extra_metadata={"status": "active", "in_stock": True},
            in_stock=True,
            catalog_status="merchant_hidden",
            merchant_hidden_at=datetime.now(timezone.utc),
            meta_removed_at=None,
            archived_at=None,
        )
        active = SimpleNamespace(
            external_id="27310682888555270",
            extra_metadata={"status": "active", "in_stock": True},
            in_stock=True,
            catalog_status="active",
            merchant_hidden_at=None,
            meta_removed_at=None,
            archived_at=None,
        )
        assert _can_checkout_from_row(hidden) is False
        assert _can_checkout_from_row(active) is True


class TestInactiveCatalogLineStrip:
    """Regression: tenant-33 style availability list must drop hidden SKUs."""

    def setup_method(self) -> None:
        self._prev = os.environ.get("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE")
        os.environ["NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE"] = "enforce"

    def teardown_method(self) -> None:
        if self._prev is None:
            os.environ.pop("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE", None)
        else:
            os.environ["NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE"] = self._prev

    def _tenant33_skus(self) -> list:
        return [
            _sku(
                103,
                "\u0639\u0633\u0644 \u0627\u0644\u0636\u064f\u0631\u0645 \u0627\u0644\u062c\u0628\u0644\u064a",
                checkout=False,
            ),
            _sku(
                109,
                "\u0639\u0633\u0644 \u0637\u0644\u062d \u0646\u062c\u062f \u0627\u0644\u0628\u0631\u064a \u0625\u0646\u062a\u0627\u062c \u0645\u0646\u062d\u0644\u0646\u0627  1 \u0643\u064a\u0644\u0648",
                checkout=True,
            ),
            _sku(
                111,
                "\u0639\u0633\u0644 \u0633\u0645\u0631 \u0627\u0644\u062d\u062c\u0627\u0632 \u0625\u0646\u062a\u0627\u062c \u0642\u062f\u064a\u0645",
                checkout=True,
            ),
        ]

    def test_enforce_strips_hidden_dharm_from_availability_list(self) -> None:
        raw = (
            "\u0639\u0646\u062f\u0646\u0627 \u062d\u0627\u0644\u064a\u0627\u064b:\n\n"
            "\u2022 \u0627\u0644\u0637\u0644\u062d \u0627\u0644\u0628\u0644\u062f\u064a\n"
            "\u2022 \u0633\u0645\u0631 \u0627\u0644\u062d\u062c\u0627\u0632 (\u062c\u062f\u064a\u062f 1447 + \u0642\u062f\u064a\u0645 1446)\n"
            "\u2022 \u0627\u0644\u0636\u064f\u0631\u0645 \u0627\u0644\u062c\u0628\u0644\u064a\n\n"
            "\u0648\u0628\u0639\u062f \u0641\u064a\u0647 \u0645\u0646\u062a\u062c\u0627\u062a \u0646\u062d\u0644"
        )
        ctx = _ctx(skus=self._tenant33_skus())
        result = apply_product_availability_truth_guard(
            reply=raw,
            availability_context=ctx,
            inbound_text="\u0648\u0634 \u0627\u0644\u0645\u062a\u0648\u0641\u0631 \u0627\u0644\u0627\u0646",
            tenant_id=33,
        )
        assert result.replaced is True
        assert "\u0627\u0644\u0636\u064f\u0631\u0645" not in result.reply
        assert "\u0627\u0644\u0637\u0644\u062d" in result.reply
        assert "\u0633\u0645\u0631 \u0627\u0644\u062d\u062c\u0627\u0632" in result.reply
        assert result.action == "strip_inactive_catalog_lines"

    def test_enforce_keeps_active_talh_when_focused(self) -> None:
        reply = "\u0645\u0646 \u0623\u0642\u0648\u0649 \u0627\u0644\u0623\u0646\u0648\u0627\u0639 \u0639\u0646\u062f\u0646\u0627"
        ctx = _ctx(
            skus=self._tenant33_skus(),
            focus={
                "id": 109,
                "title": "\u0639\u0633\u0644 \u0637\u0644\u062d \u0646\u062c\u062f \u0627\u0644\u0628\u0631\u064a \u0625\u0646\u062a\u0627\u062c \u0645\u0646\u062d\u0644\u0646\u0627  1 \u0643\u064a\u0644\u0648",
            },
        )
        result = apply_product_availability_truth_guard(
            reply=reply,
            availability_context=ctx,
            inbound_text="\u0627\u0628\u064a \u0635\u0648\u0631\u0629 \u0627\u0644\u0637\u0644\u062d",
            tenant_id=33,
        )
        assert result.replaced is False
        assert result.reply == reply


class TestShippingInquiryGuardBypass:
    """Regression: shipping fee replies must not be rewritten as product availability."""

    _SHIPPING_REPLY = (
        "نعم، الشحن إلى جدة متوفر وتكلفته 35 ريال."
    )

    def setup_method(self) -> None:
        self._prev = os.environ.get("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE")
        os.environ["NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE"] = "enforce"

    def teardown_method(self) -> None:
        if self._prev is None:
            os.environ.pop("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE", None)
        else:
            os.environ["NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE"] = self._prev

    def test_shipping_topic_preserves_fee_reply_with_availability_marker(self) -> None:
        result = apply_product_availability_truth_guard(
            reply=self._SHIPPING_REPLY,
            availability_context=_ctx(skus=[], connected=False),
            inbound_text="كم سعر الشحن لجدة؟",
            decision_topic=TOPIC_SHIPPING,
            tenant_id=99,
        )
        assert result.reply == self._SHIPPING_REPLY
        assert result.replaced is False
        assert result.action == "allowed_shipping_inquiry"
        assert result.availability_claim_blocked is False

    def test_non_shipping_topic_preserves_shipping_fee_reply(self) -> None:
        result = apply_product_availability_truth_guard(
            reply=self._SHIPPING_REPLY,
            availability_context=_ctx(skus=[], connected=False),
            inbound_text="كم سعر الشحن لجدة؟",
            tenant_id=99,
        )
        assert result.replaced is False
        assert result.reply == self._SHIPPING_REPLY
        assert "35" in result.reply
        assert result.reason == "topic_scope_skip_full_rewrite"


class TestCatalogProductFactAnswerExempt:
    def setup_method(self) -> None:
        self._prev = os.environ.get("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE")
        os.environ["NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE"] = "enforce"

    def teardown_method(self) -> None:
        if self._prev is None:
            os.environ.pop("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE", None)
        else:
            os.environ["NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE"] = self._prev

    def test_price_fact_answer_preserves_non_orderable_talh_line(self) -> None:
        reply = (
            "من الكتالوج:\n"
            "• عسل طلح نجد البري إنتاج منحلنا  1 كيلو سعره 387 ريال، "
            "والمنتج غير متاح للطلب حالياً"
        )
        ctx = _ctx(
            skus=[
                _sku(
                    109,
                    "عسل طلح نجد البري إنتاج منحلنا  1 كيلو",
                    checkout=False,
                )
            ],
        )
        result = apply_product_availability_truth_guard(
            reply=reply,
            availability_context=ctx,
            inbound_text="كم سعر الطلح؟",
            chosen_path="fact_bound_persona_compose",
            question_kind="price",
            catalog_product_ids=[109],
            checkout_pressure_allowed=False,
            surface="catalog_product_answer",
            tenant_id=33,
        )
        assert result.replaced is False
        assert "387" in result.reply
        assert "غير متاح للطلب" in result.reply

    def test_browse_availability_list_still_strips_non_orderable(self) -> None:
        raw = (
            "عندنا حالياً:\n\n"
            "• عسل طلح نجد البري\n"
            "• عسل سمر الحجاز\n"
            "• عسل الضُرم الجبلي"
        )
        ctx = _ctx(
            skus=[
                _sku(109, "عسل طلح نجد البري", checkout=False),
                _sku(111, "عسل سمر الحجاز", checkout=True),
                _sku(103, "عسل الضُرم الجبلي", checkout=False),
            ],
        )
        result = apply_product_availability_truth_guard(
            reply=raw,
            availability_context=ctx,
            inbound_text="وش المتوفر الان؟",
            tenant_id=33,
        )
        assert result.replaced is True
        assert "الضُرم" not in result.reply


class TestVariantPricingPathAllow:
    """Regression: variant_pricing must not strip trusted OOS price lines."""

    _OOS_SHIRT_TITLE = "قميص قطني أزرق"
    _VARIANT_PRICE_REPLY = "قميص قطني أزرق: 99 ريال"

    def setup_method(self) -> None:
        self._prev = os.environ.get("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE")
        os.environ["NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE"] = "enforce"

    def teardown_method(self) -> None:
        if self._prev is None:
            os.environ.pop("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE", None)
        else:
            os.environ["NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE"] = self._prev

    def _oos_shirt_ctx(self) -> dict:
        return _ctx(
            skus=[_sku(5, self._OOS_SHIRT_TITLE, checkout=False)],
            focus={"id": 5, "title": self._OOS_SHIRT_TITLE, "external_id": "sku-shirt-blue"},
        )

    def test_variant_pricing_preserves_oos_price_line(self) -> None:
        result = apply_product_availability_truth_guard(
            reply=self._VARIANT_PRICE_REPLY,
            availability_context=self._oos_shirt_ctx(),
            inbound_text="كم سعره؟",
            chosen_path="variant_pricing",
            tenant_id=99,
        )
        assert result.reply == self._VARIANT_PRICE_REPLY
        assert result.action == "allowed"
        assert result.replaced is False

    def test_non_allow_path_still_strips_oos_price_line(self) -> None:
        result = apply_product_availability_truth_guard(
            reply=self._VARIANT_PRICE_REPLY,
            availability_context=self._oos_shirt_ctx(),
            inbound_text="كم سعره؟",
            chosen_path="",
            tenant_id=99,
        )
        assert result.replaced is True
        assert result.action == "strip_inactive_catalog_lines"
        assert result.reply == ""
