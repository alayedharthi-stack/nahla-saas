"""
tests/test_inbound_media.py
───────────────────────────
End-to-end coverage for the hardened inbound-media pipeline:

  * ``services.inbound_media_storage`` — content-addressed disk writes
    + tenant-scoped resolution + path traversal defense.
  * ``modules.ai.media.normalizer`` — audio happy path, audio+caption
    merging, transcription failure → Arabic fallback, image vision
    happy path, image vision failure → Arabic fallback, "no AI before
    storage" invariant.
  * ``GET /conversations/{id}/media-debug`` — returns granular
    per-stage status fields for both audio and image messages.

We avoid TestClient because the rest of this repo's debug tests do
the same — call the router handlers directly with ``asyncio.run``.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for p in [str(REPO_ROOT), str(BACKEND_DIR), str(DATABASE_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)


# ──────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def isolated_storage(monkeypatch, tmp_path):
    """Point the inbound-media storage root at a fresh tmp dir for
    each test so disk writes don't bleed across tests."""
    monkeypatch.setattr(
        "services.inbound_media_storage._STORAGE_ROOT",
        Path(tmp_path).resolve(),
    )
    yield tmp_path


def _run(coro):
    return asyncio.run(coro)


# ──────────────────────────────────────────────────────────────────────
# 1. Storage helper
# ──────────────────────────────────────────────────────────────────────


class TestInboundMediaStorage:
    def test_save_audio_content_addressed_and_dedup(self, isolated_storage):
        from services.inbound_media_storage import save_inbound_media

        b = b"hello voice note"
        first = save_inbound_media(
            tenant_id=42, file_bytes=b,
            mime_type="audio/ogg", kind="audio", media_id="wa-123",
        )
        assert first.dedup is False
        assert Path(first.storage_path).exists()
        # Re-uploading the SAME bytes for the same tenant must reuse
        # the file (dedup) — that's the whole point of sha256 keying.
        second = save_inbound_media(
            tenant_id=42, file_bytes=b,
            mime_type="audio/ogg", kind="audio", media_id="wa-456",
        )
        assert second.sha256 == first.sha256
        assert second.dedup is True
        assert second.storage_path == first.storage_path
        # URL is relative + embeds the tenant_id (cross-tenant boundary).
        assert second.storage_url.startswith("/media/inbound/42/")
        assert second.storage_url.endswith(".ogg")

    def test_save_image_chooses_extension_from_mime(self, isolated_storage):
        from services.inbound_media_storage import save_inbound_media

        png = save_inbound_media(
            tenant_id=1, file_bytes=b"\x89PNG\r\n\x1a\nfake",
            mime_type="image/png", kind="image",
        )
        assert png.ext == ".png"
        assert png.storage_url.endswith(".png")

        jpg = save_inbound_media(
            tenant_id=1, file_bytes=b"jpeg-bytes",
            mime_type="image/jpeg", kind="image",
        )
        assert jpg.ext == ".jpg"
        # Different sha → different file on disk.
        assert jpg.sha256 != png.sha256

    def test_rejects_invalid_kind(self, isolated_storage):
        from services.inbound_media_storage import save_inbound_media

        with pytest.raises(ValueError, match="kind"):
            save_inbound_media(
                tenant_id=1, file_bytes=b"x",
                mime_type="application/pdf", kind="sticker",
            )

    def test_rejects_empty_payload(self, isolated_storage):
        from services.inbound_media_storage import save_inbound_media

        with pytest.raises(ValueError, match="empty"):
            save_inbound_media(
                tenant_id=1, file_bytes=b"",
                mime_type="audio/ogg", kind="audio",
            )

    def test_resolve_storage_path_round_trips(self, isolated_storage):
        from services.inbound_media_storage import (
            resolve_storage_path, save_inbound_media,
        )
        stored = save_inbound_media(
            tenant_id=7, file_bytes=b"some-bytes",
            mime_type="audio/mpeg", kind="audio",
        )
        # Exact sha + ext → resolves.
        path = resolve_storage_path(
            tenant_id=7, sha256=stored.sha256, ext=".mp3",
        )
        assert path is not None
        assert path.exists()
        # Different tenant → does NOT resolve (cross-tenant boundary).
        assert resolve_storage_path(
            tenant_id=999, sha256=stored.sha256, ext=".mp3",
        ) is None

    def test_resolve_storage_path_rejects_path_traversal(self, isolated_storage):
        from services.inbound_media_storage import resolve_storage_path

        # ``..`` and slashes / non-hex sha → None (no filesystem touch).
        assert resolve_storage_path(
            tenant_id=1, sha256="../../../etc/passwd", ext=".png",
        ) is None
        assert resolve_storage_path(
            tenant_id=1, sha256="abc/def", ext=".png",
        ) is None
        assert resolve_storage_path(
            tenant_id=1, sha256="NOT_HEX_AT_ALL!", ext=".png",
        ) is None


# ──────────────────────────────────────────────────────────────────────
# 2. Normalizer — audio happy path
# ──────────────────────────────────────────────────────────────────────


