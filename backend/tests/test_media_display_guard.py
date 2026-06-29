"""
Regression tests — Media Display Guard (P0) + Payment Evidence display.

Ensures PDF/image extraction stays internal while payment hints and
safe cards surface for merchants. Location routing must not fire on
media without explicit caption intent.
"""
from __future__ import annotations

import pytest

from modules.ai.media.display_guard import (
    apply_media_display_outbound_guard,
    looks_like_media_extraction_dump,
)
from modules.ai.media.document_display import (
    DOCUMENT_CARD_FALLBACK_AR,
    is_readable_document_summary,
    safe_document_summary_for_display,
)
from modules.ai.media.normalizer import (
    MediaNormalizationResult,
    _build_document_display_body,
    _build_image_display_body,
    inbound_persist_body,
)
from modules.ai.media.payment_evidence_hints import (
    attach_payment_evidence_hints,
    extract_payment_evidence_hints,
    safe_payment_hints_for_display,
)
from modules.ai.media.routing_guard import resolve_pre_brain_customer_message


# ── display_body / persist contract ───────────────────────────────


class TestDocumentDisplayBody:
    def test_display_body_excludes_extraction(self):
        display = _build_document_display_body(
            filename="invoice_ar.pdf",
            label_ar="فاتورة",
            caption="هذا إيصال",
            byte_size=204800,
        )
        assert "نص الملف المستخرج" not in display
        assert "invoice_ar.pdf" in display
        assert "فاتورة" in display
        assert len(display) < 200

    def test_brain_text_may_contain_extraction_display_body_does_not(self):
        extraction = "نص طويل " * 500
        brain = f"[وثيقة PDF — تصنيف: فاتورة]\nنص الملف المستخرج:\n{extraction}"
        display = _build_document_display_body(
            filename="long.pdf",
            label_ar="فاتورة",
        )
        result = MediaNormalizationResult(
            normalized_type="document",
            text=brain,
            display_body=display,
            metadata={"pdf_text_full": extraction[:8000]},
            should_process=True,
        )
        assert "نص الملف المستخرج" in result.text
        assert "نص الملف المستخرج" not in inbound_persist_body(result)
        assert inbound_persist_body(result) == display

    def test_image_display_body_hides_vision(self):
        display = _build_image_display_body(
            caption="",
            image_kind="payment_receipt",
        )
        assert "وصف الصورة" not in display
        assert "إيصال تحويل" in display


class TestSafeDocumentSummary:
    def test_garbled_arabic_pdf_preview_hidden(self):
        garbled = "ÙØ§ÙØ±Ø® Ø§ÙÙØªØµÙ ÙØ§ÙØ§Ø³Ù " * 20
        assert not is_readable_document_summary(garbled)
        assert safe_document_summary_for_display(garbled) is None

    def test_readable_arabic_preview_allowed(self):
        text = "تم التحويل بنجاح إلى حساب المتجر"
        assert is_readable_document_summary(text)
        assert safe_document_summary_for_display(text) == text

    def test_fallback_constant(self):
        assert "PDF" in DOCUMENT_CARD_FALLBACK_AR


# ── payment evidence hints (internal) ─────────────────────────────


class TestPaymentEvidenceHints:
    def test_arabic_receipt_pdf_extracts_structured_hints(self):
        blob = (
            "تم التحويل بنجاح\n"
            "مصرف الراجحي\n"
            "المبلغ 360.00 ريال\n"
            "رقم العملية FT123456789\n"
            "15/06/2026\n"
            "من: أحمد محمد"
        )
        meta = {
            "pdf_kind": "payment_receipt",
            "payment_evidence_status": "confirmed",
        }
        hints = extract_payment_evidence_hints(blob, meta)
        assert hints.get("payment_evidence_status") == "confirmed"
        assert hints.get("bank_name") == "مصرف الراجحي"
        assert hints.get("amount") == "360"
        assert hints.get("reference_number") == "FT123456789"
        assert hints.get("transfer_date") == "15/06/2026"
        assert "أحمد" in (hints.get("sender_name") or "")

    def test_garbled_blob_yields_status_only_or_empty_fields(self):
        garbled = "ÙØ§ÙØ±Ø® " * 50
        meta = {
            "pdf_kind": "payment_receipt",
            "payment_evidence_status": "needs_confirmation",
        }
        hints = extract_payment_evidence_hints(garbled, meta)
        assert hints.get("payment_evidence_status") == "needs_confirmation"
        assert "bank_name" not in hints or not hints["bank_name"]

    def test_attach_stores_on_metadata(self):
        meta = {
            "pdf_kind": "payment_receipt",
            "payment_evidence_status": "confirmed",
        }
        attach_payment_evidence_hints(
            meta,
            internal_text="تم التحويل بنجاح 250 ريال الراجحي FT99887766",
        )
        assert "payment_evidence_hints" in meta
        assert meta["payment_evidence_hints"].get("amount") == "250"

    def test_image_receipt_metadata_internal_only_in_block(self):
        from routers import conversations as conv

        meta = {
            "normalized_inbound": {
                "source_type": "image",
                "storage_url": "/media/inbound/1/r.jpg",
                "image_kind": "payment_receipt",
                "vision_text": "OCR dump with تم التحويل 500 ريال الراجحي",
                "vision_status": "ok",
                "payment_evidence_status": "confirmed",
                "payment_evidence_hints": {
                    "amount": "500",
                    "bank_name": "مصرف الراجحي",
                    "payment_evidence_status": "confirmed",
                },
            }
        }
        block = conv._build_media_block(message_event_id=5, meta=meta)
        assert block is not None
        assert block["kind"] == "image"
        assert block["description"] is None
        assert block["payment_evidence_hints"]["amount"] == "500"


