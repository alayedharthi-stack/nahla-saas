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
    if src == "kb_free_text":
        return False
    if not src or src == "none":
        return True
    return src in _STRUCTURED_STORE_EVIDENCE


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
        default_factory=lambda: SalesChannelSlot(True, True, "whatsapp_catalog_or_enabled"),
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
        if not out and self.whatsapp_quick_order.enabled:
            out.append("whatsapp_quick_order")
        return out


def _resolve_maps_url(db: Any, tenant_id: int) -> tuple[str, str]:
    maps_url = ""
    evidence = "none"
    try:
        from database.models import StoreKnowledgeSnapshot, TenantSettings  # noqa: PLC0415

        snap = (
            db.query(StoreKnowledgeSnapshot)
            .filter(StoreKnowledgeSnapshot.tenant_id == tenant_id)
            .first()
        )
        if snap and snap.store_profile:
            maps_url = str((snap.store_profile or {}).get("maps_url") or "").strip()
            if maps_url:
                evidence = "merchant_profile_maps_url"

        settings = (
            db.query(TenantSettings)
            .filter(TenantSettings.tenant_id == tenant_id)
            .first()
        )
        if settings and not maps_url:
            store_cfg = dict(settings.store_settings or {})
            maps_url = str(store_cfg.get("google_maps_location") or "").strip()
            if maps_url:
                evidence = "structured_settings_maps_url"
    except Exception:  # noqa: BLE001  # noqa: silent-ok — maps probe must not block channel resolution
        pass

    if not maps_url:
        try:
            from modules.operations.branch_contact_evidence import (  # noqa: PLC0415
                structured_branch_contacts_enabled,
                tenant_has_structured_branch_data,
            )

            if structured_branch_contacts_enabled() and tenant_has_structured_branch_data(
                db, int(tenant_id or 0),
            ):
                evidence = "structured_branch_data"
        except Exception:  # noqa: BLE001  # noqa: silent-ok — branch probe must not block channel resolution
            pass
    return maps_url, evidence


def resolve_merchant_sales_channels(
    db: Any,
    tenant_id: int,
    *,
    store_url: str = "",
    store_url_source: str = "",
    maps_url: str = "",
) -> MerchantSalesChannels:
    """
    Single source of truth for purchase-channel availability.

    When ``store_url`` / ``maps_url`` are pre-loaded on CommerceFacts, pass them
    through together with ``store_url_source`` so Navigator matches facts loader.
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

    resolved_url = str(store_url or "").strip()
    resolved_source = str(store_url_source or "none").strip() or "none"
    if db is not None and tenant_id and not resolved_url:
        try:
            from modules.ai.brain.commerce.store_url_resolver import (  # noqa: PLC0415
                resolve_store_url,
            )

            resolution = resolve_store_url(db, int(tenant_id))
            resolved_url = str(resolution.url or "").strip()
            resolved_source = str(resolution.source or "none")
        except Exception:  # noqa: BLE001  # noqa: silent-ok — store URL resolver must not block channels
            pass

    resolved_maps = str(maps_url or "").strip()
    maps_evidence = "maps_url" if resolved_maps else "none"
    if db is not None and tenant_id and not resolved_maps:
        resolved_maps, maps_evidence = _resolve_maps_url(db, int(tenant_id))

    showroom_available = bool(resolved_maps) or maps_evidence == "structured_branch_data"

    online_available = store_url_evidence_activates_channel(
        source=resolved_source,
        found=bool(resolved_url),
    )

    return MerchantSalesChannels(
        store_url=resolved_url,
        store_url_source=resolved_source,
        maps_url=resolved_maps,
        online_store=SalesChannelSlot(
            enabled=toggles["online_store"],
            available=online_available,
            evidence="store_url" if online_available else resolved_source or "none",
        ),
        whatsapp_quick_order=SalesChannelSlot(
            enabled=toggles["whatsapp_quick_order"],
            available=toggles["whatsapp_quick_order"],
            evidence="whatsapp_catalog_or_enabled",
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
]
