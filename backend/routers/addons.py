"""
routers/addons.py
─────────────────────────────────────────────────────────────────────
Merchant Addons — pluggable feature modules for merchant stores.

Endpoints (authenticated):
  GET  /merchant/addons                     — list all addons for tenant
  POST /merchant/addons/{key}/toggle        — enable / disable an addon
  PUT  /merchant/addons/{key}/settings      — update addon settings

Endpoints (public — no auth, loaded by external stores):
  GET  /merchant/addons/widget/{tenant_id}/embed.js
       → serves the WhatsApp widget JS if enabled, empty stub if disabled
         Merchant adds ONE <script> tag; Nahla controls enable/disable.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.database import get_db
from core.tenant import resolve_tenant_id

logger = logging.getLogger("nahla.addons")

router = APIRouter()

# ── Addon Registry ────────────────────────────────────────────────────────────
# Adding a new addon = add one entry here.  Frontend picks it up automatically.

ADDON_REGISTRY: Dict[str, Dict[str, Any]] = {
    "widget": {
        "name_ar":        "ويدجت نحلة",
        "description_ar": "زر واتساب عائم في متجرك يسهّل تواصل الزوار مع نحلة",
        "badge":          "free",        # free | paid | coming_soon
        "has_settings":   True,
        "default_settings": {
            "phone":            "",
            "message":          "السلام عليكم، أبغى الاستفسار",
            "logo_url":         "",
            "position":         "left",
            "scroll_threshold": 250,
        },
    },
    "discount_popup": {
        "name_ar":        "نافذة خصم",
        "description_ar": "نافذة تظهر للزائر بعرض خاص لتحفيزه على الشراء",
        "badge":          "free",
        "has_settings":   True,
        "default_settings": {
            "title":          "عرض خاص لك!",
            "body_text":      "احصل على خصم حصري على طلبك الآن",
            "discount_type":  "percentage",   # percentage | fixed
            "discount_value": 10,
            "delay_seconds":  5,
            "show_once":      True,
        },
    },
    "first_order_coupon": {
        "name_ar":        "كوبون أول طلب",
        "description_ar": "كوبون خصم تلقائي يُقدَّم للعملاء الجدد عند أول طلب",
        "badge":          "free",
        "has_settings":   True,
        "default_settings": {
            "coupon_code":       "",
            "discount_type":     "percentage",
            "discount_value":    10,
            "min_order_value":   0,
            "validity_days":     30,
            "new_customers_only": True,
        },
    },
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_or_create_addon(db: Session, tenant_id: int, addon_key: str):
    """Return existing MerchantAddon row, creating a default if missing."""
    from models import MerchantAddon  # noqa: PLC0415
    row = (
        db.query(MerchantAddon)
        .filter(MerchantAddon.tenant_id == tenant_id, MerchantAddon.addon_key == addon_key)
        .first()
    )
    if row is None:
        meta = ADDON_REGISTRY.get(addon_key, {})
        row = MerchantAddon(
            tenant_id=tenant_id,
            addon_key=addon_key,
            is_enabled=False,
            settings_json=dict(meta.get("default_settings", {})),
        )
        db.add(row)
        db.flush()
    return row


def _serialize_addon(row, meta: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a MerchantAddon row + registry meta into an API response dict."""
    settings = dict(row.settings_json or {})
    defaults = dict(meta.get("default_settings", {}))
    merged   = {**defaults, **settings}   # settings override defaults
    return {
        "key":          row.addon_key,
        "name":         meta.get("name_ar", row.addon_key),
        "description":  meta.get("description_ar", ""),
        "badge":        meta.get("badge", "free"),
        "has_settings": meta.get("has_settings", False),
        "is_enabled":   row.is_enabled,
        "settings":     merged,
    }


# ── Schemas ───────────────────────────────────────────────────────────────────

class ToggleBody(BaseModel):
    enabled: bool


class AddonSettingsBody(BaseModel):
    settings: Dict[str, Any]


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/merchant/addons")
async def list_addons(request: Request, db: Session = Depends(get_db)):
    """Return all known addons enriched with this tenant's state."""
    tenant_id = resolve_tenant_id(request)
    from models import MerchantAddon  # noqa: PLC0415

    # Load all existing rows for this tenant in one query
    rows = {
        r.addon_key: r
        for r in db.query(MerchantAddon)
                   .filter(MerchantAddon.tenant_id == tenant_id)
                   .all()
    }

    result = []
    for key, meta in ADDON_REGISTRY.items():
        if key not in rows:
            # Create default row lazily
            rows[key] = _get_or_create_addon(db, tenant_id, key)

        result.append(_serialize_addon(rows[key], meta))

    db.commit()
    return {"addons": result}


