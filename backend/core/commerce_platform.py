"""Commerce-platform connection source of truth.

``store_settings.platform_type`` is a dashboard label. It must never imply
a live Salla / Zid / Shopify connection by itself.

Authoritative connection:
1. An enabled ``integrations`` row whose provider is salla|zid|shopify.
2. Legacy fallback: a non-empty credential field in store_settings.

Type-without-credentials and type-without-Integration are disconnected.
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.orm import Session

_CONNECTED_PROVIDERS = frozenset({"salla", "zid", "shopify"})


def resolve_connected_commerce_platform(db: Session, tenant_id: int) -> Optional[str]:
    """Return 'salla' | 'zid' | 'shopify' when a real connection exists."""
    from models import Integration  # noqa: PLC0415

    rows = (
        db.query(Integration)
        .filter(
            Integration.tenant_id == int(tenant_id),
            Integration.enabled == True,  # noqa: E712
        )
        .all()
    )
    for row in rows:
        provider = str(getattr(row, "provider", None) or "").strip().lower()
        if provider in _CONNECTED_PROVIDERS:
            return provider

    store = _store_settings(db, tenant_id)
    if str(store.get("salla_access_token") or "").strip():
        return "salla"
    if str(store.get("zid_client_id") or "").strip():
        return "zid"
    if str(store.get("shopify_access_token") or "").strip():
        return "shopify"
    return None


def platform_type_alone_is_not_connection(store_settings: Optional[dict[str, Any]]) -> bool:
    """True when platform_type is set but credentials are empty."""
    store = dict(store_settings or {})
    platform = str(store.get("platform_type") or "").strip().lower()
    if platform not in _CONNECTED_PROVIDERS:
        return False
    if platform == "salla":
        return not str(store.get("salla_access_token") or "").strip()
    if platform == "zid":
        return not str(store.get("zid_client_id") or "").strip()
    if platform == "shopify":
        return not str(store.get("shopify_access_token") or "").strip()
    return False


def _store_settings(db: Session, tenant_id: int) -> dict[str, Any]:
    from core.tenant import get_or_create_settings  # noqa: PLC0415

    try:
        settings = get_or_create_settings(db, tenant_id)
        return dict(settings.store_settings or {})
    except Exception:
        return {}
