"""
routers/zid_oauth.py
────────────────────
Zid OAuth 2.0 integration for Nahla SaaS.

Flow:
  1. Merchant installs Nahla app from Zid marketplace.
  2. Zid redirects to  GET /zid/redirect?code=XXX  (authorization code).
  3. Backend exchanges code → access_token + refresh_token via Zid OAuth server.
  4. Backend fetches merchant/store profile from Zid API.
  5. Backend finds-or-creates an isolated Tenant + User for this Zid store.
  6. Backend issues a Nahla JWT (tenant_id in claims).
  7. Merchant is redirected to the dashboard.

Embedded-app flow (merchant opens app from Zid dashboard):
  1. Zid loads Application URL  →  GET /zid/app
  2. The page reads manager_token from query-string or postMessage.
  3. Page POSTs to  /zid/token-login  to get a Nahla JWT.
  4. Auto-redirect to app.nahlah.ai/zid-callback?token=JWT.

Webhook events:
  POST /webhook/zid  — receives store events (orders, products, …)
"""

import hashlib
import hmac
import logging
import secrets as _secrets
from datetime import datetime, timezone
from textwrap import dedent

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session
from typing import Optional, Tuple

from core.config import (
    DASHBOARD_URL,
    ZID_CLIENT_ID,
    ZID_CLIENT_SECRET,
    ZID_REDIRECT_URI,
    ZID_WEBHOOK_SECRET,
)
from core.auth import create_token, hash_password
from core.database import get_db

logger = logging.getLogger("nahla.zid")
router = APIRouter(tags=["Zid"])

# ── Zid API constants ──────────────────────────────────────────────────────────
ZID_OAUTH_BASE  = "https://oauth.zid.sa"
ZID_API_BASE    = "https://api.zid.sa"
ZID_TOKEN_URL   = f"{ZID_OAUTH_BASE}/oauth/token"
ZID_PROFILE_URL = f"{ZID_API_BASE}/v1/managers/profile"
ZID_STORE_URL   = f"{ZID_API_BASE}/v1/managers/profile/store/info"

