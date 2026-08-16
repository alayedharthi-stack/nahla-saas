"""Idempotent tenant-config hygiene — obsolete keys and platform_type SoT.

Platform-wide. No tenant-id special cases. Does not change Brain/LLM
ownership, prompts, or models.

Retired AI JSON key:
  persona_composer_allowlist_tenants

Canonical platform_type:
  enabled Integration.provider (salla|zid|shopify)
  else non-empty matching store_settings credentials
  else ``custom`` — never claim salla/zid/shopify without that evidence.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

logger = logging.getLogger("nahla.tenant_config_hygiene")

OBSOLETE_AI_KEYS = frozenset({"persona_composer_allowlist_tenants"})
CONNECTED_PROVIDERS = frozenset({"salla", "zid", "shopify"})
CANONICAL_DISCONNECTED_PLATFORM = "custom"


def strip_obsolete_ai_keys(ai_settings: Optional[Dict[str, Any]]) -> tuple[Dict[str, Any], bool]:
    """Return a copy of ai_settings without retired keys."""
    out = dict(ai_settings or {})
    changed = False
    for key in OBSOLETE_AI_KEYS:
        if key in out:
            out.pop(key, None)
            changed = True
    return out, changed


def canonical_platform_type(
    store_settings: Optional[Dict[str, Any]],
    connected_provider: Optional[str],
) -> str:
    """Dashboard label that must agree with authoritative connection state."""
    connected = str(connected_provider or "").strip().lower()
    if connected in CONNECTED_PROVIDERS:
        return connected
    current = str((store_settings or {}).get("platform_type") or "").strip().lower()
    if current in CONNECTED_PROVIDERS:
        return CANONICAL_DISCONNECTED_PLATFORM
    return current or CANONICAL_DISCONNECTED_PLATFORM


def connected_provider_for_tenant(
    db: Session,
    tenant_id: int,
    store_settings: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Enabled Integration row first, then non-empty credentials. No platform_type."""
    models = _models()
    Integration = models.Integration
    rows = (
        db.query(Integration)
        .filter(
            Integration.tenant_id == int(tenant_id),
            Integration.enabled == True,  # noqa: E712
        )
        .order_by(Integration.id.asc())
        .all()
    )
    for row in rows:
        provider = str(getattr(row, "provider", None) or "").strip().lower()
        if provider in CONNECTED_PROVIDERS:
            return provider

    store = dict(store_settings or {})
    if str(store.get("salla_access_token") or "").strip():
        return "salla"
    if str(store.get("zid_client_id") or "").strip():
        return "zid"
    if str(store.get("shopify_access_token") or "").strip():
        return "shopify"
    return None


def apply_tenant_settings_hygiene(db: Session, settings: Any) -> Dict[str, Any]:
    """Normalize one TenantSettings row in place. Idempotent. Does not commit."""
    tenant_id = int(getattr(settings, "tenant_id", 0) or 0)
    ai = dict(getattr(settings, "ai_settings", None) or {})
    store = dict(getattr(settings, "store_settings", None) or {})

    ai_clean, ai_changed = strip_obsolete_ai_keys(ai)
    connected = connected_provider_for_tenant(db, tenant_id, store_settings=store)
    platform_before = str(store.get("platform_type") or "")
    platform_after = canonical_platform_type(store, connected)
    store_changed = platform_before != platform_after

    report = {
        "tenant_id": tenant_id,
        "changed": False,
        "persona_allowlist_removed": ai_changed,
        "platform_before": platform_before,
        "platform_after": platform_after,
        "connected_provider": connected,
    }
    if not ai_changed and not store_changed:
        return report

    if ai_changed:
        settings.ai_settings = ai_clean
        flag_modified(settings, "ai_settings")
    if store_changed:
        store["platform_type"] = platform_after
        settings.store_settings = store
        flag_modified(settings, "store_settings")
    settings.updated_at = datetime.now(timezone.utc)
    report["changed"] = True
    logger.info(
        "[tenant_config_hygiene] tenant=%s persona_allowlist_removed=%s "
        "platform %s->%s connected=%s",
        tenant_id,
        ai_changed,
        platform_before or "<empty>",
        platform_after,
        connected or "none",
    )
    return report


def normalize_all_tenant_settings(db: Session) -> Dict[str, Any]:
    """Walk every tenant_settings row. Idempotent. Does not commit."""
    models = _models()
    TenantSettings = models.TenantSettings
    rows = db.query(TenantSettings).all()
    reports: List[Dict[str, Any]] = []
    for settings in rows:
        reports.append(apply_tenant_settings_hygiene(db, settings))
    changed = [r for r in reports if r.get("changed")]
    return {
        "scanned": len(reports),
        "changed": len(changed),
        "persona_allowlist_removed": sum(
            1 for r in reports if r.get("persona_allowlist_removed")
        ),
        "platform_type_updated": sum(
            1 for r in reports if r.get("platform_before") != r.get("platform_after")
        ),
        "reports": reports,
    }


def _models():
    try:
        import models as m  # noqa: PLC0415

        if hasattr(m, "TenantSettings") and hasattr(m, "Integration"):
            return m
    except ImportError:
        pass
    import database.models as m  # noqa: PLC0415

    return m
