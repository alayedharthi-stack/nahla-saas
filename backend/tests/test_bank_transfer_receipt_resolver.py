"""
P0 bank transfer receipt resolver — evidence + tenant match + confirmation boost.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.bank_transfer_receipt_resolver import (  # noqa: E402
    PAYMENT_EVIDENCE_RECEIVED,
    PAYMENT_PENDING_CONFIRMATION,
    PAYMENT_RECEIVED,
    PAYMENT_REVIEW_REQUIRED,
    compose_payment_received_reply,
    resolve_bank_transfer_receipt,
)
from core.payment_evidence import (  # noqa: E402
    PAYMENT_EVIDENCE_CONFIRMED,
    PAYMENT_EVIDENCE_PRE_TRANSFER_REVIEW,
    classify_payment_evidence,
)
from core.payment_media_metadata import flatten_inbound_payment_metadata  # noqa: E402
from core.tenant_payment_accounts import (  # noqa: E402
    TenantPaymentAccounts,
    _normalise,
    canonical_iban,
)


MERCHANT_IBAN = canonical_iban("SA0380000000608010167519")
MERCHANT_BENEFICIARY = _normalise("تركي عايد الحارثي")

RAJHI_FINAL_RECEIPT = """
Al Rajhi Bank
تأكيد التحويل
Transfer Confirmation
المبلغ: 600 SAR
Amount: 600 SAR
المستفيد: تركي عايد الحارثي
Beneficiary: تركي عايد الحارثي
IBAN: SA03 8000 0000 6080 1016 7519
Date: 15/06/2024 14:30
Reference No: FT12345678901
"""

PRE_TRANSFER_SCREEN = """
Al Rajhi Bank
مراجعة بيانات التحويل
المستفيد: تركي عايد الحارثي
المبلغ: 600 SAR
اضغط تحويل لإتمام العملية
"""

WRONG_BENEFICIARY_RECEIPT = """
Al Rajhi Bank
تأكيد التحويل
المبلغ: 600 SAR
المستفيد: شخص آخر غير مسجل
IBAN: SA0380000000608010169999
Reference No: FT99999999999
"""


@pytest.fixture
def merchant_accounts() -> TenantPaymentAccounts:
    return TenantPaymentAccounts(
        ibans=(MERCHANT_IBAN,),
        beneficiaries=(MERCHANT_BENEFICIARY,),
    )


class TestBankTransferReceiptResolver:
    def test_rajhi_final_with_confirmation_is_payment_received(
        self, merchant_accounts: TenantPaymentAccounts,
    ):
        res = resolve_bank_transfer_receipt(
            RAJHI_FINAL_RECEIPT,
            tenant_accounts=merchant_accounts,
            customer_confirmation=True,
            legacy_pe_status=PAYMENT_EVIDENCE_PRE_TRANSFER_REVIEW,
        )
        assert res.payment_state == PAYMENT_RECEIVED
        assert res.payment_evidence_status == "confirmed"
        assert res.tenant_account_match is True
        assert res.customer_confirmation_boost is True
        assert "600" in (res.reply_ar or "")
        assert "وصل إشعار التحويل" in (res.reply_ar or "")

    def test_tenant_match_high_confidence_metadata(
        self, merchant_accounts: TenantPaymentAccounts,
    ):
        res = resolve_bank_transfer_receipt(
            RAJHI_FINAL_RECEIPT,
            tenant_accounts=merchant_accounts,
            legacy_pe_status=PAYMENT_EVIDENCE_PRE_TRANSFER_REVIEW,
        )
        patch = res.to_metadata_patch()
        assert res.tenant_account_match is True
        assert patch.get("payment_evidence_confidence") in ("high", "medium")
        assert patch["bank_receipt_extraction"]["amount"] == "600"
        assert patch["bank_receipt_extraction"]["bank_name"] == "Al Rajhi Bank"

    def test_real_pre_transfer_screen_pending_confirmation(
        self, merchant_accounts: TenantPaymentAccounts,
    ):
        res = resolve_bank_transfer_receipt(
            PRE_TRANSFER_SCREEN,
            tenant_accounts=merchant_accounts,
            legacy_pe_status=PAYMENT_EVIDENCE_PRE_TRANSFER_REVIEW,
        )
        assert res.payment_state == PAYMENT_PENDING_CONFIRMATION
        assert res.payment_evidence_status == PAYMENT_EVIDENCE_PRE_TRANSFER_REVIEW
        assert res.reply_ar is None

    def test_beneficiary_mismatch_review_required(
        self, merchant_accounts: TenantPaymentAccounts,
    ):
        res = resolve_bank_transfer_receipt(
            WRONG_BENEFICIARY_RECEIPT,
            tenant_accounts=merchant_accounts,
            legacy_pe_status=PAYMENT_EVIDENCE_PRE_TRANSFER_REVIEW,
        )
        assert res.payment_state == PAYMENT_REVIEW_REQUIRED
        assert res.tenant_account_match is False
        assert "لا تطابق" in (res.reply_ar or "")

    def test_classify_payment_evidence_rajhi_not_pre_review(self):
        verdict = classify_payment_evidence(RAJHI_FINAL_RECEIPT)
        assert verdict["status"] == PAYMENT_EVIDENCE_CONFIRMED
        assert verdict["reason"] in (
            "bank_receipt_final_fields",
            "strong_success_phrase",
        )


class TestPaymentEvidenceInboundShortCircuit:
    def test_resolver_promotes_pre_review_receipt(
        self, merchant_accounts: TenantPaymentAccounts, monkeypatch,
    ):
        from core.order_flow import maybe_handle_payment_evidence_inbound  # noqa: E402

        monkeypatch.setattr(
            "core.tenant_payment_accounts.load_tenant_payment_accounts",
            lambda db, tenant_id: merchant_accounts,
        )
        monkeypatch.setattr(
            "core.order_flow._load_brain_state",
            lambda db, tenant_id, phone: (
                None,
                {
                    "current_product_focus": {"title": "عسل"},
                    "order_prep": {
                        "selected_product": "عسل",
                        "awaiting_payment_receipt": True,
                    },
                },
            ),
        )

        md = {
            "payment_evidence_status": "pre_transfer_review",
            "image_kind": "payment_pre_review",
            "vision_text": RAJHI_FINAL_RECEIPT,
            "ocr_text": RAJHI_FINAL_RECEIPT,
        }

        out = maybe_handle_payment_evidence_inbound(
            db=MagicMock(),
            tenant_id=1,
            phone="+966500000000",
            inbound_normalized_type="image",
            inbound_metadata=md,
        )
        assert out is not None
        assert "600" in out["reply_text"]
        assert "شاشة مراجعة" not in out["reply_text"]
        assert out["state_patch"]["payment_receipt_metadata"]["payment_resolution_state"] in (
            PAYMENT_RECEIVED,
            PAYMENT_EVIDENCE_RECEIVED,
        )


class TestPaymentTextClaimWithoutReceipt:
    def test_transfer_claim_without_nearby_image_asks_politely(self, monkeypatch):
        from core.payment_intent import maybe_handle_payment_claim  # noqa: E402

        monkeypatch.setattr(
            "core.payment_intent._payment_text_claim_brain_driven_enabled",
            lambda: True,
        )
        monkeypatch.setattr(
            "core.payment_intent._maybe_promote_prior_evidence",
            lambda **kwargs: None,
        )
        monkeypatch.setattr(
            "core.payment_intent.is_post_shipment_delivery_confirmation",
            lambda *args, **kwargs: False,
        )
        monkeypatch.setattr(
            "core.order_flow._load_brain_state",
            lambda db, tenant_id, phone: (
                None,
                {
                    "current_product_focus": {"title": "عسل سدر"},
                    "order_prep": {
                        "selected_product": "عسل سدر",
                        "awaiting_payment_receipt": True,
                    },
                },
            ),
        )
        monkeypatch.setattr(
            "core.order_flow.apply_state_patch",
            lambda *args, **kwargs: None,
        )

        out = maybe_handle_payment_claim(
            MagicMock(),
            tenant_id=1,
            phone="+966500000000",
            inbound_text="تم التحويل",
            has_attached_media=False,
        )
        assert out is not None
        assert "إيصال" in out["reply_text"] or "PDF" in out["reply_text"]
        assert out["state_patch"].get("payment_receipt_received") is not True
        assert out["state_patch"].get("payment_resolution_state") == (
            "PAYMENT_PENDING_EVIDENCE"
        )


class TestFlattenInboundPaymentMetadata:
    def test_nested_normalized_inbound_surfaces_payment_evidence_status(self):
        raw = {
            "source": "whatsapp",
            "normalized_inbound": {
                "payment_evidence_status": "pre_transfer_review",
                "vision_text": RAJHI_FINAL_RECEIPT,
                "image_kind": "payment_pre_review",
            },
        }
        flat = flatten_inbound_payment_metadata(raw)
        assert flat["payment_evidence_status"] == "pre_transfer_review"
        assert flat["vision_text"] == RAJHI_FINAL_RECEIPT


class TestPromotePriorEvidence:
    def test_promotion_on_follow_up_transfer_text(
        self, merchant_accounts: TenantPaymentAccounts, monkeypatch,
    ):
        from core.payment_intent import _maybe_promote_prior_evidence  # noqa: E402

        monkeypatch.setattr(
            "core.tenant_payment_accounts.load_tenant_payment_accounts",
            lambda db, tenant_id: merchant_accounts,
        )

        now = datetime.now(timezone.utc)
        ev = MagicMock()
        ev.created_at = now - timedelta(minutes=2)
        ev.extra_metadata = {
            "normalized_inbound": {
                "payment_evidence_status": "pre_transfer_review",
                "image_kind": "payment_pre_review",
                "vision_text": RAJHI_FINAL_RECEIPT,
            },
        }

        class _FakeQuery:
            def filter(self, *args, **kwargs):
                return self

            def order_by(self, *args, **kwargs):
                return self

            def limit(self, n):
                return self

            def all(self):
                return [ev]

        db = MagicMock()
        db.query.return_value = _FakeQuery()

        out = _maybe_promote_prior_evidence(
            db=db,
            tenant_id=1,
            phone="+966500000000",
            selected_summary={
                "selected_product": "عسل",
                "awaiting_payment_receipt": True,
            },
        )
        assert out is not None
        assert "600" in out["reply_text"]
        assert out["state_patch"]["payment_receipt_received"] is True
        assert out["state_patch"]["payment_receipt_metadata"]["payment_resolution_state"] == (
            PAYMENT_RECEIVED
        )


class TestComposePaymentReceivedReply:
    def test_expected_wording(self):
        reply = compose_payment_received_reply("600")
        assert "600" in reply
        assert "وصل إشعار التحويل" in reply
        assert "شاشة مراجعة" not in reply


class TestMultiMerchantAccounts:
    def test_matches_second_registered_account(self):
        from core.tenant_payment_accounts import (
            TenantPaymentAccounts,
            canonical_iban,
            receipt_matches_tenant_accounts,
        )

        rajhi = canonical_iban("SA0380000000608010167519")
        ahli = canonical_iban("SA4410000000000000000001")
        accounts = TenantPaymentAccounts(ibans=(rajhi, ahli))
        receipt = (
            "Al Ahli Bank\n"
            "المبلغ: 250 SAR\n"
            f"IBAN: {ahli}\n"
            "Reference No: FT22222222222\n"
            "15/06/2024 10:00"
        )
        verdict = receipt_matches_tenant_accounts(
            accounts=accounts, receipt_text=receipt,
        )
        assert verdict["status"] == "match"
        assert verdict["matched_iban"] == ahli


class TestFalseConfirmationGuard:
    def test_text_confirmation_without_tenant_match_stays_pending(
        self, merchant_accounts: TenantPaymentAccounts,
    ):
        blob = (
            "Al Rajhi Bank\n"
            "المبلغ: 600 SAR\n"
            "المستفيد: شخص غير مسجل\n"
            "Reference No: FT11111111111\n"
        )
        res = resolve_bank_transfer_receipt(
            blob,
            tenant_accounts=merchant_accounts,
            customer_confirmation=True,
            legacy_pe_status="pre_transfer_review",
        )
        assert res.payment_state != PAYMENT_RECEIVED

    def test_receipt_data_persisted_in_metadata_patch(
        self, merchant_accounts: TenantPaymentAccounts,
    ):
        res = resolve_bank_transfer_receipt(
            RAJHI_FINAL_RECEIPT,
            tenant_accounts=merchant_accounts,
        )
        patch = res.to_metadata_patch()
        data = patch["receipt_data"]
        assert data["amount"] == "600"
        assert data["bank_name"] == "Al Rajhi Bank"
        assert data["beneficiary_name"]
        assert data["beneficiary_iban"]
        assert data["reference_number"]
        assert data["transfer_datetime"]
