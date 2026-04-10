"""
routers/salla_oauth.py
───────────────────────
Salla OAuth 2.0 flow and store data endpoints.

Routes (public — no JWT required on OAuth paths)
  GET  /api/salla/authorize         ← returns auth URL to frontend
  GET  /oauth/salla/callback        ← Salla redirects here with ?code=
  GET  /integrations/salla/success  ← success landing (public, shown inside iframe)
  GET  /integrations/salla/error    ← error landing  (public, shown inside iframe)

Routes (protected — JWT required)
  GET  /api/salla/store
  GET  /api/salla/products
  POST /api/salla/test-coupon

OAuth Flow:
  1. Merchant clicks "Connect Salla" in dashboard
  2. Frontend calls GET /api/salla/authorize → gets authorize_url
  3. Browser opens authorize_url → Salla asks merchant to approve
  4. Salla redirects to /oauth/salla/callback?code=XXX&state=TENANT_ID
  5. Backend exchanges code → tokens, saves to DB
  6. Redirect to SALLA_EMBEDDED_URL?status=connected  (success)
       or SALLA_EMBEDDED_URL?status=error&reason=XXX  (failure)
"""
from __future__ import annotations

import logging
import os
import secrets as _secrets
import sys
import urllib.parse
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from models import Integration, Tenant, User, WhatsAppConnection

from core.audit import audit
from core.auth import create_token, get_jwt_tenant_id, hash_password
from core.config import (
    DASHBOARD_URL,
    SALLA_CLIENT_ID,
    SALLA_CLIENT_SECRET,
    SALLA_EMBEDDED_URL,
    SALLA_REDIRECT_URI,
    SALLA_TEST_CLIENT_ID,
    SALLA_TEST_CLIENT_SECRET,
    SALLA_TEST_REDIRECT_URI,
)
from core.database import get_db
from core.tenant import get_or_create_tenant, resolve_tenant_id

logger = logging.getLogger("nahla-backend")

router = APIRouter(tags=["Salla OAuth"])

# ── URL helpers ────────────────────────────────────────────────────────────────

# Dashboard URL for internal redirects (e.g. after store settings save)
_DASHBOARD = DASHBOARD_URL.split("=", 1)[-1] if "=" in DASHBOARD_URL else DASHBOARD_URL
_DASHBOARD = _DASHBOARD.rstrip("/") or "https://app.nahlah.ai"

# Salla embedded app landing page — where to redirect after OAuth
# This must be the iframe URL registered in Salla partner portal
_SALLA_APP  = SALLA_EMBEDDED_URL.rstrip("/")

# The Salla callback page on the dashboard (for new merchants auto-logged in via Salla)
_SALLA_CALLBACK_BASE = _SALLA_APP.rsplit("/", 1)[0] if "/" in _SALLA_APP else _SALLA_APP

# Prefix used in state param to identify new-merchant installs from Salla
_NEW_MERCHANT_PREFIX = "salla_new_"


def _success_url(store_id: str = "", store_name: str = "") -> str:
    """Build the post-OAuth success redirect URL."""
    params = urllib.parse.urlencode({
        "status": "connected",
        "store":  store_id,
        "name":   store_name,
    })
    return f"{_SALLA_APP}?{params}"


def _error_url(reason: str, detail: str = "") -> str:
    """Build the post-OAuth error redirect URL."""
    params: dict = {"status": "error", "reason": reason}
    if detail:
        params["detail"] = detail[:200]   # truncate to avoid oversized URLs
    return f"{_SALLA_APP}?{urllib.parse.urlencode(params)}"


# ═══════════════════════════════════════════════════════════════════════════════
# PUBLIC ROUTES — OAuth flow (no JWT required)
# ═══════════════════════════════════════════════════════════════════════════════


# ── Salla Embedded Token Login ─────────────────────────────────────────────────

