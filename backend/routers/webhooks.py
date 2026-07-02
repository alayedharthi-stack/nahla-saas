"""
routers/webhooks.py
────────────────────
Unified webhook handler for all payment and platform webhooks.

Routes
  POST /webhook/salla
  POST /webhook/salla-oauth
  POST /payments/webhook/moyasar
  POST /billing/webhook/moyasar/subscription
  POST /webhook/hyperpay
"""
from __future__ import annotations

import json as _json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from models import (  # noqa: E402
    BillingPayment,
    BillingSubscription,
    Order,
    PaymentSession,
    Tenant,
    User,
)

from core.audit import audit
from core.billing import get_moyasar_settings
from core.config import (
    HYPERPAY_WEBHOOK_SECRET,
    MOYASAR_SECRET_KEY,
    MOYASAR_WEBHOOK_SECRET,
    SALLA_OAUTH_WEBHOOK_SECRET,
    SALLA_WEBHOOK_SECRET,
    SALLA_WEBHOOK_ENFORCE_SIGNATURE,
    SALLA_WEBHOOK_ALLOW_MISSING_SIGNATURE,
)
from core.database import get_db
from core.obs import EVENTS, log_event
from core.webhook_audit import record_result as _record_signature_audit
from core.webhook_enforcement import resolve_enforce
from core.webhook_events import (
    STATUS_FAILED,
    STATUS_RECEIVED,
    persist_event,
)
from core.webhook_security import (
    SignatureStatus,
    evaluate_replay,
    verify_salla_signature,
)

logger = logging.getLogger("nahla-backend")

router = APIRouter(tags=["Webhooks"])

_MOYASAR_FAIL_STATUSES = frozenset({"failed", "expired", "canceled", "voided", "refunded"})
_BILLING_ACTIVATABLE   = frozenset({"pending_payment"})


def _resolve_salla_tenant_id(db: Session, store_id: Optional[str]) -> Optional[int]:
    """Best-effort: map a Salla ``store_id`` to a Nahla ``tenant_id``.

    Used to look up per-tenant webhook enforcement overrides BEFORE we
    decide whether to reject. Returns ``None`` when the mapping cannot
    be resolved — callers fall back to the global flag.

    We import lazily and swallow every error: webhook ingress must NEVER
    fail because a tenant lookup blew up.
    """
    if not store_id:
        return None
    try:
        from database.models import Integration  # noqa: PLC0415
        row = (
            db.query(Integration)
            .filter(
                Integration.provider == "salla",
                Integration.external_id == str(store_id),
            )
            .first()
        )
        return int(row.tenant_id) if row and row.tenant_id else None
    except Exception:  # noqa: BLE001
        return None


def _peek_salla_store_id(raw_body: bytes) -> Optional[str]:
    """Extract ``store_id`` / ``merchant`` from a raw Salla payload.

    The body has not been HMAC-verified yet, so we only use this for
    looking up the tenant's per-tenant enforcement flag — we do NOT
    trust the value for any business logic.
    """
    try:
        peek = _json.loads(raw_body or b"{}") if raw_body else {}
    except Exception:
        return None
    if not isinstance(peek, dict):
        return None
    sid = (
        peek.get("store_id")
        or peek.get("merchant")
        or (peek.get("data") or {}).get("store_id")
        or (peek.get("data") or {}).get("merchant")
    )
    return str(sid) if sid else None


def _decide_salla(
    *,
    result,
    enforce: bool,
) -> tuple[bool, str]:
    """Translate ``VerificationResult`` + enforce flag into ``(accept, reason)``.

    Behaviour matrix (matches the legacy logic so existing call sites are
    unaffected):

    ``SECRET_NOT_CONFIGURED`` → accept, log SIG_SKIP (we have nothing to
                                verify against — flagging would break every
                                webhook in deployments that haven't set the
                                secret yet).
    ``MISSING``               → accept when ``enforce=false`` OR
                                ``ALLOW_MISSING=true``; reject otherwise.
    ``VALID``                 → accept.
    ``INVALID``               → accept when ``enforce=false``; reject when
                                ``enforce=true``.
    """
    status = result.status
    if status == SignatureStatus.SECRET_NOT_CONFIGURED:
        return True, f"SIG_SKIP: {result.detail}"
    if status == SignatureStatus.VALID:
        return True, "SIG_PASS: valid signature"
    if status == SignatureStatus.MISSING:
        if not enforce:
            return True, "SIG_SKIP: signature missing, enforcement OFF"
        if SALLA_WEBHOOK_ALLOW_MISSING_SIGNATURE:
            return True, "SIG_WARN: signature missing, allowed by ALLOW_MISSING_SIGNATURE"
        return False, "SIG_REJECT: signature missing, enforcement ON + ALLOW_MISSING=false"
    # INVALID
    if not enforce:
        return True, "SIG_WARN: invalid signature, enforcement OFF — accepted anyway"
    return False, "SIG_REJECT: invalid signature"


def _verify_salla_signature(
    raw_body: bytes,
    request_headers,
    *,
    db: Session,
    tenant_id: Optional[int] = None,
    request_meta: Optional[dict] = None,
) -> tuple[bool, str]:
    """Verify Salla Communication-app webhook HMAC-SHA256 signature.

    Returns ``(should_accept, log_reason)``. Per-tenant override via
    ``TenantSettings.extra_metadata.webhook_enforcement.salla.enforce``
    takes precedence over the global ``SALLA_WEBHOOK_ENFORCE_SIGNATURE``
    env flag — ops can flip merchants individually after Partner Portal
    config is verified.
    """
    sig_header = request_headers.get("x-salla-signature", "") or request_headers.get(
        "X-Salla-Signature", "",
    )
    result = verify_salla_signature(
        raw_body=raw_body,
        header_value=sig_header,
        secret=SALLA_WEBHOOK_SECRET or None,
        provider_label="salla",
    )
    try:
        meta = dict(request_meta or {})
        meta.setdefault("signature_header_sample", sig_header)
        _record_signature_audit(result, tenant_id=tenant_id, request_meta=meta)
    except Exception:  # noqa: silent-ok — audit is best-effort
        pass

    enforce = resolve_enforce(
        db, tenant_id, "salla",
        global_default=SALLA_WEBHOOK_ENFORCE_SIGNATURE,
    )
    return _decide_salla(result=result, enforce=enforce)


