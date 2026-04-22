"""
services.nahla_segments
───────────────────────
The **single, canonical Nahla customer-segment registry**.

This module is the *one* place in the system where the marketing-facing
cohorts ("Nahla 🐝 segments") are defined. It is consumed by:

  * Campaign wizard Step 2  → `services.campaign_wizard.segments` (thin shim)
  * Customers page chips    → `routers.customers` /customers/segments + ?segment=
  * Future: Autopilot rules → `core.automation_engine` cohort conditions
  * Future: Analytics       → `routers.analytics` cohort breakdowns
  * Future: AI brain        → `modules.ai.brain.facts.sales_context` segment hints

If you want to add, rename, or change the SQL of a Nahla segment,
do it **here** and only here. Every other file should import from
this module (directly or via the `campaign_wizard.segments` shim
that re-exports for backward compat).

──────────────────────────────────────────────────────────────────────
Two layers — keep them straight
──────────────────────────────────────────────────────────────────────

There are two related but DIFFERENT concepts in the codebase:

1. **CRM atoms**  — single-column values written to every
   `customer_profiles` row by `services.customer_intelligence`:
     * `customer_status / segment` ∈ CUSTOMER_STATUS_ORDER
       (`lead`, `new`, `active`, `vip`, `at_risk`, `inactive`)
     * `rfm_segment`              ∈ RFM_SEGMENT_ORDER
       (`champions`, `loyal_customers`, `promising`, …, `lost_customers`)

   These are the source-of-truth columns. They are recomputed every
   time `recompute_profile_for_customer` runs (after a new order, a
   manual edit, the nightly rebuild, etc.). Nothing in this file
   *writes* them — we only *read* them.

2. **Marketing cohorts** (this file) — higher-level audiences that
   merchants think and act on: "VIP customers", "dormant customers",
   "abandoned cart", "no purchase in 60 days". Each cohort is built
   on top of the CRM atoms (often as a UNION of several status / RFM
   buckets) plus extra signals (`total_orders`, `lifetime_value_score`,
   `last_order_at`, `orders.is_abandoned`).

`SEGMENTS` below is the registry of layer 2. Each segment carries the
full set of CRM atoms it reads (`crm_statuses`, `rfm_buckets`) so the
docs / UI / coherence tests can show the merchant exactly what a chip
means without having to read SQL.

──────────────────────────────────────────────────────────────────────
Cross-tenant safety
──────────────────────────────────────────────────────────────────────

Tenant scoping is enforced exactly once, in `_base_query`. Filter
builders never see tenant_id directly (other than the `abandoned_cart`
join that needs it for the Orders subquery). This makes it impossible
for a future contributor to accidentally write a cross-tenant leak.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Query, Session

from models import Customer, CustomerProfile, Order
from services.customer_intelligence import (
    CUSTOMER_STATUS_ORDER,
    RFM_SEGMENT_ORDER,
)

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
    return q.filter(or_(
        CustomerProfile.id.is_(None),
        CustomerProfile.segment == "lead",
        CustomerProfile.segment == "new",
    ))


def _f_promising(q: Query, _db: Session, _tid: int) -> Query:
    return q.filter(or_(
        CustomerProfile.rfm_segment == "promising",
        CustomerProfile.rfm_segment == "potential_loyalists",
    ))


def _f_vip(q: Query, _db: Session, _tid: int) -> Query:
    return q.filter(or_(
        CustomerProfile.segment == "vip",
        CustomerProfile.rfm_segment == "champions",
        CustomerProfile.rfm_segment == "cant_lose_them",
    ))


def _f_dormant(q: Query, _db: Session, _tid: int) -> Query:
    return q.filter(or_(
        CustomerProfile.segment == "at_risk",
        CustomerProfile.rfm_segment == "at_risk",
        CustomerProfile.rfm_segment == "about_to_sleep",
        CustomerProfile.rfm_segment == "needs_attention",
    ))


def _f_lost(q: Query, _db: Session, _tid: int) -> Query:
    return q.filter(or_(
        CustomerProfile.segment == "inactive",
        CustomerProfile.rfm_segment == "lost_customers",
        CustomerProfile.rfm_segment == "hibernating",
    ))


def _f_one_time(q: Query, _db: Session, _tid: int) -> Query:
    return q.filter(CustomerProfile.total_orders == 1)


def _f_repeat(q: Query, _db: Session, _tid: int) -> Query:
    return q.filter(CustomerProfile.total_orders >= 2)


# Threshold for the "high spenders" cohort. `lifetime_value_score` is
# normalised 0..1 by `compute_lifetime_value_score`. 0.7 was chosen
# because the existing CRM page colours scores >= 0.7 in the VIP-ish
# tier — keeping a single threshold across the codebase avoids "12
# whales here, 9 there" inconsistencies.
HIGH_SPENDER_LTV_THRESHOLD = 0.7


def _f_high_spenders(q: Query, _db: Session, _tid: int) -> Query:
    return q.filter(CustomerProfile.lifetime_value_score >= HIGH_SPENDER_LTV_THRESHOLD)


def _f_no_purchase_window(days: int) -> FilterBuilder:
    """Customers whose last order is older than `days` (or, for the broadest
    90-day bucket only, who have never ordered). Powers the 30/60/90-day
    reactivation cohorts."""

    def _builder(q: Query, _db: Session, _tid: int) -> Query:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
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
    """Customers (Salla-only today) linked via `salla_customer_id` to at
    least one `orders.is_abandoned = True` row inside the same tenant.

    Stores on other platforms get an empty cohort, which is a known
    limitation surfaced in the segment description so the merchant
    doesn't think their data is missing.
    """
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
class NahlaSegment:
    """One marketing cohort.

    Fields:
        key:               machine-stable id used in URLs / API payloads.
        label_ar / _en:    short label shown on a chip.
        description_ar:    one-liner shown as tooltip on the chip.
        criteria_ar:       full plain-Arabic explanation of which customers
                           land in this cohort. Shown to the merchant in
                           the segment info card / wizard step. **Always
                           kept in sync with the SQL `builder` below.**
        icon:              lucide-react icon name (frontend renders).
        natural_goals:     campaign goal keys this cohort is "natural" for.
                           The wizard recommender uses this to pre-suggest
                           segments and to score templates.
        crm_statuses:      tuple of `customer_status / segment` values
                           this cohort consumes. Empty tuple means the
                           filter does not depend on `customer_status`.
        rfm_buckets:       tuple of `rfm_segment` values consumed.
                           Empty tuple means the filter does not depend
                           on RFM.
        builder:           SQLAlchemy filter — the only authoritative
                           definition. The text fields above MUST stay
                           in sync with this; coherence tests in
                           `tests/test_nahla_segments.py` enforce that
                           every value listed in `crm_statuses` /
                           `rfm_buckets` actually exists in the
                           `*_ORDER` enums of `customer_intelligence.py`.
    """
    key: str
    label_ar: str
    label_en: str
    description_ar: str
    criteria_ar: str
    icon: str
    natural_goals: Tuple[str, ...]
    crm_statuses: Tuple[str, ...]
    rfm_buckets:  Tuple[str, ...]
    builder: FilterBuilder = field(repr=False)


# Order matters — this is the order the chips render in.
SEGMENTS: Tuple[NahlaSegment, ...] = (
    NahlaSegment(
        key="all",
        label_ar="جميع العملاء",
        label_en="All customers",
        description_ar="كل العملاء داخل قاعدة بياناتك",
        criteria_ar=(
            "كل عميل مسجّل في المتجر داخل نحلة، بصرف النظر عن مرحلته أو "
            "تفاعله. مفيد لاستعراض القاعدة كاملة أو لإرسال إعلان عام."
        ),
        icon="Users",
        natural_goals=(),
        crm_statuses=(),
        rfm_buckets=(),
        builder=_f_all,
    ),
    NahlaSegment(
        key="new",
        label_ar="عملاء جدد",
        label_en="New customers",
        description_ar="عملاء انضموا حديثاً ولم يطلبوا أو طلبوا منذ أيام قليلة",
        criteria_ar=(
            "العميل يدخل هذه الشريحة في إحدى ثلاث حالات: لم تُحسب له بعد "
            "بطاقة سلوك (انضم للتو)، أو حالته «عميل محتمل» (سجّل بدون "
            "أي طلب)، أو حالته «عميل جديد» (أوّل طلب خلال آخر 30 يوماً)."
        ),
        icon="UserPlus",
        natural_goals=("welcome",),
        crm_statuses=("lead", "new"),
        rfm_buckets=(),
        builder=_f_new,
    ),
    NahlaSegment(
        key="promising",
        label_ar="عملاء واعدون",
        label_en="Promising customers",
        description_ar="عملاء أبدوا اهتماماً قوياً قابلاً للتحويل",
        criteria_ar=(
            "حسب تحليل RFM، هؤلاء عملاء قاب قوسين من أن يصبحوا متكررين: "
            "تفاعلهم وتكرار شرائهم يضعهم في خانة «واعد» أو «مرشح للولاء». "
            "حملة عرض ذكية الآن قد تحوّلهم إلى عملاء أوفياء."
        ),
        icon="Sparkles",
        natural_goals=("promotion", "reorder"),
        crm_statuses=(),
        rfm_buckets=("promising", "potential_loyalists"),
        builder=_f_promising,
    ),
    NahlaSegment(
        key="vip",
        label_ar="عملاء VIP",
        label_en="VIP customers",
        description_ar="أعلى شريحة من حيث الإنفاق والولاء",
        criteria_ar=(
            "العميل يصبح VIP إذا تحقّق أحد الآتي: حالته الحسابية «VIP» "
            "(إنفاق إجمالي ≥ 3000 ريال أو ≥ 2000 ريال على ≥ 5 طلبات)، "
            "أو موقعه في تحليل RFM «الأبطال» (Champions)، أو «لا يجب "
            "خسارتهم» (Cant Lose Them — عميل غالٍ بدأ يقلّ تفاعله)."
        ),
        icon="Crown",
        natural_goals=("promotion", "reorder"),
        crm_statuses=("vip",),
        rfm_buckets=("champions", "cant_lose_them"),
        builder=_f_vip,
    ),
    NahlaSegment(
        key="dormant",
        label_ar="عملاء خاملون",
        label_en="Dormant customers",
        description_ar="عملاء كانوا نشطين ثم خفّ تفاعلهم — لم يفقدوا بعد",
        criteria_ar=(
            "حالة العميل «في خطر المغادرة» (آخر طلب بين 60 و 90 يوماً)، "
            "أو موقعه في RFM «في خطر»، «على وشك الخمول»، أو «يحتاج "
            "اهتماماً». رسالة تنشيط مبكرة الآن أرخص بكثير من محاولة "
            "استعادتهم لاحقاً."
        ),
        icon="Moon",
        natural_goals=("reactivation",),
        crm_statuses=("at_risk",),
        rfm_buckets=("at_risk", "about_to_sleep", "needs_attention"),
        builder=_f_dormant,
    ),
    NahlaSegment(
        key="lost",
        label_ar="عملاء فقدوا التفاعل",
        label_en="Lost customers",
        description_ar="عملاء توقّفوا تماماً عن التفاعل ويحتاجون استعادة",
        criteria_ar=(
            "حالة العميل «غير نشط» (لم يطلب منذ أكثر من 90 يوماً)، أو "
            "موقعه في RFM «عملاء مفقودون» أو «شبه خاملون». تحتاج "
            "حملة استعادة قوية، عادةً مع كوبون أو عرض خاص."
        ),
        icon="UserX",
        natural_goals=("reactivation",),
        crm_statuses=("inactive",),
        rfm_buckets=("lost_customers", "hibernating"),
        builder=_f_lost,
    ),
    NahlaSegment(
        key="one_time",
        label_ar="عملاء اشتروا مرة واحدة",
        label_en="One-time buyers",
        description_ar="عملاء أكملوا طلباً واحداً فقط",
        criteria_ar=(
            "إجمالي الطلبات المحسوبة (المُنجزة، باستثناء الملغاة "
            "والسلات المتروكة) يساوي طلباً واحداً بالضبط. الهدف "
            "تحويل هذا الطلب الأول إلى علاقة مستمرة."
        ),
        icon="ShoppingBag",
        natural_goals=("reorder",),
        crm_statuses=(),
        rfm_buckets=(),
        builder=_f_one_time,
    ),
    NahlaSegment(
        key="repeat",
        label_ar="عملاء متكررون",
        label_en="Repeat buyers",
        description_ar="عملاء أكملوا طلبين أو أكثر",
        criteria_ar=(
            "إجمالي الطلبات المحسوبة ≥ 2. هؤلاء عملاء أثبتوا أنهم "
            "يثقون بالمتجر — مرشّحون مثاليون لعروض الولاء وإعادة "
            "الشراء."
        ),
        icon="Repeat",
        natural_goals=("reorder", "promotion"),
        crm_statuses=(),
        rfm_buckets=(),
        builder=_f_repeat,
    ),
    NahlaSegment(
        key="high_spenders",
        label_ar="عملاء مرتفعو الإنفاق",
        label_en="High spenders",
        description_ar="عملاء بإنفاق إجمالي مرتفع",
        criteria_ar=(
            f"درجة قيمة العمر التشغيلية (LTV) ≥ {HIGH_SPENDER_LTV_THRESHOLD:.2f} "
            "(على مقياس 0–1). تُحسب هذه الدرجة تلقائياً بعد كل طلب جديد "
            "أو إعادة حساب البطاقة، ولا تتطلب أي إعداد يدوي."
        ),
        icon="TrendingUp",
        natural_goals=("promotion",),
        crm_statuses=(),
        rfm_buckets=(),
        builder=_f_high_spenders,
    ),
    NahlaSegment(
        key="abandoned_cart",
        label_ar="عملاء لديهم سلات متروكة",
        label_en="Abandoned cart",
        description_ar="عملاء بدأوا الشراء ثم تركوا السلة قبل الإكمال",
        criteria_ar=(
            "العميل لديه على الأقل سلة واحدة بحالة «متروكة» في طلبات "
            "المتجر. يعمل حالياً مع متاجر سلة فقط — متاجر المنصّات "
            "الأخرى ستُظهر صفراً حتى يدعم النظام مزامنة سلاتها."
        ),
        icon="ShoppingCart",
        natural_goals=("reminder", "reactivation"),
        crm_statuses=(),
        rfm_buckets=(),
        builder=_f_abandoned_cart,
    ),
    NahlaSegment(
        key="no_purchase_30",
        label_ar="عملاء لم يشتروا منذ 30 يوماً",
        label_en="No purchase in 30d",
        description_ar="آخر طلب أقدم من 30 يوماً (سبق له الشراء)",
        criteria_ar=(
            "العميل سبق له الشراء، لكن آخر طلب له أقدم من 30 يوماً. "
            "العملاء الذين لم يطلبوا أبداً مُستثنون لأن إرسال تذكير "
            "إعادة شراء لمن لم يشتري قط رسالة غير مناسبة."
        ),
        icon="Calendar",
        natural_goals=("reactivation",),
        crm_statuses=(),
        rfm_buckets=(),
        builder=_f_no_purchase_window(30),
    ),
    NahlaSegment(
        key="no_purchase_60",
        label_ar="عملاء لم يشتروا منذ 60 يوماً",
        label_en="No purchase in 60d",
        description_ar="آخر طلب أقدم من 60 يوماً (سبق له الشراء)",
        criteria_ar=(
            "العميل سبق له الشراء، لكن آخر طلب له أقدم من 60 يوماً. "
            "نافذة مناسبة لحملة تنشيط متوسطة الإلحاح."
        ),
        icon="Calendar",
        natural_goals=("reactivation",),
        crm_statuses=(),
        rfm_buckets=(),
        builder=_f_no_purchase_window(60),
    ),
    NahlaSegment(
        key="no_purchase_90",
        label_ar="عملاء لم يشتروا منذ 90 يوماً",
        label_en="No purchase in 90d",
        description_ar="آخر طلب أقدم من 90 يوماً، أو لم يطلب أبداً",
        criteria_ar=(
            "العميل آخر طلب له أقدم من 90 يوماً، أو لم يطلب أبداً. "
            "هذه الشريحة أوسع لأن عميلاً لم يطلب منذ 90 يوماً يستحق "
            "محاولة استعادة قوية بنفس قدر العميل الذي لم يطلب أبداً "
            "بعد التسجيل."
        ),
        icon="Calendar",
        natural_goals=("reactivation",),
        crm_statuses=(),
        rfm_buckets=(),
        builder=_f_no_purchase_window(90),
    ),
)


_BY_KEY: Dict[str, NahlaSegment] = {s.key: s for s in SEGMENTS}


def get_segment(key: str) -> Optional[NahlaSegment]:
    return _BY_KEY.get((key or "").strip().lower())


def all_segment_keys() -> Tuple[str, ...]:
    """Stable ordered tuple of every Nahla segment key. Useful for tests
    and for any future place that needs to enumerate cohorts without
    importing the full registry."""
    return tuple(s.key for s in SEGMENTS)


def serialize_segment(seg: NahlaSegment, customer_count: int) -> Dict[str, Any]:
    """Public JSON shape returned by both /campaigns/wizard/segments and
    /customers/segments. Centralised here so the two surfaces never
    drift in field names or types."""
    return {
        "key":             seg.key,
        "label_ar":        seg.label_ar,
        "label_en":        seg.label_en,
        "description_ar":  seg.description_ar,
        "criteria_ar":     seg.criteria_ar,
        "icon":            seg.icon,
        "natural_goals":   list(seg.natural_goals),
        "crm_statuses":    list(seg.crm_statuses),
        "rfm_buckets":     list(seg.rfm_buckets),
        "customer_count":  customer_count,
    }


# ── Query helpers ────────────────────────────────────────────────────────────


def _base_query(db: Session, tenant_id: int) -> Query:
    """LEFT OUTER JOIN Customer ⨝ CustomerProfile, scoped by tenant_id.

    The OUTER join is intentional: a freshly-imported customer may not
    have a CustomerProfile row yet, but the "new" / "all" segments must
    still surface them.
    """
    return (
        db.query(Customer)
        .outerjoin(CustomerProfile, CustomerProfile.customer_id == Customer.id)
        .filter(Customer.tenant_id == tenant_id)
    )


def _reachable_filter(q: Query) -> Query:
    """A campaign can only message customers we can actually reach on
    WhatsApp. Apply on top of every segment so the wizard's count never
    includes silently-unreachable rows."""
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
    """Public entry point used by both routers. Returns None when the
    key is unknown so callers can 422 cleanly."""
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
    """
    q = build_segment_query(
        segment_key, db, tenant_id, require_reachable=require_reachable,
    )
    if q is None:
        return 0
    try:
        return q.with_entities(func.count(func.distinct(Customer.id))).scalar() or 0
    except Exception as exc:  # noqa: silent-ok — see comment below
        # `abandoned_cart` on a non-Salla tenant raises because
        # `salla_customer_id` may be NULL across the table; rather than
        # 500ing the whole segments endpoint we log and return 0 so the
        # other twelve chips still render their counts.
        logger.warning(
            "[nahla_segments] count failed for segment=%s tenant=%s: %s",
            segment_key, tenant_id, exc,
        )
        return 0


