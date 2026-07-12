"""
core/merchant_capabilities.py
─────────────────────────────
Platform-wide merchant capability resolver for template visibility and UI
grouping. Read-path only — no outbound sends, no order mutations.

Composes existing authoritative signals:
  * ``sales_channel_capabilities.resolve_merchant_sales_channels``
  * ``Integration`` rows (any provider, not Salla-specific)
  * ``native_catalog_capability.evaluate_native_catalog_capability``
  * ``merchant_payment_methods.load_merchant_payment_methods``
  * ``TenantSettings.store_settings`` for ``default_order_channel``

KB-only store URLs must NOT activate external checkout (enforced by
``store_url_evidence_activates_channel`` in the sales-channel resolver).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Literal, Optional

logger = logging.getLogger("nahla.merchant_capabilities")

MerchantMode = Literal["whatsapp_only", "external_store", "hybrid"]
OrderChannelPreference = Literal["external_store", "whatsapp", "adaptive"]

_VALID_ORDER_CHANNELS = frozenset({"external_store", "whatsapp", "adaptive"})


def capability_aware_templates_enabled() -> bool:
    """Env flag: visibility-only capability filtering for Nahla library."""
    val = str(os.getenv("CAPABILITY_AWARE_TEMPLATES", "")).strip().lower()
    return val in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class MerchantCapabilities:
    has_external_store: bool
    supports_external_checkout: bool
    supports_external_coupons: bool
    supports_whatsapp_orders: bool
    supports_nahla_orders: bool
    supports_bank_transfer: bool
    supports_cod: bool
    has_whatsapp_catalog: bool
    has_external_tracking: bool
    has_nahla_tracking: bool
    has_payment_link: bool

    def to_dict(self) -> Dict[str, bool]:
        return {
            "has_external_store":          self.has_external_store,
            "supports_external_checkout":  self.supports_external_checkout,
            "supports_external_coupons":   self.supports_external_coupons,
            "supports_whatsapp_orders":    self.supports_whatsapp_orders,
            "supports_nahla_orders":       self.supports_nahla_orders,
            "supports_bank_transfer":      self.supports_bank_transfer,
            "supports_cod":                self.supports_cod,
            "has_whatsapp_catalog":        self.has_whatsapp_catalog,
            "has_external_tracking":       self.has_external_tracking,
            "has_nahla_tracking":          self.has_nahla_tracking,
            "has_payment_link":            self.has_payment_link,
        }


def _has_active_store_integration(db: Any, tenant_id: int) -> bool:
    """True when any enabled integration can call its provider API."""
    if db is None or not tenant_id:
        return False
    try:
        from database.models import Integration  # noqa: PLC0415
        from services.salla_guard import is_active_binding  # noqa: PLC0415

        rows = (
            db.query(Integration)
            .filter(Integration.tenant_id == int(tenant_id))
            .all()
        )
        return any(is_active_binding(row) for row in rows)
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[MerchantCapabilities] integration probe failed tenant=%s: %s",
            tenant_id,
            exc,
        )
        return False


def read_default_order_channel(db: Any, tenant_id: int) -> OrderChannelPreference:
    """Additive read from ``store_settings.default_order_channel``."""
    try:
        from core.tenant import DEFAULT_STORE, get_or_create_settings, merge_defaults  # noqa: PLC0415

        if db is None or not tenant_id:
            return "adaptive"
        settings = get_or_create_settings(db, int(tenant_id))
        store_cfg = merge_defaults(settings.store_settings, DEFAULT_STORE)
        raw = str(store_cfg.get("default_order_channel") or "adaptive").strip().lower()
        if raw in _VALID_ORDER_CHANNELS:
            return raw  # type: ignore[return-value]
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[MerchantCapabilities] default_order_channel read failed tenant=%s: %s",
            tenant_id,
            exc,
        )
    return "adaptive"


def resolve_merchant_mode(caps: MerchantCapabilities) -> MerchantMode:
    ext = caps.supports_external_checkout
    wa = caps.supports_whatsapp_orders or caps.supports_nahla_orders
    if ext and wa:
        return "hybrid"
    if wa and not ext:
        return "whatsapp_only"
    return "external_store"


def resolve_merchant_capabilities(db: Any, tenant_id: int) -> MerchantCapabilities:
    """Compute tenant capabilities from persisted platform signals."""
    tid = int(tenant_id or 0)
    has_external_store = _has_active_store_integration(db, tid)

    channels = None
    try:
        from modules.ai.brain.commerce.sales_channel_capabilities import (  # noqa: PLC0415
            resolve_merchant_sales_channels,
        )

        channels = resolve_merchant_sales_channels(db, tid)
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[MerchantCapabilities] sales channels failed tenant=%s: %s",
            tid,
            exc,
        )

    supports_external_checkout = bool(
        channels
        and channels.online_store.enabled
        and channels.online_store.available
    )
    # Integration without structured checkout URL must not unlock checkout templates.
    if has_external_store and not supports_external_checkout:
        has_external_store = False

    whatsapp_toggle = bool(
        channels and channels.whatsapp_quick_order.enabled
    ) if channels else True

    has_whatsapp_catalog = False
    try:
        from core.native_catalog_capability import (  # noqa: PLC0415
            evaluate_native_catalog_capability,
        )

        has_whatsapp_catalog = bool(
            evaluate_native_catalog_capability(db, tid).eligible
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[MerchantCapabilities] catalog probe failed tenant=%s: %s",
            tid,
            exc,
        )

    supports_whatsapp_orders = bool(whatsapp_toggle)
    supports_nahla_orders = supports_whatsapp_orders

    payment = None
    try:
        from core.merchant_payment_methods import load_merchant_payment_methods  # noqa: PLC0415

        payment = load_merchant_payment_methods(db, tid)
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[MerchantCapabilities] payment methods failed tenant=%s: %s",
            tid,
            exc,
        )

    supports_bank_transfer = bool(payment and payment.bank_transfer_enabled)
    supports_cod = bool(payment and payment.cash_on_delivery_enabled)
    has_payment_link = bool(
        supports_external_checkout
        or (payment and (payment.moyasar_checkout_ready or payment.moyasar_enabled))
    )

    supports_external_coupons = bool(
        supports_external_checkout and has_external_store
    )

    has_external_tracking = bool(has_external_store and supports_external_checkout)
    has_nahla_tracking = False  # PR5 — no public Nahla tracking page yet

    return MerchantCapabilities(
        has_external_store=has_external_store,
        supports_external_checkout=supports_external_checkout,
        supports_external_coupons=supports_external_coupons,
        supports_whatsapp_orders=supports_whatsapp_orders,
        supports_nahla_orders=supports_nahla_orders,
        supports_bank_transfer=supports_bank_transfer,
        supports_cod=supports_cod,
        has_whatsapp_catalog=has_whatsapp_catalog,
        has_external_tracking=has_external_tracking,
        has_nahla_tracking=has_nahla_tracking,
        has_payment_link=has_payment_link,
    )


__all__ = [
    "MerchantCapabilities",
    "MerchantMode",
    "OrderChannelPreference",
    "capability_aware_templates_enabled",
    "read_default_order_channel",
    "resolve_merchant_capabilities",
    "resolve_merchant_mode",
]