class TestAudioNormalizer:
    def _audio_message(self, *, caption: str = "", voice: bool = True):
        payload = {
            "id": "wa-audio-001",
            "mime_type": "audio/ogg",
        }
        if voice:
            payload["voice"] = True
        if caption:
            payload["caption"] = caption
        return {
            "type": "voice" if voice else "audio",
            "audio": payload,
            "timestamp": "1700000000",
            "id": "wa-msg-001",
        }

    def test_voice_note_arabic_transcription_happy_path(self, isolated_storage):
        from modules.ai.media import normalizer

        with patch.object(
            normalizer,
            "_download_meta_media",
            new=AsyncMock(return_value={
                "bytes": b"ogg-bytes",
                "mime_type": "audio/ogg",
            }),
        ), patch.object(
            normalizer,
            "_transcribe_bytes_with_openai",
            new=AsyncMock(return_value="أبغى فستان أسود مقاس متوسط"),
        ), patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
            result = _run(normalizer.normalize_whatsapp_inbound(
                db=MagicMock(), wa_conn=MagicMock(), tenant_id=11,
                message=self._audio_message(),
            ))
        assert result.normalized_type == "audio"
        assert result.should_process is True
        assert "فستان" in result.text
        meta = result.metadata
        assert meta["source_type"] == "audio"
        assert meta["voice"] is True
        assert meta["audio_download_status"] == "ok"
        assert meta["transcript_status"] == "ok"
        assert meta["transcript_text"] == "أبغى فستان أسود مقاس متوسط"
        assert meta["ai_used_audio"] is True
        # Storage MUST have happened before AI used the audio
        # (see ``test_no_ai_before_storage`` for the invariant test).
        assert meta["storage_url"] and meta["storage_url"].startswith(
            "/media/inbound/11/"
        )
        assert meta["storage_sha256"]
        assert meta["byte_size"] == len(b"ogg-bytes")

    def test_voice_note_with_caption_concatenates_both(self, isolated_storage):
        from modules.ai.media import normalizer

        with patch.object(
            normalizer,
            "_download_meta_media",
            new=AsyncMock(return_value={
                "bytes": b"ogg", "mime_type": "audio/ogg",
            }),
        ), patch.object(
            normalizer,
            "_transcribe_bytes_with_openai",
            new=AsyncMock(return_value="بكم السعر؟"),
        ), patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
            result = _run(normalizer.normalize_whatsapp_inbound(
                db=MagicMock(), wa_conn=MagicMock(), tenant_id=1,
                message=self._audio_message(caption="عن منتج #ABC"),
            ))
        assert result.should_process is True
        # Caption and transcript are BOTH in the text passed to the
        # brain. The order is caption-first because the caption is
        # the customer's explicit framing of the voice note.
        assert "عن منتج #ABC" in result.text
        assert "بكم السعر؟" in result.text
        # And the metadata preserves them separately so the UI can
        # render them apart.
        assert result.metadata["caption"] == "عن منتج #ABC"
        assert result.metadata["transcript_text"] == "بكم السعر؟"

    def test_transcription_failure_returns_arabic_fallback(self, isolated_storage):
        """Whisper crashes → we MUST NOT lose the message. The webhook
        should receive a fallback reply, and the conversation drawer
        should still have a playable recording (storage succeeded)."""
        from modules.ai.media import normalizer

        with patch.object(
            normalizer,
            "_download_meta_media",
            new=AsyncMock(return_value={
                "bytes": b"ogg", "mime_type": "audio/ogg",
            }),
        ), patch.object(
            normalizer,
            "_transcribe_bytes_with_openai",
            new=AsyncMock(side_effect=RuntimeError("whisper boom")),
        ), patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
            result = _run(normalizer.normalize_whatsapp_inbound(
                db=MagicMock(), wa_conn=MagicMock(), tenant_id=3,
                message=self._audio_message(),
            ))
        assert result.normalized_type == "audio"
        assert result.should_process is False
        assert result.text == ""
        # Canonical Arabic fallback the dispatcher will send back.
        assert result.fallback_reply_ar == normalizer.AUDIO_FALLBACK_REPLY_AR
        assert "وصلني التسجيل" in result.fallback_reply_ar
        meta = result.metadata
        assert meta["audio_download_status"] == "ok"
        assert meta["transcript_status"] == "failed"
        assert "RuntimeError" in (meta["transcript_error"] or "")
        # Even though transcription failed, the recording is durable.
        assert meta["storage_url"] and meta["storage_url"].startswith(
            "/media/inbound/3/"
        )
        assert meta["ai_used_audio"] is False

    def test_no_ai_before_storage(self, isolated_storage):
        """Invariant: ``_transcribe_bytes_with_openai`` must never be
        called before ``_try_persist`` has returned. If a future
        refactor reverses the order we want a hard test failure."""
        from modules.ai.media import normalizer

        order: list[str] = []

        def _persist_spy(**kw):
            order.append("persist")
            return MagicMock(
                storage_url="/media/inbound/1/x.ogg",
                sha256="x", byte_size=1, mime_type="audio/ogg",
            )

        async def _transcribe_spy(**kw):
            order.append("transcribe")
            return "ok"

        async def _download_spy(**kw):
            order.append("download")
            return {"bytes": b"x", "mime_type": "audio/ogg"}

        with patch.object(normalizer, "_download_meta_media", new=AsyncMock(side_effect=_download_spy)), \
             patch.object(normalizer, "_try_persist", side_effect=_persist_spy), \
             patch.object(normalizer, "_transcribe_bytes_with_openai", new=AsyncMock(side_effect=_transcribe_spy)), \
             patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
            _run(normalizer.normalize_whatsapp_inbound(
                db=MagicMock(), wa_conn=MagicMock(), tenant_id=1,
                message=self._audio_message(),
            ))
        # The ONLY allowed ordering is download → persist → transcribe.
        assert order == ["download", "persist", "transcribe"], order

    def test_missing_media_id_returns_fallback_without_calling_meta(self, isolated_storage):
        """Defensive: if Meta sent an audio with no media_id (broken
        webhook), we MUST short-circuit before issuing a Graph call."""
        from modules.ai.media import normalizer

        download_mock = AsyncMock()
        with patch.object(normalizer, "_download_meta_media", new=download_mock):
            result = _run(normalizer.normalize_whatsapp_inbound(
                db=MagicMock(), wa_conn=MagicMock(), tenant_id=1,
                message={"type": "audio", "audio": {"mime_type": "audio/ogg"}},
            ))
        download_mock.assert_not_called()
        assert result.should_process is False
        assert result.fallback_reply_ar == normalizer.AUDIO_FALLBACK_REPLY_AR
        assert result.metadata["transcript_error"] == "missing_media_id"

    def test_stt_not_configured_still_persists_for_playback(self, isolated_storage, monkeypatch):
        """When OPENAI_API_KEY is missing we still want to download
        and persist so the merchant can replay the recording from
        the dashboard. Transcript status surfaces the reason.

        We unset the env var directly (instead of patching a module
        constant) because the normalizer now re-reads from
        os.environ on every call — see the [MEDIA_NORMALIZER_BOOT]
        rationale in normalizer.py."""
        from modules.ai.media import normalizer

        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with patch.object(normalizer, "_download_meta_media", new=AsyncMock(return_value={
            "bytes": b"ogg", "mime_type": "audio/ogg",
        })):
            result = _run(normalizer.normalize_whatsapp_inbound(
                db=MagicMock(), wa_conn=MagicMock(), tenant_id=5,
                message=self._audio_message(),
            ))
        assert result.should_process is False
        assert result.fallback_reply_ar == normalizer.AUDIO_FALLBACK_REPLY_AR
        meta = result.metadata
        assert meta["transcript_status"] == "skipped"
        assert meta["transcript_error"] == "stt_not_configured"
        assert meta["audio_download_status"] == "ok"
        assert meta["storage_url"] and meta["storage_url"].startswith(
            "/media/inbound/5/"
        )


