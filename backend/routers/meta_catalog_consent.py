"""
routers/meta_catalog_consent.py
───────────────────────────────
Catalog-only Meta consent entry for the ``/catalog`` dashboard page.

  GET  /merchant/catalog/meta-consent/status    JWT; availability + secret-free state
  POST /merchant/catalog/meta-consent/start     JWT (Authorization header); returns the
                                                Meta dialog URL — the JWT never enters a URL
  GET  /merchant/catalog/meta-consent/callback  exact JWT-public path; Meta's browser redirect

Off unless ``core.meta_catalog_consent_config`` reports every precondition
(feature flag, verified review environment, app credentials, dedicated
configuration id, exact https callback, dashboard URL, dedicated encryption
key, server-side approval of this tenant's catalog and business). The tenant
comes only from the validated JWT; the catalog and business only from the
server-side approval. Requested scopes: catalog_management and
business_management. No WhatsApp permission, WABA/phone discovery,
registration, subscription, send or coexistence change happens here, and no
WhatsApp connection is created or marked connected.

The callback consumes the durable single-use nonce (denials included) before
any Graph call, verifies what Meta granted, stores the token encrypted and
returns to the fixed ``<DASHBOARD_URL>/catalog`` with a fixed status code in
the fragment. Provider errors, codes, tokens and state are never reflected
or logged.
"""
from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from core.auth import PLATFORM_ADMIN_ROLES, get_jwt_tenant_id, require_authenticated
from core.database import get_db
from core.meta_catalog_consent_config import (
    CONFIG_ID_ENV,
    REQUESTED_SCOPES,
    approval_for_tenant,
    canonical_redirect_uri,
    dashboard_return_url,
    evaluate_consent_availability,
    meta_app_credentials,
)
from core.whatsapp_oauth_nonce import (
    NonceRejected,
    NonceStorageUnavailable,
    consume_catalog_consent_nonce,
    persist_catalog_consent_nonce,
)
from services import meta_catalog_consent as consent

logger = logging.getLogger("nahla.meta_catalog_consent")

router = APIRouter(prefix="/merchant/catalog/meta-consent", tags=["Merchant Catalog Consent"])

_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache", "Referrer-Policy": "no-referrer"}


def _entitlement_and_scope_code(db: Session, tenant_id: int) -> Optional[str]:
    """Tenant-level preconditions after the environment ones; None when all hold."""
    from core.plan_entitlements import get_entitlements  # noqa: PLC0415
    from services.whatsapp_catalog_sync_scope import tenant_in_sync_scope  # noqa: PLC0415

    # Consent enabled on a database without the 0120 table fails closed here,
    # before any nonce is issued or any Graph call is made.
    if not consent.authorization_table_exists(db):
        return consent.C_STORAGE_UNAVAILABLE
    try:
        ent = get_entitlements(db, int(tenant_id), strict_lookup=True)
        if not ent.has_feature("meta_catalog_sync"):
            return consent.C_ENTITLEMENT_MISSING
    except Exception:  # noqa: BLE001 — lookup failure is fail-closed, not a pass
        return consent.C_ENTITLEMENT_MISSING
    if not tenant_in_sync_scope(int(tenant_id)):
        return consent.C_SYNC_SCOPE_EXCLUDED
    return None


def _merchant_session(request: Request) -> int:
    """Tenant from the validated JWT; platform-admin and impersonation sessions refused."""
    payload = require_authenticated(request)
    if payload.get("impersonation") or str(payload.get("role") or "").strip() in PLATFORM_ADMIN_ROLES:
        raise HTTPException(status_code=403, detail={"error": "merchant_session_required"})
    return get_jwt_tenant_id(request)


@router.get("/status")
async def consent_status(request: Request, db: Session = Depends(get_db)):
    tenant_id = _merchant_session(request)
    availability = evaluate_consent_availability(tenant_id=tenant_id)
    body: Dict[str, Any] = {
        "available": availability.available,
        "reason": availability.reason,
        "requested_scopes": list(REQUESTED_SCOPES),
        "authorization": {"state": "none"},
    }
    if availability.available:
        blocker = _entitlement_and_scope_code(db, tenant_id)
        if blocker:
            body["available"] = False
            body["reason"] = blocker
        body["authorization"] = consent.authorization_summary(db, tenant_id)
        body["approved"] = {
            "catalog_id": availability.approval.catalog_id,
            "business_id": availability.approval.business_id,
        }
    return JSONResponse(body, headers=_NO_STORE)


