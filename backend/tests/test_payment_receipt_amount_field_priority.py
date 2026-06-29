"""
Payment receipt field-priority extraction — Alinma/Rajhi PDF regression.

Ensures Amount: SAR 175 wins over VAT Percentage 15% and fee/charge lines.
"""
from __future__ import annotations

import os
import sys

import pytest

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.bank_transfer_receipt_resolver import (  # noqa: E402
    PAYMENT_PENDING_CONFIRMATION,
    PAYMENT_REVIEW_REQUIRED,
    build_receipt_data,
    extract_bank_receipt_fields,
    resolve_bank_transfer_receipt,
)
from core.payment_evidence import PAYMENT_EVIDENCE_CONFIRMED  # noqa: E402
from core.payment_receipt_field_parser import (  # noqa: E402
    parse_payment_receipt_fields,
    parsed_fields_to_hints,
)
from core.tenant_payment_accounts import TenantPaymentAccounts  # noqa: E402
from modules.ai.media.payment_evidence_hints import (  # noqa: E402
    extract_payment_evidence_hints,
    safe_payment_hints_for_display,
)

ALINMA_REPORT_TEXT = """
Amount: SAR 175
VAT Percentage: 15%
VAT Amount: SAR 0.04
Fee Amount: SAR 0.25
Total Charge Amount: SAR 0.29
Bank Name / Type: مصرف الراجحي / بنك محلي
Transaction Date: 20:18:32 29-06-2026
Reference #: 20260629SAINMAINMA2BXXX1201810
Beneficiary: تركي عايد الحارثي
Target Account: SA8180000266608010132057
From Account: xxxx xxxx xxxx 5000
"""


@pytest.fixture
def alinma_meta() -> dict:
    return {
        "pdf_kind": "payment_receipt",
        "payment_evidence_status": "needs_confirmation",
        "filename": "alinma-Report.pdf",
    }


class TestAmountFieldPriority:
    def test_primary_amount_not_vat_percentage(self):
        parsed = parse_payment_receipt_fields(ALINMA_REPORT_TEXT)
        assert parsed.amount == "175"
        assert parsed.amount_confidence == "high"
        assert parsed.vat_percentage == "15%"

    def test_vat_amount_not_payment_amount(self):
        parsed = parse_payment_receipt_fields(ALINMA_REPORT_TEXT)
        assert parsed.vat_amount == "0.04"
        assert parsed.amount != parsed.vat_amount

    def test_fee_amount_not_payment_amount(self):
        parsed = parse_payment_receipt_fields(ALINMA_REPORT_TEXT)
        assert parsed.fee_amount == "0.25"
        assert parsed.amount != parsed.fee_amount

    def test_total_charge_not_payment_amount(self):
        parsed = parse_payment_receipt_fields(ALINMA_REPORT_TEXT)
        assert parsed.total_charge_amount == "0.29"
        assert parsed.amount != parsed.total_charge_amount

    def test_reference_hash_extracted_fully(self):
        parsed = parse_payment_receipt_fields(ALINMA_REPORT_TEXT)
        assert parsed.reference_number == "20260629SAINMAINMA2BXXX1201810"
        assert parsed.reference_number.lower() != "reference"

    def test_from_account_masked_not_generic_sender(self):
        parsed = parse_payment_receipt_fields(ALINMA_REPORT_TEXT)
        hints = parsed_fields_to_hints(parsed)
        assert hints.get("from_account_masked") == "xxxx xxxx xxxx 5000"
        assert hints.get("sender_name") == "xxxx xxxx xxxx 5000"
        assert hints.get("sender_name") != "حساب"
        assert hints.get("sender_name") != "Account"

    def test_hints_serialization_separates_amount_and_vat(self, alinma_meta):
        hints = extract_payment_evidence_hints(ALINMA_REPORT_TEXT, alinma_meta)
        display = safe_payment_hints_for_display(hints)
        assert display is not None
        assert display["amount"] == "175"
        assert display["vat_percentage"] == "15%"
        assert display["fee_amount"] == "0.25"
        assert display["vat_amount"] == "0.04"
        assert display["total_charge_amount"] == "0.29"
        assert display["reference_number"] == "20260629SAINMAINMA2BXXX1201810"
        assert display["beneficiary_name"] == "تركي عايد الحارثي"
        assert display["to_account"] == "SA8180000266608010132057"

    def test_resolver_extraction_matches_parser(self):
        ext = extract_bank_receipt_fields(
            ALINMA_REPORT_TEXT,
            filename="alinma-Report.pdf",
        )
        assert ext.amount == "175"
        assert ext.reference_number == "20260629SAINMAINMA2BXXX1201810"
        assert ext.from_account_masked == "xxxx xxxx xxxx 5000"
        receipt = build_receipt_data(ext)
        assert receipt["amount"] == "175"
        assert receipt["vat_percentage"] == "15%"
        assert receipt["amount_parse_confidence"] == "high"

    def test_reply_policy_does_not_auto_confirm(self):
        merchant_accounts = TenantPaymentAccounts(
            ibans=("SA8180000266608010139999",),
            beneficiaries=("متجر غير مطابق",),
        )
        res = resolve_bank_transfer_receipt(
            ALINMA_REPORT_TEXT,
            tenant_accounts=merchant_accounts,
            filename="alinma-Report.pdf",
        )
        assert res.payment_state in {
            PAYMENT_PENDING_CONFIRMATION,
            PAYMENT_REVIEW_REQUIRED,
        }
        assert res.payment_evidence_status != PAYMENT_EVIDENCE_CONFIRMED
        reply = res.reply_ar or ""
        assert "تم الدفع" not in reply
        assert "تم التأكيد" not in reply
        assert "تم تسجيله" not in reply