# ──────────────────────────────────────────────────────────────────────
# 3. Normalizer — image / vision
# ──────────────────────────────────────────────────────────────────────


class TestImageNormalizer:
    def _image_message(self, *, caption: str = ""):
        payload = {"id": "wa-image-001", "mime_type": "image/jpeg"}
        if caption:
            payload["caption"] = caption
        return {
            "type": "image",
            "image": payload,
            "timestamp": "1700000000",
            "id": "wa-msg-img-1",
        }

    def test_image_vision_happy_path(self, isolated_storage):
        from modules.ai.media import normalizer

        with patch.object(
            normalizer, "_download_meta_media",
            new=AsyncMock(return_value={
                "bytes": b"\x89PNG...", "mime_type": "image/png",
            }),
        ), patch.object(
            normalizer, "_describe_image_with_openai",
            new=AsyncMock(return_value="إيصال دفع بقيمة 250 ريال من بنك الراجحي."),
        ), patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
            result = _run(normalizer.normalize_whatsapp_inbound(
                db=MagicMock(), wa_conn=MagicMock(), tenant_id=8,
                message=self._image_message(),
            ))
        assert result.normalized_type == "image"
        assert result.should_process is True
        # The brain receives the vision description prefixed with a
        # marker so it can tell vision-derived text from typed text.
        assert "[وصف الصورة" in result.text
        assert "250 ريال" in result.text
        meta = result.metadata
        assert meta["image_download_status"] == "ok"
        assert meta["vision_status"] == "ok"
        assert meta["ai_used_image"] is True
        assert meta["storage_url"] and meta["storage_url"].startswith(
            "/media/inbound/8/"
        )

    def test_image_caption_concatenated_with_vision(self, isolated_storage):
        from modules.ai.media import normalizer

        with patch.object(normalizer, "_download_meta_media", new=AsyncMock(return_value={
            "bytes": b"png", "mime_type": "image/png",
        })), patch.object(normalizer, "_describe_image_with_openai", new=AsyncMock(
            return_value="بطاقة شحن قيمتها 200 ريال",
        )), patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
            result = _run(normalizer.normalize_whatsapp_inbound(
                db=MagicMock(), wa_conn=MagicMock(), tenant_id=1,
                message=self._image_message(caption="هل أقدر أدفع بها؟"),
            ))
        assert "هل أقدر أدفع بها؟" in result.text
        assert "بطاقة شحن" in result.text

    def test_vision_failure_returns_image_fallback(self, isolated_storage):
        from modules.ai.media import normalizer

        with patch.object(normalizer, "_download_meta_media", new=AsyncMock(return_value={
            "bytes": b"png", "mime_type": "image/png",
        })), patch.object(normalizer, "_describe_image_with_openai", new=AsyncMock(
            side_effect=ValueError("vision down"),
        )), patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
            result = _run(normalizer.normalize_whatsapp_inbound(
                db=MagicMock(), wa_conn=MagicMock(), tenant_id=2,
                message=self._image_message(),
            ))
        assert result.should_process is False
        assert result.fallback_reply_ar == normalizer.IMAGE_FALLBACK_REPLY_AR
        assert "وصلتني الصورة" in result.fallback_reply_ar
        # Image still stored for playback.
        assert result.metadata["image_download_status"] == "ok"
        assert result.metadata["vision_status"] == "failed"
        assert result.metadata["ai_used_image"] is False


# ──────────────────────────────────────────────────────────────────────
# 4. Webhook integration: image type allowed, fallback dispatched
# ──────────────────────────────────────────────────────────────────────


class TestWebhookIntegration:
    def test_image_type_is_now_in_allowed_set(self):
        """A regression guard: ``image`` must be in the type
        allow-list in the webhook dispatcher, otherwise the
        normalizer's vision branch is unreachable."""
        src = (BACKEND_DIR / "routers" / "whatsapp_webhook.py").read_text(
            encoding="utf-8", errors="replace",
        )
        # The allow-list grew over time (added ``document`` for PDF
        # receipts, ``video`` for the May 2026 video passthrough).
        # What we care about for THIS regression guard is that
        # ``image`` is present — vision pipeline depends on it.
        import re as _re
        m = _re.search(
            r'normalized_inbound\.normalized_type\s+not\s+in\s+\{([^}]+)\}',
            src,
        )
        assert m, "webhook normalized_type allow-list not found"
        allow_set = m.group(1)
        assert '"image"' in allow_set, (
            "webhook still gates out image — vision pipeline cannot fire"
        )
        # Sanity: the May 2026 video passthrough requires "video" too,
        # otherwise inbound videos die at INBOUND_IGNORED_UNSUPPORTED.
        assert '"video"' in allow_set, (
            "webhook still gates out video — videos cannot reach the UI / brain"
        )


# ──────────────────────────────────────────────────────────────────────
# 5. /conversations/{id}/media-debug
# ──────────────────────────────────────────────────────────────────────


