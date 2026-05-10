"""services/billing_activation.py
─────────────────────────────────
Single source of truth for "subscription becomes active after payment".

Problem this module solves
──────────────────────────
Before this file existed, activation logic only lived inside the Moyasar
webhook handler (``routers/webhooks.py:billing_webhook_moyasar``). That
worked in theory, but in practice no webhook is delivered for Moyasar
hosted-page invoices because the ``callback_url`` parameter on an
invoice is the **browser redirect URL** after the customer pays — *not*
a server-to-server webhook. Result: every Moyasar invoice payment
ended with the merchant looking at the polling spinner forever, while
the subscription stayed in ``pending_payment`` and the merchant kept
seeing the trial / unpaid UI even though Moyasar held their money.

The fix is to make ``GET /billing/payment-result`` (the endpoint the
``BillingResult.tsx`` page polls) reconcile *live* against the Moyasar
``GET /v1/invoices/{id}`` API, plus offer the same logic via an admin
recovery endpoint.

Both paths funnel into ``activate_subscription_from_moyasar_invoice``
below so we cannot drift between them.

Idempotency
───────────
Every entry point is safe to retry:

  * If ``sub.status`` is already ``"active"`` we no-op, return the
    existing ``BillingPayment`` row, and emit no notifications.
  * If a ``BillingPayment`` row already exists for the same
    ``transaction_reference`` (Moyasar payment id) we return it and skip.
  * Notifications run inside best-effort try/excepts so a failed
    WhatsApp / email send never blocks activation.

This file deliberately does NOT import the FastAPI router or webhook
router — it speaks pure DB + Moyasar API so it can be called from
schedulers, admin shells, tests, and the result-page reconcile path
without circular imports.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from sqlalchemy.orm import Session

from core.billing import get_moyasar_settings
from core.config import MOYASAR_SECRET_KEY
from models import BillingPayment, BillingPlan, BillingSubscription, Tenant, User

logger = logging.getLogger("nahla.billing.activation")


# ── Status constants ────────────────────────────────────────────────────
ACTIVATABLE_STATUSES = frozenset({"pending_payment"})
ACTIVE_STATUS = "active"


def _moyasar_client(db: Session, tenant_id: int):
    """Return a configured ``MoyasarClient`` for the tenant.

    Falls back to platform-level secret. Returns ``None`` when neither
    is configured — callers must treat this as "Moyasar disabled".
    """
    cfg = get_moyasar_settings(db, tenant_id) or {}
    secret_key = cfg.get("secret_key") or MOYASAR_SECRET_KEY
    if not secret_key:
        return None

    from payment_gateways.moyasar import MoyasarClient  # noqa: PLC0415
    return MoyasarClient(
        secret_key=secret_key,
        publishable_key=cfg.get("publishable_key", ""),
    )


def normalize_moyasar_event(event: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
    """Detect whether the webhook body is invoice-style or payment-style.

    Moyasar delivers two shapes depending on which event you subscribed
    to in the dashboard:

      • Payment event (flat):
        ``{ "id": "<payment>", "status": "paid", "metadata": {...}, ... }``

      • Invoice event (envelope):
        ``{ "id": "<event>", "type": "invoice.paid",
            "data": { "id": "<invoice>", "status": "paid",
                      "metadata": {...}, "payments": [...] } }``

    Returns a tuple ``(payload, event_type)`` where ``payload`` is the
    inner object that has ``status`` / ``metadata`` and ``event_type`` is
    ``"invoice"`` or ``"payment"`` for downstream branching.
    """
    inner = event.get("data") if isinstance(event, dict) else None
    if isinstance(inner, dict) and ("status" in inner or "metadata" in inner):
        return inner, "invoice"
    return event or {}, "payment"


def extract_payment_id_from_invoice(invoice_data: Dict[str, Any]) -> str:
    """Pull the first paid payment id out of an invoice payload.

    Moyasar embeds the payment(s) under ``data.payments``. We pick the
    first one with ``status == "paid"`` since that's the canonical
    transaction reference we want to record on ``BillingPayment``.
    Falls back to the invoice id if no payment list is present (so we
    still have *something* unique to dedupe against).
    """
    payments = invoice_data.get("payments") or []
    if isinstance(payments, list):
        for p in payments:
            if isinstance(p, dict) and p.get("status") == "paid" and p.get("id"):
                return str(p["id"])
    # last-resort: use the invoice id as a stable reference
    return str(invoice_data.get("id") or "")


def activate_subscription_from_moyasar_invoice(
    db: Session,
    sub: BillingSubscription,
    *,
    invoice_data: Dict[str, Any],
    payment_id: Optional[str] = None,
    source: str = "reconcile",
) -> Tuple[bool, str]:
    """Idempotently flip a subscription to ``active`` based on a Moyasar
    invoice payload.

    Returns ``(activated, reason)`` where ``activated`` is True when the
    sub is now active *because of this call*, and False otherwise. The
    ``reason`` string is meant for log lines / admin responses:

      • ``"already_active"`` — sub was already active, no-op.
      • ``"invoice_not_paid"`` — Moyasar reports something other than ``paid``.
      • ``"unexpected_status"`` — sub is in a status we refuse to
        auto-flip (e.g. ``cancelled``); admin must intervene manually.
      • ``"duplicate_payment"`` — we already recorded a BillingPayment
        for this transaction_reference.
      • ``"activated"`` — newly flipped to active.

    All paths are safe to retry; the function never raises on a benign
    skip. It DOES raise if ``invoice_data`` is malformed, since that
    indicates a programming bug rather than a runtime condition.
    """
    if not isinstance(invoice_data, dict):
        raise ValueError("invoice_data must be a dict")

    invoice_status = (invoice_data.get("status") or "").lower()
    if invoice_status != "paid":
        return False, "invoice_not_paid"

    if sub.status == ACTIVE_STATUS:
        return False, "already_active"

    if sub.status not in ACTIVATABLE_STATUSES:
        # We refuse to silently revive cancelled / failed subscriptions.
        # That should be an explicit admin action, not a side-effect.
        logger.warning(
            "[activation] refusing to activate sub=%s status=%r (not in %s)",
            sub.id, sub.status, sorted(ACTIVATABLE_STATUSES),
        )
        return False, "unexpected_status"

    txn_ref = payment_id or extract_payment_id_from_invoice(invoice_data)
    if not txn_ref:
        raise ValueError("Could not derive a transaction reference from invoice")

    # ── Idempotency: short-circuit on duplicate BillingPayment ─────────
    existing = (
        db.query(BillingPayment)
        .filter(
            BillingPayment.transaction_reference == txn_ref,
            BillingPayment.gateway == "moyasar",
        )
        .first()
    )
    if existing:
        # If the duplicate row exists but the sub is still pending, that
        # is a stuck state — fall through to the activation block so we
        # *do* flip the status this time. Mark the source for the log.
        if sub.status == ACTIVE_STATUS:
            return False, "duplicate_payment"

    # ── Activation ────────────────────────────────────────────────────
    sub.status = ACTIVE_STATUS
    meta = dict(sub.extra_metadata or {})
    meta["moyasar_payment_id"] = txn_ref
    meta["moyasar_invoice_id"] = invoice_data.get("id") or meta.get("moyasar_invoice_id")
    meta["paid_at"] = datetime.now(timezone.utc).isoformat()
    meta["activation_source"] = source
    sub.extra_metadata = meta

    invoice_amount_h = int(invoice_data.get("amount") or 0)
    final_amount_sar = (invoice_amount_h // 100) or int(meta.get("price_charged_sar") or 0)

    if not existing:
        billing_payment = BillingPayment(
            tenant_id=sub.tenant_id,
            subscription_id=sub.id,
            amount_sar=final_amount_sar,
            currency="SAR",
            gateway="moyasar",
            transaction_reference=txn_ref,
            status="paid",
            paid_at=datetime.now(timezone.utc),
            extra_metadata={
                "moyasar_invoice": {
                    "id":     invoice_data.get("id"),
                    "amount": invoice_data.get("amount"),
                    "status": invoice_data.get("status"),
                    # Keep metadata for forensics — never store raw card data.
                    "metadata": invoice_data.get("metadata"),
                },
                "activation_source": source,
            },
        )
        db.add(billing_payment)
        db.flush()
        billing_payment_id = billing_payment.id
    else:
        billing_payment_id = existing.id

    db.commit()

    logger.info(
        "[NAHLA PAYMENT ACTIVATED] tenant=%s sub=%s payment_id=%s "
        "amount=%s SAR billing_payment_id=%s source=%s",
        sub.tenant_id, sub.id, txn_ref, final_amount_sar,
        billing_payment_id, source,
    )

    # ── Best-effort merchant receipt notifications ─────────────────────
    # Notification failures must NEVER block the activation rollback.
    try:
        _send_activation_receipts(
            db,
            sub=sub,
            payment_id=txn_ref,
            amount_sar=final_amount_sar,
        )
    except Exception as exc:
        logger.warning(
            "[activation] receipt-send failed tenant=%s sub=%s: %s",
            sub.tenant_id, sub.id, exc,
        )

    return True, "activated"


def _send_activation_receipts(
    db: Session,
    *,
    sub: BillingSubscription,
    payment_id: str,
    amount_sar: int,
) -> None:
    """Send the merchant receipt over WhatsApp + email (best-effort).

    Pulled out so the main activation function reads cleanly. Any
    exception here is caught by the caller's try/except.
    """
    import asyncio  # noqa: PLC0415

    from core.notifications import send_email  # noqa: PLC0415
    from core.wa_notify import notify_payment_invoice  # noqa: PLC0415
    from services.billing_formatter import (  # noqa: PLC0415
        build_nahla_email_invoice_html,
        resolve_billing_context,
    )

    paid_now = datetime.now(timezone.utc)
    ctx = resolve_billing_context(
        db,
        tenant_id=sub.tenant_id,
        sub=sub,
        plan_obj=None,
        payment_id=payment_id,
        payment_amount_sar=amount_sar,
        paid_at=paid_now,
    )

    email_addr = ctx.get("merchant_email")
    phone = ctx.get("merchant_phone")

    if email_addr:
        asyncio.ensure_future(send_email(
            to=email_addr,
            subject=(
                f"🧾 فاتورة اشتراك نحلة AI — "
                f"{ctx['plan_name']} #{ctx['invoice_id']}"
            ),
            html=build_nahla_email_invoice_html(ctx),
        ))

    if phone:
        # We deliberately fire-and-forget here; the surrounding
        # transaction has already been committed.
        asyncio.ensure_future(notify_payment_invoice(
            phone,
            ctx["store_name"],
            ctx["plan_name"],
            ctx["amount_sar"],
            ctx["invoice_id"],
            paid_now,
            merchant_name=ctx["merchant_name"],
            billing_period=ctx["billing_period"],
            tenant_id=ctx["tenant_id"],
            ends_at=ctx["ends_at"],
        ))


async def reconcile_subscription_from_moyasar(
    db: Session,
    sub: BillingSubscription,
    *,
    source: str = "result_page_poll",
) -> Tuple[bool, str]:
    """Live-reconcile a single subscription against Moyasar's API.

    This is the path used by ``GET /billing/payment-result`` — instead
    of waiting for a webhook that may never fire, we just *ask* Moyasar
    whether the invoice was paid and act on the answer. Idempotent and
    safe to call from a polling loop.

    Returns the same ``(activated, reason)`` tuple as
    ``activate_subscription_from_moyasar_invoice``. Adds these reasons:

      • ``"no_invoice_id"`` — sub has no Moyasar invoice attached
        (e.g. demo / HyperPay / pre-checkout state).
      • ``"moyasar_unconfigured"`` — neither tenant nor platform has a
        Moyasar secret key. Cannot reconcile.
      • ``"moyasar_api_error"`` — network / 4xx / 5xx from Moyasar; we
        log and return False (caller can retry on next poll).
    """
    if sub.status == ACTIVE_STATUS:
        return False, "already_active"

    meta = sub.extra_metadata or {}
    invoice_id = meta.get("moyasar_invoice_id")
    if not invoice_id:
        return False, "no_invoice_id"

    client = _moyasar_client(db, sub.tenant_id)
    if client is None:
        return False, "moyasar_unconfigured"

    try:
        invoice_data = await client.get_invoice(invoice_id)
    except Exception as exc:
        logger.warning(
            "[reconcile] Moyasar API error tenant=%s sub=%s invoice=%s: %s",
            sub.tenant_id, sub.id, invoice_id, exc,
        )
        return False, "moyasar_api_error"

    return activate_subscription_from_moyasar_invoice(
        db, sub, invoice_data=invoice_data, source=source,
    )
