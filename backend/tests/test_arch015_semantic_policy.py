"""
ARCH-015-FIX Tests First — semantic / override policy (N-12..N-18, N-27..N-30).
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
    PAYMENT_KINDS,
    apply_semantic_layers_like_normalizer,
    assert_truth_consistent,
    build_metadata_after_payment_gate,
    merge_metadata_production_semantics,
)
from modules.ai.media.semantic_classifier import (  # noqa: E402
    MEDIA_PAYMENT_RECEIPT,
    MEDIA_PRODUCT_IMAGE,
    MEDIA_RELIGIOUS_SOCIAL,
    MEDIA_SOCIAL_IMAGE,
    allows_payment_media_ack,
    apply_semantic_payment_override,
    classify_media_semantic,
)

PDF_PRE_TRANSFER = """مراجعة بيانات التحويل
اسم المستفيد: محمد علي
الآيبان: SA0380000000608010167519
المبلغ: 358 ر.س
تأكد من البيانات واضغط تحويل"""

REAL_RECEIPT_WITH_MABROOK = """تم التحويل بنجاح
المبلغ: 360 ريال
إلى: متجر نهلة - الراجحي
رقم العملية: 9981234"""


class TestArch015WeakEvidencePreservation:
    """N-12, N-13 — B1 weak evidence truth preservation."""

    def test_n12_weak_pe_not_overridden_by_semantic_document(self):
        md = build_metadata_after_payment_gate(
            PDF_PRE_TRANSFER,
            filename="Transfer-Receipt.pdf",
            normalized_type="document",
        )
        assert md["payment_evidence_status"] == "pre_transfer_review"
        assert md["pdf_kind"] == "payment_pre_review"
        assert md.get("media_semantic_category") == "unrelated_media"
        assert_truth_consistent(md)

    def test_n13_weak_pe_not_overridden_by_semantic_unrelated(self):
        text = """شاشة مراجعة التحويل
اسم المستفيد: محمد
الآيبان: SA0380000000608010167519
تأكد من البيانات واضغط تحويل"""
        md = build_metadata_after_payment_gate(text, normalized_type="image")
        assert md["payment_evidence_status"] == "pre_transfer_review"
        assert md["image_kind"] == "payment_pre_review"
        assert_truth_consistent(md)


class TestArch015AckPolicyWithoutTruthMutation:
    """N-14, N-15 — B4: block ACK, preserve pe."""

    def test_n14_product_semantic_blocks_ack_without_clearing_pe(self):
        base = {
            "payment_evidence_status": "needs_confirmation",
            "pdf_kind": "payment_pending_evidence",
            "media_semantic_category": MEDIA_PRODUCT_IMAGE,
        }
        assert base["payment_evidence_status"] == "needs_confirmation"
        assert not allows_payment_media_ack(
            semantic_category=MEDIA_PRODUCT_IMAGE,
            payment_evidence_status="needs_confirmation",
            awaiting_payment_receipt=True,
            has_active_order=True,
        )
        assert_truth_consistent(base)

    def test_n15_social_semantic_blocks_ack_without_clearing_pe(self):
        base = {
            "payment_evidence_status": "needs_confirmation",
            "image_kind": "payment_pending_evidence",
            "media_semantic_category": MEDIA_SOCIAL_IMAGE,
        }
        assert not allows_payment_media_ack(
            semantic_category=MEDIA_SOCIAL_IMAGE,
            payment_evidence_status="needs_confirmation",
            awaiting_payment_receipt=True,
            has_active_order=True,
        )
        assert_truth_consistent(base)


class TestArch015ConfirmedOverrideAndMerge:
    """N-16, N-17 — confirmed-only override + B3 merge."""

    def test_n16_confirmed_downgraded_only_when_semantic_contradicts(self):
        md = {
            "payment_evidence_status": "confirmed",
            "pdf_kind": "payment_receipt",
            "media_semantic_category": MEDIA_PRODUCT_IMAGE,
        }
        out = apply_semantic_payment_override(md)
        assert out["payment_evidence_status"] == "not_payment"
        assert "pdf_kind" not in out

    def test_n17_merge_popped_kind_removed_from_base_meta(self):
        base = {
            "payment_evidence_status": "confirmed",
            "pdf_kind": "payment_receipt",
            "media_semantic_category": MEDIA_PRODUCT_IMAGE,
        }
        overridden = apply_semantic_payment_override(base)
        merged = merge_metadata_production_semantics(base, overridden)
        assert_truth_consistent(merged)


class TestArch015SemanticOrdering:
    """N-18 — B2: weak pe before generic_document."""

    def test_n18_semantic_weak_pe_before_generic_document(self):
        sem = classify_media_semantic(
            text_blob=PDF_PRE_TRANSFER,
            filename="Transfer-Receipt.pdf",
            normalized_type="document",
            payment_evidence_status="pre_transfer_review",
            pdf_kind="payment_pre_review",
        )
        assert sem.reason != "generic_document"
        assert sem.category != "document"


class TestArch015B1bConfirmedSocialOrdering:
    """N-27..N-30 — B1b: social/religious before confirmed semantic."""

    def test_n27_confirmed_with_social_hint_semantic_is_not_payment_receipt(self):
        sem = classify_media_semantic(
            text_blob="Eid Mubarak celebration poster greeting eid",
            payment_evidence_status="confirmed",
            pdf_kind="payment_receipt",
            normalized_type="image",
        )
        assert sem.category != MEDIA_PAYMENT_RECEIPT
        assert sem.category in {MEDIA_SOCIAL_IMAGE, MEDIA_RELIGIOUS_SOCIAL}

    def test_n28_non_commerce_eid_never_yields_payment_semantic_even_if_pe_confirmed(self):
        sem = classify_media_semantic(
            text_blob="",
            non_commerce_category="eid_greeting",
            payment_evidence_status="confirmed",
            pdf_kind="payment_receipt",
            normalized_type="image",
        )
        assert sem.category == MEDIA_RELIGIOUS_SOCIAL
        assert sem.category != MEDIA_PAYMENT_RECEIPT

    def test_n29_confirmed_override_downgrades_social_contradiction_and_clears_kind(self):
        meta = apply_semantic_layers_like_normalizer(
            {
                "payment_evidence_status": "confirmed",
                "pdf_kind": "payment_receipt",
                "payment_evidence_reason": "strong_success_phrase",
            },
            text_blob="eid greeting poster celebration",
            normalized_type="image",
        )
        assert meta["payment_evidence_status"] == "not_payment"
        assert meta.get("pdf_kind") not in PAYMENT_KINDS
        assert meta.get("image_kind") not in PAYMENT_KINDS
        assert_truth_consistent(meta)

    def test_n30_real_receipt_with_mabrook_stays_confirmed(self):
        md = build_metadata_after_payment_gate(
            REAL_RECEIPT_WITH_MABROOK,
            filename="receipt.pdf",
            normalized_type="document",
        )
        assert md["payment_evidence_status"] == "confirmed"
        assert md["pdf_kind"] == "payment_receipt"
        assert_truth_consistent(md)