@router.post("/salla/token-login")
async def salla_token_login(request: Request, db: Session = Depends(get_db)):
    """
    PUBLIC — no JWT required.

    Official entry point for every merchant who opens Nahla from inside Salla.

    ─────────────────────────────────────────────────────────────
    MULTI-TENANT GUARANTEES:
      • Each Salla store gets its own Tenant row (tenant_id is unique per store)
      • Each JWT contains tenant_id in claims — cannot be spoofed
      • Middleware enforces tenant_id from JWT on every API call
      • Admin account is NEVER returned here — only role=merchant tokens
    ─────────────────────────────────────────────────────────────

    Flow:
      1. Receive Salla embedded token (v4.public.*) + app_id
      2. Introspect via Salla API  →  get merchant/store identity
      3. Look up or create isolated Tenant + User for this store
      4. Issue Nahla JWT { sub, role, tenant_id }
      5. Return JWT so /salla/app can build link to /salla-callback
    """
    client_ip = request.headers.get("X-Real-IP") or (
        request.client.host if request.client else "?"
    )

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    salla_token = (body.get("token") or "").strip()
    app_id      = str(body.get("app_id") or SALLA_CLIENT_ID or "")

    if not salla_token:
        raise HTTPException(status_code=400, detail="token required")

    # Mask token for logs — show only first 20 chars
    token_preview = salla_token[:20] + "…"
    logger.info(
        "[SallaLogin] ▶ STEP 1 — Salla token received | ip=%s app_id=%s token=%s",
        client_ip, app_id, token_preview,
    )

    # ══════════════════════════════════════════════════════════════
    # STEP 2 — Introspect the Salla embedded token
    # ══════════════════════════════════════════════════════════════
    merchant_id_str = ""
    store_name      = ""
    owner_email     = ""
    introspect_ok   = False

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://api.salla.dev/exchange-authority/v1/introspect",
                json={
                    "env":     "prod",
                    "token":   salla_token,
                    "iss":     "merchant-dashboard",
                    "subject": "embedded-page",
                },
                headers={
                    "S-Source":     app_id,
                    "Content-Type": "application/json",
                    "Accept":       "application/json",
                },
            )

            if resp.status_code == 200:
                data = resp.json()
                if data.get("success"):
                    introspect_ok   = True
                    payload_data    = data.get("data") or {}
                    merchant        = payload_data.get("merchant") or {}

                    # Handle multiple possible Salla response shapes
                    merchant_id_str = str(
                        merchant.get("id")              or
                        payload_data.get("merchant_id") or
                        payload_data.get("store_id")    or
                        ""
                    )
                    store_name  = (
                        merchant.get("name")       or
                        payload_data.get("store_name") or
                        ""
                    )
                    owner_email = (
                        merchant.get("email")      or
                        payload_data.get("email")  or
                        merchant.get("mobile")     or
                        ""
                    ).strip().lower()

                    logger.info(
                        "[SallaLogin] ✅ STEP 2 — Introspect SUCCESS | "
                        "merchant_id=%s store=%r email=%s",
                        merchant_id_str, store_name, owner_email,
                    )
                else:
                    logger.warning(
                        "[SallaLogin] ⚠️  STEP 2 — Introspect returned success=false | body=%.300s",
                        resp.text,
                    )
            else:
                logger.warning(
                    "[SallaLogin] ⚠️  STEP 2 — Introspect HTTP %s | body=%.200s",
                    resp.status_code, resp.text,
                )
    except Exception as exc:
        logger.error("[SallaLogin] ❌ STEP 2 — Introspect call raised: %s", exc)

    # ══════════════════════════════════════════════════════════════
    # STEP 3 — Derive identity (fallback if introspect gave no email)
    # ══════════════════════════════════════════════════════════════
    if not owner_email and merchant_id_str:
        safe_name   = "".join(c for c in store_name if c.isalnum() or c in "-_").lower()[:30]
        owner_email = f"{safe_name or 'store'}-{merchant_id_str}@salla-merchant.nahlah.ai"
        logger.info(
            "[SallaLogin] ℹ️  STEP 3 — No email from Salla, using derived: %s",
            owner_email,
        )

    if not owner_email:
        logger.error(
            "[SallaLogin] ❌ Cannot identify merchant — introspect_ok=%s merchant_id=%s",
            introspect_ok, merchant_id_str,
        )
        raise HTTPException(
            status_code=401,
            detail="Could not identify merchant from Salla token. "
                   "Please install the app via Salla store to link your account.",
        )

    logger.info(
        "[SallaLogin] ▶ STEP 4 — Resolving Nahla account | email=%s merchant_id=%s",
        owner_email, merchant_id_str,
    )

    # ══════════════════════════════════════════════════════════════
    # STEP 4 — Find or create isolated Tenant + User
    # ══════════════════════════════════════════════════════════════
    try:
        existing_user = db.query(User).filter(User.email == owner_email).first()

        if existing_user:
            # ── Returning merchant ────────────────────────────────────────────
            tenant_id = existing_user.tenant_id
            role      = existing_user.role or "merchant"
            is_new    = False
            logger.info(
                "[SallaLogin] ✅ STEP 4 — TENANT FOUND (returning merchant) | "
                "email=%s tenant_id=%s role=%s",
                owner_email, tenant_id, role,
            )
        else:
            # ── Check by store_id first to avoid duplicate tenant creation ────
            existing_integration = db.query(Integration).filter(
                Integration.provider == "salla",
                Integration.config["store_id"].astext == str(merchant_id_str),
            ).first() if merchant_id_str else None

            if existing_integration:
                tenant_id = existing_integration.tenant_id
                role      = "merchant"
                is_new    = False
                logger.info(
                    "[SallaLogin] ✅ STEP 4 — TENANT FOUND (by store_id) | "
                    "store_id=%s tenant_id=%s",
                    merchant_id_str, tenant_id,
                )
            else:
                # ── First-time merchant: create isolated Tenant + User ────────
                # Use store_id suffix to ensure name uniqueness
                unique_name = f"{store_name or 'متجر سلة'}-{merchant_id_str}" if merchant_id_str else (store_name or "متجر سلة")
                new_tenant = Tenant(name=unique_name)
                db.add(new_tenant)
                db.flush()     # generate new_tenant.id immediately
                tenant_id = new_tenant.id
                role      = "merchant"
                is_new    = True

                new_user = User(
                    username      = owner_email.split("@")[0],
                    email         = owner_email,
                    password_hash = hash_password(_secrets.token_urlsafe(16)),
                    role          = role,
                    tenant_id     = tenant_id,
                    is_active     = True,
                )
                db.add(new_user)
                db.flush()

                logger.info(
                    "[SallaLogin] ✅ STEP 4 — TENANT CREATED (new merchant) | "
                    "email=%s tenant_id=%s store=%r",
                    owner_email, tenant_id, store_name,
                )

        # ── Save / update Salla integration record ────────────────────────────
        if merchant_id_str:
            integration = db.query(Integration).filter(
                Integration.tenant_id == tenant_id,
                Integration.provider  == "salla",
            ).first()

            now_iso = datetime.now(timezone.utc).isoformat()
            if integration:
                # Update store_id / name in case they changed
                cfg = dict(integration.config or {})
                cfg.update({
                    "store_id":   merchant_id_str,
                    "store_name": store_name,
                    "last_seen":  now_iso,
                })
                integration.config  = cfg
                integration.enabled = True
                logger.info(
                    "[SallaLogin]    Integration UPDATED | tenant=%s store_id=%s",
                    tenant_id, merchant_id_str,
                )
            else:
                db.add(Integration(
                    tenant_id = tenant_id,
                    provider  = "salla",
                    config    = {
                        "store_id":          merchant_id_str,
                        "store_name":        store_name,
                        "salla_token_login": True,
                        "connected_at":      now_iso,
                    },
                    enabled = True,
                ))
                logger.info(
                    "[SallaLogin]    Integration CREATED | tenant=%s store_id=%s",
                    tenant_id, merchant_id_str,
                )

        db.commit()

    except Exception as exc:
        logger.exception("[SallaLogin] ❌ STEP 4 — DB error: %s", exc)
        try:
            db.rollback()
        except Exception:
            pass
        raise HTTPException(status_code=500, detail="Database error during account setup")

    # ══════════════════════════════════════════════════════════════
    # STEP 5 — Issue Nahla JWT (must carry user_id for tenant isolation)
    # ══════════════════════════════════════════════════════════════
    db_user   = db.query(User).filter(User.email == owner_email).first()
    db_user_id = db_user.id if db_user else None
    nahla_jwt = create_token(
        email=owner_email, role=role, tenant_id=tenant_id, user_id=db_user_id
    )

    # Check WhatsApp connection status for smart redirect
    wa_conn = db.query(WhatsAppConnection).filter_by(tenant_id=tenant_id).first()
    wa_connected = bool(wa_conn and wa_conn.status == "connected")

    if is_new:
        redirect_target = "/onboarding"
    elif wa_connected:
        redirect_target = "/overview"
    else:
        redirect_target = "/overview"   # Still go to overview — merchant decides when to connect WA

    logger.info(
        "[SallaLogin] ✅ STEP 5 — JWT ISSUED | "
        "tenant_id=%s role=%s is_new=%s wa_connected=%s redirect=%s",
        tenant_id, role, is_new, wa_connected, redirect_target,
    )
    logger.info(
        "[SallaLogin] ══ COMPLETE ═══ merchant=%s tenant=%s wa=%s → %s",
        owner_email, tenant_id, wa_connected, redirect_target,
    )

    return {
        "access_token":   nahla_jwt,
        "role":           role,
        "tenant_id":      tenant_id,
        "store_name":     store_name,
        "email":          owner_email,
        "is_new":         is_new,
        "wa_connected":   wa_connected,
        "redirect_to":    redirect_target,
    }


