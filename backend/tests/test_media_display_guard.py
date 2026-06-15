"""
Regression tests — Media Display Guard (P0)

Ensures PDF/document extraction stays internal while the dashboard
and outbound customer replies show safe, short copy only.
"""
from __future__ import annotations

import pytest

from modules.ai.media.display_guard import (
    apply_media_display_outbound_guard,
    looks_like_media_extraction_dump,
)
from modules.ai.media.normalizer import (
    MediaNormalizationResult,
    _build_document_display_body,
    inbound_persist_body,
)


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

    def test_inbound_persist_body_falls_back_to_text_for_plain_messages(self):
        result = MediaNormalizationResult(
            normalized_type="text",
            text="مرحبا",
            should_process=True,
        )
        assert inbound_persist_body(result) == "مرحبا"


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
                "caption": "إيصال الدفع",
            }
        }
        block = conv._build_media_block(message_event_id=99, meta=meta)
        assert block is not None
        assert block["kind"] == "document"
        assert block["filename"] == "receipt.pdf"
        assert block["storage_url"] == "/media/inbound/7/receipt.pdf"
        assert block["summary"] == "تم التحويل بنجاح"
        assert block["download_status"] == "ok"

    def test_build_media_block_document_not_raw_full_text(self):
        from routers import conversations as conv

        full_dump = "A" * 5000
        meta = {
            "normalized_inbound": {
                "source_type": "document",
                "storage_url": "/media/inbound/1/x.pdf",
                "pdf_text_full": full_dump,
                "pdf_text_preview": full_dump[:280],
            }
        }
        block = conv._build_media_block(message_event_id=1, meta=meta)
        assert block is not None
        assert block.get("summary") == full_dump[:280]
        assert len(block.get("summary") or "") <= 280


# ── outbound guard ────────────────────────────────────────────────


class TestOutboundMediaDisplayGuard:
    @pytest.mark.parametrize(
        "payload",
        [
            "نص الملف المستخرج:\n" + ("شروط وأحكام " * 200),
            "[وثيقة PDF — تصنيف: فاتورة]\n" + ("x" * 1000),
            '{"pdf_text_full": "secret"}',
            "رابط الملف: https://lookaside.fbsbx.com/abc",
            "/media/inbound/33/secret.pdf",
        ],
    )
    def test_blocks_extraction_dumps(self, payload: str):
        assert looks_like_media_extraction_dump(payload)
        scrubbed, changed = apply_media_display_outbound_guard(payload)
        assert changed is True
        assert "نص الملف المستخرج" not in scrubbed
        assert len(scrubbed) < 120

    def test_allows_normal_short_reply(self):
        reply = "شكراً، استلمنا الملف وسنراجعه."
        assert not looks_like_media_extraction_dump(reply)
        out, changed = apply_media_display_outbound_guard(reply)
        assert changed is False
        assert out == reply

    def test_safety_long_unstructured_extraction(self):
        blob = "مستخرج من الملف: " + ("بيانات " * 300)
        assert looks_like_media_extraction_dump(blob)
        scrubbed, _ = apply_media_display_outbound_guard(blob)
        assert len(scrubbed) < 120


# ── ingestion pipeline alias ──────────────────────────────────────


class TestIngestionPipelineExport:
    def test_pipeline_reexports(self):
        from modules.ai.media import ingestion_pipeline as pipe

        assert pipe.normalize_whatsapp_inbound is not None
        assert pipe.inbound_persist_body is not None
        assert pipe.apply_media_display_outbound_guard is not None


# ── other media types unchanged ───────────────────────────────────


class TestOtherMediaBlocksUnchanged:
    def test_image_block_still_works(self):
        from routers import conversations as conv

        block = conv._build_media_block(
            message_event_id=2,
            meta={
                "normalized_inbound": {
                    "source_type": "image",
                    "storage_url": "/media/inbound/1/i.jpg",
                    "mime_type": "image/jpeg",
                    "vision_status": "ok",
                },
            },
        )
        assert block is not None
        assert block["kind"] == "image"

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
