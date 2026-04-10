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
       ↳ Universal Salla Partner Portal snippet — auto-detects store_id,
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
    SELECT tenant_id FROM integrations
    WHERE provider='salla' AND config->>'store_id' = :salla_store_id;
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
    Cache-Control: public, max-age=60, stale-while-revalidate=300
    CDN caches the JS for up to 60 s. Changes take effect within 1 min.
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
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
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
        "name_ar":        "زر واتساب",
        "description_ar": "زر محادثة واتساب عائم في أسفل المتجر — يفتح محادثة مع التاجر مباشرة",
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
        },
        "default_rules": {
            **_DEFAULT_RULES,
            "trigger":            "scroll",
            "scroll_percent":     20,
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


# ── JS / CSS generators ───────────────────────────────────────────────────────

_STUB = "/* Nahla Widgets — all disabled */"

_JS_HEADERS = {
    "Content-Type":  "application/javascript; charset=utf-8",
    "Cache-Control": "public, max-age=60, stale-while-revalidate=300",
    "X-Robots-Tag":  "noindex",
}

_NAHLA_CDN_LOGO = "data:image/png;base64,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IIQjacr4tOZzd45pb/n64/f+rkfXyr6TVS0R3P/fn3+XglrA4vbbb2/76g9+8vadO/e9c6hQPriqNXFcs0aodcP501HLWIXA1PusWTwpcR1FY2Pjntam3B2LF8z52S+v/e4jUVzLdl66dCnJWS0RXMKfLBwkdePTlO/T/e4LF2/fuefEXbt3Hbu3WDp+tBz6stYbW7zcbsgKfKkZ29K0vKmh8cnxY9vvvfrLV66YOWP6SG1BQ9LdLZLtY0LCf3eF0NX1Z05qSkqO7z7386npB1o5aV4kJ82zctJc606ao8WEeXbCgYevTqd8/or1d5LUQFKek/A/1/rY/ZfPS5culfO6uz1tjDz96EN/2pjxq8aipLUoA8ZgPFfRmMv8pFINBJMnp2oiswLQSUlNQsL/LrqilJRMO+yYm9Xk+daZOC/2Js2zYvwc0z7noMplX7xq+n6BJs8qIeFvcHcHcNJZ5x7TOPNAKybO096UeZEzYa6ddcgxv5dSJjmxCQl/682mtVZOXnzUKjH5AOtMnhfmpy+ybzjjnNOg1i0oeUQJCX8r6oI6+sTTPurPXGTlxAVmwoFHbLbWekmkmSRokvA3ZskSA/D208/8dVPWHREuzJg84TdCiLCrqyvZTiYk/D0yU6QQTDn8iB+1zj/UXnbZNyZCEixJSPi7Bk9ed+qpJxzxhpPvlkKS3LUlJPyd+c53vuNfd911Y+tnt+T8lpCQkJDAv1buZfIUEhISEhISEhISEhISEhISEhISEhISEhISEhISEhISEhISEhISEhISEhISEhISEhISEhISEhISEhISEhISEhISEhISEhISEhISEhISEhISEhISEhISEhISEhISEhISEhISEhISEhISEhISEhISEhISEhISEhISEhISEhISEhISEv6M/wcIj+TuDUq49AAAAABJRU5ErkJggg=="


def _build_nahla_widgets_js(widgets: list[Dict[str, Any]], tenant_id: int = 0) -> str:
    """
    Render the full nahla-widgets.js bundle with tenant config baked in.
    All widget code is self-contained — no additional network requests needed.
    """
    import json  # noqa: PLC0415

    wa      = next((w for w in widgets if w["widget_key"] == "whatsapp_widget"), None)
    popup   = next((w for w in widgets if w["widget_key"] == "discount_popup"), None)
    slide   = next((w for w in widgets if w["widget_key"] == "slide_offer"), None)

    wa_cfg    = {**(wa["settings"]      if wa    else {}), **(wa["display_rules"]    if wa    else {}), "enabled": bool(wa    and wa["is_enabled"])}
    popup_cfg = {**(popup["settings"]   if popup else {}), **(popup["display_rules"] if popup else {}), "enabled": bool(popup and popup["is_enabled"])}
    slide_cfg = {**(slide["settings"]   if slide else {}), **(slide["display_rules"] if slide else {}), "enabled": bool(slide and slide["is_enabled"])}

    if not any([wa_cfg.get("enabled"), popup_cfg.get("enabled"), slide_cfg.get("enabled")]):
        return _STUB

    wa_json    = json.dumps(wa_cfg,    ensure_ascii=False)
    popup_json = json.dumps(popup_cfg, ensure_ascii=False)
    slide_json = json.dumps(slide_cfg, ensure_ascii=False)
    logo       = _NAHLA_CDN_LOGO

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
  if(pages.indexOf('product')>-1&&(p.indexOf('/products/')>-1||p.indexOf('/product/')>-1))return true;
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
      width:110px;height:110px;object-fit:contain;mix-blend-mode:normal;
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
  `);

  var wrap=document.createElement('a');
  wrap.id='nahla-wa';
  wrap.href='https://wa.me/'+c.phone+'?text='+encodeURIComponent(c.message||'');
  wrap.target='_blank';wrap.rel='noopener noreferrer';
  wrap.innerHTML=
    '<img class="nahla-bee" src="'+logo+'" alt="نحلة">'+
    '<div class="nw-circle">'+
      '<span class="nw-orbit o1"></span>'+
      '<span class="nw-orbit o2"></span>'+
      '<span class="nw-orbit o3"></span>'+
      '<span class="nw-orbit o4"></span>'+
      '<img class="nw-icon" src="https://upload.wikimedia.org/wikipedia/commons/6/6b/WhatsApp.svg" alt="واتساب">'+
    '</div>';
  document.body.appendChild(wrap);

  // Logo is pre-processed (transparent PNG as base64) — no canvas removal needed

  function checkShow(){{
    var scrolled=window.scrollY/(document.body.scrollHeight-window.innerHeight||1)*100;
    var pct=c.scroll_percent||20;
    if(c.trigger==='scroll'&&scrolled<pct)return;
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
    var code=N.popupCfg&&N.popupCfg.coupon_code?N.popupCfg.coupon_code:'';
    if(!code){{hide();return;}}
    cta.textContent='⏳ جاري…';cta.disabled=true;
    try{{navigator.clipboard.writeText(code);}}catch(e){{}}
    _applyCode(code,cta);
  }});

  // Redirect to cart with coupon in URL — Salla applies it natively
  function _applyCode(code,btn){{
    btn.textContent='⏳ جاري تطبيق الخصم…';
    btn.disabled=true;
    var lm=window.location.pathname.match(/^\/([a-z]{{2}})\//);
    var base=(lm?'/'+lm[1]:'')+'/cart?coupon='+encodeURIComponent(code);
    setTimeout(function(){{window.location.href=base;}},900);
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
    current.update(body.settings)
    row.settings_json = current
    row.updated_at = datetime.now(timezone.utc)
    db.commit()

    logger.info("[widgets/settings] tenant=%s widget=%s", tenant_id, widget_key)
    return _serialize(row, WIDGET_REGISTRY[widget_key])


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
    salla_token = (integration.config or {}).get("token") if integration else None

    if not salla_token:
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

    js = _build_nahla_widgets_js(widgets, tenant_id)
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
# Configure ONCE in Salla Partner Portal → App → App Snippets:
#   URL: https://api.nahlah.ai/merchant/widgets/salla-auto.js
# After that: every Salla store that installs the Nahla app loads widgets
# automatically.  Enabling / disabling from Nahla takes effect within 60 s.

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
    console.log('[Nahla] Loading widget bundle for store_id=' + id);
    var s = document.createElement('script');
    s.src  = API + '/merchant/widgets/salla/' + id + '/nahla-widgets.js';
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

    integrations = db.query(Integration).filter(Integration.provider == "salla").all()
    tenant_id = None
    for integ in integrations:
        if str((integ.config or {}).get("store_id", "")) == str(salla_store_id):
            tenant_id = integ.tenant_id
            break

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
    return Response(content=_build_nahla_widgets_js(widgets, tenant_id), headers=_JS_HEADERS)


# ── Public aliases for salla-auto.js (configured in Salla Partner Portal) ────
# Salla Partner Portal → App Script URL must point to one of:
#   https://api.nahlah.ai/salla-auto.js          ← preferred (short)
#   https://api.nahlah.ai/static/salla-auto.js   ← legacy alias

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
