"""
whatsapp_ai_live
────────────────
Cutoff timestamp for conversational AI on WhatsApp.

Any inbound whose WhatsApp message timestamp is strictly *before*
`WhatsAppConnection.whatsapp_ai_live_since` is stored for inbox/history
only — Brain / legacy conversational replies MUST NOT run.

The cutoff is stamped once (when NULL) at successful connection writes,
and advanced only via explicit merchant/admin reset.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

__all__ = [
    "parse_whatsapp_message_timestamp_utc",
    "to_utc_aware",
    "stamp_whatsapp_ai_live_since_if_empty",
    "is_inbound_before_ai_live_since",
]


def to_utc_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_whatsapp_message_timestamp_utc(raw: Any) -> Optional[datetime]:
    """WhatsApp Cloud API sends Unix epoch *seconds* as string."""
    if raw is None:
        return None
    try:
        sec = int(float(str(raw).strip()))
        return datetime.fromtimestamp(sec, tz=timezone.utc)
    except Exception:
        return None


def stamp_whatsapp_ai_live_since_if_empty(conn: Any) -> None:
    """Set ``whatsapp_ai_live_since`` to now if the column exists and is NULL."""
    if conn is None:
        return
    if getattr(conn, "whatsapp_ai_live_since", None) is not None:
        return
    conn.whatsapp_ai_live_since = datetime.now(timezone.utc)


def is_inbound_before_ai_live_since(
    conn: Any,
    message_ts_utc: Optional[datetime],
) -> bool:
    """
    True → skip AI + live side-effects (notifications / automation hooks).

    Missing WhatsApp timestamp → treat as **live** (use current time
    mentally in callers) — here we return False when ``message_ts_utc``
    is None so interactive taps without a parsed timestamp still behave
    as live traffic.

    Missing cutoff on row → False (no gating until stamped).
    """
    if conn is None:
        return False
    cutoff = getattr(conn, "whatsapp_ai_live_since", None)
    if cutoff is None:
        return False
    if message_ts_utc is None:
        return False
    msg_u = message_ts_utc if message_ts_utc.tzinfo else message_ts_utc.replace(tzinfo=timezone.utc)
    return msg_u < to_utc_aware(cutoff)