@router.post("/merchant/addons/{addon_key}/toggle")
async def toggle_addon(
    addon_key: str,
    body: ToggleBody,
    request: Request,
    db: Session = Depends(get_db),
):
    """Enable or disable an addon for the current tenant."""
    if addon_key not in ADDON_REGISTRY:
        raise HTTPException(status_code=404, detail=f"addon '{addon_key}' not found")

    tenant_id = resolve_tenant_id(request)
    row = _get_or_create_addon(db, tenant_id, addon_key)
    row.is_enabled = body.enabled
    row.updated_at = datetime.now(timezone.utc)
    db.commit()

    logger.info(
        "[addons/toggle] tenant=%s addon=%s enabled=%s",
        tenant_id, addon_key, body.enabled,
    )
    return _serialize_addon(row, ADDON_REGISTRY[addon_key])


@router.put("/merchant/addons/{addon_key}/settings")
async def update_addon_settings(
    addon_key: str,
    body: AddonSettingsBody,
    request: Request,
    db: Session = Depends(get_db),
):
    """Update settings for a specific addon."""
    if addon_key not in ADDON_REGISTRY:
        raise HTTPException(status_code=404, detail=f"addon '{addon_key}' not found")

    tenant_id = resolve_tenant_id(request)
    row = _get_or_create_addon(db, tenant_id, addon_key)

    # Merge incoming settings over existing (partial update)
    current = dict(row.settings_json or {})
    current.update(body.settings)
    row.settings_json = current
    row.updated_at    = datetime.now(timezone.utc)
    db.commit()

    logger.info("[addons/settings] tenant=%s addon=%s", tenant_id, addon_key)
    return _serialize_addon(row, ADDON_REGISTRY[addon_key])


# ── Public embed endpoint (no auth) ──────────────────────────────────────────
# Merchants add ONE <script> tag to their Salla/Zid store:
#   <script src="https://api.nahlah.ai/merchant/addons/widget/{tenant_id}/embed.js"></script>
# Nahla controls enable/disable from the dashboard — no Salla changes needed.

_NAHLA_CDN_LOGO = (
    "https://cdn.salla.sa/XVEDq/"
    "b1ec4359-6895-49dc-80e7-06fd33b75df8-1000x666.66666666667-"
    "xMM28RbT68xVWoSgEtzBgpW1w4cDN7sEQAhQmLwD.jpg"
)

_JS_HEADERS = {
    "Content-Type":                "application/javascript; charset=utf-8",
    "Cache-Control":               "no-cache, no-store, must-revalidate",
    "Access-Control-Allow-Origin": "*",
    "X-Content-Type-Options":      "nosniff",
}

_STUB = "/* Nahla widget: not enabled */"


def _build_widget_js(s: dict) -> str:
    phone  = str(s.get("phone",            "")).strip()
    msg    = str(s.get("message",          "السلام عليكم، أبغى الاستفسار")).strip()
    logo   = str(s.get("logo_url",         "")).strip() or _NAHLA_CDN_LOGO
    pos    = "right" if s.get("position") == "right" else "left"
    thresh = int(s.get("scroll_threshold", 250))
    pos_x    = f"{pos}:40px"
    pos_x_mo = f"{pos}:20px"

    return f"""/* Nahla WhatsApp Widget — nahlah.ai */
(function(){{
  var WA_NUMBER='{phone}',WA_MESSAGE='{msg}',LOGO='{logo}';
  var btn=document.createElement('a');
  btn.href='https://wa.me/'+WA_NUMBER+'?text='+encodeURIComponent(WA_MESSAGE);
  btn.target='_blank';btn.rel='noopener noreferrer';btn.id='nahla-whatsapp';
  btn.innerHTML='<img src="'+LOGO+'" class="nahla-bee" alt="نحلة">'
    +'<div class="circle"><span class="orbit o1"></span><span class="orbit o2"></span>'
    +'<span class="orbit o3"></span><span class="orbit o4"></span>'
    +'<img src="https://upload.wikimedia.org/wikipedia/commons/6/6b/WhatsApp.svg" class="icon" alt="واتساب"></div>';
  document.body.appendChild(btn);
  function checkShow(){{if(window.scrollY>{thresh}||document.body.scrollHeight<=window.innerHeight+300)btn.classList.add('show');}}
  window.addEventListener('scroll',checkShow,{{passive:true}});checkShow();
  var s=document.createElement('style');
  s.innerHTML=[
    '#nahla-whatsapp{{position:fixed;bottom:55px;{pos_x};z-index:9999;opacity:0;transform:scale(.8);transition:opacity .4s,transform .4s;display:flex;flex-direction:column;align-items:center;gap:6px;text-decoration:none;}}',
    '#nahla-whatsapp.show{{opacity:1;transform:scale(1);}}',
    '.nahla-bee{{width:110px;height:110px;object-fit:contain;animation:bee-float 3s ease-in-out infinite;}}',
    '@keyframes bee-float{{0%,100%{{transform:translateY(0) rotate(-4deg);}}50%{{transform:translateY(-7px) rotate(4deg);}}}}',
    '.circle{{position:relative;width:65px;height:65px;background:#25D366;border-radius:50%;display:flex;align-items:center;justify-content:center;box-shadow:0 4px 18px rgba(37,211,102,.45);}}',
    '.icon{{width:30px;height:30px;z-index:2;position:relative;}}',
    '.orbit{{position:absolute;inset:0;border-radius:50%;border:2.5px solid rgba(37,211,102,.65);animation:apple-wave 2.8s cubic-bezier(.4,0,.2,1) infinite;}}',
    '.o1{{animation-delay:0s;}}.o2{{animation-delay:.7s;}}.o3{{animation-delay:1.4s;}}.o4{{animation-delay:2.1s;}}',
    '@keyframes apple-wave{{0%{{transform:scale(.92);opacity:.85;}}30%{{transform:scale(1.25);opacity:.55;}}60%{{transform:scale(1.65);opacity:.22;}}85%{{transform:scale(1.95);opacity:.05;}}100%{{transform:scale(2.05);opacity:0;}}}}',
    '@media(max-width:600px){{.circle{{width:58px;height:58px;}}.icon{{width:26px;height:26px;}}.nahla-bee{{width:90px;height:90px;}}#nahla-whatsapp{{bottom:50px;{pos_x_mo};}}}}'
  ].join('');
  document.head.appendChild(s);
}})();"""


