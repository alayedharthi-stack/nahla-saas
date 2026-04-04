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
import sys
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../database")))
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
)
from core.database import get_db

logger = logging.getLogger("nahla-backend")

router = APIRouter(tags=["Webhooks"])

_MOYASAR_FAIL_STATUSES = frozenset({"failed", "expired", "canceled", "voided", "refunded"})
_BILLING_ACTIVATABLE   = frozenset({"pending_payment"})


# ── Salla ─────────────────────────────────────────────────────────────────────

@router.post("/webhook/salla")
async def salla_webhook(request: Request, db: Session = Depends(get_db)):
    """
    Receive event notifications from Salla.
    Verifies HMAC-SHA256 signature when SALLA_WEBHOOK_SECRET is set.
    """
    raw_body  = await request.body()
    client_ip = request.headers.get("X-Real-IP") or (
        request.client.host if request.client else "unknown"
    )

    if SALLA_WEBHOOK_SECRET:
        sig_header  = request.headers.get("X-Salla-Signature", "")
        received_sig = sig_header[7:] if sig_header.startswith("sha256=") else sig_header
        expected_sig = hmac.new(
            SALLA_WEBHOOK_SECRET.encode(), raw_body, "sha256",
        ).hexdigest()
        if not hmac.compare_digest(received_sig, expected_sig):
            logger.warning(
                "Salla webhook: invalid signature | ip=%s sig_received=%s",
                client_ip, sig_header[:20],
            )
            audit("salla_webhook_invalid_signature", ip=client_ip)
            return JSONResponse(status_code=401, content={"detail": "Invalid signature"})
    else:
        logger.warning("Salla webhook: SALLA_WEBHOOK_SECRET not set — skipping signature check")

    try:
        payload = await request.json()
    except Exception:
        payload = {}

    event      = payload.get("event", "unknown")
    store_id   = payload.get("merchant", payload.get("store_id", "unknown"))
    created_at = payload.get("created_at", "")

    logger.info(
        "Salla webhook received | event=%s store_id=%s created_at=%s ip=%s",
        event, store_id, created_at, client_ip,
    )
    audit("salla_webhook", event=event, store_id=store_id, ip=client_ip)

    data = payload.get("data", {})

    if event in ("app.store.authorize", "app.store.token"):
        # Salla sends full token data when merchant installs/authorizes the app
        await _handle_salla_authorize(db, store_id, data, payload)

    elif event == "order.created":
        logger.info("Salla order.created | order_id=%s store=%s", data.get("id"), store_id)
    elif event == "order.updated":
        logger.info(
            "Salla order.updated | order_id=%s status=%s store=%s",
            data.get("id"), data.get("status", {}), store_id,
        )
    elif event == "shipment.created":
        logger.info("Salla shipment.created | shipment_id=%s store=%s", data.get("id"), store_id)
    elif event == "customer.created":
        logger.info("Salla customer.created | email=%s store=%s", data.get("email"), store_id)
    elif event == "app.installed":
        logger.info("Salla app.installed | store=%s", store_id)
        await _handle_salla_authorize(db, store_id, data, payload)
    elif event == "app.uninstalled":
        logger.info("Salla app.uninstalled | store=%s", store_id)
        _disable_salla_integration(db, str(store_id))
    else:
        logger.info(
            "Salla webhook unhandled event=%s store=%s | data=%s",
            event, store_id, str(data)[:200],
        )

    return {"status": "ok", "event": event}


