"""
Unified merchant sales-channel availability — structured evidence only.

Operational facts for Commerce Navigator / checkout_route_owner parity.
Never emits customer reply text.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

PurchaseChannelId = str

_STRUCTURED_STORE_EVIDENCE = frozenset({
    "merchant_profile",
    "structured_settings",
    "integration",
    "merchant_override",
})

_DEFAULT_CHANNEL_TOGGLES: Dict[str, Dict[str, bool]] = {
    "online_store": {"enabled": True},
    "whatsapp_quick_order": {"enabled": True},
    "showroom_visit": {"enabled": True},
}


def parse_sales_channel_toggles(store_settings: Optional[Dict[str, Any]]) -> Dict[str, bool]:
    """Read merchant toggles from store_settings.sales_channels."""
    cfg = dict(store_settings or {})
    raw = cfg.get("sales_channels") or {}
    if not isinstance(raw, dict):
        raw = {}
    out: Dict[str, bool] = {}
    for ch, default in _DEFAULT_CHANNEL_TOGGLES.items():
        entry = raw.get(ch) or {}
        if isinstance(entry, dict):
            out[ch] = bool(entry.get("enabled", default["enabled"]))
        else:
            out[ch] = bool(default["enabled"])
    return out


def store_url_evidence_activates_channel(*, source: str = "", found: bool = False) -> bool:
    """KB-only URLs must not activate online_store purchase channel."""
    if not found:
        return False
    src = str(source or "").strip().lower()
    if src.startswith("kb_free_text"):
        return False
    if not src or src == "none":
        return True
    head = src.split(":", 1)[0].strip()
    return src in _STRUCTURED_STORE_EVIDENCE or head in _STRUCTURED_STORE_EVIDENCE


def _canonical_customer_url(url: str) -> str:
    try:
        from modules.ai.brain.commerce.store_url_resolver import (  # noqa: PLC0415
            canonical_merchant_storefront_url,
        )

        return canonical_merchant_storefront_url(url)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — invalid URL must not activate a channel
        return ""


@dataclass(frozen=True)
class SalesChannelSlot:
    enabled: bool
    available: bool
    evidence: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "available": self.available,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class MerchantSalesChannels:
    store_url: str = ""
    store_url_source: str = "none"
    maps_url: str = ""
    online_store: SalesChannelSlot = field(
        default_factory=lambda: SalesChannelSlot(False, False, "none"),
    )
    whatsapp_quick_order: SalesChannelSlot = field(
        default_factory=lambda: SalesChannelSlot(True, False, "none"),
    )
    showroom_visit: SalesChannelSlot = field(
        default_factory=lambda: SalesChannelSlot(True, False, "none"),
    )

    def availability_facts(self) -> Dict[str, Dict[str, Any]]:
        return {
            "online_store": self.online_store.to_dict(),
            "whatsapp_quick_order": self.whatsapp_quick_order.to_dict(),
            "showroom_visit": self.showroom_visit.to_dict(),
        }

    def available_purchase_channel_ids(self) -> List[PurchaseChannelId]:
        out: List[PurchaseChannelId] = []
        if self.online_store.enabled and self.online_store.available:
            out.append("online_store")
        if self.whatsapp_quick_order.enabled and self.whatsapp_quick_order.available:
            out.append("whatsapp_quick_order")
        if self.showroom_visit.enabled and self.showroom_visit.available:
            out.append("showroom_visit")
        return out


def _resolve_maps_url(db: Any, tenant_id: int) -> tuple[str, str]:
    """Showroom maps evidence — same canonical location as CTA and facts."""
    try:
        from modules.operations.branch_contact_evidence import (  # noqa: PLC0415
            resolve_canonical_location,
        )

        loc = resolve_canonical_location(db, int(tenant_id or 0))
        if loc.showroom_visit_available and loc.maps_url:
            maps = _canonical_customer_url(loc.maps_url) or str(loc.maps_url or "").strip()
            if maps:
                return maps, loc.source or "structured_branch"
    except Exception:  # noqa: BLE001  # noqa: silent-ok — canonical location must not block channels
        pass
    return "", "none"


def whatsapp_order_processing_ready(
    db: Any,
    tenant_id: int,
    *,
    whatsapp_order_ready: Optional[bool] = None,
) -> bool:
    """WhatsApp connection/order-processing readiness — not native catalog browse.

    Canonical owner: connected ``WhatsAppConnection`` with a phone_number_id,
    same signal as ``trial_lifecycle._tenant_has_connected_whatsapp``.
    Distinct from ``evaluate_native_catalog_capability.eligible``.
    """
    if whatsapp_order_ready is not None:
        return bool(whatsapp_order_ready)
    if db is None or not tenant_id:
        return False
    try:
        from models import WhatsAppConnection  # noqa: PLC0415

        conn = (
            db.query(WhatsAppConnection)
            .filter(WhatsAppConnection.tenant_id == int(tenant_id))
            .first()
        )
        if conn is None:
            return False
        return str(getattr(conn, "status", "") or "") == "connected" and bool(
            getattr(conn, "phone_number_id", None)
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — WhatsApp capability must fail closed
        return False


def _whatsapp_order_capability_ready(
    db: Any,
    tenant_id: int,
    *,
    whatsapp_order_ready: Optional[bool],
) -> bool:
    return whatsapp_order_processing_ready(
        db,
        tenant_id,
        whatsapp_order_ready=whatsapp_order_ready,
    )


def resolve_merchant_sales_channels(
    db: Any,
    tenant_id: int,
    *,
    store_url: str = "",
    store_url_source: str = "",
    maps_url: str = "",
    whatsapp_order_ready: Optional[bool] = None,
) -> MerchantSalesChannels:
    """
    Single source of truth for purchase-channel availability.

    When ``store_url`` / ``maps_url`` are pre-loaded on CommerceFacts, pass them
    through together with ``store_url_source`` so Navigator matches facts loader.

    WhatsApp availability is ``enabled AND`` WhatsApp order-processing
    readiness (connected WhatsApp number) — never native-catalog browse
    eligibility, and never ``enabled == available``.
    """
    toggles = {"online_store": True, "whatsapp_quick_order": True, "showroom_visit": True}
    try:
        from core.tenant import DEFAULT_STORE, get_or_create_settings, merge_defaults  # noqa: PLC0415

        if db is not None and tenant_id:
            settings = get_or_create_settings(db, int(tenant_id))
            toggles = parse_sales_channel_toggles(
                merge_defaults(settings.store_settings, DEFAULT_STORE),
            )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — toggle read must not block resolution
        pass

    resolved_url = _canonical_customer_url(store_url)
    resolved_source = str(store_url_source or "none").strip() or "none"
    if db is not None and tenant_id and not resolved_url:
        try:
            from modules.ai.brain.commerce.store_url_resolver import (  # noqa: PLC0415
                resolve_store_url,
            )

            resolution = resolve_store_url(db, int(tenant_id))
            resolved_url = _canonical_customer_url(resolution.url)
            resolved_source = str(resolution.source or "none")
        except Exception:  # noqa: BLE001  # noqa: silent-ok — store URL resolver must not block channels
            pass

    resolved_maps = _canonical_customer_url(maps_url)
    maps_evidence = "maps_url" if resolved_maps else "none"
    if db is not None and tenant_id and not resolved_maps:
        resolved_maps, maps_evidence = _resolve_maps_url(db, int(tenant_id))
        if resolved_maps and not _canonical_customer_url(resolved_maps):
            # Canonical location already proved a usable maps URL; keep it.
            pass

    showroom_available = bool(resolved_maps)

    online_available = store_url_evidence_activates_channel(
        source=resolved_source,
        found=bool(resolved_url),
    )

    wa_enabled = bool(toggles["whatsapp_quick_order"])
    wa_ready = _whatsapp_order_capability_ready(
        db,
        tenant_id,
        whatsapp_order_ready=whatsapp_order_ready,
    )
    wa_available = bool(wa_enabled and wa_ready)
    if wa_available:
        wa_evidence = "whatsapp_order_processing"
    elif wa_enabled:
        wa_evidence = "whatsapp_connection_unavailable"
    else:
        wa_evidence = "whatsapp_disabled"

    return MerchantSalesChannels(
        store_url=resolved_url if online_available else (resolved_url if resolved_url else ""),
        store_url_source=resolved_source,
        maps_url=resolved_maps,
        online_store=SalesChannelSlot(
            enabled=toggles["online_store"],
            available=online_available,
            evidence="store_url" if online_available else resolved_source or "none",
        ),
        whatsapp_quick_order=SalesChannelSlot(
            enabled=wa_enabled,
            available=wa_available,
            evidence=wa_evidence,
        ),
        showroom_visit=SalesChannelSlot(
            enabled=toggles["showroom_visit"],
            available=showroom_available,
            evidence=maps_evidence if showroom_available else "none",
        ),
    )


__all__ = [
    "MerchantSalesChannels",
    "SalesChannelSlot",
    "parse_sales_channel_toggles",
    "resolve_merchant_sales_channels",
    "store_url_evidence_activates_channel",
    "whatsapp_order_processing_ready",
]