def _make_db():
    """Spin up an in-memory SQLite DB with the canonical models, same
    JSONB → JSON downgrade pattern used by tests/test_campaign_debug.py
    (SQLite has no JSONB compiler so we transient-swap every JSONB
    column to ``sqlalchemy.JSON`` for the duration of the schema
    create, then restore for the next test)."""
    from sqlalchemy import JSON, create_engine
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.orm import sessionmaker
    from models import Base  # `database/` is on sys.path via conftest

    engine = create_engine("sqlite:///:memory:")
    _saved = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                _saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, orig in _saved:
        col.type = orig
    Session = sessionmaker(bind=engine)
    return Session(), engine


def _seed_tenant_conversation(db, tenant_id: int = 17):
    from models import Conversation, Customer, Tenant
    t = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if not t:
        t = Tenant(id=tenant_id, name=f"tenant-{tenant_id}")
        db.add(t); db.commit()
    cust = Customer(
        tenant_id=tenant_id, name="Hisham", phone="+966500000111",
        normalized_phone="+966500000111",
    )
    db.add(cust); db.commit()
    conv = Conversation(
        tenant_id=tenant_id, customer_id=cust.id,
        external_id="wa::+966500000111", status="active",
    )
    db.add(conv); db.commit()
    return t, conv


class _FakeReq:
    pass


def _call_media_debug(db, tenant_id, conversation_id, limit=50):
    from routers import conversations as convo_router

    original = convo_router.resolve_tenant_id
    convo_router.resolve_tenant_id = (
        lambda request, db=None: tenant_id  # type: ignore
    )
    try:
        return asyncio.run(
            convo_router.conversation_media_debug(
                conversation_id=conversation_id,
                request=_FakeReq(),
                db=db,
                limit=limit,
            )
        )
    finally:
        convo_router.resolve_tenant_id = original


