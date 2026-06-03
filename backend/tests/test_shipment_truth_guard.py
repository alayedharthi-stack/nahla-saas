"""Tests for shipment truth guard — blocks false shipment-completed wording."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")):
    if p not in sys.path:
        sys.path.insert(0, p)

from modules.ai.brain.postprocess.shipment_evidence import (  # noqa: E402
    evaluate_shipment_evidence,
)
from modules.ai.brain.postprocess.shipment_truth_guard import (  # noqa: E402
    SAFE_PRE_SHIPMENT_REPLY_AR,
    apply_shipment_truth_guard,
    reply_contains_shipment_completed_wording,
)


def _bundle(
    *,
    order_status: str = "pending_review",
    shipping_status: str = "not_shipped",
    tracking_url: str | None = None,
    tracking_number: str | None = None,
) -> dict:
    return {
        "active_order_context": {
            "order_id": "12345",
            "order_status": order_status,
            "shipping_status": shipping_status,
            "tracking_url": tracking_url,
            "tracking_number": tracking_number,
        },
    }


class TestBlockedFalseShipment:
    def test_blocks_ship_imperative_under_review_no_tracking(self) -> None:
        llm_reply = "تم الشحن، راح يوصلك قريب"
        result = apply_shipment_truth_guard(
            reply=llm_reply,
            commerce_bundle=_bundle(order_status="under_review"),
            inbound_metadata={},
            tenant_id=33,
            conversation_id=9001,
        )
        assert result.replaced is True
        assert result.action == "blocked_false_shipment"
        assert result.reply == SAFE_PRE_SHIPMENT_REPLY_AR
        assert "تم الشحن" not in result.reply

    def test_blocks_no_problem_ship_imperative_without_evidence(self) -> None:
        llm_reply = "شحناه مع شركة الشحن"
        result = apply_shipment_truth_guard(
            reply=llm_reply,
            commerce_bundle=_bundle(order_status="pending_review"),
            inbound_metadata={},
        )
        assert result.replaced is True
        assert result.reply == SAFE_PRE_SHIPMENT_REPLY_AR


class TestAllowedWithEvidence:
    def test_allows_when_tracking_url_present(self) -> None:
        llm_reply = "تم الشحن، تقدر تتبع الطلب من الرابط"
        result = apply_shipment_truth_guard(
            reply=llm_reply,
            commerce_bundle=_bundle(
                order_status="pending_review",
                tracking_url="https://track.example/abc",
            ),
        )
        assert result.replaced is False
        assert result.action == "allowed"
        assert result.reply == llm_reply

    def test_allows_when_structured_status_shipped(self) -> None:
        llm_reply = "تم الشحن وفي الطريق لشركة الشحن"
        result = apply_shipment_truth_guard(
            reply=llm_reply,
            commerce_bundle=_bundle(
                order_status="shipped",
                shipping_status="shipped",
            ),
        )
        assert result.replaced is False
        assert result.action == "allowed"
        assert result.reply == llm_reply


class TestEvidenceHelperUntrustedSources:
    def test_payment_receipt_received_alone_not_evidence(self) -> None:
        evidence = evaluate_shipment_evidence(
            commerce_bundle=_bundle(order_status="under_review"),
            payment_receipt_received=True,
        )
        assert evidence.evidence_ok is False
        assert evidence.reason == "payment_receipt_alone_not_shipment_evidence"

        result = apply_shipment_truth_guard(
            reply="تم الشحن",
            commerce_bundle=_bundle(order_status="under_review"),
            payment_receipt_received=True,
        )
        assert result.replaced is True

    def test_prior_bot_shipped_text_without_metadata_not_evidence(self) -> None:
        """Structured bundle stays pre-shipment; smb echo alone is not proof."""
        evidence = evaluate_shipment_evidence(
            commerce_bundle=_bundle(order_status="pending_review"),
            inbound_metadata={"event_type": "smb_message_echo"},
        )
        assert evidence.evidence_ok is False

        result = apply_shipment_truth_guard(
            reply="تم الشحن",
            commerce_bundle=_bundle(order_status="pending_review"),
            inbound_metadata={"event_type": "smb_message_echo"},
        )
        assert result.replaced is True

    def test_trusted_order_shipped_automation_counts(self) -> None:
        evidence = evaluate_shipment_evidence(
            commerce_bundle=_bundle(order_status="pending_review"),
            inbound_metadata={"automation_trigger": "order_shipped"},
        )
        assert evidence.evidence_ok is True
        assert evidence.evidence_source == "automation_order_shipped"


class TestShipmentWordingDetection:
    @pytest.mark.parametrize(
        "phrase",
        [
            "تم الشحن",
            "شحناه",
            "تم تسليمها للناقل",
            "في الطريق لشركة الشحن",
            "خرجت مع شركة الشحن",
        ],
    )
    def test_markers_detected(self, phrase: str) -> None:
        assert reply_contains_shipment_completed_wording(phrase) is True

    def test_safe_reply_not_flagged(self) -> None:
        assert reply_contains_shipment_completed_wording(SAFE_PRE_SHIPMENT_REPLY_AR) is False
