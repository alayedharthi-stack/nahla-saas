"""
core/native_catalog_fallback.py
─────────────────────────────────
Operational fallback when WhatsApp ``catalog_message`` fails at send time.

Produces an honest reply — no claim that the native catalog appeared.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger("nahla.native_catalog")

_CATALOG_CLAIM_PHRASE = "تفضّل، اختر من الكتالوج"

_WHATSAPP_QUICK_ORDER_FALLBACK_AR = (
    "ما ظهر الكتالوج هنا، أقدر أكمّل طلبك بالواتساب. "
    "اكتب اسم المنتج أو النوع اللي تبيه."
)

_STORE_FALLBACK_INTRO_AR = (
    "ما ظهر الكتالوج هنا. تقدر تتصفح منتجاتنا من المتجر الإلكتروني:"
)

_STORE_CTA_LABEL_AR = "فتح المتجر الإلكتروني"


@dataclass(frozen=True)
class NativeCatalogFallbackDecision:
    """Structured outbound fallback after a native catalog send failure."""

    text: str
    cta_url: str = ""
    cta_label: str = ""
    delivery_mode: str = "text"
    failure_reason: str = ""


def _load_store_url(db: Any, tenant_id: int) -> str:
    if db is None or not tenant_id:
        return ""
    try:
        from modules.ai.brain.commerce.checkout_route_owner import (  # noqa: PLC0415
            load_channel_capabilities,
        )

        caps = load_channel_capabilities(db, int(tenant_id))
        return str(getattr(caps, "store_url", "") or "").strip()
    except Exception as exc:  # noqa: BLE001  # noqa: silent-ok — optional store capability probe
        logger.debug(
            "[NATIVE_CATALOG] store_capability_load_failed tenant=%s err=%s",
            tenant_id,
            exc,
        )
        return ""


def compose_native_catalog_failure_decision(
    db: Any,
    tenant_id: Optional[int],
    *,
    failure_reason: str = "",
    customer_message: str = "",
) -> NativeCatalogFallbackDecision:
    """Return tenant-aware fallback after native catalog send failure."""
    _ = customer_message  # reserved for future scoped browse fallback
    store_url = _load_store_url(db, tenant_id) if tenant_id else ""
    if store_url:
        return NativeCatalogFallbackDecision(
            text=_STORE_FALLBACK_INTRO_AR,
            cta_url=store_url,
            cta_label=_STORE_CTA_LABEL_AR,
            delivery_mode="cta_url",
            failure_reason=str(failure_reason or "").strip(),
        )
    return NativeCatalogFallbackDecision(
        text=_WHATSAPP_QUICK_ORDER_FALLBACK_AR,
        delivery_mode="text",
        failure_reason=str(failure_reason or "").strip(),
    )


def compose_native_catalog_failure_reply(
    db: Any,
    tenant_id: Optional[int],
    *,
    failure_reason: str = "",
    customer_message: str = "",
) -> str:
    """Backward-compatible text-only wrapper."""
    decision = compose_native_catalog_failure_decision(
        db,
        tenant_id,
        failure_reason=failure_reason,
        customer_message=customer_message,
    )
    text = str(decision.text or "").strip()
    if _CATALOG_CLAIM_PHRASE in text:
        return _WHATSAPP_QUICK_ORDER_FALLBACK_AR
    return text or _WHATSAPP_QUICK_ORDER_FALLBACK_AR


__all__ = [
    "NativeCatalogFallbackDecision",
    "compose_native_catalog_failure_decision",
    "compose_native_catalog_failure_reply",
]