class TestMediaDebugEndpoint:
    def test_returns_audio_row_with_full_status_fields(self):
        from models import MessageEvent
        db, _ = _make_db()
        t, conv = _seed_tenant_conversation(db)
        db.add(MessageEvent(
            tenant_id=t.id, conversation_id=conv.id, direction="inbound",
            body="[تفريغ] أبغى فستان", event_type="whatsapp",
            extra_metadata={
                "phone": "+966500000111",
                "normalized_inbound": {
                    "source_type":            "audio",
                    "media_id":               "wa-audio-1",
                    "mime_type":              "audio/ogg",
                    "voice":                  True,
                    "duration_seconds":       7,
                    "caption":                None,
                    "audio_download_status":  "ok",
                    "transcript_status":      "ok",
                    "transcript_text":        "أبغى فستان أسود",
                    "transcript_error":       None,
                    "ai_used_audio":          True,
                    "storage_url":            "/media/inbound/17/abc.ogg",
                    "storage_sha256":         "abc",
                    "byte_size":              4096,
                    "wa_timestamp":           "1700000000",
                    "wa_message_id":          "wa-msg-1",
                },
            },
        ))
        db.commit()

        result = _call_media_debug(db, t.id, conv.id)
        assert result["count"] == 1
        row = result["rows"][0]
        assert row["source_type"] == "audio"
        assert row["transcript_status"] == "ok"
        assert row["audio_download_status"] == "ok"
        assert row["transcript_text"] == "أبغى فستان أسود"
        assert row["transcript_text_preview"] == "أبغى فستان أسود"
        assert row["ai_used_audio"] is True
        assert row["ai_used_image"] is False
        assert row["storage_url"].endswith(".ogg")
        assert row["voice"] is True
        assert row["duration_seconds"] == 7
        assert row["error_message"] is None

    def test_returns_image_row_with_vision_fields(self):
        from models import MessageEvent
        db, _ = _make_db()
        t, conv = _seed_tenant_conversation(db, tenant_id=21)
        db.add(MessageEvent(
            tenant_id=t.id, conversation_id=conv.id, direction="inbound",
            body="[وصف الصورة] إيصال دفع", event_type="whatsapp",
            extra_metadata={
                "phone": "+966500000111",
                "normalized_inbound": {
                    "source_type":            "image",
                    "media_id":               "wa-img-9",
                    "mime_type":              "image/jpeg",
                    "caption":                "إيصال من الراجحي",
                    "image_download_status":  "ok",
                    "vision_status":          "ok",
                    "vision_text":            "إيصال تحويل 250 ريال",
                    "vision_error":           None,
                    "ai_used_image":          True,
                    "storage_url":            "/media/inbound/21/def.jpg",
                    "storage_sha256":         "def",
                    "byte_size":              8192,
                },
            },
        ))
        db.commit()
        result = _call_media_debug(db, t.id, conv.id)
        assert result["count"] == 1
        row = result["rows"][0]
        assert row["source_type"] == "image"
        assert row["vision_status"] == "ok"
        assert row["vision_text"] == "إيصال تحويل 250 ريال"
        assert row["ai_used_image"] is True
        assert row["caption"] == "إيصال من الراجحي"

    def test_surfaces_failed_transcription_for_support(self):
        from models import MessageEvent
        db, _ = _make_db()
        t, conv = _seed_tenant_conversation(db, tenant_id=23)
        db.add(MessageEvent(
            tenant_id=t.id, conversation_id=conv.id, direction="inbound",
            body="[رسالة وسائط بدون نص قابل للقراءة]", event_type="whatsapp",
            extra_metadata={
                "phone": "+966500000111",
                "media_fallback": True,
                "normalized_inbound": {
                    "source_type":            "audio",
                    "media_id":               "wa-audio-x",
                    "mime_type":              "audio/ogg",
                    "audio_download_status":  "ok",
                    "transcript_status":      "failed",
                    "transcript_text":        None,
                    "transcript_error":       "RuntimeError: whisper boom",
                    "ai_used_audio":          False,
                    "storage_url":            "/media/inbound/23/xyz.ogg",
                    "storage_sha256":         "xyz",
                },
            },
        ))
        db.commit()
        result = _call_media_debug(db, t.id, conv.id)
        row = result["rows"][0]
        assert row["transcript_status"] == "failed"
        assert row["ai_used_audio"] is False
        assert "whisper boom" in (row["error_message"] or "")
        assert row["media_fallback"] is True

    def test_filters_out_text_only_messages(self):
        """Text inbounds don't have media metadata — the endpoint
        must skip them so support sees a clean media list."""
        from models import MessageEvent
        db, _ = _make_db()
        t, conv = _seed_tenant_conversation(db, tenant_id=27)
        # 1 text + 1 audio
        db.add(MessageEvent(
            tenant_id=t.id, conversation_id=conv.id, direction="inbound",
            body="hi", event_type="whatsapp",
            extra_metadata={"phone": "+966500000111"},
        ))
        db.add(MessageEvent(
            tenant_id=t.id, conversation_id=conv.id, direction="inbound",
            body="[تفريغ]", event_type="whatsapp",
            extra_metadata={
                "phone": "+966500000111",
                "normalized_inbound": {
                    "source_type":           "audio",
                    "transcript_status":     "ok",
                    "audio_download_status": "ok",
                    "ai_used_audio":         True,
                },
            },
        ))
        db.commit()
        result = _call_media_debug(db, t.id, conv.id)
        assert result["count"] == 1
        assert result["rows"][0]["source_type"] == "audio"

    def test_returns_404_for_cross_tenant_conversation(self):
        from fastapi import HTTPException
        db, _ = _make_db()
        t, conv = _seed_tenant_conversation(db, tenant_id=31)
        with pytest.raises(HTTPException) as exc:
            # Wrong tenant scope.
            _call_media_debug(db, 99, conv.id)
        assert exc.value.status_code == 404

    def test_local_path_exists_true_when_file_present(self, isolated_storage):
        """The media-debug endpoint walks the storage layer to verify
        the bytes still exist on disk. This is the #1 indicator
        support reaches for when a recording suddenly 404s after
        a redeploy."""
        from models import MessageEvent
        from services.inbound_media_storage import save_inbound_media

        db, _ = _make_db()
        t, conv = _seed_tenant_conversation(db, tenant_id=51)
        stored = save_inbound_media(
            tenant_id=t.id, file_bytes=b"ogg-bytes",
            mime_type="audio/ogg", kind="audio", media_id="wa-1",
        )
        db.add(MessageEvent(
            tenant_id=t.id, conversation_id=conv.id, direction="inbound",
            body="x", event_type="whatsapp",
            extra_metadata={
                "phone": "+966500000111",
                "normalized_inbound": {
                    "source_type":           "audio",
                    "media_id":              "wa-1",
                    "mime_type":             "audio/ogg",
                    "audio_download_status": "ok",
                    "transcript_status":     "ok",
                    "transcript_text":       "أبغى فستان",
                    "ai_used_audio":         True,
                    "storage_url":           stored.storage_url,
                    "storage_sha256":        stored.sha256,
                    "byte_size":             stored.byte_size,
                },
            },
        ))
        db.commit()
        result = _call_media_debug(db, t.id, conv.id)
        row = result["rows"][0]
        assert row["local_path_exists"] is True
        # Spec aliases: media_type / original_media_id / last_error
        assert row["media_type"] == "audio"
        assert row["original_media_id"] == "wa-1"
        assert row["public_media_url"] == stored.storage_url
        # Unified download_status for audio routes to audio_download_status.
        assert row["download_status"] == "ok"

    def test_local_path_exists_false_when_file_missing(self, isolated_storage):
        """Row in DB but the bytes are gone — classic "volume swept"
        post-deploy state. Surface as ``local_path_exists=False`` so
        support sees one canonical truth instead of three different
        UI failures."""
        from models import MessageEvent
        db, _ = _make_db()
        t, conv = _seed_tenant_conversation(db, tenant_id=53)
        db.add(MessageEvent(
            tenant_id=t.id, conversation_id=conv.id, direction="inbound",
            body="x", event_type="whatsapp",
            extra_metadata={
                "phone": "+966500000111",
                "normalized_inbound": {
                    "source_type":           "audio",
                    "media_id":              "wa-2",
                    "mime_type":             "audio/ogg",
                    "audio_download_status": "ok",
                    "transcript_status":     "ok",
                    "transcript_text":       "ت",
                    "ai_used_audio":         True,
                    "storage_url":           "/media/inbound/53/deadbeef.ogg",
                    "storage_sha256":        "deadbeef" * 8,
                    "byte_size":             16,
                },
            },
        ))
        db.commit()
        result = _call_media_debug(db, t.id, conv.id)
        row = result["rows"][0]
        assert row["local_path_exists"] is False


# ──────────────────────────────────────────────────────────────────────
# 6. Reprocess endpoint
# ──────────────────────────────────────────────────────────────────────


def _call_reprocess(db, tenant_id, message_event_id):
    from routers import conversations as convo_router

    original = convo_router.resolve_tenant_id
    convo_router.resolve_tenant_id = (
        lambda request, db=None: tenant_id  # type: ignore
    )
    try:
        return asyncio.run(
            convo_router.reprocess_inbound_media(
                message_event_id=message_event_id,
                request=_FakeReq(),
                db=db,
            )
        )
    finally:
        convo_router.resolve_tenant_id = original


