"""
tests/test_video_passthrough.py
────────────────────────────────
Lock-in coverage for the lightweight video passthrough (May 2026).

Before this patch, inbound WhatsApp ``video`` messages fell through
to the webhook's ``INBOUND_IGNORED_UNSUPPORTED`` branch — the
customer got NO reply at all, even when the video had a useful
caption ("خاص بارك الله بك لاترسل" / a Hajj dua reel / a
beekeeping clip).

The fix policy (per user spec):
  * Video that is NOT a receipt and NOT a map MUST flow to the
    brain as ``general_media`` — caption + filename + lightweight
    forwarding signals only, no canned template, no heavy layer.
  * Payment / order / shipping / map short-circuits MUST refuse
    to fire on a ``video`` normalized_type.
  * One ``[MEDIA_ROUTE_TRACE] media_type=video`` log line per
    inbound video so on-call can grep production.

These tests pin all four guarantees.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock


_BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


# P1-E: vision/download failure is metadata-only — never customer-facing brain text.
_FORBIDDEN_CUSTOMER_FACING_IN_BRAIN = (
    "تعذّر استخراج وصف بصري",
    "لم أتمكن من مشاهدة",
    "لم أستطيع مشاهدة",
    "لا أستطيع مشاهدة",
    "غير مفهوم",
    "ممنوع القراءة",
    "حافظ على ربط المحادثة بالطلب أو الشحنة",
)


def _assert_brain_text_has_no_customer_facing_failure(text: str | None) -> None:
    body = text or ""
    for phrase in _FORBIDDEN_CUSTOMER_FACING_IN_BRAIN:
        assert phrase not in body, (
            f"forbidden customer-facing phrase in brain text: {phrase!r}"
        )


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def _patched_video_normaliser(monkeypatch):
    """Patch out the network-side ``_download_meta_media`` so we test
    the brain-facing text + metadata + trace without hitting Meta.
    The persistence helper is also patched so we don't write to disk.
    """
    from modules.ai.media import normalizer as _nrm

    monkeypatch.setattr(
        _nrm, "_download_meta_media",
        AsyncMock(return_value=None),
    )
    return _nrm


# ──────────────────────────────────────────────────────────────────────
# 1. Dispatch — video msg_type reaches _process_video
# ──────────────────────────────────────────────────────────────────────


class TestVideoDispatch:

    def test_video_msg_type_routes_to_process_video(self, monkeypatch):
        """The top-level dispatcher must pick the video branch and
        return ``normalized_type='video'`` with ``should_process=True``."""
        _nrm = _patched_video_normaliser(monkeypatch)

        async def _go():
            return await _nrm.normalize_whatsapp_inbound(
                db=MagicMock(),
                wa_conn=MagicMock(),
                tenant_id=1,
                message={
                    "type": "video",
                    "id": "wamid.V0",
                    "timestamp": "1715980000",
                    "video": {
                        "id": "media-1",
                        "mime_type": "video/mp4",
                        "caption": "بارك الله بك لاترسل",
                    },
                },
            )

        res = _run(_go())
        assert res.normalized_type == "video"
        assert res.should_process is True
        assert "[فيديو من العميل]" in (res.text or "")
        assert "بارك الله بك لاترسل" in (res.text or "")
        # Metadata contract.
        meta = res.metadata
        assert meta["source_type"] == "video"
        assert meta["mime_type"] == "video/mp4"
        assert meta["caption"] == "بارك الله بك لاترسل"
        assert meta["media_id"] == "media-1"

    def test_video_without_caption_still_processes(self, monkeypatch):
        """A bare video with no caption must STILL flow to the
        brain — the customer expects ANY reply, not silence."""
        _nrm = _patched_video_normaliser(monkeypatch)

        async def _go():
            return await _nrm.normalize_whatsapp_inbound(
                db=MagicMock(), wa_conn=MagicMock(), tenant_id=1,
                message={
                    "type": "video",
                    "id": "wamid.V1",
                    "timestamp": "1715980001",
                    "video": {"id": "media-2", "mime_type": "video/mp4"},
                },
            )

        res = _run(_go())
        assert res.normalized_type == "video"
        assert res.should_process is True
        assert "[فيديو من العميل]" in (res.text or "")
        # P1-E: still a customer video turn; failure details stay in metadata only.
        _assert_brain_text_has_no_customer_facing_failure(res.text)
        assert "هذا فيديو من العميل" in (res.text or "")
        assert res.metadata.get("video_download_status") == "failed"
        assert res.metadata.get("frame_vision_error") == "video_not_downloaded"

    def test_forwarded_marker_is_threaded_to_brain(self, monkeypatch):
        """``context.frequently_forwarded`` gives the brain a strong
        tone hint that the video is generic / viral content (dua /
        greeting reel) and not a customer-specific question."""
        _nrm = _patched_video_normaliser(monkeypatch)

        async def _go():
            return await _nrm.normalize_whatsapp_inbound(
                db=MagicMock(), wa_conn=MagicMock(), tenant_id=1,
                message={
                    "type": "video",
                    "id": "wamid.V2",
                    "timestamp": "1715980002",
                    "video": {"id": "media-3", "mime_type": "video/mp4"},
                    "context": {"frequently_forwarded": True},
                },
            )

        res = _run(_go())
        assert "أُعيد توجيهه مرات عديدة" in (res.text or "")
        assert res.metadata["frequently_forwarded"] is True


# ──────────────────────────────────────────────────────────────────────
# 2. Short-circuits — video must NEVER trigger payment/order/map paths
# ──────────────────────────────────────────────────────────────────────


class TestVideoBypassesShortCircuits:
    """Defensive lock-in: even if a future caller mistakenly sets
    ``image_kind=payment_receipt`` on a video, the order_flow
    short-circuits MUST refuse to fire because the gate is on
    ``normalized_type in ('document', 'image')`` — videos are
    excluded by design."""

    def test_video_does_not_trigger_receipt_short_circuit(self):
        from core.order_flow import maybe_handle_receipt_inbound

        decision = maybe_handle_receipt_inbound(
            db=MagicMock(),
            tenant_id=1,
            phone="+966500000060",
            inbound_normalized_type="video",
            inbound_metadata={
                # Deliberately set the slots that would otherwise
                # fire the short-circuit on an image:
                "image_kind": "payment_receipt",
                "payment_evidence_status": "confirmed",
            },
        )
        assert decision is None

    def test_video_does_not_trigger_payment_evidence_short_circuit(self):
        from core.order_flow import maybe_handle_payment_evidence_inbound

        decision = maybe_handle_payment_evidence_inbound(
            db=MagicMock(),
            tenant_id=1,
            phone="+966500000061",
            inbound_normalized_type="video",
            inbound_metadata={
                "image_kind": "payment_pre_review",
                "payment_evidence_status": "pre_transfer_review",
            },
        )
        assert decision is None

    def test_video_does_not_trigger_map_short_circuit(self):
        from core.order_flow import maybe_handle_map_image_inbound

        decision = maybe_handle_map_image_inbound(
            db=MagicMock(),
            tenant_id=1,
            phone="+966500000062",
            inbound_normalized_type="video",
            inbound_metadata={"image_kind": "map_screenshot"},
        )
        assert decision is None


# ──────────────────────────────────────────────────────────────────────
# 3. MEDIA_ROUTE_TRACE — one grep-able line per inbound video
# ──────────────────────────────────────────────────────────────────────


class TestVideoMediaRouteTrace:

    def test_video_emits_media_route_trace_with_required_fields(self, monkeypatch):
        _nrm = _patched_video_normaliser(monkeypatch)

        # Boost the logger so the trace makes it to our handler.
        records: list = []

        class _H(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        h = _H(); h.setLevel(logging.DEBUG)
        prev_level     = _nrm.logger.level
        prev_propagate = _nrm.logger.propagate
        _nrm.logger.setLevel(logging.DEBUG)
        _nrm.logger.propagate = True
        _nrm.logger.addHandler(h)
        try:
            async def _go():
                return await _nrm.normalize_whatsapp_inbound(
                    db=MagicMock(), wa_conn=MagicMock(), tenant_id=42,
                    message={
                        "type": "video",
                        "id": "wamid.MRT",
                        "timestamp": "1715980003",
                        "video": {
                            "id": "media-X",
                            "mime_type": "video/mp4",
                            "caption": "خاص بارك الله",
                            "filename": "VID_0001.mp4",
                        },
                        "context": {"forwarded": True},
                    },
                )
            _run(_go())
        finally:
            _nrm.logger.removeHandler(h)
            _nrm.logger.setLevel(prev_level)
            _nrm.logger.propagate = prev_propagate

        trace_line = next(
            (m for m in records if "[MEDIA_ROUTE_TRACE]" in m),
            None,
        )
        assert trace_line is not None, (
            "expected one [MEDIA_ROUTE_TRACE] line per inbound video"
        )
        for fragment in (
            "media_type=video",
            "tenant=42",
            "media_id=media-X",
            "mime=video/mp4",
            "filename='VID_0001.mp4'",
            "caption='خاص بارك الله'",
            "forwarded=True",
            "thumbnail_available=False",
            "ocr_text_preview=''",
            "final_route=vision_brain",
            "block_reason=none",
        ):
            assert fragment in trace_line, (
                f"missing required field {fragment!r}\n"
                f"trace: {trace_line}"
            )


# ──────────────────────────────────────────────────────────────────────
# 4. UI surface — conversations API returns a video media block
# ──────────────────────────────────────────────────────────────────────


class TestConversationsApiSurfacesVideo:
    """Lock the contract between the persisted ``normalized_inbound``
    metadata (written by ``_handle_merchant_message``) and the
    ``_media_block`` helper that the conversations messages endpoint
    uses to populate ``message.media`` for the dashboard.

    Pre-May-2026, ``_media_block`` only matched ``source_type`` in
    ``{audio, image}`` — a video row therefore came back as a plain
    text bubble even after the webhook accepted it. These tests pin
    that a video row produces a ``kind='video'`` block carrying the
    storage_url + duration + filename the UI expects.
    """

    def test_video_row_produces_video_media_block(self):
        from routers import conversations as _conv

        extra_metadata = {
            "normalized_inbound": {
                "source_type":          "video",
                "storage_url":          "/media/inbound/42/abc123.mp4",
                "mime_type":            "video/mp4",
                "duration_seconds":     7,
                "video_download_status": "ok",
                "caption":              "بارك الله بك",
                "filename":             "VID_0001.mp4",
                "forwarded":            True,
                "frequently_forwarded": False,
            }
        }

        block = _conv._build_media_block(
            message_event_id=1234, meta=extra_metadata,
        )
        assert block is not None
        assert block["kind"]            == "video"
        assert block["storage_url"]     == "/media/inbound/42/abc123.mp4"
        assert block["mime_type"]       == "video/mp4"
        assert block["duration"]        == 7
        assert block["caption"]         == "بارك الله بك"
        assert block["filename"]        == "VID_0001.mp4"
        assert block["forwarded"]       is True
        assert block["frequently_forwarded"] is False
        assert block["download_status"] == "ok"

    def test_audio_and_image_blocks_still_work(self):
        """Regression guard — adding video must NOT break the
        audio / image branches the dashboard has shipped for a year.
        """
        from routers import conversations as _conv

        audio_block = _conv._build_media_block(
            message_event_id=1,
            meta={
                "normalized_inbound": {
                    "source_type":          "audio",
                    "storage_url":          "/media/inbound/1/a.ogg",
                    "mime_type":            "audio/ogg",
                    "duration_seconds":     3,
                    "voice":                True,
                    "transcript_text":      "السلام عليكم",
                    "transcript_status":    "ok",
                    "audio_download_status": "ok",
                    "ai_used_audio":        True,
                },
            },
        )
        assert audio_block is not None
        assert audio_block["kind"] == "audio"

        image_block = _conv._build_media_block(
            message_event_id=2,
            meta={
                "normalized_inbound": {
                    "source_type":          "image",
                    "storage_url":          "/media/inbound/1/i.jpg",
                    "mime_type":            "image/jpeg",
                    "vision_text":          "صورة منتج",
                    "vision_status":        "ok",
                    "image_download_status": "ok",
                    "ai_used_image":        True,
                },
            },
        )
        assert image_block is not None
        assert image_block["kind"] == "image"

    def test_unknown_source_type_returns_none(self):
        from routers import conversations as _conv
        assert _conv._build_media_block(
            message_event_id=1, meta={"normalized_inbound": {"source_type": "sticker"}},
        ) is None


# ──────────────────────────────────────────────────────────────────────
# 5. Topic inference — light hints from caption + filename only
# ──────────────────────────────────────────────────────────────────────


class TestVideoTopicHints:
    """``_infer_video_topic_hints`` is the lightweight content-aware
    layer: caption + filename are pattern-matched to short topic
    labels (دعاء/نحل/منتج/شحنة/شكوى) so the brain can engage with
    the video instead of replying "ما أقدر أشوف الفيديو". The
    function is intentionally narrow — auto-generated filenames are
    ignored, no-signal videos return an empty list, the brain
    keeps its persona + conversation context."""

    def test_dua_caption_hints_greeting(self, monkeypatch):
        _nrm = _patched_video_normaliser(monkeypatch)
        hits = _nrm._infer_video_topic_hints(
            caption="يارب استجب لنا في عشر ذي الحجة",
            filename="",
        )
        assert "دعاء_أو_تهنئة" in hits

    def test_beekeeping_caption_hints_bees(self, monkeypatch):
        _nrm = _patched_video_normaliser(monkeypatch)
        hits = _nrm._infer_video_topic_hints(
            caption="شوف خلية النحل هذي",
            filename="",
        )
        assert "نحل_أو_عسل" in hits

    def test_product_caption_hints_product(self, monkeypatch):
        _nrm = _patched_video_normaliser(monkeypatch)
        hits = _nrm._infer_video_topic_hints(
            caption="ابي اشتري هذا المنتج بسرعة",
            filename="",
        )
        assert "منتج_أو_شراء" in hits

    def test_shipment_caption_hints_shipment(self, monkeypatch):
        _nrm = _patched_video_normaliser(monkeypatch)
        hits = _nrm._infer_video_topic_hints(
            caption="وين طلبي ومتى تتبع شحنتي؟",
            filename="",
        )
        assert "شحنة_أو_توصيل" in hits

    def test_auto_filename_does_not_create_false_hits(self, monkeypatch):
        """``VID_20260518_142301.mp4`` is metadata, not content.
        The inference must NOT use it as a signal."""
        _nrm = _patched_video_normaliser(monkeypatch)
        hits = _nrm._infer_video_topic_hints(
            caption="",
            filename="VID_20260518_142301.mp4",
        )
        assert hits == []

    def test_meaningful_filename_does_drive_hits(self, monkeypatch):
        """A user-typed filename like ``Hajj_dua_reel.mp4`` DOES carry
        signal — we should hint at دعاء/تهنئة."""
        _nrm = _patched_video_normaliser(monkeypatch)
        hits = _nrm._infer_video_topic_hints(
            caption="",
            filename="Hajj_dua_reel.mp4",
        )
        assert "دعاء_أو_تهنئة" in hits

    def test_empty_signals_return_no_hits(self, monkeypatch):
        _nrm = _patched_video_normaliser(monkeypatch)
        assert _nrm._infer_video_topic_hints(caption="", filename="") == []

    def test_brain_text_surfaces_topic_hints_without_failure_notes(self, monkeypatch):
        """P1-E: topic hints reach the brain; vision/failure notes do not."""
        _nrm = _patched_video_normaliser(monkeypatch)

        async def _go():
            return await _nrm.normalize_whatsapp_inbound(
                db=MagicMock(), wa_conn=MagicMock(), tenant_id=1,
                message={
                    "type": "video",
                    "id": "wamid.HINT",
                    "timestamp": "1715980111",
                    "video": {
                        "id": "media-h",
                        "mime_type": "video/mp4",
                        "caption": "يارب استجب",
                    },
                },
            )

        res = _run(_go())
        assert res.text is not None
        _assert_brain_text_has_no_customer_facing_failure(res.text)
        assert "دعاء_أو_تهنئة" in res.text
        assert "topic_hints" in res.metadata
        assert "دعاء_أو_تهنئة" in res.metadata["topic_hints"]

    def test_brain_text_for_no_signal_video_still_processes(self, monkeypatch):
        """Bare video (no caption, auto filename) still routes; no false hints."""
        _nrm = _patched_video_normaliser(monkeypatch)

        async def _go():
            return await _nrm.normalize_whatsapp_inbound(
                db=MagicMock(), wa_conn=MagicMock(), tenant_id=1,
                message={
                    "type": "video",
                    "id": "wamid.BARE",
                    "timestamp": "1715980222",
                    "video": {
                        "id": "media-b",
                        "mime_type": "video/mp4",
                        "filename": "VID_20260518_142301.mp4",
                    },
                },
            )

        res = _run(_go())
        assert res.text is not None
        assert res.should_process is True
        _assert_brain_text_has_no_customer_facing_failure(res.text)
        assert "[فيديو من العميل]" in res.text
        assert res.metadata.get("topic_hints") in (None, [])
        assert res.metadata.get("frame_vision_error") == "video_not_downloaded"


# ──────────────────────────────────────────────────────────────────────
# 6. Frame extraction + vision — "خفيف" pre-brain understanding layer
# ──────────────────────────────────────────────────────────────────────


class TestVideoFrameVision:
    """The May 2026 video-understanding layer: extract ONE frame via
    ffmpeg, run the same OpenAI vision describer the image branch
    uses, and surface the description on the brain-facing text so
    the brain receives an actual visual summary — not just metadata.

    Fail-open contract:
      * ffmpeg missing OR extraction fails → ``frame_vision_status``
        is 'skipped'/'failed', the video STILL reaches the brain.
      * vision_not_configured → 'skipped', no exception.
      * vision succeeds → ``frame_vision_text`` lands in the brain
        text under "النص الظاهر/الوصف من الفيديو: ..." and gets
        folded into ``topic_hints``.
    """

    def _patch_download_and_vision(
        self,
        monkeypatch,
        *,
        downloaded_bytes: bytes = b"FAKE_MP4_BYTES",
        frame_bytes=b"FAKE_JPEG_BYTES",
        vision_text: str = "",
        vision_raises: bool = False,
        openai_key: str = "sk-test",
    ):
        from modules.ai.media import normalizer as _nrm

        monkeypatch.setattr(
            _nrm, "_download_meta_media",
            AsyncMock(return_value={
                "bytes":     downloaded_bytes,
                "mime_type": "video/mp4",
            }),
        )
        monkeypatch.setattr(
            _nrm, "_try_persist",
            lambda **kwargs: None,
        )
        monkeypatch.setattr(
            _nrm, "_extract_video_frame",
            AsyncMock(return_value=frame_bytes),
        )
        monkeypatch.setattr(
            _nrm, "_runtime_openai_key",
            lambda: openai_key,
        )

        async def _desc(**kwargs):
            if vision_raises:
                raise RuntimeError("vision blew up")
            return vision_text

        monkeypatch.setattr(
            _nrm, "_describe_image_with_openai",
            AsyncMock(side_effect=_desc),
        )
        return _nrm

    def test_frame_vision_text_lands_in_brain_text(self, monkeypatch):
        _nrm = self._patch_download_and_vision(
            monkeypatch,
            vision_text="صورة عيد عليها كتابة «يارب استجب» و «ذي الحجة».",
        )

        async def _go():
            return await _nrm.normalize_whatsapp_inbound(
                db=MagicMock(), wa_conn=MagicMock(), tenant_id=1,
                message={
                    "type": "video",
                    "id": "wamid.FV1",
                    "timestamp": "1715980333",
                    "video": {
                        "id": "media-fv",
                        "mime_type": "video/mp4",
                    },
                },
            )

        res = _run(_go())
        assert res.metadata["frame_extracted"] is True
        assert res.metadata["frame_vision_status"] == "ok"
        assert "ذي الحجة" in res.metadata["frame_vision_text"]
        # The brain-facing text MUST include the description so the
        # LLM sees what's in the video.
        assert "النص الظاهر/الوصف من الفيديو" in res.text
        assert "يارب استجب" in res.text
        # Topic hint now folds in vision text so the Hajj video
        # produces the greeting/dua hint even without a caption.
        assert "topic_hints" in res.metadata
        assert "دعاء_أو_تهنئة" in res.metadata["topic_hints"]
        _assert_brain_text_has_no_customer_facing_failure(res.text)
        assert res.metadata.get("product_media_signal") is not True

    def test_vision_failure_does_not_drop_video(self, monkeypatch):
        _nrm = self._patch_download_and_vision(
            monkeypatch, vision_raises=True,
        )

        async def _go():
            return await _nrm.normalize_whatsapp_inbound(
                db=MagicMock(), wa_conn=MagicMock(), tenant_id=1,
                message={
                    "type": "video",
                    "id": "wamid.FV2",
                    "timestamp": "1715980334",
                    "video": {
                        "id": "media-fv2",
                        "mime_type": "video/mp4",
                        "caption": "يارب",
                    },
                },
            )

        res = _run(_go())
        # Video still reaches the brain.
        assert res.normalized_type == "video"
        assert res.should_process is True
        assert res.metadata["frame_extracted"] is True
        assert res.metadata["frame_vision_status"] == "failed"
        assert res.metadata.get("frame_vision_error")
        _assert_brain_text_has_no_customer_facing_failure(res.text)
        # Caption-driven topic hint still works for downstream routing.
        assert "دعاء_أو_تهنئة" in res.metadata.get("topic_hints") or []

    def test_no_openai_key_skips_vision_gracefully(self, monkeypatch):
        _nrm = self._patch_download_and_vision(
            monkeypatch, openai_key="",
        )

        async def _go():
            return await _nrm.normalize_whatsapp_inbound(
                db=MagicMock(), wa_conn=MagicMock(), tenant_id=1,
                message={
                    "type": "video",
                    "id": "wamid.FV3",
                    "timestamp": "1715980335",
                    "video": {"id": "media-fv3", "mime_type": "video/mp4"},
                },
            )

        res = _run(_go())
        assert res.metadata["frame_extracted"] is True
        assert res.metadata["frame_vision_status"] == "skipped"
        assert res.metadata["frame_vision_error"] == "vision_not_configured"
        # Brain still receives the video, just without the visual
        # summary. No exception, no canned excuse.
        assert res.normalized_type == "video"

    def test_no_frame_extracted_marks_skipped_and_continues(self, monkeypatch):
        _nrm = self._patch_download_and_vision(
            monkeypatch, frame_bytes=None,
        )

        async def _go():
            return await _nrm.normalize_whatsapp_inbound(
                db=MagicMock(), wa_conn=MagicMock(), tenant_id=1,
                message={
                    "type": "video",
                    "id": "wamid.FV4",
                    "timestamp": "1715980336",
                    "video": {"id": "media-fv4", "mime_type": "video/mp4"},
                },
            )

        res = _run(_go())
        assert res.metadata["frame_extracted"] is False
        assert res.metadata["frame_vision_status"] == "skipped"
        assert res.metadata["frame_vision_error"] == "frame_not_extracted"
        # The video is still routed to the brain.
        assert res.normalized_type == "video"
        assert res.should_process is True

    def test_video_understanding_trace_is_emitted(self, monkeypatch):
        _nrm = self._patch_download_and_vision(
            monkeypatch,
            vision_text="فيديو لخلية نحل بداخلها ملكة.",
        )

        records: list = []

        class _H(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        h = _H(); h.setLevel(logging.DEBUG)
        prev_level     = _nrm.logger.level
        prev_propagate = _nrm.logger.propagate
        _nrm.logger.setLevel(logging.DEBUG)
        _nrm.logger.propagate = True
        _nrm.logger.addHandler(h)
        try:
            async def _go():
                return await _nrm.normalize_whatsapp_inbound(
                    db=MagicMock(), wa_conn=MagicMock(), tenant_id=99,
                    message={
                        "type": "video",
                        "id": "wamid.VUT",
                        "timestamp": "1715980337",
                        "video": {
                            "id": "media-vut",
                            "mime_type": "video/mp4",
                        },
                    },
                )
            _run(_go())
        finally:
            _nrm.logger.removeHandler(h)
            _nrm.logger.setLevel(prev_level)
            _nrm.logger.propagate = prev_propagate

        trace = next(
            (m for m in records if "[VIDEO_UNDERSTANDING_TRACE]" in m),
            None,
        )
        assert trace is not None, (
            "expected one [VIDEO_UNDERSTANDING_TRACE] line per inbound video"
        )
        for fragment in (
            "tenant=99",
            "media_id=media-vut",
            "frame_extracted=True",
            "frame_vision_status=ok",
            "نحل",  # vision text leaked into preview
            "نحل_أو_عسل",  # topic hint
        ):
            assert fragment in trace, (
                f"missing fragment {fragment!r}\ntrace: {trace}"
            )
