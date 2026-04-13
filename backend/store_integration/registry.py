"""
AdapterRegistry
───────────────
Resolves the correct BaseStoreAdapter for a given tenant.
Reads from the Integration table (provider='salla', enabled=True).
No caching — always fresh from DB so credential updates take effect immediately.
"""
from __future__ import annotations
import logging
import os, sys
from typing import Optional

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from database.session import SessionLocal
from database.models import Integration

logger = logging.getLogger("nahla.store_integration.registry")

_ADAPTER_REGISTRY = {}  # platform -> adapter class, registered at import time

def register_adapter(platform: str):
    def decorator(cls):
        _ADAPTER_REGISTRY[platform] = cls
        return cls
    return decorator

def get_adapter(tenant_id: int):
    """
    Returns a BaseStoreAdapter instance for the tenant, or None if no
    store integration is configured.
    """
    try:
        import store_adapters.salla_adapter  # noqa: F401
    except ImportError:
        pass

    db = SessionLocal()
    try:
        integration = (
            db.query(Integration)
            .filter(
                Integration.tenant_id == tenant_id,
                Integration.enabled == True,  # noqa: E712
                Integration.provider.in_(list(_ADAPTER_REGISTRY.keys())),
            )
            .first()
        )
        if not integration:
            all_for_tenant = (
                db.query(Integration)
                .filter(Integration.tenant_id == tenant_id, Integration.provider == "salla")
                .first()
            )
            if all_for_tenant:
                cfg = all_for_tenant.config or {}
                logger.warning(
                    "[Registry] tenant=%s has salla integration BUT enabled=%s | "
                    "store_id=%s has_token=%s has_refresh=%s",
                    tenant_id, all_for_tenant.enabled,
                    cfg.get("store_id", ""),
                    bool(cfg.get("api_key")),
                    bool(cfg.get("refresh_token")),
                )
            else:
                logger.info("[Registry] tenant=%s — no integration found at all", tenant_id)
            return None

        adapter_cls = _ADAPTER_REGISTRY.get(integration.provider)
        if not adapter_cls:
            logger.warning("[Registry] No adapter class for provider=%s", integration.provider)
            return None

        cfg = integration.config or {}
        has_token = bool(cfg.get("api_key"))
        has_refresh = bool(cfg.get("refresh_token"))
        logger.info(
            "[Registry] tenant=%s → adapter=%s store_id=%s has_token=%s has_refresh=%s",
            tenant_id, integration.provider, cfg.get("store_id", ""), has_token, has_refresh,
        )
        if not has_token:
            logger.error(
                "[Registry] tenant=%s — integration enabled but api_key is EMPTY — sync will fail",
                tenant_id,
            )

        return adapter_cls(
            api_key=cfg.get("api_key", ""),
            store_id=cfg.get("store_id", ""),
            refresh_token=cfg.get("refresh_token", ""),
            tenant_id=tenant_id,
        )
    except Exception as exc:
        logger.error("[Registry] tenant=%s error: %s", tenant_id, exc)
        return None
    finally:
        db.close()