def _verify_salla_oauth_signature(
    raw_body: bytes,
    request_headers,
    *,
    db: Session,
    tenant_id: Optional[int] = None,
    request_meta: Optional[dict] = None,
) -> tuple[bool, str]:
    """Verify Salla Sync OAuth-app webhook signature.

    Same algorithm as ``_verify_salla_signature`` but uses the dedicated
    ``SALLA_OAUTH_WEBHOOK_SECRET``. Per-tenant override is checked under
    the ``salla_oauth`` provider key so ops can stage Communication-app
    enforcement independently of Sync-app enforcement.
    """
    sig_header = request_headers.get("x-salla-signature", "") or request_headers.get(
        "X-Salla-Signature", "",
    )
    result = verify_salla_signature(
        raw_body=raw_body,
        header_value=sig_header,
        secret=SALLA_OAUTH_WEBHOOK_SECRET or None,
        provider_label="salla_oauth",
    )
    try:
        meta = dict(request_meta or {})
        meta.setdefault("signature_header_sample", sig_header)
        _record_signature_audit(result, tenant_id=tenant_id, request_meta=meta)
    except Exception:  # noqa: silent-ok — audit is best-effort
        pass

    enforce = resolve_enforce(
        db, tenant_id, "salla_oauth",
        global_default=SALLA_WEBHOOK_ENFORCE_SIGNATURE,
    )
    return _decide_salla(result=result, enforce=enforce)


# ── Salla ─────────────────────────────────────────────────────────────────────

@router.post("/webhook/salla")
async def salla_webhook(request: Request, db: Session = Depends(get_db)):
    """
    Durable receiver for Salla webhooks.

    Responsibilities (in order):
      1. Read raw body + verify HMAC signature.
      2. Parse JSON (failures are still persisted with status='failed').
      3. Persist the raw event to `webhook_events` and COMMIT.
      4. Return 200 OK immediately — a 200 from this endpoint ONLY means
         "event received and durably stored", NOT "business logic ran".

    All business processing (order upsert, customer recompute, coupon
    triggers, OAuth token saves, uninstall handling) is performed
    asynchronously by `core.webhook_dispatcher` claiming rows from
    `webhook_events`.

    Failures in the dispatcher retry with exponential backoff and land in
    `status='dead_letter'` for admin replay — nothing is silently lost.
    """
    raw_body  = await request.body()
    client_ip = request.headers.get("X-Real-IP") or (
        request.client.host if request.client else "unknown"
    )

    # ── HIT LOG (very first thing — visible in Railway tail) ─────────────────
    # This MUST appear in logs even if signature verification or JSON parsing
    # fails below.  If you don't see this line for an Easy-mode reinstall,
    # Salla is NOT delivering to https://api.nahlah.ai/webhook/salla — check
    # Webhook URL in https://salla.dev/dashboard for your app.
    _hit_event = "?"
    _hit_store = "?"
    _hit_has_access = False
    _hit_has_refresh = False
    try:
        _peek = _json.loads(raw_body or b"{}") if raw_body else {}
        if isinstance(_peek, dict):
            _hit_event = str(_peek.get("event") or "?")
            _hit_store = str(
                _peek.get("merchant")
                or _peek.get("store_id")
                or (_peek.get("data") or {}).get("merchant")
                or (_peek.get("data") or {}).get("store_id")
                or "?"
            )
            _data = _peek.get("data") or {}
            if isinstance(_data, dict):
                _hit_has_access  = bool(_data.get("access_token") or _peek.get("access_token"))
                _hit_has_refresh = bool(_data.get("refresh_token") or _peek.get("refresh_token"))
    except Exception:
        pass
    logger.info(
        "[Salla Webhook HIT] method=POST path=/webhook/salla ip=%s "
        "event=%s store_id=%s has_access_token=%s has_refresh_token=%s "
        "body_len=%s content_type=%s ua=%s",
        client_ip, _hit_event, _hit_store, _hit_has_access, _hit_has_refresh,
        len(raw_body),
        request.headers.get("content-type", ""),
        (request.headers.get("user-agent", "") or "")[:80],
    )

    log_event(
        EVENTS.WEBHOOK_RECEIVED,
        provider="salla",
        ip=client_ip,
        body_len=len(raw_body),
        content_type=request.headers.get("content-type", ""),
        user_agent=(request.headers.get("user-agent", "") or "")[:80],
    )

    # ── 1. Signature verification ────────────────────────────────────────────
    # Resolve tenant_id BEFORE verifying so a per-tenant enforcement
    # override applies to this request (Phase 1B per-merchant rollout).
    _peek_store_id = _peek_salla_store_id(raw_body)
    _peek_tenant_id = _resolve_salla_tenant_id(db, _peek_store_id)
    sig_accepted, sig_reason = _verify_salla_signature(
        raw_body,
        request.headers,
        db=db,
        tenant_id=_peek_tenant_id,
        request_meta={
            "ip": client_ip,
            "user_agent": (request.headers.get("user-agent", "") or "")[:120],
            "store_id": _peek_store_id,
        },
    )
    signature_valid = sig_accepted

    if not sig_accepted:
        # Persist the rejected event for audit, then reject so the caller
        # knows we won't process it. This is the only HTTP error we return.
        log_event(
            EVENTS.WEBHOOK_SIGNATURE_INVALID,
            provider="salla",
            reason=sig_reason,
            ip=client_ip,
        )
        try:
            persist_event(
                db,
                provider="salla",
                raw_body=raw_body,
                headers=request.headers,
                signature_valid=False,
                initial_status=STATUS_FAILED,
                initial_error=f"signature_invalid: {sig_reason}",
            )
        except Exception as _exc:
            logger.exception("[Salla WH] Could not persist rejected event: %s", _exc)
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    # Replay protection (Phase 1B-5) — flag-gated; no-op by default.
    # Salla legitimate retries arrive with the same body for hours, so the
    # default replay TTL of 24h could reject genuine retries — we run this
    # ONLY when ops have flipped both flags after observing actual retry
    # rate. Salla also has app-level dedup via ``external_event_id`` which
    # remains the primary defense.
    if evaluate_replay(
        "salla",
        raw_body,
        tenant_id=_peek_tenant_id,
        request_meta={"ip": client_ip, "store_id": _peek_store_id},
    ):
        return JSONResponse({"status": "ignored", "reason": "replay"})

    # ── 2. JSON parsing (tolerant) ───────────────────────────────────────────
    parsed_payload: dict | None = None
    parse_error: str | None = None
    try:
        parsed_payload = await request.json()
        if not isinstance(parsed_payload, dict):
            parse_error = f"payload_not_object: {type(parsed_payload).__name__}"
            parsed_payload = None
    except Exception as exc:
        parse_error = f"invalid_json: {exc}"
        log_event(
            EVENTS.WEBHOOK_INVALID_JSON,
            provider="salla",
            ip=client_ip,
            err=exc,
            raw_preview=raw_body[:200].decode("utf-8", errors="replace") if raw_body else "",
        )

    # ── 3. Extract event metadata for indexing ───────────────────────────────
    event_type: str | None = None
    store_id: str | None = None
    external_event_id: str | None = None
    if parsed_payload is not None:
        event_type = parsed_payload.get("event") or None
        store_id = str(parsed_payload.get("merchant") or parsed_payload.get("store_id") or "") or None
        data = parsed_payload.get("data") or {}
        if isinstance(data, dict):
            # Salla uses `id` inside `data` for orders/products/customers.
            # Combine with event_type to form a synthetic external_event_id
            # that's deterministic for this event so retries idempotent.
            entity_id = data.get("id")
            if entity_id is not None and event_type:
                external_event_id = f"salla:{event_type}:{entity_id}"

    audit("salla_webhook", salla_event=event_type or "unknown", store_id=store_id or "unknown", ip=client_ip)

    # ── 4. Persist durably (the ONLY business effect of this handler) ────────
    initial_status = STATUS_RECEIVED if parsed_payload is not None else STATUS_FAILED
    initial_error = parse_error

    try:
        ev = persist_event(
            db,
            provider="salla",
            raw_body=raw_body,
            headers=request.headers,
            parsed_payload=parsed_payload,
            event_type=event_type,
            external_event_id=external_event_id,
            store_id=store_id,
            signature_valid=signature_valid,
            initial_status=initial_status,
            initial_error=initial_error,
        )
    except Exception as exc:
        # Could not even persist — this is a real outage. Return 500 so
        # Salla will retry; returning 200 here would silently lose the event.
        logger.exception("[Salla WH] FATAL: could not persist webhook event: %s", exc)
        raise HTTPException(status_code=500, detail="webhook persistence failure")

    return {
        "status": "received",
        "webhook_event_id": ev.id,
        "event": event_type or "unknown",
    }


