"""Payment receipt order grounding — product/address only with confirmed order evidence."""
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

from core.order_flow import _compose_receipt_ack, maybe_handle_receipt_inbound  # noqa: E402
from core.receipt_order_grounding import (  # noqa: E402
    RECEIPT_AMOUNT_MISMATCH_ACK_AR,
    RECEIPT_UNLINKED_ORDER_ACK_AR,
    compose_grounded_receipt_ack,
    evaluate_receipt_order_grounding,
    is_remaining_payment_balance_message,
    receipt_payment_context_active,
)
from core.reply_instruction import (  # noqa: E402
    CONSTRAINT_INCLUDE_ORDER_FACTS,
    build_payment_receipt_instruction,
)
from modules.ai.brain.commerce.checkout_slot_fallback import (  # noqa: E402
    build_checkout_slot_fallback_reply,
)
from modules.ai.brain.postprocess.operational_reply_validator import (  # noqa: E402
    validate_operational_reply,
)
from modules.ai.brain.turn.final_turn_audit import detect_final_turn_violations  # noqa: E402
from modules.ai.brain.turn.final_turn_contract import FinalTurnContract  # noqa: E402


def _mock_db(*, brain_state: dict) -> MagicMock:
    conv = SimpleNamespace(id=42, extra_metadata={"brain_state": brain_state})
    db = MagicMock()
    db.query.return_value.join.return_value.filter.return_value.order_by.return_value.first.return_value = conv
    return db


def _confirmed_line_item(
    *,
    name: str = "عسل صيفي 250 جرام",
    product_id: str = "p-summer-250",
    quantity: int = 1,
    price: float = 175.0,
) -> dict:
    return {
        "product_id": product_id,
        "product_name": name,
        "quantity": quantity,
        "unit_price": price,
        "item_price": price,
        "from_native_catalog_order": True,
        "source": "whatsapp_native_catalog_order",
        "match_status": "confirmed",
    }


def _confirmed_prep(
    *,
    line_items: list | None = None,
    total: float = 175.0,
    draft_order_id: str = "draft-1",
    missing_fields: list | None = None,
) -> dict:
    return {
        "catalog_line_items_authoritative": True,
        "catalog_checkout_total": total,
        "line_items": line_items or [_confirmed_line_item()],
        "awaiting_payment_receipt": True,
        "payment_method": "bank_transfer",
        "order_status": "awaiting_payment",
        "missing_fields": missing_fields or ["delivery_address", "city"],
    }


RECEIPT_MD = {
    "pdf_kind": "payment_receipt",
    "payment_evidence_status": "confirmed",
    "mime_type": "application/pdf",
    "amount": 175,
    "pdf_text_preview": "Al Rajhi Bank Amount 175 SAR Date 2026/06/28",
}


class TestReceiptOrderGroundingEvidence:
    def test_stale_focus_alone_is_not_confirmed_order(self) -> None:
        bs = {
            "current_product_focus": {
                "title": "عسل صيفي 250 جرام",
                "price": 175,
            },
            "order_prep": {},
        }
        ev = evaluate_receipt_order_grounding(bs, inbound_metadata=RECEIPT_MD)
        assert ev.has_confirmed_order is False
        assert ev.can_mention_product is False
        assert ev.can_request_address is False
        assert ev.reason == "stale_product_focus_only"

    def test_authoritative_line_items_are_confirmed(self) -> None:
        bs = {
            "draft_order_id": "draft-1",
            "order_prep": _confirmed_prep(),
        }
        ev = evaluate_receipt_order_grounding(bs, inbound_metadata=RECEIPT_MD)
        assert ev.has_confirmed_order is True
        assert ev.can_mention_product is True
        assert ev.can_request_address is True

    def test_amount_mismatch_blocks_product_and_address(self) -> None:
        bs = {
            "draft_order_id": "draft-1",
            "order_prep": _confirmed_prep(total=319.0),
        }
        ev = evaluate_receipt_order_grounding(bs, inbound_metadata=RECEIPT_MD)
        assert ev.amount_mismatch is True
        assert ev.can_mention_product is False
        assert ev.can_request_address is False

    def test_payment_context_rejects_stale_focus_only(self) -> None:
        summary = {"selected_product": "عسل صيفي 250 جرام"}
        assert receipt_payment_context_active(summary) is False


