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
from core.fallback_policy import EMPTY_REPLY_OPERATIONAL_AR, empty_reply_fallback  # noqa: E402
from modules.ai.brain.postprocess.shipment_truth_guard import (  # noqa: E402
    CLAIM_KIND_DELIVERY_ETA,
    CLAIM_KIND_SHIPMENT,
    CLAIM_KIND_TRACKING_PROMISE,
    SAFE_PRE_SHIPMENT_REPLY_AR,
    apply_shipment_truth_guard,
    detect_ungrounded_shipment_claim_kinds,
    reply_contains_delivery_eta_claim,
    reply_contains_shipment_completed_wording,
    reply_contains_tracking_promise_claim,
    resolve_outbound_after_shipment_scrub,
    should_skip_brain_silent_ack_after_shipment_scrub,
    strip_ungrounded_shipment_claim_sentences,
)
from routers.whatsapp_webhook import _should_suppress_empty_outbound_reply  # noqa: E402


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
        assert result.action == "blocked_ungrounded_shipment_claim"
        assert "تم الشحن" not in result.reply
        assert "يوصل" not in result.reply

    def test_blocks_no_problem_ship_imperative_without_evidence(self) -> None:
        llm_reply = "شحناه مع شركة الشحن"
        result = apply_shipment_truth_guard(
            reply=llm_reply,
            commerce_bundle=_bundle(order_status="pending_review"),
            inbound_metadata={},
        )
        assert result.replaced is True
        assert "شحنا" not in result.reply


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
            "شحنت لك الطلب",
            "تم شحن طلبك",
            "تم تسليمها للناقل",
            "في الطريق لشركة الشحن",
            "خرجت مع شركة الشحن",
            "طلبك بالطريق",
        ],
    )
    def test_markers_detected(self, phrase: str) -> None:
        assert reply_contains_shipment_completed_wording(phrase) is True

    def test_safe_reply_not_flagged(self) -> None:
        assert reply_contains_shipment_completed_wording(SAFE_PRE_SHIPMENT_REPLY_AR) is False


class TestProductionRegressionCases:
    def test_no_shipped_claim_without_shipping_evidence(self) -> None:
        llm_reply = (
            "تمامي، شحنت لك الطلب، وإن شاء الله يوصل قريب. "
            "عادة الشحن يستغرق من 2-4 أيام عمل. "
            "إذا فيه أي تحديثات أو رابط تتبع، نرسل لك مباشرة."
        )
        result = apply_shipment_truth_guard(
            reply=llm_reply,
            commerce_bundle=_bundle(order_status="pending_review"),
            inbound_metadata={},
        )
        assert result.replaced is True
        assert result.action == "blocked_ungrounded_shipment_claim"
        assert "شحنت" not in result.reply
        assert reply_contains_shipment_completed_wording(result.reply) is False

    def test_no_delivery_eta_without_shipping_evidence(self) -> None:
        llm_reply = "طلبك يوصل خلال 2-4 أيام إن شاء الله"
        result = apply_shipment_truth_guard(
            reply=llm_reply,
            commerce_bundle=_bundle(order_status="under_review"),
        )
        assert result.replaced is True
        assert reply_contains_delivery_eta_claim(result.reply) is False
        assert CLAIM_KIND_DELIVERY_ETA in result.blocked_claims

    def test_allows_shipped_claim_with_tracking_evidence(self) -> None:
        llm_reply = (
            "تم الشحن، تقدر تتبع الطلب من الرابط. "
            "يوصل خلال 2-4 أيام إن شاء الله."
        )
        result = apply_shipment_truth_guard(
            reply=llm_reply,
            commerce_bundle=_bundle(
                order_status="shipped",
                shipping_status="shipped",
                tracking_number="TRK-998877",
            ),
        )
        assert result.replaced is False
        assert result.action == "allowed"
        assert result.reply == llm_reply

    def test_customer_mentions_previous_order_does_not_mark_shipped(self) -> None:
        """Customer ordering free-text must not let LLM reply claim shipped."""
        inbound = "انا طالبه نص كيلو سدر ونص كيلو طلح"
        assert reply_contains_shipment_completed_wording(inbound) is False

        llm_reply = (
            "تمامي، شحنت لك الطلب، وإن شاء الله يوصل قريب. "
            "عادة الشحن يستغرق من 2-4 أيام عمل."
        )
        result = apply_shipment_truth_guard(
            reply=llm_reply,
            commerce_bundle=_bundle(order_status="pending_review"),
        )
        assert result.replaced is True
        assert reply_contains_shipment_completed_wording(result.reply) is False
        assert reply_contains_delivery_eta_claim(result.reply) is False
        assert evaluate_shipment_evidence(
            commerce_bundle=_bundle(order_status="pending_review"),
        ).evidence_ok is False

    def test_shipment_guard_metadata_logs_blocked_claim(self) -> None:
        llm_reply = "شحنت لك الطلب"
        kinds = detect_ungrounded_shipment_claim_kinds(llm_reply)
        assert CLAIM_KIND_SHIPMENT in kinds

        result = apply_shipment_truth_guard(
            reply=llm_reply,
            commerce_bundle=_bundle(order_status="pending_review"),
        )
        assert result.blocked_claims == (CLAIM_KIND_SHIPMENT,)
        assert result.action == "blocked_ungrounded_shipment_claim"
        assert "شحنت" not in result.reply


class TestScrubPreservesSafeChunks:
    def test_scrub_keeps_non_claim_prefix(self) -> None:
        llm_reply = "تمام، شحنت لك الطلب"
        scrubbed = strip_ungrounded_shipment_claim_sentences(llm_reply)
        assert scrubbed == "تمام"
        assert reply_contains_tracking_promise_claim(
            "إذا صدر رابط تتبع نرسل لك مباشرة"
        )


class TestEmptyScrubOutboundSafety:
    def test_shipment_guard_empty_after_scrub_does_not_emit_empty_or_bad_fallback(
        self,
    ) -> None:
        llm_reply = (
            "شحنت لك الطلب، "
            "وإن شاء الله يوصل قريب. "
            "عادة الشحن يستغرق من 2-4 أيام عمل. "
            "إذا فيه أي تحديثات أو رابط تتبع، "
            "نرسل لك مباشرة."
        )
        guard = apply_shipment_truth_guard(
            reply=llm_reply,
            commerce_bundle=_bundle(order_status="pending_review"),
        )
        assert guard.scrubbed_empty is True
        assert guard.replaced is True
        assert not guard.reply.strip()

        assert should_skip_brain_silent_ack_after_shipment_scrub(
            reply=guard.reply,
            shipment_claim_scrubbed_empty=True,
        )

        final_reply, suppress_send, skip_silent_ack = resolve_outbound_after_shipment_scrub(
            guard_result=guard,
            empty_reply_fallback_text=empty_reply_fallback(),
        )
        assert suppress_send is True
        assert skip_silent_ack is True
        assert not final_reply.strip()
        assert final_reply != EMPTY_REPLY_OPERATIONAL_AR
        assert _should_suppress_empty_outbound_reply(final_reply) is True
        assert reply_contains_shipment_completed_wording(final_reply) is False
        assert reply_contains_delivery_eta_claim(final_reply) is False
        assert reply_contains_tracking_promise_claim(final_reply) is False