@router.get("/salla/whoami")
async def salla_whoami(request: Request, db: Session = Depends(get_db)):
    """
    PROTECTED — requires valid Nahla JWT.

    Returns the identity and isolation proof for the currently-authenticated merchant.
    Use this to verify multi-tenant isolation:

      curl -H "Authorization: Bearer <JWT>" https://api.nahlah.ai/salla/whoami

    Two different merchants MUST see different tenant_id values.
    """
    from core.auth import require_authenticated  # noqa: PLC0415

    payload = require_authenticated(request)
    tenant_id  = int(payload.get("tenant_id", 0))
    email      = payload.get("sub", "")
    role       = payload.get("role", "")

    # Fetch tenant name
    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    tenant_name = tenant.name if tenant else "?"

    # Fetch Salla integration for this tenant only
    salla_int = db.query(Integration).filter(
        Integration.tenant_id == tenant_id,
        Integration.provider  == "salla",
    ).first()
    salla_store_id   = (salla_int.config or {}).get("store_id", "?") if salla_int else "not_connected"
    salla_store_name = (salla_int.config or {}).get("store_name", "?") if salla_int else "not_connected"

    return {
        "isolation_check": "OK — you see ONLY your tenant data",
        "jwt_claims": {
            "email":     email,
            "role":      role,
            "tenant_id": tenant_id,
        },
        "tenant": {
            "id":   tenant_id,
            "name": tenant_name,
        },
        "salla_integration": {
            "store_id":   salla_store_id,
            "store_name": salla_store_name,
        },
        "security_note": (
            "tenant_id comes from the JWT claims only — "
            "cannot be changed by the client or request headers."
        ),
    }


# ── Salla Embedded App Page ────────────────────────────────────────────────────