class TestReceiptAckReplies:
    def test_t1_receipt_without_confirmed_order(self) -> None:
        db = _mock_db(
            brain_state={
                "current_product_focus": {"title": "عسل صيفي 250 جرام", "price": 175},
                "order_prep": {},
            }
        )
        decision = maybe_handle_receipt_inbound(
            db=db,
            tenant_id=1,
            phone="+966500000001",
            inbound_normalized_type="document",
            inbound_metadata=dict(RECEIPT_MD),
        )
        assert decision is not None
        reply = decision["reply_text"]
        assert "عسل" not in reply
        assert "250" not in reply
        assert "عنوان" not in reply
        assert "وصل" in reply

    def test_t2_receipt_with_confirmed_draft(self) -> None:
        db = _mock_db(brain_state={"draft_order_id": "d1", "order_prep": _confirmed_prep()})
        decision = maybe_handle_receipt_inbound(
            db=db,
            tenant_id=1,
            phone="+966500000002",
            inbound_normalized_type="document",
            inbound_metadata=dict(RECEIPT_MD),
        )
        assert decision is not None
        reply = decision["reply_text"]
        assert "عسل صيفي 250 جرام" in reply
        assert "عنوان" in reply or "توصيل" in reply or "قوقل" in reply

    def test_t3_amount_mismatch_ack(self) -> None:
        bs = {"draft_order_id": "d1", "order_prep": _confirmed_prep(total=319.0)}
        ev = evaluate_receipt_order_grounding(bs, inbound_metadata=RECEIPT_MD)
        from core.receipt_order_grounding import apply_receipt_grounding_to_summary  # noqa: E402
        from core.order_flow import _focus_summary  # noqa: E402

        summary = apply_receipt_grounding_to_summary(_focus_summary(bs), ev)
        reply = _compose_receipt_ack(summary)
        assert reply == RECEIPT_AMOUNT_MISMATCH_ACK_AR
        assert "تم تأكيد" not in reply
        assert "عسل" not in reply

    def test_t5_stale_focus_grounded_ack(self) -> None:
        reply = compose_grounded_receipt_ack(
            {
                "can_mention_receipt_product": False,
                "selected_product": "",
            }
        )
        assert reply == RECEIPT_UNLINKED_ORDER_ACK_AR


class TestPaymentReceiptInstruction:
    def test_unlinked_instruction_has_no_order_facts(self) -> None:
        instr = build_payment_receipt_instruction(
            legacy_copy=RECEIPT_UNLINKED_ORDER_ACK_AR,
            summary={
                "can_mention_receipt_product": False,
                "needs_order_linking_or_review": True,
                "receipt_order_evidence": {"receipt_amount": 175},
            },
        )
        assert CONSTRAINT_INCLUDE_ORDER_FACTS not in instr.constraints
        assert instr.facts.get("needs_order_linking_or_review") is True
        assert "selected_product" not in instr.facts

    def test_confirmed_instruction_includes_order_facts(self) -> None:
        instr = build_payment_receipt_instruction(
            legacy_copy="ok",
            summary={
                "can_mention_receipt_product": True,
                "can_request_receipt_address": True,
                "selected_product": "عسل صيفي 250 جرام",
                "price": 175,
            },
        )
        assert CONSTRAINT_INCLUDE_ORDER_FACTS in instr.constraints
        assert instr.facts.get("selected_product") == "عسل صيفي 250 جرام"


class TestRemainingBalanceAndSlotFallback:
    def test_t4_remaining_balance_detector(self) -> None:
        assert is_remaining_payment_balance_message("هذا الباقي الله يعطيك العافيه") is True

    def test_t4_no_address_fallback_on_stale_receipt_state(self) -> None:
        state = SimpleNamespace(
            order_prep=SimpleNamespace(
                to_dict=lambda: {
                    "payment_receipt_received": True,
                    "payment_method": "bank_transfer",
                    "missing_fields": ["delivery_address"],
                    "line_items": [],
                }
            ),
            current_product_focus={"title": "عسل صيفي 250 جرام"},
        )
        reply = build_checkout_slot_fallback_reply(
            state=state,
            inbound_text="هذا الباقي الله يعطيك العافيه",
        )
        assert reply is None


class TestOperationalValidator:
    def test_blocks_product_in_unlinked_receipt_compose(self) -> None:
        instr = build_payment_receipt_instruction(
            legacy_copy=RECEIPT_UNLINKED_ORDER_ACK_AR,
            summary={"can_mention_receipt_product": False},
        )
        bad = validate_operational_reply(
            "استلمت إيصال التحويل لعسل صيفي 250 جرام",
            instr,
        )
        assert bad.ok is False


class TestFinalTurnContractShadow:
    def test_receipt_violations_shadow_only(self) -> None:
        contract = FinalTurnContract(
            response_purpose="payment_receipt",
            turn_owner="order_flow",
            decision_action="llm_reply",
            decision_topic="payment",
            known_facts={
                "payment_receipt_turn": True,
                "receipt_confirmed_order": False,
                "receipt_product_label": "عسل صيفي 250 جرام",
            },
        )
        violations = detect_final_turn_violations(
            contract,
            "استلمت إيصال التحويل لعسل صيفي 250 جرام. الآن احتاج منك عنوان التوصيل",
        )
        assert "payment_receipt_product_claim_without_order_evidence" in violations
        assert "payment_receipt_address_request_without_confirmed_order" in violations