_ZID_HEADERS = {
    "Accept":          "application/json",
    "Accept-Language": "ar",
    "Role":            "manager",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_or_create_tenant_user(db: Session, store_id: str, store_name: str, email: str):
    """
    Find-or-create an isolated Tenant + User for the given Zid store.
    Returns (tenant_id, user_id, is_new).
    """
    from database.models import Integration, Tenant, User  # noqa: PLC0415

    # Look up existing integration
    integration = (
        db.query(Integration)
        .filter(
            Integration.provider    == "zid",
            Integration.external_id == str(store_id),
        )
        .first()
    )

    if integration:
        tenant_id = integration.tenant_id
        user      = db.query(User).filter(User.tenant_id == tenant_id).first()
        logger.info("[Zid] Returning merchant | store_id=%s tenant_id=%s", store_id, tenant_id)
        return tenant_id, user.id if user else None, False

    # ── First-time: create Tenant + User ────────────────────────────────────────
    tenant = Tenant(name=store_name or f"متجر زد {store_id}")
    db.add(tenant)
    db.flush()

    zid_email = email or f"zid-{store_id}@zid-merchant.nahlah.ai"
    user = User(
        username      = zid_email.split("@")[0],
        email         = zid_email,
        password_hash = hash_password(_secrets.token_urlsafe(16)),
        role          = "merchant",
        tenant_id     = tenant.id,
        is_active     = True,
        created_at    = datetime.now(timezone.utc),
    )
    db.add(user)
    db.flush()

    # Save integration record
    integration = Integration(
        tenant_id   = tenant.id,
        provider    = "zid",
        external_id = str(store_id),
        status      = "active",
        config      = {"store_id": store_id, "store_name": store_name},
    )
    db.add(integration)
    db.flush()

    logger.info(
        "[Zid] ✅ New merchant | store_id=%s email=%s tenant_id=%s",
        store_id, zid_email, tenant.id,
    )
    return tenant.id, user.id, True


def _save_zid_tokens(
    db: Session,
    tenant_id: int,
    access_token: str,
    refresh_token: str,
    store_id: str,
    store_name: str,
    email: str,
) -> None:
    """Persist/update Zid OAuth tokens in the Integration record."""
    from database.models import Integration  # noqa: PLC0415

    integ = (
        db.query(Integration)
        .filter(
            Integration.tenant_id   == tenant_id,
            Integration.provider    == "zid",
        )
        .first()
    )
    if integ:
        integ.access_token  = access_token
        integ.refresh_token = refresh_token
        integ.status        = "active"
        integ.config        = {
            "store_id":   store_id,
            "store_name": store_name,
            "email":      email,
        }
    db.flush()


# ── Embedded app HTML page ─────────────────────────────────────────────────────

def _embedded_app_html(error_msg: str = "") -> str:
    """HTML page served when Zid loads the app inside its iframe."""
    error_block = (
        f'<p class="error">{error_msg}</p>'
        if error_msg else ""
    )
    return dedent(f"""
    <!DOCTYPE html>
    <html dir="rtl" lang="ar">
    <head>
      <meta charset="UTF-8"/>
      <meta name="viewport" content="width=device-width,initial-scale=1"/>
      <title>نحلة — ربط المتجر</title>
      <style>
        *{{box-sizing:border-box;margin:0;padding:0}}
        body{{font-family:'Segoe UI',Tahoma,Arial,sans-serif;background:#f8fafc;
              display:flex;align-items:center;justify-content:center;
              min-height:100vh;padding:24px;direction:rtl}}
        .card{{background:#fff;border-radius:16px;padding:40px 32px;
               max-width:440px;width:100%;text-align:center;
               box-shadow:0 4px 24px rgba(0,0,0,.08)}}
        .logo{{display:inline-flex;align-items:center;gap:8px;
               font-size:22px;font-weight:700;color:#1a1a2e;margin-bottom:24px}}
        .logo-icon{{width:40px;height:40px;background:#6c3fe8;border-radius:10px;
                    display:flex;align-items:center;justify-content:center;
                    color:#fff;font-size:18px}}
        .ai-badge{{background:linear-gradient(135deg,#6c3fe8,#a855f7);
                   color:#fff;font-size:9px;padding:2px 6px;border-radius:20px;
                   font-weight:700;letter-spacing:.5px}}
        h1{{font-size:20px;color:#1a1a2e;margin-bottom:8px}}
        p{{color:#6b7280;font-size:14px;line-height:1.6;margin-bottom:20px}}
        .btn{{display:inline-block;background:#6c3fe8;color:#fff;
              padding:12px 28px;border-radius:10px;text-decoration:none;
              font-size:15px;font-weight:600;border:none;cursor:pointer;
              transition:background .2s}}
        .btn:hover{{background:#5a32c8}}
        .btn:disabled{{background:#a0aec0;cursor:not-allowed}}
        .status{{margin-top:16px;font-size:13px;color:#9ca3af;min-height:20px}}
        .error{{color:#ef4444;font-size:13px;margin-bottom:12px}}
        .spinner{{display:inline-block;width:18px;height:18px;
                  border:2px solid #e5e7eb;border-top-color:#6c3fe8;
                  border-radius:50%;animation:spin .8s linear infinite;
                  margin-left:8px;vertical-align:middle}}
        @keyframes spin{{to{{transform:rotate(360deg)}}}}
      </style>
    </head>
    <body>
    <div class="card">
      <div class="logo">
        <div class="logo-icon">🐝</div>
        نحلة
        <span class="ai-badge">AI</span>
      </div>
      <h1>مرحباً في نحلة</h1>
      <p>مساعدك الذكي للمبيعات عبر واتساب</p>
      {error_block}
      <button class="btn" id="ctaBtn" disabled>
        جاري التحقق... <span class="spinner"></span>
      </button>
      <p class="status" id="statusMsg">يتم التحقق من هويتك...</p>
    </div>

    <script>
    (function() {{
      var btn       = document.getElementById('ctaBtn');
      var statusMsg = document.getElementById('statusMsg');

      function setStatus(msg) {{ statusMsg.textContent = msg; }}

      function goToDashboard(link) {{
        btn.href     = link;
        btn.target   = '_blank';
        btn.disabled = false;
        btn.innerHTML = 'فتح لوحة التحكم &#8599;';
        setStatus('تم التحقق بنجاح');
        try {{
          window.top.location.href = link;
        }} catch(e) {{
          window.open(link, '_blank');
        }}
      }}

      function showError(msg) {{
        btn.disabled   = true;
        btn.textContent = 'حدث خطأ';
        setStatus(msg);
      }}

      // Read manager_token from URL params
      var params       = new URLSearchParams(window.location.search);
      var managerToken = params.get('manager_token') || params.get('token');
      var storeId      = params.get('store_id');

      if (!managerToken) {{
        showError('لم يتم استلام رمز التحقق من زد. يرجى إعادة المحاولة.');
        return;
      }}

      setStatus('جاري تسجيل الدخول...');

      fetch('/zid/token-login', {{
        method:  'POST',
        headers: {{'Content-Type': 'application/json'}},
        body:    JSON.stringify({{
          manager_token: managerToken,
          store_id:      storeId
        }})
      }})
      .then(function(r) {{ return r.json(); }})
      .then(function(data) {{
        if (data.access_token) {{
          var isNew   = data.is_new;
          var path    = isNew ? '/onboarding' : '/overview';
          var dashLink = '{DASHBOARD_URL}/zid-callback?token=' + data.access_token
                        + '&redirect=' + encodeURIComponent(path);
          setStatus(isNew ? 'مرحباً! جاري إعداد متجرك...' : 'مرحباً مجدداً! جاري التوجيه...');
          setTimeout(function() {{ goToDashboard(dashLink); }}, 1200);
        }} else {{
          showError(data.detail || 'فشل التحقق من الهوية');
        }}
      }})
      .catch(function(err) {{
        showError('خطأ في الاتصال بالخادم');
        console.error('[Nahla/Zid] token-login error:', err);
      }});
    }})();
    </script>
    </body>
    </html>
    """).strip()


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/zid/app", response_class=HTMLResponse, include_in_schema=False)
async def zid_embedded_app(request: Request):
    """
    Embedded app page loaded by Zid inside its dashboard iframe.
    Zid passes manager_token as a query parameter.
    """
    return HTMLResponse(content=_embedded_app_html(), status_code=200)


@router.get("/zid/redirect")
async def zid_oauth_redirect(
    request: Request,
    code: Optional[str] = None,
    manager_token: Optional[str] = None,
    store_id: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """
    OAuth callback from Zid after merchant installs/authorizes the app.
    Receives authorization code → exchanges for tokens → creates tenant.
    """
    logger.info(
        "[Zid] Redirect received | code=%s manager_token=%s store_id=%s",
        bool(code), bool(manager_token), store_id,
    )

    # ── Path A: manager_token passed directly (Zid embedded install) ─────────
    if manager_token:
        return await _handle_manager_token(manager_token, store_id, db)

    # ── Path B: authorization code exchange ───────────────────────────────────
    if not code:
        logger.error("[Zid] Redirect: no code or manager_token received")
        return RedirectResponse(
            f"{DASHBOARD_URL}/error?reason=zid_missing_code", status_code=302
        )

    if not ZID_CLIENT_ID or not ZID_CLIENT_SECRET:
        logger.error("[Zid] ZID_CLIENT_ID / ZID_CLIENT_SECRET not configured")
        return RedirectResponse(
            f"{DASHBOARD_URL}/error?reason=zid_not_configured", status_code=302
        )

    # Exchange code for tokens
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            token_resp = await client.post(
                ZID_TOKEN_URL,
                json={
                    "client_id":     ZID_CLIENT_ID,
                    "client_secret": ZID_CLIENT_SECRET,
                    "grant_type":    "authorization_code",
                    "code":          code,
                    "redirect_uri":  ZID_REDIRECT_URI,
                },
                headers={"Accept": "application/json"},
            )
            token_resp.raise_for_status()
            token_data = token_resp.json()
    except Exception as exc:
        logger.error("[Zid] Token exchange failed: %s", exc)
        return RedirectResponse(
            f"{DASHBOARD_URL}/error?reason=zid_token_exchange_failed", status_code=302
        )

    access_token  = token_data.get("access_token", "")
    refresh_token = token_data.get("refresh_token", "")

    if not access_token:
        logger.error("[Zid] Token exchange returned no access_token: %s", token_data)
        return RedirectResponse(
            f"{DASHBOARD_URL}/error?reason=zid_no_token", status_code=302
        )

    # Fetch merchant profile
    store_info, owner_email, zid_store_id, store_name = await _fetch_zid_profile(access_token)

    try:
        tenant_id, _user_id, is_new = _get_or_create_tenant_user(
            db, zid_store_id, store_name, owner_email
        )
        _save_zid_tokens(db, tenant_id, access_token, refresh_token,
                         zid_store_id, store_name, owner_email)
        db.commit()
    except Exception as exc:
        logger.exception("[Zid] DB error: %s", exc)
        db.rollback()
        return RedirectResponse(
            f"{DASHBOARD_URL}/error?reason=zid_db_error", status_code=302
        )

    email_for_jwt = owner_email or f"zid-{zid_store_id}@zid-merchant.nahlah.ai"
    nahla_token = create_token(
        email=email_for_jwt,
        role="merchant",
        tenant_id=tenant_id,
        user_id=_user_id,
    )
    path = "/onboarding" if is_new else "/overview"
    redirect_url = (
        f"{DASHBOARD_URL}/zid-callback?token={nahla_token}"
        f"&redirect={path}"
    )
    logger.info(
        "[Zid] ✅ OAuth complete | tenant_id=%s user_id=%s is_new=%s → %s",
        tenant_id, _user_id, is_new, path,
    )
    return RedirectResponse(redirect_url, status_code=302)


@router.post("/zid/token-login")
async def zid_token_login(request: Request, db: Session = Depends(get_db)):
    """
    Called from the embedded app page with a Zid manager_token.
    Validates the token, finds/creates tenant, returns a Nahla JWT.
    """
    try:
        body          = await request.json()
        manager_token = body.get("manager_token", "").strip()
        store_id_hint = body.get("store_id", "")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request body")

    if not manager_token:
        raise HTTPException(status_code=400, detail="manager_token مطلوب")

    logger.info("[Zid] token-login | store_id_hint=%s", store_id_hint)

    store_info, owner_email, zid_store_id, store_name = await _fetch_zid_profile(manager_token)

    if not zid_store_id:
        zid_store_id = str(store_id_hint) if store_id_hint else "unknown"
    if not store_name:
        store_name = f"متجر زد {zid_store_id}"

    try:
        tenant_id, _user_id, is_new = _get_or_create_tenant_user(
            db, zid_store_id, store_name, owner_email
        )
        _save_zid_tokens(db, tenant_id, manager_token, "",
                         zid_store_id, store_name, owner_email)
        db.commit()
    except Exception as exc:
        logger.exception("[Zid] token-login DB error: %s", exc)
        db.rollback()
        raise HTTPException(status_code=500, detail="خطأ في قاعدة البيانات")

    email_for_jwt = owner_email or f"zid-{zid_store_id}@zid-merchant.nahlah.ai"
    nahla_token   = create_token(
        email=email_for_jwt,
        role="merchant",
        tenant_id=tenant_id,
        user_id=_user_id,
    )
    logger.info(
        "[Zid] ✅ token-login success | tenant_id=%s user_id=%s is_new=%s email=%s",
        tenant_id, _user_id, is_new, email_for_jwt,
    )
    return {
        "access_token": nahla_token,
        "tenant_id":    tenant_id,
        "user_id":      _user_id,
        "is_new":       is_new,
        "store_name":   store_name,
    }


@router.post("/webhook/zid")
async def zid_webhook(request: Request, db: Session = Depends(get_db)):
    """
    Receive webhook events from Zid (orders, products, customers, …).
    Verifies HMAC signature when ZID_WEBHOOK_SECRET is configured.
    """
    body_bytes = await request.body()

    # Signature verification
    if ZID_WEBHOOK_SECRET:
        sig_header = request.headers.get("X-Zid-Signature", "")
        mac      = hmac.new(ZID_WEBHOOK_SECRET.encode(), body_bytes, hashlib.sha256)
        expected = mac.hexdigest()
        if not hmac.compare_digest(sig_header, expected):
            logger.warning("[Zid Webhook] Invalid signature")
            raise HTTPException(status_code=401, detail="Invalid webhook signature")

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"status": "ok"})

    event_type = payload.get("event") or payload.get("type", "unknown")
    store_id   = str(payload.get("store_id", ""))

    logger.info("[Zid Webhook] Event=%s store_id=%s", event_type, store_id)

    # Find the tenant for this store
    if store_id:
        from database.models import Integration  # noqa: PLC0415
        integ = (
            db.query(Integration)
            .filter(
                Integration.provider    == "zid",
                Integration.external_id == store_id,
            )
            .first()
        )
        tenant_id = integ.tenant_id if integ else None
        if tenant_id:
            await _dispatch_zid_event(db, tenant_id, event_type, payload)

    return JSONResponse({"status": "ok"})


