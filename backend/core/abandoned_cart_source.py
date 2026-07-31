"""
abandoned_cart_source.py
────────────────────────
Tenant-level abandoned-cart *source configuration model* (Phase 1).

This module defines configured vs effective source resolution only.
It must NOT gate sends, mute carts, or alter recovery runtime behavior.
Phase 2+ will consume these helpers at eligibility gates.

Allowed values (closed):
  - salla_storefront
  - nahla_shop
  - disabled

Semantics:
  configured_source  — explicit merchant override stored on TenantSettings
                       (NULL / unset = no override; never backfilled)
  effective_source   — configured override if present, else connection default:
                         Salla-connected → salla_storefront
                         otherwise       → nahla_shop

Webhook / cart ingest must never write configured_source.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy.orm import Session

ABANDONED_CART_SOURCE_SALLA_STOREFRONT = "salla_storefront"
ABANDONED_CART_SOURCE_NAHLA_SHOP = "nahla_shop"
ABANDONED_CART_SOURCE_DISABLED = "disabled"

ALLOWED_ABANDONED_CART_SOURCES = frozenset(
    {
        ABANDONED_CART_SOURCE_SALLA_STOREFRONT,
        ABANDONED_CART_SOURCE_NAHLA_SHOP,
        ABANDONED_CART_SOURCE_DISABLED,
    }
)


class AbandonedCartSourceValidationError(ValueError):
    """Unknown or architecturally unavailable abandoned_cart_source value."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class AbandonedCartSourceResolution:
    """configured_source is None when no merchant override is stored."""

    tenant_id: int
    configured_source: Optional[str]
    effective_source: str
    salla_connected: bool


def normalize_configured_abandoned_cart_source(raw: Any) -> Optional[str]:
    """Normalize a stored/input value to an allowed source or None.

    Raises AbandonedCartSourceValidationError for unknown non-empty values.
    Empty / None → None (no override).
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        value = raw.strip()
    else:
        value = str(raw).strip()
    if not value:
        return None
    lowered = value.lower()
    if lowered not in ALLOWED_ABANDONED_CART_SOURCES:
        raise AbandonedCartSourceValidationError(
            f"Unknown abandoned_cart_source={raw!r}; "
            f"allowed={sorted(ALLOWED_ABANDONED_CART_SOURCES)}",
            code="invalid_abandoned_cart_source",
        )
    return lowered


def default_abandoned_cart_source(*, salla_connected: bool) -> str:
    """Computed default when no configured override exists."""
    if salla_connected:
        return ABANDONED_CART_SOURCE_SALLA_STOREFRONT
    return ABANDONED_CART_SOURCE_NAHLA_SHOP


def validate_abandoned_cart_source_for_connection(
    source: str,
    *,
    salla_connected: bool,
) -> str:
    """Validate an explicit merchant choice against connection availability.

    Decision (Phase 1): choosing salla_storefront without an active Salla
    store binding is rejected — the source is architecturally unavailable.
    nahla_shop and disabled remain valid for any tenant.
    """
    normalized = normalize_configured_abandoned_cart_source(source)
    if normalized is None:
        raise AbandonedCartSourceValidationError(
            "abandoned_cart_source override cannot be empty; use clear instead",
            code="invalid_abandoned_cart_source",
        )
    if (
        normalized == ABANDONED_CART_SOURCE_SALLA_STOREFRONT
        and not salla_connected
    ):
        raise AbandonedCartSourceValidationError(
            "abandoned_cart_source=salla_storefront requires an active Salla store binding",
            code="unavailable_abandoned_cart_source",
        )
    return normalized


def resolve_effective_abandoned_cart_source(
    *,
    configured_source: Optional[str],
    salla_connected: bool,
) -> str:
    """Resolve effective source without writing defaults to storage."""
    configured = normalize_configured_abandoned_cart_source(configured_source)
    if configured is not None:
        return configured
    return default_abandoned_cart_source(salla_connected=salla_connected)


def tenant_is_salla_connected(db: Session, tenant_id: int) -> bool:
    """True when the tenant has an active Salla store binding.

    Mirrors abandoned-cart scheduler / Easy-mode rules:
    enabled Integration(provider=salla) with api_key and not needs_reauth.
    """
    from models import Integration  # noqa: PLC0415
    from services.salla_guard import is_active_binding  # noqa: PLC0415

    rows = (
        db.query(Integration)
        .filter(
            Integration.tenant_id == int(tenant_id),
            Integration.provider == "salla",
            Integration.enabled == True,  # noqa: E712
        )
        .all()
    )
    for intg in rows:
        cfg = intg.config or {}
        if cfg.get("needs_reauth"):
            continue
        if is_active_binding(intg):
            return True
    return False


def get_configured_abandoned_cart_source(settings: Any) -> Optional[str]:
    """Read configured override from TenantSettings (None if unset)."""
    if settings is None:
        return None
    raw = getattr(settings, "abandoned_cart_source", None)
    return normalize_configured_abandoned_cart_source(raw)


def set_configured_abandoned_cart_source(
    settings: Any,
    source: Optional[str],
    *,
    salla_connected: bool,
) -> Optional[str]:
    """Persist an explicit merchant override (or clear with None).

    Call only from merchant/settings APIs — never from webhooks or cart ingest.
    """
    if source is None or (isinstance(source, str) and not source.strip()):
        settings.abandoned_cart_source = None
        return None
    validated = validate_abandoned_cart_source_for_connection(
        source,
        salla_connected=salla_connected,
    )
    settings.abandoned_cart_source = validated
    return validated


def resolve_tenant_abandoned_cart_source(
    db: Session,
    tenant_id: int,
    *,
    settings: Any = None,
) -> AbandonedCartSourceResolution:
    """Load configured + effective source for a tenant (read-only)."""
    if settings is None:
        from models import TenantSettings  # noqa: PLC0415

        settings = (
            db.query(TenantSettings)
            .filter(TenantSettings.tenant_id == int(tenant_id))
            .one_or_none()
        )

    salla_connected = tenant_is_salla_connected(db, int(tenant_id))
    configured = get_configured_abandoned_cart_source(settings)
    effective = resolve_effective_abandoned_cart_source(
        configured_source=configured,
        salla_connected=salla_connected,
    )
    return AbandonedCartSourceResolution(
        tenant_id=int(tenant_id),
        configured_source=configured,
        effective_source=effective,
        salla_connected=salla_connected,
    )
