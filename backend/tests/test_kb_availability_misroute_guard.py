"""PR-KB1 + PR-KB2 — availability misroute and guard inversion (platform-wide)."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")):
    if p not in sys.path:
        sys.path.insert(0, p)

from modules.ai.brain.commerce.solution_seeking import (  # noqa: E402
    classify_solution_seeking_commerce,
)
from modules.ai.brain.commerce_reply_humanizer import (  # noqa: E402
    apply_commerce_reply_humanizer,
)
from modules.ai.brain.intent import rules  # noqa: E402
from modules.ai.brain.intent_priority.types import GOAL_PRODUCT_AVAILABILITY  # noqa: E402
from modules.ai.brain.postprocess.product_availability_evidence import (  # noqa: E402
    EVIDENCE_CONFLICT,
    EVIDENCE_RESOLVED_AVAILABLE,
    EVIDENCE_UNKNOWN,
    EVIDENCE_VARIANT_OPTIONS,
    evaluate_product_availability_evidence,
)
from modules.ai.brain.postprocess.product_availability_truth_guard import (  # noqa: E402
    _UNKNOWN_REPLY_AR,
    apply_product_availability_truth_guard,
    build_operational_availability_conflict_reply,
    reply_positive_options_claim,
)
from modules.ai.brain.types import INTENT_ASK_PRODUCT, INTENT_NEED_BASED_PRODUCT_ADVICE  # noqa: E402


def _sku(pid: int, title: str, *, checkout: bool) -> dict:
    from core.product_entity_resolution import family_key_from_title  # noqa: E402

    return {
        "id": pid,
        "title": title,
        "sku": f"SKU-{pid}",
        "external_id": f"ext-{pid}",
        "can_checkout": checkout,
        "in_stock": checkout,
        "years": [],
        "weights": [],
        "family_key": family_key_from_title(title),
    }


def _ctx(
    *,
    skus: list,
    kb: list | None = None,
    links: list | None = None,
    focus: dict | None = None,
) -> dict:
    return {
        "platform_connected": True,
        "focus_product": focus,
        "recommended_product_ids": [],
        "catalog_skus": skus,
        "kb_signals": kb or [],
        "product_links": links or [],
    }


def _variant_evidence(*, label: str = "Widget Line") -> MagicMock:
    ev = MagicMock()
    ev.evidence_state = EVIDENCE_VARIANT_OPTIONS
    ev.evidence_ok_for_positive = True
    ev.kb_avail_polarity = None
    ev.entity = SimpleNamespace(product_id=None, family_key="inbound:test")
    return ev


class TestKB1AvailabilityNotSolutionSeeking:
    @pytest.mark.parametrize(
        "message",
        [
            "في عندك طرود نحل؟",
            "عندكم طرود نحل؟",
            "هل يوجد طرود نحل؟",
        ],
    )
    def test_bee_package_availability_not_solution_seeking(self, message: str) -> None:
        assert classify_solution_seeking_commerce(message) is None

    def test_lam_inside_noun_does_not_trigger_structure(self) -> None:
        assert classify_solution_seeking_commerce("في عندك طرود نحل؟") is None

    @pytest.mark.parametrize(
        "message",
        [
            "أبي عسل للمناعة",
            "وش تنصحني للبرد",
            "عسل للسكر",
            "عندكم عسل مناسب للقولون؟",
        ],
    )
    def test_real_solution_seeking_still_matches(self, message: str) -> None:
        assert classify_solution_seeking_commerce(message) is not None

    @pytest.mark.parametrize(
        "message",
        [
            "في عندك طرود نحل؟",
            "عندكم طرود نحل؟",
        ],
    )
    def test_rules_intent_not_solution_seeking(self, message: str) -> None:
        intent = rules.match(message)
        assert intent is None or intent.name != INTENT_NEED_BASED_PRODUCT_ADVICE


class TestKB2GuardNeverInventsAvailability:
    def setup_method(self) -> None:
        self._prev = os.environ.get("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE")
        os.environ["NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE"] = "enforce"

    def teardown_method(self) -> None:
        if self._prev is None:
            os.environ.pop("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE", None)
        else:
            os.environ["NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE"] = self._prev

    def test_unknown_with_label_does_not_invent_motawfir(self) -> None:
        ev = evaluate_product_availability_evidence(
            availability_context=_ctx(
                skus=[_sku(1, "Honey Type A", checkout=True)],
            ),
            inbound_text="في عندك طرود نحل؟",
        )
        assert ev.evidence_state == EVIDENCE_UNKNOWN
        reply = build_operational_availability_conflict_reply(
            ev,
            availability_context=_ctx(skus=[_sku(1, "Honey Type A", checkout=True)]),
            inbound_text="في عندك طرود نحل؟",
        )
        assert "متوفر" not in reply
        assert "بعدة خيارات" not in reply
        assert reply == _UNKNOWN_REPLY_AR

    def test_conflict_with_label_does_not_invent_motawfir(self) -> None:
        ev = evaluate_product_availability_evidence(
            availability_context=_ctx(
                skus=[_sku(2, "Gamma Line 2025", checkout=False)],
                focus={"id": 2, "title": "Gamma Line 2025"},
                kb=[{
                    "section_id": 10,
                    "kind": "quick_update",
                    "avail_polarity": "positive",
                    "primary_year": "2025",
                    "linked_product_ids": [2],
                }],
                links=[{"section_id": 10, "product_id": 2, "source": "manual", "confidence": None}],
            ),
        )
        assert ev.evidence_state == EVIDENCE_CONFLICT
        reply = build_operational_availability_conflict_reply(
            ev,
            inbound_text="Gamma Line 2025",
        )
        assert "متوفر" not in reply
        assert "بعدة خيارات" not in reply

    def test_unresolved_entity_guard_rewrite_no_style_followup(self) -> None:
        result = apply_product_availability_truth_guard(
            reply="متوفر طرود نحل بعدة خيارات.",
            availability_context=_ctx(
                skus=[_sku(3, "Catalog Honey", checkout=True)],
            ),
            inbound_text="في عندك طرود نحل؟",
            tenant_id=1,
        )
        assert result.replaced is True
        assert "متوفر" not in result.reply
        styled = apply_commerce_reply_humanizer(
            result.reply,
            inbound_text="في عندك طرود نحل؟",
            intent_name=INTENT_ASK_PRODUCT,
            primary_customer_goal=GOAL_PRODUCT_AVAILABILITY,
            chosen_path="llm",
            post_guard_rewrite=True,
            tenant_id=1,
            conversation_id=1,
            turn_id=1,
        ).reply
        assert "وش خيار" not in styled
        assert "أي الحجم" not in styled

    def test_kb_negative_polarity_blocks_positive_rewrite(self) -> None:
        ev = evaluate_product_availability_evidence(
            availability_context=_ctx(
                skus=[_sku(4, "Service Item", checkout=False)],
                kb=[{
                    "section_id": 20,
                    "kind": "quick_update",
                    "avail_polarity": "negative",
                    "primary_year": None,
                    "linked_product_ids": [],
                }],
            ),
            inbound_text="في عندك service item؟",
        )
        reply = build_operational_availability_conflict_reply(
            ev,
            inbound_text="في عندك service item؟",
        )
        assert "متوفر" not in reply

    def test_variant_options_still_allows_positive_variants(self) -> None:
        with patch(
            "modules.ai.brain.postprocess.product_availability_truth_guard._product_label_for_reply",
            return_value="عسل طلح",
        ):
            reply = build_operational_availability_conflict_reply(_variant_evidence())
        assert reply == "متوفر عسل طلح بعدة خيارات."

    def test_resolved_available_still_allows_positive_single(self) -> None:
        ev = MagicMock()
        ev.evidence_state = EVIDENCE_RESOLVED_AVAILABLE
        ev.evidence_ok_for_positive = True
        with patch(
            "modules.ai.brain.postprocess.product_availability_truth_guard._product_label_for_reply",
            return_value="Kappa Unit",
        ):
            reply = build_operational_availability_conflict_reply(ev)
        assert reply == "متوفر Kappa Unit."

    def test_enforce_unknown_positive_claim_rewrites_to_honest_uncertainty(self) -> None:
        result = apply_product_availability_truth_guard(
            reply="متوفر",
            availability_context=_ctx(skus=[_sku(5, "Iota Product", checkout=True)]),
            inbound_text="هل المنتج متوفر؟",
            tenant_id=99,
        )
        assert result.replaced is True
        assert result.reply == _UNKNOWN_REPLY_AR
        assert "متوفر" not in result.reply
        assert "أي حجم" not in result.reply

    def test_options_variety_claim_detected_without_motawfir(self) -> None:
        reply = "بالنسبة لطرود النحل، عندنا تشكيلة متنوعة."
        assert reply_positive_options_claim(reply) is True
        result = apply_product_availability_truth_guard(
            reply=reply,
            availability_context=_ctx(skus=[_sku(10, "Catalog Honey", checkout=True)]),
            inbound_text="في عندك طرود نحل ؟",
            tenant_id=1,
        )
        assert result.replaced is True
        assert "تشكيلة متنوعة" not in result.reply