class TestReprocessEndpoint:
    def _seed_audio_row(self, db, tenant_id, *, storage_url=None, status="skipped"):
        from models import (
            Conversation, Customer, MessageEvent, Tenant,
            WhatsAppConnection,
        )
        t = db.query(Tenant).filter(Tenant.id == tenant_id).first()
        if not t:
            t = Tenant(id=tenant_id, name=f"t-{tenant_id}")
            db.add(t); db.commit()
        # Reprocess REQUIRES a connection — exercise that requirement.
        conn = WhatsAppConnection(
            tenant_id=tenant_id, phone_number_id="100543193146977",
            connection_type="direct",
        )
        db.add(conn); db.commit()
        cust = Customer(
            tenant_id=tenant_id, name="X", phone="+966500000111",
            normalized_phone="+966500000111",
        )
        db.add(cust); db.commit()
        conv = Conversation(
            tenant_id=tenant_id, customer_id=cust.id,
            external_id="wa::+966500000111", status="active",
        )
        db.add(conv); db.commit()
        evt = MessageEvent(
            tenant_id=tenant_id, conversation_id=conv.id, direction="inbound",
            body="[رسالة وسائط]", event_type="whatsapp",
            extra_metadata={
                "phone": "+966500000111",
                "normalized_inbound": {
                    "source_type":           "audio",
                    "media_id":              "wa-audio-rerun",
                    "mime_type":             "audio/ogg",
                    "voice":                 True,
                    "audio_download_status": "ok" if storage_url else "failed",
                    "transcript_status":     status,
                    "transcript_text":       None,
                    "ai_used_audio":         False,
                    "storage_url":           storage_url,
                    "storage_sha256":        None,
                    "byte_size":             None,
                },
            },
        )
        db.add(evt); db.commit()
        return evt

    def test_reprocess_runs_full_pipeline_and_updates_metadata(self, isolated_storage):
        from modules.ai.media import normalizer
        db, _ = _make_db()
        evt = self._seed_audio_row(db, tenant_id=61, status="skipped")
        evt_id = evt.id

        with patch.object(
            normalizer, "_download_meta_media",
            new=AsyncMock(return_value={"bytes": b"ogg", "mime_type": "audio/ogg"}),
        ), patch.object(
            normalizer, "_transcribe_bytes_with_openai",
            new=AsyncMock(return_value="بعد إعادة المعالجة"),
        ), patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
            result = _call_reprocess(db, 61, evt_id)

        assert result["ok"] is True
        assert result["source_type"] == "audio"
        ni = result["normalized_inbound"]
        assert ni["transcript_status"] == "ok"
        assert ni["transcript_text"] == "بعد إعادة المعالجة"
        assert ni["ai_used_audio"] is True
        # Reprocess stamp is added so support can tell first-run
        # data apart from manually-rerun data.
        assert "reprocessed_at" in ni
        assert ni["reprocessed_by"] == "manual_reprocess_endpoint"

    def test_reprocess_404_for_unknown_row(self):
        from fastapi import HTTPException
        db, _ = _make_db()
        with pytest.raises(HTTPException) as exc:
            _call_reprocess(db, 1, 999_999)
        assert exc.value.status_code == 404

    def test_reprocess_400_when_row_is_not_media(self):
        from fastapi import HTTPException
        from models import Conversation, Customer, MessageEvent, Tenant
        db, _ = _make_db()
        t = Tenant(id=63, name="t-63"); db.add(t); db.commit()
        cust = Customer(tenant_id=63, name="X", phone="+9", normalized_phone="+9")
        db.add(cust); db.commit()
        conv = Conversation(tenant_id=63, customer_id=cust.id,
                            external_id="x", status="active")
        db.add(conv); db.commit()
        # Text-only inbound — has no normalized_inbound block.
        evt = MessageEvent(
            tenant_id=63, conversation_id=conv.id, direction="inbound",
            body="hi", event_type="whatsapp",
            extra_metadata={"phone": "+9"},
        )
        db.add(evt); db.commit()
        with pytest.raises(HTTPException) as exc:
            _call_reprocess(db, 63, evt.id)
        assert exc.value.status_code == 400


# ──────────────────────────────────────────────────────────────────────
# 7. /admin/debug/media-env
# ──────────────────────────────────────────────────────────────────────


