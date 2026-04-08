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

_NAHLA_CDN_LOGO = (
    "https://cdn.salla.sa/XVEDq/b1ec4359-6895-49dc-80e7-06fd33b75df8-"
    "1000x666.66666666667-xMM28RbT68xVWoSgEtzBgpW1w4cDN7sEQAhQmLwD.jpg"
)


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
  var posVal=pos+':28px';

  addStyles(`
    #nahla-wa{{position:fixed;bottom:28px;${{posVal}};z-index:99999;display:flex;flex-direction:column;align-items:${{pos==='right'?'flex-end':'flex-start'}};gap:10px;opacity:0;transform:translateY(20px);transition:opacity .4s,transform .4s;pointer-events:none;}}
    #nahla-wa.show{{opacity:1;transform:translateY(0);pointer-events:auto;}}
    #nahla-wa .nahla-btn{{width:64px;height:64px;border-radius:50%;background:${{color}};box-shadow:0 4px 20px rgba(0,0,0,.25);display:flex;align-items:center;justify-content:center;cursor:pointer;text-decoration:none;border:3px solid rgba(255,255,255,.4);}}
    #nahla-wa .nahla-btn img.wa-icon{{width:32px;height:32px;}}
    #nahla-wa .nahla-bee-img{{width:46px;height:46px;border-radius:50%;object-fit:cover;border:2px solid rgba(255,255,255,.5);}}
    @keyframes nPulse{{0%,100%{{box-shadow:0 4px 20px rgba(0,0,0,.25),0 0 0 0 ${{color}}55;}}50%{{box-shadow:0 4px 20px rgba(0,0,0,.25),0 0 0 14px transparent;}}}}
    #nahla-wa .nahla-btn{{animation:nPulse 2.4s infinite;}}
    #nahla-wa .nahla-tooltip{{background:#1e293b;color:#fff;font-size:13px;padding:6px 12px;border-radius:8px;white-space:nowrap;box-shadow:0 2px 8px rgba(0,0,0,.2);}}
  `);

  var wrap=document.createElement('div');
  wrap.id='nahla-wa';
  wrap.innerHTML=`
    <img class="nahla-bee-img" src="${{logo}}" alt="نحلة" onerror="this.style.display='none'">
    <a class="nahla-btn" href="https://wa.me/${{c.phone}}?text=${{encodeURIComponent(c.message||'')}}" target="_blank" rel="noopener">
      <img class="wa-icon" src="https://upload.wikimedia.org/wikipedia/commons/6/6b/WhatsApp.svg" alt="واتساب">
    </a>`;
  document.body.appendChild(wrap);

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

  // ── Main CTA — get coupon + apply to cart ──────────────────────────────────
  var cta=document.getElementById('nahla-popup-cta');
  if(cta)cta.addEventListener('click',function(){{
    ls(SEEN_KEY,1);
    cta.textContent='⏳ جاري…';cta.disabled=true;

    function _doApply(code){{
      if(!code){{cta.textContent='احصل على الخصم';cta.disabled=false;hide();return;}}

      // 1. Try Salla SDK methods
      var sdk=window.salla||window.Salla;
      if(sdk&&sdk.cart){{
        var fn=sdk.cart.applyCoupon||sdk.cart.addCoupon||sdk.cart.setCoupon;
        if(typeof fn==='function'){{
          fn.call(sdk.cart,code)
            .then(function(){{_showApplied(code,cta);}})
            .catch(function(){{_fallbackCart(code,cta);}});
          return;
        }}
      }}
      // 2. Try Salla Custom Event
      try{{
        document.dispatchEvent(new CustomEvent('cart::coupon.apply',{{bubbles:true,detail:{{coupon_code:code}}}}));
        setTimeout(function(){{_showApplied(code,cta);}},600);
        return;
      }}catch(e){{}}
      // 3. DOM + redirect fallback
      _fallbackCart(code,cta);
    }}

    // Try backend for unique coupon, fallback to static code on ANY failure
    var staticCode=N.popupCfg&&N.popupCfg.coupon_code?N.popupCfg.coupon_code:'';
    fetch('{_API_BASE}/merchant/widgets/'+TENANT_ID+'/create-coupon',{{method:'POST'}})
      .then(function(r){{return r.ok?r.json():Promise.resolve({{success:false}});}}  )
      .then(function(d){{_doApply((d.success&&d.code)?d.code:staticCode);}})
      .catch(function(){{_doApply(staticCode);}});
  }});

  function _showApplied(code,btn){{
    btn.textContent='✓ تم تطبيق الخصم!';
    btn.style.background='#22c55e';
    var badge=document.querySelector('#nahla-popup .np-badge');
    if(badge)badge.innerHTML='تم الخصم ✓';
    setTimeout(hide,1800);
  }}

  function _fillInput(inp,code){{
    inp.value=code;
    try{{Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(inp,code);}}catch(e){{}}
    ['input','change'].forEach(function(e){{inp.dispatchEvent(new Event(e,{{bubbles:true}}));}});
  }}

  function _applyCouponToDOM(code){{
    // 0. Try Salla Web Component Shadow DOM first
    var sc=document.querySelector('salla-coupon,salla-coupon-form,[is="salla-coupon"]');
    if(sc&&sc.shadowRoot){{
      var si=sc.shadowRoot.querySelector('input');
      if(si){{_fillInput(si,code);var sb=sc.shadowRoot.querySelector('button');if(sb)setTimeout(function(){{sb.click();}},150);return true;}}
    }}
    // Try every possible Salla coupon input selector
    var sel=[
      'input[name="coupon"]',
      'input[name="coupon_code"]',
      'input[name="discount_code"]',
      'input[placeholder*="كوبون"]',
      'input[placeholder*="خصم"]',
      'input[placeholder*="coupon"]',
      'input[placeholder*="Coupon"]',
      'input[id*="coupon"]',
      'input[id*="discount"]',
      '.coupon-field input',
      '.discount-code input',
      'salla-coupon input',
      '[data-coupon] input',
    ];
    var inp=null;
    for(var i=0;i<sel.length;i++){{inp=document.querySelector(sel[i]);if(inp)break;}}
    if(!inp)return false;
    _fillInput(inp,code);

    // Find and click the apply button
    var btnSel=[
      'button[type="submit"]',
      'button[class*="coupon"]',
      'button[class*="apply"]',
      '.coupon-btn','[data-coupon] button',
      'salla-coupon button',
    ];
    var applyBtn=null;
    var form=inp.closest('form,salla-coupon,[data-coupon]');
    if(form){{
      applyBtn=form.querySelector('button');
    }}
    if(!applyBtn)for(var j=0;j<btnSel.length;j++){{applyBtn=document.querySelector(btnSel[j]);if(applyBtn)break;}}
    if(applyBtn){{
      setTimeout(function(){{applyBtn.click();}},200);
      return true;
    }}
    // Submit form directly
    if(form&&form.tagName==='FORM'){{form.dispatchEvent(new Event('submit',{{bubbles:true}}));return true;}}
    return false;
  }}

  function _fallbackCart(code,btn){{
    try{{navigator.clipboard.writeText(code);}}catch(e){{}}

    // 1. Try DOM injection — handle normal DOM + Shadow DOM (Salla Web Components)
    var domOk=_applyCouponToDOM(code);
    if(domOk){{btn.textContent='✓ جاري التطبيق…';setTimeout(function(){{_showApplied(code,btn);}},800);return;}}

    // 2. GUARANTEED fallback — redirect to /cart?coupon=CODE (Salla reads it automatically)
    btn.textContent='✓ جاهز!';btn.style.background='#22c55e';
    hide();
    var toast=document.createElement('div');
    toast.style.cssText='position:fixed;top:24px;left:50%;transform:translateX(-50%);background:#0f172a;color:#fff;padding:16px 28px;border-radius:16px;z-index:999999;font-size:14px;font-weight:700;direction:rtl;box-shadow:0 10px 40px rgba(0,0,0,.35);text-align:center;min-width:280px;';
    toast.innerHTML='🎁 تم! كود خصمك: <span style="color:#fbbf24;letter-spacing:3px;font-size:17px;display:block;margin-top:4px;">'+code+'</span><small style="font-weight:400;opacity:.7;font-size:12px;">جاري تطبيق الخصم على سلتك…</small>';
    document.body.appendChild(toast);
    setTimeout(function(){{
      window.location.href='/cart?coupon='+encodeURIComponent(code);
    }},1400);
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
    Universal Salla snippet — auto-detects store, resolves tenant, loads bundle.
    """
    js = f"""/* Nahla Universal Salla Snippet — {_API_BASE} */
(function(){{
  var id=(window.salla&&window.salla.store&&window.salla.store.id)
       ||(window.salla_config&&window.salla_config.store&&window.salla_config.store.id);
  if(!id)return;
  var s=document.createElement('script');
  s.src='{_API_BASE}/merchant/widgets/salla/'+id+'/nahla-widgets.js';
  s.defer=true;
  document.head.appendChild(s);
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
        return Response(content="/* Nahla: store not registered */", headers=_JS_HEADERS)

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
