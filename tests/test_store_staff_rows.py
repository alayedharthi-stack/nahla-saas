"""Rows a person at the store typed, recognised from the platform's own record (#1170).

The classifier never reads what a message says to decide who sent it; it reads
the markers the three writers stamp. These tests drive the real writers
(``_ingest_smb_message_echoes``, ``_ingest_coexistence_history``) and feed what
they store to ``staff_row``, so a change to the stored shape fails here.
Generic store data only.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest

from core import store_staff_rows as ssr

CUSTOMER = "966500000001"


def _row(event_type: str, metadata: Dict[str, Any], body: str) -> Any:
    return ssr.staff_row(event_type, metadata, body)


# ── Classification from recorded markers ─────────────────────────────────────


def test_a_business_app_text_echo_is_the_staff_s_words():
    row = _row("smb_message_echo", {"source": "merchant_mobile_app", "echo_type": "text"},
               "حذاء رياضي أبيض متوفر مقاس 42")
    assert row == ssr.StaffRow("whatsapp_business_app", "text", "حذاء رياضي أبيض متوفر مقاس 42")


def test_an_inbox_reply_is_the_staff_s_words():
    assert _row("manual_reply", {"is_ai": False}, "قميص قطني أزرق وصل") == ssr.StaffRow(
        "nahla_inbox", "text", "قميص قطني أزرق وصل")


@pytest.mark.parametrize("event_type, metadata", [
    ("ai_reply", {"is_ai": True}),
    ("campaign", {"is_ai": False}),           # platform-sent, not typed at the store
    ("automation", {"is_ai": False}),
    ("", {}),
    (None, None),
])
def test_rows_no_writer_marked_as_staff_are_not_the_staff_s(event_type, metadata):
    assert _row(event_type, metadata, "نص") is None


def test_imported_history_placeholders_are_recognised_only_in_the_importer_s_format():
    assert ssr.history_placeholder_kind("[image]") == "image"
    assert ssr.history_placeholder_kind("[unsupported_type]") == "unsupported_type"
    for words in ("[تم]", "[OK]", "[]", "image", "[image] شوفه", ""):
        assert ssr.history_placeholder_kind(words) is None


# ── The real writers, read back ──────────────────────────────────────────────


class _Db:
    def __init__(self) -> None:
        self.added: List[Any] = []

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    def flush(self) -> None:
        return None


@pytest.fixture
def echo_ingest(monkeypatch):
    monkeypatch.setattr("routers.conversations._get_or_create_conversation",
                        lambda *a, **k: SimpleNamespace(id=7, status="active"))
    download = AsyncMock()
    save = MagicMock()
    monkeypatch.setattr("modules.ai.media.normalizer._download_meta_media", download)
    monkeypatch.setattr("services.inbound_media_storage.save_inbound_media", save)

    def run(echo: Dict[str, Any]) -> Any:
        from routers.whatsapp_webhook import _ingest_smb_message_echoes

        db = _Db()
        asyncio.run(_ingest_smb_message_echoes(
            db, SimpleNamespace(id=1, tenant_id=5),
            {"metadata": {"phone_number_id": "PID"}, "message_echoes": [{"to": CUSTOMER, **echo}]}))
        (event,) = [m for m in db.added if hasattr(m, "event_type")]
        return ssr.staff_row(event.event_type, event.extra_metadata, event.body)

    return SimpleNamespace(run=run, download=download, save=save)


def test_a_stored_media_echo_keeps_its_caption_and_kind(echo_ingest):
    echo_ingest.download.return_value = {"bytes": b"x", "mime_type": "image/jpeg"}
    echo_ingest.save.return_value = SimpleNamespace(storage_url="/m/1.jpg", storage_sha256="s")
    row = echo_ingest.run({"id": "w1", "type": "image",
                           "image": {"id": "M1", "mime_type": "image/jpeg", "caption": "المقاسات المتوفرة"}})
    assert row == ssr.StaffRow("whatsapp_business_app", "image", "المقاسات المتوفرة")


def test_a_media_echo_that_could_not_be_stored_still_keeps_its_caption(echo_ingest):
    """Review finding F2: the caption is the body when the download fails."""
    echo_ingest.download.return_value = None
    row = echo_ingest.run({"id": "w2", "type": "document",
                           "document": {"id": "M2", "mime_type": "application/pdf",
                                        "caption": "فاتورة الطلب"}})
    assert row == ssr.StaffRow("whatsapp_business_app", "document", "فاتورة الطلب")


def test_a_media_echo_without_a_caption_that_could_not_be_stored_carries_no_words(echo_ingest):
    echo_ingest.download.return_value = None
    row = echo_ingest.run({"id": "w3", "type": "document",
                           "document": {"id": "M3", "mime_type": "application/pdf"}})
    assert row == ssr.StaffRow("whatsapp_business_app", "document", "")


def test_an_unsupported_echo_carries_no_words(echo_ingest):
    row = echo_ingest.run({"id": "w4", "type": "sticker", "sticker": {"id": "S"}})
    assert row == ssr.StaffRow("whatsapp_business_app", "sticker", "")


def _ingest_history(monkeypatch, messages: List[Dict[str, Any]]) -> List[Any]:
    from routers.whatsapp_webhook import _ingest_coexistence_history

    class _Query:
        def filter(self, *a, **k):
            return self

        order_by = limit = filter

        def all(self):
            return []

    added: List[Any] = []
    monkeypatch.setattr("routers.conversations._get_or_create_conversation",
                        lambda *a, **k: SimpleNamespace(id=9))
    db = SimpleNamespace(add=added.append, query=lambda *a, **k: _Query())
    conn = SimpleNamespace(tenant_id=5, phone_number_id="PID",
                           extra_metadata={"connection_mode": "coexistence"})
    _ingest_coexistence_history(db, conn, {"history": [{"threads": [
        {"id": CUSTOMER, "messages": messages}]}]})
    return added


def test_imported_history_from_the_store_is_the_staff_s_and_from_the_customer_is_not(monkeypatch):
    added = _ingest_history(monkeypatch, [
        {"id": "h1", "from": CUSTOMER, "type": "text", "text": {"body": "عندكم عطر ورد؟"}},
        {"id": "h2", "from": "966511111111", "type": "text", "text": {"body": "إيه متوفر"}},
        {"id": "h3", "from": "966511111111", "type": "image", "image": {"id": "I"}},
        {"id": "h4", "from": "966511111111", "type": "image", "image": {"id": "I", "caption": "عطر ورد 100ml"}},
    ])
    rows = [(e.direction, ssr.staff_row(e.event_type, e.extra_metadata, e.body)) for e in added]
    assert rows[0][0] == "inbound"
    assert rows[1:] == [
        ("outbound", ssr.StaffRow("whatsapp_business_app", "text", "إيه متوفر", imported=True)),
        ("outbound", ssr.StaffRow("whatsapp_business_app", "image", "", imported=True)),
        ("outbound", ssr.StaffRow("whatsapp_business_app", "image", "عطر ورد 100ml", imported=True)),
    ]


def test_history_imported_before_the_type_was_recorded_is_read_from_its_placeholder():
    legacy = {"source": "coexistence_history", "message_id": "old"}
    assert ssr.staff_row("coexistence_history", legacy, "[document]") == ssr.StaffRow(
        "whatsapp_business_app", "document", "", imported=True)
    assert ssr.staff_row("coexistence_history", legacy, "تمام") == ssr.StaffRow(
        "whatsapp_business_app", "text", "تمام", imported=True)
