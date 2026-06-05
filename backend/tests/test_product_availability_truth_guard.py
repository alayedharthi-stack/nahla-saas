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
    _CONFLICT_REPLY_AR,
    _UNKNOWN_REPLY_AR,
    apply_product_availability_truth_guard,
    product_availability_guard_mode,
    reply_availability_polarity,
)


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

    def test_family_mixed_availability(self) -> None:
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
        assert ev.evidence_state in (EVIDENCE_CONFLICT, EVIDENCE_RESOLVED_UNAVAILABLE, EVIDENCE_UNKNOWN)

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
        result = apply_product_availability_truth_guard(
            reply=reply,
            availability_context=_ctx(
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
            ),
            tenant_id=99,
        )
        assert result.replaced is True
        assert result.reply == _CONFLICT_REPLY_AR

    def test_enforce_rewrites_unknown(self) -> None:
        reply = "\u0645\u062a\u0648\u0641\u0631"
        result = apply_product_availability_truth_guard(
            reply=reply,
            availability_context=_ctx(skus=[_sku(60, "Iota Product", checkout=True)], connected=True),
            inbound_text="generic greeting",
            tenant_id=99,
        )
        assert result.replaced is True
        assert result.reply == _UNKNOWN_REPLY_AR

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
