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
    # Ensure salla adapter is registered
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
                Integration.enabled == True,
                Integration.provider.in_(list(_ADAPTER_REGISTRY.keys())),
            )
            .first()
        )
        if not integration:
            return None

        adapter_cls = _ADAPTER_REGISTRY.get(integration.provider)
        if not adapter_cls:
            logger.warning(f"No adapter registered for platform: {integration.provider}")
            return None

        cfg = integration.config or {}
        return adapter_cls(
            api_key=cfg.get("api_key", ""),
            store_id=cfg.get("store_id", ""),
            refresh_token=cfg.get("refresh_token", ""),
            tenant_id=tenant_id,
        )
    except Exception as exc:
        logger.error(f"AdapterRegistry error for tenant {tenant_id}: {exc}")
        return None
    finally:
        db.close()