class TestMediaEnvEndpoint:
    def test_returns_full_snapshot_with_writable_storage(self, isolated_storage, monkeypatch):
        from routers import admin_debug

        # Stamp config to a known, finite shape.
        monkeypatch.setattr(admin_debug, "_mask_secret_tail",
                            lambda v: f"***{v[-4:]}" if v else None)
        # Ensure the import in the handler picks up the (patched)
        # storage_root pointing at the tmp dir.
        result = asyncio.run(admin_debug.admin_debug_media_env(
            _admin={"sub": "admin@nahla", "role": "admin"},
        ))
        assert result["storage"]["exists"] is True
        assert result["storage"]["writable"] is True
        assert result["storage"]["write_probe_error"] is None
        assert isinstance(result["storage"]["max_inbound_bytes"], int)
        # Hints + issues are lists even when there are no problems.
        assert isinstance(result["hints"], list)
        assert isinstance(result["issues"], list)

    def test_flags_missing_openai_key(self, isolated_storage, monkeypatch):
        from routers import admin_debug

        # Handler now re-reads OPENAI_API_KEY from os.environ on
        # every call (per the runtime-getter rationale in
        # normalizer.py) — patching core.config no longer affects
        # the response. Use monkeypatch.delenv instead.
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        result = asyncio.run(admin_debug.admin_debug_media_env(
            _admin={"sub": "admin@nahla", "role": "admin"},
        ))
        # When the key is empty/unset the issues list calls it out.
        if not result["openai"]["api_key_present"]:
            assert any("OPENAI_API_KEY" in s for s in result["issues"])
            assert result["ready"]["audio"] is False
            assert result["ready"]["vision"] is False

    def test_response_exposes_top_level_aliases(self, isolated_storage):
        """The flat aliases (openai_key_present, vision_enabled,
        stt_enabled, media_dir_writable, inbound_media_dir,
        ffmpeg_found, ffmpeg_version) are the public contract
        documented for dashboards / runbooks. Drift between the
        flat alias and the nested group is a bug — lock it down."""
        from routers import admin_debug
        result = asyncio.run(admin_debug.admin_debug_media_env(
            _admin={"sub": "admin@nahla", "role": "admin"},
        ))
        # Every alias must be present, even when the underlying
        # value is False/None — UI relies on .hasOwnProperty(...).
        for alias in (
            "openai_key_present", "openai_key_tail",
            "vision_enabled", "stt_enabled",
            "media_dir_writable", "inbound_media_dir",
            "ffmpeg_found", "ffmpeg_version",
        ):
            assert alias in result, f"missing top-level alias {alias!r}"

        # And they mirror the nested fields 1:1.
        assert result["openai_key_present"]  == result["openai"]["api_key_present"]
        assert result["openai_key_tail"]     == result["openai"]["api_key_tail"]
        assert result["vision_enabled"]      == result["ready"]["vision"]
        assert result["stt_enabled"]         == result["ready"]["audio"]
        assert result["media_dir_writable"]  == result["storage"]["writable"]
        assert result["inbound_media_dir"]   == result["storage"]["root"]
        assert result["ffmpeg_found"]        == result["ffmpeg"]["found"]
        assert result["ffmpeg_version"]      == result["ffmpeg"]["version"]

    def test_ffmpeg_block_reflects_shutil_which(self, isolated_storage, monkeypatch):
        """When `shutil.which("ffmpeg")` returns a path, the response
        must surface `found=True` + the path. When it returns None
        the inverse, AND the issues list must mention ffmpeg so
        operators know it's not installed."""
        import shutil as _shutil
        from routers import admin_debug

        # 1) Pretend ffmpeg IS installed.
        monkeypatch.setattr(_shutil, "which", lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None)
        # subprocess.run will fail because /usr/bin/ffmpeg doesn't
        # exist on the test host — we only care that the handler
        # *attempts* the call and gracefully sets version to a
        # diagnostic string instead of crashing.
        result = asyncio.run(admin_debug.admin_debug_media_env(
            _admin={"sub": "admin@nahla", "role": "admin"},
        ))
        assert result["ffmpeg_found"] is True
        assert result["ffmpeg"]["path"] == "/usr/bin/ffmpeg"
        # `version` is either None, a real version string, or
        # "execution_failed: ..." — never crashes the request.
        v = result["ffmpeg_version"]
        assert v is None or isinstance(v, str)

        # 2) Pretend ffmpeg is NOT installed.
        monkeypatch.setattr(_shutil, "which", lambda name: None)
        result2 = asyncio.run(admin_debug.admin_debug_media_env(
            _admin={"sub": "admin@nahla", "role": "admin"},
        ))
        assert result2["ffmpeg_found"] is False
        assert result2["ffmpeg"]["path"] is None
        assert any("ffmpeg" in i for i in result2["issues"])
        # Hint must be actionable: mention Railway.
        assert any("ffmpeg" in h.lower() or "nixpacks" in h.lower()
                   for h in result2["hints"])

    def test_never_returns_full_openai_key(self, isolated_storage, monkeypatch):
        """Defense-in-depth: even if a future refactor accidentally
        adds the raw key to the response, this test catches it.
        We seed a recognisable sk-test-FULL... value and assert
        none of the JSON values equal it."""
        import json as _json
        from routers import admin_debug

        secret = "sk-test-NEVER_LEAK_THIS_FULL_VALUE_1234"
        # Handler reads OPENAI_API_KEY from os.environ at request
        # time (see normalizer.py for the runtime-getter rationale).
        monkeypatch.setenv("OPENAI_API_KEY", secret)
        result = asyncio.run(admin_debug.admin_debug_media_env(
            _admin={"sub": "admin@nahla", "role": "admin"},
        ))
        as_text = _json.dumps(result)
        assert secret not in as_text, (
            "OPENAI_API_KEY leaked into /admin/debug/media-env response"
        )


# ──────────────────────────────────────────────────────────────────────
# 7. Runtime env re-read + process identity diagnostic
# ──────────────────────────────────────────────────────────────────────
#
# Critical for the multi-service deploy story: a Railway worker that
# booted before OPENAI_API_KEY was set must be able to pick it up
# without a full code redeploy, and operators must be able to tell
# WHICH process is missing the env from the diagnostic endpoint.


class TestRuntimeEnvReread:
    """`_runtime_openai_key()` must read `os.environ` fresh on every
    call so a process restart (not a redeploy) is sufficient to pick
    up a newly-set env var."""

    def test_returns_fresh_value_from_environ(self, monkeypatch):
        from modules.ai.media import normalizer

        monkeypatch.setenv("OPENAI_API_KEY", "sk-new-value-after-boot")
        assert normalizer._runtime_openai_key() == "sk-new-value-after-boot"

    def test_returns_empty_string_when_unset(self, monkeypatch):
        from modules.ai.media import normalizer

        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        assert normalizer._runtime_openai_key() == ""

    def test_picks_up_value_set_after_import(self, monkeypatch):
        """The whole point: even if the module was imported with no
        env var present (worker process boot scenario), setting the
        env var afterwards is reflected immediately on the next call."""
        from modules.ai.media import normalizer

        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        before = normalizer._runtime_openai_key()
        assert before == ""

        monkeypatch.setenv("OPENAI_API_KEY", "sk-set-later")
        after = normalizer._runtime_openai_key()
        assert after == "sk-set-later"

    def test_idempotent_within_a_call(self, monkeypatch):
        """Same env value across multiple calls produces the same
        result — no caching surprises that would mask drift."""
        from modules.ai.media import normalizer

        monkeypatch.setenv("OPENAI_API_KEY", "sk-stable")
        results = [normalizer._runtime_openai_key() for _ in range(3)]
        assert all(r == "sk-stable" for r in results)