def list_segments_with_counts(
    db: Session,
    tenant_id: int,
    *,
    require_reachable: bool = True,
) -> List[Dict[str, Any]]:
    """Public: every segment + its count for this tenant. Counts are
    computed sequentially (one query per segment, ~13 total) — fine
    for a low-frequency endpoint hit once per page load.

    Both /campaigns/wizard/segments and /customers/segments call this
    so they always emit the exact same shape and the exact same numbers.
    """
    out: List[Dict[str, Any]] = []
    for seg in SEGMENTS:
        n = count_segment(seg.key, db, tenant_id, require_reachable=require_reachable)
        out.append(serialize_segment(seg, n))
    return out


# ── Sample helpers (preview) ─────────────────────────────────────────────────


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
    segment_key: str,
    db: Session,
    tenant_id: int,
    *,
    limit: int = 5,
    require_reachable: bool = True,
) -> List[Dict[str, Any]]:
    """Return up to `limit` customers from the segment, with phone/email
    masked. Powers the wizard's Step 2 preview and the customers page's
    "show me 5 examples" affordance."""
    q = build_segment_query(
        segment_key, db, tenant_id, require_reachable=require_reachable,
    )
    if q is None:
        return []
    try:
        rows = q.order_by(Customer.id.desc()).limit(limit).all()
    except Exception as exc:  # noqa: silent-ok — same reasoning as count_segment
        logger.warning(
            "[nahla_segments] sample failed for segment=%s tenant=%s: %s",
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


# ── Coherence helpers ────────────────────────────────────────────────────────


def coherence_report() -> Dict[str, Any]:
    """Human-readable structural audit of the registry. Used by the test
    suite to fail loudly if any `crm_statuses` / `rfm_buckets` value
    drifts from the canonical CUSTOMER_STATUS_ORDER / RFM_SEGMENT_ORDER
    enums.

    Returns a dict with `errors: list[str]` (empty when consistent).
    """
    errors: List[str] = []
    for seg in SEGMENTS:
        for s in seg.crm_statuses:
            if s not in CUSTOMER_STATUS_ORDER:
                errors.append(
                    f"segment '{seg.key}' lists CRM status '{s}' which is "
                    f"NOT in CUSTOMER_STATUS_ORDER {CUSTOMER_STATUS_ORDER}"
                )
        for r in seg.rfm_buckets:
            if r not in RFM_SEGMENT_ORDER:
                errors.append(
                    f"segment '{seg.key}' lists RFM bucket '{r}' which is "
                    f"NOT in RFM_SEGMENT_ORDER {RFM_SEGMENT_ORDER}"
                )
    return {
        "segment_count": len(SEGMENTS),
        "errors":        errors,
    }


__all__ = [
    "NahlaSegment",
    "SEGMENTS",
    "HIGH_SPENDER_LTV_THRESHOLD",
    "get_segment",
    "all_segment_keys",
    "serialize_segment",
    "build_segment_query",
    "count_segment",
    "list_segments_with_counts",
    "sample_segment",
    "coherence_report",
]
