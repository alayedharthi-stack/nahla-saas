from __future__ import annotations

from typing import Any, Optional

WHATSAPP_PROVIDER_META = "meta"
WHATSAPP_PROVIDER_360DIALOG = "dialog360"

WHATSAPP_CONNECTION_TYPE_DIRECT = "direct"
WHATSAPP_CONNECTION_TYPE_EMBEDDED = "embedded"
WHATSAPP_CONNECTION_TYPE_COEXISTENCE = "coexistence"


def wa_provider(conn: Optional[Any]) -> str:
    raw = str(getattr(conn, "provider", "") or "").strip().lower()
    if raw in {WHATSAPP_PROVIDER_META, WHATSAPP_PROVIDER_360DIALOG}:
        return raw
    return WHATSAPP_PROVIDER_META


def provider_label(conn: Optional[Any]) -> Optional[str]:
    if not conn:
        return None
    provider = wa_provider(conn)
    if provider == WHATSAPP_PROVIDER_360DIALOG:
        return "platform_managed"
    return "meta"


def merchant_channel_label(conn: Optional[Any]) -> Optional[str]:
    if not conn:
        return None
    provider = wa_provider(conn)
    ctype = str(getattr(conn, "connection_type", "") or "").strip().lower()
    if provider == WHATSAPP_PROVIDER_360DIALOG or ctype == WHATSAPP_CONNECTION_TYPE_COEXISTENCE:
        return "واتساب الجوال + الذكاء الاصطناعي"
    if ctype == WHATSAPP_CONNECTION_TYPE_EMBEDDED:
        return "ربط عبر Meta"
    if ctype == WHATSAPP_CONNECTION_TYPE_DIRECT:
        return "إدخال مباشر"
    return "واتساب الأعمال"
