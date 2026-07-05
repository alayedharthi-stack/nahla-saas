"""
catalog_browse_silent_recovery.py
──────────────────────────────────
P1 safety net when Brain returns an empty reply on catalog/product browse turns.

Operational — evidence-backed catalog direction only; no LLM compose required.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any, Optional

logger = logging.getLogger("nahla.brain.postprocess.catalog_browse_silent_recovery")

RECOVERY_SOURCE = "catalog_browse_silent_recovery"

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")

# Generic inventory browse — e.g. «عندكم منتجات؟» (ask_product without breadth helpers).
_GENERIC_INVENTORY_BROWSE_RE = re.compile(
    r"(?:"
    r"(?:عندكم|عندك|عندنا)\s*(?:من\s+)?(?:ال)?(?:منتجات|انواع|أنواع|متوفر|متاح)"
    r"|(?:وش|ايش|ايه)\s+عند(?:كم|ك)\s+(?:من\s+)?(?:ال)?(?:منتجات|انواع|أنواع)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_BROWSE_INTENTS = frozenset({
    "ask_product",
    "product_visual_request",
})


def _normalize(text: str) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text)).lower()
    t = _NORM_RE.sub("", t)
    t = (
        t.replace("\u0623", "\u0627")
        .replace("\u0625", "\u0627")
        .replace("\u0622", "\u0627")
        .replace("\u0649", "\u064a")
        .replace("\u0629", "\u0647")
    )
    return _WS_RE.sub(" ", t).strip()


def is_catalog_browse_silent_recovery_message(message: str) -> bool:
    """True when inbound is a broad catalog/product browse ask (not a specific SKU ask)."""
    raw = (message or "").strip()
    if not raw:
        return False

    intent_name = ""
    try:
        from modules.ai.brain.intent import rules as intent_rules  # noqa: PLC0415

        matched = intent_rules.match(raw)
        if matched is not None:
            intent_name = str(getattr(matched, "name", "") or "").strip()
    except Exception:  # noqa: BLE001  # noqa: silent-ok — intent probe must not break recovery
        pass

    try:
        from modules.ai.brain.catalog.catalog_browse_turn_policy import (  # noqa: PLC0415
            is_catalog_browse_message,
        )

        if is_catalog_browse_message(raw, intent_name=intent_name):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — browse policy probe must not break recovery
        pass

    try:
        from modules.ai.brain.commerce.commerce_entry_catalog_delivery import (  # noqa: PLC0415
            _is_explicit_catalog_browse_request,
        )

        if _is_explicit_catalog_browse_request(raw, None):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — catalog delivery probe must not break recovery
        pass

    try:
        from modules.ai.brain.commerce.product_breadth_policy import (  # noqa: PLC0415
            explicit_broad_browse_requested,
            global_availability_browse_requested,
            global_catalog_browse_requested,
        )

        if (
            global_availability_browse_requested(raw)
            or global_catalog_browse_requested(raw)
            or explicit_broad_browse_requested(raw)
        ):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — breadth policy probe must not break recovery
        pass

    if intent_name in _BROWSE_INTENTS and _GENERIC_INVENTORY_BROWSE_RE.search(_normalize(raw)):
        return True

    if intent_name == "product_visual_request":
        return True

    return False


def _tenant_has_catalog_products(db: Any, tenant_id: int) -> Optional[bool]:
    """Return True/False when DB is available; None when probe fails."""
    if db is None or not tenant_id:
        return None
    try:
        from core.catalog import apply_active_catalog_query_filters  # noqa: PLC0415
        from models import Product  # noqa: PLC0415
        from sqlalchemy import func  # noqa: PLC0415

        count = (
            apply_active_catalog_query_filters(
                db.query(func.count(Product.id)).filter(
                    Product.tenant_id == int(tenant_id),
                    Product.external_id.isnot(None),
                    Product.external_id != "",
                ),
                Product,
            ).scalar()
            or 0
        )
        return int(count) > 0
    except Exception:  # noqa: BLE001  # noqa: silent-ok — product probe must not break recovery
        logger.debug(
            "[CATALOG_BROWSE_SILENT_RECOVERY] product_probe_failed tenant=%s",
            tenant_id,
        )
        return None


def resolve_catalog_browse_silent_recovery_reply(
    *,
    has_products: Optional[bool] = None,
) -> str:
    """Deterministic safe reply — never the compose-failure technical fallback."""
    if has_products is False:
        from modules.ai.brain.compose import templates as T  # noqa: PLC0415

        return T.no_products(variant=0)

    from modules.ai.brain.commerce.catalog_body_policy import TECHNICAL_CATALOG_BODY  # noqa: PLC0415

    return TECHNICAL_CATALOG_BODY


def try_catalog_browse_silent_recovery(
    *,
    inbound_text: str = "",
    tenant_id: Optional[int] = None,
    db: Any = None,
) -> Optional[str]:
    """
    Return a safe catalog browse reply when *inbound_text* is a browse turn.

    Returns ``None`` when this recovery does not apply.
    """
    raw = (inbound_text or "").strip()
    if not raw or not is_catalog_browse_silent_recovery_message(raw):
        return None

    try:
        from modules.ai.brain.commerce.product_knowledge_or_comparison import (  # noqa: PLC0415
            is_product_knowledge_message,
        )

        if is_product_knowledge_message(raw):
            return None
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional product-knowledge probe
        pass

    has_products = _tenant_has_catalog_products(db, int(tenant_id or 0))
    reply = resolve_catalog_browse_silent_recovery_reply(has_products=has_products)
    logger.info(
        "[CATALOG_BROWSE_SILENT_RECOVERY] tenant=%s has_products=%s reply_len=%d",
        tenant_id,
        has_products,
        len(reply or ""),
    )
    return reply


__all__ = [
    "RECOVERY_SOURCE",
    "is_catalog_browse_silent_recovery_message",
    "resolve_catalog_browse_silent_recovery_reply",
    "try_catalog_browse_silent_recovery",
]
