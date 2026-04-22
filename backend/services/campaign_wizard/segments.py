"""
campaign_wizard.segments
────────────────────────
Named, reusable customer segments — **the canonical Nahla segment
registry**. Used by:

  * Campaign wizard Step 2 (target audience)
  * Customers page filter chips (`GET /customers?segment=...`)
  * Future: Autopilot rule conditions, Analytics dashboards

Single registry, single SQL, single set of definitions — so a count
shown on the Customers page always equals the count shown in the
wizard for the same tenant + segment.

Design:

  * Each segment is a small `CustomerSegment` object that knows how to
    build a SQLAlchemy filter on top of `Customer (+ CustomerProfile)`.
  * Tenant scoping is *always* applied at the registry level — segments
    themselves never need to remember the tenant id, which removes the
    most common cross-tenant leak vector.
  * Counts are cheap (`SELECT COUNT(*)`) and the sample helper returns
    five rows with phone/email masked.

Sources of truth used (matches `services/customer_intelligence.py`):

  * `customers.normalized_phone`              — required for any send
  * `customer_profiles.segment`               — lead | new | active |
                                                vip | at_risk | inactive
                                                (see CUSTOMER_STATUS_ORDER)
  * `customer_profiles.rfm_segment`           — champions | loyal_customers
                                                | promising | needs_attention
                                                | about_to_sleep | at_risk
                                                | cant_lose_them | hibernating
                                                | lost_customers | regulars
                                                (see RFM_SEGMENT_ORDER)
  * `customer_profiles.total_orders`          — one_time / repeat
  * `customer_profiles.lifetime_value_score`  — high spenders (0–1 normalised)
  * `customer_profiles.last_order_at`         — N-day windows
  * `orders.is_abandoned`                     — abandoned_cart segment

IMPORTANT: there is NO `"churned"` or `"vip"` value in `rfm_segment`,
and NO `"churned"` value in `segment`. Earlier drafts of this file
used those names (mirroring the CustomerProfile column comment) but
they never matched real data — the CRM canonically uses `"inactive"`
for churn and `"champions"` / `"cant_lose_them"` for the top RFM
buckets. Always cross-check against `customer_intelligence.py` before
adding a new filter here.
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
    # "New" in Nahla's mental model = anyone who joined the tenant but
    # hasn't built a buying history yet, OR who placed their first
    # order within the last 30 days. That covers three concrete shapes:
    #
    #   1. Imported/registered, no profile row yet            (id IS NULL)
    #   2. Has profile but `compute_customer_status` returned 'lead'
    #      (signed up, zero countable orders)
    #   3. Has profile and was tagged 'new' (first order ≤ 30d ago)
    #
    # Earlier this filter only matched (1) + (3) and silently excluded
    # 'lead' customers — which were exactly the people a welcome
    # campaign should target. See `compute_customer_status` in
    # services/customer_intelligence.py for the canonical definitions.
    return q.filter(or_(
        CustomerProfile.id.is_(None),
        CustomerProfile.segment == "lead",
        CustomerProfile.segment == "new",
    ))


def _f_promising(q: Query, _db: Session, _tid: int) -> Query:
    # 'promising' is an RFM bucket from `compute_rfm_segment`: customers
    # whose recency+frequency suggest growth potential. Also include
    # `potential_loyalists` because the merchant intent is identical
    # ("ready to convert into repeat buyers") and the two buckets shift
    # between recompute runs.
    return q.filter(or_(
        CustomerProfile.rfm_segment == "promising",
        CustomerProfile.rfm_segment == "potential_loyalists",
    ))


def _f_vip(q: Query, _db: Session, _tid: int) -> Query:
    # VIP comes from two complementary signals:
    #   * `compute_customer_status` writes 'vip' on high spend/frequency.
    #   * `compute_rfm_segment` writes 'champions' (top RFM cell) and
    #     'cant_lose_them' (high LTV but recency dropping).
    # There is NO `rfm_segment == 'vip'` bucket — the previous filter
    # using that value matched zero rows. Union the three real buckets
    # so the merchant's "VIP customers" chip mirrors the same audience
    # the CRM page colours gold.
    return q.filter(or_(
        CustomerProfile.segment == "vip",
        CustomerProfile.rfm_segment == "champions",
        CustomerProfile.rfm_segment == "cant_lose_them",
    ))


def _f_dormant(q: Query, _db: Session, _tid: int) -> Query:
    # "Dormant" = at the edge of churn but not gone yet. Matches both
    # the explicit CRM `at_risk` status (60–90 days idle in
    # `compute_customer_status`) and the RFM `at_risk` / `about_to_sleep`
    # / `needs_attention` buckets which capture the same intent on
    # accounts whose status hasn't yet flipped to `inactive`.
    return q.filter(or_(
        CustomerProfile.segment == "at_risk",
        CustomerProfile.rfm_segment == "at_risk",
        CustomerProfile.rfm_segment == "about_to_sleep",
        CustomerProfile.rfm_segment == "needs_attention",
    ))


def _f_lost(q: Query, _db: Session, _tid: int) -> Query:
    # The CRM uses 'inactive' (NOT 'churned') for >90-day-idle customers
    # — the previous filter on 'churned' silently matched zero rows.
    # Union with the RFM 'lost_customers' / 'hibernating' buckets so the
    # chip captures the full "lost engagement" cohort the merchant cares
    # about for a winback campaign.
    return q.filter(or_(
        CustomerProfile.segment == "inactive",
        CustomerProfile.rfm_segment == "lost_customers",
        CustomerProfile.rfm_segment == "hibernating",
    ))


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


def count_segment(
    segment_key: str,
    db: Session,
    tenant_id: int,
    *,
    require_reachable: bool = True,
) -> int:
    """Customer count for a single segment, scoped to tenant.

    `require_reachable=True` (default) — counts only customers the
    merchant can actually message on WhatsApp; this is what the campaign
    wizard wants. The customers-management page passes `False` so the
    chip says "VIP (12)" even if one of those 12 has a bad phone, so
    the merchant can see them in the list and fix the data.

    Returns 0 (not None) on unknown segment so the API can render a
    consistent UI even if the frontend ever sends a stale key. Errors
    (e.g. abandoned_cart on stores not on Salla) are swallowed and
    logged, again returning 0 — refusing to show *any* counts because
    one segment misbehaves would be a worse UX than showing 0 here.
    """
    q = build_segment_query(
        segment_key, db, tenant_id, require_reachable=require_reachable,
    )
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