# ── Salla "Sync" OAuth app — separate webhook endpoint with its own secret ──

@router.post("/webhook/salla-oauth")
async def salla_oauth_webhook(request: Request, db: Session = Depends(get_db)):
    """
    Durable receiver for the SECOND Salla app (Custom OAuth Sync app).

    Why a separate endpoint?
    ────────────────────────
    The Sync OAuth app (SALLA_OAUTH_CLIENT_ID) has its OWN webhook secret in
    Salla Partner Portal — completely independent from the Communication
    App's secret.  Per the Dual Integration Architecture we keep the two
    streams strictly separated:

      • POST /webhook/salla       — Communication App,  SALLA_WEBHOOK_SECRET
      • POST /webhook/salla-oauth — Sync OAuth App,     SALLA_OAUTH_WEBHOOK_SECRET

    This handler MUST NOT fall back to SALLA_WEBHOOK_SECRET under any
    circumstance — a request signed with the Communication App's secret
    arriving here is treated as a wrong-app delivery and rejected.

    Persistence model
    ─────────────────
    Events are persisted with ``provider='salla_oauth'`` so they show up as
    a distinct stream in ``webhook_events`` (and are routed to the
    ``salla_oauth`` dispatcher in ``core.webhook_dispatcher``).  The
    business logic reuses ``_dispatch_salla`` because the event payload
    schema is identical — but the dispatcher tags ``app_origin='sync_oauth'``
    so the resulting Integration row carries ``api_sync_enabled=True``,
    matching what ``/api/salla/oauth/callback`` writes for the synchronous
    OAuth path.
    """
    raw_body  = await request.body()
    client_ip = request.headers.get("X-Real-IP") or (
        request.client.host if request.client else "unknown"
    )

    # ── HIT LOG (mirrors /webhook/salla so ops can grep both streams) ────────
    _hit_event = "?"
    _hit_store = "?"
    _hit_has_access  = False
    _hit_has_refresh = False
    try:
        _peek = _json.loads(raw_body or b"{}") if raw_body else {}
        if isinstance(_peek, dict):
            _hit_event = str(_peek.get("event") or "?")
            _hit_store = str(
                _peek.get("merchant")
                or _peek.get("store_id")
                or (_peek.get("data") or {}).get("merchant")
                or (_peek.get("data") or {}).get("store_id")
                or "?"
            )
            _data = _peek.get("data") or {}
            if isinstance(_data, dict):
                _hit_has_access  = bool(_data.get("access_token") or _peek.get("access_token"))
                _hit_has_refresh = bool(_data.get("refresh_token") or _peek.get("refresh_token"))
    except Exception:
        pass
    logger.info(
        "[Salla OAuth Webhook HIT] method=POST path=/webhook/salla-oauth ip=%s "
        "event=%s store_id=%s has_access_token=%s has_refresh_token=%s "
        "body_len=%s content_type=%s ua=%s",
        client_ip, _hit_event, _hit_store, _hit_has_access, _hit_has_refresh,
        len(raw_body),
        request.headers.get("content-type", ""),
        (request.headers.get("user-agent", "") or "")[:80],
    )

    log_event(
        EVENTS.WEBHOOK_RECEIVED,
        provider="salla_oauth",
        ip=client_ip,
        body_len=len(raw_body),
        content_type=request.headers.get("content-type", ""),
        user_agent=(request.headers.get("user-agent", "") or "")[:80],
    )

    # ── 1. Signature verification (USES THE OAUTH-APP SECRET ONLY) ───────────
    _peek_store_id = _peek_salla_store_id(raw_body)
    _peek_tenant_id = _resolve_salla_tenant_id(db, _peek_store_id)
    sig_accepted, sig_reason = _verify_salla_oauth_signature(
        raw_body,
        request.headers,
        db=db,
        tenant_id=_peek_tenant_id,
        request_meta={
            "ip": client_ip,
            "user_agent": (request.headers.get("user-agent", "") or "")[:120],
            "store_id": _peek_store_id,
        },
    )
    signature_valid = sig_accepted

    if not sig_accepted:
        log_event(
            EVENTS.WEBHOOK_SIGNATURE_INVALID,
            provider="salla_oauth",
            reason=sig_reason,
            ip=client_ip,
        )
        try:
            persist_event(
                db,
                provider="salla_oauth",
                raw_body=raw_body,
                headers=request.headers,
                signature_valid=False,
                initial_status=STATUS_FAILED,
                initial_error=f"signature_invalid: {sig_reason}",
            )
        except Exception as _exc:
            logger.exception("[Salla OAuth WH] Could not persist rejected event: %s", _exc)
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    # Replay protection (Phase 1B-5) — flag-gated; no-op by default.
    if evaluate_replay(
        "salla_oauth",
        raw_body,
        tenant_id=_peek_tenant_id,
        request_meta={"ip": client_ip, "store_id": _peek_store_id},
    ):
        return JSONResponse({"status": "ignored", "reason": "replay"})

    # ── 2. JSON parsing (tolerant) ───────────────────────────────────────────
    parsed_payload: dict | None = None
    parse_error: str | None = None
    try:
        parsed_payload = await request.json()
        if not isinstance(parsed_payload, dict):
            parse_error = f"payload_not_object: {type(parsed_payload).__name__}"
            parsed_payload = None
    except Exception as exc:
        parse_error = f"invalid_json: {exc}"
        log_event(
            EVENTS.WEBHOOK_INVALID_JSON,
            provider="salla_oauth",
            ip=client_ip,
            err=exc,
            raw_preview=raw_body[:200].decode("utf-8", errors="replace") if raw_body else "",
        )

    # ── 3. Extract event metadata for indexing ───────────────────────────────
    event_type: str | None = None
    store_id: str | None = None
    external_event_id: str | None = None
    if parsed_payload is not None:
        event_type = parsed_payload.get("event") or None
        store_id = str(parsed_payload.get("merchant") or parsed_payload.get("store_id") or "") or None
        data = parsed_payload.get("data") or {}
        if isinstance(data, dict):
            entity_id = data.get("id")
            if entity_id is not None and event_type:
                # Namespace under salla_oauth so it cannot collide with an
                # event from the Communication App carrying the same id.
                external_event_id = f"salla_oauth:{event_type}:{entity_id}"

    audit(
        "salla_oauth_webhook",
        salla_event=event_type or "unknown",
        store_id=store_id or "unknown",
        ip=client_ip,
    )

    # ── 4. Persist with provider='salla_oauth' (separate stream) ─────────────
    initial_status = STATUS_RECEIVED if parsed_payload is not None else STATUS_FAILED
    initial_error = parse_error

    try:
        ev = persist_event(
            db,
            provider="salla_oauth",
            raw_body=raw_body,
            headers=request.headers,
            parsed_payload=parsed_payload,
            event_type=event_type,
            external_event_id=external_event_id,
            store_id=store_id,
            signature_valid=signature_valid,
            initial_status=initial_status,
            initial_error=initial_error,
        )
    except Exception as exc:
        logger.exception("[Salla OAuth WH] FATAL: could not persist webhook event: %s", exc)
        raise HTTPException(status_code=500, detail="webhook persistence failure")

    return {
        "status": "received",
        "webhook_event_id": ev.id,
        "event": event_type or "unknown",
        "app": "salla_oauth_sync",
    }