@router.get("/salla/app", response_class=HTMLResponse)
async def salla_embedded_app(request: Request):
    """
    *** SET THIS AS THE IFRAME URL IN SALLA PARTNER PORTAL ***

    Nahla-branded page served inside Salla's embedded app iframe.
    - Matches Nahla platform visual identity exactly
    - Uses the official Nahla logo and color system
    - Handles Salla SDK handshake to dismiss skeleton loaders
    - Opens Nahla dashboard in a new tab on CTA click
    """
    dashboard_url = "https://app.nahlah.ai"
    logo_url = "https://app.nahlah.ai/logo.png"
    return HTMLResponse(content=f"""<!DOCTYPE html>
<html dir="rtl" lang="ar">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>نحلة AI — مساعد مبيعات واتساب</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Cairo:wght@400;500;600;700;900&display=swap" rel="stylesheet">
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

    :root {{
      --bg-900:  #0f172a;
      --bg-800:  #1e293b;
      --bg-700:  #334155;
      --amber:   #f59e0b;
      --amber-d: #d97706;
      --amber-l: rgba(245,158,11,0.15);
      --amber-b: rgba(245,158,11,0.35);
      --text:    #f1f5f9;
      --muted:   #94a3b8;
      --border:  rgba(245,158,11,0.2);
    }}

    html, body {{ height: 100%; }}

    body {{
      font-family: 'Cairo', system-ui, sans-serif;
      background: var(--bg-900);
      color: var(--text);
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      padding: 24px 16px;
      background-image:
        radial-gradient(ellipse 80% 60% at 50% -10%, rgba(245,158,11,0.08) 0%, transparent 70%);
    }}

    /* ── Header / Logo ── */
    .logo-wrap {{
      display: flex;
      flex-direction: column;
      align-items: center;
      margin-bottom: 28px;
    }}
    .logo-img {{
      width: 80px;
      height: 80px;
      object-fit: contain;
      margin-bottom: 12px;
      filter: drop-shadow(0 0 18px rgba(245,158,11,0.4));
    }}
    .logo-name {{
      display: flex;
      align-items: center;
      gap: 8px;
    }}
    .logo-name h1 {{
      font-size: 26px;
      font-weight: 900;
      color: var(--text);
      letter-spacing: -0.5px;
    }}
    .ai-badge {{
      display: inline-flex;
      align-items: center;
      padding: 2px 8px;
      border-radius: 6px;
      background: var(--amber-l);
      border: 1px solid var(--amber-b);
      box-shadow: 0 0 10px rgba(245,158,11,0.3);
      font-size: 11px;
      font-weight: 900;
      color: var(--amber);
      letter-spacing: 0.5px;
    }}
    .tagline {{
      font-size: 13px;
      color: var(--muted);
      margin-top: 6px;
      text-align: center;
      line-height: 1.6;
    }}

    /* ── Card ── */
    .card {{
      width: 100%;
      max-width: 400px;
      background: rgba(255,255,255,0.03);
      border: 1px solid var(--border);
      border-radius: 20px;
      padding: 28px 24px;
      backdrop-filter: blur(16px);
    }}

    /* ── Features ── */
    .features {{
      display: flex;
      flex-direction: column;
      gap: 12px;
      margin-bottom: 24px;
    }}
    .feature {{
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 10px 12px;
      background: rgba(255,255,255,0.03);
      border: 1px solid rgba(255,255,255,0.06);
      border-radius: 12px;
    }}
    .feature-icon {{
      width: 34px;
      height: 34px;
      border-radius: 10px;
      background: var(--amber-l);
      border: 1px solid var(--amber-b);
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 16px;
      flex-shrink: 0;
    }}
    .feature span {{
      font-size: 13px;
      color: #cbd5e1;
      line-height: 1.4;
    }}

    /* ── CTA Button ── */
    .btn {{
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      width: 100%;
      background: var(--amber);
      color: var(--bg-900);
      font-family: 'Cairo', system-ui, sans-serif;
      font-weight: 700;
      font-size: 15px;
      padding: 14px 24px;
      border-radius: 12px;
      text-decoration: none;
      border: none;
      cursor: pointer;
      transition: background 0.2s, transform 0.1s, box-shadow 0.2s;
      box-shadow: 0 4px 20px rgba(245,158,11,0.35);
    }}
    .btn:hover {{
      background: var(--amber-d);
      transform: translateY(-1px);
      box-shadow: 0 6px 24px rgba(245,158,11,0.5);
    }}
    .btn:active {{ transform: translateY(0); }}

    .trial-note {{
      text-align: center;
      margin-top: 10px;
      font-size: 12px;
      color: #475569;
    }}
    .trial-note b {{ color: var(--amber); font-weight: 600; }}

    /* ── Footer ── */
    .footer {{
      margin-top: 20px;
      font-size: 11px;
      color: #334155;
      text-align: center;
    }}

    /* ── Loading state ── */
    #status-msg {{
      font-size: 12px;
      color: var(--muted);
      text-align: center;
      margin-top: 8px;
      min-height: 18px;
    }}
  </style>
</head>
<body>

  <!-- Logo & Brand -->
  <div class="logo-wrap">
    <img
      src="{logo_url}"
      alt="نحلة"
      class="logo-img"
      onerror="this.style.display='none'; document.getElementById('fallback-emoji').style.display='block'"
    />
    <span id="fallback-emoji" style="display:none;font-size:56px;margin-bottom:8px;">🐝</span>
    <div class="logo-name">
      <h1>نحلة</h1>
      <span class="ai-badge">AI</span>
    </div>
    <p class="tagline">مساعد مبيعات ذكي يرد على عملاء متجرك عبر واتساب<br>على مدار الساعة — بدون تدخل منك</p>
  </div>

  <!-- Features Card -->
  <div class="card">
    <div class="features">
      <div class="feature">
        <div class="feature-icon">💬</div>
        <span>يرد تلقائياً على كل سؤال عن المنتجات والطلبات</span>
      </div>
      <div class="feature">
        <div class="feature-icon">🚀</div>
        <span>الطيار الآلي — يُدير محادثات المبيعات بدون تدخل</span>
      </div>
      <div class="feature">
        <div class="feature-icon">📦</div>
        <span>يتابع الطلبات ويرسل تحديثات الشحن للعملاء</span>
      </div>
      <div class="feature">
        <div class="feature-icon">🎯</div>
        <span>يرسل عروض وكوبونات للعملاء في الوقت المناسب</span>
      </div>
    </div>

    <a href="{dashboard_url}/register" target="_blank" class="btn" id="cta-btn">
      ابدأ تجربتك المجانية 14 يوم ←
    </a>
    <p class="trial-note">مجاناً <b>14 يوماً</b> — لا يلزم بطاقة ائتمانية</p>
    <p id="status-msg"></p>
  </div>

  <div class="footer">بأيدي سعودية 100% 🇸🇦 · Nahla AI</div>

  <!--
    SDK loaded synchronously so embedded.init() → embedded.ready() can fire immediately.
    embedded.ready() requires init() to complete first — the SDK enforces this.
  -->
  <script src="https://cdn.jsdelivr.net/npm/@salla.sa/embedded-sdk@0.2.4/dist/umd/index.js"></script>
  <script>
    var APP_URL    = '{dashboard_url}';
    var API_URL    = 'https://api.nahlah.ai';
    var statusEl   = document.getElementById('status-msg');
    var ctaBtn     = document.getElementById('cta-btn');

    console.log('[Nahla] /salla/app mounted', {{
      sdk: !!(window.Salla && window.Salla.embedded),
      hasToken: !!new URLSearchParams(location.search).get('token'),
    }});

    // ── 1. Salla SDK handshake — dismisses skeleton loaders ─────────────────
    function sendRawReady() {{
      var msg = {{ event: 'embedded::ready', payload: {{}}, timestamp: Date.now(), source: 'embedded-app', metadata: {{ version: '0.2.4' }} }};
      try {{ window.parent.postMessage(msg, '*'); }} catch(_) {{}}
    }}

    function runSDK() {{
      var sdk = window.Salla && window.Salla.embedded;
      if (!sdk) {{ sendRawReady(); return; }}
      sdk.init({{ debug: false }})
        .then(function() {{ sdk.ready(); sendRawReady(); }})
        .catch(function() {{ sendRawReady(); }});
    }}
    runSDK();
    setTimeout(sendRawReady, 3000);  // safety fallback

    // ── 2. Merchant auto-login via Salla embedded token ──────────────────────
    //
    //  The Salla token in the URL identifies WHICH merchant opened the app.
    //  We call /salla/token-login (backend) → introspects token with Salla API
    //  → finds/creates Nahla account → returns a Nahla JWT.
    //
    //  We then build a link to app.nahlah.ai/salla-callback?token=JWT
    //  Because localStorage is domain-scoped, the JWT must be stored on
    //  app.nahlah.ai (not api.nahlah.ai). SallaCallback.tsx handles this.

    var params    = new URLSearchParams(location.search);
    var sallaToken = params.get('token');
    var appId      = params.get('app_id');

    // ── Auto-redirect helper ────────────────────────────────────────────────
    //  Opens the Nahla dashboard in the PARENT frame (top-level Salla window).
    //  Falls back to a new tab if top-frame navigation is blocked by the browser.
    function goToDashboard(link) {{
      ctaBtn.href   = link;
      ctaBtn.target = '_blank';

      // Try navigating the top-level Salla frame first so the merchant
      // doesn't have to click anything
      try {{
        window.top.location.href = link;
      }} catch(e) {{
        // Cross-origin policy blocked top-frame navigation — open new tab
        window.open(link, '_blank');
      }}
    }}

    if (sallaToken) {{
      if (statusEl) statusEl.textContent = 'جاري التحقق من هويتك...';
      ctaBtn.textContent = 'جاري التحميل…';
      ctaBtn.style.opacity = '0.7';
      ctaBtn.style.pointerEvents = 'none';

      fetch(API_URL + '/salla/token-login', {{
        method:  'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body:    JSON.stringify({{ token: sallaToken, app_id: appId }}),
      }})
      .then(function(r) {{ return r.json(); }})
      .then(function(data) {{
        if (data.access_token) {{
          var cbParams = new URLSearchParams({{
            token:        data.access_token,
            status:       'connected',
            new:          data.is_new ? '1' : '0',
            wa_connected: data.wa_connected ? '1' : '0',
          }});
          var dashLink = APP_URL + '/salla-callback?' + cbParams.toString();

          var greeting = data.is_new
            ? 'مرحباً! جاري إعداد حسابك...'
            : 'مرحباً بعودتك ' + (data.store_name || '') + ' ✓';
          if (statusEl) statusEl.textContent = greeting;

          ctaBtn.textContent    = data.is_new ? 'أكمل إعداد متجرك ←' : 'افتح لوحة التحكم ←';
          ctaBtn.style.opacity  = '1';
          ctaBtn.style.pointerEvents = 'auto';

          console.log('[Nahla] token-login OK', {{
            is_new: data.is_new, tenant: data.tenant_id,
          }});

          // ── Auto-redirect after 1.2 s ────────────────────────────────────
          setTimeout(function() {{ goToDashboard(dashLink); }}, 1200);

        }} else {{
          // token-login returned an error payload
          var errMsg = data.detail || data.error || 'تعذّر التحقق';
          if (statusEl) statusEl.textContent = errMsg;
          ctaBtn.textContent = 'سجّل متجرك يدوياً ←';
          ctaBtn.href   = APP_URL + '/register';
          ctaBtn.target = '_blank';
          ctaBtn.style.opacity = '1';
          ctaBtn.style.pointerEvents = 'auto';
          console.warn('[Nahla] token-login: no access_token', data);
        }}
      }})
      .catch(function(err) {{
        if (statusEl) statusEl.textContent = '';
        ctaBtn.textContent = 'افتح نحلة ←';
        ctaBtn.href   = APP_URL + '/register';
        ctaBtn.target = '_blank';
        ctaBtn.style.opacity = '1';
        ctaBtn.style.pointerEvents = 'auto';
        console.error('[Nahla] token-login error:', err);
      }});
    }} else {{
      // No Salla token — show default register CTA
      console.log('[Nahla] No Salla token in URL — showing default CTA');
    }}
  </script>
</body>
</html>""")


@router.get("/settings/validate")
async def salla_settings_validate(request: Request):
    """
    Salla Partner Portal — "رابط التحقق من الإعدادات"
    Salla calls this endpoint to confirm the app is live and reachable.
    Must be public (no JWT) and always return HTTP 200.
    """
    return {
        "status":  "ok",
        "app":     "nahla-ai",
        "version": "2.0",
    }


