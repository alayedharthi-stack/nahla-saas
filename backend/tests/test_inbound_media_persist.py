"""
backend/tests/test_inbound_media_persist.py
───────────────────────────────────────────
Regression suite for the May 2026 #41 silent-drop fix in
``routers/whatsapp_webhook.py``.

Production bug we are pinning down:
  Customer "علي كامل" sent a video AND an image around 1:05 PM. Both
  were visible in WhatsApp itself but neither created a Nahla
  conversation row. Other text messages from the same window arrived
  normally, so this was a media-specific silent drop, not a routing
  failure.

Root cause (audit summary):
  Two webhook paths could end with ``return`` and NO MessageEvent /
  conversation insert when:
    * ``normalized_type`` fell outside ``{text, audio, image, document,
       video}`` — sticker, reaction, location, contacts, …
    * ``normalized_inbound.text`` came back empty AND there was no
      ``fallback_reply_ar`` available (e.g. forwarded image/video the
      vision model couldn't describe).
  The pre-fix code wrote an ``ai_quality_events`` row in both cases
  (so the AI Quality dashboard showed it) but never touched
  ``message_events`` / ``conversations`` — the merchant inbox stayed
  silent. The fix routes both paths through the new
  :func:`_persist_inbound_only` helper which creates the conversation
  and writes a placeholder inbound row, so the merchant's inbox
  always reflects the customer's actual send history.

These tests pin the helper in isolation: full webhook integration is
covered by the existing ``test_whatsapp_inbound_*`` suites which
already exercise the dispatch path end-to-end.

Run:
    cd backend
    python -m pytest tests/test_inbound_media_persist.py -v
"""
from __future__ import annotations

import logging
import os
import sys
import types
from typing import Any, Dict, List, Optional

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)


# ── Stubs ────────────────────────────────────────────────────────────────────


class _StubConversation:
    def __init__(self, convo_id: int = 7) -> None:
        self.id = convo_id


class _StubDB:
    """Minimal DB-like object — the helper only forwards to
    ``_get_or_create_conversation`` and ``StateManager.save_message``,
    both of which we stub directly."""


