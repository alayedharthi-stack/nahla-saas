"""
services/offer_attribution_service.py
─────────────────────────────────────
Closes the loop between an issued offer and the order that redeemed it.

When an order is paid (Moyasar webhook → `order_paid`) or arrives via the
store-platform webhook (`order_created`), this service walks:

    order.coupon_code  →  Coupon row  →  extra_metadata.decision_id
                                              ↓
                                  OfferDecisionLedger.attributed = True

so the analytics surface (Phase 5) can answer "which decisions converted
into revenue, and at what discount level".

Design notes
────────────
• Idempotent: re-attributing the same (decision, order) pair is a no-op.
• Never raises into the caller — attribution failures must NEVER block a
  payment webhook from acknowledging.
• Stays out of the decision path: this module only **reads** the decision
  ledger and writes attribution columns. The decision was already made.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from models import Coupon, OfferDecisionLedger, Order


logger = logging.getLogger(__name__)


def attribute_order_to_decision(
    db: Session,
    *,
    tenant_id: int,
    order_id: int,
    payload: Optional[Dict[str, Any]] = None,
) -> Optional[OfferDecisionLedger]:
    """
    Attribute an order back to its originating decision (if any).

    Returns the updated `OfferDecisionLedger` row when attribution succeeds
    or already existed; returns None when the order has no resolvable
    coupon code or the coupon carries no `decision_id`.

    Safe to call from inside any webhook — wraps every step in a defensive
    try/except.
    """
    try:
        order = (
            db.query(Order)
            .filter(Order.id == order_id, Order.tenant_id == tenant_id)
            .first()
        )
        if order is None:
            return None

        coupon_code = _coupon_code_from_order(order, payload)
        if not coupon_code:
            return None

        coupon = (
            db.query(Coupon)
            .filter(Coupon.tenant_id == tenant_id, Coupon.code == coupon_code)
            .first()
        )
        if coupon is None:
            return None

        meta = coupon.extra_metadata or {}
        decision_id = (meta.get("decision_id") or "").strip()
        if not decision_id:
            return None

        ledger = (
            db.query(OfferDecisionLedger)
            .filter(
                OfferDecisionLedger.tenant_id == tenant_id,
                OfferDecisionLedger.decision_id == decision_id,
            )
            .first()
        )
        if ledger is None:
            return None

        # Idempotent: if we already attributed this order to this decision
        # we return the existing row unchanged.
        if ledger.attributed and ledger.order_id == order.id:
            return ledger

        revenue = _resolve_revenue(order, payload)
        ledger.attributed     = True
        ledger.order_id       = order.id
        ledger.revenue_amount = revenue
        ledger.redeemed_at    = datetime.now(timezone.utc).replace(tzinfo=None)
        db.flush()
        return ledger
    except Exception as exc:
        logger.exception(
            "[OfferAttributionService] failed tenant=%s order=%s: %s",
            tenant_id, order_id, exc,
        )
        return None


# ── Helpers ──────────────────────────────────────────────────────────────

def _coupon_code_from_order(order: Order, payload: Optional[Dict[str, Any]]) -> Optional[str]:
    """
    Find the coupon code on the order.

    We look at three places, in order:
      1. payload['coupon_code'] / payload['promo_code'] — explicit hint
         passed by the webhook caller (takes precedence so a webhook can
         override a stale order row);
      2. order.extra_metadata.coupon_code / promo_code / discount_code;
      3. order.customer_info.coupon_code (Salla / Zid sometimes nest it).
    """
    if isinstance(payload, dict):
        for key in ("coupon_code", "promo_code", "discount_code"):
            value = payload.get(key)
            if value:
                return str(value).strip().upper() or None

    meta = order.extra_metadata or {}
    if isinstance(meta, dict):
        for key in ("coupon_code", "promo_code", "discount_code"):
            value = meta.get(key)
            if value:
                return str(value).strip().upper() or None

    customer_info = order.customer_info or {}
    if isinstance(customer_info, dict):
        for key in ("coupon_code", "promo_code", "discount_code"):
            value = customer_info.get(key)
            if value:
                return str(value).strip().upper() or None
    return None


def _resolve_revenue(order: Order, payload: Optional[Dict[str, Any]]) -> Optional[Decimal]:
    """
    Realised revenue = order.total (preferred) → payload['amount'] →
    order.extra_metadata.total. Returns None when no plausible value.
    """
    candidates = []
    if order.total is not None:
        candidates.append(order.total)
    if isinstance(payload, dict):
        candidates.append(payload.get("amount"))
        candidates.append(payload.get("total"))
    meta = order.extra_metadata or {}
    if isinstance(meta, dict):
        candidates.append(meta.get("total"))
        candidates.append(meta.get("amount"))

    for raw in candidates:
        if raw in (None, "", 0):
            continue
        try:
            return Decimal(str(raw))
        except Exception:
            continue
    return None