@router.get("/zid/status")
async def zid_status(request: Request, db: Session = Depends(get_db)):
    """Return Zid connection status for the current tenant."""
    from core.middleware import resolve_tenant_id  # noqa: PLC0415
    from database.models import Integration        # noqa: PLC0415

    tenant_id = resolve_tenant_id(request)
    integ = (
        db.query(Integration)
        .filter(
            Integration.tenant_id == tenant_id,
            Integration.provider  == "zid",
        )
        .first()
    )
    if not integ:
        return {"connected": False}

    return {
        "connected":   True,
        "store_id":    integ.external_id,
        "store_name":  (integ.config or {}).get("store_name", ""),
        "status":      integ.status,
        "connected_at": integ.created_at.isoformat() if integ.created_at else None,
    }


# ── Internal helpers ──────────────────────────────────────────────────────────

async def _fetch_zid_profile(access_token: str):
    """
    Fetch merchant profile + store info from Zid API.
    Returns (store_info_dict, owner_email, store_id_str, store_name_str).
    """
    headers = {
        **_ZID_HEADERS,
        "Authorization": f"Bearer {access_token}",
        "X-Manager-Token": access_token,
    }
    store_info   = {}
    owner_email  = ""
    zid_store_id = ""
    store_name   = ""

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # Fetch manager profile
            prof_resp = await client.get(ZID_PROFILE_URL, headers=headers)
            if prof_resp.status_code == 200:
                prof_data   = prof_resp.json()
                manager     = prof_data.get("manager", prof_data)
                owner_email = (
                    manager.get("email") or
                    manager.get("username") or
                    ""
                )
                zid_store_id = str(
                    manager.get("store_id") or
                    manager.get("id") or
                    ""
                )
                logger.info(
                    "[Zid] Profile fetched | email=%s store_id=%s",
                    owner_email, zid_store_id,
                )

            # Fetch store info
            store_resp = await client.get(ZID_STORE_URL, headers=headers)
            if store_resp.status_code == 200:
                store_data   = store_resp.json()
                store_info   = store_data.get("store", store_data)
                store_name   = (
                    store_info.get("name_ar") or
                    store_info.get("name") or
                    store_info.get("subdomain") or
                    ""
                )
                if not zid_store_id:
                    zid_store_id = str(store_info.get("id", ""))
    except Exception as exc:
        logger.warning("[Zid] Profile fetch error: %s", exc)

    return store_info, owner_email, zid_store_id, store_name