class _SaveMessageRecorder:
    """Captures every ``StateManager.save_message`` call so tests can
    assert the inbound row was written with the correct metadata."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.next_exc: Optional[Exception] = None

    def __call__(
        self,
        db: Any,
        phone: str,
        body: str,
        direction: str,
        *,
        conversation_id: Optional[int] = None,
        tenant_id: Optional[int] = None,
        extra_metadata: Optional[Dict[str, Any]] = None,
        **_kwargs: Any,
    ) -> None:
        if self.next_exc is not None:
            raise self.next_exc
        self.calls.append({
            "phone":           phone,
            "body":            body,
            "direction":       direction,
            "conversation_id": conversation_id,
            "tenant_id":       tenant_id,
            "extra_metadata":  dict(extra_metadata or {}),
        })


@pytest.fixture
def patched_helpers(monkeypatch: pytest.MonkeyPatch):
    """Install conversation + save_message stubs and return both
    recorders so tests can introspect what the helper did."""
    # Build a minimal `routers.conversations` shim with the helper
    # the production code imports inside its try/except.
    convo_module = types.ModuleType("routers.conversations")
    convo_create_calls: List[Dict[str, Any]] = []

    def _fake_get_or_create_conversation(db, tenant_id, phone, *args, **kwargs):
        convo_create_calls.append({
            "tenant_id": tenant_id, "phone": phone,
        })
        return _StubConversation(convo_id=42)

    convo_module._get_or_create_conversation = _fake_get_or_create_conversation
    monkeypatch.setitem(sys.modules, "routers.conversations", convo_module)

    # Patch StateManager.save_message used inside the helper.
    from core import conversation_engine as _ce  # noqa: PLC0415
    recorder = _SaveMessageRecorder()
    monkeypatch.setattr(_ce.StateManager, "save_message", recorder)

    return {
        "save_recorder":      recorder,
        "convo_create_calls": convo_create_calls,
    }


# ── Helper unit tests ────────────────────────────────────────────────────────


def test_persist_inbound_only_happy_path_creates_convo_and_logs(
    patched_helpers, caplog: pytest.LogCaptureFixture,
) -> None:
    """Happy path: helper looks up / creates the conversation, writes
    one inbound MessageEvent with the structured metadata, and emits
    a single ``[INBOUND_MEDIA_STORE]`` line."""
    from routers.whatsapp_webhook import _persist_inbound_only

    db = _StubDB()
    metadata = {
        "source_type":      "image",
        "media_id":         "abc123",
        "mime_type":        "image/jpeg",
        "caption":          "صورة المنتج",
        "transcript_status": None,
    }

    caplog.set_level(logging.INFO)
    ok = _persist_inbound_only(
        db=db,
        tenant_id=33,
        sender="966500000000",
        msg_type="image",
        normalized_type="image",
        inbound_metadata=metadata,
        wa_msg_id="wamid.HBg=",
        drop_reason="empty_text_no_fallback",
    )

    assert ok is True
    assert len(patched_helpers["convo_create_calls"]) == 1
    assert patched_helpers["convo_create_calls"][0]["tenant_id"] == 33

    calls = patched_helpers["save_recorder"].calls
    assert len(calls) == 1, f"expected exactly one inbound save, got {calls!r}"
    saved = calls[0]
    assert saved["direction"] == "inbound"
    assert saved["tenant_id"] == 33
    assert saved["conversation_id"] == 42
    assert saved["phone"] == "966500000000"

    meta = saved["extra_metadata"]
    assert meta["media_persist_only"] is True
    assert meta["drop_reason"] == "empty_text_no_fallback"
    assert meta["wa_message_id"] == "wamid.HBg="
    assert meta["normalized_inbound"]["media_id"] == "abc123"
    assert meta["normalized_inbound"]["mime_type"] == "image/jpeg"

    log_lines = [r.getMessage() for r in caplog.records]
    assert any(
        "[INBOUND_MEDIA_STORE]" in ln
        and "tenant_id=33" in ln
        and "msg_type=image" in ln
        and "normalized_type=image" in ln
        and "has_caption=True" in ln
        and "wa_msg_id=wamid.HBg=" in ln
        and "persisted=True" in ln
        for ln in log_lines
    ), (
        "expected a single structured [INBOUND_MEDIA_STORE] line on success; "
        f"got: {log_lines!r}"
    )


def test_persist_inbound_only_video_without_caption(
    patched_helpers, caplog: pytest.LogCaptureFixture,
) -> None:
    """Video without caption (the exact 'علي كامل' regression shape)
    must still produce a conversation + inbound row even though the
    customer did not type any caption."""
    from routers.whatsapp_webhook import _persist_inbound_only

    caplog.set_level(logging.INFO)
    ok = _persist_inbound_only(
        db=_StubDB(),
        tenant_id=33,
        sender="966500000001",
        msg_type="video",
        normalized_type="video",
        inbound_metadata={
            "source_type": "video",
            "media_id":    "vid-001",
            "mime_type":   "video/mp4",
            # caption deliberately absent
        },
        wa_msg_id="wamid.video.001",
        drop_reason="empty_text_no_fallback",
    )

    assert ok is True
    saved = patched_helpers["save_recorder"].calls[0]
    assert saved["extra_metadata"]["normalized_inbound"]["mime_type"] == "video/mp4"
    log_msg = "\n".join(r.getMessage() for r in caplog.records)
    assert "has_caption=False" in log_msg
    assert "msg_type=video" in log_msg


def test_persist_inbound_only_unsupported_type_uses_custom_placeholder(
    patched_helpers, caplog: pytest.LogCaptureFixture,
) -> None:
    """Sticker / reaction / location land here with a normalized_type
    outside the brain's allow-list. The placeholder body is opaque
    on purpose ("[رسالة وسائط: sticker]") so the merchant inbox
    shows what kind of message arrived even though the AI did not
    process it."""
    from routers.whatsapp_webhook import _persist_inbound_only

    caplog.set_level(logging.INFO)
    ok = _persist_inbound_only(
        db=_StubDB(),
        tenant_id=33,
        sender="966500000002",
        msg_type="sticker",
        normalized_type="sticker",
        inbound_metadata={
            "source_type": "sticker",
            "media_id":    "sticker-001",
        },
        wa_msg_id="wamid.sticker.001",
        drop_reason="unsupported_type:sticker",
        placeholder_body="[رسالة وسائط: sticker]",
    )

    assert ok is True
    saved = patched_helpers["save_recorder"].calls[0]
    assert saved["body"] == "[رسالة وسائط: sticker]"
    assert saved["extra_metadata"]["drop_reason"] == "unsupported_type:sticker"
    log_msg = "\n".join(r.getMessage() for r in caplog.records)
    assert "drop_reason=unsupported_type:sticker" in log_msg


def test_persist_inbound_only_returns_false_and_logs_on_save_failure(
    patched_helpers, caplog: pytest.LogCaptureFixture,
) -> None:
    """When ``StateManager.save_message`` raises (e.g. DB rollback in
    progress), the helper MUST NOT bubble — it must log a single
    ``[INBOUND_MEDIA_ERROR]`` line and return False so the webhook
    ack loop completes."""
    from routers.whatsapp_webhook import _persist_inbound_only

    patched_helpers["save_recorder"].next_exc = RuntimeError("db locked")

    caplog.set_level(logging.INFO)
    ok = _persist_inbound_only(
        db=_StubDB(),
        tenant_id=33,
        sender="966500000003",
        msg_type="image",
        normalized_type="image",
        inbound_metadata={"source_type": "image"},
        wa_msg_id="wamid.fail",
        drop_reason="empty_text_no_fallback",
    )

    assert ok is False
    log_lines = [r.getMessage() for r in caplog.records]
    assert any(
        "[INBOUND_MEDIA_ERROR]" in ln
        and "persisted=False" in ln
        and "tenant_id=33" in ln
        and "db locked" in ln
        for ln in log_lines
    ), (
        "expected [INBOUND_MEDIA_ERROR] with the underlying exception; "
        f"got: {log_lines!r}"
    )
    # No success line on failure.
    assert not any("[INBOUND_MEDIA_STORE]" in ln for ln in log_lines)


def test_persist_inbound_only_returns_false_on_convo_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """When ``_get_or_create_conversation`` itself raises, the helper
    must still degrade gracefully: log INBOUND_MEDIA_ERROR + return
    False. No save_message call is made because there's no convo id
    to attach the row to."""
    from routers.whatsapp_webhook import _persist_inbound_only

    convo_module = types.ModuleType("routers.conversations")

    def _explode(*_a, **_kw):
        raise RuntimeError("tenant lookup failed")

    convo_module._get_or_create_conversation = _explode
    monkeypatch.setitem(sys.modules, "routers.conversations", convo_module)

    caplog.set_level(logging.INFO)
    ok = _persist_inbound_only(
        db=_StubDB(),
        tenant_id=33,
        sender="966500000004",
        msg_type="image",
        normalized_type="image",
        inbound_metadata={"source_type": "image"},
        drop_reason="empty_text_no_fallback",
    )

    assert ok is False
    log_lines = [r.getMessage() for r in caplog.records]
    assert any(
        "[INBOUND_MEDIA_ERROR]" in ln
        and "persisted=False" in ln
        and "tenant lookup failed" in ln
        for ln in log_lines
    ), f"expected error log on convo failure; got: {log_lines!r}"


def test_persist_inbound_only_default_placeholder_used_when_unspecified(
    patched_helpers,
) -> None:
    """Callers who don't pass ``placeholder_body`` get the canonical
    Arabic copy. The shape stays consistent across the dispatcher's
    drop sites so dashboard search by body still groups them
    together."""
    from routers.whatsapp_webhook import _persist_inbound_only

    ok = _persist_inbound_only(
        db=_StubDB(),
        tenant_id=33,
        sender="966500000005",
        msg_type="image",
        normalized_type="image",
        inbound_metadata={"source_type": "image"},
    )

    assert ok is True
    saved = patched_helpers["save_recorder"].calls[0]
    assert saved["body"] == "[رسالة وسائط بدون نص قابل للقراءة]"