@router.post("/start")
async def consent_start(request: Request, db: Session = Depends(get_db)):
    tenant_id = _merchant_session(request)
    availability = evaluate_consent_availability(tenant_id=tenant_id)
    if not availability.available:
        status = 404 if availability.reason == "disabled" else 409
        raise HTTPException(status_code=status, detail={"error": "catalog_consent_unavailable",
                                                         "reason": availability.reason})
    blocker = _entitlement_and_scope_code(db, tenant_id)
    if blocker:
        raise HTTPException(status_code=409, detail={"error": "catalog_consent_unavailable", "reason": blocker})

    app_id, _secret = meta_app_credentials()
    approval = availability.approval
    redirect_uri = availability.redirect_uri
    nonce = secrets.token_urlsafe(24)
    issued_at = int(datetime.now(timezone.utc).timestamp())
    try:
        persist_catalog_consent_nonce(
            db,
            nonce=nonce,
            tenant_id=tenant_id,
            redirect_uri=redirect_uri,
            catalog_id=approval.catalog_id,
            business_id=approval.business_id,
            expires_at=datetime.fromtimestamp(issued_at, tz=timezone.utc)
            + timedelta(seconds=consent.STATE_TTL_SECONDS),
        )
        db.commit()
    except (NonceStorageUnavailable, NonceRejected):
        db.rollback()
        logger.warning("[META_CATALOG_CONSENT] start refused tenant=%s reason=nonce_storage", tenant_id)
        raise HTTPException(status_code=503, detail={"error": "catalog_consent_unavailable",
                                                     "reason": consent.C_STORAGE_UNAVAILABLE}) from None
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.warning("[META_CATALOG_CONSENT] start refused tenant=%s reason=nonce_persist", tenant_id)
        raise HTTPException(status_code=503, detail={"error": "catalog_consent_unavailable",
                                                     "reason": consent.C_STORAGE_UNAVAILABLE}) from None

    state = consent.sign_state(
        tenant_id=tenant_id, nonce=nonce, redirect_uri=redirect_uri, approval=approval,
        app_id=app_id, issued_at=issued_at,
    )
    url = consent.build_authorize_url(
        state=state,
        redirect_uri=redirect_uri,
        app_id=app_id,
        config_id=str(os.environ.get(CONFIG_ID_ENV) or "").strip(),
        graph_version=consent._graph_version(),
    )
    logger.info("[META_CATALOG_CONSENT] start tenant=%s", tenant_id)
    return JSONResponse({"authorize_url": url, "expires_in": consent.STATE_TTL_SECONDS}, headers=_NO_STORE)


def _finish(return_url: Optional[str], code: str) -> Any:
    """Fixed return target and a fixed code; nothing from the request is reflected."""
    safe = code if code in consent.RESULT_CODES else consent.C_ERROR
    if not return_url:
        return JSONResponse({"status": safe}, status_code=400, headers=_NO_STORE)
    return RedirectResponse(f"{return_url}#meta_catalog_consent={safe}", status_code=302, headers=_NO_STORE)


@router.get("/callback")
async def consent_callback(
    request: Request,
    db: Session = Depends(get_db),
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
):
    environment = evaluate_consent_availability()
    # Even when unavailable, return to the fixed review dashboard if (and only
    # if) DASHBOARD_URL is a valid non-production https origin.
    return_url = environment.dashboard_return_url or dashboard_return_url()
    if not environment.available:
        logger.warning("[META_CATALOG_CONSENT] callback refused reason=%s", environment.reason)
        return _finish(return_url, consent.C_UNAVAILABLE)
    tenant_id: Optional[int] = None
    try:
        if not state:
            raise consent.ConsentError(consent.C_INVALID_STATE)
        parsed = consent.verify_state(state)
        tenant_id = parsed.tenant_id
        try:
            consume_catalog_consent_nonce(
                nonce=parsed.nonce,
                tenant_id=parsed.tenant_id,
                redirect_uri=parsed.redirect_uri,
                catalog_id=parsed.catalog_id,
                business_id=parsed.business_id,
            )
        except NonceRejected:
            raise consent.ConsentError(consent.C_REPLAYED) from None
        except NonceStorageUnavailable:
            raise consent.ConsentError(consent.C_STORAGE_UNAVAILABLE) from None

        # The nonce is spent: a denial, an error and a success all end here once.
        if error:
            raise consent.ConsentError(consent.C_DENIED)
        if parsed.redirect_uri != canonical_redirect_uri():
            raise consent.ConsentError(consent.C_CALLBACK_MISMATCH)
        app_id, app_secret = meta_app_credentials()
        if parsed.app_id != app_id:
            raise consent.ConsentError(consent.C_APP_MISMATCH)
        approval = approval_for_tenant(parsed.tenant_id)
        if approval is None:
            raise consent.ConsentError(consent.C_NOT_APPROVED)
        if (approval.catalog_id, approval.business_id) != (parsed.catalog_id, parsed.business_id):
            raise consent.ConsentError(consent.C_ASSET_CHANGED)
        blocker = _entitlement_and_scope_code(db, parsed.tenant_id)
        if blocker:
            raise consent.ConsentError(blocker)
        from models import Tenant  # noqa: PLC0415

        if db.query(Tenant.id).filter(Tenant.id == parsed.tenant_id).first() is None:
            raise consent.ConsentError(consent.C_TENANT_MISSING)
        if not code:
            raise consent.ConsentError(consent.C_MISSING_CODE)

        token, _expires_in = await consent.exchange_code(
            code=code, redirect_uri=parsed.redirect_uri, app_id=app_id, app_secret=app_secret,
        )
        verified = await consent.verify_access(token=token, approval=approval, app_id=app_id, app_secret=app_secret)
        consent.persist_authorization(db, approval=approval, verified=verified, app_id=app_id)
    except consent.ConsentError as exc:
        logger.warning("[META_CATALOG_CONSENT] callback refused tenant=%s code=%s", tenant_id, exc.code)
        return _finish(return_url, exc.code)
    except Exception as exc:  # noqa: BLE001 — never reflect or log the provider payload
        try:
            db.rollback()
        except Exception:  # noqa: silent-ok — best-effort rollback; the callback already returns a fixed error
            pass
        logger.error("[META_CATALOG_CONSENT] callback failed tenant=%s kind=%s", tenant_id, type(exc).__name__)
        return _finish(return_url, consent.C_ERROR)
    logger.info("[META_CATALOG_CONSENT] callback connected tenant=%s", tenant_id)
    return _finish(return_url, consent.C_CONNECTED)