async def _handle_manager_token(manager_token: str, store_id: Optional[str], db: Session):
    """Handle direct manager_token redirect (non-code flow)."""
    store_info, owner_email, zid_store_id, store_name = await _fetch_zid_profile(manager_token)

    if not zid_store_id:
        zid_store_id = str(store_id) if store_id else "unknown"
    if not store_name:
        store_name = f"متجر زد {zid_store_id}"

    try:
        tenant_id, _user_id, is_new = _get_or_create_tenant_user(
            db, zid_store_id, store_name, owner_email
        )
        _save_zid_tokens(db, tenant_id, manager_token, "",
                         zid_store_id, store_name, owner_email)
        db.commit()
    except Exception as exc:
        logger.exception("[Zid] handle_manager_token DB error: %s", exc)
        db.rollback()
        return RedirectResponse(
            f"{DASHBOARD_URL}/error?reason=zid_db_error", status_code=302
        )

    email_for_jwt = owner_email or f"zid-{zid_store_id}@zid-merchant.nahlah.ai"
    nahla_token   = create_token(
        email=email_for_jwt,
        role="merchant",
        tenant_id=tenant_id,
        user_id=_user_id,
    )
    path = "/onboarding" if is_new else "/overview"
    redirect_url = (
        f"{DASHBOARD_URL}/zid-callback?token={nahla_token}&redirect={path}"
    )
    logger.info("[Zid] ✅ Manager-token flow complete | tenant_id=%s user_id=%s", tenant_id, _user_id)
    return RedirectResponse(redirect_url, status_code=302)


async def _dispatch_zid_event(db: Session, tenant_id: int, event_type: str, payload: dict):
    """Route Zid webhook events to the appropriate handler."""
    logger.info("[Zid Webhook] Dispatching | tenant=%s event=%s", tenant_id, event_type)
    # Future: handle order.created, product.updated, customer.created …
