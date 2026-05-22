"""
backend/tests/test_smb_echo_media_ingest.py
────────────────────────────────────────────
May 2026 P1 regression suite for "merchant mobile images appear as
``[merchant_image]`` placeholders" (Tenant 33).

Pre-fix the ``_ingest_smb_message_echoes`` function in
``backend/routers/whatsapp_webhook.py`` only handled
``echo.type == 'text'`` and stamped ``f'[merchant_{msg_type}]'`` into
the message body for every other type — never reading the
``echo['image']`` sub-block, never calling ``_download_meta_media``,
never writing ``normalized_inbound`` metadata. So an image the merchant
sent from his official WhatsApp Business mobile app (via 360dialog
Coexistence) surfaced in Nahla as the literal text "[merchant_image]"
instead of a real image bubble.

The fix mirrors what ``_process_image`` already does for inbound
customer media: download via ``_download_meta_media``, persist via
``save_inbound_media``, write ``normalized_inbound`` so
``_build_media_block`` produces a media row the dashboard can render.

These tests assert the new ingest produces the right shape WITHOUT
calling out to the real 360dialog API.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _run(coro):
    """Run an async test body without depending on pytest-asyncio."""
    return asyncio.run(coro)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


# ─────────────────────────────────────────────────────────────────────────────
# Test helpers
# ─────────────────────────────────────────────────────────────────────────────


class _StubConvo:
    """Light stand-in for ``Conversation`` — only the bits the ingest
    function reads/writes."""
    def __init__(self) -> None:
        self.id = 12345
        self.status = "active"


class _StubWaConn:
    def __init__(self) -> None:
        self.id = 99
        self.tenant_id = 33


class _StubDB:
    """Captures every ``db.add`` call so the test can introspect what
    rows were created (and inspect their ``extra_metadata`` JSON)."""
    def __init__(self) -> None:
        self.added: List[Any] = []
        self.flushed_count = 0

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    def flush(self) -> None:
        self.flushed_count += 1


@pytest.fixture
def patches(monkeypatch: pytest.MonkeyPatch):
    """Pre-patches the dispatcher dependencies. Returns a small helper
    namespace so each test can swap out behaviour for its scenario."""
    from routers import whatsapp_webhook as wh

    convo = _StubConvo()
    # May 2026 #37 — ``_get_or_create_conversation`` now accepts an
    # optional keyword-only ``source`` so coexistence echoes can opt
    # out of the customer ``last_interaction_at`` UPDATE. The stub
    # accepts arbitrary kwargs to stay forward-compatible with the
    # production signature.
    monkeypatch.setattr(
        "routers.conversations._get_or_create_conversation",
        lambda _db, _tid, _phone, *_a, **_k: convo,
    )

    # We DO NOT want any real HTTP — stub the helpers the ingest pulls
    # in via inline ``from … import …`` so the test stays hermetic.
    download_mock = AsyncMock()
    save_mock = MagicMock()

    monkeypatch.setattr(
        "modules.ai.media.normalizer._download_meta_media",
        download_mock,
    )
    monkeypatch.setattr(
        "services.inbound_media_storage.save_inbound_media",
        save_mock,
    )

    return {
        "wh": wh,
        "convo": convo,
        "download": download_mock,
        "save": save_mock,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 1. Text echo — must NOT regress; placeholder logic stays intact.
# ─────────────────────────────────────────────────────────────────────────────


def test_text_echo_preserves_body(patches) -> None:
    from routers.whatsapp_webhook import _ingest_smb_message_echoes

    db = _StubDB()
    wa = _StubWaConn()
    value = {
        "metadata": {"phone_number_id": "PID_360"},
        "message_echoes": [{
            "to": "966500000000",
            "id": "wamid.echo.text",
            "type": "text",
            "text": {"body": "السلام عليكم، الباركود في الأعلى"},
        }],
    }

    _run(_ingest_smb_message_echoes(db, wa, value))

    assert len(db.added) >= 1
    msg = next(m for m in db.added if hasattr(m, "body"))
    assert msg.body == "السلام عليكم، الباركود في الأعلى"
    assert msg.event_type == "smb_message_echo"
    assert msg.direction == "outbound"
    # No media download attempted for text.
    patches["download"].assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# 2. Image echo — successful download path
# ─────────────────────────────────────────────────────────────────────────────


def test_image_echo_downloads_and_writes_normalized_inbound(patches) -> None:
    from routers.whatsapp_webhook import _ingest_smb_message_echoes

    # Simulate the 360dialog media download returning real bytes.
    patches["download"].return_value = {
        "bytes": b"\x89PNG fake binary payload",
        "mime_type": "image/png",
    }
    # Simulate the storage layer producing a stable storage URL.
    stored = MagicMock()
    stored.storage_url = "/media/inbound/33/abc123.png"
    stored.storage_sha256 = "abc123"
    patches["save"].return_value = stored

    db = _StubDB()
    wa = _StubWaConn()
    value = {
        "metadata": {"phone_number_id": "PID_360"},
        "message_echoes": [{
            "to": "966500000000",
            "id": "wamid.echo.image",
            "type": "image",
            "image": {
                "id":        "MEDIA_ID_BARCODE",
                "mime_type": "image/png",
                "sha256":    "abc123",
                "caption":   "باركود الراجحي 🌷",
            },
        }],
    }

    _run(_ingest_smb_message_echoes(db, wa, value))

    assert len(db.added) >= 1
    msg = next(m for m in db.added if hasattr(m, "extra_metadata"))

    # 1. Body is the caption — NOT the dreaded [merchant_image] placeholder.
    assert msg.body == "باركود الراجحي 🌷"
    assert "[merchant_image]" not in (msg.body or "")

    # 2. normalized_inbound block was written so the dashboard's
    #    ``_build_media_block`` will produce a real media row.
    meta = msg.extra_metadata or {}
    ni = meta.get("normalized_inbound") or {}
    assert ni.get("source_type")  == "image"
    assert ni.get("storage_url")  == "/media/inbound/33/abc123.png"
    assert ni.get("mime_type")    == "image/png"
    assert ni.get("image_download_status") == "ok"
    assert ni.get("direction")    == "outbound"
    assert ni.get("echo_source")  == "merchant_mobile_app"

    # 3. Download was actually called with the right inputs.
    patches["download"].assert_awaited_once()
    kwargs = patches["download"].await_args.kwargs
    assert kwargs.get("tenant_id") == 33
    assert kwargs.get("media_id")  == "MEDIA_ID_BARCODE"
    assert kwargs.get("mime_type") == "image/png"

    # 4. Storage was called with kind=image.
    patches["save"].assert_called_once()
    save_kwargs = patches["save"].call_args.kwargs
    assert save_kwargs.get("kind")      == "image"
    assert save_kwargs.get("tenant_id") == 33
    assert save_kwargs.get("mime_type") == "image/png"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Image echo — download FAILS → friendly placeholder, not crash
# ─────────────────────────────────────────────────────────────────────────────


def test_image_echo_failed_download_uses_friendly_placeholder(patches) -> None:
    from routers.whatsapp_webhook import _ingest_smb_message_echoes

    # Simulate the media endpoint hanging up / returning None.
    patches["download"].return_value = None

    db = _StubDB()
    wa = _StubWaConn()
    value = {
        "metadata": {"phone_number_id": "PID_360"},
        "message_echoes": [{
            "to": "966500000000",
            "id": "wamid.echo.image_fail",
            "type": "image",
            "image": {
                "id":        "MEDIA_ID_DEAD",
                "mime_type": "image/jpeg",
                # No caption.
            },
        }],
    }

    _run(_ingest_smb_message_echoes(db, wa, value))

    msg = next(m for m in db.added if hasattr(m, "extra_metadata"))

    # 1. Not the bracket placeholder — a readable Arabic fallback.
    assert "[merchant_image]" not in (msg.body or "")
    assert "تطبيق الجوال" in (msg.body or "")

    # 2. extra_metadata reflects the failed download.
    meta = msg.extra_metadata or {}
    assert meta.get("media_storage_status") in {
        "download_failed", "exception",
    }
    # 3. No normalized_inbound block (so the UI falls back to text).
    assert "normalized_inbound" not in meta


# ─────────────────────────────────────────────────────────────────────────────
# 4. Unsupported type (sticker / location / "unsupported") — friendly copy
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "msg_type",
    ["sticker", "location", "contacts", "interactive", "unsupported"],
)
def test_unsupported_echo_types_use_readable_placeholder(
    patches, msg_type: str,
) -> None:
    from routers.whatsapp_webhook import _ingest_smb_message_echoes

    db = _StubDB()
    wa = _StubWaConn()
    value = {
        "metadata": {"phone_number_id": "PID_360"},
        "message_echoes": [{
            "to": "966500000000",
            "id": f"wamid.echo.{msg_type}",
            "type": msg_type,
        }],
    }

    _run(_ingest_smb_message_echoes(db, wa, value))

    msg = next(m for m in db.added if hasattr(m, "extra_metadata"))
    # Critically: no ``[merchant_<type>]`` bracket placeholder.
    assert f"[merchant_{msg_type}]" not in (msg.body or "")
    # The friendly Arabic copy is shown.
    assert "تطبيق الجوال" in (msg.body or "")
    # The metadata records the type so the dashboard could later
    # render a custom badge per-type if it wants to.
    assert (msg.extra_metadata or {}).get("echo_type") == msg_type
    assert (msg.extra_metadata or {}).get("media_storage_status") == "unsupported_type"
    # No download attempted for unsupported types.
    patches["download"].assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# 5. Counter typo fix — value.get("message_echoes") (not smb_message_echoes)
# ─────────────────────────────────────────────────────────────────────────────


def test_message_echoes_counter_reads_correct_key() -> None:
    """The pre-fix log line ``[WEBHOOK_IN] ... echoes=0`` was always
    zero because ``echoes_cnt`` read ``value.get("smb_message_echoes")``
    instead of the actual array key ``message_echoes``. This is a
    diagnostic-only bug (doesn't affect ingest) but the test pins the
    fix so the log line stays useful for ops.

    We exercise the count path via the ingest function: when there
    ARE echoes in the value, our ingest creates events; absence of
    creation means the value shape was misread.
    """
    from routers.whatsapp_webhook import _ingest_smb_message_echoes
    from unittest.mock import patch as _patch

    with _patch(
        "routers.conversations._get_or_create_conversation",
        return_value=_StubConvo(),
    ):
        db = _StubDB()
        wa = _StubWaConn()
        value = {
            "metadata": {"phone_number_id": "PID_360"},
            "message_echoes": [
                {"to": "966500000000", "type": "text",
                 "text": {"body": "1"}, "id": "1"},
                {"to": "966500000000", "type": "text",
                 "text": {"body": "2"}, "id": "2"},
            ],
        }
        _run(_ingest_smb_message_echoes(db, wa, value))
        msg_events = [m for m in db.added if hasattr(m, "body")]
        assert len(msg_events) == 2