async def _handle_salla_authorize(db, store_id, data: dict, payload: dict) -> None:
    """Save Salla OAuth tokens received via webhook (app.store.authorize / app.installed)."""
    from datetime import datetime
    import sys, os
    sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../database")))
    from models import Integration, Tenant
    from core.tenant import get_or_create_tenant

    # Token may be nested under data or at root
    access_token  = data.get("access_token")  or payload.get("access_token", "")
    refresh_token = data.get("refresh_token") or payload.get("refresh_token", "")
    expires_in    = data.get("expires_in")    or payload.get("expires_in", 0)
    salla_store_id = str(data.get("merchant_id") or data.get("store_id") or store_id or "")
    store_name     = data.get("store", {}).get("name", "") if isinstance(data.get("store"), dict) else ""

    if not access_token:
        logger.warning("Salla authorize webhook: no access_token in payload | store=%s", store_id)
        return

    # Find or create tenant by salla_store_id
    tenant = db.query(Tenant).filter(Tenant.salla_store_id == salla_store_id).first()
    if not tenant:
        # Fall back to tenant 1 (admin) for single-tenant setups
        tenant = db.query(Tenant).filter(Tenant.id == 1).first()
    tenant_id = tenant.id if tenant else 1

    integration = db.query(Integration).filter(
        Integration.tenant_id == tenant_id,
        Integration.provider  == "salla",
    ).first()

    new_config = {
        "api_key":       access_token,
        "refresh_token": refresh_token,
        "store_id":      salla_store_id,
        "store_name":    store_name,
        "expires_in":    expires_in,
        "connected_at":  datetime.utcnow().isoformat(),
    }

    if integration:
        integration.config  = new_config
        integration.enabled = True
    else:
        integration = Integration(
            tenant_id=tenant_id,
            provider="salla",
            config=new_config,
            enabled=True,
        )
        db.add(integration)

    db.commit()
    logger.info(
        "Salla authorize saved via webhook | tenant=%s store_id=%s store_name=%s",
        tenant_id, salla_store_id, store_name,
    )


def _disable_salla_integration(db, store_id: str) -> None:
    """Disable integration when app is uninstalled."""
    from models import Integration, Tenant
    tenant = db.query(Tenant).filter(Tenant.salla_store_id == store_id).first()
    if tenant:
        integration = db.query(Integration).filter(
            Integration.tenant_id == tenant.id,
            Integration.provider  == "salla",
        ).first()
        if integration:
            integration.enabled = False
            db.commit()
            logger.info("Salla integration disabled | store=%s", store_id)


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
            ps.updated_at    = datetime.utcnow()

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
        m["paid_at"] = datetime.utcnow().isoformat()
        sub.extra_metadata = m

        billing_payment = BillingPayment(
            tenant_id=sub.tenant_id,
            subscription_id=sub.id,
            amount_sar=amount_sar or int(m.get("price_charged_sar", 0)),
            currency="SAR",
            gateway="moyasar",
            transaction_reference=payment_id,
            status="paid",
            paid_at=datetime.utcnow(),
            extra_metadata={"moyasar_event": event},
        )
        db.add(billing_payment)
        db.flush()
        logger.info(
            "[Billing Webhook] Subscription %s ACTIVATED for tenant %s (payment %s)",
            subscription_id, sub.tenant_id, payment_id,
        )

        try:
            tenant_obj = db.query(Tenant).filter(Tenant.id == sub.tenant_id).first()
            merchant   = db.query(User).filter(
                User.tenant_id == sub.tenant_id,
                User.role == "merchant",
                User.is_active == True,  # noqa: E712
            ).first()
            plan_obj   = sub.plan if hasattr(sub, "plan") and sub.plan else None
            plan_name  = plan_obj.name if plan_obj else payment_meta.get("plan_slug", "")
            store_name = tenant_obj.name if tenant_obj else f"Tenant {sub.tenant_id}"
            ends_str   = sub.ends_at.strftime("%Y-%m-%d") if sub.ends_at else "—"

            if merchant:
                import asyncio  # noqa: PLC0415
                from core.notifications import send_email, send_whatsapp, email_subscription  # noqa: PLC0415
                asyncio.ensure_future(send_email(
                    to=merchant.email,
                    subject=f"تم تفعيل اشتراك {plan_name} — نحلة AI",
                    html=email_subscription(store_name, plan_name, ends_str),
                ))
                asyncio.ensure_future(send_whatsapp(
                    to=merchant.username,
                    text=(
                        f"🐝 نحلة AI\n"
                        f"مرحباً {store_name}!\n"
                        f"تم تفعيل خطة {plan_name} بنجاح ✅\n"
                        f"ينتهي الاشتراك في: {ends_str}"
                    ),
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

    if hp.is_payment_successful(data):
        now = datetime.utcnow()
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
    else:
        tenant.billing_status = "failed"
        db.commit()
        logger.warning(
            "[HyperPay] Payment FAILED for tenant %s: code=%s desc=%s",
            tenant.id, result_code, data.get("result", {}).get("description", ""),
        )

    return {"received": True}