def _resolve_tenant_from_store(db, store_id) -> int | None:
    """Look up the Nahla tenant_id that owns a given Salla store_id."""
    from models import Integration  # noqa: PLC0415
    from services.salla_store_identity import (  # noqa: PLC0415
        find_salla_integration_by_identity,
        SallaStoreIdentity,
        resolve_tenant_for_salla_store,
    )

    sid = str(store_id or "").strip()
    if not sid:
        return None

    tenant_id, integration, matched_via = resolve_tenant_for_salla_store(
        db,
        SallaStoreIdentity(store_id=sid),
        include_disabled=False,
        allow_alias_match=True,
    )
    if tenant_id is not None:
        logger.info(
            "[Salla WH] resolved store_id=%s → tenant=%s (integration id=%s via=%s)",
            sid, tenant_id, integration.id if integration else None, matched_via,
        )
        return tenant_id

    disabled_integration, disabled_via = find_salla_integration_by_identity(
        db, sid, include_disabled=True, allow_alias_match=True,
    )
    if disabled_integration and not disabled_integration.enabled:
        logger.warning(
            "[Salla WH] store_id=%s found BUT disabled | tenant=%s enabled=%s via=%s — webhook ignored",
            sid, disabled_integration.tenant_id, disabled_integration.enabled, disabled_via,
        )
    else:
        logger.warning("[Salla WH] store_id=%s NOT FOUND in any integration — webhook dropped", sid)
    return None


def _trigger_easy_initial_sync(tenant_id: int, salla_store_id: str) -> None:
    """
    Fire-and-forget the same full_sync that the OAuth callback used to run.

    Called from `_handle_salla_authorize` after tokens are persisted, in
    BOTH the existing-integration branch (re-install / token refresh) and
    the new-integration branch (first install via Easy Mode).

    Runs in a background asyncio task with its own DB session so the
    webhook receiver can return 200 OK immediately — Salla retries any
    webhook that doesn't get a 200 within ~10s.
    """
    try:
        import asyncio as _asyncio  # noqa: PLC0415

        async def _do_sync(tid: int, sid: str) -> None:
            # Small delay so the integration row is fully visible to other
            # sessions and any post-commit hooks have settled.
            await _asyncio.sleep(2)
            from core.database import get_db as _gdb  # noqa: PLC0415
            from services.store_sync import StoreSyncService  # noqa: PLC0415

            logger.info(
                "[Salla Easy] initial sync started | tenant=%s store=%s",
                tid, sid,
            )
            _db = next(_gdb())
            try:
                svc = StoreSyncService(_db, tid)
                result = await svc.full_sync(triggered_by="easy_mode_webhook")
                logger.info(
                    "[Salla Easy] initial sync completed | tenant=%s store=%s "
                    "status=%s products=%s customers=%s orders=%s coupons=%s "
                    "abandoned_carts=%s",
                    tid, sid,
                    result.get("status"),
                    result.get("products_synced", 0),
                    result.get("customers_synced", 0),
                    result.get("orders_synced", 0),
                    result.get("coupons_synced", 0),
                    result.get("abandoned_carts_synced", 0),
                )
            except Exception as exc:
                logger.error(
                    "[Salla Easy] initial sync FAILED | tenant=%s store=%s err=%s",
                    tid, sid, exc,
                )
            finally:
                try:
                    _db.close()
                except Exception:
                    pass

        _asyncio.ensure_future(_do_sync(tenant_id, salla_store_id))
        logger.info(
            "[Salla Easy] initial sync task queued | tenant=%s store=%s",
            tenant_id, salla_store_id,
        )
    except Exception as exc:
        logger.warning(
            "[Salla Easy] could not queue initial sync | tenant=%s store=%s err=%s",
            tenant_id, salla_store_id, exc,
        )