# ── conversations API media block ─────────────────────────────────


class TestDocumentMediaBlock:
    def test_build_media_block_document(self):
        from routers import conversations as conv

        meta = {
            "normalized_inbound": {
                "source_type": "document",
                "storage_url": "/media/inbound/7/receipt.pdf",
                "mime_type": "application/pdf",
                "filename": "receipt.pdf",
                "byte_size": 512000,
                "document_download_status": "ok",
                "pdf_kind": "payment_receipt",
                "pdf_text_status": "ok",
                "pdf_text_preview": "تم التحويل بنجاح",
                "payment_evidence_hints": {"amount": "360", "bank_name": "مصرف الراجحي"},
                "caption": "إيصال الدفع",
            }
        }
        block = conv._build_media_block(message_event_id=99, meta=meta)
        assert block is not None
        assert block["kind"] == "document"
        assert block["summary"] == "تم التحويل بنجاح"
        assert block["payment_evidence_hints"]["amount"] == "360"
        assert "pdf_text_full" not in str(block)

    def test_garbled_preview_not_in_summary(self):
        from routers import conversations as conv

        garbled = "ÙØ§ÙØ±Ø® " * 40
        meta = {
            "normalized_inbound": {
                "source_type": "document",
                "storage_url": "/media/inbound/1/x.pdf",
                "pdf_text_full": garbled * 100,
                "pdf_text_preview": garbled[:280],
                "pdf_kind": "payment_receipt",
            }
        }
        block = conv._build_media_block(message_event_id=1, meta=meta)
        assert block is not None
        assert block.get("summary") is None


# ── pre-brain routing guard (location) ────────────────────────────


class TestMediaRoutingGuard:
    def test_pdf_brain_text_with_moqam_does_not_route(self):
        msg = resolve_pre_brain_customer_message(
            brain_text=(
                "[وثيقة PDF]\nنص الملف المستخرج:\n"
                "موقع المتجر: الرياض\nعنوان المحل"
            ),
            inbound_metadata={
                "source_type": "document",
                "caption": "",
            },
        )
        assert msg == ""
        from modules.ai.brain.commerce.contact_route_policy import is_location_query
        assert not is_location_query(msg)

    def test_image_brain_text_does_not_route_without_caption(self):
        msg = resolve_pre_brain_customer_message(
            brain_text="[وصف الصورة] موقع المتجر في الرياض",
            inbound_metadata={"source_type": "image", "caption": ""},
        )
        assert msg == ""
        from modules.ai.brain.commerce.contact_route_policy import is_location_query
        assert not is_location_query(msg)

    def test_pdf_after_prior_location_question_no_caption(self):
        """Simulates PDF arriving after customer asked location earlier."""
        msg = resolve_pre_brain_customer_message(
            brain_text="[وثيقة PDF — تصنيف: إيصال]\nنص الملف: وين موقعكم",
            inbound_metadata={"source_type": "document", "caption": ""},
        )
        from modules.ai.brain.commerce.contact_route_policy import is_location_query
        assert not is_location_query(msg)

    def test_explicit_location_caption_on_pdf_still_routes(self):
        msg = resolve_pre_brain_customer_message(
            brain_text="[وثيقة PDF]\nنص طويل...",
            inbound_metadata={
                "source_type": "document",
                "caption": "وين موقعكم؟",
            },
        )
        assert msg == "وين موقعكم؟"
        from modules.ai.brain.commerce.contact_route_policy import is_location_query
        assert is_location_query(msg)

    def test_plain_text_location_still_works(self):
        msg = resolve_pre_brain_customer_message(
            brain_text="وين موقعكم",
            inbound_metadata={"source_type": "text"},
        )
        from modules.ai.brain.commerce.contact_route_policy import is_location_query
        assert is_location_query(msg)


# ── outbound guard ────────────────────────────────────────────────


class TestOutboundMediaDisplayGuard:
    @pytest.mark.parametrize(
        "payload",
        [
            "نص الملف المستخرج:\n" + ("شروط وأحكام " * 200),
            "[وثيقة PDF — تصنيف: فاتورة]\n" + ("x" * 1000),
        ],
    )
    def test_blocks_extraction_dumps(self, payload: str):
        assert looks_like_media_extraction_dump(payload)
        scrubbed, changed = apply_media_display_outbound_guard(payload)
        assert changed is True
        assert "نص الملف المستخرج" not in scrubbed


class TestOtherMediaBlocksUnchanged:
    def test_audio_block_still_works(self):
        from routers import conversations as conv

        block = conv._build_media_block(
            message_event_id=3,
            meta={
                "normalized_inbound": {
                    "source_type": "audio",
                    "storage_url": "/media/inbound/1/a.ogg",
                    "transcript_status": "ok",
                },
            },
        )
        assert block is not None
        assert block["kind"] == "audio"

    def test_video_block_still_works(self):
        from routers import conversations as conv

        block = conv._build_media_block(
            message_event_id=4,
            meta={
                "normalized_inbound": {
                    "source_type": "video",
                    "storage_url": "/media/inbound/1/v.mp4",
                },
            },
        )
        assert block is not None
        assert block["kind"] == "video"
