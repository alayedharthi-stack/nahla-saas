"""
routers/webhooks.py
────────────────────
Unified webhook handler for all payment and platform webhooks.

Routes
  POST /webhook/salla
  POST /payments/webhook/moyasar
  POST /billing/webhook/moyasar/subscription
  POST /webhook/stripe
  POST /webhook/hyperpay
"""
from __future__ import annotations

import hmac
import json as _json
import logging
import os
from datetime import datetime, timedelta, timezone

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
    SALLA_WEBHOOK_SECRET,
    SALLA_WEBHOOK_ENFORCE_SIGNATURE,
    SALLA_WEBHOOK_ALLOW_MISSING_SIGNATURE,
)
from core.database import get_db
from core.obs import EVENTS, log_event
from core.webhook_events import (
    STATUS_FAILED,
    STATUS_RECEIVED,
    persist_event,
)

logger = logging.getLogger("nahla-backend")

router = APIRouter(tags=["Webhooks"])

_MOYASAR_FAIL_STATUSES = frozenset({"failed", "expired", "canceled", "voided", "refunded"})
_BILLING_ACTIVATABLE   = frozenset({"pending_payment"})


def _verify_salla_signature(raw_body: bytes, request_headers) -> tuple[bool, str]:
    """Verify Salla webhook HMAC-SHA256 signature.

    Returns ``(should_accept, log_reason)``.

    Behaviour matrix (controlled by env vars):
      ENFORCE=false  → always accept, log only
      ENFORCE=true + valid sig   → accept
      ENFORCE=true + missing sig → accept/reject per ALLOW_MISSING_SIGNATURE
      ENFORCE=true + invalid sig → reject
    """
    sig_header = request_headers.get("x-salla-signature", "")
    has_secret = bool(SALLA_WEBHOOK_SECRET)

    if not has_secret:
        return True, "SIG_SKIP: no SALLA_WEBHOOK_SECRET configured"

    if not sig_header:
        if not SALLA_WEBHOOK_ENFORCE_SIGNATURE:
            return True, "SIG_SKIP: signature missing, enforcement OFF"
        if SALLA_WEBHOOK_ALLOW_MISSING_SIGNATURE:
            return True, "SIG_WARN: signature missing, allowed by ALLOW_MISSING_SIGNATURE"
        return False, "SIG_REJECT: signature missing, enforcement ON + ALLOW_MISSING=false"

    expected = hmac.new(SALLA_WEBHOOK_SECRET.encode(), raw_body, "sha256").hexdigest()
    sig_ok = hmac.compare_digest(expected, sig_header)

    if sig_ok:
        return True, "SIG_PASS: valid signature"

    if not SALLA_WEBHOOK_ENFORCE_SIGNATURE:
        return True, "SIG_WARN: invalid signature, enforcement OFF — accepted anyway"

    return False, "SIG_REJECT: invalid signature"


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

    log_event(
        EVENTS.WEBHOOK_RECEIVED,
        provider="salla",
        ip=client_ip,
        body_len=len(raw_body),
        content_type=request.headers.get("content-type", ""),
        user_agent=(request.headers.get("user-agent", "") or "")[:80],
    )

    # ── 1. Signature verification ────────────────────────────────────────────
    sig_accepted, sig_reason = _verify_salla_signature(raw_body, request.headers)
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


def _resolve_tenant_from_store(db, store_id) -> int | None:
    """Look up the Nahla tenant_id that owns a given Salla store_id."""
    from models import Integration  # noqa: PLC0415
    sid = str(store_id)

    active = db.query(Integration).filter(
        Integration.provider == "salla",
        Integration.enabled == True,  # noqa: E712
        Integration.external_store_id == sid,
    ).first()
    if active:
        logger.info("[Salla WH] resolved store_id=%s → tenant=%s (integration id=%s)", sid, active.tenant_id, active.id)
        return active.tenant_id

    disabled = db.query(Integration).filter(
        Integration.provider == "salla",
        Integration.external_store_id == sid,
    ).first()
    if disabled:
        logger.warning(
            "[Salla WH] store_id=%s found BUT disabled | tenant=%s enabled=%s — webhook ignored",
            sid, disabled.tenant_id, disabled.enabled,
        )
    else:
        logger.warning("[Salla WH] store_id=%s NOT FOUND in any integration — webhook dropped", sid)
    return None