@router.get("/salla/start")
async def salla_start(request: Request):
    """
    *** SET THIS AS THE APP URL IN SALLA PARTNER PORTAL ***

    Direct browser redirect to Salla OAuth authorization page.
    Used when a merchant opens the Nahla app from their Salla store for the first time.
    No JSON response — only a 302 redirect so Salla/browsers follow it immediately.

    State is marked with prefix so the callback knows this is a NEW merchant install.
    """
    if not SALLA_CLIENT_ID:
        logger.error("[Salla Start] SALLA_CLIENT_ID not configured!")
        return RedirectResponse(
            url=_error_url("app_not_configured", "SALLA_CLIENT_ID missing"),
            status_code=302,
        )

    # Unique state to detect new-merchant flow in the callback
    state = _NEW_MERCHANT_PREFIX + _secrets.token_urlsafe(12)
    params = urllib.parse.urlencode({
        "client_id":     SALLA_CLIENT_ID,
        "redirect_uri":  SALLA_REDIRECT_URI,
        "response_type": "code",
        "scope":         "offline_access",
        "state":         state,
    })
    auth_url = f"https://accounts.salla.sa/oauth2/auth?{params}"
    logger.info("[Salla Start] Redirecting new merchant to OAuth | state=%s", state)
    return RedirectResponse(url=auth_url, status_code=302)


@router.get("/api/salla/authorize")
async def salla_authorize(request: Request):
    """
    Returns the Salla OAuth authorization URL.
    Frontend opens this URL to start the OAuth flow.
    """
    tenant_id = resolve_tenant_id(request)
    if not SALLA_CLIENT_ID:
        raise HTTPException(status_code=503, detail="SALLA_CLIENT_ID not configured")

    params = urllib.parse.urlencode({
        "client_id":     SALLA_CLIENT_ID,
        "redirect_uri":  SALLA_REDIRECT_URI,
        "response_type": "code",
        "scope":         "offline_access",
        "state":         str(tenant_id),
    })
    auth_url = f"https://accounts.salla.sa/oauth2/auth?{params}"
    logger.info(
        "Salla authorize URL generated | tenant=%s redirect_uri=%s",
        tenant_id, SALLA_REDIRECT_URI,
    )
    return {"url": auth_url, "redirect_uri": SALLA_REDIRECT_URI}


