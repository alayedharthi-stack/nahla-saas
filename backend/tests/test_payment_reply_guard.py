"""Tests for payment reply guard — blocks false receipt confirmation."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")):
    if p not in sys.path:
        sys.path.insert(0, p)

from modules.ai.brain.postprocess.payment_reply_guard import (  # noqa: E402
    FUTURE_TRANSFER_REPLY_AR,
    REJECTED_EVIDENCE_REPLY_AR,
    TEXT_CLAIM_NO_EVIDENCE_REPLY_AR,
    apply_payment_reply_guard,
    detect_future_transfer_intent,
    payment_evidence_allows_receipt_ack,
    reply_contains_receipt_confirmation,
)
from modules.ai.brain.postprocess.payment_evidence import (  # noqa: E402
    evaluate_payment_evidence,
)


class TestRejectedPdfMetadata:
    def test_blocks_llm_receipt_wording_for_rejected_pdf(self) -> None:
        llm_reply = "وصل الإيصال، وسيتم متابعة الطلب وتجهيزه بإذن الله"
        metadata = {
            "pdf_kind": "payment_pending_evidence",
            "payment_evidence_status": "not_payment",
            "payment_evidence_reason": "semantic_rejected_document",
        }
        result = apply_payment_reply_guard(
            reply=llm_reply,
            inbound_text="[document]",
            inbound_metadata=metadata,
            tenant_id=1,
            conversation_id=99,
        )
        assert result.replaced is True
        assert result.action == "blocked_receipt_confirmation"
        assert result.reply == REJECTED_EVIDENCE_REPLY_AR
        assert "وصل الإيصال" not in result.reply


class TestConfirmedReceiptAllowed:
    def test_allows_receipt_ack_when_evidence_confirmed(self) -> None:
        llm_reply = "وصلنا إيصال التحويل، شكراً لك"
        metadata = {
            "pdf_kind": "payment_receipt",
            "payment_evidence_status": "confirmed",
        }
        result = apply_payment_reply_guard(
            reply=llm_reply,
            inbound_metadata=metadata,
        )
        assert result.replaced is False
        assert result.action == "allowed"
        assert result.reply == llm_reply


class TestFutureTransferIntent:
    def test_blocks_false_receipt_on_future_transfer_promise(self) -> None:
        inbound = "انا أحول لك الآن\nوفي أسرع وقت إرسله"
        llm_reply = "وصل الإيصال، وسيتم متابعة الطلب وتجهيزه بإذن الله"
        result = apply_payment_reply_guard(
            reply=llm_reply,
            inbound_text=inbound,
            inbound_metadata={},
            tenant_id=33,
            conversation_id=7664,
        )
        assert result.replaced is True
        assert result.reply == FUTURE_TRANSFER_REPLY_AR
        assert "وصل الإيصال" not in result.reply

    @pytest.mark.parametrize(
        "phrase",
        [
            "بحول لك الآن",
            "أحول وأرسل لك الإيصال",
            "أنا بدفع الحين",
            "بعد شوي احول لك",
        ],
    )
    def test_future_phrases_detected_not_past_claim(self, phrase: str) -> None:
        assert detect_future_transfer_intent(phrase) is True
        llm_reply = "وصل الإيصال وسيتم تجهيز الطلب"
        result = apply_payment_reply_guard(
            reply=llm_reply,
            inbound_text=phrase,
        )
        assert result.replaced is True
        assert result.reply == FUTURE_TRANSFER_REPLY_AR


class TestStaleStateOverride:
    def test_future_intent_blocks_despite_payment_receipt_received(self) -> None:
        llm_reply = "وصل الإيصال، وسيتم متابعة الطلب وتجهيزه بإذن الله"
        result = apply_payment_reply_guard(
            reply=llm_reply,
            inbound_text="بعد شوي احول لك",
            inbound_metadata={},
            payment_receipt_received=True,
        )
        assert result.replaced is True
        assert result.reason == "future_transfer_intent"
        assert result.reply == FUTURE_TRANSFER_REPLY_AR

    def test_future_intent_blocks_while_awaiting_receipt(self) -> None:
        llm_reply = "وصلنا إيصال التحويل"
        result = apply_payment_reply_guard(
            reply=llm_reply,
            inbound_text="انا احول لك الان",
            inbound_metadata={"payment_evidence_status": "not_payment"},
            payment_receipt_received=False,
        )
        assert result.replaced is True
        assert result.reply == FUTURE_TRANSFER_REPLY_AR

    def test_confirmed_media_still_allowed_with_stale_state(self) -> None:
        llm_reply = "وصلنا إيصال التحويل، شكراً لك"
        metadata = {
            "pdf_kind": "payment_receipt",
            "payment_evidence_status": "confirmed",
        }
        result = apply_payment_reply_guard(
            reply=llm_reply,
            inbound_text="[document receipt]",
            inbound_metadata=metadata,
            payment_receipt_received=True,
        )
        assert result.replaced is False
        assert result.action == "allowed"

    def test_stale_receipt_flag_alone_blocks_receipt_wording(self) -> None:
        reply = "تم استلام الإيصال وسيتم تجهيز الطلب"
        result = apply_payment_reply_guard(
            reply=reply,
            inbound_metadata={"payment_evidence_status": "not_payment"},
            inbound_text="شكراً لك",
            payment_receipt_received=True,
        )
        assert result.replaced is True
        assert result.reply == TEXT_CLAIM_NO_EVIDENCE_REPLY_AR
        assert result.reason == "no_structured_payment_evidence"


class TestPaymentSlice1TextClaims:
    def test_blocks_receipt_wording_on_text_only_payment_claim(self) -> None:
        llm_reply = (
            "الله يبارك خير 🙏 وصل الإيصال، "
            "وسيتم متابعة الطلب وتجهيزه بإذن الله."
        )
        result = apply_payment_reply_guard(
            reply=llm_reply,
            inbound_text="تم الدفع",
            inbound_metadata={},
            payment_receipt_received=False,
            tenant_id=33,
            conversation_id=9001,
        )
        assert result.replaced is True
        assert result.reply == TEXT_CLAIM_NO_EVIDENCE_REPLY_AR
        assert "وصل الإيصال" not in result.reply
        assert result.reason == "customer_text_claim_without_evidence"

    @pytest.mark.parametrize(
        "inbound",
        ["حولت", "دفعت الآن", "ارسلت المبلغ"],
    )
    def test_customer_transfer_claims_are_not_evidence(self, inbound: str) -> None:
        evidence = evaluate_payment_evidence(
            inbound_metadata={},
            inbound_text=inbound,
        )
        assert evidence.evidence_ok is False

        result = apply_payment_reply_guard(
            reply="تم تأكيد الدفع",
            inbound_text=inbound,
            inbound_metadata={},
        )
        assert result.replaced is True
        assert result.reply == TEXT_CLAIM_NO_EVIDENCE_REPLY_AR


class TestConfirmedFlowUnchanged:
    def test_stale_receipt_flag_does_not_allow_wording(self) -> None:
        reply = "تم استلام الإيصال وسيتم تجهيز الطلب"
        assert payment_evidence_allows_receipt_ack(
            {"payment_evidence_status": "not_payment"},
            payment_receipt_received=True,
            inbound_text="شكراً",
        ) is False
        result = apply_payment_reply_guard(
            reply=reply,
            inbound_metadata={"payment_evidence_status": "not_payment"},
            inbound_text="شكراً",
            payment_receipt_received=True,
        )
        assert result.replaced is True
        assert result.reply == TEXT_CLAIM_NO_EVIDENCE_REPLY_AR

    def test_deterministic_path_skips_guard(self) -> None:
        reply = "وصلنا إيصال التحويل"
        result = apply_payment_reply_guard(
            reply=reply,
            chosen_path="payment_receipt_ack",
            inbound_metadata={"payment_evidence_status": "not_payment"},
        )
        assert result.replaced is False


class TestReceiptMarkerDetection:
    def test_detects_common_false_confirm_phrases(self) -> None:
        assert reply_contains_receipt_confirmation(
            "وصل الإيصال، وسيتم متابعة الطلب وتجهيزه"
        )
        assert reply_contains_receipt_confirmation("سيتم تجهيز الطلب بإذن الله")
