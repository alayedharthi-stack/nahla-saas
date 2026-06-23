"""
P0 — Arabic Presentation Forms must not block payment-evidence classification.
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.arabic_ocr_normalization import normalize_arabic_ocr_text  # noqa: E402
from core.payment_evidence import (  # noqa: E402
    PAYMENT_EVIDENCE_CONFIRMED,
    PAYMENT_EVIDENCE_NEEDS_CONFIRMATION,
    PAYMENT_EVIDENCE_NOT_PAYMENT,
    classify_payment_evidence,
)

# Rajhi account statement excerpt as extracted by pypdf (Presentation Forms).
_RAJHI_STATEMENT_PRESENTATION = (
    "ﻛﺸﻒ ﺍﻟﺤﺴﺎﺏ ﺍﻟﺘﺎﺭﻳﺦ :23/06/2026 "
    "ﻧﻮﻉ ﻗﻨﺎﺓ ﺍﻻﺗﺼﺎﻝﺍﻟﺮﺍﺟﺤﻲ ﺍﻋﻤﺎﻝ "
    "ﺭﻗﻢ ﺣﺴﺎﺏ ﺍﻟﻤﺴﺘﻔﻴﺪ26600608010132057 "
    "ﺍﺳﻢ ﺍﻟﻤﺴﺘﻔﻴﺪﺗﺮﻛﻲ ﻋﺎﻳﺪ ﺣﺴﻴﻦ ﺍﻟﻌﻤﻴﺮﻱ "
    "ﺗﻔﺎﺻﻴﻞ : 1,187.00- ﺗﺤﻮﻳﻞ ﺩﺍﺧﻞ ﺍﻟﺮﺍﺟﺤﻲ "
    "ﺍﻟﻤﺒﻠﻎ : ﺍﻟﺮﻗﻢ ﺍﻟﻤﺮﺟﻌﻰ :694000010006080945542202606239000001"
)

_RAJHI_STATEMENT_WITH_SUCCESS = (
    _RAJHI_STATEMENT_PRESENTATION + " تم التحويل بنجاح"
)


class TestArabicOcrNormalization:
    def test_presentation_forms_normalize_to_canonical_arabic(self) -> None:
        norm = normalize_arabic_ocr_text(_RAJHI_STATEMENT_PRESENTATION)
        assert "كشف" in norm or "الحساب" in norm
        assert "تحويل" in norm
        assert "الراجحي" in norm
        assert "المبلغ" in norm


class TestStatementPdfPaymentEvidence:
    def test_statement_without_success_is_needs_confirmation_not_not_payment(self) -> None:
        verdict = classify_payment_evidence(
            _RAJHI_STATEMENT_PRESENTATION,
            filename="statement.pdf",
        )
        assert verdict["status"] != PAYMENT_EVIDENCE_NOT_PAYMENT
        assert verdict["status"] == PAYMENT_EVIDENCE_NEEDS_CONFIRMATION

    def test_statement_without_success_is_not_confirmed(self) -> None:
        verdict = classify_payment_evidence(
            _RAJHI_STATEMENT_PRESENTATION,
            filename="statement.pdf",
        )
        assert verdict["status"] != PAYMENT_EVIDENCE_CONFIRMED

    def test_statement_with_success_marker_is_confirmed(self) -> None:
        verdict = classify_payment_evidence(
            _RAJHI_STATEMENT_WITH_SUCCESS,
            filename="statement.pdf",
        )
        assert verdict["status"] == PAYMENT_EVIDENCE_CONFIRMED


class TestReferenceWithoutSuccessMarker:
    def test_explicit_reference_number_without_success_is_needs_confirmation(self) -> None:
        text = (
            "Al Rajhi Bank\n"
            "Beneficiary: Test User\n"
            "Amount: 500.00 SAR\n"
            "Reference Number: FT9988776655\n"
        )
        verdict = classify_payment_evidence(text, filename="statement.pdf")
        assert verdict["status"] == PAYMENT_EVIDENCE_NEEDS_CONFIRMATION
        assert verdict["status"] != PAYMENT_EVIDENCE_CONFIRMED
        assert verdict["reason"] == "payment_context_no_success_marker"
