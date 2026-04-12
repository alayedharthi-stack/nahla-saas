"""
services/salla_guard.py
────────────────────────
Central guard layer for Salla integrations.

Enforces the rule: **one active binding per Salla store_id** across the
entire system.  Every code path that creates, updates, or queries a Salla
integration MUST go through these helpers.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Optional

from sqlalchemy.orm import Session

_THIS = os.path.dirname(os.path.abspath(__file__))
_DB   = os.path.abspath(os.path.join(_THIS, "../../database"))
for _p in (_THIS, _DB):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from models import Integration  # noqa: E402

logger = logging.getLogger("nahla.salla_guard")

# ── Integration health definitions ────────────────────────────────────────────

def has_valid_tokens(integration: Optional[Integration]) -> bool:
    """True only when both access-token and refresh-token are present."""
    if not integration:
        return False
    cfg = integration.config or {}
    return bool(cfg.get("api_key")) and bool(cfg.get("refresh_token"))


def is_oauth_completed(integration: Optional[Integration]) -> bool:
    """True when the integration went through a full OAuth exchange."""
    if not integration:
        return False
    cfg = integration.config or {}
    return bool(cfg.get("api_key") and cfg.get("refresh_token") and cfg.get("connected_at"))


def is_active_binding(integration: Optional[Integration]) -> bool:
    """An integration is truly *active* only when enabled AND has valid tokens."""
    if not integration:
        return False
    return bool(integration.enabled) and has_valid_tokens(integration)


# ── Ownership claim ──────────────────────────────────────────────────────────

def claim_store_for_tenant(
    db: Session,
    *,
    store_id: str,
    tenant_id: int,
    new_config: dict,
) -> Integration:
    """Atomically make *tenant_id* the sole active owner of *store_id*.

    1. Disables every OTHER enabled integration for the same ``store_id``.
    2. Creates or updates the integration for *tenant_id*.
    3. Returns the newly-active integration row (uncommitted — caller commits).
    """
    if not store_id:
        raise ValueError("store_id is required to claim a store binding")

    # ── Revoke stale bindings ─────────────────────────────────────────────
    stale_rows = (
        db.query(Integration)
        .filter(
            Integration.provider == "salla",
            Integration.tenant_id != tenant_id,
            Integration.config["store_id"].astext == str(store_id),
        )
        .all()
    )
    for s in stale_rows:
        s.enabled = False
        stale_cfg = dict(s.config or {})
        stale_cfg.pop("api_key", None)
        stale_cfg.pop("refresh_token", None)
        stale_cfg["revoked_reason"] = f"re-authed under tenant {tenant_id}"
        s.config = stale_cfg
        logger.warning(
            "[SallaGuard] REVOKED integration id=%s | old_tenant=%s → new_tenant=%s | store_id=%s",
            s.id, s.tenant_id, tenant_id, store_id,
        )

    # ── Upsert the winning integration ────────────────────────────────────
    integration = (
        db.query(Integration)
        .filter(Integration.tenant_id == tenant_id, Integration.provider == "salla")
        .first()
    )
    if integration:
        integration.config = new_config
        integration.enabled = True
        logger.info(
            "[SallaGuard] UPDATED integration id=%s | tenant=%s store_id=%s",
            integration.id, tenant_id, store_id,
        )
    else:
        integration = Integration(
            tenant_id=tenant_id,
            provider="salla",
            config=new_config,
            enabled=True,
        )
        db.add(integration)
        db.flush()
        logger.info(
            "[SallaGuard] CREATED integration id=%s | tenant=%s store_id=%s",
            integration.id, tenant_id, store_id,
        )

    return integration


# ── Pre-sync validation ──────────────────────────────────────────────────────

def validate_before_sync(db: Session, tenant_id: int) -> tuple[bool, str]:
    """Check whether a sync is safe to run.

    Returns ``(ok, message)``.  When *ok* is False, the sync MUST NOT proceed.
    """
    integration = (
        db.query(Integration)
        .filter(
            Integration.tenant_id == tenant_id,
            Integration.provider == "salla",
        )
        .first()
    )

    if not integration:
        return False, "لا يوجد ربط سلة لهذا التاجر."

    if not integration.enabled:
        return False, "ربط سلة معطّل. أعد الربط عبر OAuth."

    if not has_valid_tokens(integration):
        logger.warning(
            "[SallaGuard] SYNC_BLOCKED | tenant=%s — integration exists but tokens are missing/empty",
            tenant_id,
        )
        return False, "توكن سلة مفقود أو غير مكتمل. أعد ربط المتجر عبر OAuth."

    store_id = (integration.config or {}).get("store_id", "")
    if store_id:
        duplicate = (
            db.query(Integration)
            .filter(
                Integration.provider == "salla",
                Integration.enabled == True,  # noqa: E712
                Integration.tenant_id != tenant_id,
                Integration.config["store_id"].astext == str(store_id),
            )
            .first()
        )
        if duplicate:
            logger.error(
                "[SallaGuard] DUPLICATE_BINDING | store_id=%s active on tenant=%s AND tenant=%s — blocking sync for tenant=%s",
                store_id, tenant_id, duplicate.tenant_id, tenant_id,
            )
            return False, "هذا المتجر مربوط بحساب آخر. تواصل مع الدعم."

    return True, "OK"
