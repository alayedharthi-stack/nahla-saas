"""
routers/salla_app_settings.py
──────────────────────────────
Salla App Quick-Setup Settings integration.

Partner Portal fields → Nahla tenant settings mapping:

  Salla field key              Nahla setting group / key
  ─────────────────────────    ──────────────────────────────────────
  nahla_enabled                whatsapp.auto_reply_enabled
  whatsapp_number              whatsapp.phone_number (owner display)
  reply_tone                   ai.reply_tone  (friendly|formal|marketing)
  abandoned_cart_enabled       ai.abandoned_cart_enabled  (custom key)
  discount_percentage          ai.allowed_discount_levels
  autopilot_enabled            ai.autopilot_enabled  (custom key)

Endpoints:
  GET  /salla/app-settings              — return current Quick-Setup values for a store
  POST /salla/app-settings/webhook      — receive app.settings webhook from Salla (no auth)
  PUT  /salla/app-settings              — update Quick-Setup values (authenticated)
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.database import get_db
from core.tenant import (
    DEFAULT_AI,
    DEFAULT_WHATSAPP,
    get_or_create_settings,
    merge_ai_defaults,
    merge_defaults,
    resolve_tenant_id,
)

logger = logging.getLogger("nahla.salla_app_settings")
router = APIRouter(tags=["salla-app-settings"])

# Salla webhook secret for signature verification (same key used for all webhooks)
_SALLA_WEBHOOK_SECRET = os.getenv("SALLA_WEBHOOK_SECRET", "")

# ── Tone mapping: Salla label → internal key ──────────────────────────────────
_TONE_MAP: Dict[str, str] = {
    "ودي":      "friendly",
    "رسمي":     "formal",
    "تسويقي":   "marketing",
    "friendly":  "friendly",
    "formal":    "formal",
    "marketing": "marketing",
}


# ── Pydantic schema ────────────────────────────────────────────────────────────

class SallaQuickSetupIn(BaseModel):
    """
    Mirrors the fields defined in Salla Partner Portal App Settings.
    All fields are optional — partial updates are supported.
    """
    nahla_enabled:           Optional[bool]  = Field(None, description="تفعيل نحلة الذكية")
    whatsapp_number:         Optional[str]   = Field(None, description="رقم واتساب المتجر")
    reply_tone:              Optional[str]   = Field(None, description="أسلوب الرد")
    abandoned_cart_enabled:  Optional[bool]  = Field(None, description="تفعيل استرجاع السلة المتروكة")
    discount_percentage:     Optional[float] = Field(None, description="نسبة الخصم التلقائي", ge=0, le=100)
    autopilot_enabled:       Optional[bool]  = Field(None, description="تفعيل الطيار الآلي")


# ── Core mapping function ─────────────────────────────────────────────────────

def apply_quick_setup(settings_obj, payload: SallaQuickSetupIn, db: Session) -> dict:
    """
    Merge Quick-Setup values into the tenant settings row.
    Returns a summary dict of what changed.
    """
    changed: dict[str, Any] = {}

    # ── WhatsApp settings ─────────────────────────────────────────────────────
    wa = merge_defaults(settings_obj.whatsapp_settings, DEFAULT_WHATSAPP)

    if payload.nahla_enabled is not None:
        wa["auto_reply_enabled"] = payload.nahla_enabled
        changed["auto_reply_enabled"] = payload.nahla_enabled

    if payload.whatsapp_number is not None:
        number = payload.whatsapp_number.strip().lstrip("+")
        wa["owner_whatsapp_number"] = number
        changed["whatsapp_number"] = number

    settings_obj.whatsapp_settings = wa

    # ── AI settings ───────────────────────────────────────────────────────────
    ai = merge_ai_defaults(settings_obj.ai_settings)

    if payload.reply_tone is not None:
        tone = _TONE_MAP.get(payload.reply_tone.strip(), payload.reply_tone.strip())
        ai["reply_tone"] = tone
        changed["reply_tone"] = tone

    if payload.abandoned_cart_enabled is not None:
        ai["abandoned_cart_enabled"] = payload.abandoned_cart_enabled
        changed["abandoned_cart_enabled"] = payload.abandoned_cart_enabled

    if payload.discount_percentage is not None:
        ai["allowed_discount_levels"] = str(int(payload.discount_percentage))
        changed["discount_percentage"] = payload.discount_percentage

    if payload.autopilot_enabled is not None:
        ai["autopilot_enabled"] = payload.autopilot_enabled
        changed["autopilot_enabled"] = payload.autopilot_enabled

    settings_obj.ai_settings = ai
    settings_obj.updated_at  = datetime.now(timezone.utc)
    db.add(settings_obj)
    return changed


def _read_quick_setup(settings_obj) -> dict:
    """Extract Quick-Setup view from stored tenant settings."""
    wa = merge_defaults(settings_obj.whatsapp_settings, DEFAULT_WHATSAPP)
    ai = merge_ai_defaults(settings_obj.ai_settings)

    raw_tone = ai.get("reply_tone", "friendly")
    tone_ar  = {v: k for k, v in _TONE_MAP.items() if k in ("ودي", "رسمي", "تسويقي")}.get(raw_tone, raw_tone)

    return {
        "nahla_enabled":          wa.get("auto_reply_enabled", True),
        "whatsapp_number":        wa.get("owner_whatsapp_number", ""),
        "reply_tone":             tone_ar,
        "abandoned_cart_enabled": ai.get("abandoned_cart_enabled", False),
        "discount_percentage":    float(ai.get("allowed_discount_levels", "10") or 10),
        "autopilot_enabled":      ai.get("autopilot_enabled", True),
    }


# ── GET — current Quick-Setup values (authenticated) ─────────────────────────

@router.get("/salla/app-settings")
async def get_salla_app_settings(request: Request, db: Session = Depends(get_db)):
    """Return current Quick-Setup settings for the authenticated tenant."""
    tenant_id = resolve_tenant_id(request)
    s = get_or_create_settings(db, tenant_id)
    db.commit()
    return {
        "ok":       True,
        "settings": _read_quick_setup(s),
        "links": {
            "dashboard": "https://app.nahlah.ai/settings",
            "pricing":   "https://app.nahlah.ai/app/pricing",
        },
    }


# ── PUT — update Quick-Setup values (authenticated) ──────────────────────────

@router.put("/salla/app-settings")
async def update_salla_app_settings(
    body:    SallaQuickSetupIn,
    request: Request,
    db:      Session = Depends(get_db),
):
    """Update Quick-Setup settings for the authenticated tenant."""
    tenant_id = resolve_tenant_id(request)
    s = get_or_create_settings(db, tenant_id)
    changed = apply_quick_setup(s, body, db)
    db.commit()
    logger.info("[SallaAppSettings] PUT tenant=%s changed=%s", tenant_id, list(changed.keys()))
    return {
        "ok":      True,
        "changed": changed,
        "settings": _read_quick_setup(s),
    }


# ── POST — Salla webhook (no auth, HMAC-verified) ────────────────────────────

@router.post("/salla/app-settings/webhook")
async def salla_app_settings_webhook(request: Request, db: Session = Depends(get_db)):
    """
    Receive app.settings webhook from Salla Partner Portal.

    Salla sends this event whenever a merchant saves the App Settings form.
    Payload shape:
    {
      "event":    "app.settings",
      "merchant": "<store_id>",
      "data": {
        "settings": {
          "nahla_enabled":          true,
          "whatsapp_number":        "966XXXXXXXXX",
          "reply_tone":             "ودي",
          "abandoned_cart_enabled": false,
          "discount_percentage":    10,
          "autopilot_enabled":      true
        }
      }
    }
    """
    raw_body = await request.body()

    # ── Signature verification (optional but strongly recommended) ───────────
    if _SALLA_WEBHOOK_SECRET:
        sig = request.headers.get("x-salla-signature", "") or request.headers.get("signature", "")
        expected = hmac.new(
            _SALLA_WEBHOOK_SECRET.encode(),
            raw_body,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(sig, expected):
            logger.warning("[SallaAppSettings] Invalid webhook signature")
            raise HTTPException(status_code=401, detail="Invalid webhook signature")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event_type = payload.get("event", "")
    if event_type != "app.settings":
        # Tolerate other events landing on this endpoint — just ack
        return {"received": True, "event": event_type, "processed": False}

    store_id = str(payload.get("merchant") or payload.get("store_id") or "")
    data     = payload.get("data") or {}
    raw_cfg  = (data.get("settings") or data) if isinstance(data, dict) else {}

    if not store_id:
        logger.warning("[SallaAppSettings] Webhook missing merchant/store_id")
        return {"received": True, "processed": False, "reason": "missing_store_id"}

    # ── Resolve tenant ────────────────────────────────────────────────────────
    from routers.webhooks import _resolve_tenant_from_store  # noqa: PLC0415
    tenant_id = _resolve_tenant_from_store(db, store_id)
    if tenant_id is None:
        logger.warning("[SallaAppSettings] Unresolved store_id=%s — settings queued for retry", store_id)
        # Return 200 so Salla doesn't disable the webhook; the merchant will
        # complete OAuth and on next settings save the tenant will exist.
        return {"received": True, "processed": False, "reason": "tenant_not_yet_linked"}

    # ── Parse and apply ───────────────────────────────────────────────────────
    try:
        setup = SallaQuickSetupIn(
            nahla_enabled           = _bool(raw_cfg.get("nahla_enabled")),
            whatsapp_number         = _str(raw_cfg.get("whatsapp_number")),
            reply_tone              = _str(raw_cfg.get("reply_tone")),
            abandoned_cart_enabled  = _bool(raw_cfg.get("abandoned_cart_enabled")),
            discount_percentage     = _float(raw_cfg.get("discount_percentage")),
            autopilot_enabled       = _bool(raw_cfg.get("autopilot_enabled")),
        )
    except Exception as exc:
        logger.error("[SallaAppSettings] Failed to parse settings payload: %s", exc)
        raise HTTPException(status_code=422, detail=f"Invalid settings payload: {exc}")

    s = get_or_create_settings(db, tenant_id)
    changed = apply_quick_setup(s, setup, db)
    db.commit()

    logger.info(
        "[SallaAppSettings] Webhook applied | tenant=%s store=%s changed=%s",
        tenant_id, store_id, list(changed.keys()),
    )
    return {
        "received":  True,
        "processed": True,
        "tenant_id": tenant_id,
        "changed":   list(changed.keys()),
    }


# ── Type coercion helpers ─────────────────────────────────────────────────────

def _bool(v) -> Optional[bool]:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    return str(v).lower() in ("true", "1", "yes", "on")


def _str(v) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def _float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None