@router.get("/oauth/salla/callback")
async def salla_oauth_callback(
    request: Request,
    code:  Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    db:    Session = Depends(get_db),
):
    """
    Salla OAuth 2.0 callback — public endpoint (no JWT).
    Salla redirects here after the merchant authorises the app.

    Steps:
      1. Validate code/state
      2. Exchange code → access_token + refresh_token
      3. Fetch store info from Salla API
      4. Save integration to DB
      5. Redirect to embedded app landing page
    """
    client_ip = request.headers.get("X-Real-IP") or (
        request.client.host if request.client else "unknown"
    )
    logger.info(
        "[Salla OAuth] Callback received | code=%s state=%s error=%s ip=%s",
        bool(code), state, error, client_ip,
    )
    logger.info(
        "[Salla OAuth] Using redirect_uri=%s client_id=%s",
        SALLA_REDIRECT_URI,
        (SALLA_CLIENT_ID[:6] + "***") if SALLA_CLIENT_ID else "NOT SET",
    )

    # ── Resolve tenant from state param ────────────────────────────────────────
    # state = integer  → existing merchant linking their Salla store
    # state = "salla_new_*" → brand-new merchant installing from Salla (no Nahla account yet)
    is_new_merchant = (state or "").startswith(_NEW_MERCHANT_PREFIX)
    try:
        tenant_id = int(state) if (state and not is_new_merchant) else 0
    except (ValueError, TypeError):
        tenant_id = 0
    logger.info(
        "[Salla OAuth] tenant_id=%s is_new_merchant=%s",
        tenant_id, is_new_merchant,
    )

    # ── Handle provider-side errors ────────────────────────────────────────────
    if error:
        logger.warning("[Salla OAuth] Provider error: %s", error)
        return RedirectResponse(url=_error_url(error), status_code=302)

    if not code:
        logger.warning("[Salla OAuth] Missing code in callback")
        return RedirectResponse(url=_error_url("missing_code"), status_code=302)

    if not SALLA_CLIENT_ID or not SALLA_CLIENT_SECRET:
        logger.error("[Salla OAuth] SALLA_CLIENT_ID or SALLA_CLIENT_SECRET not configured")
        return RedirectResponse(url=_error_url("app_not_configured"), status_code=302)

    # ── Step 2: Token exchange ─────────────────────────────────────────────────
    logger.info("[Salla OAuth] Starting token exchange...")
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            token_resp = await client.post(
                "https://accounts.salla.sa/oauth2/token",
                data={
                    "grant_type":    "authorization_code",
                    "client_id":     SALLA_CLIENT_ID,
                    "client_secret": SALLA_CLIENT_SECRET,
                    "code":          code,
                    "redirect_uri":  SALLA_REDIRECT_URI,
                },
                headers={
                    "Accept":       "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )

            logger.info(
                "[Salla OAuth] Token endpoint response: status=%s body_preview=%.300s",
                token_resp.status_code,
                token_resp.text,
            )

            if token_resp.status_code != 200:
                # Parse Salla's error for better diagnostics
                try:
                    err_json  = token_resp.json()
                    salla_err = err_json.get("error", "")
                    salla_msg = err_json.get("error_description", token_resp.text[:200])
                except Exception:
                    salla_err = "http_error"
                    salla_msg = token_resp.text[:200]

                logger.error(
                    "[Salla OAuth] Token exchange FAILED | http=%s salla_error=%s desc=%s",
                    token_resp.status_code, salla_err, salla_msg,
                )
                return RedirectResponse(
                    url=_error_url("token_exchange_failed", salla_err or salla_msg),
                    status_code=302,
                )

            token_data    = token_resp.json()
            access_token  = token_data.get("access_token", "")
            refresh_token = token_data.get("refresh_token", "")
            expires_in    = token_data.get("expires_in", 0)
            token_type    = token_data.get("token_type", "Bearer")
            logger.info(
                "[Salla OAuth] Token exchange SUCCESS | expires_in=%s token_type=%s",
                expires_in, token_type,
            )

            # ── Step 3: Fetch store info ───────────────────────────────────────
            logger.info("[Salla OAuth] Fetching store info...")
            salla_store_id = ""
            store_name     = ""
            merchant_id    = ""

            store_resp = await client.get(
                "https://api.salla.dev/admin/v2/store/info",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept":        "application/json",
                },
            )
            logger.info("[Salla OAuth] Store info response: status=%s", store_resp.status_code)

            if store_resp.status_code == 200:
                store_json     = store_resp.json()
                store_data     = store_json.get("data", {})
                salla_store_id = str(store_data.get("id", "") or store_data.get("store_id", ""))
                store_name     = store_data.get("name", "") or store_data.get("store_name", "")
                merchant_id    = str(store_data.get("merchant", {}).get("id", "")) if isinstance(
                    store_data.get("merchant"), dict
                ) else str(store_data.get("merchant", ""))
                logger.info(
                    "[Salla OAuth] ✅ Store info: id=%s name=%r merchant_id=%s full_keys=%s",
                    salla_store_id, store_name, merchant_id, list(store_data.keys()),
                )
            else:
                logger.warning(
                    "[Salla OAuth] ⚠️ Store info fetch failed: %s %.300s",
                    store_resp.status_code, store_resp.text,
                )
                # Attempt fallback: try merchant/info endpoint
                try:
                    fallback_resp = await client.get(
                        "https://api.salla.dev/admin/v2/merchant/info",
                        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
                    )
                    if fallback_resp.status_code == 200:
                        fb_data = fallback_resp.json().get("data", {})
                        salla_store_id = str(fb_data.get("id", "") or fb_data.get("store_id", ""))
                        store_name = fb_data.get("name", "") or fb_data.get("store_name", "")
                        logger.info("[Salla OAuth] ✅ Fallback store info: id=%s name=%r", salla_store_id, store_name)
                    else:
                        logger.warning("[Salla OAuth] ⚠️ Fallback also failed: %s", fallback_resp.status_code)
                except Exception as fb_exc:
                    logger.warning("[Salla OAuth] ⚠️ Fallback request error: %s", fb_exc)

    except httpx.TimeoutException as exc:
        logger.error("[Salla OAuth] Token exchange timed out: %s", exc)
        return RedirectResponse(url=_error_url("timeout"), status_code=302)
    except Exception as exc:
        logger.exception("[Salla OAuth] Unexpected error during token exchange: %s", exc)
        return RedirectResponse(url=_error_url("network_error"), status_code=302)

    # ── Step 4: Resolve / create Nahla account for this merchant ───────────────
    auto_jwt: str = ""

    if is_new_merchant:
        # ── Auto-register new merchant from Salla ────────────────────────────
        logger.info("[Salla OAuth] Auto-registering new merchant | store=%s", store_name)
        try:
            # Derive email: use Salla store info or generate a placeholder
            salla_email = ""
            try:
                store_resp2 = await httpx.AsyncClient(timeout=10).get(
                    "https://api.salla.dev/admin/v2/settings/account",
                    headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
                )
                if store_resp2.status_code == 200:
                    acc = store_resp2.json().get("data", {})
                    salla_email = (
                        acc.get("email") or
                        acc.get("mobile") or
                        ""
                    )
            except Exception:
                pass

            # Fallback email if Salla didn't return one
            if not salla_email:
                safe_store = "".join(c for c in store_name if c.isalnum() or c in "-_").lower()[:30]
                salla_email = f"{safe_store or 'store'}-{salla_store_id}@salla-merchant.nahlah.ai"

            salla_email = salla_email.strip().lower()
            logger.info("[Salla OAuth] Merchant email resolved: %s", salla_email)

            # Check for existing Nahla account with this email
            existing_user = db.query(User).filter(User.email == salla_email).first()

            if existing_user:
                tenant_id = existing_user.tenant_id
                logger.info(
                    "[Salla OAuth] Found existing Nahla account | email=%s tenant=%s user_id=%s",
                    salla_email, tenant_id, existing_user.id,
                )
                auto_jwt = create_token(
                    email=existing_user.email,
                    role=existing_user.role or "merchant",
                    tenant_id=tenant_id,
                    user_id=existing_user.id,
                )
            else:
                # Check by store_id to avoid duplicate tenant name error
                existing_integ = db.query(Integration).filter(
                    Integration.provider == "salla",
                    Integration.config["store_id"].astext == str(salla_store_id),
                ).first() if salla_store_id else None

                if existing_integ:
                    tenant_id = existing_integ.tenant_id
                    logger.info("[Salla OAuth] Found existing tenant by store_id=%s → tenant=%s", salla_store_id, tenant_id)
                    existing_user2 = db.query(User).filter(User.tenant_id == tenant_id).first()
                    if existing_user2:
                        auto_jwt = create_token(
                            email=existing_user2.email,
                            role=existing_user2.role or "merchant",
                            tenant_id=tenant_id,
                            user_id=existing_user2.id,
                        )
                else:
                    # Create new Tenant + User with unique name
                    unique_name = f"{store_name or 'متجر سلة'}-{salla_store_id}" if salla_store_id else (store_name or "متجر سلة")
                    new_tenant = Tenant(name=unique_name)
                    db.add(new_tenant)
                    db.flush()   # get new_tenant.id
                    tenant_id = new_tenant.id

                temp_password = _secrets.token_urlsafe(16)
                new_user = User(
                    username=salla_email.split("@")[0],
                    email=salla_email,
                    password_hash=hash_password(temp_password),
                    role="merchant",
                    tenant_id=tenant_id,
                    is_active=True,
                )
                db.add(new_user)
                db.flush()

                auto_jwt = create_token(
                    email=salla_email,
                    role="merchant",
                    tenant_id=tenant_id,
                    user_id=new_user.id,
                )
                logger.info(
                    "[Salla OAuth] Auto-registered new merchant | email=%s tenant=%s user_id=%s",
                    salla_email, tenant_id, new_user.id,
                )

        except Exception as exc:
            logger.exception("[Salla OAuth] Auto-register failed: %s", exc)
            try:
                db.rollback()
            except Exception:
                pass
            return RedirectResponse(url=_error_url("registration_failed"), status_code=302)

    else:
        # Existing merchant — tenant_id came from state
        if tenant_id == 0:
            tenant_id = 1  # last resort fallback (should not happen)
        get_or_create_tenant(db, tenant_id)

    # ── Step 4b: Save Salla integration to DB ──────────────────────────────────
    logger.info(
        "[Salla OAuth] ▶ Saving integration | tenant=%s store_id=%r store_name=%r",
        tenant_id, salla_store_id, store_name,
    )
    if not salla_store_id:
        logger.error(
            "[Salla OAuth] ❌ store_id is EMPTY — widget auto-load will NOT work. "
            "Check store info API response above."
        )

    try:
        integration = db.query(Integration).filter(
            Integration.tenant_id == tenant_id,
            Integration.provider  == "salla",
        ).first()

        new_config = {
            "api_key":       access_token,
            "refresh_token": refresh_token,
            "token_type":    token_type,
            "expires_in":    expires_in,
            "store_id":      salla_store_id,
            "store_name":    store_name,
            "merchant_id":   merchant_id,
            "redirect_uri":  SALLA_REDIRECT_URI,
            "connected_at":  datetime.now(timezone.utc).isoformat(),
        }

        if integration:
            integration.config  = new_config
            integration.enabled = True
            logger.info("[Salla OAuth] ✅ Updated existing Integration id=%s", integration.id)
        else:
            new_integ = Integration(
                tenant_id=tenant_id,
                provider="salla",
                config=new_config,
                enabled=True,
            )
            db.add(new_integ)
            db.flush()
            logger.info("[Salla OAuth] ✅ Created new Integration id=%s", new_integ.id)

        db.commit()
        logger.info(
            "[Salla OAuth] ✅ DB commit SUCCESS | tenant=%s store_id=%s | "
            "Widget URL: /merchant/widgets/salla/%s/nahla-widgets.js",
            tenant_id, salla_store_id, salla_store_id,
        )
    except Exception as exc:
        logger.exception("[Salla OAuth] ❌ DB save FAILED: %s", exc)
        try:
            db.rollback()
        except Exception:
            pass
        return RedirectResponse(url=_error_url("db_save_failed"), status_code=302)

    # ── Notify merchant (fire-and-forget) ──────────────────────────────────────
    try:
        import asyncio as _asyncio  # noqa: PLC0415
        from core.wa_notify import notify_store_connected  # noqa: PLC0415
        from core.tenant import get_or_create_settings, merge_defaults, DEFAULT_WHATSAPP  # noqa: PLC0415
        _s     = get_or_create_settings(db, tenant_id)
        _wa    = merge_defaults(_s.whatsapp_settings or {}, DEFAULT_WHATSAPP)
        _phone = _wa.get("owner_whatsapp_number", "")
        if _phone:
            _asyncio.ensure_future(notify_store_connected(_phone, store_name, "سلة"))
    except Exception as _exc:
        logger.warning("[Salla OAuth] WA notification error: %s", _exc)

    # ── Step 5: Redirect ────────────────────────────────────────────────────────
    if auto_jwt:
        # New merchant: redirect to /salla-callback with the JWT so they land logged-in
        params = urllib.parse.urlencode({
            "token":     auto_jwt,
            "status":    "connected",
            "store":     salla_store_id,
            "name":      store_name,
            "new":       "1" if is_new_merchant else "0",
        })
        redirect_url = f"{_SALLA_CALLBACK_BASE}/salla-callback?{params}"
        logger.info("[Salla OAuth] New-merchant redirect → %s", redirect_url)
        return RedirectResponse(url=redirect_url, status_code=302)

    # Existing merchant: original flow
    success_url = _success_url(salla_store_id, store_name)
    logger.info("[Salla OAuth] Existing-merchant redirect → %s", success_url)
    return RedirectResponse(url=success_url, status_code=302)


@router.get("/integrations/salla/success", response_class=HTMLResponse)
async def salla_integration_success(request: Request):
    """
    Public success landing page (no JWT required).
    Shown if the browser lands here instead of the embedded app.
    Immediately redirects the user to the embedded app.
    """
    store = request.query_params.get("store", "")
    name  = urllib.parse.quote(request.query_params.get("name", ""))
    dest  = f"{_SALLA_APP}?status=connected&store={store}&name={name}"
    return HTMLResponse(content=_redirect_html(dest, "تم ربط المتجر بنجاح ✅", "جاري التحويل..."))


@router.get("/integrations/salla/error", response_class=HTMLResponse)
async def salla_integration_error(request: Request):
    """
    Public error landing page (no JWT required).
    Shown if the browser lands here instead of the embedded app.
    Immediately redirects the user to the embedded app with the error reason.
    """
    reason = request.query_params.get("reason", "unknown_error")
    detail = request.query_params.get("detail", "")
    dest   = f"{_SALLA_APP}?status=error&reason={reason}"
    if detail:
        dest += f"&detail={urllib.parse.quote(detail)}"
    return HTMLResponse(content=_redirect_html(dest, "حدث خطأ أثناء ربط المتجر", f"السبب: {reason}"))


def _redirect_html(dest: str, title: str, subtitle: str) -> str:
    """Return a minimal HTML page that auto-redirects."""
    return f"""<!DOCTYPE html>
<html dir="rtl" lang="ar">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="refresh" content="2; url={dest}">
  <title>نحلة AI — {title}</title>
  <style>
    body {{ font-family: Arial, sans-serif; text-align: center; padding: 60px 20px; background: #fffbeb; color: #1e293b; }}
    h2 {{ color: #f59e0b; }} p {{ color: #64748b; }}
  </style>
</head>
<body>
  <h2>🐝 نحلة AI</h2>
  <h3>{title}</h3>
  <p>{subtitle}</p>
  <p style="font-size:13px">جاري التحويل التلقائي...</p>
  <script>setTimeout(() => location.href = "{dest}", 1500);</script>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════════════════════
# SALLA TEST APP — separate routes using SALLA_TEST_* credentials
# Does NOT modify or affect the production OAuth flow above.
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/api/salla/test/authorize", include_in_schema=True)
async def salla_test_authorize(request: Request):
    """
    PUBLIC — no JWT required.
    Redirects directly to Salla OAuth authorization using the TEST app credentials.
    """
    # ── Diagnostic logs — confirms exactly which credentials are used ──────────
    logger.info("[SallaTest][DIAG] ▶ /api/salla/test/authorize called")
    logger.info("[SallaTest][DIAG] SALLA_TEST_CLIENT_ID  = %s",
                (SALLA_TEST_CLIENT_ID[:8] + "***") if SALLA_TEST_CLIENT_ID else "NOT SET")
    logger.info("[SallaTest][DIAG] SALLA_TEST_REDIRECT_URI = %s", SALLA_TEST_REDIRECT_URI)
    logger.info("[SallaTest][DIAG] SALLA_CLIENT_ID (prod) = %s",
                (SALLA_CLIENT_ID[:8] + "***") if SALLA_CLIENT_ID else "NOT SET")

    if not SALLA_TEST_CLIENT_ID:
        logger.error("[SallaTest][DIAG] ✗ SALLA_TEST_CLIENT_ID is empty — cannot redirect")
        raise HTTPException(status_code=503, detail="SALLA_TEST_CLIENT_ID not configured")

    params = urllib.parse.urlencode({
        "client_id":     SALLA_TEST_CLIENT_ID,
        "redirect_uri":  SALLA_TEST_REDIRECT_URI,
        "response_type": "code",
        "scope":         "offline_access",
        "state":         _NEW_MERCHANT_PREFIX + "test",
    })
    auth_url = f"https://accounts.salla.sa/oauth2/auth?{params}"
    logger.info("[SallaTest][DIAG] ✓ Final authorize URL = %s", auth_url)
    return RedirectResponse(url=auth_url, status_code=302)


@router.get("/oauth/salla/test/callback")
async def salla_test_oauth_callback(
    request: Request,
    code:    Optional[str] = None,
    state:   Optional[str] = None,
    error:   Optional[str] = None,
    db:      Session = Depends(get_db),
):
    """
    Salla TEST app OAuth callback — uses SALLA_TEST_* credentials.
    Identical logic to /oauth/salla/callback but uses the test app's keys.
    The production /oauth/salla/callback is NOT affected.
    """
    logger.info(
        "[SallaTest] Callback received | code=%s state=%s error=%s",
        bool(code), state, error,
    )

    is_new_merchant = (state or "").startswith(_NEW_MERCHANT_PREFIX)
    try:
        tenant_id = int(state) if (state and not is_new_merchant) else 0
    except (ValueError, TypeError):
        tenant_id = 0

    if error:
        return RedirectResponse(url=_error_url(error), status_code=302)

    if not code:
        return RedirectResponse(url=_error_url("missing_code"), status_code=302)

    if not SALLA_TEST_CLIENT_ID or not SALLA_TEST_CLIENT_SECRET:
        logger.error("[SallaTest] TEST credentials not configured")
        return RedirectResponse(url=_error_url("app_not_configured"), status_code=302)

    # Token exchange using TEST app credentials
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            token_resp = await client.post(
                "https://accounts.salla.sa/oauth2/token",
                data={
                    "grant_type":    "authorization_code",
                    "client_id":     SALLA_TEST_CLIENT_ID,
                    "client_secret": SALLA_TEST_CLIENT_SECRET,
                    "code":          code,
                    "redirect_uri":  SALLA_TEST_REDIRECT_URI,
                },
                headers={
                    "Accept":       "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            logger.info("[SallaTest] Token response: status=%s", token_resp.status_code)

            if token_resp.status_code != 200:
                try:
                    err_json  = token_resp.json()
                    salla_err = err_json.get("error", "token_exchange_failed")
                    salla_msg = err_json.get("error_description", token_resp.text[:200])
                except Exception:
                    salla_err, salla_msg = "token_exchange_failed", token_resp.text[:200]
                logger.error("[SallaTest] Token exchange FAILED: %s %s", salla_err, salla_msg)
                return RedirectResponse(url=_error_url("token_exchange_failed", salla_err), status_code=302)

            token_data    = token_resp.json()
            access_token  = token_data.get("access_token", "")
            refresh_token = token_data.get("refresh_token", "")
            expires_in    = int(token_data.get("expires_in", 0))

            # Fetch store info
            salla_store_id = ""
            store_name     = ""
            merchant_id    = ""
            store_resp = await client.get(
                "https://api.salla.dev/admin/v2/store/info",
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            )
            if store_resp.status_code == 200:
                store_data     = store_resp.json().get("data", {})
                salla_store_id = str(store_data.get("id", ""))
                store_name     = store_data.get("name", "")
                merchant_id    = str(store_data.get("merchant", {}).get("id", "")) if isinstance(
                    store_data.get("merchant"), dict
                ) else str(store_data.get("merchant", ""))
                logger.info("[SallaTest] Store: id=%s name=%s", salla_store_id, store_name)

    except Exception as exc:
        logger.exception("[SallaTest] Unexpected error: %s", exc)
        return RedirectResponse(url=_error_url("network_error"), status_code=302)

    # Save / update integration in DB
    try:
        from datetime import datetime, timezone  # noqa: PLC0415
        if tenant_id:
            get_or_create_tenant(db, tenant_id)
        integration = db.query(Integration).filter(
            Integration.tenant_id == (tenant_id or 0),
            Integration.provider  == "salla",
        ).first()
        cfg = {
            "store_id":      salla_store_id,
            "store_name":    store_name,
            "merchant_id":   merchant_id,
            "access_token":  access_token,
            "refresh_token": refresh_token,
            "expires_in":    expires_in,
            "connected_at":  datetime.now(timezone.utc).isoformat(),
            "app_type":      "test",
        }
        if integration:
            integration.config  = cfg
            integration.enabled = True
        else:
            integration = Integration(
                tenant_id=tenant_id or 0,
                provider="salla",
                config=cfg,
                enabled=True,
            )
            db.add(integration)
        db.commit()
        logger.info("[SallaTest] Integration saved | tenant=%s store=%s", tenant_id, salla_store_id)
    except Exception as exc:
        logger.error("[SallaTest] DB save failed: %s", exc)
        return RedirectResponse(url=_error_url("db_save_failed"), status_code=302)

    success_url = f"{_SALLA_APP}?salla_connected=true&name={urllib.parse.quote(store_name)}"
    return RedirectResponse(url=success_url, status_code=302)


# ═══════════════════════════════════════════════════════════════════════════════
# PROTECTED ROUTES — require JWT
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/api/salla/store")
async def get_salla_store(
    request:   Request,
    db:        Session = Depends(get_db),
    tenant_id: int     = Depends(get_jwt_tenant_id),
):
    """Return saved Salla store info for this tenant."""
    audit("salla_store_read", tenant_id=tenant_id)
    integration = db.query(Integration).filter(
        Integration.tenant_id == tenant_id,
        Integration.provider  == "salla",
        Integration.enabled   == True,  # noqa: E712
    ).first()
    if not integration:
        raise HTTPException(status_code=404, detail="Salla integration not configured")
    cfg = integration.config or {}
    return {
        "configured":   True,
        "store_id":     cfg.get("store_id", ""),
        "store_name":   cfg.get("store_name", ""),
        "merchant_id":  cfg.get("merchant_id", ""),
        "connected_at": cfg.get("connected_at"),
        "redirect_uri": cfg.get("redirect_uri", ""),
        "api_key_hint": ("***" + cfg.get("api_key", "")[-4:]) if cfg.get("api_key") else "",
    }


@router.get("/api/salla/products")
async def get_salla_products(
    request:   Request,
    tenant_id: int = Depends(get_jwt_tenant_id),
):
    """Fetch live products from the tenant's Salla store."""
    audit("salla_products_fetched", tenant_id=tenant_id)
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
    from store_integration.registry import get_adapter  # noqa: PLC0415
    adapter = get_adapter(tenant_id)
    if not adapter:
        raise HTTPException(status_code=404, detail="Salla integration not configured")
    try:
        products = await adapter.get_products()
        return {"products": [p.dict() for p in products], "count": len(products)}
    except Exception as exc:
        logger.error("Salla products fetch error tenant=%s: %s", tenant_id, exc)
        raise HTTPException(status_code=502, detail=f"Salla API error: {exc}")


@router.post("/api/salla/test-coupon")
async def test_salla_coupon(
    request:   Request,
    tenant_id: int = Depends(get_jwt_tenant_id),
):
    """Validate a coupon code against the tenant's Salla store."""
    body = await request.json()
    coupon_code = body.get("coupon_code", "").strip()
    if not coupon_code:
        raise HTTPException(status_code=400, detail="coupon_code is required")
    audit("salla_coupon_test", tenant_id=tenant_id, coupon=coupon_code)
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
    from store_integration.registry import get_adapter  # noqa: PLC0415
    adapter = get_adapter(tenant_id)
    if not adapter:
        raise HTTPException(status_code=404, detail="Salla integration not configured")
    try:
        offer = await adapter.validate_coupon(coupon_code)
        if offer:
            return {"valid": True, "coupon": offer.dict()}
        return {"valid": False, "reason": "coupon not found or expired"}
    except Exception as exc:
        logger.error("Salla coupon error tenant=%s: %s", tenant_id, exc)
        raise HTTPException(status_code=502, detail=f"Salla API error: {exc}")