@router.post("/merchant/addons/widget/salla-install")
async def salla_auto_install_widget(request: Request, db: Session = Depends(get_db)):
    """
    Try to automatically inject the widget <script> tag into the merchant's
    Salla store via the Salla Admin API.

    Response:
      { success: true,  method: "api" }          — script injected automatically
      { success: false, reason: "...",            — API not available or no connection
        script_tag: "...", salla_admin_url: "..." }
    """
    import httpx as _httpx  # noqa: PLC0415

    tenant_id = resolve_tenant_id(request)
    from models import Integration, MerchantAddon  # noqa: PLC0415

    # ── 1. Get the addon row (must exist and be enabled) ─────────────────────
    addon_row = (
        db.query(MerchantAddon)
        .filter(MerchantAddon.tenant_id == tenant_id, MerchantAddon.addon_key == "widget")
        .first()
    )

    api_base = os.environ.get("BACKEND_URL", "https://api.nahlah.ai")
    embed_url  = f"{api_base}/merchant/addons/widget/{tenant_id}/embed.js"
    script_tag = f'<script src="{embed_url}"></script>'
    salla_admin_url = "https://s.salla.sa/settings/scripts"

    # ── 2. Locate Salla integration ───────────────────────────────────────────
    integration = (
        db.query(Integration)
        .filter(Integration.tenant_id == tenant_id, Integration.provider == "salla")
        .first()
    )
    if not integration:
        return {
            "success": False, "reason": "no_salla_connection",
            "script_tag": script_tag, "salla_admin_url": salla_admin_url,
            "message": "لم يتم ربط متجر سلة — أضف الكود يدوياً",
        }

    salla_token = (integration.config or {}).get("token") or (integration.config or {}).get("access_token")
    if not salla_token:
        return {
            "success": False, "reason": "no_token",
            "script_tag": script_tag, "salla_admin_url": salla_admin_url,
            "message": "رمز سلة غير موجود — أضف الكود يدوياً",
        }

    # ── 3. Try Salla Admin v2 custom-scripts endpoint ─────────────────────────
    try:
        async with _httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://api.salla.dev/admin/v2/store/scripts",
                headers={
                    "Authorization": f"Bearer {salla_token}",
                    "Content-Type":  "application/json",
                    "Accept":        "application/json",
                },
                json={"src": embed_url, "event": "onload"},
            )
            if resp.status_code in (200, 201):
                body = resp.json()
                logger.info("[addons/salla-install] API success tenant=%s", tenant_id)
                return {
                    "success": True, "method": "api",
                    "message": "تم تثبيت الويدجت تلقائياً في متجرك ✓",
                    "script_id": body.get("data", {}).get("id"),
                }
            logger.info(
                "[addons/salla-install] Salla scripts API returned %s — falling back",
                resp.status_code,
            )
    except Exception as exc:
        logger.info("[addons/salla-install] Salla API call failed: %s — falling back", exc)

    # ── 4. Fallback: Salla doesn't expose a public scripts REST API yet ───────
    # Return everything the frontend needs for the Quick-Install wizard.
    store_id = (integration.config or {}).get("store_id", "")
    return {
        "success":        False,
        "reason":         "api_not_available",
        "script_tag":     script_tag,
        "embed_url":      embed_url,
        "salla_store_id": store_id,
        # Deep-link to Salla admin custom scripts page (opened in new tab)
        "salla_admin_url": "https://s.salla.sa/settings/scripts",
        "message": (
            "الإضافة التلقائية غير متاحة في سلة حالياً — "
            "أضف الكود أدناه في إعدادات متجرك (خطوة واحدة فقط)"
        ),
    }


