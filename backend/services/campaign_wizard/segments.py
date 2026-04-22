"""
campaign_wizard.segments
────────────────────────
Named, reusable customer segments shown as Step 2 of the wizard.

Design:

  * Each segment is a small `CustomerSegment` object that knows how to
    build a SQLAlchemy filter on top of `Customer (+ CustomerProfile)`.
  * Tenant scoping is *always* applied at the registry level — segments
    themselves never need to remember the tenant id, which removes the
    most common cross-tenant leak vector.
  * Counts are cheap (`SELECT COUNT(*)`) and the sample helper returns
    five rows with phone/email masked.

Sources of truth used:
  * `customers.normalized_phone`              — required for any send
  * `customer_profiles.segment`               — new | active | at_risk |
                                                churned | vip
  * `customer_profiles.rfm_segment`           — vip | promising | …
  * `customer_profiles.total_orders`          — one_time / repeat
  * `customer_profiles.lifetime_value_score`  — high spenders
  * `customer_profiles.last_order_at`         — N-day windows
  * `orders.is_abandoned` joined via JSONB     — abandoned_cart segment
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Query, Session

from models import Customer, CustomerProfile, Order

logger = logging.getLogger(__name__)


# ── Filter builders ──────────────────────────────────────────────────────────
#
# Each builder takes the running `Query[Customer]` (already joined to
# CustomerProfile via a LEFT OUTER JOIN — see `_base_query`) and returns
# the same query with extra `.filter(...)` calls. Builders never touch
# tenant scoping — that is enforced once in `_base_query`.

FilterBuilder = Callable[[Query, Session, int], Query]


def _f_all(q: Query, _db: Session, _tid: int) -> Query:
    return q


def _f_new(q: Query, _db: Session, _tid: int) -> Query:
    # "new" in nahla = profile.segment == 'new' OR no profile yet (signed
    # up but never ordered). Both branches are interesting to a welcome
    # campaign so we union them with OR-on-NULL.
    return q.filter(or_(CustomerProfile.segment == "new", CustomerProfile.id.is_(None)))


def _f_promising(q: Query, _db: Session, _tid: int) -> Query:
    return q.filter(CustomerProfile.rfm_segment == "promising")


def _f_vip(q: Query, _db: Session, _tid: int) -> Query:
    # CustomerProfile carries two parallel segment columns; either being
    # "vip" qualifies. This matches what the dashboard CRM page already
    # surfaces as "VIP" so the merchant's mental model stays consistent.
    return q.filter(or_(CustomerProfile.segment == "vip", CustomerProfile.rfm_segment == "vip"))


def _f_dormant(q: Query, _db: Session, _tid: int) -> Query:
    return q.filter(CustomerProfile.segment == "at_risk")


def _f_lost(q: Query, _db: Session, _tid: int) -> Query:
    return q.filter(CustomerProfile.segment == "churned")


def _f_one_time(q: Query, _db: Session, _tid: int) -> Query:
    return q.filter(CustomerProfile.total_orders == 1)


def _f_repeat(q: Query, _db: Session, _tid: int) -> Query:
    return q.filter(CustomerProfile.total_orders >= 2)


def _f_high_spenders(q: Query, _db: Session, _tid: int) -> Query:
    # 0.7 was chosen because lifetime_value_score is normalised 0–1 and
    # the existing CRM page colours scores >= 0.7 in the "VIP-ish" tier.
    return q.filter(CustomerProfile.lifetime_value_score >= 0.7)


def _f_no_purchase_window(days: int) -> FilterBuilder:
    """Customers whose last order is older than `days` (or who have never
    ordered). Used to build the 30/60/90-day reactivation cohorts."""

    def _builder(q: Query, _db: Session, _tid: int) -> Query:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        # NB: we accept "never ordered" only for the broadest 90-day
        # bucket — sending a 30-day reactivation message to a brand-new
        # signup would be obviously wrong.
        if days >= 90:
            return q.filter(or_(
                CustomerProfile.last_order_at.is_(None),
                CustomerProfile.last_order_at < cutoff,
            ))
        return q.filter(
            CustomerProfile.last_order_at.isnot(None),
            CustomerProfile.last_order_at < cutoff,
        )

    return _builder


def _f_abandoned_cart(q: Query, db: Session, tenant_id: int) -> Query:
    """Customers linked (via salla_customer_id) to at least one
    `orders.is_abandoned = True` row inside the same tenant.

    We deliberately avoid `customer_info->>'phone'` JSONB extraction
    here — it works in Postgres but breaks under SQLite which is what
    the unit test suite uses. salla_customer_id is a first-class column
    on both Customer and (via JSON in test DBs / first-class in some
    deployments) Order.external_id; the join below is therefore safe
    cross-dialect when the merchant is on Salla. Stores on other
    platforms simply get an empty cohort, which matches today's UX.
    """
    # Use a `select()` (not `.subquery()`) so SQLAlchemy 2.x stops
    # warning about implicit subquery coercion inside `IN`.
    abandoned_external_ids = (
        select(Order.external_id)
        .where(
            Order.tenant_id == tenant_id,
            Order.is_abandoned.is_(True),
            Order.external_id.isnot(None),
        )
    )
    return q.filter(
        Customer.salla_customer_id.isnot(None),
        Customer.salla_customer_id.in_(abandoned_external_ids),
    )


# ── Segment registry ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CustomerSegment:
    key: str
    label_ar: str
    label_en: str
    description_ar: str
    icon: str
    # Goal keys this segment is "natural" for. The recommender uses this
    # to upgrade matching templates; the wizard uses it to reorder the
    # segment list when a goal is already chosen.
    natural_goals: Tuple[str, ...]
    builder: FilterBuilder


SEGMENTS: Tuple[CustomerSegment, ...] = (
    CustomerSegment("all",            "جميع العملاء",                 "All customers",         "كل العملاء داخل قاعدة بياناتك", "Users",            (), _f_all),
    CustomerSegment("new",            "عملاء جدد",                     "New customers",         "عملاء انضموا حديثاً ولم يطلبوا بعد", "UserPlus",        ("welcome",),       _f_new),
    CustomerSegment("promising",      "عملاء واعدون",                  "Promising customers",   "عملاء أبدوا اهتماماً قوياً قابلاً للتحويل", "Sparkles",   ("promotion","reorder"), _f_promising),
    CustomerSegment("vip",            "عملاء VIP",                      "VIP customers",         "أعلى شريحة من حيث الإنفاق والولاء",   "Crown",            ("promotion","reorder"), _f_vip),
    CustomerSegment("dormant",        "عملاء خاملون",                   "Dormant customers",     "عملاء كانوا نشطين ثم خفّ تفاعلهم",      "Moon",             ("reactivation",),  _f_dormant),
    CustomerSegment("lost",           "عملاء فقدوا التفاعل",            "Lost customers",        "عملاء توقّفوا عن التفاعل تماماً",        "UserX",            ("reactivation",),  _f_lost),
    CustomerSegment("one_time",       "عملاء اشتروا مرة واحدة",         "One-time buyers",       "عملاء أكملوا طلباً واحداً فقط",          "ShoppingBag",      ("reorder",),       _f_one_time),
    CustomerSegment("repeat",         "عملاء متكررون",                  "Repeat buyers",         "عملاء أكملوا طلبين أو أكثر",            "Repeat",           ("reorder","promotion"), _f_repeat),
    CustomerSegment("high_spenders",  "عملاء مرتفعو الإنفاق",            "High spenders",         "عملاء بإنفاق إجمالي مرتفع",             "TrendingUp",       ("promotion",),     _f_high_spenders),
    CustomerSegment("abandoned_cart", "عملاء لديهم سلات متروكة",         "Abandoned cart",        "عملاء لديهم عربات لم تكتمل",             "ShoppingCart",     ("reminder","reactivation"), _f_abandoned_cart),
    CustomerSegment("no_purchase_30", "عملاء لم يشتروا منذ 30 يوماً",    "No purchase in 30d",    "آخر طلب أقدم من 30 يوماً",                "Calendar",         ("reactivation",),  _f_no_purchase_window(30)),
    CustomerSegment("no_purchase_60", "عملاء لم يشتروا منذ 60 يوماً",    "No purchase in 60d",    "آخر طلب أقدم من 60 يوماً",                "Calendar",         ("reactivation",),  _f_no_purchase_window(60)),
    CustomerSegment("no_purchase_90", "عملاء لم يشتروا منذ 90 يوماً",    "No purchase in 90d",    "آخر طلب أقدم من 90 يوماً",                "Calendar",         ("reactivation",),  _f_no_purchase_window(90)),
)


_BY_KEY: Dict[str, CustomerSegment] = {s.key: s for s in SEGMENTS}


def get_segment(key: str) -> Optional[CustomerSegment]:
    return _BY_KEY.get((key or "").strip().lower())


# ── Query helpers ────────────────────────────────────────────────────────────


def _base_query(db: Session, tenant_id: int) -> Query:
    """LEFT OUTER JOIN Customer ⨝ CustomerProfile, scoped by tenant_id.

    The OUTER join is intentional: a freshly-imported customer may not
    have a CustomerProfile row yet, but the "new" / "all" segments
    must still surface them.
    """
    return (
        db.query(Customer)
        .outerjoin(CustomerProfile, CustomerProfile.customer_id == Customer.id)
        .filter(Customer.tenant_id == tenant_id)
    )


def _reachable_filter(q: Query) -> Query:
    """A campaign can only message customers we can actually reach on
    WhatsApp. Apply this on top of every segment so the merchant never
    sees a count that includes silently-unreachable rows."""
    return q.filter(
        Customer.normalized_phone.isnot(None),
        Customer.normalized_phone != "",
    )


def build_segment_query(
    segment_key: str,
    db: Session,
    tenant_id: int,
    *,
    require_reachable: bool = True,
) -> Optional[Query]:
    """Public entry point used by the router. Returns None when the key
    is unknown so callers can 404 cleanly."""
    seg = get_segment(segment_key)
    if seg is None:
        return None
    q = _base_query(db, tenant_id)
    q = seg.builder(q, db, tenant_id)
    if require_reachable:
        q = _reachable_filter(q)
    return q


def count_segment(segment_key: str, db: Session, tenant_id: int) -> int:
    """Reachable customer count for a single segment, scoped to tenant.

    Returns 0 (not None) on unknown segment so the API can render a
    consistent UI even if the frontend ever sends a stale key. Errors
    (e.g. abandoned_cart on stores not on Salla) are swallowed and
    logged, again returning 0 — refusing to show *any* counts because
    one segment misbehaves would be a worse UX than showing 0 here.
    """
    q = build_segment_query(segment_key, db, tenant_id)
    if q is None:
        return 0
    try:
        # NB: use a fresh count query rather than `.count()` on the
        # outer-joined query — SQLAlchemy's ORM `.count()` wraps in a
        # subquery that, with the OUTER JOIN to CustomerProfile, can
        # over-count if a customer somehow has multiple profile rows.
        # We count distinct Customer.id explicitly for safety.
        return q.with_entities(func.count(func.distinct(Customer.id))).scalar() or 0
    except Exception as exc:
        logger.warning(
            "[campaign_wizard.segments] count failed for segment=%s tenant=%s: %s",
            segment_key, tenant_id, exc,
        )
        return 0


def list_segments_with_counts(db: Session, tenant_id: int) -> List[Dict[str, Any]]:
    """Public: every segment + its reachable count for this tenant.
    Used by `GET /campaigns/wizard/segments`. Counts are computed
    sequentially (13 queries) — fine for a wizard step that is hit
    once per session and never on a hot path."""
    out: List[Dict[str, Any]] = []
    for seg in SEGMENTS:
        out.append({
            "key": seg.key,
            "label_ar": seg.label_ar,
            "label_en": seg.label_en,
            "description_ar": seg.description_ar,
            "icon": seg.icon,
            "natural_goals": list(seg.natural_goals),
            "customer_count": count_segment(seg.key, db, tenant_id),
        })
    return out


def _mask_phone(phone: Optional[str]) -> str:
    """Show the last 4 digits only — enough for the merchant to recognise
    a row in their own customer base, not enough to dox in a screenshot."""
    if not phone:
        return ""
    s = str(phone)
    if len(s) <= 4:
        return s
    return "•" * (len(s) - 4) + s[-4:]


def _mask_email(email: Optional[str]) -> str:
    if not email or "@" not in str(email):
        return ""
    local, _, domain = str(email).partition("@")
    if not local:
        return f"@{domain}"
    head = local[0]
    return f"{head}{'•' * max(1, len(local) - 1)}@{domain}"


def sample_segment(
    segment_key: str, db: Session, tenant_id: int, *, limit: int = 5,
) -> List[Dict[str, Any]]:
    """Return up to `limit` customers from the segment, with phone/email
    masked. Powers the "preview" on Step 2 so the merchant can sanity-
    check who they are about to message."""
    q = build_segment_query(segment_key, db, tenant_id)
    if q is None:
        return []
    try:
        rows = q.order_by(Customer.id.desc()).limit(limit).all()
    except Exception as exc:
        logger.warning(
            "[campaign_wizard.segments] sample failed for segment=%s tenant=%s: %s",
            segment_key, tenant_id, exc,
        )
        return []
    return [
        {
            "id": c.id,
            "name": c.name or "—",
            "phone_masked": _mask_phone(c.normalized_phone or c.phone),
            "email_masked": _mask_email(c.email),
        }
        for c in rows
    ]
