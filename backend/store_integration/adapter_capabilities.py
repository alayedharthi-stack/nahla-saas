"""
store_integration/adapter_capabilities.py
──────────────────────────────────────────
Neutral, read-only commerce adapter capability declarations.

Provider adapters opt in via ``STORE_CAPABILITIES`` on the adapter class.
Unknown providers and missing declarations default to **false** — never
optimistic true.

This module is the single place merchant_capabilities consults for:
  * commerce vs non-commerce integrations
  * external coupon redemption support
  * external tracking URL support
  * store-issued payment link generation
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, Optional

logger = logging.getLogger("nahla.store_integration.adapter_capabilities")

# Capability keys adapters may declare on ``STORE_CAPABILITIES``.
CAP_SUPPORTS_COUPON_REDEMPTION = "supports_coupon_redemption"
CAP_SUPPORTS_TRACKING_URLS = "supports_tracking_urls"
CAP_SUPPORTS_PAYMENT_LINK_GENERATION = "supports_payment_link_generation"


@dataclass(frozen=True)
class StoreAdapterCapabilities:
    """Merchant-level store adapter signals — not per-order URLs."""

    provider: str = ""
    has_active_commerce_integration: bool = False
    supports_coupon_redemption: bool = False
    supports_tracking_urls: bool = False
    supports_payment_link_generation: bool = False

    def to_dict(self) -> Dict[str, bool]:
        return {
            "provider":                          bool(self.provider),
            "has_active_commerce_integration":   self.has_active_commerce_integration,
            "supports_coupon_redemption":        self.supports_coupon_redemption,
            "supports_tracking_urls":            self.supports_tracking_urls,
            "supports_payment_link_generation":    self.supports_payment_link_generation,
        }


_EMPTY = StoreAdapterCapabilities()


def _load_adapter_registry() -> Dict[str, type]:
    try:
        import store_adapters.salla_adapter  # noqa: F401, PLC0415
    except ImportError:
        pass
    from store_integration.registry import _ADAPTER_REGISTRY  # noqa: PLC0415

    return dict(_ADAPTER_REGISTRY)


def commerce_store_providers() -> FrozenSet[str]:
    """Registered store platform adapters (commerce integrations only)."""
    return frozenset(_load_adapter_registry().keys())


def _declared_capabilities(adapter_cls: type) -> Dict[str, bool]:
    raw = getattr(adapter_cls, "STORE_CAPABILITIES", None)
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, bool] = {}
    for key in (
        CAP_SUPPORTS_COUPON_REDEMPTION,
        CAP_SUPPORTS_TRACKING_URLS,
        CAP_SUPPORTS_PAYMENT_LINK_GENERATION,
    ):
        if key in raw:
            out[key] = bool(raw[key])
    return out


def _adapter_exposes_method(adapter_cls: type, method: str) -> bool:
    """True when the concrete adapter implements ``method`` beyond the ABC stub."""
    impl = getattr(adapter_cls, method, None)
    if impl is None:
        return False
    try:
        from store_adapters.base_adapter import BaseStoreAdapter  # noqa: PLC0415

        base_impl = getattr(BaseStoreAdapter, method, None)
        return impl is not base_impl
    except Exception:
        return callable(impl)


def pick_active_commerce_integration(db: Any, tenant_id: int) -> Optional[Any]:
    """
    Return the canonical active commerce ``Integration`` row, or None.

    Non-commerce integrations (billing, messaging, etc.) are excluded by
    restricting to registered store adapter providers.
    """
    if db is None or not tenant_id:
        return None

    providers = commerce_store_providers()
    if not providers:
        return None

    try:
        from database.models import Integration  # noqa: PLC0415
        from services.salla_guard import is_active_binding  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        logger.debug("[AdapterCaps] import failed: %s", exc)
        return None

    # Provider-specific canonical pickers (when present) win over generic rows.
    if "salla" in providers:
        try:
            from store_integration.registry import pick_active_salla_integration  # noqa: PLC0415

            salla_row = pick_active_salla_integration(db, int(tenant_id))
            if salla_row and is_active_binding(salla_row):
                return salla_row
        except Exception as exc:  # noqa: BLE001  # noqa: silent-ok — integration picker is best-effort
            logger.debug("[AdapterCaps] salla picker failed tenant=%s: %s", tenant_id, exc)

    rows = (
        db.query(Integration)
        .filter(
            Integration.tenant_id == int(tenant_id),
            Integration.provider.in_(list(providers)),
        )
        .order_by(Integration.id.desc())
        .all()
    )
    for row in rows:
        if is_active_binding(row):
            return row
    return None


def resolve_store_adapter_capabilities(
    db: Any,
    tenant_id: int,
) -> StoreAdapterCapabilities:
    """
    Resolve conservative store-adapter capabilities for a tenant.

    Defaults are false. A capability becomes true only when:
      1. An active commerce integration exists, AND
      2. The adapter class explicitly declares the capability true, AND
      3. For coupons/payment links, the adapter exposes the operational method.
    """
    integration = pick_active_commerce_integration(db, tenant_id)
    if integration is None:
        return _EMPTY

    provider = str(getattr(integration, "provider", "") or "").strip().lower()
    registry = _load_adapter_registry()
    adapter_cls = registry.get(provider)
    if adapter_cls is None:
        return StoreAdapterCapabilities(
            provider=provider,
            has_active_commerce_integration=True,
        )

    declared = _declared_capabilities(adapter_cls)

    coupon = bool(
        declared.get(CAP_SUPPORTS_COUPON_REDEMPTION, False)
        and _adapter_exposes_method(adapter_cls, "validate_coupon")
    )
    tracking = bool(declared.get(CAP_SUPPORTS_TRACKING_URLS, False))
    payment_links = bool(
        declared.get(CAP_SUPPORTS_PAYMENT_LINK_GENERATION, False)
        and _adapter_exposes_method(adapter_cls, "generate_payment_link")
    )

    return StoreAdapterCapabilities(
        provider=provider,
        has_active_commerce_integration=True,
        supports_coupon_redemption=coupon,
        supports_tracking_urls=tracking,
        supports_payment_link_generation=payment_links,
    )


__all__ = [
    "CAP_SUPPORTS_COUPON_REDEMPTION",
    "CAP_SUPPORTS_PAYMENT_LINK_GENERATION",
    "CAP_SUPPORTS_TRACKING_URLS",
    "StoreAdapterCapabilities",
    "commerce_store_providers",
    "pick_active_commerce_integration",
    "resolve_store_adapter_capabilities",
]
