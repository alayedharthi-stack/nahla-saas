"""
routers/integrations.py
───────────────────────
Integrations status endpoints — single source of truth for all integration states.

Routes
  GET  /integrations/whatsapp/status  — unified WhatsApp connection status
  GET  /integrations/debug            — full tenant / user / store / WA debug view
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from core.auth import get_current_user
from core.database import get_db
from core.tenant import resolve_tenant_id
from models import Integration, Tenant, User, WhatsAppConnection

logger = logging.getLogger("nahla.integrations")
router = APIRouter(prefix="/integrations", tags=["Integrations"])


# ── helpers ───────────────────────────────────────────────────────────────────

def _wa_status_payload(conn: WhatsAppConnection | None) -> dict:
    """Return the unified WhatsApp status dict (mirrors /whatsapp/status)."""
    if not conn or conn.status == "not_connected":
        return {"connected": False, "status": "not_connected"}
    meta = dict(conn.extra_metadata or {})
    return {
        "connected":             bool(conn.status == "connected" and conn.sending_enabled),
        "status":                conn.status,
        "phone_number":          conn.phone_number,
        "display_phone_number":  conn.phone_number,
        "business_display_name": conn.business_display_name,
        "phone_number_id":       conn.phone_number_id,
        "waba_id":               conn.whatsapp_business_account_id,
        "verification_status":   (
            meta.get("meta_code_verification_status")
            or ("verified" if conn.status == "connected" else conn.status)
        ),
        "name_status":           meta.get("meta_name_status"),
        "meta_phone_status":     meta.get("meta_phone_status"),
        "message":               meta.get("embedded_status_message"),
        "sending_enabled":       bool(conn.sending_enabled),
        "connected_at":          conn.connected_at.isoformat() if conn.connected_at else None,
    }


# ── routes ────────────────────────────────────────────────────────────────────

@router.get("/whatsapp/status")
async def integrations_whatsapp_status(
    request: Request,
    db: Session = Depends(get_db),
    _user: Dict[str, Any] = Depends(get_current_user),   # enforce JWT
):
    """
    Unified WhatsApp connection status — alias of GET /whatsapp/status.
    Both pages (WhatsApp Connect + Integrations) must use the same source of truth.
    """
    tenant_id = resolve_tenant_id(request)
    conn = db.query(WhatsAppConnection).filter_by(tenant_id=tenant_id).first()
    logger.info("[integrations/whatsapp/status] tenant=%s status=%s",
                tenant_id, conn.status if conn else "none")
    return _wa_status_payload(conn)


@router.get("/debug")
async def integrations_debug(
    request: Request,
    db: Session = Depends(get_db),
    user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Debug endpoint — returns full tenant / user / store / WhatsApp mapping.
    Use this to verify that the correct tenant is resolved for the logged-in user.

    Response fields
    ---------------
    jwt_claims      — what the JWT token contains (sub, role, tenant_id)
    resolved_tenant — tenant resolved from JWT / header
    user_in_db      — user record from the `users` table
    tenant_in_db    — tenant record from the `tenants` table
    whatsapp        — WhatsApp connection for this tenant
    salla           — Salla integration for this tenant (if any)
    zid             — Zid integration for this tenant (if any)
    """
    resolved_tenant_id = resolve_tenant_id(request)

    # JWT claims
    jwt_info = {
        "sub":       user.get("sub"),
        "role":      user.get("role"),
        "tenant_id": user.get("tenant_id"),
    }

    # User from DB
    db_user = db.query(User).filter_by(email=user.get("sub")).first()
    user_info: dict = {}
    if db_user:
        user_info = {
            "id":        db_user.id,
            "email":     db_user.email,
            "role":      db_user.role,
            "tenant_id": db_user.tenant_id,
            "is_active": db_user.is_active,
            "has_password": bool(getattr(db_user, "password_hash", None)),
        }

    # Tenant from DB
    tenant = db.query(Tenant).filter_by(id=resolved_tenant_id).first()
    tenant_info: dict = {}
    if tenant:
        tenant_info = {
            "id":   tenant.id,
            "name": tenant.name,
        }

    # WhatsApp connection
    wa_conn = db.query(WhatsAppConnection).filter_by(tenant_id=resolved_tenant_id).first()
    wa_info = _wa_status_payload(wa_conn)

    # Salla integration
    salla_int = db.query(Integration).filter_by(
        tenant_id=resolved_tenant_id, provider="salla"
    ).first()
    salla_info: dict = {}
    if salla_int and salla_int.config:
        cfg = salla_int.config
        salla_info = {
            "store_id":   cfg.get("store_id"),
            "store_name": cfg.get("store_name"),
            "connected":  True,
        }

    # Zid integration
    zid_int = db.query(Integration).filter_by(
        tenant_id=resolved_tenant_id, provider="zid"
    ).first()
    zid_info: dict = {}
    if zid_int and zid_int.config:
        cfg = zid_int.config
        zid_info = {
            "store_id":   cfg.get("store_id") or cfg.get("manager_id"),
            "store_name": cfg.get("store_name"),
            "connected":  True,
        }

    logger.info(
        "[integrations/debug] jwt_tenant=%s resolved_tenant=%s db_user_tenant=%s wa_status=%s",
        jwt_info.get("tenant_id"),
        resolved_tenant_id,
        user_info.get("tenant_id"),
        wa_info.get("status"),
    )

    return {
        "jwt_claims":      jwt_info,
        "resolved_tenant": resolved_tenant_id,
        "user_in_db":      user_info,
        "tenant_in_db":    tenant_info,
        "whatsapp":        wa_info,
        "salla":           salla_info,
        "zid":             zid_info,
        # Flag any mismatch
        "tenant_mismatch": (
            jwt_info.get("tenant_id") != resolved_tenant_id
            or (user_info.get("tenant_id") and user_info["tenant_id"] != resolved_tenant_id)
        ),
    }
