"""
routers/widgets.py
──────────────────────────────────────────────────────────────────────────────
Conversion Widgets System — visual sales-boost tools displayed in merchant stores.

Authenticated endpoints (JWT required):
  GET  /merchant/widgets                          list all widgets + state
  POST /merchant/widgets/{key}/toggle             enable / disable
  PUT  /merchant/widgets/{key}/settings           update settings
  PUT  /merchant/widgets/{key}/rules              update display rules
  POST /merchant/widgets/salla-install            try Salla API injection

Public endpoints (no auth — served to external stores via <script> tag):
  GET  /merchant/widgets/{tenant_id}/nahla-widgets.js
       ↳ Full JS bundle with all enabled widgets injected server-side.
         Disabled = returns a 1-line stub (fast, no extra RTT).
  GET  /merchant/widgets/{tenant_id}/config.json
       ↳ JSON config for enabled widgets (used by advanced integrations).
  GET  /merchant/widgets/salla-auto.js
       ↳ Universal loader called by the published Salla Partner App Snippet;
         auto-detects store_id,
         maps to tenant, then loads the per-tenant bundle.
  GET  /merchant/widgets/salla/{salla_store_id}/nahla-widgets.js
       ↳ Salla store-ID-based entry point (used by salla-auto.js).

──────────────────────────────────────────────────────────────────────────────
Security & caching answers:

• Store identification in salla-auto.js:
    Reads window.salla?.store?.id (injected by Salla's Twilight SDK on every
    storefront page). Falls back to window.salla_config?.store?.id.
    The store ID is then appended to the nahla-widgets.js URL so the backend
    can resolve tenant_id from the Integration table.

• store_id → tenant_id mapping:
    SELECT tenant_id FROM integrations WHERE provider='salla'
    AND external_store_id = :salla_store_id;
    Legacy connections fall back to config->>'store_id'.
    No cross-tenant leakage is possible — each store_id is unique per Salla
    and only returns config for its linked tenant.

• Widget disabled:
    Returns /* Nahla: widget off */ (50-byte stub). The server still responds
    200 so the <script> tag doesn't fire browser console errors.

• Settings fetch failure:
    The bundle is self-contained — config is rendered server-side into the JS
    at request time. If the DB is unavailable, the server returns a 200 stub.
    No client-side fetch = no runtime failure on the store.

• Caching:
    Cache-Control: no-store
    Changes take effect on the next storefront page load.
    tenant_id is part of the URL so CDN cannot mix responses across tenants.

• Endpoint security:
    - Authenticated endpoints use resolve_tenant_id() (JWT/session required).
    - Public endpoints are parameterised by tenant_id in the URL path.
      They only expose is_enabled + display settings — never tokens, API keys,
      phone numbers are exposed but only to whoever has the tenant's script URL,
      which is the same as what the store owner embeds publicly anyway.
    - No endpoint returns another tenant's data.

• Production domain:
    Configure in Salla Partner Portal → App Snippets:
    URL: https://api.nahlah.ai/merchant/widgets/salla-auto.js
"""
from __future__ import annotations

import logging
import os
import re
import ipaddress
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.database import get_db
from core.tenant import resolve_tenant_id

logger = logging.getLogger("nahla.widgets")

router = APIRouter()

_API_BASE = os.environ.get("BACKEND_URL", "https://api.nahlah.ai")

# ── Default display rules per trigger type ────────────────────────────────────
_DEFAULT_RULES: Dict[str, Any] = {
    "trigger":            "entry",   # entry | scroll | exit_intent | click_tab
    "show_after_seconds": 0,
    "show_on_pages":      ["all"],   # all | home | product | cart | checkout
    "show_once_per_user": True,
    "scroll_percent":     50,        # used when trigger=scroll
}

# ── Widget Registry ────────────────────────────────────────────────────────────
# Adding a new widget = add one entry here.  Frontend + script pick it up.