async def _handle_salla_authorize(
    db, store_id, data: dict, payload: dict,
    *, app_origin: str = "easy_mode",
) -> None:
    """
    Save Salla OAuth tokens received via webhook + trigger initial sync.

    Handles three Easy Mode events from Salla:
      • app.store.authorize  — tokens delivered after merchant authorizes
      • app.store.token      — tokens refreshed by Salla
      • app.installed        — install confirmation (may or may not carry tokens)

    Behaviour:
      • Persists access_token + refresh_token + store metadata in
        `integrations.config` with `app_type='easy'` and
        `api_key_source='easy_mode_webhook'`.
      • Sets `external_store_id` on the Integration row so webhook routing
        and tenant resolution work correctly.
      • Re-activates a soft-disabled integration if the merchant
        re-installs after uninstalling.
      • Fires a background `StoreSyncService.full_sync` so products,
        customers, orders, and coupons are pre-loaded before the merchant
        clicks 'استخدام التطبيق' inside Salla.

    ``app_origin`` controls which app-type fingerprint we stamp on the
    Integration row:

      • ``"easy_mode"`` (default) — Communication App / Easy Mode webhook.
      • ``"sync_oauth"`` — the SECOND Custom OAuth app (Sync app).  In this
        case the row is also flagged with ``api_sync_enabled=True`` and
        ``api_canonical=True`` so ``pick_active_salla_integration`` treats
        it as the canonical source of refresh_token (matches the markers
        written by ``/api/salla/oauth/callback``).
    """
    is_sync_oauth = (app_origin == "sync_oauth")
    app_type_label    = "custom_oauth_sync" if is_sync_oauth else "easy"
    api_key_src_label = "custom_oauth_sync_webhook" if is_sync_oauth else "easy_mode_webhook"
    from models import Integration, Tenant, User  # noqa: PLC0415
    from core.auth import hash_password, create_token  # noqa: PLC0415
    from core.tenant import get_or_create_tenant  # noqa: PLC0415
    import secrets as _sec  # noqa: PLC0415

    # Token may be nested under data or at root level
    access_token  = (data.get("access_token")  or payload.get("access_token",  "")).strip()
    refresh_token = (data.get("refresh_token") or payload.get("refresh_token", "")).strip()
    expires_in    = data.get("expires_in")    or payload.get("expires_in", 0)
    from services.salla_store_identity import normalize_salla_ids_from_event_data  # noqa: PLC0415

    event_identity = normalize_salla_ids_from_event_data(data)
    salla_store_id = event_identity.store_id
    store_name     = event_identity.store_name or (
        data.get("store", {}).get("name", "")
        if isinstance(data.get("store"), dict) else
        data.get("name", "")
    ) or f"متجر سلة {salla_store_id}"

    logger.info(
        "[Salla Easy] authorize received | store_id=%s has_access_token=%s "
        "has_refresh_token=%s expires_in=%s store_name=%s event=%s",
        salla_store_id, bool(access_token), bool(refresh_token),
        expires_in, store_name, payload.get("event", "?"),
    )

    # ── Find existing integration by salla store_id (canonical + aliases) ───
    from services.salla_store_identity import (  # noqa: PLC0415
        find_salla_integration_for_identity,
        promote_integration_canonical_store,
        SallaStoreIdentity,
    )

    existing_integration = None
    try:
        identity = SallaStoreIdentity(
            store_id=salla_store_id,
            merchant_account_id=event_identity.merchant_account_id,
            alias_ids=event_identity.alias_ids,
        )
        existing_integration, matched_via = find_salla_integration_for_identity(
            db, identity, include_disabled=True, allow_alias_match=True,
        )
        if existing_integration and salla_store_id:
            promote_integration_canonical_store(db, existing_integration, identity)
    except Exception as _e:
        logger.warning("[Salla Easy] integration lookup error: %s", _e)

    if existing_integration:
        from services.salla_guard import claim_store_for_tenant  # noqa: PLC0415

        was_disabled = not existing_integration.enabled
        tenant_id = existing_integration.tenant_id
        new_cfg = dict(existing_integration.config or {})
        _now_utc = datetime.now(timezone.utc)
        _expires_at: str | None = None
        if expires_in:
            try:
                _expires_at = (_now_utc + timedelta(seconds=int(expires_in))).isoformat()
            except Exception:
                pass

        new_cfg.update({
            "api_key":         access_token or new_cfg.get("api_key", ""),
            "refresh_token":   refresh_token or new_cfg.get("refresh_token", ""),
            "expires_in":      expires_in,
            "store_id":        salla_store_id,
            "store_name":      store_name,
            "connected_at":    _now_utc.isoformat(),
            "app_type":        app_type_label,
            "api_key_source":  api_key_src_label,
            # ── Easy Mode token tracking ─────────────────────────────────────
            "token_source":    api_key_src_label,
            "easy_mode":       not is_sync_oauth,
            "token_refresh_attempts": 0,
        })
        if _expires_at:
            new_cfg["expires_at"]       = _expires_at
            new_cfg["token_expires_at"] = _expires_at   # backward compat alias
        if refresh_token:
            new_cfg["refresh_token_received_at"] = _now_utc.isoformat()
        # Clear stale refresh-failure markers — fresh token from Salla supersedes them
        new_cfg.pop("token_refresh_status",   None)
        new_cfg.pop("token_refresh_error",    None)
        new_cfg.pop("token_refresh_failed_at",None)
        if is_sync_oauth:
            # Mirror the markers written by /api/salla/oauth/callback so
            # _score_integration treats this row as the canonical Sync row.
            new_cfg["api_sync_enabled"] = True
            new_cfg["api_canonical"]    = True
            new_cfg["is_canonical"]     = True
        new_cfg.pop("soft_disabled",         None)
        new_cfg.pop("uninstalled_at",        None)
        new_cfg.pop("needs_reauth",          None)
        new_cfg.pop("needs_reauth_at",       None)
        new_cfg.pop("needs_reauth_reason",   None)
        # Also clear the no_auto_refresh flag that _mark_needs_reauth sets
        # when invalid_grant is returned.  Fresh tokens from Salla supersede it.
        new_cfg.pop("no_auto_refresh",       None)
        new_cfg.pop("no_auto_refresh_reason",None)
        new_cfg.pop("no_auto_refresh_at",    None)
        # Scrub markers left by /admin/debug/salla/cleanup so a manual
        # preflight followed by an Easy-mode reinstall produces a fully
        # clean config (no leftover disabled_reason / superseded flags).
        new_cfg.pop("superseded_by_oauth_reconnect", None)
        new_cfg.pop("disabled_reason",               None)
        new_cfg.pop("disabled_at",                   None)
        claim_store_for_tenant(
            db, store_id=salla_store_id, tenant_id=tenant_id, new_config=new_cfg,
        )
        db.commit()
        logger.info(
            "[SALLA TOKEN] authorize webhook token saved | "
            "tenant=%s store=%s has_access=%s has_refresh=%s expires_in=%s expires_at=%s",
            tenant_id, salla_store_id,
            bool(access_token), bool(refresh_token), expires_in, _expires_at,
        )
        if was_disabled:
            logger.info(
                "[Salla Easy] tokens saved (RE-ACTIVATED) | tenant=%s store=%s",
                tenant_id, salla_store_id,
            )
        else:
            logger.info(
                "[Salla Easy] tokens saved (refreshed existing integration) | "
                "tenant=%s store=%s",
                tenant_id, salla_store_id,
            )

        # Trigger initial sync only when we actually have a fresh access
        # token (not for token-less app.installed events that arrive before
        # app.store.authorize).
        if access_token:
            _trigger_easy_initial_sync(tenant_id, salla_store_id)
        return

    # ── No existing integration: auto-create a new Nahla merchant account ─────
    if not access_token:
        # app.installed without a token — just log and return (the
        # subsequent app.store.authorize event will carry the token and
        # land in this same handler).
        logger.info(
            "[Salla Easy] authorize received WITHOUT token — waiting for "
            "app.store.authorize event | store=%s", salla_store_id,
        )
        return

    try:
        # Auto-create tenant + user for this Salla store
        new_tenant = Tenant(name=store_name)
        db.add(new_tenant)
        db.flush()
        tenant_id = new_tenant.id

        salla_email = f"salla-{salla_store_id}@salla-merchant.nahlah.ai"
        new_user = User(
            username=f"salla-{salla_store_id}",
            email=salla_email,
            password_hash=hash_password(_sec.token_urlsafe(16)),
            role="merchant",
            tenant_id=tenant_id,
            is_active=True,
        )
        db.add(new_user)
        db.flush()

        _now_utc2 = datetime.now(timezone.utc)
        _expires_at2: str | None = None
        if expires_in:
            try:
                _expires_at2 = (_now_utc2 + timedelta(seconds=int(expires_in))).isoformat()
            except Exception:
                pass

        new_integ_cfg = {
            "api_key":         access_token,
            "refresh_token":   refresh_token,
            "store_id":        salla_store_id,
            "store_name":      store_name,
            "expires_in":      expires_in,
            "connected_at":    _now_utc2.isoformat(),
            "app_type":        app_type_label,
            "api_key_source":  api_key_src_label,
            # ── Easy Mode token tracking ──────────────────────────────────────
            "token_source":    api_key_src_label,
            "easy_mode":       not is_sync_oauth,
            "enabled":         True,
            "token_refresh_attempts": 0,
        }
        if _expires_at2:
            new_integ_cfg["expires_at"]       = _expires_at2
            new_integ_cfg["token_expires_at"] = _expires_at2
        if refresh_token:
            new_integ_cfg["refresh_token_received_at"] = _now_utc2.isoformat()
        if is_sync_oauth:
            new_integ_cfg["api_sync_enabled"] = True
            new_integ_cfg["api_canonical"]    = True
            new_integ_cfg["is_canonical"]     = True
        integration = Integration(
            tenant_id=tenant_id,
            provider="salla",
            external_store_id=salla_store_id,
            config=new_integ_cfg,
            enabled=True,
        )
        db.add(integration)
        db.commit()
        logger.info(
            "[SALLA TOKEN] authorize webhook token saved | "
            "tenant=%s store=%s has_access=%s has_refresh=%s expires_in=%s expires_at=%s (NEW)",
            tenant_id, salla_store_id,
            bool(access_token), bool(refresh_token), expires_in, _expires_at2,
        )
        logger.info(
            "[Salla Easy] tokens saved (NEW tenant + integration) | "
            "tenant=%s email=%s store=%s",
            tenant_id, salla_email, salla_store_id,
        )

        # Pre-load the merchant's data so the mini-dashboard isn't empty
        # when they click 'استخدام التطبيق' inside Salla a few seconds
        # later.
        _trigger_easy_initial_sync(tenant_id, salla_store_id)

    except Exception as exc:
        logger.exception("[Salla Easy] auto-create FAILED: %s", exc)
        try:
            db.rollback()
        except Exception:
            pass


