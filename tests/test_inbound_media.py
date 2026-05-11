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
                mime_type="application/pdf", kind="document",
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
        ), patch.object(
            normalizer, "OPENAI_API_KEY", "sk-test",
        ):
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
        ), patch.object(
            normalizer, "OPENAI_API_KEY", "sk-test",
        ):
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
        ), patch.object(
            normalizer, "OPENAI_API_KEY", "sk-test",
        ):
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
             patch.object(normalizer, "OPENAI_API_KEY", "sk-test"):
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

    def test_stt_not_configured_still_persists_for_playback(self, isolated_storage):
        """When OPENAI_API_KEY is missing we still want to download
        and persist so the merchant can replay the recording from
        the dashboard. Transcript status surfaces the reason."""
        from modules.ai.media import normalizer

        with patch.object(normalizer, "_download_meta_media", new=AsyncMock(return_value={
            "bytes": b"ogg", "mime_type": "audio/ogg",
        })), patch.object(normalizer, "OPENAI_API_KEY", ""):
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
        ), patch.object(
            normalizer, "OPENAI_API_KEY", "sk-test",
        ):
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
        )), patch.object(normalizer, "OPENAI_API_KEY", "sk-test"):
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
        )), patch.object(normalizer, "OPENAI_API_KEY", "sk-test"):
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
        assert '{"text", "audio", "image"}' in src, (
            "webhook still gates out image — vision pipeline cannot fire"
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