WIDGET_REGISTRY: Dict[str, Dict[str, Any]] = {

    "whatsapp_widget": {
        "name_ar":        "واتساب مع شعار متحرك",
        "description_ar": "شعار متحرك فوق زر واتساب مع دوائر نابضة — قابل لتغيير الصورة والموضع",
        "category":       "communication",
        "badge":          "free",
        "has_settings":   True,
        "icon":           "MessageCircle",
        "default_settings": {
            "phone":            "",
            "message":          "السلام عليكم، أبغى الاستفسار",
            "logo_url":         "",
            "position":         "left",        # left | right
            "theme_color":      "#25D366",
            "show_on_mobile":   True,
            "show_on_desktop":  True,
            "scroll_threshold_px": 250,
        },
        "default_rules": {
            **_DEFAULT_RULES,
            "trigger":            "scroll",
            "show_once_per_user": False,
        },
    },

    "discount_popup": {
        "name_ar":        "نافذة خصم",
        "description_ar": "نافذة منبثقة تعرض خصماً حصرياً للزائر — تزيد التحويل فوراً",
        "category":       "conversion",
        "badge":          "free",
        "has_settings":   True,
        "icon":           "Gift",
        "default_settings": {
            "title":             "عرض حصري لك! 🎁",
            "description":       "احصل على خصم على طلبك الأول",
            "discount_type":     "percentage",  # percentage | fixed | text
            "discount_value":    10,
            "coupon_code":       "",             # shown with copy button when set
            "input_type":        "none",         # none | email | whatsapp
            "input_placeholder": "أدخل بريدك الإلكتروني",
            "button_text":       "احصل على الخصم",
            "button_color":      "#6366F1",
            "show_close_button": True,
        },
        "default_rules": {
            **_DEFAULT_RULES,
            "trigger":            "entry",
            "show_after_seconds": 5,
            "show_once_per_user": True,
        },
    },

    "slide_offer": {
        "name_ar":        "شريط عرض جانبي",
        "description_ar": "شريط صغير على طرف الشاشة يعرض العرض — ينقر عليه الزائر ليرى التفاصيل",
        "category":       "conversion",
        "badge":          "free",
        "has_settings":   True,
        "icon":           "Tag",
        "default_settings": {
            "text":              "احصل على خصم 10% 🏷️",
            "position":          "left",         # left | right
            "bg_color":          "#6366F1",
            "text_color":        "#ffffff",
            "trigger_popup":     True,            # opens discount_popup on click
        },
        "default_rules": {
            **_DEFAULT_RULES,
            "trigger":            "entry",
            "show_after_seconds": 3,
            "show_once_per_user": False,
        },
    },
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_or_create(db: Session, tenant_id: int, widget_key: str):
    """Return existing MerchantWidget row, creating a default if missing."""
    from models import MerchantWidget  # noqa: PLC0415

    row = (
        db.query(MerchantWidget)
        .filter(MerchantWidget.tenant_id == tenant_id, MerchantWidget.widget_key == widget_key)
        .first()
    )
    if row is None:
        meta = WIDGET_REGISTRY.get(widget_key, {})
        row = MerchantWidget(
            tenant_id     = tenant_id,
            widget_key    = widget_key,
            is_enabled    = False,
            settings_json = dict(meta.get("default_settings", {})),
            display_rules = dict(meta.get("default_rules", _DEFAULT_RULES)),
        )
        db.add(row)
        db.flush()
    return row


def _serialize(row, meta: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a MerchantWidget row + registry meta into an API dict."""
    settings = dict(row.settings_json or {})
    defaults = dict(meta.get("default_settings", {}))
    rules    = dict(row.display_rules   or meta.get("default_rules", _DEFAULT_RULES))
    return {
        "key":          row.widget_key,
        "name":         meta.get("name_ar", row.widget_key),
        "description":  meta.get("description_ar", ""),
        "category":     meta.get("category", "general"),
        "badge":        meta.get("badge", "free"),
        "icon":         meta.get("icon", "Puzzle"),
        "has_settings": meta.get("has_settings", False),
        "is_enabled":   row.is_enabled,
        "settings":     {**defaults, **settings},
        "display_rules": rules,
    }


def _migrate_legacy_whatsapp_settings(db: Session, tenant_id: int, row) -> None:
    """Keep the earlier manual-widget preferences when moving to one control."""
    defaults = WIDGET_REGISTRY["whatsapp_widget"]["default_settings"]
    current = dict(row.settings_json or {})
    if current.get("phone") or current.get("logo_url") or current.get("position", "left") != "left":
        return
    from models import TenantSettings  # noqa: PLC0415

    tenant_settings = db.query(TenantSettings).filter(TenantSettings.tenant_id == tenant_id).first()
    legacy = dict((tenant_settings.extra_metadata or {}).get("widget_settings") or {}) if tenant_settings else {}
    if not legacy:
        return
    migrated = {**defaults, **current}
    if re.fullmatch(r"\d{8,15}", str(legacy.get("phone") or "")):
        migrated["phone"] = str(legacy["phone"])
    if isinstance(legacy.get("message"), str):
        migrated["message"] = legacy["message"]
    if legacy.get("position") in {"left", "right"}:
        migrated["position"] = legacy["position"]
    try:
        migrated["logo_url"] = _safe_widget_image_url(legacy.get("logo_url"))
    except ValueError:
        pass
    if isinstance(legacy.get("scroll_threshold"), int):
        migrated["scroll_threshold_px"] = max(0, min(2000, legacy["scroll_threshold"]))
    row.settings_json = migrated


# ── JS / CSS generators ───────────────────────────────────────────────────────

_STUB = "/* Nahla Widgets — all disabled */"

_JS_HEADERS = {
    "Content-Type":  "application/javascript; charset=utf-8",
    "Cache-Control": "no-store",
    "X-Robots-Tag":  "noindex",
}



_NAHLA_STORE_LOGO = "https://app.nahlah.ai/whatsapp-bee-transparent.png"


def _safe_widget_image_url(value: Any) -> str:
    """Allow merchant images from public HTTPS hosts only."""
    if not value:
        return ""
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("invalid_widget_image_url")
    url = value.strip()
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username
        or parsed.password
        or host == "localhost"
        or host.endswith(".localhost")
        or host.endswith(".local")
    ):
        raise ValueError("invalid_widget_image_url")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("invalid_widget_image_url")
    return url


def _build_nahla_widgets_js(widgets: list[Dict[str, Any]], tenant_id: int = 0, db=None) -> str:
    """
    Render the full nahla-widgets.js bundle with tenant config baked in.
    All widget code is self-contained — no additional network requests needed.

    Phone number priority for WhatsApp widget:
      1. Merchant's CONNECTED WhatsApp number (from WhatsAppConnection table)
      2. Manually configured phone in widget settings
      3. Empty (widget won't show if both are empty)
    """
    import json  # noqa: PLC0415

    wa      = next((w for w in widgets if w["widget_key"] == "whatsapp_widget"), None)
    popup   = next((w for w in widgets if w["widget_key"] == "discount_popup"), None)
    slide   = next((w for w in widgets if w["widget_key"] == "slide_offer"), None)

    wa_cfg    = {**(wa["settings"]      if wa    else {}), **(wa["display_rules"]    if wa    else {}), "enabled": bool(wa    and wa["is_enabled"])}
    popup_cfg = {**(popup["settings"]   if popup else {}), **(popup["display_rules"] if popup else {}), "enabled": bool(popup and popup["is_enabled"])}
    slide_cfg = {**(slide["settings"]   if slide else {}), **(slide["display_rules"] if slide else {}), "enabled": bool(slide and slide["is_enabled"])}

    # ── Auto-fill WhatsApp phone from connected number (tenant-isolated) ─────
    # This prevents showing another tenant's phone and ensures correct isolation.
    if db is not None and tenant_id and wa_cfg.get("enabled"):
        try:
            from models import WhatsAppConnection  # noqa: PLC0415
            conn = db.query(WhatsAppConnection).filter_by(
                tenant_id=tenant_id, status="connected"
            ).first()
            if conn and conn.phone_number:
                # Strip leading + and spaces for wa.me URL
                connected_phone = conn.phone_number.lstrip("+").replace(" ", "")
                if connected_phone:
                    wa_cfg["phone"] = connected_phone
                    logger.debug(
                        "[widgets] Using connected WA phone=%s for tenant=%s",
                        connected_phone, tenant_id,
                    )
            elif not wa_cfg.get("phone"):
                # No connected number and no manually configured phone → disable widget
                wa_cfg["enabled"] = False
                logger.debug(
                    "[widgets] WhatsApp widget disabled — no phone for tenant=%s", tenant_id
                )
        except Exception as _e:
            logger.warning("[widgets] Could not fetch WA connection for tenant=%s: %s", tenant_id, _e)

    if not any([wa_cfg.get("enabled"), popup_cfg.get("enabled"), slide_cfg.get("enabled")]):
        return _STUB

    try:
        wa_cfg["logo_url"] = _safe_widget_image_url(wa_cfg.get("logo_url"))
    except ValueError:
        wa_cfg["logo_url"] = ""
    wa_json    = json.dumps(wa_cfg,    ensure_ascii=False)
    popup_json = json.dumps(popup_cfg, ensure_ascii=False)
    slide_json = json.dumps(slide_cfg, ensure_ascii=False)
    logo       = _NAHLA_STORE_LOGO

    return f"""/* ============================================================
   Nahla Conversion Widgets — nahla-widgets.js
   https://nahlah.ai  |  Loaded by merchant store script tag
   ============================================================ */
(function(N){{
'use strict';

// ── Config (server-rendered per tenant) ──────────────────────
var TENANT_ID='{tenant_id}';
N.waCfg    = {wa_json};
N.popupCfg = {popup_json};
N.slideCfg = {slide_cfg if isinstance(slide_cfg, str) else slide_json};

// ── Utils ─────────────────────────────────────────────────────
function ls(k,v){{
  var KEY='nahla_'+k;
  if(v!==undefined){{try{{localStorage.setItem(KEY,JSON.stringify(v));}}catch(e){{}}}}
  else{{try{{return JSON.parse(localStorage.getItem(KEY));}}catch(e){{return null;}}}}
}}
function q(sel){{return document.querySelector(sel);}}
function css(el,st){{Object.assign(el.style,st);}}
function onReady(fn){{document.readyState!=='loading'?fn():document.addEventListener('DOMContentLoaded',fn);}}
function addStyles(s){{var el=document.createElement('style');el.textContent=s;document.head.appendChild(el);}}

// ── Shared coupon application helpers (used by popup + auto-apply) ──────────
function _fillInput(inp,code){{
  inp.focus();
  try{{Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(inp,code);}}catch(e){{inp.value=code;}}
  inp.dispatchEvent(new Event('input',{{bubbles:true,cancelable:true}}));
  inp.dispatchEvent(new Event('change',{{bubbles:true,cancelable:true}}));
}}

function _findApplyBtn(inp){{
  // 1. Nearest button walking up from input (most reliable)
  var p=inp.parentElement;
  while(p&&p!==document.body){{
    var near=p.querySelector('button');
    if(near)return near;
    p=p.parentElement;
  }}
  // 2. Next sibling button
  var next=inp.nextElementSibling;
  while(next){{if(next.tagName==='BUTTON')return next;next=next.nextElementSibling;}}
  // 3. Search all buttons by known text
  var keywords=['تطبيق','Apply','apply'];
  var allBtns=document.querySelectorAll('button');
  for(var b=0;b<allBtns.length;b++){{
    var txt=allBtns[b].textContent.trim();
    for(var k=0;k<keywords.length;k++){{if(txt===keywords[k]||txt.indexOf(keywords[k])!==-1)return allBtns[b];}}
  }}
  return null;
}}

function _applyCouponToPage(code){{
  // 0. Salla web component (shadow DOM)
  var sc=document.querySelector('salla-coupon,salla-coupon-form');
  if(sc){{
    try{{if(typeof sc.applyCoupon==='function'){{sc.applyCoupon(code);setTimeout(function(){{window.location.reload();}},1500);return true;}}}}catch(e){{}}
    var root=sc.shadowRoot||sc;
    var si=root.querySelector('input');
    if(si){{
      _fillInput(si,code);
      var sb=root.querySelector('button[type="submit"],button');
      if(sb)setTimeout(function(){{sb.click();setTimeout(function(){{window.location.reload();}},1500);}},600);
      return true;
    }}
  }}
  // 1. Regular input selectors
  var sel=['input[name="coupon"]','input[name="coupon_code"]','input[name="discount_code"]',
    'input[placeholder*="خصم"]','input[placeholder*="كوبون"]','input[placeholder*="coupon"]',
    'input[id*="coupon"]','input[id*="discount"]','.coupon-field input','[data-coupon] input'];
  var inp=null;
  for(var i=0;i<sel.length;i++){{inp=document.querySelector(sel[i]);if(inp)break;}}
  if(!inp)return false;
  _fillInput(inp,code);
  var btn=_findApplyBtn(inp);
  if(btn){{
    setTimeout(function(){{
      btn.click();
      setTimeout(function(){{window.location.reload();}},1500);
    }},600);
    return true;
  }}
  var form=inp.closest('form');
  if(form){{
    setTimeout(function(){{
      form.dispatchEvent(new Event('submit',{{bubbles:true,cancelable:true}}));
      setTimeout(function(){{window.location.reload();}},1500);
    }},600);
    return true;
  }}
  return false;
}}

// ── Page detection ────────────────────────────────────────────
function matchPage(pages){{
  if(!pages||pages.indexOf('all')>-1)return true;
  var p=location.pathname;
  if(pages.indexOf('home')>-1&&(p==='/'||p==='/index.html'))return true;
  if(pages.indexOf('product')>-1&&(p.indexOf('/products/')>-1||p.indexOf('/product/')>-1||/^p[0-9]+$/.test(p.split('/').filter(Boolean).pop()||'')))return true;
  if(pages.indexOf('cart')>-1&&p.indexOf('/cart')>-1)return true;
  if(pages.indexOf('checkout')>-1&&p.indexOf('/checkout')>-1)return true;
  return false;
}}

// ══════════════════════════════════════════════════════════════
// 1. WhatsApp Widget
// ══════════════════════════════════════════════════════════════
function initWhatsApp(c){{
  if(!c.enabled||!c.phone)return;
  if(!matchPage(c.show_on_pages))return;
  // An older, manually installed Nahla button may still be in the theme.
  if(document.getElementById('nahla-whatsapp')||document.getElementById('nahla-wa'))return;

  var isMobile=/Android|iPhone|iPad/i.test(navigator.userAgent);
  if(isMobile&&c.show_on_mobile===false)return;
  if(!isMobile&&c.show_on_desktop===false)return;

  var logo=c.logo_url||'{logo}';
  var color=c.theme_color||'#25D366';
  var pos=c.position==='right'?'right':'left';
  var posVal=pos+':40px';

  addStyles(`
    #nahla-wa{{
      position:fixed;bottom:55px;${{posVal}};z-index:99999;
      display:flex;flex-direction:column;align-items:center;gap:6px;
      opacity:0;transform:scale(.8);
      transition:opacity .4s,transform .4s;
      pointer-events:none;text-decoration:none;
    }}
    #nahla-wa.show{{opacity:1;transform:scale(1);pointer-events:auto;}}
    #nahla-wa .nahla-bee{{
      width:110px;height:110px;object-fit:contain;
      animation:bee-float 3s ease-in-out infinite;
    }}
    @keyframes bee-float{{
      0%,100%{{transform:translateY(0) rotate(-4deg);}}
      50%{{transform:translateY(-7px) rotate(4deg);}}
    }}
    #nahla-wa .nw-circle{{
      position:relative;width:65px;height:65px;
      background:${{color}};border-radius:50%;
      display:flex;align-items:center;justify-content:center;
      box-shadow:0 4px 18px rgba(37,211,102,.45);
    }}
    #nahla-wa .nw-icon{{width:30px;height:30px;z-index:2;position:relative;}}
    #nahla-wa .nw-orbit{{
      position:absolute;inset:0;border-radius:50%;
      border:2.5px solid rgba(37,211,102,.65);
      animation:apple-wave 2.8s cubic-bezier(.4,0,.2,1) infinite;
    }}
    #nahla-wa .o1{{animation-delay:0s;}}
    #nahla-wa .o2{{animation-delay:.7s;}}
    #nahla-wa .o3{{animation-delay:1.4s;}}
    #nahla-wa .o4{{animation-delay:2.1s;}}
    @keyframes apple-wave{{
      0%{{transform:scale(.92) rotate(0deg);opacity:.85;}}
      30%{{transform:scale(1.25) rotate(108deg);opacity:.55;}}
      60%{{transform:scale(1.65) rotate(216deg);opacity:.22;}}
      85%{{transform:scale(1.95) rotate(306deg);opacity:.05;}}
      100%{{transform:scale(2.05) rotate(360deg);opacity:0;}}
    }}
    @media(max-width:600px){{
      #nahla-wa .nw-circle{{width:58px;height:58px;}}
      #nahla-wa .nw-icon{{width:26px;height:26px;}}
      #nahla-wa .nahla-bee{{width:90px;height:90px;}}
      #nahla-wa{{bottom:50px;${{pos}}:20px;}}
    }}
    @media(prefers-reduced-motion:reduce){{
      #nahla-wa .nahla-bee,#nahla-wa .nw-orbit{{animation:none;}}
    }}
  `);

  var wrap=document.createElement('a');
  wrap.id='nahla-wa';
  wrap.href='https://wa.me/'+String(c.phone).replace(/[^0-9]/g,'')+'?text='+encodeURIComponent(c.message||'');
  wrap.target='_blank';wrap.rel='noopener noreferrer';
  wrap.setAttribute('aria-label','تواصل عبر واتساب');
  var brand=document.createElement('img');
  brand.className='nahla-bee';
  brand.src=logo;
  brand.alt=c.logo_url?'شعار المتجر':'نحلة';
  var circle=document.createElement('div');
  circle.className='nw-circle';
  for(var orbit=1;orbit<=4;orbit++){{
    var ring=document.createElement('span');
    ring.className='nw-orbit o'+orbit;
    circle.appendChild(ring);
  }}
  var icon=document.createElement('img');
  icon.className='nw-icon';
  icon.src='https://upload.wikimedia.org/wikipedia/commons/6/6b/WhatsApp.svg';
  icon.alt='واتساب';
  circle.appendChild(icon);
  wrap.appendChild(brand);
  wrap.appendChild(circle);
  document.body.appendChild(wrap);

  function checkShow(){{
    var threshold=Number(c.scroll_threshold_px);
    if(!isFinite(threshold)||threshold<0)threshold=250;
    if(c.trigger==='scroll'&&window.scrollY<=threshold&&
       document.body.scrollHeight>window.innerHeight+300)return;
    wrap.classList.add('show');
    window.removeEventListener('scroll',checkShow);
  }}

  if(c.show_after_seconds>0){{
    setTimeout(function(){{wrap.classList.add('show');}},c.show_after_seconds*1000);
  }}else if(c.trigger==='scroll'){{
    window.addEventListener('scroll',checkShow,{{passive:true}});checkShow();
  }}else{{
    setTimeout(function(){{wrap.classList.add('show');}},500);
  }}
}}

// ══════════════════════════════════════════════════════════════
// 2. Discount Popup
// ══════════════════════════════════════════════════════════════
function initDiscountPopup(c,fromSlide){{
  if(!c.enabled)return null;
  if(!matchPage(c.show_on_pages))return null;

  var SEEN_KEY='popup_seen_v1';
  if(!fromSlide&&c.show_once_per_user&&ls(SEEN_KEY))return null;

  var btnColor=c.button_color||'#6366F1';
  var discountLabel='';
  if(c.discount_type==='percentage')discountLabel=c.discount_value+'%';
  else if(c.discount_type==='fixed')discountLabel=c.discount_value+' ر.س';
  else discountLabel=c.discount_value||'';

  addStyles(`
    #nahla-popup-ov{{position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:999998;display:flex;align-items:center;justify-content:center;backdrop-filter:blur(4px);opacity:0;transition:opacity .3s;}}
    #nahla-popup-ov.show{{opacity:1;}}
    #nahla-popup{{background:#fff;border-radius:20px;padding:36px 32px 28px;max-width:420px;width:92%;text-align:center;position:relative;box-shadow:0 25px 60px rgba(0,0,0,.2);transform:scale(.92);transition:transform .3s;}}
    #nahla-popup-ov.show #nahla-popup{{transform:scale(1);}}
    #nahla-popup .np-badge{{display:inline-block;background:linear-gradient(135deg,#6366F1,#8B5CF6);color:#fff;font-size:28px;font-weight:800;padding:10px 22px;border-radius:12px;margin-bottom:14px;}}
    #nahla-popup h2{{font-size:22px;font-weight:700;color:#1e293b;margin:0 0 8px;}}
    #nahla-popup p{{font-size:15px;color:#64748b;margin:0 0 18px;line-height:1.6;}}
    #nahla-popup input{{width:100%;box-sizing:border-box;border:1.5px solid #e2e8f0;border-radius:10px;padding:11px 14px;font-size:15px;outline:none;margin-bottom:12px;direction:rtl;}}
    #nahla-popup input:focus{{border-color:${{btnColor}};}}
    #nahla-popup .np-btn{{width:100%;background:${{btnColor}};color:#fff;border:none;border-radius:10px;padding:13px;font-size:16px;font-weight:700;cursor:pointer;transition:opacity .2s;}}
    #nahla-popup .np-btn:hover{{opacity:.88;}}
    #nahla-popup .np-close{{position:absolute;top:14px;left:14px;background:none;border:none;font-size:22px;color:#94a3b8;cursor:pointer;line-height:1;padding:4px;}}
    #nahla-popup .np-close:hover{{color:#475569;}}
    #nahla-popup .np-coupon{{display:flex;align-items:center;gap:8px;background:#f8fafc;border:2px dashed ${{btnColor}};border-radius:10px;padding:10px 14px;margin-bottom:12px;}}
    #nahla-popup .np-coupon-code{{flex:1;font-size:18px;font-weight:800;color:#1e293b;letter-spacing:2px;text-align:center;direction:ltr;}}
    #nahla-popup .np-copy{{background:${{btnColor}};color:#fff;border:none;border-radius:7px;padding:6px 12px;font-size:12px;font-weight:700;cursor:pointer;white-space:nowrap;transition:opacity .2s;}}
    #nahla-popup .np-copy:hover{{opacity:.85;}}
    #nahla-popup .np-copy.copied{{background:#22c55e;}}
  `);

  var ov=document.createElement('div');
  ov.id='nahla-popup-ov';
  var couponHtml='';
  if(c.coupon_code){{
    couponHtml='<div class="np-coupon">'
      +'<span class="np-coupon-code">'+c.coupon_code+'</span>'
      +'<button class="np-copy" id="nahla-copy-btn">نسخ</button>'
      +'</div>';
  }}
  ov.innerHTML=`<div id="nahla-popup">
    ${{c.show_close_button!==false?'<button class="np-close" id="nahla-popup-close">✕</button>':''}}
    ${{discountLabel?'<div class="np-badge">-'+discountLabel+'</div>':''}}
    <h2>${{c.title||'عرض حصري لك!'}}</h2>
    <p>${{c.description||''}}</p>
    ${{couponHtml}}
    ${{c.input_type&&c.input_type!=='none'?'<input type="'+(c.input_type==='email'?'email':'tel')+'" placeholder="'+(c.input_placeholder||'')+'" id="nahla-popup-input">':''}}
    <button class="np-btn" id="nahla-popup-cta">${{c.button_text||'احصل على الخصم'}}</button>
  </div>`;
  document.body.appendChild(ov);

  function show(){{setTimeout(function(){{ov.classList.add('show');}},50);}}
  function hide(){{ov.classList.remove('show');setTimeout(function(){{ov.remove();}},300);ls(SEEN_KEY,1);}}

  var closeBtn=document.getElementById('nahla-popup-close');
  if(closeBtn)closeBtn.addEventListener('click',hide);
  ov.addEventListener('click',function(e){{if(e.target===ov)hide();}});

  // ── Copy button ────────────────────────────────────────────────────────────
  var copyBtn=document.getElementById('nahla-copy-btn');
  if(copyBtn)copyBtn.addEventListener('click',function(){{
    try{{navigator.clipboard.writeText(c.coupon_code);}}catch(e){{}}
    copyBtn.textContent='تم النسخ ✓';copyBtn.classList.add('copied');
    setTimeout(function(){{copyBtn.textContent='نسخ';copyBtn.classList.remove('copied');}},2000);
  }});

  // ── Main CTA — apply coupon ────────────────────────────────────────────────
  var cta=document.getElementById('nahla-popup-cta');
  if(cta)cta.addEventListener('click',function(){{
    ls(SEEN_KEY,1);
    cta.textContent='⏳ جاري…';cta.disabled=true;

    // 1. Use pre-configured static code if available
    var staticCode=N.popupCfg&&N.popupCfg.coupon_code?N.popupCfg.coupon_code:'';
    if(staticCode){{
      try{{navigator.clipboard.writeText(staticCode);}}catch(e){{}}
      _applyCode(staticCode,cta);
      return;
    }}

    // 2. Request a dynamic coupon from the backend
    var apiBase='{_API_BASE}';
    fetch(apiBase+'/merchant/widgets/'+TENANT_ID+'/create-coupon',{{method:'POST'}})
      .then(function(r){{return r.json();}})
      .then(function(d){{
        if(d.success&&d.code){{
          try{{navigator.clipboard.writeText(d.code);}}catch(e){{}}
          _applyCode(d.code,cta);
        }}else{{
          // No coupon available — still redirect to cart
          cta.textContent='احصل على الخصم';cta.disabled=false;
          _applyCode('',cta);
        }}
      }})
      .catch(function(){{
        cta.textContent='احصل على الخصم';cta.disabled=false;
        hide();
      }});
  }});

  // Redirect to cart with coupon in URL — Salla applies it natively
  function _applyCode(code,btn){{
    btn.textContent='⏳ جاري تطبيق الخصم…';
    btn.disabled=true;
    var lm=window.location.pathname.match(new RegExp('^/([a-z]{{2}})/'));
    var cartPath=(lm?'/'+lm[1]:'')+'/cart';
    var dest=code?cartPath+'?coupon='+encodeURIComponent(code):cartPath;
    setTimeout(function(){{window.location.href=dest;}},900);
  }}

  return {{show:show,hide:hide}};
}}

// ══════════════════════════════════════════════════════════════
// 3. Slide Offer Tab
// ══════════════════════════════════════════════════════════════
function initSlideOffer(c){{
  if(!c.enabled)return;
  if(!matchPage(c.show_on_pages))return;

  var pos=c.position==='right'?'right':'left';
  var bg=c.bg_color||'#6366F1';
  var fg=c.text_color||'#fff';

  addStyles(`
    #nahla-slide-tab{{
      position:fixed;top:50%;transform:translateY(-50%) translateX(${{pos==='left'?'-100%':'100%'}});
      ${{pos}}:0;z-index:99997;
      background:${{bg}};color:${{fg}};
      writing-mode:vertical-rl;text-orientation:mixed;
      ${{pos==='left'?'transform:translateY(-50%) rotate(180deg) translateY(-100%);':'transform:translateY(-50%);'}}
      padding:16px 10px;font-size:14px;font-weight:700;
      border-radius:${{pos==='left'?'0 10px 10px 0':'10px 0 0 10px'}};
      cursor:pointer;box-shadow:0 4px 16px rgba(0,0,0,.2);
      opacity:0;transition:opacity .4s,transform .4s;
      user-select:none;
    }}
    #nahla-slide-tab.show{{opacity:1;transform:translateY(-50%);}}
    #nahla-slide-tab:hover{{filter:brightness(1.1);}}
  `);

  var tab=document.createElement('div');
  tab.id='nahla-slide-tab';
  tab.textContent=c.text||'عرض خاص!';
  document.body.appendChild(tab);

  // Show after delay
  setTimeout(function(){{tab.classList.add('show');}}, (c.show_after_seconds||3)*1000);

  // Click opens discount popup if configured
  if(c.trigger_popup!==false&&N.popupCfg&&N.popupCfg.enabled){{
    tab.addEventListener('click',function(){{
      var existing=document.getElementById('nahla-popup-ov');
      if(existing){{existing.classList.add('show');return;}}
      var p=initDiscountPopup(N.popupCfg,true);
      if(p)p.show();
    }});
  }}
}}

// ══════════════════════════════════════════════════════════════
// Cleanup: remove any stale pending coupon from localStorage
// (Salla handles ?coupon=CODE natively in the cart page)
// ══════════════════════════════════════════════════════════════
(function(){{
  try{{localStorage.removeItem('nahla_pending_coupon');}}catch(e){{}}
}})();

// ══════════════════════════════════════════════════════════════
// Bootstrap
// ══════════════════════════════════════════════════════════════
onReady(function(){{
  initWhatsApp(N.waCfg);
  // Popup: run unless slide is shown (slide triggers it on demand)
  if(N.popupCfg.enabled&&!(N.slideCfg&&N.slideCfg.enabled&&N.slideCfg.trigger_popup!==false)){{
    var pop=initDiscountPopup(N.popupCfg,false);
    if(pop){{
      var delay=(N.popupCfg.show_after_seconds||5)*1000;
      setTimeout(function(){{pop.show();}},delay);
    }}
  }}
  initSlideOffer(N.slideCfg);
}});

}})(window.Nahla=window.Nahla||{{}});"""


# ── Schemas ───────────────────────────────────────────────────────────────────

class ToggleBody(BaseModel):
    enabled: bool


class WidgetSettingsBody(BaseModel):
    settings: Dict[str, Any]


class WidgetRulesBody(BaseModel):
    rules: Dict[str, Any]


# ── Authenticated routes ───────────────────────────────────────────────────────

@router.get("/merchant/widgets")
async def list_widgets(request: Request, db: Session = Depends(get_db)):
    """Return all registered widgets enriched with this tenant's state."""
    tenant_id = resolve_tenant_id(request)
    from models import MerchantWidget  # noqa: PLC0415

    rows = {
        r.widget_key: r
        for r in db.query(MerchantWidget)
                   .filter(MerchantWidget.tenant_id == tenant_id)
                   .all()
    }

    result = []
    for key, meta in WIDGET_REGISTRY.items():
        if key not in rows:
            rows[key] = _get_or_create(db, tenant_id, key)
        if key == "whatsapp_widget":
            _migrate_legacy_whatsapp_settings(db, tenant_id, rows[key])
        result.append(_serialize(rows[key], meta))

    db.commit()
    return {"widgets": result}


@router.post("/merchant/widgets/{widget_key}/toggle")
async def toggle_widget(
    widget_key: str,
    body: ToggleBody,
    request: Request,
    db: Session = Depends(get_db),
):
    if widget_key not in WIDGET_REGISTRY:
        raise HTTPException(status_code=404, detail=f"widget '{widget_key}' not found")

    tenant_id = resolve_tenant_id(request)
    row = _get_or_create(db, tenant_id, widget_key)
    if widget_key == "whatsapp_widget" and body.enabled and not (row.settings_json or {}).get("phone"):
        from models import WhatsAppConnection  # noqa: PLC0415

        connected = db.query(WhatsAppConnection).filter_by(
            tenant_id=tenant_id, status="connected"
        ).first()
        if not connected or not connected.phone_number:
            raise HTTPException(status_code=400, detail="whatsapp_phone_required")
    row.is_enabled = body.enabled
    row.updated_at = datetime.now(timezone.utc)
    db.commit()

    logger.info("[widgets/toggle] tenant=%s widget=%s enabled=%s", tenant_id, widget_key, body.enabled)
    return _serialize(row, WIDGET_REGISTRY[widget_key])


@router.put("/merchant/widgets/{widget_key}/settings")
async def update_widget_settings(
    widget_key: str,
    body: WidgetSettingsBody,
    request: Request,
    db: Session = Depends(get_db),
):
    if widget_key not in WIDGET_REGISTRY:
        raise HTTPException(status_code=404, detail=f"widget '{widget_key}' not found")

    tenant_id = resolve_tenant_id(request)
    row = _get_or_create(db, tenant_id, widget_key)
    current = dict(row.settings_json or {})
    if widget_key == "whatsapp_widget":
        incoming = body.settings
        if "logo_url" in incoming:
            try:
                incoming["logo_url"] = _safe_widget_image_url(incoming["logo_url"])
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        if "phone" in incoming:
            phone = str(incoming["phone"] or "")
            if phone and not re.fullmatch(r"\d{8,15}", phone):
                raise HTTPException(status_code=400, detail="invalid_whatsapp_phone")
        if "position" in incoming and incoming["position"] not in {"left", "right"}:
            raise HTTPException(status_code=400, detail="invalid_widget_position")
        if "theme_color" in incoming and not re.fullmatch(r"#[0-9a-fA-F]{6}", str(incoming["theme_color"])):
            raise HTTPException(status_code=400, detail="invalid_widget_color")
        if "scroll_threshold_px" in incoming:
            threshold = incoming["scroll_threshold_px"]
            if isinstance(threshold, bool) or not isinstance(threshold, int) or not 0 <= threshold <= 2000:
                raise HTTPException(status_code=400, detail="invalid_widget_scroll_threshold")
    current.update(body.settings)
    row.settings_json = current
    row.updated_at = datetime.now(timezone.utc)
    db.commit()

    logger.info("[widgets/settings] tenant=%s widget=%s", tenant_id, widget_key)
    return _serialize(row, WIDGET_REGISTRY[widget_key])


@router.post("/merchant/widgets/whatsapp_widget/logo", status_code=201)
async def upload_whatsapp_widget_logo(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Upload a merchant image to durable, tenant-scoped public storage."""
    from services.catalog_media_storage import CatalogMediaStorageError, CatalogMediaValidationError
    from services.catalog_media_storage import MAX_UPLOAD_BYTES
    from services.widget_media_storage import upload_widget_logo

    tenant_id = resolve_tenant_id(request)
    try:
        content = await file.read(MAX_UPLOAD_BYTES + 1)
        return upload_widget_logo(tenant_id=tenant_id, content=content)
    except CatalogMediaValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except CatalogMediaStorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.put("/merchant/widgets/{widget_key}/rules")
async def update_widget_rules(
    widget_key: str,
    body: WidgetRulesBody,
    request: Request,
    db: Session = Depends(get_db),
):
    if widget_key not in WIDGET_REGISTRY:
        raise HTTPException(status_code=404, detail=f"widget '{widget_key}' not found")

    tenant_id = resolve_tenant_id(request)
    row = _get_or_create(db, tenant_id, widget_key)
    current = dict(row.display_rules or {})
    current.update(body.rules)
    row.display_rules = current
    row.updated_at = datetime.now(timezone.utc)
    db.commit()

    logger.info("[widgets/rules] tenant=%s widget=%s", tenant_id, widget_key)
    return _serialize(row, WIDGET_REGISTRY[widget_key])


@router.post("/merchant/widgets/salla-install")
async def salla_auto_install_widgets(request: Request, db: Session = Depends(get_db)):
    """Try to inject nahla-widgets.js into the merchant's Salla store via API."""
    import httpx as _httpx  # noqa: PLC0415

    tenant_id = resolve_tenant_id(request)
    from models import Integration  # noqa: PLC0415

    embed_url  = f"{_API_BASE}/merchant/widgets/{tenant_id}/nahla-widgets.js"
    script_tag = f'<script src="{embed_url}" defer></script>'
    salla_admin_url = "https://s.salla.sa/settings/scripts"

    integration = (
        db.query(Integration)
        .filter(Integration.tenant_id == tenant_id, Integration.provider == "salla")
        .first()
    )
    if not integration:
        return {
            "success": False, "reason": "no_salla_connection",
            "script_tag": script_tag, "salla_admin_url": salla_admin_url,
            "message": "ربط متجر سلة غير مكتمل — أضف الكود يدوياً",
        }

    salla_token = (integration.config or {}).get("token") or (integration.config or {}).get("access_token")
    if not salla_token:
        return {
            "success": False, "reason": "no_token",
            "script_tag": script_tag, "salla_admin_url": salla_admin_url,
            "message": "رمز سلة غير موجود — أضف الكود يدوياً",
        }

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
                logger.info("[widgets/salla-install] API success tenant=%s", tenant_id)
                return {
                    "success": True, "method": "api",
                    "message": "تم تثبيت الويدجتات تلقائياً في متجرك ✓",
                    "script_id": resp.json().get("data", {}).get("id"),
                }
            logger.info("[widgets/salla-install] Salla scripts API %s — fallback", resp.status_code)
    except Exception as exc:
        logger.info("[widgets/salla-install] Salla API failed: %s — fallback", exc)

    return {
        "success":        False,
        "reason":         "api_not_available",
        "script_tag":     script_tag,
        "embed_url":      embed_url,
        "salla_store_id": (integration.config or {}).get("store_id", ""),
        "salla_admin_url": salla_admin_url,
        "message": "أضف الكود أدناه في إعدادات متجرك — خطوة واحدة فقط",
    }


# ── Public store-script endpoints ─────────────────────────────────────────────

@router.post("/merchant/widgets/{tenant_id}/create-coupon", include_in_schema=False)
async def create_unique_coupon(tenant_id: int, db: Session = Depends(get_db)):
    """
    Called from the store's discount popup (no JWT — public, tenant_id in URL).
    1. Looks up merchant's Salla access token.
    2. Creates a unique one-time coupon via Salla Admin API.
    3. Returns the coupon code so the widget can apply it to the cart.

    Falls back to the configured static coupon_code if Salla API fails.
    """
    import httpx as _httpx  # noqa: PLC0415
    import random, string  # noqa: PLC0415

    from models import Integration, MerchantWidget  # noqa: PLC0415

    # ── Get popup settings (for discount type / value / static code) ──────────
    popup = (
        db.query(MerchantWidget)
        .filter(MerchantWidget.tenant_id == tenant_id, MerchantWidget.widget_key == "discount_popup")
        .first()
    )
    settings = dict(popup.settings_json or {}) if popup else {}
    static_code   = settings.get("coupon_code", "")
    discount_type = settings.get("discount_type", "percentage")   # percentage | fixed
    discount_value = int(settings.get("discount_value", 10))

    # ── Lookup Salla token ────────────────────────────────────────────────────
    integration = (
        db.query(Integration)
        .filter(Integration.tenant_id == tenant_id, Integration.provider == "salla")
        .first()
    )
    cfg = integration.config or {} if integration else {}
    # Token stored as "api_key" (OAuth flow) or legacy "token" key
    salla_token = cfg.get("api_key") or cfg.get("token") or cfg.get("access_token") or ""

    if not salla_token:
        logger.warning("[widgets/create-coupon] No Salla token for tenant=%s — cfg_keys=%s", tenant_id, list(cfg.keys()))
        # No Salla token → return static code if set
        if static_code:
            return {"success": True, "code": static_code, "method": "static"}
        return {"success": False, "reason": "no_token", "code": ""}

    # ── Generate unique one-time coupon code ──────────────────────────────────
    suffix   = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    uniq_code = f"NAHLA{suffix}"

    # ── Call Salla Admin API ──────────────────────────────────────────────────
    from datetime import datetime, timedelta  # noqa: PLC0415
    expiry = (datetime.utcnow() + timedelta(days=1)).strftime("%Y-%m-%d")

    salla_type = "PERCENT" if discount_type == "percentage" else "FIXED"

    payload = {
        "code":               uniq_code,
        "type":               salla_type,
        "percent_off":        discount_value if salla_type == "PERCENT" else 0,
        "amount_off":         discount_value if salla_type == "FIXED"   else 0,
        "limit":              1,          # one use total
        "limit_per_user":     1,
        "status":             "active",
        "expiry_date":        expiry,
    }

    try:
        async with _httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(
                "https://api.salla.dev/admin/v2/coupons",
                headers={
                    "Authorization": f"Bearer {salla_token}",
                    "Content-Type":  "application/json",
                    "Accept":        "application/json",
                },
                json=payload,
            )
            if resp.status_code in (200, 201):
                data = resp.json()
                code = (data.get("data") or {}).get("code", uniq_code)
                logger.info("[widgets/create-coupon] Salla coupon created tenant=%s code=%s", tenant_id, code)
                return {"success": True, "code": code, "method": "salla_api"}
            else:
                logger.info("[widgets/create-coupon] Salla API %s — fallback static", resp.status_code)
    except Exception as exc:
        logger.info("[widgets/create-coupon] Salla API error: %s — fallback static", exc)

    # ── Fallback to static code ───────────────────────────────────────────────
    if static_code:
        return {"success": True, "code": static_code, "method": "static"}
    return {"success": False, "reason": "api_failed", "code": ""}


@router.get("/merchant/widgets/{tenant_id}/nahla-widgets.js", include_in_schema=False)
async def serve_widgets_js(tenant_id: int, db: Session = Depends(get_db)):
    """
    Per-tenant store script — all widgets bundled, config baked in.
    This is what merchants embed: <script src="…/nahla-widgets.js">
    """
    from models import MerchantWidget  # noqa: PLC0415

    rows = (
        db.query(MerchantWidget)
        .filter(MerchantWidget.tenant_id == tenant_id)
        .all()
    )

    widgets = [
        {
            "widget_key":    r.widget_key,
            "is_enabled":    r.is_enabled,
            "settings":      dict(r.settings_json or {}),
            "display_rules": dict(r.display_rules  or {}),
        }
        for r in rows
    ]

    if not any(w["is_enabled"] for w in widgets):
        return Response(content=_STUB, headers=_JS_HEADERS)

    js = _build_nahla_widgets_js(widgets, tenant_id, db=db)
    logger.info("[widgets/js] tenant=%s widgets=%d", tenant_id, len(rows))
    return Response(content=js, headers=_JS_HEADERS)


@router.get("/merchant/widgets/{tenant_id}/config.json", include_in_schema=False)
async def serve_widgets_config(tenant_id: int, db: Session = Depends(get_db)):
    """Public JSON config for advanced integrations (GTM, custom themes …)."""
    from models import MerchantWidget  # noqa: PLC0415

    rows = (
        db.query(MerchantWidget)
        .filter(MerchantWidget.tenant_id == tenant_id, MerchantWidget.is_enabled == True)  # noqa: E712
        .all()
    )
    data = [
        {
            "widget_key":    r.widget_key,
            "settings":      dict(r.settings_json or {}),
            "display_rules": dict(r.display_rules  or {}),
        }
        for r in rows
    ]
    return JSONResponse(
        content={"tenant_id": tenant_id, "widgets": data},
        headers={"Cache-Control": "public, max-age=60"},
    )


# ── Universal Salla Partner Portal snippet ────────────────────────────────────
# Publish the JavaScript loader in docs/runbooks/salla-sales-widget-snippet.md
# once through Salla Partner Portal → App → App Snippets.
# After that: every Salla store that installs the Nahla app loads widgets
# automatically. Enabling / disabling from Nahla takes effect on the next page load.

@router.get("/merchant/widgets/salla-auto.js", include_in_schema=False)
async def serve_salla_auto_snippet():
    """
    Universal Salla snippet — auto-detects store ID from multiple sources, loads bundle.
    """
    js = f"""/* Nahla Universal Salla Snippet v3 — {_API_BASE} */
(function(){{
  var API = '{_API_BASE}';

  function _getStoreId() {{
    // 1. Salla Twilight SDK — lowercase (standard)
    var s = window.salla;
    if (s) {{
      var id = (s.store && (s.store.id || s.store.merchant_id))
            || (s.env   && (s.env.storeId || s.env.store_id || s.env.merchantId))
            || (s.config && (typeof s.config.get === 'function' ? s.config.get('store.id') : s.config.store_id))
            || (s.settings && s.settings.store_id)
            || (s.data && s.data.store && s.data.store.id);
      if (id) return String(id).trim();
    }}

    // 2. Salla Twilight SDK — uppercase (some versions)
    var S2 = window.Salla;
    if (S2) {{
      var id2 = (S2.store && (S2.store.id || S2.store.merchant_id))
             || (S2.config && typeof S2.config.get === 'function' && S2.config.get('store.id'))
             || (S2.env && (S2.env.storeId || S2.env.store_id));
      if (id2) return String(id2).trim();
    }}

    // 3. salla_config global
    var sc = window.salla_config;
    if (sc) {{
      var id3 = (sc.store && sc.store.id) || sc.store_id || sc.merchant_id;
      if (id3) return String(id3).trim();
    }}

    // 4. window.app or window.store (some Salla themes)
    var app = window.app || window.storeApp;
    if (app && app.store) {{
      var id4 = app.store.id || app.store.merchant_id;
      if (id4) return String(id4).trim();
    }}

    // 5. Meta tags injected by Salla theme
    var meta = document.querySelector(
      'meta[name="salla:store_id"],meta[name="store-id"],' +
      'meta[property="salla:store_id"],meta[name="merchant_id"],' +
      'meta[name="store_id"],meta[name="salla-store-id"]'
    );
    if (meta) {{ var mv = meta.getAttribute('content'); if (mv) return mv; }}

    // 6. Data attributes on <body> or <html>
    var body = document.body || document.documentElement;
    if (body) {{
      var d = body.dataset;
      var id5 = d.storeId || d.sallaStoreId || d.merchantId || d.store || d.salla;
      if (id5) return id5;
      // Salla themes commonly expose the stable store ID as salla-1234567890.
      var themeStoreClass = String(body.className || '').match(/(?:^|\\s)salla-([0-9]+)(?:\\s|$)/);
      if (themeStoreClass) return themeStoreClass[1];
    }}

    // 7. Any element with data-salla-app, data-store-id or data-merchant-id
    var appEl = document.querySelector(
      '[data-salla-app],[data-store-id],[data-merchant-id],[data-store],[data-salla]'
    );
    if (appEl) {{
      var idA = appEl.getAttribute('data-store-id') ||
                appEl.getAttribute('data-merchant-id') ||
                appEl.getAttribute('data-salla-app') ||
                appEl.getAttribute('data-salla');
      if (idA) return idA;
    }}

    // 8. JSON-LD / application/json script tags with store info
    var jsonTags = document.querySelectorAll('script[type="application/json"],script[type="application/ld+json"]');
    for (var i = 0; i < jsonTags.length; i++) {{
      try {{
        var obj = JSON.parse(jsonTags[i].textContent);
        var idJ = (obj.store && obj.store.id) || obj.store_id || obj.merchant_id ||
                  (obj.data && obj.data.store && obj.data.store.id);
        if (idJ) return String(idJ).trim();
      }} catch(e) {{}}
    }}

    // 9. URL params (some Salla previews pass store_id in the URL)
    try {{
      var params = new URLSearchParams(window.location.search);
      var idU = params.get('store_id') || params.get('merchant_id') || params.get('store');
      if (idU) return idU;
    }} catch(e) {{}}

    return null;
  }}

  function _load(id) {{
    id = String(id).trim();
    if (!id || id === 'null' || id === 'undefined') return;
    if (document.querySelector('script[data-nahla-store-bundle]')) return;
    console.log('[Nahla] Loading widget bundle for store_id=' + id);
    var s = document.createElement('script');
    s.src  = API + '/merchant/widgets/salla/' + id + '/nahla-widgets.js';
    s.dataset.nahlaStoreBundle = '';
    s.defer = true;
    s.onerror = function() {{
      console.warn('[Nahla] Widget bundle not found for store_id=' + id);
    }};
    document.head.appendChild(s);
  }}

  function _detect() {{
    var id = _getStoreId();
    console.log('[Nahla] salla-auto.js store_id detected:', id || 'NOT FOUND');
    if (id) {{ _load(id); return true; }}
    return false;
  }}

  // Try immediately (works if SDK already loaded)
  if (_detect()) return;

  // Retry on DOMContentLoaded
  document.addEventListener('DOMContentLoaded', function() {{
    if (_detect()) return;

    // Retry up to 10 times with 300ms intervals waiting for Salla SDK
    var tries = 0;
    var t = setInterval(function() {{
      tries++;
      if (_detect() || tries >= 10) clearInterval(t);
    }}, 300);
  }});
}})();"""
    return Response(content=js, headers=_JS_HEADERS)


@router.get("/merchant/widgets/salla/{salla_store_id}/nahla-widgets.js", include_in_schema=False)
async def serve_widgets_js_by_salla(salla_store_id: str, db: Session = Depends(get_db)):
    """
    Resolve Salla store → tenant, then serve the widget bundle.
    Used by the universal salla-auto.js snippet above.
    Security: a Salla store_id is unique; only its linked tenant's config is returned.
    """
    from models import Integration, MerchantWidget  # noqa: PLC0415

    integration = db.query(Integration).filter(
        Integration.provider == "salla",
        Integration.external_store_id == str(salla_store_id),
    ).first()
    # Compatibility for connections created before external_store_id was filled.
    if integration is None:
        integration = db.query(Integration).filter(
            Integration.provider == "salla",
            Integration.config["store_id"].astext == str(salla_store_id),
        ).first()
    tenant_id = integration.tenant_id if integration else None

    if tenant_id is None:
        logger.warning("[widgets/by-salla] store_id=%s NOT registered — use POST /admin/link-salla-store to link it", salla_store_id)
        return Response(content="/* Nahla: store not registered — store_id=" + str(salla_store_id) + " */", headers=_JS_HEADERS)

    rows = (
        db.query(MerchantWidget)
        .filter(MerchantWidget.tenant_id == tenant_id)
        .all()
    )
    widgets = [
        {
            "widget_key":    r.widget_key,
            "is_enabled":    r.is_enabled,
            "settings":      dict(r.settings_json or {}),
            "display_rules": dict(r.display_rules  or {}),
        }
        for r in rows
    ]
    if not any(w["is_enabled"] for w in widgets):
        return Response(content=_STUB, headers=_JS_HEADERS)

    logger.info("[widgets/by-salla] store=%s tenant=%s", salla_store_id, tenant_id)
    return Response(content=_build_nahla_widgets_js(widgets, tenant_id, db=db), headers=_JS_HEADERS)


# ── Public legacy aliases for the universal loader ───────────────────────────

async def _salla_auto_snippet_content() -> str:
    """Return the salla-auto.js bundle (delegates to main handler)."""
    resp = await serve_salla_auto_snippet()
    return resp.body.decode() if hasattr(resp, "body") else resp.body


@router.get("/salla-auto.js", include_in_schema=False)
async def salla_auto_js_root():
    """Public root-level alias — https://api.nahlah.ai/salla-auto.js"""
    return await serve_salla_auto_snippet()


@router.get("/static/salla-auto.js", include_in_schema=False)
async def salla_auto_js_static():
    """Public /static alias — https://api.nahlah.ai/static/salla-auto.js"""
    return await serve_salla_auto_snippet()
