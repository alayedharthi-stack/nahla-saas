"""Customer-origin vs OCR payment intent split."""
from __future__ import annotations

import logging

import pytest

from core.ai_libraries import is_payment_query
from modules.ai.brain.commerce.conversational_priority import (
    has_payment_outbound_consent,
    is_receipt_inbound,
)
from modules.ai.brain.commerce.customer_origin_intent import (
    classify_payment_intent_source,
    customer_origin_has_payment_request,
    filter_payment_media_attachments,
    is_payment_media_key,
    split_inbound_text,
)
from modules.ai.postprocess.safety_nets import apply_media_key_safety_net


def test_split_image_without_caption_uses_empty_customer_origin():
    split = split_inbound_text(
        "[وصف الصورة المرسلة] تحويل الراجحي iban باركود",
        inbound_metadata={
            "source_type": "image",
            "vision_text": "تحويل الراجحي iban باركود",
            "image_kind": "payment_pending_evidence",
        },
        normalized_type="image",
    )
    assert split.customer_origin == ""
    assert split.customer_origin_source == "empty"
    assert "راجحي" in split.evidence
    assert not customer_origin_has_payment_request(split.customer_origin)
    assert classify_payment_intent_source(split) in {"vision", "merged", "ocr"}


def test_split_image_with_caption_keeps_customer_origin_only():
    split = split_inbound_text(
        "وش هذا؟\n\n[وصف الصورة] تحويل الراجحي iban",
        inbound_metadata={
            "source_type": "image",
            "caption": "وش هذا؟",
            "vision_text": "تحويل الراجحي iban باركود",
        },
        normalized_type="image",
    )
    assert split.customer_origin == "وش هذا؟"
    assert not customer_origin_has_payment_request(split.customer_origin)
    assert is_payment_query(split.merged)


@pytest.mark.parametrize(
    "caption",
    [
        "أرسل الباركود",
        "كيف أدفع",
        "أرسل الحساب",
        "أرسل الآيبان",
        "ابي باركود الراجحي",
    ],
)
def test_explicit_customer_requests_detected(caption: str):
    split = split_inbound_text(
        f"{caption}\n\n[وصف الصورة] تحويل",
        inbound_metadata={
            "source_type": "image",
            "caption": caption,
            "vision_text": "تحويل الراجحي",
        },
        normalized_type="image",
    )
    assert customer_origin_has_payment_request(split.customer_origin)
    assert classify_payment_intent_source(split) == "customer_origin"


def test_pure_text_message_is_customer_origin():
    split = split_inbound_text(
        "كيف أدفع",
        inbound_metadata={"source_type": "text"},
        normalized_type="text",
    )
    assert split.customer_origin == "كيف أدفع"
    assert customer_origin_has_payment_request(split.customer_origin)


def test_payment_pending_evidence_is_receipt_inbound():
    assert is_receipt_inbound(
        {
            "image_kind": "payment_pending_evidence",
            "payment_evidence_status": "needs_confirmation",
        },
        normalized_type="image",
    )


def test_has_payment_outbound_consent_denies_ocr_only(caplog):
    caplog.set_level(logging.INFO, logger="nahla.brain.customer_origin_intent")
    allowed = has_payment_outbound_consent(
        "[وصف الصورة المرسلة] تحويل الراجحي iban باركود",
        inbound_metadata={
            "image_kind": "payment_pending_evidence",
            "vision_text": "تحويل الراجحي iban باركود",
            "source_type": "image",
        },
        normalized_type="image",
        tenant_id=33,
        route="test",
    )
    assert allowed is False
    assert any("[CUSTOMER_ORIGIN_INTENT]" in r.message for r in caplog.records)
    assert any("[PAYMENT_INTENT_SOURCE]" in r.message for r in caplog.records)


def test_has_payment_outbound_consent_allows_explicit_caption(caplog):
    caplog.set_level(logging.INFO, logger="nahla.brain.customer_origin_intent")
    allowed = has_payment_outbound_consent(
        "أرسل الباركود\n\n[وصف الصورة] anything",
        inbound_metadata={
            "caption": "أرسل الباركود",
            "source_type": "image",
        },
        normalized_type="image",
        tenant_id=33,
        route="test",
    )
    assert allowed is True


def test_media_key_safety_net_skips_payment_on_ocr_only(monkeypatch):
    monkeypatch.setattr(
        "modules.ai.postprocess.safety_nets.media_key_net_enabled",
        lambda: True,
    )

    class _FakeResolution:
        def to_attachment(self):
            return {"id": 42, "media_key": "payment_rajhi_barcode"}

    monkeypatch.setattr(
        "services.media_resolver.resolve_for_query",
        lambda db, tenant_id, q: (_FakeResolution(), "payment_rajhi_barcode"),
    )

    result = apply_media_key_safety_net(
        db=None,
        tenant_id=33,
        customer_msg="[وصف الصورة] الراجحي iban باركود",
        existing_media_attachments=[],
        detected_media_key_markers=0,
        inbound_metadata={
            "vision_text": "الراجحي iban باركود",
            "source_type": "image",
        },
        normalized_type="image",
    )
    assert result.fired is False
    assert result.skipped_reason == "no_customer_origin_payment_intent"


def test_filter_payment_media_attachments():
    atts = [
        {"id": 1, "media_key": "payment_rajhi_barcode"},
        {"id": 2, "media_key": "store_certificate"},
    ]
    filtered = filter_payment_media_attachments(atts, allow_payment=False)
    assert len(filtered) == 1
    assert filtered[0]["media_key"] == "store_certificate"
    assert is_payment_media_key("payment_rajhi_barcode")