def _disable_salla_integration(db, store_id: str) -> None:
    """Soft-disable integration on app.uninstalled.

    We keep api_key and refresh_token intact so the integration can
    automatically re-activate when Salla sends app.installed or
    app.store.authorize again (common with Easy-mode reinstalls).
    Tokens are preserved behind a ``soft_disabled`` flag — the
    _handle_salla_authorize path checks for this and re-enables.
    """
    from models import Integration  # noqa: PLC0415

    sid = str(store_id)
    integrations = db.query(Integration).filter(
        Integration.provider == "salla",
        Integration.external_store_id == sid,
    ).all()

    for intg in integrations:
        intg.enabled = False
        cfg = dict(intg.config or {})
        cfg["soft_disabled"] = True
        cfg["uninstalled_at"] = datetime.now(timezone.utc).isoformat()
        intg.config = cfg
        logger.warning(
            "[Salla Webhook] Integration SOFT-DISABLED (app.uninstalled) | "
            "tenant=%s store_id=%s — tokens preserved for auto-reactivation",
            intg.tenant_id, sid,
        )

    if integrations:
        db.commit()
    else:
        logger.warning("[Salla Webhook] app.uninstalled — no integration found for store_id=%s", sid)


# ── Moyasar payment webhook ───────────────────────────────────────────────────

@router.post("/payments/webhook/moyasar")
async def moyasar_webhook(request: Request, db: Session = Depends(get_db)):
    """
    Handle Moyasar payment webhook callbacks.
    Verifies HMAC-SHA256 signature and updates Order + PaymentSession status.
    """
    raw_body  = await request.body()
    signature = request.headers.get("signature", "")

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    meta           = data.get("metadata") or {}
    tenant_id      = int(meta.get("tenant_id", 0))
    order_id_str   = meta.get("order_id", "")
    payment_id     = data.get("id", "")
    payment_status = data.get("status", "")

    if tenant_id:
        cfg            = get_moyasar_settings(db, tenant_id)
        webhook_secret = cfg.get("webhook_secret", "")

        if webhook_secret and signature:
            sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
            from payment_gateways.moyasar import MoyasarClient  # noqa: PLC0415
            client = MoyasarClient(secret_key=cfg.get("secret_key", ""))
            if not client.verify_webhook_signature(raw_body, signature, webhook_secret):
                logger.warning("[Moyasar Webhook] Invalid signature for tenant=%s", tenant_id)
                raise HTTPException(status_code=401, detail="Invalid webhook signature")

        ps = (
            db.query(PaymentSession)
            .filter(
                PaymentSession.gateway_payment_id == payment_id,
                PaymentSession.tenant_id == tenant_id,
            )
            .first()
        )
        if ps:
            ps.status        = "paid" if payment_status in ("paid", "authorized") else "failed"
            ps.callback_data = data
            ps.updated_at    = datetime.now(timezone.utc)

        if order_id_str:
            try:
                oid   = int(order_id_str)
                order = db.query(Order).filter(
                    Order.id == oid, Order.tenant_id == tenant_id,
                ).first()
                if order:
                    if payment_status in ("paid", "authorized"):
                        order.status = "paid"
                        logger.info(
                            "[Moyasar Webhook] Order #%s marked paid for tenant=%s", oid, tenant_id,
                        )
                        # Emit automation event for order payment
                        try:
                            from core.automation_engine import emit_automation_event  # noqa: PLC0415
                            from models import Customer  # noqa: PLC0415
                            _ci = order.customer_info or {}
                            _raw_phone = _ci.get("mobile") or _ci.get("phone")
                            _cust = None
                            if _raw_phone:
                                from services.customer_intelligence import normalize_phone as _np  # noqa: PLC0415
                                _phone = _np(_raw_phone) or _raw_phone
                                _cust = db.query(Customer).filter(
                                    Customer.tenant_id == tenant_id,
                                    Customer.phone == _phone,
                                ).first()
                            emit_automation_event(
                                db, tenant_id, "order_paid",
                                customer_id=_cust.id if _cust else None,
                                payload={
                                    "order_id": oid,
                                    "payment_id": payment_id,
                                    "amount": data.get("amount"),
                                    "gateway": "moyasar",
                                },
                            )
                        except Exception as _ae:
                            logger.debug("[Webhook] emit order_paid failed: %s", _ae)

                        # Attribute the order back to its decision (if any).
                        # Failures here MUST NOT block the webhook ack.
                        try:
                            from services.offer_attribution_service import (  # noqa: PLC0415
                                attribute_order_to_decision,
                            )
                            attribute_order_to_decision(
                                db,
                                tenant_id=tenant_id,
                                order_id=oid,
                                payload={
                                    "amount": data.get("amount"),
                                    "payment_id": payment_id,
                                },
                            )
                        except Exception as _ae:
                            logger.debug("[Webhook] offer attribution failed: %s", _ae)
                    elif payment_status == "failed":
                        order.status = "payment_failed"
            except (ValueError, TypeError):
                pass

        from observability.event_logger import log_event  # noqa: PLC0415
        log_event(
            db, tenant_id, category="payment",
            event_type=f"payment.{payment_status}",
            summary=f"Moyasar {payment_status}: payment {payment_id}",
            severity="info" if payment_status in ("paid", "authorized") else "warning",
            payload={"payment_id": payment_id, "status": payment_status, "order_id": order_id_str},
            reference_id=order_id_str or payment_id,
        )
        db.commit()

    logger.info(
        "[Moyasar Webhook] id=%s status=%s tenant=%s", payment_id, payment_status, tenant_id,
    )
    return {"received": True}


