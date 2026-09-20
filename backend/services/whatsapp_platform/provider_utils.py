"""Which WhatsApp provider a stored connection names, and whether it is one
this platform still supports.

Meta WhatsApp Cloud API is the only supported provider. 360dialog was removed;
its connection rows were not, because a merchant's history, phone number and
connection record are not ours to delete. So a row may still name a provider
this code no longer speaks, and the one answer that must never be given for it
is "meta": that would send a merchant's traffic to Meta with credentials issued
by somebody else, on a number Meta may not have.

``wa_provider`` therefore answers ``meta`` only for a row that says Meta (or
says nothing, which is how every Meta row was written before the column
existed), and ``unsupported`` for everything else. Callers that act on a
connection — send, token resolution, configuration — ask
``require_supported_provider`` first and refuse rather than guess.
"""
from __future__ import annotations

from typing import Any, Optional

WHATSAPP_PROVIDER_META = "meta"

# What a row names when it is not Meta. Not a provider anyone can select: it is
# the answer for a stored value this platform does not support.
WHATSAPP_PROVIDER_UNSUPPORTED = "unsupported"

# Named so diagnostics can say *which* retired integration a row is left over
# from. Nothing routes on this set; it is for the operator reading the log.
RETIRED_PROVIDERS = frozenset({"dialog360", "360dialog", "d360"})

WHATSAPP_CONNECTION_TYPE_DIRECT = "direct"
WHATSAPP_CONNECTION_TYPE_EMBEDDED = "embedded"
WHATSAPP_CONNECTION_TYPE_COEXISTENCE = "coexistence"
WHATSAPP_CONNECTION_TYPE_ASSISTED = "assisted"


class UnsupportedWhatsAppProvider(RuntimeError):
    """This connection names a provider the platform no longer speaks."""

    def __init__(self, raw: str) -> None:
        self.raw = str(raw or "")
        super().__init__(
            f"whatsapp connection provider {self.raw!r} is not supported; "
            f"Meta WhatsApp Cloud API is the only supported provider"
        )


def raw_provider(conn: Optional[Any]) -> str:
    """Exactly what the row says, normalised for comparison. Never guessed."""
    return str(getattr(conn, "provider", "") or "").strip().lower()


def wa_provider(conn: Optional[Any]) -> str:
    """``meta`` or ``unsupported``. A row is never promoted to Meta.

    An empty value is Meta: that is what every Meta row held before the column
    was introduced, and it is the platform's own default rather than another
    provider's leftover.
    """
    raw = raw_provider(conn)
    if raw in {"", WHATSAPP_PROVIDER_META}:
        return WHATSAPP_PROVIDER_META
    return WHATSAPP_PROVIDER_UNSUPPORTED


def provider_is_supported(conn: Optional[Any]) -> bool:
    return wa_provider(conn) == WHATSAPP_PROVIDER_META


def require_supported_provider(conn: Optional[Any]) -> None:
    """Raise unless this connection is one the platform can actually use."""
    if not provider_is_supported(conn):
        raise UnsupportedWhatsAppProvider(raw_provider(conn))


def provider_label(conn: Optional[Any]) -> Optional[str]:
    if not conn:
        return None
    if not provider_is_supported(conn):
        return WHATSAPP_PROVIDER_UNSUPPORTED
    return WHATSAPP_PROVIDER_META


def merchant_channel_label(conn: Optional[Any]) -> Optional[str]:
    if not conn:
        return None
    ctype = str(getattr(conn, "connection_type", "") or "").strip().lower()
    if ctype == WHATSAPP_CONNECTION_TYPE_COEXISTENCE:
        return "واتساب الجوال + الذكاء الاصطناعي"
    if ctype == WHATSAPP_CONNECTION_TYPE_EMBEDDED:
        return "ربط عبر Meta"
    if ctype == WHATSAPP_CONNECTION_TYPE_DIRECT:
        return "إدخال مباشر"
    if ctype == WHATSAPP_CONNECTION_TYPE_ASSISTED:
        return "طلب ربط بمساعدة نحلة"
    return "واتساب الأعمال"
