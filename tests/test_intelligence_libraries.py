"""Tests for the manual-coupons + AI-media-library helpers.

These are pure-Python contract tests for the deterministic logic that
sits between the CRUD endpoints and the merchant brain. The CRUD
endpoints themselves are exercised by the existing pytest fixtures
that spin up Postgres; here we only cover the bits that don't need a
live database:

* ``_is_currently_active`` window check (start/expiry math).
* ``extract_media_markers`` token parsing (single / dup / multi / mixed
  / capped / cleaned text), with a stubbed DB session that hands back
  ``AIMediaItem`` rows so we don't need Alembic to have run.
* WhatsApp ``_send_media_message`` outer-type mapping (pdf → document,
  audio gets no caption, document carries filename, etc.) — wraps the
  HTTP helper in an :class:`AsyncMock` so we can assert the payload.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for p in [str(REPO_ROOT), str(BACKEND_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)


# ─────────────────────────────────────────────────────────────────────────
# _is_currently_active
# ─────────────────────────────────────────────────────────────────────────

def test_is_currently_active_inactive_flag_dominates():
    from core.ai_libraries import _is_currently_active

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert _is_currently_active(False, None, None, now=now) is False
    assert _is_currently_active(False, now - timedelta(days=1), now + timedelta(days=1), now=now) is False


def test_is_currently_active_starts_at_in_the_future_blocks():
    from core.ai_libraries import _is_currently_active

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert _is_currently_active(True, now + timedelta(hours=1), None, now=now) is False
    assert _is_currently_active(True, now - timedelta(hours=1), None, now=now) is True


def test_is_currently_active_expired_blocks():
    from core.ai_libraries import _is_currently_active

    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    assert _is_currently_active(True, None, now - timedelta(seconds=1), now=now) is False
    assert _is_currently_active(True, None, now + timedelta(seconds=1), now=now) is True


def test_is_currently_active_no_window_means_always():
    from core.ai_libraries import _is_currently_active

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert _is_currently_active(True, None, None, now=now) is True


# ─────────────────────────────────────────────────────────────────────────
# extract_media_markers
# ─────────────────────────────────────────────────────────────────────────

def _stub_media_row(*, id_: int, tenant_id: int = 1, active: bool = True,
                    media_type: str = "image", file_url: str = "https://cdn/x.png",
                    title: str = "x", mime: str | None = "image/png"):
    return SimpleNamespace(
        id=id_, tenant_id=tenant_id, is_active=active,
        media_type=media_type, file_url=file_url, title=title,
        mime_type=mime, storage_kind="local",
    )


def _make_db_session(rows: list):
    """Return a MagicMock that emulates the SQLAlchemy ``in_(seen_ids)``
    chain used by ``extract_media_markers``.
    """
    db = MagicMock()
    query = MagicMock()
    db.query.return_value = query
    query.filter.return_value = query
    query.all.return_value = rows
    return db


def test_extract_media_markers_no_markers_passes_through():
    from core.ai_libraries import extract_media_markers

    db = _make_db_session([])
    cleaned, attachments = extract_media_markers(db, tenant_id=1, reply_text="مرحبا، كيف أساعدك؟")
    assert cleaned == "مرحبا، كيف أساعدك؟"
    assert attachments == []


def test_extract_media_markers_resolves_single_id():
    from core.ai_libraries import extract_media_markers

    db = _make_db_session([_stub_media_row(id_=42, media_type="image")])
    cleaned, attachments = extract_media_markers(
        db, tenant_id=1, reply_text="تفضل بيانات التحويل وهذه صورة الباركود [MEDIA:42]",
    )
    assert "[MEDIA:42]" not in cleaned
    assert "بيانات التحويل" in cleaned
    assert len(attachments) == 1
    assert attachments[0]["id"] == 42
    assert attachments[0]["media_type"] == "image"


def test_extract_media_markers_dedupes_repeated_id():
    from core.ai_libraries import extract_media_markers

    db = _make_db_session([_stub_media_row(id_=7)])
    cleaned, attachments = extract_media_markers(
        db, tenant_id=1, reply_text="انظر [MEDIA:7] ثم انظر [MEDIA:7] مرة أخرى",
    )
    assert cleaned.count("[MEDIA:") == 0
    # The same id repeated should ship the file exactly once.
    assert [a["id"] for a in attachments] == [7]


def test_extract_media_markers_caps_at_max_attachments():
    from core.ai_libraries import extract_media_markers

    rows = [_stub_media_row(id_=i) for i in (1, 2, 3, 4)]
    db = _make_db_session(rows)
    cleaned, attachments = extract_media_markers(
        db, tenant_id=1,
        reply_text="[MEDIA:1] [MEDIA:2] [MEDIA:3] [MEDIA:4]",
        max_attachments=2,
    )
    assert "[MEDIA:" not in cleaned
    assert [a["id"] for a in attachments] == [1, 2]


def test_extract_media_markers_drops_disabled_or_missing():
    """The DB returns only id=10; id=99 is missing/disabled and must vanish."""
    from core.ai_libraries import extract_media_markers

    db = _make_db_session([_stub_media_row(id_=10)])
    cleaned, attachments = extract_media_markers(
        db, tenant_id=1, reply_text="نص [MEDIA:10] [MEDIA:99]",
    )
    assert "[MEDIA:10]" not in cleaned
    assert "[MEDIA:99]" not in cleaned
    assert [a["id"] for a in attachments] == [10]


def test_extract_media_markers_preserves_llm_order():
    from core.ai_libraries import extract_media_markers

    rows = [_stub_media_row(id_=2), _stub_media_row(id_=5)]
    db = _make_db_session(rows)
    _, attachments = extract_media_markers(
        db, tenant_id=1, reply_text="[MEDIA:5] ثم [MEDIA:2]", max_attachments=5,
    )
    # Even though the DB returned 2 before 5, the output must follow the
    # order in which the LLM cited them.
    assert [a["id"] for a in attachments] == [5, 2]


def test_extract_media_markers_accepts_title_hint():
    from core.ai_libraries import extract_media_markers

    db = _make_db_session([_stub_media_row(id_=11)])
    cleaned, attachments = extract_media_markers(
        db, tenant_id=1,
        reply_text="هنا الصورة [MEDIA:11|product photo]",
    )
    assert "[MEDIA:11" not in cleaned
    assert [a["id"] for a in attachments] == [11]


def test_extract_media_markers_collapses_blank_lines_left_behind():
    from core.ai_libraries import extract_media_markers

    db = _make_db_session([_stub_media_row(id_=3)])
    cleaned, _ = extract_media_markers(
        db, tenant_id=1,
        reply_text="السطر الأول\n\n[MEDIA:3]\n\nالسطر الأخير",
    )
    # Marker disappears AND the resulting double-blank doesn't grow into a triple.
    assert "[MEDIA:" not in cleaned
    assert "\n\n\n" not in cleaned
    assert cleaned.startswith("السطر الأول")
    assert cleaned.endswith("السطر الأخير")


# ─────────────────────────────────────────────────────────────────────────
# _send_media_message — payload shape
# ─────────────────────────────────────────────────────────────────────────

def _run(coro):
    return asyncio.run(coro)


def test_send_media_image_payload_shape():
    from routers import whatsapp_webhook as wh

    captured = {}

    async def fake_post(phone_id, payload, **kwargs):
        captured["phone_id"] = phone_id
        captured["payload"] = payload
        return True

    with patch.object(wh, "_post_wa", new=AsyncMock(side_effect=fake_post)):
        ok = _run(wh._send_media_message(
            phone_id="PID", to="+966500000000",
            media_type="image", media_url="https://cdn/x.png",
            caption="هذه صورة",
        ))
    assert ok is True
    p = captured["payload"]
    assert p["type"] == "image"
    assert p["image"]["link"] == "https://cdn/x.png"
    assert p["image"]["caption"] == "هذه صورة"
    assert "filename" not in p["image"]


def test_send_media_pdf_routes_through_document_with_filename():
    from routers import whatsapp_webhook as wh

    captured = {}

    async def fake_post(phone_id, payload, **kwargs):
        captured["payload"] = payload
        return True

    with patch.object(wh, "_post_wa", new=AsyncMock(side_effect=fake_post)):
        _run(wh._send_media_message(
            phone_id="PID", to="+966500000000",
            media_type="pdf", media_url="https://cdn/policy.pdf",
            caption="سياسة الشحن", filename="policy.pdf",
        ))
    p = captured["payload"]
    assert p["type"] == "document"
    assert p["document"]["link"] == "https://cdn/policy.pdf"
    assert p["document"]["caption"] == "سياسة الشحن"
    assert p["document"]["filename"] == "policy.pdf"


def test_send_media_audio_strips_caption_and_filename():
    from routers import whatsapp_webhook as wh

    captured = {}

    async def fake_post(phone_id, payload, **kwargs):
        captured["payload"] = payload
        return True

    with patch.object(wh, "_post_wa", new=AsyncMock(side_effect=fake_post)):
        _run(wh._send_media_message(
            phone_id="PID", to="+966500000000",
            media_type="audio", media_url="https://cdn/voice.ogg",
            caption="should be ignored", filename="ignored.ogg",
        ))
    p = captured["payload"]
    assert p["type"] == "audio"
    assert p["audio"]["link"] == "https://cdn/voice.ogg"
    assert "caption" not in p["audio"]
    assert "filename" not in p["audio"]


def test_send_media_unknown_type_returns_false_without_post():
    from routers import whatsapp_webhook as wh

    with patch.object(wh, "_post_wa", new=AsyncMock(return_value=True)) as mock_post:
        ok = _run(wh._send_media_message(
            phone_id="PID", to="+966500000000",
            media_type="hologram", media_url="https://cdn/x",
        ))
    assert ok is False
    assert mock_post.await_count == 0


def test_send_media_caption_trimmed_to_1024_chars():
    from routers import whatsapp_webhook as wh

    captured = {}

    async def fake_post(phone_id, payload, **kwargs):
        captured["payload"] = payload
        return True

    long_caption = "x" * 5000
    with patch.object(wh, "_post_wa", new=AsyncMock(side_effect=fake_post)):
        _run(wh._send_media_message(
            phone_id="PID", to="+966500000000",
            media_type="video", media_url="https://cdn/v.mp4",
            caption=long_caption,
        ))
    assert len(captured["payload"]["video"]["caption"]) == 1024
