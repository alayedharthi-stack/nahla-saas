"""
ARCH-015-FIX Tests First — receipt verdict truth (N-25).
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

from arch015_helpers import (  # noqa: E402
    build_metadata_after_payment_gate,
    normalize_pdf,
)
from core.receipt_verdict import ReceiptVerdict, compute_receipt_verdict  # noqa: E402

PDF_PRE_TRANSFER = """مراجعة بيانات التحويل
اسم المستفيد: محمد علي
الآيبان: SA0380000000608010167519
المبلغ: 358 ر.س
تأكد من البيانات واضغط تحويل"""


class TestArch015ReceiptVerdict:
    """N-25"""

    def test_n25_weak_pe_yields_unclear_not_fake_or_corrupted(
        self, isolated_storage, monkeypatch,
    ):
        md = normalize_pdf(
            monkeypatch,
            pdf_text=PDF_PRE_TRANSFER,
            filename="Transfer-Receipt.pdf",
            isolated_storage=isolated_storage,
        )
        rv = compute_receipt_verdict(
            payment_evidence_status=md.get("payment_evidence_status"),
            pdf_kind=md.get("pdf_kind"),
            has_attached_media=True,
        )
        assert rv.verdict == ReceiptVerdict.UNCLEAR_RECEIPT
        assert rv.verdict != ReceiptVerdict.FAKE_OR_CORRUPTED

    def test_n25_truth_preserved_image_weak_pe_unclear(self):
        text = """شاشة مراجعة التحويل
الآيبان: SA0380000000608010167519
المبلغ: 358 ر.س
تأكد من البيانات واضغط تحويل"""
        md = build_metadata_after_payment_gate(text, normalized_type="image")
        rv = compute_receipt_verdict(
            payment_evidence_status=md["payment_evidence_status"],
            image_kind=md.get("image_kind"),
            has_attached_media=True,
        )
        assert rv.verdict == ReceiptVerdict.UNCLEAR_RECEIPT
