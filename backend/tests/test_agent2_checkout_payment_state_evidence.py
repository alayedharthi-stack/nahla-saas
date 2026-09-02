"""AGENT-2 — checkout payment state, destination, and text evidence.

Platform-wide regressions. Assert state / routing / facts, not Arabic copy.
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from typing import Any, Dict
from unittest.mock import MagicMock, patch

from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
_REPO = os.path.abspath(os.path.join(_BACKEND, ".."))
for _p in (_BACKEND, os.path.join(_REPO, "database"), _REPO):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.address_ingest_post_persist import (  # noqa: E402
    reproject_address_ingest_decision_after_persist,
)
from core.merchant_payment_methods import (  # noqa: E402
    PAYMENT_METHOD_BANK_TRANSFER,
    inbound_is_payment_method_choice,
    parse_payment_method_from_text,
)
from core.order_flow import (  # noqa: E402
    apply_state_patch,
    maybe_handle_payment_method_selection_inbound,
    maybe_handle_wa_address_inbound,
    persist_checkout_location_outcome,
)
from core.payment_intent import (  # noqa: E402
    detect_payment_confirmation_text,
    maybe_handle_payment_claim,
)
from core.payment_receipt_field_parser import (  # noqa: E402
    assess_transfer_text_evidence,
)
from core.reply_instruction import (  # noqa: E402
    CONSTRAINT_ASK_PAYMENT_PROOF,
    CONSTRAINT_NO_PAYMENT_CONFIRM,
    build_address_instruction,
    build_payment_method_instruction,
)
from core.wa_native_catalog_order import (  # noqa: E402
    NativeCatalogOrderItem,
    NativeCatalogOrderPayload,
    apply_native_order_to_state,
)
from core.wa_order_lifecycle import (  # noqa: E402
    STATUS_PAID,
    has_payment_submission,
    is_payment_verified,
    resolve_wa_order_status,
)
from core.wa_payment_submission import (  # noqa: E402
    build_payment_submission_prep_patch,
    checkout_may_present_payment_destination,
    isolate_active_payment_for_new_checkout,
    resolve_verified_payment_destinations,
)
from modules.ai.brain.commerce.commerce_turn_contract import (  # noqa: E402
    attach_commerce_turn_contract,
    build_commerce_turn_contract,
    canonical_checkout_next_slot,
)
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)
from modules.ai.order_flow_v2.missing_fields import next_missing_field  # noqa: E402
from modules.ai.order_flow_v2.payment_evidence import payment_confirmation_allowed  # noqa: E402
from modules.ai.order_flow_v2.state import has_payment_method  # noqa: E402
from models import Base, Conversation, Customer, Tenant  # noqa: E402

GENERIC_MERCHANT = "متجر تجريبي عام"
GENERIC_CUSTOMER_FIRST = "أحمد"
GENERIC_CUSTOMER_LAST = "سالم"
GENERIC_CITY = "الرياض"
GENERIC_MAPS = "https://maps.google.com/?q=24.7136,46.6753"
SHOE_SKU = "shoe-white-runner"
SHOE_TITLE = "حذاء رياضي أبيض"
SHOE_PRODUCT_ID = "142"
COMPLETE_IBAN = "SA0380000000608010167519"
OTHER_TENANT_IBAN = "SA4420000001234567891234"

AR_TRANSFER_TEXT = (
    "تحويل بنكي\n"
    "المبلغ: 85 ريال\n"
    "من حساب: ****4412\n"
    "إلى حساب: ****9012\n"
    "المستفيد: متجر تجريبي عام\n"
    "رقم العملية: TXN998877\n"
    "التاريخ: 2026-09-01 14:22"
)
EN_TRANSFER_TEXT = (
    "Bank transfer\n"
    "Amount: SAR 85\n"
    "From account: ****4412\n"
    "To account: ****9012\n"
    "Beneficiary: Generic Store\n"
    "Reference: REF-445566\n"
    "Time: 2026-09-01 14:22"
)
AMOUNT_ONLY_TEXT = "المبلغ: 126 ريال"
UNPUNCTUATED_TRANSFER_TEXT = (
    "حوالة صادرة بـSR 85 من4412 لـ9012؛\n"
    "اسم مستفيد عام 26/9/1 21:48"
)


def _methods(*, bank: bool = True) -> Any:
    methods = [PAYMENT_METHOD_BANK_TRANSFER] if bank else []
    return SimpleNamespace(
        bank_transfer_enabled=bank,
        cash_on_delivery_enabled=False,
        moyasar_enabled=False,
        moyasar_checkout_ready=False,
        manual_payment_enabled=False,
        available_methods=methods,
        source="test",
    )


def _checkout_op(**overrides: Any) -> Dict[str, Any]:
    op: Dict[str, Any] = {
        "product_id": SHOE_PRODUCT_ID,
        "quantity": 1,
        "catalog_checkout_total": 126.0,
        "catalog_checkout_currency": "SAR",
        "checkout_channel": "whatsapp_catalog",
        "catalog_line_items_authoritative": True,
        "customer_first_name": GENERIC_CUSTOMER_FIRST,
        "customer_last_name": GENERIC_CUSTOMER_LAST,
        "city": GENERIC_CITY,
        "google_maps_url": GENERIC_MAPS,
        "latitude": 24.7136,
        "longitude": 46.6753,
        "line_items": [
            {
                "product_id": SHOE_PRODUCT_ID,
                "product_retailer_id": SHOE_SKU,
                "product_name": SHOE_TITLE,
                "title": SHOE_TITLE,
                "quantity": 1,
                "unit_price": 126.0,
                "currency": "SAR",
            },
        ],
    }
    op.update(overrides)
    return op


def _load_state(op: Dict[str, Any]):
    bs = {
        "order_prep": dict(op),
        "current_product_focus": {
            "id": op.get("product_id"),
            "title": SHOE_TITLE,
            "from_catalog_order": True,
        },
    }
    conv = SimpleNamespace(id=8830, extra_metadata={"brain_state": bs})
    return conv, bs


class TestT1IsolateOldPaymentEvidence:
    def test_old_receipt_archived_new_checkout_clean(self) -> None:
        prep = OrderPreparationState(
            product_id="old-99",
            quantity=2,
            customer_first_name=GENERIC_CUSTOMER_FIRST,
            customer_last_name=GENERIC_CUSTOMER_LAST,
            city=GENERIC_CITY,
            google_maps_url=GENERIC_MAPS,
            payment_receipt_received=True,
            order_status="under_review",
            payment_receipt_metadata={"filename": "Transaction-Receipt.pdf"},
            payment_method="bank_transfer",
            payment_review_state="pending_review",
            checkout_payment_id="old-scope-1",
        )
        old_meta = dict(prep.payment_receipt_metadata)
        result = isolate_active_payment_for_new_checkout(
            prep, reason="native_catalog_order", tenant_id=10,
        )
        assert result["archived"] is True
        history = list(prep.payment_evidence_history or [])
        assert history, "old evidence must be preserved in history"
        archived = history[-1]["evidence"]
        assert archived["payment_receipt_received"] is True
        assert archived["payment_receipt_metadata"]["filename"] == "Transaction-Receipt.pdf"
        assert archived["checkout_payment_id"] == "old-scope-1"
        assert old_meta["filename"] == "Transaction-Receipt.pdf"

        assert prep.payment_receipt_received is False
        assert prep.payment_evidence_received is False
        assert prep.payment_method == ""
        assert prep.payment_destination == {}
        assert prep.payment_review_state == "not_started"
        assert prep.order_status == "awaiting_address"
        assert prep.checkout_payment_id != "old-scope-1"
        assert prep.customer_first_name == GENERIC_CUSTOMER_FIRST
        assert prep.city == GENERIC_CITY
        assert prep.google_maps_url == GENERIC_MAPS

    def test_native_catalog_order_isolates_stale_payment_keeps_product_truth(self) -> None:
        state = MerchantConversationState()
        state.order_prep = OrderPreparationState(
            payment_receipt_received=True,
            order_status="under_review",
            payment_receipt_metadata={"filename": "Transaction-Receipt.pdf"},
            customer_first_name=GENERIC_CUSTOMER_FIRST,
            customer_last_name=GENERIC_CUSTOMER_LAST,
            city=GENERIC_CITY,
            google_maps_url=GENERIC_MAPS,
        )
        payload = NativeCatalogOrderPayload(
            catalog_id="CAT-G",
            customer_note="",
            items=[
                NativeCatalogOrderItem(
                    product_retailer_id=SHOE_SKU,
                    quantity=1,
                    item_price=126.0,
                    currency="SAR",
                    name=SHOE_TITLE,
                ),
            ],
            raw_product_items=[{"product_retailer_id": SHOE_SKU, "quantity": 1}],
        )
        db = MagicMock()
        with patch("core.wa_native_catalog_order.build_line_items_from_payload") as mock_build:
            mock_build.return_value = MagicMock(
                line_items=[
                    {
                        "product_id": SHOE_PRODUCT_ID,
                        "product_retailer_id": SHOE_SKU,
                        "product_name": SHOE_TITLE,
                        "quantity": 1,
                        "unit_price": 126.0,
                        "currency": "SAR",
                        "from_native_catalog_order": True,
                        "match_status": "confirmed",
                    },
                ],
            )
            apply_native_order_to_state(db=db, tenant_id=10, state=state, payload=payload)

        prep = state.order_prep
        assert prep.payment_receipt_received is False
        assert prep.payment_method == ""
        assert prep.payment_review_state == "not_started"
        history = list(prep.payment_evidence_history or [])
        assert history[-1]["evidence"]["payment_receipt_metadata"]["filename"] == (
            "Transaction-Receipt.pdf"
        )
        assert prep.product_id == SHOE_PRODUCT_ID
        assert prep.quantity == 1
        assert prep.catalog_checkout_total == 126.0
        assert prep.line_items[0]["product_retailer_id"] == SHOE_SKU
        assert prep.line_items[0]["product_name"] == SHOE_TITLE
        assert prep.customer_first_name == GENERIC_CUSTOMER_FIRST
        assert prep.city == GENERIC_CITY
        assert prep.google_maps_url == GENERIC_MAPS


class TestT2MethodChoiceNotClaim:
    def test_tahweel_is_method_choice_not_parser_claim_token(self) -> None:
        methods = _methods()
        assert parse_payment_method_from_text("تحويل") is None
        assert inbound_is_payment_method_choice("تحويل", methods) == PAYMENT_METHOD_BANK_TRANSFER
        assert inbound_is_payment_method_choice("تحويل بنكي", methods) == PAYMENT_METHOD_BANK_TRANSFER
        assert inbound_is_payment_method_choice("تم التحويل", methods) is None
        assert inbound_is_payment_method_choice("لا هذا ايصال مدفوع", methods) is None
        assert detect_payment_confirmation_text("تم التحويل") is True

    def test_checkout_payment_method_slot_persists_bank_transfer_not_claim(self) -> None:
        op = _checkout_op()
        dest = [{
            "iban": COMPLETE_IBAN,
            "source": "tenant_payment_accounts",
            "tenant_id": 10,
            "complete": True,
            "verified_or_eligible": True,
        }]
        with patch(
            "core.order_flow._load_brain_state",
            return_value=_load_state(op),
        ), patch(
            "core.merchant_payment_methods.load_merchant_payment_methods",
            return_value=_methods(),
        ), patch(
            "modules.ai.order_flow_v2.missing_fields.compute_v2_missing_fields",
            return_value=["payment_method"],
        ), patch(
            "core.wa_payment_submission.resolve_verified_payment_destinations",
            return_value=dest,
        ), patch(
            "modules.ai.brain.postprocess.payment_credential_guard.compose_verified_bank_transfer_block",
            return_value=f"IBAN {COMPLETE_IBAN}",
        ):
            decision = maybe_handle_payment_method_selection_inbound(
                db=MagicMock(),
                tenant_id=10,
                phone="966500000001",
                inbound_text="تحويل",
            )
        assert decision is not None
        patch_out = decision["state_patch"]
        assert patch_out["payment_method"] == PAYMENT_METHOD_BANK_TRANSFER
        assert patch_out.get("payment_claim_unverified") is False
        assert patch_out.get("payment_evidence_received") is False
        assert patch_out.get("payment_verified") is False
        assert patch_out.get("payment_settled") is False
        assert patch_out.get("order_status") != "payment_submitted"
        assert decision.get("payment_claim") is False
        assert decision.get("payment_submitted") is False


class TestT3MethodPersistence:
    def test_payment_method_roundtrip_survives_dot_message(self) -> None:
        prep = OrderPreparationState(
            payment_method=PAYMENT_METHOD_BANK_TRANSFER,
            payment_review_state="not_started",
            product_id=SHOE_PRODUCT_ID,
            catalog_line_items_authoritative=True,
            customer_first_name=GENERIC_CUSTOMER_FIRST,
            customer_last_name=GENERIC_CUSTOMER_LAST,
            city=GENERIC_CITY,
            google_maps_url=GENERIC_MAPS,
        )
        restored = OrderPreparationState.from_dict(prep.to_dict())
        assert restored.payment_method == PAYMENT_METHOD_BANK_TRANSFER
        assert has_payment_method(restored.to_dict()) is True
        missing = ["payment_method"] if not has_payment_method(restored.to_dict()) else []
        assert next_missing_field(missing) != "payment_method"
        assert inbound_is_payment_method_choice(".", _methods()) is None
        assert parse_payment_method_from_text(".") is None


class TestT4T5T6Destination:
    def test_complete_verified_destination_persisted_before_receipt_ask(self) -> None:
        op = _checkout_op()
        dest = [{
            "iban": COMPLETE_IBAN,
            "source": "tenant_payment_accounts",
            "tenant_id": 10,
            "complete": True,
            "verified_or_eligible": True,
        }]
        with patch(
            "core.order_flow._load_brain_state",
            return_value=_load_state(op),
        ), patch(
            "core.merchant_payment_methods.load_merchant_payment_methods",
            return_value=_methods(),
        ), patch(
            "modules.ai.order_flow_v2.missing_fields.compute_v2_missing_fields",
            return_value=["payment_method"],
        ), patch(
            "core.wa_payment_submission.resolve_verified_payment_destinations",
            return_value=dest,
        ), patch(
            "modules.ai.brain.postprocess.payment_credential_guard.compose_verified_bank_transfer_block",
            return_value=f"الآيبان الخاص بالمتجر: {COMPLETE_IBAN}",
        ):
            decision = maybe_handle_payment_method_selection_inbound(
                db=MagicMock(),
                tenant_id=10,
                phone="966500000001",
                inbound_text="تحويل بنكي",
            )
        assert decision is not None
        dest_out = decision["state_patch"]["payment_destination"]
        assert dest_out["iban"] == COMPLETE_IBAN
        assert dest_out["tenant_id"] == 10
        assert dest_out["complete"] is True
        instr = decision.get("reply_instruction") or {}
        facts = instr.get("facts") if isinstance(instr, dict) else {}
        constraints = instr.get("constraints") if isinstance(instr, dict) else []
        assert facts.get("payment_destination_available") is True
        assert CONSTRAINT_ASK_PAYMENT_PROOF in constraints
        assert COMPLETE_IBAN in str(decision.get("reply_text") or "")

    def test_tenant_b_never_receives_tenant_a_iban(self) -> None:
        accounts_a = SimpleNamespace(ibans=(COMPLETE_IBAN,))
        accounts_b = SimpleNamespace(ibans=())

        def _load(_db, *, tenant_id: int):
            return accounts_a if int(tenant_id) == 10 else accounts_b

        with patch(
            "core.tenant_payment_accounts.load_tenant_payment_accounts",
            side_effect=_load,
        ):
            dest_a = resolve_verified_payment_destinations(MagicMock(), tenant_id=10)
            dest_b = resolve_verified_payment_destinations(MagicMock(), tenant_id=20)
        assert dest_a and dest_a[0]["iban"] == COMPLETE_IBAN
        assert dest_a[0]["tenant_id"] == 10
        assert dest_b == []
        assert OTHER_TENANT_IBAN not in str(dest_b)

    def test_no_eligible_destination_does_not_invent_or_ask_receipt(self) -> None:
        op = _checkout_op()
        with patch(
            "core.order_flow._load_brain_state",
            return_value=_load_state(op),
        ), patch(
            "core.merchant_payment_methods.load_merchant_payment_methods",
            return_value=_methods(),
        ), patch(
            "modules.ai.order_flow_v2.missing_fields.compute_v2_missing_fields",
            return_value=["payment_method"],
        ), patch(
            "core.wa_payment_submission.resolve_verified_payment_destinations",
            return_value=[],
        ), patch(
            "modules.ai.brain.postprocess.payment_credential_guard.compose_verified_bank_transfer_block",
        ) as compose_dest:
            decision = maybe_handle_payment_method_selection_inbound(
                db=MagicMock(),
                tenant_id=20,
                phone="966500000002",
                inbound_text="تحويل",
            )
        assert decision is not None
        patch_out = decision["state_patch"]
        assert patch_out["payment_method"] == PAYMENT_METHOD_BANK_TRANSFER
        assert not patch_out.get("payment_destination")
        assert patch_out.get("awaiting_payment_receipt") is False
        compose_dest.assert_not_called()
        instr = decision.get("reply_instruction") or {}
        constraints = instr.get("constraints") if isinstance(instr, dict) else []
        assert CONSTRAINT_ASK_PAYMENT_PROOF not in constraints
        assert COMPLETE_IBAN not in str(decision.get("reply_text") or "")
        assert "SA" not in str(decision.get("reply_text") or "")

    def test_truncated_iban_rejected(self) -> None:
        accounts = SimpleNamespace(ibans=("SA0380000000", COMPLETE_IBAN[:10]))
        with patch(
            "core.tenant_payment_accounts.load_tenant_payment_accounts",
            return_value=accounts,
        ):
            dest = resolve_verified_payment_destinations(MagicMock(), tenant_id=10)
        assert dest == []


class TestT7LocationDeliveryOnly:
    def test_address_instruction_strips_payment_facts(self) -> None:
        instr = build_address_instruction(
            legacy_copy="تم",
            summary={
                "selected_product": SHOE_TITLE,
                "order_status": "under_review",
                "payment_receipt_received": True,
                "awaiting_payment_receipt": True,
            },
            checkout_facts={
                "checkout_city": GENERIC_CITY,
                "checkout_maps_url": GENERIC_MAPS,
                "payment_receipt_received": True,
                "order_status": "under_review",
                "payment_review_state": "pending_review",
            },
            next_missing_field="payment_method",
            missing_fields=["payment_method"],
        )
        assert instr.facts.get("address_ack_scope") == "delivery_only"
        assert "payment_receipt_received" not in instr.facts
        assert instr.facts.get("order_status") in (None, "")
        assert instr.facts.get("payment_review_state") in (None, "")
        assert CONSTRAINT_NO_PAYMENT_CONFIRM in instr.constraints
        assert instr.facts.get("checkout_city") == GENERIC_CITY
        assert instr.facts.get("ADDRESS_MODEL_INPUT_HAS_PAYMENT_FACTS") is None

    def test_address_model_input_has_no_payment_facts(self) -> None:
        instr = build_address_instruction(
            legacy_copy="تم",
            summary={
                "selected_product": SHOE_TITLE,
                "payment_receipt_received": True,
                "payment_evidence_received": True,
                "payment_review_state": "pending_review",
            },
            checkout_facts={
                "checkout_maps_url": GENERIC_MAPS,
                "payment_method": PAYMENT_METHOD_BANK_TRANSFER,
                "payment_destination": {"iban": COMPLETE_IBAN},
                "awaiting_payment_receipt": True,
            },
        )
        payment_keys = [
            key for key in instr.facts
            if str(key).startswith("payment_")
            or str(key).startswith("awaiting_payment")
            or key in {"order_status"}
        ]
        assert payment_keys == ["payment_state_committed"]
        assert instr.facts.get("payment_state_committed") is False
        assert "iban" not in str(instr.facts).lower()
        assert COMPLETE_IBAN not in str(instr.facts)

    def test_address_action_cannot_mutate_payment_state(self) -> None:
        op = _checkout_op(
            payment_receipt_received=False,
            payment_method=PAYMENT_METHOD_BANK_TRANSFER,
        )
        with patch(
            "core.order_flow._load_brain_state",
            return_value=_load_state(op),
        ):
            decision = maybe_handle_wa_address_inbound(
                db=MagicMock(),
                tenant_id=10,
                phone="966500000001",
                inbound_normalized_type="location",
                inbound_metadata={
                    "location": {"latitude": 24.7136, "longitude": 46.6753},
                },
            )
        assert decision is not None
        patch_out = decision["state_patch"]
        assert "latitude" in patch_out or "google_maps_url" in patch_out
        for key in (
            "payment_receipt_received",
            "payment_review_state",
            "payment_method",
            "order_status",
            "payment_confirmed",
        ):
            assert key not in patch_out

    def test_maps_url_patch_has_no_payment_keys(self) -> None:
        op = _checkout_op()
        with patch(
            "core.order_flow._load_brain_state",
            return_value=_load_state(op),
        ):
            decision = maybe_handle_wa_address_inbound(
                db=MagicMock(),
                tenant_id=10,
                phone="966500000001",
                inbound_normalized_type="text",
                inbound_text=GENERIC_MAPS,
            )
        assert decision is not None
        assert decision["state_patch"].get("google_maps_url") == GENERIC_MAPS
        assert "payment_receipt_received" not in decision["state_patch"]


class TestT8T9T10TextEvidence:
    def test_two_generic_transfer_texts_are_pending_review_not_settled(self) -> None:
        for blob in (AR_TRANSFER_TEXT, EN_TRANSFER_TEXT):
            assessment = assess_transfer_text_evidence(blob)
            assert assessment.sufficient is True, blob
            assert assessment.amount_only is False
            assert assessment.review_state == "pending_review"
            assert assessment.fields.amount
            assert assessment.fields.dest_account_suffix or assessment.fields.beneficiary_name

        op = _checkout_op(payment_method=PAYMENT_METHOD_BANK_TRANSFER)
        with patch(
            "core.order_flow._load_brain_state",
            return_value=_load_state(op),
        ), patch(
            "core.merchant_payment_methods.load_merchant_payment_methods",
            return_value=_methods(),
        ), patch(
            "core.wa_payment_submission.apply_wa_payment_submission",
        ) as apply_sub:
            decision = maybe_handle_payment_claim(
                MagicMock(),
                tenant_id=10,
                phone="966500000001",
                inbound_text=AR_TRANSFER_TEXT,
                has_attached_media=False,
            )
        assert decision is not None
        patch_out = decision["state_patch"]
        assert patch_out["payment_method"] == PAYMENT_METHOD_BANK_TRANSFER
        assert patch_out["payment_evidence_received"] is True
        assert patch_out["payment_receipt_received"] is False
        assert patch_out["payment_review_state"] == "pending_review"
        assert patch_out["payment_verified"] is False
        assert patch_out["payment_settled"] is False
        apply_sub.assert_not_called()

    def test_amount_only_is_insufficient(self) -> None:
        assessment = assess_transfer_text_evidence(AMOUNT_ONLY_TEXT)
        assert assessment.sufficient is False
        assert assessment.amount_only is True
        assert assessment.review_state == "insufficient"

        op = _checkout_op(
            payment_method=PAYMENT_METHOD_BANK_TRANSFER,
            awaiting_payment_receipt=True,
        )
        with patch(
            "core.order_flow._load_brain_state",
            return_value=_load_state(op),
        ), patch(
            "core.merchant_payment_methods.load_merchant_payment_methods",
            return_value=_methods(),
        ):
            decision = maybe_handle_payment_claim(
                MagicMock(),
                tenant_id=10,
                phone="966500000001",
                inbound_text=AMOUNT_ONLY_TEXT,
                has_attached_media=False,
            )
        assert decision is not None
        patch_out = decision["state_patch"]
        assert patch_out.get("payment_verified") is False
        assert patch_out.get("payment_settled") is False
        assert patch_out.get("payment_confirmed") is False
        assert decision.get("evidence_insufficient") is True

    def test_transfer_done_claim_is_not_settlement(self) -> None:
        assert inbound_is_payment_method_choice("تم التحويل", _methods()) is None
        claim_patch = build_payment_submission_prep_patch(submission_type="text_claim")
        assert claim_patch.get("payment_confirmed") is False
        assert claim_patch.get("payment_verification_status") == "pending"
        op = _checkout_op(payment_method=PAYMENT_METHOD_BANK_TRANSFER)
        with patch(
            "core.order_flow._load_brain_state",
            return_value=_load_state(op),
        ), patch(
            "core.merchant_payment_methods.load_merchant_payment_methods",
            return_value=_methods(),
        ), patch(
            "core.payment_intent._payment_text_claim_brain_driven_enabled",
            return_value=True,
        ), patch(
            "core.wa_payment_submission.apply_wa_payment_submission",
            return_value={"linked": True},
        ), patch(
            "core.payment_intent._stamp_text_claim_unverified_state",
            return_value={"payment_claim_unverified": True},
        ):
            decision = maybe_handle_payment_claim(
                MagicMock(),
                tenant_id=10,
                phone="966500000001",
                inbound_text="تم التحويل",
                has_attached_media=False,
            )
        if decision is not None:
            patch_out = decision.get("state_patch") or {}
            assert patch_out.get("payment_verified") is not True
            assert patch_out.get("payment_settled") is not True
            assert patch_out.get("payment_confirmed") is not True


class TestT11T12ProductAndSingleExecution:
    def test_method_instruction_does_not_reask_method_after_selection(self) -> None:
        instr = build_payment_method_instruction(
            legacy_copy="x",
            payment_method=PAYMENT_METHOD_BANK_TRANSFER,
            destination_available=True,
            payment_destination={"iban": COMPLETE_IBAN, "tenant_id": 10, "complete": True},
        )
        assert instr.facts["payment_method"] == PAYMENT_METHOD_BANK_TRANSFER
        assert instr.facts["payment_claim"] is False
        assert instr.facts["payment_verified"] is False
        assert instr.facts["payment_settled"] is False

    def test_one_method_choice_is_one_decision_not_claim(self) -> None:
        op = _checkout_op()
        with patch(
            "core.order_flow._load_brain_state",
            return_value=_load_state(op),
        ), patch(
            "core.merchant_payment_methods.load_merchant_payment_methods",
            return_value=_methods(),
        ), patch(
            "modules.ai.order_flow_v2.missing_fields.compute_v2_missing_fields",
            return_value=["payment_method"],
        ), patch(
            "core.wa_payment_submission.resolve_verified_payment_destinations",
            return_value=[],
        ):
            method_decision = maybe_handle_payment_method_selection_inbound(
                db=MagicMock(),
                tenant_id=10,
                phone="966500000001",
                inbound_text="تحويل",
            )
        with patch(
            "core.order_flow._load_brain_state",
            return_value=_load_state(op),
        ), patch(
            "core.merchant_payment_methods.load_merchant_payment_methods",
            return_value=_methods(),
        ):
            claim_decision = maybe_handle_payment_claim(
                MagicMock(),
                tenant_id=10,
                phone="966500000001",
                inbound_text="تحويل",
                has_attached_media=False,
            )
        assert method_decision is not None
        assert method_decision.get("deterministic_path") == "payment_method_ack"
        assert claim_decision is None
        assert "state_patch" in method_decision
        assert isinstance(method_decision["state_patch"], dict)


def _incomplete_location_op(**overrides: Any) -> Dict[str, Any]:
    op = _checkout_op()
    op.pop("google_maps_url", None)
    op.pop("latitude", None)
    op.pop("longitude", None)
    op.update(overrides)
    return op


def _sqlite_session():
    engine = create_engine("sqlite:///:memory:")
    saved: list = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, orig in saved:
        col.type = orig
    return sessionmaker(bind=engine)(), engine


def _seed_checkout_row(db, *, prep: Dict[str, Any], phone: str = "966500000001"):
    tenant = Tenant(name=GENERIC_MERCHANT, is_active=True)
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    customer = Customer(
        tenant_id=tenant.id,
        phone=phone,
        normalized_phone=phone.lstrip("+"),
        name=f"{GENERIC_CUSTOMER_FIRST} {GENERIC_CUSTOMER_LAST}",
    )
    db.add(customer)
    db.commit()
    db.refresh(customer)
    conv = Conversation(
        tenant_id=tenant.id,
        customer_id=customer.id,
        status="open",
        extra_metadata={"brain_state": {"order_prep": dict(prep)}},
    )
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return tenant, customer, conv


def _commerce_facts() -> CommerceFacts:
    return CommerceFacts(
        store_name=GENERIC_MERCHANT,
        has_products=True,
        product_count=4,
        in_stock_count=4,
        orderable=True,
        snapshot_fresh=True,
    )


class TestEarlyMethodDoesNotPresentIban:
    def test_method_persists_while_location_missing_without_iban(self) -> None:
        op = _incomplete_location_op()
        dest = [{
            "iban": COMPLETE_IBAN,
            "source": "tenant_payment_accounts",
            "tenant_id": 10,
            "complete": True,
            "verified_or_eligible": True,
        }]
        missing = ["delivery_address", "payment_method"]
        assert checkout_may_present_payment_destination(missing) is False
        with patch(
            "core.order_flow._load_brain_state",
            return_value=_load_state(op),
        ), patch(
            "core.merchant_payment_methods.load_merchant_payment_methods",
            return_value=_methods(),
        ), patch(
            "modules.ai.order_flow_v2.missing_fields.compute_v2_missing_fields",
            return_value=missing,
        ), patch(
            "core.wa_payment_submission.resolve_verified_payment_destinations",
            return_value=dest,
        ), patch(
            "modules.ai.brain.postprocess.payment_credential_guard.compose_verified_bank_transfer_block",
            return_value=f"IBAN {COMPLETE_IBAN}",
        ) as compose_dest:
            decision = maybe_handle_payment_method_selection_inbound(
                db=MagicMock(),
                tenant_id=10,
                phone="966500000001",
                inbound_text="تحويل",
            )
        assert decision is not None
        patch_out = decision["state_patch"]
        assert patch_out["payment_method"] == PAYMENT_METHOD_BANK_TRANSFER
        assert not patch_out.get("payment_destination")
        assert patch_out.get("awaiting_payment_receipt") is False
        assert decision.get("payment_claim") is False
        assert decision.get("next_missing_field") == "delivery_address"
        compose_dest.assert_not_called()
        assert COMPLETE_IBAN not in str(decision.get("reply_text") or "")
        instr = decision.get("reply_instruction") or {}
        facts = instr.get("facts") if isinstance(instr, dict) else {}
        constraints = instr.get("constraints") if isinstance(instr, dict) else []
        assert facts.get("payment_destination_available") is False
        assert CONSTRAINT_ASK_PAYMENT_PROOF not in constraints
        assert facts.get("next_missing_field") == "delivery_address"


class TestLiveCatalogThenEarlyMethodThenLocation:
    def test_full_sequence_presents_iban_only_after_address_complete(self) -> None:
        db, _engine = _sqlite_session()
        prep = _incomplete_location_op(
            customer_first_name=GENERIC_CUSTOMER_FIRST,
            customer_last_name=GENERIC_CUSTOMER_LAST,
            city=GENERIC_CITY,
        )
        tenant, _customer, _conv = _seed_checkout_row(db, prep=prep)
        dest = [{
            "iban": COMPLETE_IBAN,
            "source": "tenant_payment_accounts",
            "tenant_id": tenant.id,
            "complete": True,
            "verified_or_eligible": True,
        }]
        with patch(
            "core.order_missing_fields_engine.missing_fields_engine_enabled",
            return_value=False,
        ), patch(
            "core.merchant_payment_methods.load_merchant_payment_methods",
            return_value=_methods(),
        ), patch(
            "core.wa_payment_submission.resolve_verified_payment_destinations",
            return_value=dest,
        ), patch(
            "modules.ai.brain.postprocess.payment_credential_guard.compose_verified_bank_transfer_block",
            return_value=f"الآيبان الخاص بالمتجر: {COMPLETE_IBAN}",
        ):
            early = maybe_handle_payment_method_selection_inbound(
                db=db,
                tenant_id=tenant.id,
                phone="966500000001",
                inbound_text="تحويل",
            )
            assert early is not None
            assert early["state_patch"]["payment_method"] == PAYMENT_METHOD_BANK_TRANSFER
            assert not early["state_patch"].get("payment_destination")
            assert COMPLETE_IBAN not in str(early.get("reply_text") or "")
            assert apply_state_patch(
                db,
                tenant_id=tenant.id,
                phone="966500000001",
                state_patch=early["state_patch"],
            ) is True

            location_decision = maybe_handle_wa_address_inbound(
                db=db,
                tenant_id=tenant.id,
                phone="966500000001",
                inbound_normalized_type="location",
                inbound_metadata={
                    "location": {"latitude": 24.7136, "longitude": 46.6753},
                },
            )
            assert location_decision is not None
            for key in (
                "payment_method",
                "payment_destination",
                "payment_receipt_received",
                "payment_evidence_received",
            ):
                assert key not in (location_decision.get("state_patch") or {})
            ok, reason = persist_checkout_location_outcome(
                db,
                tenant_id=tenant.id,
                phone="966500000001",
                state_patch=location_decision.get("state_patch") or {},
            )
            assert ok is True, reason

            rebuilt = reproject_address_ingest_decision_after_persist(
                db,
                tenant_id=tenant.id,
                phone="966500000001",
                inbound_text="",
                address_type="whatsapp_location",
            )
        assert rebuilt.get("deterministic_path") == "payment_method_ack"
        dest_out = (rebuilt.get("state_patch") or {}).get("payment_destination") or {}
        assert dest_out.get("iban") == COMPLETE_IBAN
        assert COMPLETE_IBAN in str(rebuilt.get("reply_text") or "")
        _, bs = _load_state_from_db(db, tenant_id=tenant.id, phone="966500000001")
        op_now = dict(bs.get("order_prep") or {})
        assert op_now.get("payment_method") == PAYMENT_METHOD_BANK_TRANSFER
        assert (op_now.get("payment_destination") or {}).get("iban") == COMPLETE_IBAN
        assert op_now.get("awaiting_payment_receipt") is True


def _load_state_from_db(db, *, tenant_id: int, phone: str):
    from core.order_flow import _load_brain_state  # noqa: PLC0415

    return _load_brain_state(db, tenant_id=tenant_id, phone=phone)


class TestDotContinuationOperational:
    def test_persisted_bank_transfer_is_not_reasked_on_dot(self) -> None:
        db, _engine = _sqlite_session()
        prep = _checkout_op(payment_method=PAYMENT_METHOD_BANK_TRANSFER)
        tenant, _customer, _conv = _seed_checkout_row(db, prep=prep)
        assert apply_state_patch(
            db,
            tenant_id=tenant.id,
            phone="966500000001",
            state_patch={"payment_method": PAYMENT_METHOD_BANK_TRANSFER},
        ) is True
        _, bs = _load_state_from_db(db, tenant_id=tenant.id, phone="966500000001")
        restored = OrderPreparationState.from_dict(dict(bs.get("order_prep") or {}))
        assert restored.payment_method == PAYMENT_METHOD_BANK_TRANSFER
        assert inbound_is_payment_method_choice(".", _methods()) is None

        ctx = BrainContext(
            tenant_id=int(tenant.id),
            customer_phone="966500000001",
            message=".",
            intent=Intent(name="ask_product", confidence=0.4, raw_message="."),
            state=MerchantConversationState(stage="ordering", order_prep=restored),
            facts=_commerce_facts(),
            history=[],
            profile={"name": f"{GENERIC_CUSTOMER_FIRST} {GENERIC_CUSTOMER_LAST}"},
        )
        with patch(
            "modules.ai.brain.commerce.commerce_turn_contract._load_order_context_for_contract",
            return_value=SimpleNamespace(
                known_previous_address=None,
                prefill=SimpleNamespace(shipping_edit_requested=False),
                customer_id=1,
            ),
        ), patch(
            "modules.ai.brain.commerce.catalog_order_checkout.is_current_catalog_order_submitted",
            return_value=False,
        ), patch(
            "modules.ai.brain.commerce.catalog_order_checkout.try_active_catalog_checkout_continue_decision",
            return_value=None,
        ), patch(
            "core.order_missing_fields_engine.missing_fields_engine_enabled",
            return_value=False,
        ):
            contract = build_commerce_turn_contract(ctx, db=db)
            attach_commerce_turn_contract(ctx, contract)
            missing, nxt = canonical_checkout_next_slot(ctx)
        assert has_payment_method(restored.to_dict()) is True
        assert "payment_method" not in missing
        assert nxt != "payment_method"


class TestUnpunctuatedTransferText:
    def test_unlabeled_sms_is_evidence_not_receipt_or_settlement(self) -> None:
        assessment = assess_transfer_text_evidence(UNPUNCTUATED_TRANSFER_TEXT)
        assert assessment.sufficient is True
        assert assessment.review_state == "pending_review"
        assert assessment.fields.amount
        assert assessment.fields.source_account_suffix or assessment.fields.dest_account_suffix

        op = _checkout_op(payment_method=PAYMENT_METHOD_BANK_TRANSFER)
        with patch(
            "core.order_flow._load_brain_state",
            return_value=_load_state(op),
        ), patch(
            "core.merchant_payment_methods.load_merchant_payment_methods",
            return_value=_methods(),
        ):
            decision = maybe_handle_payment_claim(
                MagicMock(),
                tenant_id=10,
                phone="966500000001",
                inbound_text=UNPUNCTUATED_TRANSFER_TEXT,
                has_attached_media=False,
            )
        assert decision is not None
        patch_out = decision["state_patch"]
        assert patch_out["payment_method"] == PAYMENT_METHOD_BANK_TRANSFER
        assert patch_out["payment_evidence_received"] is True
        assert patch_out["payment_receipt_received"] is False
        assert patch_out["payment_review_state"] == "pending_review"
        assert patch_out["payment_verified"] is False
        assert patch_out["payment_settled"] is False
        assert patch_out.get("payment_submission_received") is not True
        assert "payment_receipt_metadata" not in patch_out


class TestTextEvidenceDoesNotVerifyOrShip:
    def test_evidence_alone_is_not_verified_settled_or_shipping_ready(self) -> None:
        prep = {
            "payment_method": PAYMENT_METHOD_BANK_TRANSFER,
            "payment_evidence_received": True,
            "payment_receipt_received": False,
            "payment_review_state": "pending_review",
            "payment_verified": False,
            "payment_settled": False,
            "payment_confirmed": False,
            "product_id": SHOE_PRODUCT_ID,
            "line_items": _checkout_op()["line_items"],
            "customer_first_name": GENERIC_CUSTOMER_FIRST,
            "customer_last_name": GENERIC_CUSTOMER_LAST,
            "city": GENERIC_CITY,
            "google_maps_url": GENERIC_MAPS,
        }
        assert has_payment_submission(prep) is False
        assert is_payment_verified(prep) is False
        assert payment_confirmation_allowed(prep) is False
        assert prep.get("payment_verified") is False
        assert prep.get("payment_settled") is False
        status, _missing, _addr = resolve_wa_order_status(prep, {})
        assert status != STATUS_PAID
        assert status != "paid"


class TestHistoricalEvidenceOwner:
    def test_new_history_row_does_not_drop_older_rows(self) -> None:
        history = [
            {
                "checkout_payment_id": f"old-{idx}",
                "evidence": {"payment_receipt_received": True, "product_id": f"p-{idx}"},
            }
            for idx in range(21)
        ]
        prep = OrderPreparationState(
            payment_receipt_received=True,
            payment_receipt_metadata={"filename": "new-receipt.pdf"},
            checkout_payment_id="active-old",
            payment_evidence_history=list(history),
            product_id="new-item",
        )
        result = isolate_active_payment_for_new_checkout(
            prep, reason="native_catalog_order", tenant_id=10,
        )
        assert result["archived"] is True
        kept = list(prep.payment_evidence_history or [])
        assert len(kept) == 22
        assert kept[0]["checkout_payment_id"] == "old-0"
        assert kept[20]["checkout_payment_id"] == "old-20"
        assert kept[-1]["checkout_payment_id"] == "active-old"
        assert kept[-1]["evidence"]["payment_receipt_metadata"]["filename"] == "new-receipt.pdf"
        assert prep.payment_receipt_received is False
        assert prep.checkout_payment_id != "active-old"

    def test_old_receipt_stays_tied_to_old_checkout_not_active(self) -> None:
        prep = OrderPreparationState(
            payment_receipt_received=True,
            payment_receipt_metadata={"filename": "Transaction-Receipt.pdf"},
            checkout_payment_id="checkout-old",
            product_id="old-99",
        )
        isolate_active_payment_for_new_checkout(
            prep, reason="native_catalog_order", tenant_id=10,
        )
        history = list(prep.payment_evidence_history or [])
        assert history[-1]["checkout_payment_id"] == "checkout-old"
        assert history[-1]["evidence"]["payment_receipt_received"] is True
        assert prep.payment_receipt_received is False
        assert prep.payment_receipt_metadata in ({}, None)
        assert prep.checkout_payment_id != "checkout-old"
        assert all(
            row.get("checkout_payment_id") != prep.checkout_payment_id
            for row in history
        )

