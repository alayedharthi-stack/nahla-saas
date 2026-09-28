"""Which stored outbound rows a person at the store typed, from the platform's own record.

A conversation row sent from the store's number is not necessarily something
the assistant said. Three platform writers record rows that a person at the
store typed, and each marks them when it stores them:

* the WhatsApp Business app echo on a shared number
  (``event_type=smb_message_echo``, ``metadata.source=merchant_mobile_app``,
  ``metadata.echo_type``) — ``routers.whatsapp_webhook._ingest_smb_message_echoes``;
* the Business app history imported when a shared number is connected
  (``event_type=coexistence_history``, outbound) —
  ``routers.whatsapp_webhook._ingest_coexistence_history``;
* the Nahla inbox reply (``event_type=manual_reply``) —
  ``routers.conversations.reply_to_conversation``.

Nothing here reads what a message says to decide who sent it. Where a writer
had no words to store for a media message it wrote a display placeholder into
the body; the placeholder formats are defined here, used by those writers, and
recognised here, so the placeholder is never passed on as the staff member's
words.
"""
from __future__ import annotations

import dataclasses
from typing import Any, Optional

STAFF_ROLE = "store_staff"

CHANNEL_BUSINESS_APP = "whatsapp_business_app"
CHANNEL_NAHLA_INBOX = "nahla_inbox"

SMB_ECHO_EVENT = "smb_message_echo"
SMB_ECHO_SOURCE = "merchant_mobile_app"
HISTORY_EVENT = "coexistence_history"
MANUAL_REPLY_EVENT = "manual_reply"

TEXT_KIND = "text"

# What the dashboard shows for a Business app echo of a type it cannot render.
SMB_ECHO_UNSUPPORTED_DISPLAY = "📎 رسالة من تطبيق الجوال — صيغة غير مدعومة"


def smb_echo_media_placeholder(msg_type: str) -> str:
    """The body stored for a Business app media echo that has no caption and
    whose media could not be stored."""
    return f"📎 رسالة {msg_type} من تطبيق الجوال"


def history_media_placeholder(msg_type: str) -> str:
    """The body stored for an imported history media message without a caption."""
    return f"[{msg_type}]"


def history_placeholder_kind(body: Any) -> Optional[str]:
    """The media type an imported history placeholder names, or ``None``.

    Rows imported before the type was recorded in metadata carry it only in
    the placeholder the importer wrote (``history_media_placeholder``).
    """
    text = str(body or "").strip()
    if len(text) < 3 or text[0] != "[" or text[-1] != "]":
        return None
    inner = text[1:-1]
    if inner.isascii() and inner.islower() and inner.replace("_", "").isalpha():
        return inner
    return None


@dataclasses.dataclass(frozen=True)
class StaffRow:
    channel: str
    kind: str
    text: str                  # the staff member's words; "" when the row carries none
    imported: bool = False     # from the history imported when the number was connected


def staff_row(event_type: Any, metadata: Any, body: Any) -> Optional[StaffRow]:
    """The staff record of one outbound row, or ``None`` when no writer marked it.

    ``body`` is the row's text as the caller established it (for a sent reply,
    what the wire audit recorded).
    """
    meta = metadata if isinstance(metadata, dict) else {}
    event = str(event_type or "").strip().lower()
    source = str(meta.get("source") or "").strip().lower()
    words = str(body or "").strip()

    if event == SMB_ECHO_EVENT or source == SMB_ECHO_SOURCE:
        kind = str(meta.get("echo_type") or TEXT_KIND).strip().lower() or TEXT_KIND
        if kind != TEXT_KIND:
            normalized = meta.get("normalized_inbound")
            if isinstance(normalized, dict):
                words = str(normalized.get("caption") or "").strip()
            elif words in {smb_echo_media_placeholder(kind), SMB_ECHO_UNSUPPORTED_DISPLAY}:
                words = ""
        return StaffRow(CHANNEL_BUSINESS_APP, kind, words)

    if event == HISTORY_EVENT or source == HISTORY_EVENT:
        recorded = str(meta.get("message_type") or "").strip().lower()
        kind = recorded or history_placeholder_kind(words) or TEXT_KIND
        if kind != TEXT_KIND and words == history_media_placeholder(kind):
            words = ""
        return StaffRow(CHANNEL_BUSINESS_APP, kind, words, imported=True)

    if event == MANUAL_REPLY_EVENT:
        return StaffRow(CHANNEL_NAHLA_INBOX, TEXT_KIND, words)

    return None
