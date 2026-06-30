"""MobilyPay / e-wallet transfer PDF must route payment, never catalog."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")):
    if p not in sys.path:
        sys.path.insert(0, p)

from core.payment_receipt_field_parser import parse_payment_receipt_fields  # noqa: E402
from core.receipt_order_grounding import (  # noqa: E402
    RECEIPT_PENDING_VERIFICATION_ACK_AR,
    RECEIPT_UNLINKED_ORDER_ACK_AR,
    compose_pending_merchant_receipt_ack,
)
from modules.ai.brain.commerce.commerce_entry_catalog_delivery import (  # noqa: E402
    try_commerce_entry_catalog_decision,
)
from modules.ai.brain.commerce.payment_evidence_turn_route import (  # noqa: E402
    TOPIC_PAYMENT_EVIDENCE_PENDING,
    current_turn_has_payment_evidence,
    try_payment_evidence_turn_decision,
)
from modules.ai.brain.decision.actions import ACTION_LLM_REPLY  # noqa: E402
from modules.ai.brain.intent import rules as intent_rules  # noqa: E402
from modules.ai.brain.postprocess.catalog_product_grounding_guard import (  # noqa: E402
    apply_catalog_product_grounding_guard,
)
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    MerchantConversationState,
    OrderPreparationState,
)

MOBILY_FILENAME = "MobilyPay_Transaction 30Jun2026 06 58 AM.pdf"

MOBILY_TEXT = """
MobilyPay
Transaction Number: 3411962759
Date and Time: 6:58 am 30/06/2026
Amount Transfer: 387.00 SAR
Purpose: المشتريات
Customer Mobile: +966502252344
Receiver: +966555906901
Receiver Name: T**** A*******
""".strip()


def _mobily_metadata(
    *,
    amount: str = "387",
    pe_status: str = "needs_confirmation",
    pdf_kind: str = "payment_pending_evidence",
) -> dict:
    fields = parse_payment_receipt_fields(MOBILY_TEXT, filename=MOBILY_FILENAME)
    return {
        "normalized_type": "document",
        "source_type": "document",
        "has_attached_media": True,
        "mime_type": "application/pdf",
        "filename": MOBILY_FILENAME,
        "pdf_kind": pdf_kind,
        "payment_evidence_status": pe_status,
        "potential_payment_document": True,
        "pdf_text_preview": MOBILY_TEXT[:280],
        "receipt_data": {
            "amount": amount,
            "reference_number": fields.reference_number or "3411962759",
            "beneficiary_name": fields.beneficiary_name or "T**** A*******",
            "receiver_mobile": fields.receiver_mobile or "+966555906901",
            "customer_mobile": fields.customer_mobile or "+966502252344",
            "bank_name": fields.bank_name or "Mobily Pay",
        },
        "payment_evidence_hints": {
            "amount": amount,
            "reference_number": fields.reference_number or "3411962759",
            "receiver_mobile": fields.receiver_mobile or "+966555906901",
            "customer_mobile": fields.customer_mobile or "+966502252344",
        },
    }


def _ctx(
    message: str = "",
    *,
    state: Optional[MerchantConversationState] = None,
    inbound_metadata: Optional[dict] = None,
) -> BrainContext:
    intent = intent_rules.match(message or " ")
    return BrainContext(
        tenant_id=33,
        customer_phone="966500000001",
        message=message,
        intent=intent,
        state=state or MerchantConversationState(greeted=True),
        facts=CommerceFacts(has_products=True, product_count=5, orderable=True),
        profile={"inbound_metadata": dict(inbound_metadata or {})},
    )


def _order_state_387() -> MerchantConversationState:
    st = MerchantConversationState(greeted=True, stage="checkout")
    st.order_prep = OrderPreparationState(
        product_id="p-1",
        customer_first_name="سارة",
        city="الرياض",
        short_address_code="RIYD1234",
        awaiting_payment_receipt=True,
        order_status="awaiting_payment",
        catalog_line_items_authoritative=True,
        line_items=[
            {
                "product_id": "p-1",
                "product_name": "عسل سدر 500g",
                "quantity": 1,
                "unit_price": 387.0,
                "item_price": 387.0,
                "from_native_catalog_order": True,
                "source": "whatsapp_native_catalog_order",
                "match_status": "confirmed",
            },
        ],
        catalog_checkout_total=387.0,
    )
    st.current_product_focus = {"id": "p-1", "title": "عسل سدر 500g", "price": 387.0}
    return st


class TestMobilyPayPaymentDocumentRouting:
    def test_mobilypay_transaction_number_parsed(self) -> None:
        fields = parse_payment_receipt_fields(MOBILY_TEXT, filename=MOBILY_FILENAME)
        assert fields.reference_number == "3411962759"
        assert fields.amount == "387"

    def test_mobilypay_receiver_not_customer_mobile(self) -> None:
        fields = parse_payment_receipt_fields(MOBILY_TEXT, filename=MOBILY_FILENAME)
        assert fields.receiver_mobile == "+966555906901"
        assert fields.customer_mobile == "+966502252344"
        assert fields.receiver_mobile != fields.customer_mobile

    def test_mobilypay_pdf_routes_payment_not_catalog(self) -> None:
        meta = _mobily_metadata()
        ctx = _ctx("", inbound_metadata=meta)
        assert current_turn_has_payment_evidence(ctx) is True
        assert try_commerce_entry_catalog_decision(ctx) is None
        decision = try_payment_evidence_turn_decision(ctx)
        assert decision is not None
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == TOPIC_PAYMENT_EVIDENCE_PENDING
        assert decision.args.get("block_catalog_escalation") is True
        assert not decision.args.get("state_patch")

    def test_amount_only_insufficient_routes_pending_review(self) -> None:
        meta = _mobily_metadata(pe_status="amount_only_insufficient")
        ctx = _ctx("", inbound_metadata=meta)
        decision = try_payment_evidence_turn_decision(ctx)
        assert decision is not None
        assert decision.args.get("payment_receipt_route_kind") == "pending_merchant_review"
        assert decision.args.get("topic") == TOPIC_PAYMENT_EVIDENCE_PENDING

    def test_amount_only_insufficient_no_paid_state(self) -> None:
        meta = _mobily_metadata(pe_status="amount_only_insufficient")
        ctx = _ctx("", inbound_metadata=meta, state=_order_state_387())
        decision = try_payment_evidence_turn_decision(ctx)
        assert decision is not None
        assert not decision.args.get("state_patch")
        instruction = decision.args.get("reply_instruction") or {}
        legacy = str(instruction.get("legacy_copy") or "")
        assert "الكتالوج" not in legacy
        assert "paid" not in legacy.lower()

    def test_catalog_guard_does_not_rewrite_payment_document(self) -> None:
        meta = _mobily_metadata()
        meta["turn_owner_contract"] = {
            "topic": TOPIC_PAYMENT_EVIDENCE_PENDING,
            "block_catalog_push": True,
            "blocked_postprocess": ["catalog_grounding"],
        }
        bad_llm = "عندنا عسل سدر وعسل طلح وعسل شفا — تبغى أي واحد؟"
        result = apply_catalog_product_grounding_guard(
            reply=bad_llm,
            inbound_text="",
            inbound_metadata=meta,
            tenant_id=33,
        )
        assert result.replaced is False
        assert "الخيارات المؤكدة" not in result.reply

    def test_mobilypay_387_links_open_order_when_total_matches(self) -> None:
        meta = _mobily_metadata(amount="387")
        ctx = _ctx("", inbound_metadata=meta, state=_order_state_387())
        decision = try_payment_evidence_turn_decision(ctx)
        assert decision is not None
        instruction = decision.args.get("reply_instruction") or {}
        legacy = str(instruction.get("legacy_copy") or "")
        assert RECEIPT_PENDING_VERIFICATION_ACK_AR in legacy or legacy == RECEIPT_PENDING_VERIFICATION_ACK_AR
        summary = compose_pending_merchant_receipt_ack(
            {
                "can_mention_receipt_product": True,
                "receipt_amount_mismatch": False,
            },
        )
        assert summary == RECEIPT_PENDING_VERIFICATION_ACK_AR

    def test_mobilypay_387_no_order_needs_linking_not_catalog(self) -> None:
        meta = _mobily_metadata(amount="387")
        ctx = _ctx("", inbound_metadata=meta)
        decision = try_payment_evidence_turn_decision(ctx)
        assert decision is not None
        instruction = decision.args.get("reply_instruction") or {}
        legacy = str(instruction.get("legacy_copy") or "")
        assert RECEIPT_UNLINKED_ORDER_ACK_AR in legacy or legacy == RECEIPT_UNLINKED_ORDER_ACK_AR
        assert "الكتالوج" not in legacy
        assert try_commerce_entry_catalog_decision(ctx) is None


class TestPotentialPaymentDocumentSensitivity:
    def test_transaction_filename_alone_is_not_payment_document(self) -> None:
        from core.payment_document_signals import (  # noqa: PLC0415
            metadata_has_potential_payment_document,
        )

        meta = {
            "filename": "transaction_summary_Q2.pdf",
            "normalized_type": "document",
            "pdf_kind": "unknown",
            "payment_evidence_status": "not_payment",
        }
        assert metadata_has_potential_payment_document(meta, text="Page 1 summary") is False

    def test_transaction_with_amount_is_payment_document(self) -> None:
        from core.payment_document_signals import (  # noqa: PLC0415
            metadata_has_potential_payment_document,
        )

        meta = {
            "filename": "transaction_summary.pdf",
            "receipt_data": {"amount": "387"},
        }
        assert metadata_has_potential_payment_document(meta) is True

    def test_mobilypay_filename_alone_is_payment_document(self) -> None:
        from core.payment_document_signals import (  # noqa: PLC0415
            metadata_has_potential_payment_document,
        )

        meta = {"filename": MOBILY_FILENAME}
        assert metadata_has_potential_payment_document(meta) is True
