"""
Shared tenant resolution utility.

Maps a platform store_id (string) to the internal Nahla tenant_id (int).
Used by webhook handlers in all platform integrations so the same
lookup logic is not duplicated per platform.
"""

import sys
import os
from typing import Any, Dict, Optional

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from database.models import Integration, Tenant
from database.session import SessionLocal


def get_tenant_id_for_store(provider: str, store_id: str) -> Optional[int]:
    """
    Look up the internal tenant_id for a platform store.

    Args:
        provider:  'salla' | 'zid' | 'shopify' | …
        store_id:  The platform's unique store identifier (string).

    Returns:
        The Nahla tenant_id (int) or None if not found / disabled.
    """
    db = SessionLocal()
    try:
        integration = (
            db.query(Integration)
            .filter(
                Integration.provider == provider,
                Integration.enabled  == True,
                Integration.config["store_id"].astext == str(store_id),
            )
            .first()
        )
        return integration.tenant_id if integration else None
    finally:
        db.close()


def get_integration_config(provider: str, store_id: str) -> Optional[Dict[str, Any]]:
    """
    Return the full integration config dict for a store, or None if not found.

    Useful for retrieving access_token / refresh_token before making API calls.
    """
    db = SessionLocal()
    try:
        integration = (
            db.query(Integration)
            .filter(
                Integration.provider == provider,
                Integration.enabled  == True,
                Integration.config["store_id"].astext == str(store_id),
            )
            .first()
        )
        if not integration:
            return None
        return {
            "tenant_id":      integration.tenant_id,
            "integration_id": integration.id,
            "store_id":       store_id,
            **integration.config,   # access_token, refresh_token, store_name, …
        }
    finally:
        db.close()


def upsert_tenant_and_integration(
    provider: str,
    store_data: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Create or update a Tenant + Integration row after a successful OAuth flow.

    store_data must contain:
      store_id, store_name, access_token, refresh_token
    Optionally:
      store_domain, store_email

    Returns a dict: {id, name, domain, provider, store_id, integration_id}
    """
    store_id   = str(store_data.get("store_id", ""))
    store_name = store_data.get("store_name") or f"{provider.capitalize()} Store"
    domain     = store_data.get("store_domain") or None

    db = SessionLocal()
    try:
        # Re-install: update tokens on existing integration
        existing = (
            db.query(Integration)
            .filter(
                Integration.provider == provider,
                Integration.config["store_id"].astext == store_id,
            )
            .first()
        )

        if existing:
            existing.config = {
                **existing.config,
                "access_token":  store_data.get("access_token"),
                "refresh_token": store_data.get("refresh_token"),
            }
            existing.enabled = True
            db.commit()
            tenant = db.query(Tenant).filter(Tenant.id == existing.tenant_id).first()
            return _to_dict(tenant, existing, provider)

        # Fresh install
        tenant = Tenant(name=store_name, domain=domain, is_active=True)
        db.add(tenant)
        db.flush()  # populate tenant.id

        integration = Integration(
            provider   = provider,
            tenant_id  = tenant.id,
            enabled    = True,
            config     = {
                "store_id":    store_id,
                "store_name":  store_name,
                "store_domain": store_data.get("store_domain", ""),
                "store_email":  store_data.get("store_email", ""),
                "access_token":  store_data.get("access_token"),
                "refresh_token": store_data.get("refresh_token"),
            },
        )
        db.add(integration)
        db.commit()
        db.refresh(tenant)
        return _to_dict(tenant, integration, provider)

    finally:
        db.close()


def _to_dict(tenant: Tenant, integration: Integration, provider: str) -> Dict[str, Any]:
    return {
        "id":             tenant.id,
        "name":           tenant.name,
        "domain":         tenant.domain,
        "provider":       provider,
        "store_id":       integration.config.get("store_id"),
        "integration_id": integration.id,
    }
