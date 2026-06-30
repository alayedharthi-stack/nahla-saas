"""Payment receipt evidence must block catalog delivery and win routing."""
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

from modules.ai.brain.catalog.navigation import try_catalog_navigation_decision  # noqa: E402
from modules.ai.brain.commerce.commerce_entry_catalog_delivery import (  # noqa: E402
    CatalogDeliveryKind,
    try_commerce_entry_catalog_decision,
)
from modules.ai.brain.commerce.general_media_reply_guard import (  # noqa: E402
    build_safe_general_image_facts,
)
from modules.ai.brain.commerce.payment_evidence_turn_route import (  # noqa: E402
    TOPIC_PAYMENT_RECEIPT_RECEIVED,
    current_turn_has_payment_evidence,
    try_payment_evidence_turn_decision,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_CATALOG_NAVIGATE,
    ACTION_LLM_REPLY,
)
from modules.ai.brain.intent import rules as intent_rules  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    MerchantConversationState,
    OrderPreparationState,
)


def _receipt_metadata(
    *,
    amount: float = 350.0,
    beneficiary: str = "تركي عايد حسين الحارثي",
    pe_status: str = "confirmed",
    media_kind: str = "image",
    image_kind: str = "payment_receipt",
) -> dict:
    return {
        "normalized_type": media_kind,
        "source_type": media_kind,
        "has_attached_media": True,
        "mime_type": "image/jpeg" if media_kind == "image" else "application/pdf",
        "image_kind": image_kind,
        "payment_evidence_status": pe_status,
        "media_semantic_category": "payment_receipt",
        "receipt_data": {
            "amount": amount,
            "beneficiary_name": beneficiary,
            "bank_name": "Al Rajhi Bank",
            "transfer_date": "2026-06-30",
        },
        "payment_evidence_hints": {
            "amount": amount,
            "beneficiary_name": beneficiary,
        },
        "vision_text": (
            "تم التحويل بنجاح\n"
            f"مبلغ التحويل: {int(amount)} ريال\n"
            f"المستفيد: {beneficiary}\n"
            "تاريخ التحويل: 30 يونيو 2026"
        ),
    }


def _ctx(
    message: str = "",
    *,
    state: Optional[MerchantConversationState] = None,
    inbound_metadata: Optional[dict] = None,
    db: Any = None,
) -> BrainContext:
    intent = intent_rules.match(message or " ")
    ctx = BrainContext(
        tenant_id=33,
        customer_phone="966500000001",
        message=message,
        intent=intent,
        state=state or MerchantConversationState(greeted=True),
        facts=CommerceFacts(has_products=True, product_count=5, orderable=True),
        profile={"inbound_metadata": dict(inbound_metadata or {})},
    )
    if db is not None:
        ctx._db = db  # type: ignore[attr-defined]
    return ctx


def _active_order_state() -> MerchantConversationState:
    st = MerchantConversationState(greeted=True, stage="checkout")
    st.order_prep = OrderPreparationState(
        product_id="p-9",
        customer_first_name="نورة",
        city="جدة",
        short_address_code="JEDD1234",
        awaiting_payment_receipt=True,
        order_status="awaiting_payment",
        catalog_line_items_authoritative=True,
        line_items=[
            {
                "product_id": "p-9",
                "product_name": "عسل سدر 250g",
                "quantity": 1,
                "unit_price": 350.0,
                "item_price": 350.0,
                "from_native_catalog_order": True,
                "source": "whatsapp_native_catalog_order",
                "match_status": "confirmed",
            },
        ],
        catalog_checkout_total=350.0,
    )
    st.current_product_focus = {"id": "p-9", "title": "عسل سدر 250g", "price": 350.0}
    return st