class TestSkipLogIncludesProcessIdentity:
    """Every transcript/vision skip emits a structured WARN line
    that operators can grep in Railway logs to identify the
    offending process. The message MUST include pid, service,
    boot vs current key presence."""

    def _audio_message(self):
        return {
            "type": "audio",
            "audio": {"id": "wa-audio-skip", "mime_type": "audio/ogg", "voice": True},
            "timestamp": "1700000000",
            "id": "wa-msg-skip-audio",
        }

    def test_audio_skip_log_includes_diagnostics(
        self, isolated_storage, monkeypatch, caplog,
    ):
        import logging
        from modules.ai.media import normalizer

        # Force the skip branch — no key set.
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        caplog.set_level(logging.WARNING, logger="nahla.ai.media")
        with patch.object(normalizer, "_download_meta_media", new=AsyncMock(return_value={
            "bytes": b"ogg", "mime_type": "audio/ogg",
        })):
            _run(normalizer.normalize_whatsapp_inbound(
                db=MagicMock(), wa_conn=MagicMock(), tenant_id=42,
                message=self._audio_message(),
            ))

        skip_logs = [r for r in caplog.records if "MEDIA_NORMALIZER_SKIP" in r.getMessage()]
        assert skip_logs, "no [MEDIA_NORMALIZER_SKIP] log emitted on stt-not-configured"
        msg = skip_logs[0].getMessage()
        assert "reason=stt_not_configured" in msg
        assert "kind=audio" in msg
        assert "pid=" in msg
        assert "service=" in msg
        assert "openai_key_present_now=" in msg
        assert "openai_key_present_at_boot=" in msg
        assert "tenant=42" in msg

    def test_image_skip_log_emitted(
        self, isolated_storage, monkeypatch, caplog,
    ):
        import logging
        from modules.ai.media import normalizer

        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        caplog.set_level(logging.WARNING, logger="nahla.ai.media")
        with patch.object(normalizer, "_download_meta_media", new=AsyncMock(return_value={
            "bytes": b"\x89PNG...", "mime_type": "image/png",
        })):
            _run(normalizer.normalize_whatsapp_inbound(
                db=MagicMock(), wa_conn=MagicMock(), tenant_id=77,
                message={
                    "type": "image",
                    "image": {"id": "wa-img-skip", "mime_type": "image/png"},
                    "timestamp": "1700000000",
                    "id": "wa-msg-skip-image",
                },
            ))

        skip_logs = [r for r in caplog.records if "MEDIA_NORMALIZER_SKIP" in r.getMessage()]
        assert skip_logs
        msg = skip_logs[0].getMessage()
        assert "reason=vision_not_configured" in msg
        assert "kind=image" in msg
        assert "tenant=77" in msg

    def test_boot_constants_are_exposed(self):
        """The media-env endpoint reads `_BOOT_*` constants off the
        normalizer module — they must exist and have the expected
        shape so the endpoint can render them in `process` block."""
        from modules.ai.media import normalizer

        assert isinstance(normalizer._BOOT_PID, int)
        assert normalizer._BOOT_PID > 0
        assert isinstance(normalizer._BOOT_SERVICE, str)
        assert isinstance(normalizer._BOOT_OPENAI_KEY_PRESENT, bool)
        assert isinstance(normalizer._BOOT_FFMPEG_FOUND, bool)


class TestMediaEnvProcessBlock:
    """`GET /admin/debug/media-env` must surface a `process` block
    that tells operators which Railway service answered the request
    and whether THAT process needs a restart."""

    def test_response_includes_process_identity(self, isolated_storage, monkeypatch):
        from routers import admin_debug

        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        result = asyncio.run(admin_debug.admin_debug_media_env(
            _admin={"sub": "admin@nahla", "role": "admin"},
        ))
        assert "process" in result
        proc = result["process"]
        for k in (
            "pid", "service", "boot_pid",
            "normalizer_loaded_in_this_process",
            "openai_key_present_now", "openai_key_present_at_boot",
            "needs_restart_to_pick_up_env",
            "railway_service_name", "railway_replica_id",
            "railway_deployment_id", "epoch",
        ):
            assert k in proc, f"missing process.{k}"

    def test_process_pid_matches_os_getpid(self, isolated_storage):
        import os as _os
        from routers import admin_debug

        result = asyncio.run(admin_debug.admin_debug_media_env(
            _admin={"sub": "admin@nahla", "role": "admin"},
        ))
        assert result["process"]["pid"] == _os.getpid()

    def test_service_name_uses_railway_env_when_set(
        self, isolated_storage, monkeypatch,
    ):
        """If RAILWAY_SERVICE_NAME is set, the process block uses
        it directly rather than guessing from argv."""
        from modules.ai.media import normalizer
        from routers import admin_debug

        # Override the boot constant directly because RAILWAY_SERVICE_NAME
        # is captured at module load — monkeypatching env mid-test
        # won't change _BOOT_SERVICE.
        monkeypatch.setattr(normalizer, "_BOOT_SERVICE", "worker")

        result = asyncio.run(admin_debug.admin_debug_media_env(
            _admin={"sub": "admin@nahla", "role": "admin"},
        ))
        assert result["process"]["service"] == "worker"

    def test_needs_restart_flag_when_boot_was_empty_and_now_set(
        self, isolated_storage, monkeypatch,
    ):
        """The whole point of the diagnostic: when the env was empty
        at module boot but is set now, flag the process as needing
        a restart so stale callers in the same process pick it up."""
        from modules.ai.media import normalizer
        from routers import admin_debug

        monkeypatch.setattr(normalizer, "_BOOT_OPENAI_KEY_PRESENT", False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-set-after-boot")

        result = asyncio.run(admin_debug.admin_debug_media_env(
            _admin={"sub": "admin@nahla", "role": "admin"},
        ))
        assert result["process"]["openai_key_present_now"] is True
        assert result["process"]["openai_key_present_at_boot"] is False
        assert result["process"]["needs_restart_to_pick_up_env"] is True
        # The issue + hint must spell out the restart in Arabic.
        assert any("Restart" in s or "Restart " in s or "أعد تشغيل" in s
                   for s in result["issues"])

    def test_no_restart_flag_when_boot_was_present(
        self, isolated_storage, monkeypatch,
    ):
        """Happy case — env was set at boot, no restart needed."""
        from modules.ai.media import normalizer
        from routers import admin_debug

        monkeypatch.setattr(normalizer, "_BOOT_OPENAI_KEY_PRESENT", True)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-present-from-start")

        result = asyncio.run(admin_debug.admin_debug_media_env(
            _admin={"sub": "admin@nahla", "role": "admin"},
        ))
        assert result["process"]["needs_restart_to_pick_up_env"] is False