@router.get(
    "/merchant/addons/widget/{tenant_id}/embed.js",
    include_in_schema=False,   # hide from OpenAPI — public unauthenticated route
)
async def serve_widget_embed(tenant_id: int, db: Session = Depends(get_db)):
    """
    Public endpoint — no JWT required.
    Returns the widget JS if the tenant has the widget addon enabled,
    otherwise returns a tiny stub comment.

    Usage in Salla / Zid / any store:
      <script src="https://api.nahlah.ai/merchant/addons/widget/{tenant_id}/embed.js"></script>
    """
    from models import MerchantAddon  # noqa: PLC0415

    row = (
        db.query(MerchantAddon)
        .filter(
            MerchantAddon.tenant_id == tenant_id,
            MerchantAddon.addon_key == "widget",
        )
        .first()
    )

    if not row or not row.is_enabled:
        return Response(content=_STUB, headers=_JS_HEADERS)

    settings = dict(row.settings_json or {})
    phone    = str(settings.get("phone", "")).strip()

    if not phone:
        return Response(
            content="/* Nahla widget: phone number not configured */",
            headers=_JS_HEADERS,
        )

    js = _build_widget_js(settings)
    logger.info("[addons/embed] tenant=%s serving widget JS", tenant_id)
    return Response(content=js, headers=_JS_HEADERS)


# ── Universal Salla snippet (for Partner Portal configuration) ────────────────
# Configure in Salla Partner Portal → App → Snippets:
#   URL: https://api.nahlah.ai/merchant/addons/widget/salla-auto.js
#
# This script auto-detects the store from window.salla, resolves the tenant,
# and conditionally loads the widget.  Once configured, every store that
# installs the Nahla app gets the widget capability with zero merchant effort.

@router.get("/merchant/addons/widget/salla-auto.js", include_in_schema=False)
async def serve_salla_auto_snippet(db: Session = Depends(get_db)):
    """
    Universal Salla snippet — loaded on every Salla store that has the Nahla app.
    Resolves tenant by Salla store ID (via Integration table) then delegates
    to the per-tenant embed endpoint.
    """
    api_base = os.environ.get("BACKEND_URL", "https://api.nahlah.ai")
    js = f"""/* Nahla Universal Salla Snippet */
(function(){{
  var storeId = (
    (window.salla && window.salla.store && window.salla.store.id) ||
    (window.salla_config && window.salla_config.store && window.salla_config.store.id)
  );
  if (!storeId) return;
  var s = document.createElement('script');
  s.src = '{api_base}/merchant/addons/widget/by-salla/' + storeId + '/embed.js';
  s.async = true;
  document.head.appendChild(s);
}})();"""
    return Response(content=js, headers=_JS_HEADERS)


@router.get("/merchant/addons/widget/by-salla/{salla_store_id}/embed.js", include_in_schema=False)
async def serve_widget_by_salla_store(salla_store_id: str, db: Session = Depends(get_db)):
    """
    Resolve tenant from Salla store ID and serve the widget embed JS.
    Used by the universal salla-auto.js snippet above.
    """
    from models import Integration, MerchantAddon  # noqa: PLC0415

    # Find the tenant that owns this Salla store
    integration = (
        db.query(Integration)
        .filter(
            Integration.provider == "salla",
        )
        .all()
    )
    tenant_id = None
    for integ in integration:
        cfg = integ.config or {}
        if str(cfg.get("store_id", "")) == str(salla_store_id):
            tenant_id = integ.tenant_id
            break

    if tenant_id is None:
        return Response(content="/* Nahla: store not found */", headers=_JS_HEADERS)

    row = (
        db.query(MerchantAddon)
        .filter(MerchantAddon.tenant_id == tenant_id, MerchantAddon.addon_key == "widget")
        .first()
    )
    if not row or not row.is_enabled:
        return Response(content=_STUB, headers=_JS_HEADERS)

    settings = dict(row.settings_json or {})
    if not str(settings.get("phone", "")).strip():
        return Response(content="/* Nahla widget: phone not configured */", headers=_JS_HEADERS)

    logger.info("[addons/by-salla] store=%s tenant=%s serving widget", salla_store_id, tenant_id)
    return Response(content=_build_widget_js(settings), headers=_JS_HEADERS)