class TestPaymentReceiptBlocksCatalogDelivery:
    def test_receipt_image_detected_as_payment_evidence(self) -> None:
        meta = _receipt_metadata()
        assert current_turn_has_payment_evidence(_ctx("", inbound_metadata=meta)) is True

    def test_receipt_image_blocks_catalog_and_routes_payment(self) -> None:
        meta = _receipt_metadata()
        ctx = _ctx("", inbound_metadata=meta)
        assert try_commerce_entry_catalog_decision(ctx) is None
        assert try_catalog_navigation_decision(ctx) is None

        decision = try_payment_evidence_turn_decision(ctx)
        assert decision is not None
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") in {
            TOPIC_PAYMENT_RECEIPT_RECEIVED,
            "payment_evidence_pending_review",
        }
        assert decision.args.get("block_catalog_escalation") is True
        assert decision.args.get("block_general_image_ack") is True
        goal = str(decision.args.get("response_goal") or "")
        assert "never push catalog" in goal
        assert "never confirm payment verified" in goal

    def test_receipt_no_active_order_needs_linking_not_catalog(self) -> None:
        meta = _receipt_metadata()
        ctx = _ctx("", inbound_metadata=meta)
        decision = try_payment_evidence_turn_decision(ctx)
        assert decision is not None
        assert decision.args.get("payment_receipt_route_kind") == "needs_order_linking"
        assert try_commerce_entry_catalog_decision(ctx) is None
        goal = str(decision.args.get("response_goal") or "")
        assert "which order" in goal or "needs_order_linking" in goal

    def test_receipt_active_order_attaches_without_verified_claim(self) -> None:
        meta = _receipt_metadata()
        state = _active_order_state()
        ctx = _ctx("", state=state, inbound_metadata=meta)
        decision = try_payment_evidence_turn_decision(ctx)
        assert decision is not None
        assert decision.args.get("payment_receipt_route_kind") == "attach_receipt_to_active_order"
        assert decision.args.get("state_patch")
        forbidden = decision.args.get("forbidden_claims") or []
        assert "payment_verified_without_merchant" in forbidden
        instr = decision.args.get("reply_instruction") or {}
        assert "payment_verified_without_merchant" not in str(instr.get("forbidden_claims") or instr)

    def test_pdf_receipt_same_behavior(self) -> None:
        meta = _receipt_metadata(media_kind="document", image_kind="")
        meta["pdf_kind"] = "payment_receipt"
        meta.pop("image_kind", None)
        ctx = _ctx("", inbound_metadata=meta)
        assert try_commerce_entry_catalog_decision(ctx) is None
        assert try_payment_evidence_turn_decision(ctx) is not None

    def test_explicit_catalog_later_turn_still_works_without_receipt(self) -> None:
        ctx = _ctx("أرسل الكتalog", inbound_metadata={})
        decision = try_commerce_entry_catalog_decision(ctx)
        assert decision is not None
        assert decision.action == ACTION_CATALOG_NAVIGATE
        assert decision.args.get("catalog_delivery_kind") == CatalogDeliveryKind.SEND_CATALOG.value

    def test_general_image_not_payment_evidence(self) -> None:
        meta = {
            "normalized_type": "image",
            "has_attached_media": True,
            "image_kind": "product_image",
            "media_semantic_category": "product_image",
            "vision_text": "صورة منتج على رف",
        }
        ctx = _ctx("", inbound_metadata=meta)
        assert current_turn_has_payment_evidence(ctx) is False
        facts = build_safe_general_image_facts(inbound_metadata=meta, message="")
        assert facts.get("scene_type") or facts.get("visible_elements")

    def test_receipt_allowed_facts_include_amount(self) -> None:
        meta = _receipt_metadata(amount=350.0)
        decision = try_payment_evidence_turn_decision(_ctx("", inbound_metadata=meta))
        assert decision is not None
        allowed = decision.args.get("allowed_facts") or {}
        assert allowed.get("amount") == 350.0
        assert "تركي" in str(allowed.get("beneficiary_name") or "")

    def test_ce2_regression_send_catalog_without_receipt(self) -> None:
        decision = try_commerce_entry_catalog_decision(_ctx("أرسل الكتalog"))
        assert decision is not None
        assert decision.action == ACTION_CATALOG_NAVIGATE
