"""
ARCH-015-FIX Tests First — Truth invariants + normalizer E2E (N-01..N-11, N-26).

GREEN TARGET on main today: expected FAIL (documents Production Truth Regression).
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
    PAYMENT_KIND_PE_PAIRS,
    PAYMENT_KINDS,
    assert_truth_consistent,
    build_metadata_after_payment_gate,
    normalize_pdf,
)

# ── Sample payloads (production-realistic) ──────────────────────────

PDF_PRE_TRANSFER = """مراجعة بيانات التحويل
اسم المستفيد: محمد علي
الآيبان: SA0380000000608010167519
المبلغ: 358 ر.س
تأكد من البيانات واضغط تحويل"""

PDF_NEEDS_CONFIRMATION = """البنك الراجحي
اسم المستفيد: أحمد
الآيبان: SA0380000000608010167519
المبلغ: 358 ر.س"""

PDF_CONFIRMED = """البنك الراجحي
اسم المستفيد: أحمد محمد
الآيبان: SA0380000000608010167519
المبلغ: 358.00 ر.س
تم التحويل بنجاح
رقم العملية: TXN-9981234
وقت تنفيذ العملية: 2026-05-17 13:45"""

PDF_UNRELATED = """عقد تقديم خدمات
الطرف الأول: شركة ألف
الطرف الثاني: شركة باء
مدة العقد: سنة واحدة"""

IMAGE_PRE_TRANSFER = """شاشة مراجعة التحويل في تطبيق البنك
اسم المستفيد: محمد علي
الآيبان: SA0380000000608010167519
المبلغ: 358 ريال
تأكد من البيانات واضغط تحويل"""

IMAGE_NEEDS_CONFIRMATION = """لقطة شاشة من تطبيق الراجحي
اسم المستفيد: أحمد
الآيبان: SA0380000000608010167519
المبلغ: 358 ر.س"""

HAJJ_GREETING = (
    "أهنئكم بقدوم عشر ذي الحجة، خير الأيام عند الله، "
    "تقبل الله منا ومنكم صالح الأعمال"
)

IMAGE_UNRELATED = "a photo of flowers in a garden with blue sky"


class TestArch015Invariants:
    """N-01, N-02, N-03"""

    def test_n01_inv1_payment_kind_implies_weak_or_confirmed_pe(self):
        for kind, pe in PAYMENT_KIND_PE_PAIRS.items():
            md = {"payment_evidence_status": pe, "pdf_kind": kind}
            assert_truth_consistent(md)

    def test_n02_inv2_not_payment_never_pairs_with_payment_kind(self):
        for kind in PAYMENT_KINDS:
            md = {"payment_evidence_status": "not_payment", "pdf_kind": kind}
            with pytest.raises(AssertionError, match="INV-2"):
                assert_truth_consistent(md)

    def test_n03_inv3_canonical_pairing_table(self):
        violations = []
        for kind, expected_pe in PAYMENT_KIND_PE_PAIRS.items():
            wrong = "needs_confirmation" if expected_pe == "pre_transfer_review" else "pre_transfer_review"
            md = {"payment_evidence_status": wrong, "pdf_kind": kind}
            try:
                assert_truth_consistent(md)
            except AssertionError:
                continue
            violations.append((kind, wrong))
        assert not violations, f"pairing table failed to catch: {violations}"


class TestArch015NormalizerPdfE2E:
    """N-04..N-07 — assert_truth_consistent on full normalizer output."""

    def test_n04_pdf_pre_transfer_review_metadata_after_normalizer(
        self, isolated_storage, monkeypatch,
    ):
        md = normalize_pdf(
            monkeypatch,
            pdf_text=PDF_PRE_TRANSFER,
            filename="Transfer-Receipt.pdf",
            isolated_storage=isolated_storage,
        )
        assert md.get("pdf_kind") == "payment_pre_review"
        assert md.get("payment_evidence_status") == "pre_transfer_review"
        assert_truth_consistent(md)

    def test_n05_pdf_needs_confirmation_metadata_after_normalizer(
        self, isolated_storage, monkeypatch,
    ):
        md = normalize_pdf(
            monkeypatch,
            pdf_text=PDF_NEEDS_CONFIRMATION,
            filename="receipt.pdf",
            isolated_storage=isolated_storage,
        )
        assert md.get("pdf_kind") == "payment_pending_evidence"
        assert md.get("payment_evidence_status") == "needs_confirmation"
        assert_truth_consistent(md)

    def test_n06_pdf_confirmed_metadata_after_normalizer(
        self, isolated_storage, monkeypatch,
    ):
        md = normalize_pdf(
            monkeypatch,
            pdf_text=PDF_CONFIRMED,
            filename="document_1778.pdf",
            isolated_storage=isolated_storage,
        )
        assert md.get("pdf_kind") == "payment_receipt"
        assert md.get("payment_evidence_status") == "confirmed"
        assert_truth_consistent(md)

    def test_n07_pdf_unrelated_document_stays_not_payment(
        self, isolated_storage, monkeypatch,
    ):
        md = normalize_pdf(
            monkeypatch,
            pdf_text=PDF_UNRELATED,
            filename="tax_invoice.pdf",
            isolated_storage=isolated_storage,
        )
        assert md.get("payment_evidence_status") == "not_payment"
        assert md.get("pdf_kind") not in PAYMENT_KINDS
        assert_truth_consistent(md)


class TestArch015NormalizerImageLayers:
    """N-08..N-11 — payment gate + semantic layers (image path)."""

    def test_n08_image_pre_transfer_metadata_after_normalizer(self):
        md = build_metadata_after_payment_gate(
            IMAGE_PRE_TRANSFER,
            normalized_type="image",
        )
        assert md.get("image_kind") == "payment_pre_review"
        assert md.get("payment_evidence_status") == "pre_transfer_review"
        assert_truth_consistent(md)

    def test_n09_image_needs_confirmation_metadata_after_normalizer(self):
        md = build_metadata_after_payment_gate(
            IMAGE_NEEDS_CONFIRMATION,
            normalized_type="image",
        )
        assert md.get("image_kind") == "payment_pending_evidence"
        assert md.get("payment_evidence_status") == "needs_confirmation"
        assert_truth_consistent(md)

    def test_n10_image_greeting_hard_negative_no_payment_kind(self):
        md = build_metadata_after_payment_gate(
            HAJJ_GREETING,
            normalized_type="image",
        )
        assert md.get("payment_evidence_status") == "not_payment"
        assert md.get("image_kind") not in PAYMENT_KINDS
        assert_truth_consistent(md)

    def test_n11_image_unrelated_no_payment_kind(self):
        md = build_metadata_after_payment_gate(
            IMAGE_UNRELATED,
            normalized_type="image",
        )
        assert md.get("payment_evidence_status") == "not_payment"
        assert md.get("image_kind") not in PAYMENT_KINDS
        assert_truth_consistent(md)


class TestArch015LegacyRegressionDocumented:
    """N-26 — Gate 27 case must satisfy official truth contract."""

    def test_n26_gate27_case_must_not_contradict_truth(
        self, isolated_storage, monkeypatch,
    ):
        md = normalize_pdf(
            monkeypatch,
            pdf_text=PDF_PRE_TRANSFER,
            filename="Transfer-Receipt.pdf",
            isolated_storage=isolated_storage,
        )
        assert_truth_consistent(md)