# ── Moyasar billing subscription webhook ──────────────────────────────────────

@router.post("/billing/webhook/moyasar/subscription")
async def billing_webhook_moyasar(request: Request, db: Session = Depends(get_db)):
    """
    Moyasar payment webhook handler for subscription payments.

    Accepts BOTH event shapes Moyasar can deliver:

      * **Payment-level** (flat) — what the dashboard "payment_paid" event
        produces::

            {"id": "<payment>", "status": "paid", "metadata": {...}, ...}

      * **Invoice-level** (envelope) — what "invoice_paid" produces::

            {"id": "<event>", "type": "invoice.paid",
             "data": {"id": "<invoice>", "status": "paid",
                      "metadata": {...}, "payments": [...]}}

    Old code only understood the flat shape, so any merchant who
    happened to subscribe to invoice events in the dashboard saw the
    handler silently ignore every paid invoice with a misleading log
    line ``"Unhandled status '' …"``. The normaliser below collapses
    both shapes into a single ``payload`` dict before activation.

    Note: even with this handler in perfect shape, a Moyasar invoice's
    ``callback_url`` field is the **browser redirect URL** — not a
    webhook. To get server-to-server webhooks at all, the merchant must
    register one in the Moyasar dashboard. This endpoint exists for
    when they do; the primary activation path is the live reconcile
    inside ``GET /billing/payment-result``.
    """
    body_bytes = await request.body()
    signature  = request.headers.get("x-moyasar-signature", "")

    try:
        event = _json.loads(body_bytes)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # ── Normalise the two shapes into a single payload ────────────────
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
    from services.billing_activation import (  # noqa: PLC0415
        activate_subscription_from_moyasar_invoice,
        extract_payment_id_from_invoice,
        normalize_moyasar_event,
    )

    payload, event_shape = normalize_moyasar_event(event)
    status          = (payload.get("status") or "").lower()
    amount_h        = int(payload.get("amount") or 0)
    amount_sar      = amount_h // 100
    payment_meta    = payload.get("metadata") or {}
    subscription_id = payment_meta.get("subscription_id")
    tenant_id_raw   = payment_meta.get("tenant_id")

    # For invoice-shape events, the *payment* id lives inside payments[];
    # for payment-shape events, the top-level id IS the payment id.
    if event_shape == "invoice":
        payment_id = extract_payment_id_from_invoice(payload)
    else:
        payment_id = str(payload.get("id") or "")

    logger.info(
        "[Billing Webhook] shape=%s payment_id=%s invoice_id=%s status=%s sub=%s tenant=%s",
        event_shape, payment_id, payload.get("id") if event_shape == "invoice" else "—",
        status, subscription_id, tenant_id_raw,
    )

    if not subscription_id:
        logger.warning("[Billing Webhook] No subscription_id in metadata, ignoring")
        return {"received": True}

    sub = db.query(BillingSubscription).filter(
        BillingSubscription.id == int(subscription_id)
    ).first()

    if not sub:
        logger.warning("[Billing Webhook] Subscription %s not found", subscription_id)
        return {"received": True}

    cfg            = get_moyasar_settings(db, sub.tenant_id)
    webhook_secret = cfg.get("webhook_secret", "") or MOYASAR_WEBHOOK_SECRET
    if webhook_secret:
        sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
        from payment_gateways.moyasar import MoyasarClient  # noqa: PLC0415
        secret_key = cfg.get("secret_key", "") or MOYASAR_SECRET_KEY
        client = MoyasarClient(secret_key=secret_key)
        if not client.verify_webhook_signature(body_bytes, signature, webhook_secret):
            logger.warning(
                "[Billing Webhook] Invalid signature for sub=%s tenant=%s",
                subscription_id, sub.tenant_id,
            )
            raise HTTPException(status_code=401, detail="Invalid webhook signature")

    if status == "paid":
        # Build a synthetic invoice payload that the shared activation
        # helper understands. For invoice-shape events the payload IS
        # already an invoice; for payment-shape events we fabricate
        # a minimal one that exposes the same keys.
        if event_shape == "invoice":
            invoice_data = payload
        else:
            invoice_data = {
                "id":       payload.get("invoice_id") or payment_id,
                "status":   "paid",
                "amount":   amount_h,
                "metadata": payment_meta,
                "payments": [{"id": payment_id, "status": "paid"}],
            }

        activated, reason = activate_subscription_from_moyasar_invoice(
            db, sub,
            invoice_data=invoice_data,
            payment_id=payment_id,
            source=f"webhook_{event_shape}",
        )
        if activated:
            return {"received": True, "activated": True}
        if reason == "already_active":
            return {"received": True, "already_active": True}
        if reason == "duplicate_payment":
            return {"received": True, "idempotent": True}
        if reason == "unexpected_status":
            return {"received": True, "skipped": True, "reason": reason}
        # fall-through for invoice_not_paid / unknown — let it commit.
        return {"received": True, "noop": True, "reason": reason}

    elif status in _MOYASAR_FAIL_STATUSES:
        if sub.status == "active":
            logger.warning(
                "[Billing Webhook] Ignoring %r webhook for active sub %s — not downgrading",
                status, subscription_id,
            )
            return {"received": True, "protected": True}
        sub.status = "payment_failed"
        logger.info(
            "[Billing Webhook] Payment %r for subscription %s", status, subscription_id,
        )

        # Notify merchant about payment failure
        try:
            import asyncio  # noqa: PLC0415
            from core.notifications import send_email, email_payment_failed  # noqa: PLC0415
            from core.wa_notify import _send  # noqa: PLC0415

            tenant_obj = db.query(Tenant).filter(Tenant.id == sub.tenant_id).first()
            merchant   = db.query(User).filter(
                User.tenant_id == sub.tenant_id, User.role == "merchant",
                User.is_active == True,  # noqa: E712
            ).first()
            plan_obj   = db.query(BillingPlan).filter(BillingPlan.id == sub.plan_id).first() if sub.plan_id else None
            plan_name  = (plan_obj.name if plan_obj else None) or "الخطة"
            store_name = tenant_obj.name if tenant_obj else f"Tenant {sub.tenant_id}"
            amount_sar = float(payment_meta.get("amount", 0)) / 100

            if merchant and merchant.email:
                asyncio.ensure_future(send_email(
                    to=merchant.email,
                    subject=f"❌ فشل الدفع — يرجى تجديد اشتراك {plan_name}",
                    html=email_payment_failed(store_name, plan_name, amount_sar),
                ))
            phone = getattr(merchant, "username", "") if merchant else ""
            if phone:
                from core.wa_notify import _normalize_phone  # noqa: PLC0415
                wa_text = (
                    f"🔴 نحلة AI — فشل الدفع\n"
                    f"مرحباً {store_name}،\n"
                    f"لم تتم عملية الدفع لخطة {plan_name} بنجاح.\n"
                    f"يرجى تحديث بيانات الدفع:\nhttps://app.nahlah.ai/billing"
                )
                asyncio.ensure_future(_send(_normalize_phone(phone), wa_text))
        except Exception as notify_exc:
            logger.warning("[Billing Webhook] Payment-fail notification error: %s", notify_exc)
    else:
        logger.info(
            "[Billing Webhook] Unhandled status %r for sub %s — no action taken",
            status, subscription_id,
        )

    db.commit()
    return {"received": True}


