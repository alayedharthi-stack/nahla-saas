"""
tests/test_media_semantic_classifier.py
───────────────────────────────────────
Regression: attachment acks must not assume payment without semantic proof.
"""
from __future__ import annotations

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.media.semantic_classifier import (
    MEDIA_PAYMENT_RECEIPT,
    MEDIA_PRODUCT_IMAGE,
    MEDIA_RELIGIOUS_SOCIAL,
    MEDIA_SOCIAL_IMAGE,
    MEDIA_UNRELATED,
    ACK_NEUTRAL,
    ACK_PAYMENT,
    ACK_SOCIAL,
    allows_payment_media_ack,
    apply_semantic_payment_override,
    classify_media_semantic,
    compose_neutral_attachment_ack,
)


class TestMediaSemanticClassifier:
    def test_random_image_is_neutral(self):
        sem = classify_media_semantic(
            text_blob="a photo of flowers in a garden",
            normalized_type="image",
        )
        assert sem.category in {MEDIA_UNRELATED, "unknown_media"}
        assert sem.ack_mode == ACK_NEUTRAL

    def test_social_image_not_payment(self):
        sem = classify_media_semantic(
            text_blob="",
            non_commerce_category="eid_greeting",
            normalized_type="image",
        )
        assert sem.category == MEDIA_RELIGIOUS_SOCIAL
        assert sem.ack_mode == ACK_SOCIAL
        assert not allows_payment_media_ack(
            semantic_category=sem.category,
            payment_evidence_status="needs_confirmation",
            awaiting_payment_receipt=True,
            has_active_order=True,
        )

    def test_unrelated_pdf_during_active_order_no_payment_ack(self):
        """ARCH-015 M-01: ACK blocked via policy — weak evidence truth preserved."""
        md = {
            "payment_evidence_status": "needs_confirmation",
            "pdf_kind": "payment_pending_evidence",
            "media_semantic_category": MEDIA_PRODUCT_IMAGE,
        }
        out = apply_semantic_payment_override(md)
        assert out["payment_evidence_status"] == "needs_confirmation"
        assert out.get("pdf_kind") == "payment_pending_evidence"
        assert not allows_payment_media_ack(
            semantic_category=MEDIA_PRODUCT_IMAGE,
            payment_evidence_status=out["payment_evidence_status"],
            awaiting_payment_receipt=True,
            has_active_order=True,
        )

    def test_confirmed_receipt_with_active_order(self):
        sem = classify_media_semantic(
            text_blob="transfer successful IBAN amount",
            payment_evidence_status="confirmed",
            pdf_kind="payment_receipt",
            normalized_type="document",
        )
        assert sem.category == MEDIA_PAYMENT_RECEIPT
        assert sem.ack_mode == ACK_PAYMENT
        assert allows_payment_media_ack(
            semantic_category=sem.category,
            payment_evidence_status="confirmed",
            awaiting_payment_receipt=True,
            has_active_order=True,
        )

    def test_neutral_ack_copy_has_no_payment_phrasing(self):
        ack = compose_neutral_attachment_ack("image")
        assert "بعد التحويل" not in ack
        assert "إيصال" not in ack
        assert "أتابع طلبك" not in ack

    def test_product_image_semantic(self):
        sem = classify_media_semantic(
            text_blob="jar of honey product on shelf price tag",
            normalized_type="image",
        )
        assert sem.category == MEDIA_PRODUCT_IMAGE

    def test_map_image_semantic(self):
        sem = classify_media_semantic(
            text_blob="",
            image_kind="map_screenshot",
            normalized_type="image",
        )
        assert sem.ack_mode == ACK_NEUTRAL