async def _handle_salla_authorize(db, store_id, data: dict, payload: dict) -> None:
    """Save Salla OAuth tokens received via webhook (app.store.authorize / app.installed)."""
    from models import Integration, Tenant, User  # noqa: PLC0415
    from core.auth import hash_password, create_token  # noqa: PLC0415
    from core.tenant import get_or_create_tenant  # noqa: PLC0415
    import secrets as _sec  # noqa: PLC0415

    # Token may be nested under data or at root level
    access_token  = (data.get("access_token")  or payload.get("access_token",  "")).strip()
    refresh_token = (data.get("refresh_token") or payload.get("refresh_token", "")).strip()
    expires_in    = data.get("expires_in")    or payload.get("expires_in", 0)
    salla_store_id = str(data.get("merchant_id") or data.get("store_id") or store_id or "")
    store_name     = (
        data.get("store", {}).get("name", "")
        if isinstance(data.get("store"), dict) else
        data.get("name", "")
    ) or f"متجر سلة {salla_store_id}"

    logger.info(
        "[Salla WH] _handle_salla_authorize | store_id=%s has_access_token=%s "
        "has_refresh_token=%s store_name=%s",
        salla_store_id, bool(access_token), bool(refresh_token), store_name,
    )

    # ── Find existing integration by salla store_id in config ─────────────────
    # Search ALL integrations (enabled AND disabled) so we re-activate existing
    # tenants instead of creating duplicates — critical for Easy-mode reinstalls.
    existing_integration = None
    try:
        existing_integration = db.query(Integration).filter(
            Integration.provider == "salla",
            Integration.external_store_id == salla_store_id,
        ).first()
    except Exception as _e:
        logger.warning("[Salla Webhook] Integration lookup error: %s", _e)

    if existing_integration:
        from services.salla_guard import claim_store_for_tenant  # noqa: PLC0415

        was_disabled = not existing_integration.enabled
        tenant_id = existing_integration.tenant_id
        new_cfg = dict(existing_integration.config or {})
        new_cfg.update({
            "api_key":       access_token or new_cfg.get("api_key", ""),
            "refresh_token": refresh_token or new_cfg.get("refresh_token", ""),
            "expires_in":    expires_in,
            "store_name":    store_name,
            "connected_at":  datetime.now(timezone.utc).isoformat(),
            "app_type":      "easy",
        })
        new_cfg.pop("soft_disabled", None)
        new_cfg.pop("uninstalled_at", None)
        new_cfg.pop("needs_reauth", None)
        new_cfg.pop("needs_reauth_at", None)
        new_cfg.pop("needs_reauth_reason", None)
        claim_store_for_tenant(
            db, store_id=salla_store_id, tenant_id=tenant_id, new_config=new_cfg,
        )
        db.commit()
        if was_disabled:
            logger.info(
                "[Salla Webhook] RE-ACTIVATED disabled integration via Easy webhook | "
                "tenant=%s store=%s", tenant_id, salla_store_id,
            )
        else:
            logger.info(
                "[Salla Webhook] REFRESHED tokens for active integration | "
                "tenant=%s store=%s", tenant_id, salla_store_id,
            )
        return

    # ── No existing integration: auto-create a new Nahla merchant account ─────
    if not access_token:
        # app.installed without a token — just log and return (token comes via OAuth later)
        logger.info(
            "[Salla Webhook] app.installed with no token — merchant will link via OAuth | store=%s",
            salla_store_id,
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

        integration = Integration(
            tenant_id=tenant_id,
            provider="salla",
            external_store_id=salla_store_id,
            config={
                "api_key":       access_token,
                "refresh_token": refresh_token,
                "store_id":      salla_store_id,
                "store_name":    store_name,
                "expires_in":    expires_in,
                "connected_at":  datetime.now(timezone.utc).isoformat(),
                "app_type":      "easy",
            },
            enabled=True,
        )
        db.add(integration)
        db.commit()
        logger.info(
            "[Salla Webhook] Auto-created merchant | tenant=%s email=%s store=%s",
            tenant_id, salla_email, salla_store_id,
        )
    except Exception as exc:
        logger.exception("[Salla Webhook] Auto-create failed: %s", exc)
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
                            _phone = _ci.get("mobile") or _ci.get("phone")
                            _cust = None
                            if _phone:
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
    Activates the BillingSubscription and records a BillingPayment on success.
    Hardened: idempotency, signature verification, full status handling, race protection.
    """
    body_bytes = await request.body()
    signature  = request.headers.get("x-moyasar-signature", "")

    try:
        event = _json.loads(body_bytes)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    payment_id      = event.get("id", "")
    status          = event.get("status", "")
    amount_h        = int(event.get("amount", 0))
    amount_sar      = amount_h // 100
    payment_meta    = event.get("metadata") or {}
    subscription_id = payment_meta.get("subscription_id")
    tenant_id_raw   = payment_meta.get("tenant_id")

    logger.info(
        "[Billing Webhook] event id=%s status=%s sub=%s tenant=%s",
        payment_id, status, subscription_id, tenant_id_raw,
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
    webhook_secret = cfg.get("webhook_secret", "")
    if webhook_secret:
        sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
        from payment_gateways.moyasar import MoyasarClient  # noqa: PLC0415
        client = MoyasarClient(secret_key=cfg.get("secret_key", ""))
        if not client.verify_webhook_signature(body_bytes, signature, webhook_secret):
            logger.warning(
                "[Billing Webhook] Invalid signature for sub=%s tenant=%s",
                subscription_id, sub.tenant_id,
            )
            raise HTTPException(status_code=401, detail="Invalid webhook signature")

    if status == "paid":
        if payment_id:
            existing = db.query(BillingPayment).filter(
                BillingPayment.transaction_reference == payment_id,
                BillingPayment.gateway == "moyasar",
            ).first()
            if existing:
                logger.info(
                    "[Billing Webhook] Duplicate delivery payment_id=%s — idempotent, ignoring",
                    payment_id,
                )
                return {"received": True, "idempotent": True}

        if sub.status == "active":
            logger.info(
                "[Billing Webhook] Sub %s already active — duplicate webhook ignored", subscription_id,
            )
            return {"received": True, "already_active": True}

        if sub.status not in _BILLING_ACTIVATABLE:
            logger.warning(
                "[Billing Webhook] Sub %s in unexpected status %r — skipping activation",
                subscription_id, sub.status,
            )
            return {"received": True, "skipped": True}

        sub.status = "active"
        m = dict(sub.extra_metadata or {})
        m["moyasar_payment_id"] = payment_id
        m["paid_at"] = datetime.now(timezone.utc).isoformat()
        sub.extra_metadata = m

        billing_payment = BillingPayment(
            tenant_id=sub.tenant_id,
            subscription_id=sub.id,
            amount_sar=amount_sar or int(m.get("price_charged_sar", 0)),
            currency="SAR",
            gateway="moyasar",
            transaction_reference=payment_id,
            status="paid",
            paid_at=datetime.now(timezone.utc),
            extra_metadata={"moyasar_event": event},
        )
        db.add(billing_payment)
        db.flush()
        logger.info(
            "[Billing Webhook] Subscription %s ACTIVATED for tenant %s (payment %s)",
            subscription_id, sub.tenant_id, payment_id,
        )

        try:
            import asyncio  # noqa: PLC0415
            from core.notifications import send_email, email_subscription, email_invoice  # noqa: PLC0415
            from core.wa_notify import notify_subscription_confirmed, notify_payment_invoice  # noqa: PLC0415

            tenant_obj = db.query(Tenant).filter(Tenant.id == sub.tenant_id).first()
            merchant   = db.query(User).filter(
                User.tenant_id == sub.tenant_id,
                User.role == "merchant",
                User.is_active == True,  # noqa: E712
            ).first()
            plan_obj   = db.query(BillingPlan).filter(BillingPlan.id == sub.plan_id).first() if sub.plan_id else None
            plan_name  = (plan_obj.name if plan_obj else None) or payment_meta.get("plan_slug", "الخطة")
            store_name = tenant_obj.name if tenant_obj else f"Tenant {sub.tenant_id}"
            ends_str   = sub.ends_at.strftime("%Y-%m-%d") if sub.ends_at else "—"
            amount_sar = float(payment_meta.get("amount", 0)) / 100  # Moyasar stores in halalas
            invoice_id = str(payment_id)[:12]
            pay_date   = datetime.now(timezone.utc).strftime("%Y-%m-%d")

            if merchant and merchant.email:
                asyncio.ensure_future(send_email(
                    to=merchant.email,
                    subject=f"✅ تم تفعيل اشتراك {plan_name} — نحلة AI",
                    html=email_subscription(store_name, plan_name, ends_str),
                ))
                asyncio.ensure_future(send_email(
                    to=merchant.email,
                    subject=f"🧾 فاتورة دفع #{invoice_id} — نحلة AI",
                    html=email_invoice(store_name, plan_name, amount_sar, invoice_id, pay_date),
                ))

            phone = getattr(merchant, "username", "") if merchant else ""
            if phone:
                asyncio.ensure_future(notify_subscription_confirmed(
                    phone, store_name, plan_name, amount_sar, ends_str,
                ))
                asyncio.ensure_future(notify_payment_invoice(
                    phone, store_name, plan_name, amount_sar, invoice_id, pay_date,
                ))
        except Exception as notify_exc:
            logger.warning("[Billing Webhook] Notification error: %s", notify_exc)

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
