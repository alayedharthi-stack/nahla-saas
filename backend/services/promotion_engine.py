"""
services/promotion_engine.py
────────────────────────────
Promotion evaluation + materialisation.

A `Promotion` row stores the *terms* of a discount (type, value, conditions,
validity window). When an automation fires for a specific customer, the
engine calls `materialise_for_customer` which:

  1. Confirms the promotion is currently active and the customer's profile
     satisfies its conditions (segment / min spend / etc).
  2. Issues a personal `Coupon` row carrying those exact terms — the same
     `Coupon` primitive the merchant already manages, just born from a
     promotion instead of being created by hand. The code follows the
     existing `NHxxx` short-code convention so it slots cleanly into the
     coupon dashboard, the Salla sync and the `auto_coupon` resolver in
     `automation_engine`.
  3. Bumps the promotion's `usage_count` (advisory; the per-coupon usage
     is the real source of truth at checkout time on the store side).

Why issue a coupon instead of returning a raw discount?

    Across Salla / Zid / Shopify the only universally honoured artifact
    inside a WhatsApp conversation is a *code the customer types at
    checkout*. A "promotion" rule (auto-apply, no code) only works if we
    integrate deeply with each platform's promo API surface — which is
    fine for Phase 3 but blocks Phase 1+2 today. Materialising to a
    short-lived personal `Coupon` lets the same flow work everywhere
    and keeps redemption tracking in one place.

This module is deliberately import-light (only stdlib + sqlalchemy +
models + coupon_generator) so the automation engine can use it on every
event cycle without dragging in the whole router stack.
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

_THIS = os.path.dirname(os.path.abspath(__file__))
_DB = os.path.abspath(os.path.join(_THIS, "../../database"))
for _p in (_THIS, _DB):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from models import Coupon, CustomerProfile, Promotion  # noqa: E402

logger = logging.getLogger("nahla-backend")


# ── Type catalogue ────────────────────────────────────────────────────────────

PROMOTION_TYPES = {
    "percentage",
    "fixed",
    "free_shipping",
    "threshold_discount",
    "buy_x_get_y",
}

ACTIVE_STATUS = "active"
DRAFT_STATUS = "draft"
SCHEDULED_STATUS = "scheduled"
PAUSED_STATUS = "paused"
EXPIRED_STATUS = "expired"

PROMOTION_STATUSES = {
    DRAFT_STATUS,
    SCHEDULED_STATUS,
    ACTIVE_STATUS,
    PAUSED_STATUS,
    EXPIRED_STATUS,
}


# ── Pure helpers (testable without a DB) ──────────────────────────────────────

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Coerce naive datetimes (SQLite default) to UTC-aware."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def is_promotion_active(promo: Promotion, *, now: Optional[datetime] = None) -> bool:
    """
    A promotion is "live" when:
      • status == 'active'
      • starts_at is None or starts_at <= now
      • ends_at   is None or ends_at   >  now
      • usage_limit is None or usage_count < usage_limit

    We don't auto-flip status here (that's `compute_effective_status`'s job
    on read); this predicate is pure so the engine can call it in a tight
    loop without touching the DB.
    """
    if promo is None or promo.status != ACTIVE_STATUS:
        return False

    now = now or _utcnow()
    starts = _as_aware(promo.starts_at)
    ends = _as_aware(promo.ends_at)

    if starts and starts > now:
        return False
    if ends and ends <= now:
        return False
    if promo.usage_limit is not None and (promo.usage_count or 0) >= promo.usage_limit:
        return False

    return True


def compute_effective_status(promo: Promotion, *, now: Optional[datetime] = None) -> str:
    """
    Return the status the dashboard should *display*, regardless of the
    merchant's last manual edit. A promotion stored as 'active' but past
    its `ends_at` should be shown as 'expired' so the merchant isn't
    misled.

    Order of precedence:
      manual paused / draft  → returned as-is
      past ends_at           → 'expired'
      future starts_at       → 'scheduled'
      otherwise              → 'active'
    """
    now = now or _utcnow()
    raw = (promo.status or DRAFT_STATUS).lower()
    if raw in {DRAFT_STATUS, PAUSED_STATUS}:
        return raw

    starts = _as_aware(promo.starts_at)
    ends = _as_aware(promo.ends_at)
    if ends and ends <= now:
        return EXPIRED_STATUS
    if starts and starts > now:
        return SCHEDULED_STATUS
    if raw == ACTIVE_STATUS:
        return ACTIVE_STATUS
    return raw  # unknown bucket → echo back


def _customer_segment(profile: Optional[CustomerProfile]) -> Optional[str]:
    if profile is None:
        return None
    return (
        getattr(profile, "segment", None)
        or getattr(profile, "rfm_segment", None)
        or getattr(profile, "customer_status", None)
    )


def evaluate_conditions(
    promo: Promotion,
    *,
    customer_profile: Optional[CustomerProfile] = None,
    cart_total: Optional[Decimal] = None,
) -> Tuple[bool, Optional[str]]:
    """
    Evaluate whether this customer satisfies the promotion's targeting.

    Conditions schema (all keys optional):

        {
          "min_order_amount":  100,                    # SAR
          "customer_segments": ["vip", "loyal"],       # any-of match
          "applicable_products":   ["sku-1", ...],     # advisory (advisory)
          "applicable_categories": [3, 7, ...],        # advisory
          "x_quantity": 2, "y_quantity": 1,            # buy_x_get_y
          "x_product_ids": [...], "y_product_ids": [...]
        }

    Returns ``(passed, reason_when_failed)``. Conditions absent or empty
    always pass — promotions with no conditions are universal by design.
    """
    cond = dict(promo.conditions or {})

    segments_required = cond.get("customer_segments") or []
    if segments_required:
        seg = _customer_segment(customer_profile)
        if not seg or seg not in segments_required:
            return False, f"segment_mismatch (need one of {segments_required}, got {seg!r})"

    min_amount = cond.get("min_order_amount")
    if min_amount is not None and cart_total is not None:
        try:
            if Decimal(str(cart_total)) < Decimal(str(min_amount)):
                return False, f"below_min_order_amount ({cart_total} < {min_amount})"
        except Exception:
            pass

    return True, None


# ── Materialisation (issues a real Coupon row) ────────────────────────────────

def _coupon_discount_value(promo: Promotion) -> str:
    """
    Translate a Promotion into the `Coupon.discount_value` string the
    existing coupon infrastructure expects. `free_shipping` is encoded as
    "100" + a metadata flag so render code can swap the wording.
    """
    if promo.promotion_type == "free_shipping":
        return "100"
    if promo.discount_value is None:
        return "0"
    return str(promo.discount_value)


def _coupon_discount_type(promo: Promotion) -> str:
    if promo.promotion_type in {"percentage", "threshold_discount", "free_shipping", "buy_x_get_y"}:
        return "percentage"
    if promo.promotion_type == "fixed":
        return "fixed"
    return "percentage"


def _expiry_for_personal_code(promo: Promotion, *, default_days: int) -> datetime:
    """
    Personal codes inherit the promotion's `ends_at` when set, otherwise
    fall back to `default_days` from now (matches the cart-recovery
    convention of 48-72h personal coupons).
    """
    promo_end = _as_aware(promo.ends_at)
    fallback = _utcnow() + timedelta(days=default_days)
    if promo_end and promo_end < fallback:
        return promo_end
    return fallback


async def materialise_for_customer(
    db: Session,
    *,
    promotion_id: int,
    tenant_id: int,
    customer_id: Optional[int],
    expiry_days: int = 3,
    commit: bool = True,
) -> Optional[Coupon]:
    """
    Issue a personal `Coupon` row carrying this promotion's terms.

    Idempotency: if the customer already has a non-expired, unused coupon
    issued from this promotion, that coupon is returned instead of a new
    one. Prevents duplicate codes when the engine retries an event.

    Returns None if the promotion is missing / inactive / fails its
    conditions / the coupon pool helper fails. Never raises — a discount
    failure must never block a WhatsApp send.
    """
    promo = (
        db.query(Promotion)
        .filter(Promotion.id == promotion_id, Promotion.tenant_id == tenant_id)
        .first()
    )
    if promo is None:
        logger.info(
            "[PromotionEngine] no such promotion id=%s tenant=%s",
            promotion_id, tenant_id,
        )
        return None

    if not is_promotion_active(promo):
        logger.info(
            "[PromotionEngine] promotion=%s tenant=%s not active (status=%s)",
            promo.id, tenant_id, compute_effective_status(promo),
        )
        return None

    profile: Optional[CustomerProfile] = None
    if customer_id is not None:
        profile = (
            db.query(CustomerProfile)
            .filter(
                CustomerProfile.tenant_id == tenant_id,
                CustomerProfile.customer_id == customer_id,
            )
            .first()
        )
    passed, reason = evaluate_conditions(promo, customer_profile=profile)
    if not passed:
        logger.info(
            "[PromotionEngine] promotion=%s tenant=%s customer=%s failed condition: %s",
            promo.id, tenant_id, customer_id, reason,
        )
        return None

    # Idempotency: reuse an existing live personal code for this (promo, customer).
    existing = _find_existing_personal_code(db, tenant_id, promo.id, customer_id)
    if existing is not None:
        return existing

    # Issue a new short-code via the existing generator so we share the
    # NH*** uniqueness guarantee + Salla sync hook.
    try:
        from services.coupon_generator import (  # noqa: PLC0415
            CouponGeneratorService,
            _next_short_code,
        )
        gen = CouponGeneratorService(db, tenant_id)
        reserved = gen._reserved_codes()
        code = _next_short_code(reserved)
    except Exception as exc:
        logger.warning(
            "[PromotionEngine] code generation failed promo=%s tenant=%s: %s",
            promo.id, tenant_id, exc,
        )
        return None

    coupon = Coupon(
        tenant_id=tenant_id,
        code=code,
        description=promo.name,
        discount_type=_coupon_discount_type(promo),
        discount_value=_coupon_discount_value(promo),
        expires_at=_expiry_for_personal_code(promo, default_days=expiry_days),
        extra_metadata={
            "source": "promotion",
            "promotion_id": promo.id,
            "promotion_type": promo.promotion_type,
            "customer_id": customer_id,
            "issued_at": _utcnow().isoformat(),
            "usage_count": 0,
            "usage_limit": 1,
            "category": "promo",
            "active": True,
            "salla_synced": False,
            "free_shipping": promo.promotion_type == "free_shipping",
            "min_order_amount": (promo.conditions or {}).get("min_order_amount"),
        },
    )
    db.add(coupon)

    promo.usage_count = (promo.usage_count or 0) + 1
    promo.updated_at = _utcnow().replace(tzinfo=None)
    flag_modified(coupon, "extra_metadata")

    if commit:
        db.commit()
        db.refresh(coupon)
    return coupon


def _find_existing_personal_code(
    db: Session,
    tenant_id: int,
    promotion_id: int,
    customer_id: Optional[int],
) -> Optional[Coupon]:
    """Return the most recent live coupon issued from (promo, customer)."""
    if customer_id is None:
        return None

    now_naive = _utcnow().replace(tzinfo=None)
    candidates = (
        db.query(Coupon)
        .filter(Coupon.tenant_id == tenant_id)
        .order_by(Coupon.id.desc())
        .limit(50)
        .all()
    )
    for c in candidates:
        meta = c.extra_metadata or {}
        if meta.get("source") != "promotion":
            continue
        if int(meta.get("promotion_id") or 0) != promotion_id:
            continue
        if int(meta.get("customer_id") or 0) != int(customer_id):
            continue
        if str(meta.get("used") or "").lower() == "true":
            continue
        expires = c.expires_at
        if expires is not None:
            if expires.tzinfo is None:
                if expires <= now_naive:
                    continue
            elif expires <= _utcnow():
                continue
        return c
    return None


# ── Convenience: bulk status sweep used by the dashboard summary ──────────────

def sweep_expired(db: Session, tenant_id: int, *, commit: bool = True) -> int:
    """
    Flip any promotion whose `ends_at` is in the past from active/scheduled
    to 'expired'. Returns the count of rows touched. Cheap enough to call
    from the list endpoint so the merchant always sees correct badges.
    """
    now = _utcnow()
    rows: Iterable[Promotion] = (
        db.query(Promotion)
        .filter(
            Promotion.tenant_id == tenant_id,
            Promotion.status.in_([ACTIVE_STATUS, SCHEDULED_STATUS]),
            Promotion.ends_at.isnot(None),
        )
        .all()
    )
    flipped = 0
    for promo in rows:
        ends = _as_aware(promo.ends_at)
        if ends and ends <= now:
            promo.status = EXPIRED_STATUS
            flipped += 1
    if flipped and commit:
        db.commit()
    return flipped