# ── HyperPay webhook ──────────────────────────────────────────────────────────

@router.post("/webhook/hyperpay")
async def hyperpay_webhook(request: Request, db: Session = Depends(get_db)):
    """
    HyperPay webhook endpoint — confirms payment success for local Saudi methods.
    On success: subscription_status → 'active', billing_status → 'paid'.
    On failure: billing_status → 'failed'.
    """
    payload   = await request.body()
    iv        = request.headers.get("X-Initialization-Vector", "")
    signature = request.headers.get("X-Authentication-Tag", "")

    if not HYPERPAY_WEBHOOK_SECRET and not os.environ.get("HYPERPAY_ACCESS_TOKEN"):
        raise HTTPException(status_code=503, detail="HyperPay is not configured.")

    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
    from payment_gateways.hyperpay_client import HyperPayClient  # noqa: PLC0415
    from core.config import HYPERPAY_ACCESS_TOKEN, HYPERPAY_ENTITY_ID, HYPERPAY_LIVE_MODE  # noqa: PLC0415
    hp = HyperPayClient(
        access_token=HYPERPAY_ACCESS_TOKEN,
        entity_id=HYPERPAY_ENTITY_ID,
        webhook_secret=HYPERPAY_WEBHOOK_SECRET,
        live_mode=HYPERPAY_LIVE_MODE,
    )

    if HYPERPAY_WEBHOOK_SECRET:
        if not hp.verify_webhook_signature(payload, iv, signature):
            logger.warning("[HyperPay] Webhook signature verification failed")
            raise HTTPException(status_code=400, detail="Invalid HyperPay webhook signature")

    try:
        data = _json.loads(payload)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    checkout_id = data.get("id", "")
    result_code = data.get("result", {}).get("code", "")
    payment_id  = data.get("id", checkout_id)

    tenant = db.query(Tenant).filter(Tenant.hyperpay_payment_id == checkout_id).first()
    if tenant is None:
        logger.info("[HyperPay] Webhook: no tenant found for checkout_id=%s", checkout_id)
        return {"received": True}

    merchant = db.query(User).filter(
        User.tenant_id == tenant.id,
        User.role == "merchant",
        User.is_active == True,  # noqa: E712
    ).first()
    store_name = tenant.name or f"Tenant {tenant.id}"
    phone = getattr(merchant, "username", "") if merchant else ""
    email_addr = getattr(merchant, "email", "") if merchant else ""

    if hp.is_payment_successful(data):
        now = datetime.now(timezone.utc)
        tenant.subscription_status = "active"
        tenant.billing_status      = "paid"
        tenant.is_active           = True
        tenant.current_period_end  = now + timedelta(days=30)
        tenant.hyperpay_payment_id = payment_id
        db.commit()
        logger.info(
            "[HyperPay] Payment SUCCESS for tenant %s: code=%s period_end=%s",
            tenant.id, result_code, tenant.current_period_end.date(),
        )
        # Notify merchant — success
        try:
            import asyncio  # noqa: PLC0415
            from core.notifications import send_email, email_subscription  # noqa: PLC0415
            from core.wa_notify import notify_subscription_confirmed  # noqa: PLC0415
            ends_str = tenant.current_period_end.strftime("%Y-%m-%d")
            if email_addr:
                asyncio.ensure_future(send_email(
                    to=email_addr,
                    subject="✅ تم تفعيل اشتراكك — نحلة AI",
                    html=email_subscription(store_name, "HyperPay", ends_str),
                ))
            if phone:
                asyncio.ensure_future(notify_subscription_confirmed(
                    phone, store_name, "HyperPay", 0, ends_str,
                ))
        except Exception as exc:
            logger.warning("[HyperPay] Success notification error: %s", exc)
    else:
        tenant.billing_status = "failed"
        db.commit()
        logger.warning(
            "[HyperPay] Payment FAILED for tenant %s: code=%s desc=%s",
            tenant.id, result_code, data.get("result", {}).get("description", ""),
        )
        # Notify merchant — failure
        try:
            import asyncio  # noqa: PLC0415
            from core.notifications import send_email, email_payment_failed  # noqa: PLC0415
            from core.wa_notify import _send, _normalize_phone  # noqa: PLC0415
            if email_addr:
                asyncio.ensure_future(send_email(
                    to=email_addr,
                    subject="❌ فشل الدفع — يرجى تجديد اشتراكك",
                    html=email_payment_failed(store_name, "HyperPay", 0),
                ))
            if phone:
                asyncio.ensure_future(_send(
                    _normalize_phone(phone),
                    f"🔴 نحلة AI\nفشل الدفع لـ {store_name}.\nيرجى تحديث طريقة الدفع:\nhttps://app.nahlah.ai/billing",
                ))
        except Exception as exc:
            logger.warning("[HyperPay] Failure notification error: %s", exc)

    return {"received": True}
