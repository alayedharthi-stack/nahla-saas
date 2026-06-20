"""P0 regression — payment receipt attachment metadata gate."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.order_flow import maybe_handle_receipt_inbound  # noqa: E402
from core.order_payment_policy import (  # noqa: E402
    BANK_TRANSFER_MERCHANT_ALERT,
    build_merchant_payment_alert,
    enrich_order_payment_metadata,
)
from core.payment_receipt_attachment_gate import (  # noqa: E402
    PAYMENT_RECEIPT_ATTACHMENT_ACK_AR,
    PAYMENT_RECEIPT_DUPLICATE_ACK_AR,
    ROUTE_PAYMENT_RECEIPT_RECEIVED,
    assess_payment_receipt_attachment,
    build_receipt_received_state_patch,
    try_metadata_receipt_short_circuit,
)
from modules.ai.brain.postprocess.payment_reply_guard import (  # noqa: E402
    TEXT_CLAIM_NO_EVIDENCE_REPLY_AR,
    apply_payment_reply_guard,
)


def _payment_context_summary(*, awaiting: bool = True, product: str = "عسل سدر") -> dict:
    return {
        "selected_product": product,
        "awaiting_payment_receipt": awaiting,
        "payment_receipt_received": False,
        "order_status": "awaiting_payment",
        "payment_method": "bank_transfer",
    }


def _mock_db(*, brain_state: dict | None = None) -> MagicMock:
    conv = SimpleNamespace(
        id=42,
        extra_metadata={
            "brain_state": brain_state or {},
        },
    )
    db = MagicMock()
    db.query.return_value.join.return_value.filter.return_value.order_by.return_value.first.return_value = conv
    return db


def _brain_state_awaiting_receipt(product: str = "عسل سدر") -> dict:
    return {
        "current_product_focus": {"title": product, "price": 120, "currency": "SAR"},
        "order_prep": {
            "awaiting_payment_receipt": True,
            "payment_receipt_received": False,
            "order_status": "awaiting_payment",
            "payment_method": "bank_transfer",
        },
    }


class TestReceiptAttachmentShortCircuit:
    def test_pdf_after_bank_transfer_instructions(self) -> None:
        summary = _payment_context_summary()
        decision = try_metadata_receipt_short_circuit(
            inbound_normalized_type="document",
            inbound_metadata={
                "mime_type": "application/pdf",
                "filename": "transfer.pdf",
                "payment_evidence_status": "not_payment",
            },
            summary=summary,
        )
        assert decision is not None
        assert decision["reply_text"] == PAYMENT_RECEIPT_ATTACHMENT_ACK_AR
        assert decision["route"] == ROUTE_PAYMENT_RECEIPT_RECEIVED
        assert decision["state_patch"]["payment_receipt_received"] is True
        assert decision["state_patch"]["order_status"] == "payment_submitted"

    def test_image_after_bank_transfer_instructions(self) -> None:
        summary = _payment_context_summary()
        decision = try_metadata_receipt_short_circuit(
            inbound_normalized_type="image",
            inbound_metadata={
                "mime_type": "image/jpeg",
                "payment_evidence_status": "not_payment",
            },
            summary=summary,
        )
        assert decision is not None
        assert decision["reply_text"] == PAYMENT_RECEIPT_ATTACHMENT_ACK_AR
        assert decision["state_patch"]["payment_verification_status"] == "pending"

    def test_pdf_named_receipt_not_ask_again_via_guard(self) -> None:
        llm_reply = TEXT_CLAIM_NO_EVIDENCE_REPLY_AR
        metadata = {
            "normalized_type": "document",
            "mime_type": "application/pdf",
            "filename": "bank_receipt.pdf",
            "payment_evidence_status": "not_payment",
            "awaiting_payment_receipt": True,
            "selected_product": "عسل سدر",
            "order_status": "awaiting_payment",
            "payment_method": "bank_transfer",
        }
        result = apply_payment_reply_guard(
            reply=llm_reply,
            inbound_text="[document]",
            inbound_metadata=metadata,
            payment_receipt_received=False,
        )
        assert result.replaced is True
        assert result.reply == PAYMENT_RECEIPT_ATTACHMENT_ACK_AR
        assert TEXT_CLAIM_NO_EVIDENCE_REPLY_AR not in result.reply

    def test_attachment_sets_review_needed_state(self) -> None:
        patch = build_receipt_received_state_patch(
            inbound_metadata={"mime_type": "application/pdf", "filename": "receipt.pdf"},
        )
        assert patch["payment_receipt_received"] is True
        assert patch["payment_verification_status"] == "pending"
        assert patch["payment_status"] == "pending_verification"
        assert patch["payment_receipt_metadata"]["manual_verification_required"] is True

    def test_merchant_manual_verification_alert(self) -> None:
        patch = build_receipt_received_state_patch(
            inbound_metadata={"mime_type": "application/pdf"},
        )
        meta = enrich_order_payment_metadata(
            {},
            order_prep=patch,
            target_status=patch["order_status"],
        )
        alert = build_merchant_payment_alert(
            raw_status=patch["order_status"],
            meta=meta,
        )
        assert alert is not None
        assert BANK_TRANSFER_MERCHANT_ALERT in alert["label"]

    def test_non_receipt_pdf_outside_payment_context(self) -> None:
        assessment = assess_payment_receipt_attachment(
            inbound_normalized_type="document",
            inbound_metadata={
                "mime_type": "application/pdf",
                "filename": "catalog.pdf",
                "payment_evidence_status": "not_payment",
            },
            summary={
                "selected_product": "",
                "awaiting_payment_receipt": False,
                "payment_receipt_received": False,
                "order_status": "",
                "payment_method": "",
            },
        )
        assert assessment is None

    def test_bank_transfer_still_asks_before_attachment(self) -> None:
        metadata = {
            "payment_evidence_status": "not_payment",
            "awaiting_payment_receipt": True,
            "selected_product": "عسل سدر",
            "order_status": "awaiting_payment",
            "payment_method": "bank_transfer",
        }
        result = apply_payment_reply_guard(
            reply="بعد التحويل أرسل الإيصال هنا عشان نراجعه ونكمل الطلب 🌷",
            inbound_text="تمام",
            inbound_metadata=metadata,
            payment_receipt_received=False,
        )
        assert result.replaced is False
        assert "أرسل الإيصال" in result.reply

    def test_duplicate_receipt_no_duplicate_state_patch(self) -> None:
        summary = _payment_context_summary(awaiting=False)
        summary["payment_receipt_received"] = True
        decision = try_metadata_receipt_short_circuit(
            inbound_normalized_type="document",
            inbound_metadata={
                "mime_type": "application/pdf",
                "filename": "receipt.pdf",
            },
            summary=summary,
        )
        assert decision is not None
        assert decision["duplicate"] is True
        assert decision["reply_text"] == PAYMENT_RECEIPT_DUPLICATE_ACK_AR
        assert decision["state_patch"] == {}


class TestOrderFlowMetadataFallback:
    def test_maybe_handle_receipt_inbound_without_classifier(self) -> None:
        db = _mock_db(brain_state=_brain_state_awaiting_receipt())
        decision = maybe_handle_receipt_inbound(
            db=db,
            tenant_id=33,
            phone="+966500000000",
            inbound_normalized_type="document",
            inbound_metadata={
                "mime_type": "application/pdf",
                "filename": "hawl.pdf",
                "payment_evidence_status": "not_payment",
            },
        )
        assert decision is not None
        assert decision["reply_text"] == PAYMENT_RECEIPT_ATTACHMENT_ACK_AR
        assert decision["state_patch"]["payment_receipt_received"] is True


class TestPreTransferReviewBlocksReceiptAck:
    def test_pre_transfer_review_does_not_fire_ack(self) -> None:
        summary = _payment_context_summary()
        assessment = assess_payment_receipt_attachment(
            inbound_normalized_type="document",
            inbound_metadata={
                "pdf_kind": "payment_receipt",
                "payment_evidence_status": "pre_transfer_review",
                "payment_evidence_reason": "pre_transfer_review_phrase",
            },
            summary=summary,
        )
        assert assessment is None

        db = _mock_db(brain_state=_brain_state_awaiting_receipt())
        decision = maybe_handle_receipt_inbound(
            db=db,
            tenant_id=33,
            phone="+966500000000",
            inbound_normalized_type="document",
            inbound_metadata={
                "pdf_kind": "payment_receipt",
                "payment_evidence_status": "pre_transfer_review",
                "payment_evidence_reason": "pre_transfer_review_phrase",
            },
        )
        assert decision is None

    def test_review_screen_before_receipt_submission(self) -> None:
        summary = _payment_context_summary()
        for metadata in (
            {
                "pdf_kind": "payment_pre_review",
                "payment_evidence_status": "pre_transfer_review",
            },
            {
                "image_kind": "payment_pre_review",
                "payment_evidence_status": "needs_confirmation",
            },
        ):
            assert assess_payment_receipt_attachment(
                inbound_normalized_type="document",
                inbound_metadata=metadata,
                summary=summary,
            ) is None
            assert try_metadata_receipt_short_circuit(
                inbound_normalized_type="document",
                inbound_metadata=metadata,
                summary=summary,
            ) is None

        guard_result = apply_payment_reply_guard(
            reply=TEXT_CLAIM_NO_EVIDENCE_REPLY_AR,
            inbound_text="[document]",
            inbound_metadata={
                "normalized_type": "document",
                "pdf_kind": "payment_pre_review",
                "payment_evidence_status": "pre_transfer_review",
                "awaiting_payment_receipt": True,
                "selected_product": "عسل سدر",
                "order_status": "awaiting_receipt",
                "payment_method": "bank_transfer",
            },
            payment_receipt_received=False,
        )
        assert guard_result.reply == TEXT_CLAIM_NO_EVIDENCE_REPLY_AR
        assert guard_result.replaced is False
        assert PAYMENT_RECEIPT_ATTACHMENT_ACK_AR not in guard_result.reply
