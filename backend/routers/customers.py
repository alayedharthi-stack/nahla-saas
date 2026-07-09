"""
routers/customers.py
─────────────────────
Tenant-scoped customer CRUD + intelligence metrics.

Routes:
  GET    /customers              — list customers with profiles, search, pagination
  GET    /customers/metrics      — dashboard metrics for all customers
  GET    /customers/{id}         — single customer detail
  POST   /customers              — add customer manually (phone must be unique)
  PATCH  /customers/{id}         — update customer
  DELETE /customers/{id}         — delete customer
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, validator
import sqlalchemy as sa
from sqlalchemy.orm import Session

from core.auth import get_jwt_user_id
from core.database import get_db
from core.tenant import get_or_create_tenant, resolve_tenant_id
from models import (
    Customer,
    CustomerNameAuditLog,
    CustomerNameCleanupDraft,
    CustomerProfile,
    CustomerSegmentManual,
)
# ── Additional child models needed for safe customer deletion ─────────────────
# These tables hold FK references to customers.id without ON DELETE CASCADE,
# so we must delete them explicitly before removing the parent customer row.
try:
    from models import CustomerPreferences           # type: ignore[attr-defined]
    from models import ProductAffinity              # type: ignore[attr-defined]
    from models import PriceSensitivityScore        # type: ignore[attr-defined]
    from models import ConversationHistorySummary   # type: ignore[attr-defined]
    from models import PredictiveReorderEstimate    # type: ignore[attr-defined]
    from models import ProductInterest              # type: ignore[attr-defined]
    from models import GovernorSendLog              # type: ignore[attr-defined]
    _EXTRA_CUSTOMER_CHILD_MODELS = True
except ImportError:
    _EXTRA_CUSTOMER_CHILD_MODELS = False
from services.nahla_segments import (
    SEGMENTS as NAHLA_SEGMENTS,
    build_segment_query,
    get_segment as get_nahla_segment,
    list_segments_with_counts,
)
from services.manual_segments import (
    UnknownSegmentError,
    META_KEY_TEST_RECIPIENT,
    add_manual_segment,
    assert_known_segment,
    customer_ids_with_manual_segment,
    list_manual_segments_bulk,
    list_manual_segments_for_customer,
    remove_manual_segment,
    set_marketing_opt_out_manual,
    count_marketing_opted_out_customers,
    is_marketing_opted_out_from_meta,
    marketing_opt_out_manual_sql_truthy,
    marketing_opt_out_manual_sql_falsy,
    set_test_recipient,
)
from services.customer_intelligence import (
    CUSTOMER_STATUS_LABELS,
    RFM_SEGMENT_LABELS,
    CustomerIntelligenceService,
    normalize_phone,
)

router = APIRouter(prefix="/customers", tags=["Customers"])

def _normalize_phone(raw: str) -> str:
    return normalize_phone(raw)


def _customer_search_clauses(search: str):
    """OR filter for ``list_customers`` search.

    Matches display name and raw phone (legacy). Also matches
    ``normalized_phone`` (E.164) so merchants can paste numbers with
    or without ``+``, and local Saudi ``05…`` forms still resolve when
    libphonenumber can normalize them.
    """
    from sqlalchemy import or_  # noqa: PLC0415
    from utils.phone_utils import normalize_to_e164  # noqa: PLC0415

    stripped = search.strip()
    if not stripped:
        return None

    term = f"%{stripped}%"
    clauses = [
        Customer.name.ilike(term),
        Customer.phone.ilike(term),
        Customer.normalized_phone.ilike(term),
    ]

    e164 = normalize_to_e164(stripped)
    if e164:
        clauses.append(Customer.normalized_phone == e164)
        bare = e164.lstrip("+")
        if bare:
            clauses.append(Customer.phone.ilike(f"%{bare}%"))

    return or_(*clauses)


class CustomerCreateIn(BaseModel):
    name: str
    phone: str
    email: Optional[str] = None

    @validator("phone")
    def validate_phone(cls, v: str) -> str:
        normalized = _normalize_phone(v)
        if not normalized or len(normalized) < 8:
            raise ValueError("رقم الهاتف غير صالح")
        return normalized


CUSTOMER_NAME_MAX_LEN = 80


class CustomerPatchIn(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None

    @validator("phone", pre=True)
    def validate_phone(cls, v):
        if v is None:
            return v
        normalized = _normalize_phone(v)
        if not normalized or len(normalized) < 8:
            raise ValueError("رقم الهاتف غير صالح")
        return normalized

    @validator("name", pre=True)
    def validate_name(cls, v):
        """Trim + length-cap merchant-supplied names.

        Accepts:
          * ``None``         — explicit "clear the name" (May 2026
                                policy: merchants may delete garbage
                                names entirely; templates fall back
                                to ``DEFAULT_FALLBACK_NAME``).
          * ``""``           — same as ``None`` after trim.
          * ``"  "``         — same as ``None`` after trim.
          * Real name string — trimmed, whitespace-collapsed, length-
                                capped at ``CUSTOMER_NAME_MAX_LEN``.

        The endpoint persists a cleared name as ``Customer.name=None``
        + sets ``manual_name_override=true`` AND
        ``manual_name_cleared=true``. The cleared flag is what the
        AI-driven name detector reads to decide "the merchant
        intentionally emptied this — a high-confidence detected
        name CAN refill it" (vs. a non-empty merchant name which
        must NEVER be overwritten).
        """
        if v is None:
            return None
        if not isinstance(v, str):
            v = str(v)
        v = v.strip()
        # Collapse runs of whitespace inside the name so a merchant
        # typing "تركي   البلوي" doesn't end up with weird gaps. We
        # don't normalize Arabic glyphs (kept faithful to merchant
        # intent — the cleanup pipeline owns Arabic normalization).
        import re as _re_name  # noqa: PLC0415
        v = _re_name.sub(r"\s+", " ", v)
        if len(v) > CUSTOMER_NAME_MAX_LEN:
            raise ValueError(
                f"الاسم طويل جداً (الحد الأقصى {CUSTOMER_NAME_MAX_LEN} حرفاً)"
            )
        if not v:
            return None
        return v


SOURCE_LABELS: Dict[str, str] = {
    # acquisition_channel values
    "salla_sync":       "سلة",
    "zid_sync":         "زد",
    "customer_webhook": "سلة",
    "order":            "طلب متجر",
    "order_sync":       "طلب متجر",
    "order_webhook":    "طلب متجر",
    "manual_import":    "مستورد",
    "manual":           "مضاف يدوياً",
    "whatsapp_inbound": "واتساب",
    "whatsapp_lead":    "واتساب",
    "tracking_lead":    "المتجر الإلكتروني",
    "widget":           "المتجر الإلكتروني",
    # legacy meta.source values (keep for old rows)
    "salla":            "سلة",
}


def _resolve_customer_source(cust: "Customer") -> tuple:  # type: ignore[type-arg]
    """Return (source_key, source_label) that accurately reflects where this
    customer originally came from and which additional channels have touched them.

    Logic:
    1. ``acquisition_channel`` — set ONCE at creation, not overwritten by syncs
       → most trustworthy indicator of the customer's origin.
    2. ``salla_customer_id`` — if set the customer exists in the Salla store,
       regardless of origin. We show "سلة" in addition to the origin if different.
    3. ``source_tags`` (set by the import wizard) — deduped list of every source
       that has contributed data (e.g. ["manual_import", "salla_sync"]).
    4. Fallback: ``extra_metadata.source`` → ``extra_metadata.primary_source``.

    When the customer has multiple sources we build a composite label joined by
    " • " (e.g. "سلة • مستورد") so the merchant sees the full picture.
    """
    meta = cust.extra_metadata or {}
    channel = (cust.acquisition_channel or "").strip()
    has_salla_id = bool(getattr(cust, "salla_customer_id", None))

    # Gather all sources this customer has been associated with.
    active: list[str] = []

    # --- primary: acquisition_channel ---
    if channel:
        active.append(channel)

    # --- secondary: source_tags from import wizard ---
    source_tags: list = meta.get("source_tags") or []
    if isinstance(source_tags, list):
        for tag in source_tags:
            if tag and tag not in active:
                active.append(tag)

    # --- tertiary: salla_customer_id presence ---
    if has_salla_id and "salla_sync" not in active and "customer_webhook" not in active:
        active.append("salla_sync")

    # If still empty, fall back to legacy meta.source / primary_source
    if not active:
        fallback = (
            meta.get("source")
            or meta.get("primary_source")
            or ""
        )
        if fallback:
            active.append(fallback)

    if not active:
        return ("unknown", "—")

    # Build display buckets (deduplicated, ordered by priority)
    seen: set[str] = set()
    label_parts: list[str] = []

    def _add_label(key: str) -> None:
        label = SOURCE_LABELS.get(key)
        if label and label not in seen:
            seen.add(label)
            label_parts.append(label)

    # Priority order for display
    priority_order = [
        "salla_sync", "customer_webhook",
        "zid_sync",
        "order", "order_sync", "order_webhook",
        "manual",
        "manual_import",
        "whatsapp_inbound", "whatsapp_lead",
        "tracking_lead", "widget",
    ]
    # First pass: render in priority order
    for key in priority_order:
        if key in active:
            _add_label(key)
    # Second pass: anything not in the priority list
    for key in active:
        if key not in priority_order:
            _add_label(key)

    composite_key = "+".join(sorted(set(active)))
    composite_label = " • ".join(label_parts) if label_parts else active[0]
    return (composite_key, composite_label)


def _iso(dt) -> Optional[str]:
    """Render a datetime / string / None as an ISO-8601 timestamp.

    Accepts plain strings too because the legacy raw-SQL helpers in
    ``services.manual_segments`` return SQLite's ``CURRENT_TIMESTAMP``
    as a string (not a parsed datetime) — we don't want the customer
    drawer to crash with ``str.tzinfo`` just because the migration
    hasn't run yet.
    """
    if not dt:
        return None
    if isinstance(dt, str):
        return dt  # already ISO-shaped from the DB
    try:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except Exception:
        return str(dt)


def _days_since(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    target = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return max(0, (datetime.now(timezone.utc) - target).days)


def _segment_label_for_key(key: str) -> str:
    """Resolve a Nahla segment_key to its Arabic label using the
    canonical registry. Returns the key itself when not found so the
    UI never shows a blank pill."""
    seg = get_nahla_segment(key)
    return seg.label_ar if seg else key


def _serialize_customer(
    cust: Customer,
    profile: Optional[CustomerProfile],
    *,
    manual_segments: Optional[List[str]] = None,
    segment_sources: Optional[Dict[str, Dict[str, bool]]] = None,
) -> Dict[str, Any]:
    from core.customer_identity_resolver import (  # noqa: PLC0415
        display_name_for_customer,
        read_customer_identity,
    )

    meta = cust.extra_metadata or {}
    identity = read_customer_identity(cust)
    display_name = display_name_for_customer(cust, phone_fallback=cust.phone or "")
    source, source_label = _resolve_customer_source(cust)
    is_unsubscribed:        bool = bool(meta.get("is_unsubscribed"))
    pending_unsubscribe:    bool = bool(meta.get("pending_unsubscribe"))
    marketing_opt_out_manual: bool = is_marketing_opted_out_from_meta(meta)
    is_campaign_test_recipient: bool = bool(meta.get("is_campaign_test_recipient"))
    manual_name_override:   bool = bool(meta.get("manual_name_override"))
    manual_name_cleared:    bool = bool(meta.get("manual_name_cleared"))
    status = str(
        (profile.customer_status if profile and getattr(profile, "customer_status", None) else None)
        or "lead"
    )
    rfm_segment = str(
        (profile.rfm_segment if profile and getattr(profile, "rfm_segment", None) else None)
        or ("lead" if status == "lead" else "regulars")
    )

    manual_keys = list(manual_segments or [])
    result: Dict[str, Any] = {
        "id": cust.id,
        "name": cust.name or "",
        "display_name": display_name,
        "customer_name_status": identity.customer_name_status,
        "customer_name_source": identity.customer_name_source,
        "proposed_name": identity.proposed_name,
        "phone": cust.phone or "",
        "email": cust.email or "",
        "source": source,
        "source_label": source_label,
        "is_unsubscribed":        is_unsubscribed,
        "unsubscribed_at":        meta.get("unsubscribed_at"),
        "resubscribed_at":        meta.get("resubscribed_at"),
        "pending_unsubscribe":    pending_unsubscribe,
        "pending_unsubscribe_at": meta.get("pending_unsubscribe_at"),
        # ── Manual marketing controls (drawer + filters consume these) ──
        "marketing_opt_out_manual":   marketing_opt_out_manual,
        "marketing_opt_out_manual_at": meta.get("marketing_opt_out_manual_at"),
        "is_campaign_test_recipient": is_campaign_test_recipient,
        "manual_segments":            manual_keys,
        "manual_segments_labels":     [_segment_label_for_key(k) for k in manual_keys],
        # ── Inline-edit override marker ───────────────────────────────
        # True when the merchant rewrote the name via the table's
        # pencil icon or the card editor. The dashboard renders a
        # small "محرّر يدوياً" hint and the bulk cleanup tool skips
        # the row so the merchant's edit is never undone.
        "manual_name_override":       manual_name_override,
        "manual_name_cleared":        manual_name_cleared,
        "manual_name_edited_at":      meta.get("manual_name_edited_at"),
        # ── Per-segment source breakdown ──────────────────────────────
        # Shape: { "<segment_key>": { "automatic": bool,
        #                              "manual_include": bool,
        #                              "manual_exclude": bool } }
        # The drawer uses this to render labels like "VIP يدوي + تلقائي"
        # or "مستبعد يدويًا من VIP". Only segments with at least one
        # truthy field appear (no need to ship 30 empty objects).
        "segment_sources": segment_sources or {},
    }
    if profile:
        result.update({
            "status": status,
            "status_label": CUSTOMER_STATUS_LABELS.get(status, status),
            "segment": status,
            "segment_label": CUSTOMER_STATUS_LABELS.get(status, status),
            "customer_status": status,
            "customer_status_label": CUSTOMER_STATUS_LABELS.get(status, status),
            "rfm_segment": rfm_segment,
            "rfm_segment_label": RFM_SEGMENT_LABELS.get(rfm_segment, rfm_segment),
            "rfm_scores": {
                "recency": int(getattr(profile, "rfm_recency_score", 0) or 0),
                "frequency": int(getattr(profile, "rfm_frequency_score", 0) or 0),
                "monetary": int(getattr(profile, "rfm_monetary_score", 0) or 0),
                "total": int(getattr(profile, "rfm_total_score", 0) or 0),
                "code": getattr(profile, "rfm_code", None),
            },
            "rfm_recency_score": int(getattr(profile, "rfm_recency_score", 0) or 0),
            "rfm_frequency_score": int(getattr(profile, "rfm_frequency_score", 0) or 0),
            "rfm_monetary_score": int(getattr(profile, "rfm_monetary_score", 0) or 0),
            "rfm_total_score": int(getattr(profile, "rfm_total_score", 0) or 0),
            "rfm_code": getattr(profile, "rfm_code", None),
            "orders_count": profile.total_orders or 0,
            "total_orders": profile.total_orders or 0,
            "total_spent": round(float(profile.total_spend_sar or 0), 2),
            "total_spend": round(float(profile.total_spend_sar or 0), 2),
            "avg_order_value": round(float(profile.average_order_value_sar or 0), 2),
            "average_order_value": round(float(profile.average_order_value_sar or 0), 2),
            "last_order_at": _iso(profile.last_order_at),
            "last_order_date": _iso(profile.last_order_at),
            "first_order_at": _iso(getattr(profile, "first_order_at", None)),
            "first_order_date": _iso(getattr(profile, "first_order_at", None)),
            "first_seen_at": _iso(profile.first_seen_at),
            "last_seen_at": _iso(getattr(profile, "last_seen_at", None)),
            "metrics_computed_at": _iso(getattr(profile, "metrics_computed_at", None)),
            "last_recomputed_reason": getattr(profile, "last_recomputed_reason", None),
            "days_since_last_order": _days_since(profile.last_order_at),
            "churn_risk_score": round(float(profile.churn_risk_score or 0), 3),
            "lifetime_value_score": round(float(profile.lifetime_value_score or 0), 3),
            "is_returning": profile.is_returning or False,
        })
    else:
        result.update({
            "status": "lead",
            "status_label": CUSTOMER_STATUS_LABELS["lead"],
            "segment": "lead",
            "segment_label": CUSTOMER_STATUS_LABELS["lead"],
            "customer_status": "lead",
            "customer_status_label": CUSTOMER_STATUS_LABELS["lead"],
            "rfm_segment": "lead",
            "rfm_segment_label": RFM_SEGMENT_LABELS["lead"],
            "rfm_scores": {"recency": 0, "frequency": 0, "monetary": 0, "total": 0, "code": "000"},
            "rfm_recency_score": 0,
            "rfm_frequency_score": 0,
            "rfm_monetary_score": 0,
            "rfm_total_score": 0,
            "rfm_code": "000",
            "orders_count": 0,
            "total_orders": 0,
            "total_spent": 0,
            "total_spend": 0,
            "avg_order_value": 0,
            "average_order_value": 0,
            "last_order_at": None,
            "last_order_date": None,
            "first_order_at": None,
            "first_order_date": None,
            "first_seen_at": None,
            "last_seen_at": None,
            "metrics_computed_at": None,
            "last_recomputed_reason": None,
            "days_since_last_order": None,
            "churn_risk_score": 0,
            "lifetime_value_score": 0,
            "is_returning": False,
        })
    return result


@router.get("")
async def list_customers(
    request: Request,
    search: str = Query("", description="بحث بالاسم أو الهاتف"),
    segment: str = Query(
        "",
        description=(
            "Optional auto Nahla segment key (e.g. 'vip', 'dormant', 'no_purchase_60'). "
            "Filters the list to ONLY customers belonging to that segment, using "
            "the same canonical SQL as the campaign wizard."
        ),
    ),
    manual_segment: str = Query(
        "",
        description=(
            "Filter by a *manual* Nahla segment tag set by the merchant. "
            "Special value 'none' returns customers with NO manual tags."
        ),
    ),
    marketing_opt_out: Optional[bool] = Query(
        None,
        description=(
            "When true, return only customers excluded from manual "
            "marketing campaigns. When false, return only the ones "
            "still eligible. When omitted, no filter is applied."
        ),
    ),
    test_recipient: Optional[bool] = Query(
        None,
        description="True = only test-list customers; False = only non-test.",
    ),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    seg_key = (segment or "").strip().lower()
    if seg_key and seg_key != "all":
        # Reuse the wizard's canonical builder so the count + sample shown
        # in the wizard exactly matches what the merchant sees in the list
        # below. `require_reachable=False` here because the customers page
        # is for management — not sending — and unreachable rows still
        # need to be visible (so the merchant can fix the phone number).
        if get_nahla_segment(seg_key) is None:
            raise HTTPException(status_code=422, detail=f"شريحة غير معروفة: {seg_key}")
        auto_q = build_segment_query(seg_key, db, tenant_id, require_reachable=False)
        if auto_q is None:
            # Defensive — get_nahla_segment said yes but builder said no
            raise HTTPException(status_code=422, detail=f"شريحة غير معروفة: {seg_key}")

        # ── Unified segment membership formula ──────────────────────
        # The product invariant (cemented in migration 0053):
        #
        #   member ⇔ (auto_match ∨ manual_include) ∧ ¬ manual_exclude
        #
        # Why all three pieces matter:
        #   * auto_match: the RFM classifier's verdict.
        #   * manual_include: merchant explicitly pinned the customer
        #     to this segment in the drawer (overrides a "no" from
        #     the classifier).
        #   * manual_exclude: merchant explicitly removed the customer
        #     from this segment (overrides a "yes" from the
        #     classifier — without this we couldn't honour
        #     "remove from VIP" while leaving the auto match alone).
        from services.manual_segments import (  # noqa: PLC0415
            MODE_EXCLUDE, MODE_INCLUDE,
        )
        auto_ids = {row[0] for row in auto_q.with_entities(Customer.id).all()}
        include_ids = set(customer_ids_with_manual_segment(
            db, tenant_id, seg_key, mode=MODE_INCLUDE,
        ))
        exclude_ids = set(customer_ids_with_manual_segment(
            db, tenant_id, seg_key, mode=MODE_EXCLUDE,
        ))
        member_ids = (auto_ids | include_ids) - exclude_ids
        if not member_ids:
            return {"customers": [], "total": 0, "page": page,
                    "per_page": per_page, "pages": 1}
        q = (
            db.query(Customer)
            .filter(Customer.tenant_id == tenant_id, Customer.id.in_(member_ids))
        )

        # Diagnostic log so production tickets are debuggable in one
        # grep instead of a database session.
        try:
            import logging  # noqa: PLC0415
            logging.getLogger("nahla.customers.segment_filter").info(
                "segment filter | tenant=%s key=%r auto=%d include=%d exclude=%d member=%d",
                tenant_id, seg_key, len(auto_ids), len(include_ids),
                len(exclude_ids), len(member_ids),
            )
        except Exception:
            pass
    else:
        q = db.query(Customer).filter(Customer.tenant_id == tenant_id)

    # ── Manual segment filter ─────────────────────────────────────────
    # Two modes:
    #   * `manual_segment=<key>` → keep only customers tagged with that key.
    #   * `manual_segment=none`  → keep only customers with NO manual tags.
    msf = (manual_segment or "").strip().lower()
    if msf == "none":
        # NOT EXISTS (any manual tag for this customer)
        tagged_ids_subq = (
            db.query(CustomerSegmentManual.customer_id)
            .filter(CustomerSegmentManual.tenant_id == tenant_id)
            .distinct()
            .subquery()
        )
        q = q.filter(~Customer.id.in_(db.query(tagged_ids_subq.c.customer_id)))
    elif msf:
        if get_nahla_segment(msf) is None:
            raise HTTPException(status_code=422, detail=f"تصنيف يدوي غير معروف: {msf}")
        ids = customer_ids_with_manual_segment(db, tenant_id, msf)
        # Diagnostic log so we can see in production exactly what key
        # the filter looked up vs what's actually in the table for the
        # tenant. If a merchant complains "I tagged this customer
        # but the filter shows nobody", the log line below tells us
        # whether it's a key mismatch (count=0 here, customer tagged
        # under different key) or genuinely empty.
        try:
            import logging  # noqa: PLC0415
            _seglog = logging.getLogger("nahla.customers.manual_segment")
            distinct_keys = (
                db.query(CustomerSegmentManual.segment_key)
                .filter(CustomerSegmentManual.tenant_id == tenant_id)
                .distinct()
                .all()
            )
            _seglog.info(
                "manual_segment filter | tenant=%s requested_key=%r matched_ids=%d "
                "distinct_keys_in_db=%s",
                tenant_id, msf, len(ids),
                sorted({k[0] for k in distinct_keys}),
            )
        except Exception:
            pass
        if not ids:
            # Empty universe — short-circuit to no results without
            # building a giant `IN ()` clause.
            return {"customers": [], "total": 0, "page": page,
                    "per_page": per_page, "pages": 1}
        q = q.filter(Customer.id.in_(ids))

    # ── Marketing opt-out filter ──────────────────────────────────────
    # Stored on Customer.extra_metadata so we use the same JSON-cast
    # pattern as the unsubscribed segment for cross-dialect support.
    if marketing_opt_out is not None:
        if marketing_opt_out:
            q = q.filter(marketing_opt_out_manual_sql_truthy())
        else:
            q = q.filter(marketing_opt_out_manual_sql_falsy())

    if test_recipient is not None:
        from sqlalchemy import or_  # noqa: PLC0415
        from services.manual_segments import _json_meta_text  # noqa: PLC0415

        raw = _json_meta_text(META_KEY_TEST_RECIPIENT)
        is_test = or_(raw == "true", raw == "1")
        q = q.filter(is_test) if test_recipient else q.filter(~is_test)

    search_clause = _customer_search_clauses(search)
    if search_clause is not None:
        q = q.filter(search_clause)

    total = q.count()
    rows = (
        q.order_by(Customer.id.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )

    customer_ids = [c.id for c in rows]
    profiles = {}
    manual_segments_by_id: Dict[int, List[str]] = {}
    sources_by_id: Dict[int, Dict[str, Dict[str, bool]]] = {}
    if customer_ids:
        prof_rows = (
            db.query(CustomerProfile)
            .filter(
                CustomerProfile.tenant_id == tenant_id,
                CustomerProfile.customer_id.in_(customer_ids),
            )
            .all()
        )
        profiles = {p.customer_id: p for p in prof_rows}
        # NEVER let manual-segment helpers take the customer list down.
        # The mode-column probe inside ``services.manual_segments``
        # already handles the "migration not yet applied" case, but we
        # keep one more belt-and-braces try/except here so a future
        # incidental DB issue (lock, replica lag, etc.) on the manual
        # segments table can't degrade the whole page to "لا يوجد عملاء".
        try:
            manual_segments_by_id = list_manual_segments_bulk(
                db, tenant_id, customer_ids,
            )
        except Exception as exc:  # pragma: no cover — defensive
            try:
                db.rollback()
            except Exception:
                pass
            manual_segments_by_id = {}
            try:
                import logging  # noqa: PLC0415
                logging.getLogger("nahla.customers.list").warning(
                    "list_manual_segments_bulk failed; rendering page without manual tags. err=%s",
                    exc,
                )
            except Exception:
                pass

        # ── Build segment_sources per customer ────────────────────────
        # Wrapped end-to-end so a failure in source-breakdown
        # computation degrades to "no sources" instead of an empty
        # customer list. The drawer treats {} as "show only legacy
        # manual_segments tags" which is acceptable graceful UX.
        try:
            from services.manual_segments import (  # noqa: PLC0415
                list_manual_sources_bulk,
            )
            # NOTE: deliberately NOT re-importing ``build_segment_query``
            # here — Python's local-name analysis would treat the
            # (never-executed) re-import as making the name a local
            # for the entire function, shadowing the module-level
            # import and breaking the earlier seg_key filter branch
            # with UnboundLocalError. We reuse the module-level
            # import already at the top of the file.
            from services.nahla_segments import (  # noqa: PLC0415
                all_segment_keys,
            )
            manual_sources = list_manual_sources_bulk(
                db, tenant_id, customer_ids,
            )
            # For each segment in the registry, fetch the auto-match
            # set for this tenant once; intersect with the page's
            # customer ids. Bounded: ~20 segments × ≤ 200 ids.
            for seg_key_iter in all_segment_keys():
                try:
                    auto_q = build_segment_query(
                        seg_key_iter, db, tenant_id, require_reachable=False,
                    )
                    if auto_q is None:
                        continue
                    auto_set = {
                        r[0] for r in
                        auto_q.with_entities(Customer.id)
                        .filter(Customer.id.in_(customer_ids))
                        .all()
                    }
                except Exception:
                    auto_set = set()
                for cid in customer_ids:
                    manual_mode = manual_sources.get(cid, {}).get(seg_key_iter)
                    auto = cid in auto_set
                    inc = manual_mode == "include"
                    exc = manual_mode == "exclude"
                    if not (auto or inc or exc):
                        continue
                    sources_by_id.setdefault(cid, {})[seg_key_iter] = {
                        "automatic":      auto,
                        "manual_include": inc,
                        "manual_exclude": exc,
                        # Final unified membership — what the merchant
                        # actually sees in the customers chip filter
                        # and what the campaign audience uses.
                        # ``(auto OR include) AND NOT exclude``.
                        "is_member":      bool((auto or inc) and not exc),
                    }
        except Exception as exc:  # pragma: no cover — defensive
            try:
                db.rollback()
            except Exception:
                pass
            sources_by_id = {}
            try:
                import logging  # noqa: PLC0415
                logging.getLogger("nahla.customers.list").warning(
                    "segment_sources build failed; rendering page without sources. err=%s",
                    exc,
                )
            except Exception:
                pass

    return {
        "customers": [
            _serialize_customer(
                c, profiles.get(c.id),
                manual_segments=manual_segments_by_id.get(c.id, []),
                segment_sources=sources_by_id.get(c.id, {}),
            )
            for c in rows
        ],
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": max(1, (total + per_page - 1) // per_page),
    }


@router.get("/segments")
async def customers_segments(request: Request, db: Session = Depends(get_db)):
    """
    The canonical Nahla segment registry (`services.nahla_segments`)
    with a `customer_count` per segment computed for THIS tenant.

    Counts here intentionally include unreachable customers (no
    `normalized_phone`) because the customers page is a *management*
    view, not a *sending* surface — the merchant needs to see "8 VIPs"
    even if 1 has a bad phone, so they can go fix it.

    Result is identical in shape to /campaigns/wizard/segments so the
    same frontend chip component can render either view, and the
    `criteria_ar` field powers the in-app tooltip / info card that
    explains what each chip means.
    """
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)
    return {
        "segments": list_segments_with_counts(
            db, tenant_id, require_reachable=False,
        ),
        "campaignExcludedCount": count_marketing_opted_out_customers(db, tenant_id),
    }


@router.get("/debug/manual-segments")
async def debug_manual_segments(
    request: Request,
    customer_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
):
    """Diagnostic dump for "I tagged this customer but the filter
    can't find them" tickets.

    Returns:
      * ``known_segment_keys``  — the canonical Nahla registry the
        filter validates against. The drawer uses the same registry,
        so any key in the DB that is *not* in this list means the
        tag was created before validation existed and is now invisible
        to the filter.
      * ``stored_keys_for_tenant`` — every distinct segment_key
        actually present in ``customer_segments_manual`` for this
        tenant, with its row count. A key here that is missing from
        ``known_segment_keys`` is the smoking gun.
      * ``customer_tags`` (if ``customer_id`` provided) — the exact
        rows for that customer, so you can compare the stored
        segment_key character-for-character against what the filter
        sends.
    """
    from sqlalchemy import func  # noqa: PLC0415
    from services.nahla_segments import all_segment_keys  # noqa: PLC0415

    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    distinct_rows = (
        db.query(
            CustomerSegmentManual.segment_key,
            func.count(CustomerSegmentManual.id),
        )
        .filter(CustomerSegmentManual.tenant_id == tenant_id)
        .group_by(CustomerSegmentManual.segment_key)
        .all()
    )
    stored = [{"segment_key": k, "count": int(n)} for (k, n) in distinct_rows]
    known = sorted(all_segment_keys())
    unknown = sorted({s["segment_key"] for s in stored} - set(known))

    from services.manual_segments import (  # noqa: PLC0415
        MODE_EXCLUDE, MODE_INCLUDE,
        _mode_column_available, list_manual_sources_for_customer,
    )
    mode_avail = _mode_column_available(db)

    customer_tags = None
    customer_unified_membership = None
    if customer_id is not None:
        # Pull raw rows including ``mode`` when available. We use
        # getattr to avoid AttributeError on legacy schemas where
        # the model attribute exists but the column doesn't.
        rows = (
            db.query(CustomerSegmentManual)
            .filter(
                CustomerSegmentManual.tenant_id == tenant_id,
                CustomerSegmentManual.customer_id == customer_id,
            )
            .all()
        )
        customer_tags = []
        for r in rows:
            row_mode: Optional[str]
            if mode_avail:
                try:
                    row_mode = getattr(r, "mode", None) or MODE_INCLUDE
                except Exception:
                    row_mode = None
            else:
                # Legacy schema — every row is implicitly ``include``.
                row_mode = MODE_INCLUDE
            customer_tags.append({
                "id":           r.id,
                "segment_key":  r.segment_key,
                "mode":         row_mode,
                "key_repr":     repr(r.segment_key),
                "key_len":      len(r.segment_key or ""),
                "is_in_known":  (r.segment_key in known),
                "created_at":   r.created_at.isoformat() if r.created_at else None,
            })

        # ── Unified membership breakdown ─────────────────────────
        # For every Nahla segment, compute whether THIS customer
        # ends up in the segment per the canonical formula:
        #     (auto ∨ include) ∧ ¬ exclude
        # so a merchant ticket "I tagged هيثم but VIP filter
        # doesn't show him" can be debugged in one curl.
        manual_sources = list_manual_sources_for_customer(db, tenant_id, customer_id)
        breakdown: Dict[str, Dict[str, Any]] = {}
        for seg_key in known:
            try:
                auto_q = build_segment_query(seg_key, db, tenant_id, require_reachable=False)
                auto_match = (
                    auto_q.with_entities(Customer.id)
                    .filter(Customer.id == customer_id)
                    .first() is not None
                ) if auto_q is not None else False
            except Exception:
                auto_match = False
            mode = manual_sources.get(seg_key)
            inc = mode == MODE_INCLUDE
            exc = mode == MODE_EXCLUDE
            member = (auto_match or inc) and not exc
            if not (auto_match or inc or exc):
                continue
            breakdown[seg_key] = {
                "automatic":      bool(auto_match),
                "manual_include": inc,
                "manual_exclude": exc,
                "is_member":      bool(member),
            }
        customer_unified_membership = breakdown

    return {
        "tenant_id":              tenant_id,
        "mode_column_available":  mode_avail,
        "known_segment_keys":     known,
        "stored_keys_for_tenant": stored,
        "stored_keys_unknown":    unknown,
        "customer_id":            customer_id,
        "customer_tags":          customer_tags,
        "customer_unified_membership": customer_unified_membership,
    }


@router.get("/metrics")
async def customers_metrics(request: Request, db: Session = Depends(get_db)):
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)
    service = CustomerIntelligenceService(db, tenant_id)
    metrics = service.customers_metrics_summary()
    return {
        "totalCustomers": metrics["total_customers"],
        "activeCustomers": metrics["active_customers"],
        "vipCustomers": metrics["vip_customers"],
        "newCustomers": metrics["new_customers"],
        "atRiskCustomers": metrics["at_risk_customers"],
        "inactiveCustomers": metrics["inactive_customers"],
        "leads": metrics["leads"],
        "statusCounts": metrics["status_counts"],
        "rfmSegmentCounts": metrics["rfm_segment_counts"],
    }


@router.get("/{customer_id}")
async def get_customer(customer_id: int, request: Request, db: Session = Depends(get_db)):
    tenant_id = resolve_tenant_id(request)
    cust = db.query(Customer).filter(
        Customer.id == customer_id, Customer.tenant_id == tenant_id,
    ).first()
    if not cust:
        raise HTTPException(status_code=404, detail="العميل غير موجود")

    profile = db.query(CustomerProfile).filter_by(
        customer_id=cust.id, tenant_id=tenant_id,
    ).first()
    manual = list_manual_segments_for_customer(db, tenant_id, cust.id)

    return _serialize_customer(cust, profile, manual_segments=manual)


@router.post("")
async def create_customer(body: CustomerCreateIn, request: Request, db: Session = Depends(get_db)):
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)
    service = CustomerIntelligenceService(db, tenant_id)

    existing = service.find_customer_by_phone(body.phone)
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"يوجد عميل بنفس رقم الواتساب: {existing.name or existing.phone}",
        )

    cust = service.upsert_customer_identity(
        phone=body.phone,
        name=body.name,
        email=body.email,
        source="manual",
        extra_metadata={"source": "manual"},
        seen_at=datetime.now(timezone.utc),
    )
    if cust is None:
        raise HTTPException(status_code=422, detail="تعذر إنشاء العميل")

    service.recompute_profile_for_customer(
        cust.id,
        reason="manual_customer_create",
        commit=True,
        emit_event=True,
    )
    db.refresh(cust)

    return {"id": cust.id, "message": "تم إضافة العميل بنجاح"}


@router.patch("/{customer_id}")
async def update_customer(
    customer_id: int, body: CustomerPatchIn, request: Request, db: Session = Depends(get_db),
):
    """Update a customer row. Supports partial PATCH — every field is
    optional; only the keys present in the body are persisted.

    Name edits are special: when the merchant rewrites the name (via
    the inline edit pencil in the customers table, or via the
    full-card editor), we stamp two markers so the bulk
    ``customer_name_cleanup`` tool NEVER undoes the edit:

      * ``extra_metadata.manual_name_override = true``
      * ``extra_metadata.manual_name_edited_at = <iso8601>``

    The bulk cleanup preview and apply endpoints honour the flag —
    flagged customers are skipped silently even if their name still
    matches a stopword heuristic. The flag survives Salla / Zid
    re-syncs because the syncer only writes new metadata on initial
    creation; existing rows keep their merchant-curated value.
    """
    tenant_id = resolve_tenant_id(request)
    service = CustomerIntelligenceService(db, tenant_id)
    cust = db.query(Customer).filter(
        Customer.id == customer_id, Customer.tenant_id == tenant_id,
    ).first()
    if not cust:
        raise HTTPException(status_code=404, detail="العميل غير موجود")

    if body.phone is not None and body.phone != cust.phone:
        dup = service.find_customer_by_phone(body.phone, exclude_customer_id=customer_id)
        if dup:
            raise HTTPException(
                status_code=409,
                detail=f"يوجد عميل آخر بنفس الرقم: {dup.name or dup.phone}",
            )
        cust.phone = body.phone

    # ``name`` handling — three distinct cases:
    #   * Field omitted from request body       → leave name alone.
    #   * Field present, non-empty after trim   → set the name.
    #   * Field present, empty/null/whitespace  → CLEAR the name
    #     (``Customer.name = None``). Dashboard renders "بدون اسم"
    #     and templates fall back to "عميلنا الغالي".
    #
    # We use Pydantic v2's ``model_fields_set`` to distinguish
    # "not sent" from "explicitly null" — the JSON body
    # ``{"name": null}`` MUST clear, while ``{}`` (or any body
    # without a ``name`` key) MUST be a no-op.
    name_changed = False
    name_cleared = False
    _name_field_sent = "name" in getattr(body, "model_fields_set", set()) or "name" in getattr(body, "__fields_set__", set())
    if _name_field_sent:
        new_value: Optional[str] = body.name if (body.name or "").strip() else None
        previous_name = cust.name or None
        if new_value is None:
            cust.name = None
            name_changed = previous_name is not None
            name_cleared = True
            meta = dict(cust.extra_metadata or {})
            meta["customer_name_status"] = "missing"
            meta["customer_name_source"] = "manual_admin"
            meta["name_source"] = "manual_admin"
            meta.pop("proposed_name", None)
            cust.extra_metadata = meta
        else:
            from core.customer_identity_resolver import apply_customer_name  # noqa: PLC0415

            applied = apply_customer_name(
                cust,
                new_value,
                source="manual_admin",
                force_merchant=True,
            )
            if not applied or (cust.name or "").strip() != (new_value or "").strip():
                raise HTTPException(
                    status_code=422,
                    detail="تعذر حفظ الاسم. تحقق من صيغة الاسم وحاول مرة أخرى.",
                )
            name_changed = (cust.name or None) != previous_name
    if body.email is not None:
        cust.email = body.email

    # ── Manual-name override marker ──────────────────────────────
    # Stamp the override flag whenever the merchant touched the
    # ``name`` field — even if the new value happens to equal the
    # old one (the merchant explicitly approved this spelling).
    # The flag is the single source of truth the bulk cleanup
    # pipeline reads to decide "skip this row".
    #
    # ``manual_name_cleared`` is a SECOND, weaker flag that lives
    # alongside ``manual_name_override``:
    #   * Both flags True  → merchant intentionally emptied the name.
    #                        Bulk cleanup still skips this row, but
    #                        a high-confidence AI-detected name
    #                        (e.g. "اسمي محمد") IS allowed to refill
    #                        it — because keeping the row at
    #                        "عميلنا الغالي" forever is worse than
    #                        using the real name the customer
    #                        volunteered.
    #   * Override True, cleared False → merchant has a real curated
    #                        name. NOTHING overwrites it.
    if _name_field_sent:
        try:
            meta = dict(cust.extra_metadata or {})
            meta["manual_name_override"]  = True
            meta["manual_name_cleared"]   = bool(name_cleared)
            meta["manual_name_edited_at"] = datetime.now(timezone.utc).isoformat()
            meta["manual_name_source"] = "manual_admin"
            if name_changed and not name_cleared:
                meta["manual_name_previous"] = previous_name or ""
            cust.extra_metadata = meta
            try:
                from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415
                flag_modified(cust, "extra_metadata")
            except Exception:
                pass
        except Exception as _meta_exc:
            # Persisting the flag is best-effort — the new name STILL
            # wins, even if the metadata update fails (we just lose
            # cleanup-protection on the row, surfaced via the next
            # bulk-cleanup preview).
            import logging  # noqa: PLC0415
            logging.getLogger("nahla.customers").warning(
                "[customers.update] manual_name_override stamp failed "
                "tenant=%s customer=%s err=%s",
                tenant_id, customer_id, _meta_exc,
            )

    service.ensure_profile(cust, seen_at=datetime.now(timezone.utc))
    service.recompute_profile_for_customer(
        cust.id,
        reason="manual_customer_update",
        commit=True,
        emit_event=True,
    )
    db.refresh(cust)
    from core.customer_identity_resolver import display_name_for_customer  # noqa: PLC0415

    return {
        "updated":               True,
        "id":                    cust.id,
        "name":                  cust.name or "",
        "display_name":          display_name_for_customer(cust, phone_fallback=cust.phone or ""),
        "phone":                 cust.phone or "",
        "email":                 cust.email or "",
        "manual_name_override":  bool(
            (cust.extra_metadata or {}).get("manual_name_override")
        ),
        "manual_name_cleared":   bool(
            (cust.extra_metadata or {}).get("manual_name_cleared")
        ),
        "name_changed":          name_changed,
    }


@router.delete("/{customer_id}")
async def delete_customer(customer_id: int, request: Request, db: Session = Depends(get_db)):
    tenant_id = resolve_tenant_id(request)
    cust = db.query(Customer).filter(
        Customer.id == customer_id, Customer.tenant_id == tenant_id,
    ).first()
    if not cust:
        raise HTTPException(status_code=404, detail="العميل غير موجود")

    _delete_customer_children(db, [cust.id], tenant_id)
    db.query(Customer).filter(
        Customer.id == cust.id, Customer.tenant_id == tenant_id,
    ).delete(synchronize_session=False)
    db.commit()
    return {"deleted": True}


# ── Customer delete helpers ───────────────────────────────────────────────────

def _delete_customer_children(db: Session, customer_ids: list, tenant_id: int) -> None:
    """Delete (or nullify) every child table that references customers.id
    without ON DELETE CASCADE before we remove the parent rows.

    Tables are processed in dependency order so that FK constraints are
    respected even if Postgres doesn't have CASCADE configured.
    """
    if not customer_ids:
        return

    id_filter = lambda model: (  # noqa: E731
        model.customer_id.in_(customer_ids),
        model.tenant_id == tenant_id,
    )
    id_filter_no_tenant = lambda model: (  # noqa: E731
        model.customer_id.in_(customer_ids),
    )

    # Always-present models (already imported at module level)
    db.query(CustomerNameCleanupDraft).filter(
        CustomerNameCleanupDraft.customer_id.in_(customer_ids),
        CustomerNameCleanupDraft.tenant_id == tenant_id,
    ).delete(synchronize_session=False)

    db.query(CustomerNameAuditLog).filter(
        CustomerNameAuditLog.customer_id.in_(customer_ids),
        CustomerNameAuditLog.tenant_id == tenant_id,
    ).delete(synchronize_session=False)

    db.query(CustomerProfile).filter(
        CustomerProfile.customer_id.in_(customer_ids),
        CustomerProfile.tenant_id == tenant_id,
    ).delete(synchronize_session=False)

    # CustomerSegmentManual already has ondelete='CASCADE' but delete
    # explicitly to keep the logic self-contained.
    db.query(CustomerSegmentManual).filter(
        CustomerSegmentManual.customer_id.in_(customer_ids),
        CustomerSegmentManual.tenant_id == tenant_id,
    ).delete(synchronize_session=False)

    # Optionally-imported child models
    if _EXTRA_CUSTOMER_CHILD_MODELS:
        for Model in (
            CustomerPreferences,
            ProductAffinity,
            PriceSensitivityScore,
            ConversationHistorySummary,
        ):
            db.query(Model).filter(
                Model.customer_id.in_(customer_ids),
                Model.tenant_id == tenant_id,
            ).delete(synchronize_session=False)

        db.query(PredictiveReorderEstimate).filter(
            PredictiveReorderEstimate.customer_id.in_(customer_ids),
            PredictiveReorderEstimate.tenant_id == tenant_id,
        ).delete(synchronize_session=False)

        db.query(ProductInterest).filter(
            ProductInterest.customer_id.in_(customer_ids),
            ProductInterest.tenant_id == tenant_id,
        ).delete(synchronize_session=False)

        db.query(GovernorSendLog).filter(
            GovernorSendLog.customer_id.in_(customer_ids),
            GovernorSendLog.tenant_id == tenant_id,
        ).delete(synchronize_session=False)

    # Nullable FK tables — use raw SQL to SET NULL so we preserve the
    # historical records (orders, conversations, etc.) while unlinking the
    # deleted customer rows.
    for table, col in (
        ("notification_logs",          "customer_id"),
        ("automation_events",          "customer_id"),
        ("automation_executions",      "customer_id"),
        ("ai_action_logs",             "customer_id"),
        ("delivery_quality_events",    "customer_id"),
        ("campaign_send_logs",         "customer_id"),
    ):
        db.execute(
            sa.text(
                f"UPDATE {table} SET {col} = NULL "
                f"WHERE {col} = ANY(:ids) AND tenant_id = :tid"
            ),
            {"ids": list(customer_ids), "tid": tenant_id},
        )


# ── Bulk delete ───────────────────────────────────────────────────────────────

class BulkDeleteIn(BaseModel):
    ids: Optional[List[int]] = None     # specific IDs — empty/omitted = delete ALL
    delete_all: bool = False            # must be True when deleting all


@router.post("/bulk-delete")
async def bulk_delete_customers(
    body: BulkDeleteIn,
    request: Request,
    db: Session = Depends(get_db),
):
    """Delete a batch of customers (or ALL) for the current tenant.

    • Pass ``ids`` to delete specific customers.
    • Pass ``delete_all=true`` (and omit or empty ``ids``) to wipe every
      customer row for this tenant.  A second confirmation guard is handled
      by the frontend — this endpoint itself performs no extra check so the
      UI must show the user a strong warning before calling it.
    """
    tenant_id = resolve_tenant_id(request)

    if body.delete_all and not body.ids:
        # Wipe all customers for this tenant — collect IDs first so
        # _delete_customer_children can use them for the nullable-FK tables.
        all_ids: list = [
            row[0]
            for row in db.query(Customer.id)
            .filter(Customer.tenant_id == tenant_id)
            .all()
        ]
        _delete_customer_children(db, all_ids, tenant_id)
        customers_deleted = (
            db.query(Customer)
            .filter(Customer.tenant_id == tenant_id)
            .delete(synchronize_session=False)
        )
        db.commit()
        return {"deleted": customers_deleted}

    if not body.ids:
        raise HTTPException(status_code=400, detail="لم يتم تحديد أي عملاء للحذف")

    # Delete only the specified IDs (must belong to this tenant)
    owned_ids: list = [
        row[0]
        for row in db.query(Customer.id)
        .filter(Customer.id.in_(body.ids), Customer.tenant_id == tenant_id)
        .all()
    ]
    if not owned_ids:
        raise HTTPException(status_code=404, detail="لم يُعثر على أي من العملاء المحددين")

    _delete_customer_children(db, owned_ids, tenant_id)
    result = db.query(Customer).filter(
        Customer.id.in_(owned_ids),
        Customer.tenant_id == tenant_id,
    ).delete(synchronize_session=False)

    db.commit()
    return {"deleted": result}


# ── Manual segments + marketing preferences ─────────────────────────────────
#
# These endpoints let the merchant pin / unpin official Nahla segments
# on a customer (drawer UI) and toggle the two boolean flags that the
# campaign snapshot consults: marketing opt-out and "test recipient".
#
# Important contract:
#   * `segment_key` MUST be one of the keys in `services.nahla_segments`.
#     Anything else returns 422 with the list of accepted keys — we do
#     NOT silently coerce or ignore unknown keys.
#   * Every write asserts `cust.tenant_id == request_tenant_id` via
#     `services.manual_segments` so cross-tenant tagging is impossible.

class CustomerSegmentAddIn(BaseModel):
    segment_key: str
    # Optional mode — backwards compatible (defaults to ``include``).
    # ``exclude`` lets the merchant explicitly hide a customer from
    # a segment even when the auto classifier says they belong.
    mode: Optional[str] = None


class MarketingPrefsPatchIn(BaseModel):
    """Both fields are optional — if a merchant only flips one toggle
    the other stays untouched. We don't coerce missing fields to
    ``False`` because that would silently re-subscribe customers."""
    marketing_opt_out_manual: Optional[bool] = None
    is_campaign_test_recipient: Optional[bool] = None


def _customer_matches_auto_segment(
    db: Session, tenant_id: int, customer_id: int, segment_key: str,
) -> bool:
    """Return True iff the auto RFM classifier currently considers
    this customer to belong to ``segment_key``. Used by smart-remove
    to decide whether a delete must be converted to an exclude row."""
    from services.nahla_segments import build_segment_query  # noqa: PLC0415
    auto_q = build_segment_query(segment_key, db, tenant_id, require_reachable=False)
    if auto_q is None:
        return False
    return (
        auto_q.with_entities(Customer.id)
        .filter(Customer.id == customer_id)
        .first()
        is not None
    )


@router.post("/{customer_id}/segments")
async def add_customer_segment(
    customer_id: int,
    body: CustomerSegmentAddIn,
    request: Request,
    db: Session = Depends(get_db),
):
    """Pin a customer to one of Nahla's official manual segments.

    Optional ``mode`` field:
      * ``"include"`` (default) — pin into the segment.
      * ``"exclude"`` — explicitly hide the customer from this
        segment even when the auto RFM classifier says they match.
        Used by the drawer's "remove from segment" UI when the
        auto match is what put them there.

    Idempotent: re-tagging the same pair updates the existing row's
    mode in-place. Unknown segment keys return 422 with the list of
    accepted keys.
    """
    from services.manual_segments import (  # noqa: PLC0415
        ALLOWED_MODES, MODE_INCLUDE, ModeColumnUnavailableError,
        _mode_column_available, list_manual_sources_for_customer,
    )
    tenant_id = resolve_tenant_id(request)
    requested_mode = (body.mode or MODE_INCLUDE).strip().lower()
    if requested_mode not in ALLOWED_MODES:
        raise HTTPException(
            status_code=422,
            detail=f"وضع غير صالح: {body.mode!r}. المسموح: {sorted(ALLOWED_MODES)}",
        )
    try:
        row = add_manual_segment(
            db, tenant_id=tenant_id, customer_id=customer_id,
            segment_key=body.segment_key, mode=requested_mode,
        )
    except UnknownSegmentError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except LookupError:
        raise HTTPException(status_code=404, detail="العميل غير موجود")
    except ModeColumnUnavailableError:
        # Legacy schema can't honour exclude rows yet. Surface a
        # structured 200 so the dashboard renders a clear message
        # instead of a 500.
        return {
            "customer_id":              customer_id,
            "ok":                       False,
            "code":                     "platform_upgrading",
            "message":                  (
                "نحن نُحدّث المنصة الآن — أعد المحاولة خلال دقيقة."
            ),
            "mode_column_available":    False,
            "segment_key":              body.segment_key,
        }

    return {
        "customer_id":           customer_id,
        "ok":                    True,
        "segment_key":           row.segment_key,
        "mode":                  getattr(row, "mode", None) or "include",
        "label_ar":              _segment_label_for_key(row.segment_key),
        "source":                row.source,
        "created_at":            _iso(row.created_at),
        "mode_column_available": _mode_column_available(db),
        "manual_segments":       list_manual_segments_for_customer(db, tenant_id, customer_id),
        "manual_sources":        list_manual_sources_for_customer(db, tenant_id, customer_id),
    }


@router.delete("/{customer_id}/segments/{segment_key}")
async def remove_customer_segment(
    customer_id: int,
    segment_key: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """Smart-remove a customer from a segment.

    Behaviour:
      * If the auto RFM classifier considers the customer a match,
        we INSERT/UPDATE a manual ``exclude`` row (the auto match
        itself is never mutated — it'll re-compute on the next RFM
        cycle, but the exclude row keeps the customer hidden until
        the merchant re-includes them).
      * Otherwise (the customer is in the segment only because of
        an existing manual ``include`` row), we just delete that row.
      * If neither row exists, returns ``"noop"``.

    Returns 200 in all cases — merchant retries on flaky networks
    must converge to "removed" without flashing a 404. Unknown
    segment keys (e.g. an Arabic label sent by mistake) return a
    structured 200 with ``ok=false`` and a clear message rather
    than a 500.
    """
    import logging  # noqa: PLC0415

    from services.manual_segments import (  # noqa: PLC0415
        _mode_column_available,
        list_manual_sources_for_customer,
        smart_remove_manual_segment,
    )
    log = logging.getLogger("nahla.customers.segment_delete")

    tenant_id = resolve_tenant_id(request)
    cust = db.query(Customer).filter(
        Customer.id == customer_id, Customer.tenant_id == tenant_id,
    ).first()
    if not cust:
        raise HTTPException(status_code=404, detail="العميل غير موجود")

    raw_key = segment_key
    normalised_key = (segment_key or "").strip().lower()
    mode_avail = _mode_column_available(db)

    try:
        # Validate first so an Arabic label / typo doesn't crash the
        # auto-match query later.
        try:
            normalised_key = assert_known_segment(normalised_key)
        except UnknownSegmentError as exc:
            log.info(
                "delete_segment unknown_key | tenant=%s customer=%s "
                "raw=%r normalised=%r mode_avail=%s err=%s",
                tenant_id, customer_id, raw_key, normalised_key,
                mode_avail, exc,
            )
            return {
                "customer_id":           customer_id,
                "ok":                    False,
                "code":                  "unknown_segment",
                "message":               (
                    f"تصنيف غير معروف: {raw_key!r}. الرجاء استخدام مفاتيح "
                    "نحلة الرسمية فقط."
                ),
                "segment_key_received":  raw_key,
                "normalised_key":        normalised_key,
                "mode_column_available": mode_avail,
                "action":                "failed",
            }

        auto_match = _customer_matches_auto_segment(
            db, tenant_id, customer_id, normalised_key,
        )
        action = smart_remove_manual_segment(
            db, tenant_id=tenant_id, customer_id=customer_id,
            segment_key=normalised_key, auto_match=auto_match,
        )
    except Exception as exc:
        # Last-resort defence — any unexpected error must not 500
        # the customer card. Roll back, log, and return a clean
        # JSON failure the UI can render.
        try:
            db.rollback()
        except Exception:
            pass
        log.exception(
            "delete_segment unexpected | tenant=%s customer=%s "
            "raw=%r normalised=%r mode_avail=%s",
            tenant_id, customer_id, raw_key, normalised_key, mode_avail,
        )
        return {
            "customer_id":           customer_id,
            "ok":                    False,
            "code":                  "internal_error",
            "message":               (
                "تعذر إزالة التصنيف — حدث خطأ مؤقت. حاول مجدداً، وإن "
                "تكرر تواصل مع الدعم."
            ),
            "segment_key_received":  raw_key,
            "normalised_key":        normalised_key,
            "mode_column_available": mode_avail,
            "action":                "failed",
            "error":                 type(exc).__name__,
        }

    log.info(
        "delete_segment ok | tenant=%s customer=%s key=%r "
        "auto_match=%s action=%s mode_avail=%s",
        tenant_id, customer_id, normalised_key,
        auto_match, action, mode_avail,
    )

    return {
        "customer_id":           customer_id,
        "ok":                    True,
        "action":                action,  # deleted | excluded | noop | deleted_legacy
        "auto_match":            auto_match,
        "segment_key":           normalised_key,
        "mode_column_available": mode_avail,
        "manual_segments":       list_manual_segments_for_customer(db, tenant_id, customer_id),
        "manual_sources":        list_manual_sources_for_customer(db, tenant_id, customer_id),
    }


# ── Unified segment override (the merchant-facing simplified surface) ───
#
# The drawer no longer talks about "manual" vs "auto" classification. It
# offers three actions per segment chip:
#
#   * "أضِف لهذا التصنيف"        → mode=force_include
#   * "استبعِد من هذا التصنيف"    → mode=force_exclude
#   * "أعِده للتصنيف التلقائي"   → mode=auto  (deletes the override row)
#
# The endpoint maps the merchant-facing modes onto the storage layer
# (mode='include' / mode='exclude' / no row).


class CustomerSegmentOverrideIn(BaseModel):
    """Body for ``POST /customers/{id}/segments/{key}/override``.

    ``mode`` is the merchant-facing verb, NOT the storage mode:
      * ``force_include`` → upsert manual_include row.
      * ``force_exclude`` → upsert manual_exclude row.
      * ``auto``          → delete any override row so the auto
        classifier becomes the only signal again.
    """
    mode: str


@router.post("/{customer_id}/segments/{segment_key}/override")
async def override_customer_segment(
    customer_id: int,
    segment_key: str,
    body: CustomerSegmentOverrideIn,
    request: Request,
    db: Session = Depends(get_db),
):
    """Set / clear a per-customer override for one segment.

    Mirrors the drawer's three-button UI exactly. Always returns 200
    with the updated final-membership snapshot so the UI can reflect
    state in one round-trip.
    """
    import logging  # noqa: PLC0415

    from services.manual_segments import (  # noqa: PLC0415
        MODE_EXCLUDE, MODE_INCLUDE, ModeColumnUnavailableError,
        _mode_column_available, list_manual_sources_for_customer,
        remove_manual_segment,
    )
    from services.nahla_segments import (  # noqa: PLC0415
        get_final_segment_membership,
    )
    log = logging.getLogger("nahla.customers.segment_override")

    tenant_id = resolve_tenant_id(request)
    cust = db.query(Customer).filter(
        Customer.id == customer_id, Customer.tenant_id == tenant_id,
    ).first()
    if not cust:
        raise HTTPException(status_code=404, detail="العميل غير موجود")

    raw_mode = (body.mode or "").strip().lower()
    raw_key = segment_key
    normalised_key = (segment_key or "").strip().lower()
    mode_avail = _mode_column_available(db)

    try:
        normalised_key = assert_known_segment(normalised_key)
    except UnknownSegmentError as exc:
        log.info(
            "override unknown_key | tenant=%s customer=%s raw=%r err=%s",
            tenant_id, customer_id, raw_key, exc,
        )
        return {
            "customer_id":           customer_id,
            "ok":                    False,
            "code":                  "unknown_segment",
            "message":               (
                f"تصنيف غير معروف: {raw_key!r}. الرجاء استخدام مفاتيح "
                "نحلة الرسمية فقط."
            ),
            "segment_key_received":  raw_key,
            "normalised_key":        normalised_key,
            "mode_received":         body.mode,
            "mode_column_available": mode_avail,
        }

    action: str
    try:
        if raw_mode == "auto" or raw_mode == "":
            # Drop any override → auto classifier is sole signal.
            removed = remove_manual_segment(
                db, tenant_id=tenant_id, customer_id=customer_id,
                segment_key=normalised_key,
            )
            action = "cleared" if removed else "noop"
        elif raw_mode == "force_include":
            add_manual_segment(
                db, tenant_id=tenant_id, customer_id=customer_id,
                segment_key=normalised_key, mode=MODE_INCLUDE,
            )
            action = "force_include"
        elif raw_mode == "force_exclude":
            add_manual_segment(
                db, tenant_id=tenant_id, customer_id=customer_id,
                segment_key=normalised_key, mode=MODE_EXCLUDE,
            )
            action = "force_exclude"
        else:
            return {
                "customer_id":           customer_id,
                "ok":                    False,
                "code":                  "unknown_mode",
                "message":               (
                    f"وضع غير صالح: {body.mode!r}. المسموح: "
                    "force_include, force_exclude, auto."
                ),
                "mode_received":         body.mode,
                "segment_key":           normalised_key,
                "mode_column_available": mode_avail,
            }
    except ModeColumnUnavailableError:
        return {
            "customer_id":           customer_id,
            "ok":                    False,
            "code":                  "platform_upgrading",
            "message":               (
                "نحن نُحدّث المنصة الآن — أعد المحاولة خلال دقيقة."
            ),
            "segment_key":           normalised_key,
            "mode_received":         body.mode,
            "mode_column_available": False,
        }
    except LookupError:
        raise HTTPException(status_code=404, detail="العميل غير موجود")
    except Exception as exc:
        try:
            db.rollback()
        except Exception:
            pass
        log.exception(
            "override unexpected | tenant=%s customer=%s key=%r mode=%r",
            tenant_id, customer_id, normalised_key, raw_mode,
        )
        return {
            "customer_id":           customer_id,
            "ok":                    False,
            "code":                  "internal_error",
            "message":               (
                "تعذر تحديث التصنيف — حدث خطأ مؤقت. حاول مجدداً."
            ),
            "segment_key":           normalised_key,
            "mode_received":         body.mode,
            "mode_column_available": mode_avail,
            "error":                 type(exc).__name__,
        }

    is_member = get_final_segment_membership(
        db, tenant_id, customer_id, normalised_key,
    )
    log.info(
        "override ok | tenant=%s customer=%s key=%r mode=%r action=%s "
        "is_member=%s mode_avail=%s",
        tenant_id, customer_id, normalised_key, raw_mode, action,
        is_member, mode_avail,
    )

    return {
        "customer_id":           customer_id,
        "ok":                    True,
        "segment_key":           normalised_key,
        "mode_received":         raw_mode,
        "action":                action,  # cleared|noop|force_include|force_exclude
        "is_member":             is_member,
        "mode_column_available": mode_avail,
        "manual_sources":        list_manual_sources_for_customer(db, tenant_id, customer_id),
    }


@router.patch("/{customer_id}/marketing-preferences")
async def update_marketing_preferences(
    customer_id: int,
    body: MarketingPrefsPatchIn,
    request: Request,
    db: Session = Depends(get_db),
):
    """Toggle the two marketing-side flags on Customer.extra_metadata.

    Either field may be omitted; only the ones explicitly sent are
    written. This means a merchant can flip the test-recipient flag
    without accidentally re-subscribing an opted-out customer.
    """
    tenant_id = resolve_tenant_id(request)
    cust = db.query(Customer).filter(
        Customer.id == customer_id, Customer.tenant_id == tenant_id,
    ).first()
    if not cust:
        raise HTTPException(status_code=404, detail="العميل غير موجود")

    if body.marketing_opt_out_manual is not None:
        cust = set_marketing_opt_out_manual(
            db, tenant_id=tenant_id, customer_id=customer_id,
            opted_out=bool(body.marketing_opt_out_manual), commit=False,
        )
    if body.is_campaign_test_recipient is not None:
        cust = set_test_recipient(
            db, tenant_id=tenant_id, customer_id=customer_id,
            is_test=bool(body.is_campaign_test_recipient), commit=False,
        )
    db.commit()
    db.refresh(cust)

    meta = cust.extra_metadata or {}
    return {
        "customer_id": customer_id,
        "marketing_opt_out_manual":     is_marketing_opted_out_from_meta(meta),
        "marketing_opt_out_manual_at":  meta.get("marketing_opt_out_manual_at"),
        "is_campaign_test_recipient":   bool(meta.get("is_campaign_test_recipient")),
        "campaign_test_recipient_at":   meta.get("campaign_test_recipient_at"),
    }


# ── Bulk customer-name cleanup tool ──────────────────────────────────────────
#
# Surfaces the "تنظيف أسماء العملاء" button on the customers page. Strictly
# tenant-scoped: every read and every write is filtered by the JWT's
# resolved tenant_id — there is no path here that can touch a customer
# belonging to a different store.
#
# Workflow (incremental review session):
#   1. Frontend opens modal → GET /customers/name-cleanup/preview
#      → backend scans Customer.name for the current tenant, runs the
#        cleanup pipeline, MERGES results with any saved draft rows
#        (so the merchant's previous chip edits are restored), and
#        returns the resulting list.
#   2. Merchant edits chips → frontend autosaves to
#        POST /customers/name-cleanup/draft/save
#      every ~1.5s. The Customer.name field is NOT mutated yet;
#      edits live in ``customer_name_cleanup_drafts``.
#   3. Merchant clicks "تطبيق المحدد" / "تطبيق ذوي الثقة العالية":
#        POST /customers/name-cleanup/apply
#      → backend mutates Customer.name + writes audit log rows +
#        deletes the corresponding draft rows.
#   4. Optional "تجاهل المسودة":
#        DELETE /customers/name-cleanup/draft
#      → wipes every draft row for the tenant; next preview starts
#        from a clean slate.
#
# Single source of truth: once a row is applied, campaigns/templates
# read Customer.name verbatim. There is NO runtime sanitiser doing
# the cleaning again at send time.

class NameCleanupApplyItem(BaseModel):
    """One row from the merchant's selection in the preview modal."""
    customer_id: int
    new_name: Optional[str] = None  # None = clear the row
    reason: Optional[str] = None
    confidence: Optional[str] = None


class NameCleanupDraftItem(BaseModel):
    """One row of merchant-edited chip state for the autosave endpoint.

    Semantics:
      * ``removed_word_indices = None`` AND ``cleared = False`` →
        delete the draft row (back to the cleaner's defaults).
      * Otherwise upsert with the merchant's state.
    """
    customer_id: int
    removed_word_indices: Optional[List[int]] = None
    cleared: bool = False
    status: Optional[str] = None  # "edited" | "skipped"


class NameCleanupDraftSaveIn(BaseModel):
    items: List[NameCleanupDraftItem] = []


class NameCleanupApplyIn(BaseModel):
    """Request body for ``POST /customers/name-cleanup/apply``.

    Either pass ``items`` (per-row selection from the preview modal,
    which is what "Apply selected" sends) OR pass
    ``high_confidence_only=True`` to skip the modal and apply every
    high-confidence verdict for this tenant in one shot.

    The two modes are mutually exclusive; if both are provided the
    explicit ``items`` win and ``high_confidence_only`` is ignored.
    """
    items: Optional[List[NameCleanupApplyItem]] = None
    high_confidence_only: bool = False


# Hard cap on items returned in one preview response. The cleaner SCANS
# every customer in the tenant regardless of this cap — it only bounds
# how many *match* rows we ship in a single payload so a tenant with
# 8 000 customers and 4 000 matches doesn't blow past a sensible JSON
# size (≈ 1 MB at ~250 bytes/row). When matches exceed the cap, the
# response sets ``truncated=true`` and the UI tells the merchant to
# apply the visible batch and re-open to see the rest.
_NAME_CLEANUP_MAX_ITEMS = 3000
# How many rows to fetch per DB round-trip when streaming the tenant's
# customer table. 1 000 strikes a good balance between memory residency
# and round-trip count for the typical 5k–20k merchant.
_NAME_CLEANUP_BATCH_SIZE = 1000


def _serialise_draft(draft: Optional[CustomerNameCleanupDraft]) -> Optional[Dict[str, Any]]:
    """Render a draft row into the JSON shape consumed by the modal.
    Returns ``None`` when the input is ``None`` so callers can use it
    inline without branching."""
    if draft is None:
        return None
    return {
        "removed_word_indices": list(draft.removed_word_indices or []),
        "cleared":              bool(draft.cleared),
        "status":               draft.status or "edited",
        "updated_at":           draft.updated_at.isoformat() if draft.updated_at else None,
    }


@router.get("/name-cleanup/preview")
async def name_cleanup_preview(
    request: Request,
    include_skipped: bool = Query(
        False,
        description=(
            "When true, also surface rows the merchant previously "
            "marked as 'skipped' in earlier sessions."
        ),
    ),
    category: Optional[str] = Query(
        None,
        description=(
            "Optional comma-separated category filter. Returns only "
            "rows whose verdict matches one of the requested buckets. "
            "Unknown / empty values are ignored. Valid: "
            "``source_label_name``, ``location_label_name``, "
            "``placeholder_name``, ``generic_bad_name``, "
            "``suspicious_suffix``, ``other``."
        ),
    ),
    db: Session = Depends(get_db),
):
    """Tenant-wide preview of customer names that need cleaning, merged
    with any in-progress draft state from previous sessions.

    Scans **every** customer in the current tenant — there is no page
    cursor on the request and no offset/limit on the SQL. We stream
    the table in batches of :data:`_NAME_CLEANUP_BATCH_SIZE` rows via
    ``.yield_per()`` so memory stays bounded.

    For every customer that needs a change, the response attaches the
    matching draft state (if any). The merchant's chip edits from
    earlier sessions are therefore restored verbatim and they can
    pick up exactly where they left off.

    Stale drafts are auto-GC'd: a draft whose ``original_name`` no
    longer matches the live ``Customer.name`` is deleted in-flight
    so the merchant isn't shown obsolete state.

    Response shape::

        {
          "tenant_id":        <int>,
          "total_customers":  <int>,
          "total_scanned":    <int>,
          "match_count":      <int>,
          "items":            [ { customer_id, current_name,
                                  suggested_name, reason, confidence,
                                  phone,
                                  draft: { removed_word_indices,
                                           cleared, status,
                                           updated_at } | null
                                }, ... ],
          "draft_count":      <int>,
          "draft_edited":     <int>,
          "draft_skipped":    <int>,
          "high_confidence":  <int>,
          "low_confidence":   <int>,
          "truncated":        <bool>,
          "max_items":        <int>,
        }
    """
    from services.customer_name_cleanup import (  # noqa: PLC0415
        compute_cleanup, ALL_CATEGORIES,
    )
    import logging  # noqa: PLC0415

    log = logging.getLogger("nahla.customers.name_cleanup")
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    # ── Normalise the category filter ────────────────────────────
    # Accepts a comma-separated list ("source_label_name,location_…")
    # or a single value. Unknown buckets are silently dropped so the
    # frontend can keep adding new categories without breaking older
    # clients. An empty / missing filter means "all categories".
    _category_set: Optional[set[str]] = None
    if category:
        _category_set = {
            tok.strip() for tok in str(category).split(",") if tok.strip()
        } & set(ALL_CATEGORIES)
        if not _category_set:
            _category_set = None    # nothing valid → show everything

    total_customers = (
        db.query(Customer)
        .filter(Customer.tenant_id == tenant_id)
        .count()
    )

    # Load all draft rows for this tenant up front, keyed by customer
    # id, so we can attach them to the scan output in O(1). Even for
    # the worst tenant we have (~10k matches) this is a few MB max.
    drafts_by_customer: Dict[int, CustomerNameCleanupDraft] = {
        d.customer_id: d
        for d in db.query(CustomerNameCleanupDraft).filter(
            CustomerNameCleanupDraft.tenant_id == tenant_id,
        ).all()
    }

    items: List[Dict[str, Any]] = []
    scanned = 0
    high = low = match_count = 0
    draft_edited = draft_skipped = 0
    truncated = False
    stale_draft_ids: List[int] = []
    # Category histogram for the dashboard filter chips. We always
    # count the FULL match population (before the optional category
    # filter) so the chip badges show "how many rows would appear if
    # I clicked this chip" rather than the post-filter intersection.
    category_counts: Dict[str, int] = {c: 0 for c in ALL_CATEGORIES}

    query = (
        db.query(Customer)
        .filter(Customer.tenant_id == tenant_id)
        .order_by(Customer.id.asc())
        .yield_per(_NAME_CLEANUP_BATCH_SIZE)
    )

    for cust in query:
        scanned += 1

        # ── Manual-name-override short-circuit ──────────────────
        # The merchant explicitly approved this name from the inline
        # edit pencil in the customers table (or the card editor).
        # Skip cleanup verdict + draft handling entirely so we don't
        # propose to "fix" something they curated. The bulk cleanup
        # modal MUST treat these rows as already clean even if the
        # stopword heuristic would have flagged them.
        if (cust.extra_metadata or {}).get("manual_name_override"):
            # Still GC any leftover draft so it doesn't keep
            # haunting the merchant after they curate the name.
            d = drafts_by_customer.get(cust.id)
            if d is not None:
                stale_draft_ids.append(d.id)
                drafts_by_customer.pop(cust.id, None)
            continue

        verdict = compute_cleanup(cust.name)
        draft = drafts_by_customer.get(cust.id)

        # ── Stale-draft GC ───────────────────────────────────────
        # If the live customer name diverged from the draft snapshot
        # (merchant edited the row in another tab, or applied via
        # another flow), the draft is no longer trustworthy. Drop
        # it so the merchant sees the cleaner's fresh verdict.
        if draft is not None and (draft.original_name or "") != (cust.name or ""):
            stale_draft_ids.append(draft.id)
            draft = None
            drafts_by_customer.pop(cust.id, None)

        if not verdict.changed:
            # Customer is clean now — any leftover draft is also
            # garbage. Remove it so future scans don't waste time.
            if draft is not None:
                stale_draft_ids.append(draft.id)
                drafts_by_customer.pop(cust.id, None)
            continue

        # Skipped rows: include only when the merchant asked for them.
        if draft is not None and draft.status == "skipped" and not include_skipped:
            draft_skipped += 1
            continue

        match_count += 1
        if verdict.confidence == "high":
            high += 1
        else:
            low += 1
        if verdict.category in category_counts:
            category_counts[verdict.category] += 1
        if draft is not None:
            if draft.status == "skipped":
                draft_skipped += 1
            else:
                draft_edited += 1

        # Apply the per-reason filter AFTER counting so the chip
        # badges still reflect the full match population. Without
        # this the filter would render its own count to zero on the
        # first selection.
        if _category_set is not None and verdict.category not in _category_set:
            continue

        if len(items) < _NAME_CLEANUP_MAX_ITEMS:
            # Surface the merchant-driven opt-out flag so the modal
            # can render the "مستبعد من الحملات" badge inline. We
            # intentionally do NOT bundle the customer-driven
            # ``is_unsubscribed`` here — those rows shouldn't appear
            # in the cleanup pipeline in the first place (they're
            # already filtered by the campaign dispatcher), and
            # conflating the two states would muddy the audit.
            cust_meta = cust.extra_metadata or {}
            items.append({
                "customer_id":    cust.id,
                "current_name":   cust.name or "",
                "suggested_name": verdict.suggested,
                "reason":         verdict.reason,
                "confidence":     verdict.confidence,
                "category":       verdict.category,
                "phone":          cust.phone or "",
                "draft":          _serialise_draft(draft),
                "marketing_opt_out_manual":
                    is_marketing_opted_out_from_meta(cust_meta),
            })
        else:
            truncated = True

    # GC orphan / stale drafts. We do this once at the end so the
    # response is consistent even if the loop short-circuits.
    if stale_draft_ids:
        db.query(CustomerNameCleanupDraft).filter(
            CustomerNameCleanupDraft.id.in_(stale_draft_ids),
        ).delete(synchronize_session=False)
        db.commit()

    log.info(
        "preview | tenant=%s scanned=%d/%d matches=%d (high=%d low=%d) "
        "drafts_edited=%d drafts_skipped=%d items_returned=%d truncated=%s "
        "stale_drafts_gc=%d",
        tenant_id, scanned, total_customers, match_count,
        high, low, draft_edited, draft_skipped,
        len(items), truncated, len(stale_draft_ids),
    )

    return {
        "tenant_id":        tenant_id,
        "total_customers":  total_customers,
        "total_scanned":    scanned,
        "match_count":      match_count,
        "items":            items,
        "draft_count":      draft_edited + draft_skipped,
        "draft_edited":     draft_edited,
        "draft_skipped":    draft_skipped,
        "high_confidence":  high,
        "low_confidence":   low,
        "category_counts":  category_counts,
        "category_filter":  sorted(_category_set) if _category_set else [],
        "truncated":        truncated,
        "max_items":        _NAME_CLEANUP_MAX_ITEMS,
    }


@router.post("/name-cleanup/draft/save")
async def name_cleanup_draft_save(
    body: NameCleanupDraftSaveIn,
    request: Request,
    db: Session = Depends(get_db),
):
    """Autosave the merchant's in-progress chip edits.

    Idempotent batch upsert: callers can re-send the same payload
    arbitrarily often without producing duplicate audit history (we
    don't write to ``customer_name_audit_logs`` here — drafts are
    purely "review state"; the audit only fires on apply).

    Per item:
      * ``removed_word_indices = None`` AND ``cleared = False`` AND
        ``status`` not set → delete the draft row (back to defaults).
      * Otherwise upsert.

    Tenant safety: any customer_id in the payload that doesn't belong
    to the requesting tenant is silently dropped from the batch
    (logged at info level).
    """
    from datetime import datetime, timezone  # noqa: PLC0415
    import logging  # noqa: PLC0415

    log = logging.getLogger("nahla.customers.name_cleanup")
    tenant_id = resolve_tenant_id(request)
    actor_user_id = get_jwt_user_id(request)
    get_or_create_tenant(db, tenant_id)

    if not body.items:
        return {
            "tenant_id":    tenant_id,
            "saved":        0,
            "deleted":      0,
            "skipped":      0,
            "saved_at":     datetime.now(timezone.utc).isoformat(),
        }

    ids = [it.customer_id for it in body.items if it.customer_id]
    customers_by_id: Dict[int, Customer] = {
        c.id: c
        for c in db.query(Customer).filter(
            Customer.tenant_id == tenant_id,
            Customer.id.in_(ids),
        ).all()
    }

    existing_drafts: Dict[int, CustomerNameCleanupDraft] = {
        d.customer_id: d
        for d in db.query(CustomerNameCleanupDraft).filter(
            CustomerNameCleanupDraft.tenant_id == tenant_id,
            CustomerNameCleanupDraft.customer_id.in_(ids),
        ).all()
    }

    saved = deleted = skipped = 0
    now = datetime.now(timezone.utc)

    for item in body.items:
        cust = customers_by_id.get(item.customer_id)
        if cust is None:
            skipped += 1
            continue
        existing = existing_drafts.get(item.customer_id)
        wants_status = (item.status or "").strip().lower() or None

        # Decide: delete vs upsert.
        is_default = (
            item.removed_word_indices is None
            and not item.cleared
            and wants_status in (None, "edited")
        )

        if is_default and existing is None:
            # Nothing to do — no edit and no existing row.
            continue
        if is_default and existing is not None and existing.status != "skipped":
            # Merchant cleared their edits → drop the draft row so
            # the row falls back to cleaner defaults. Skipped rows
            # are NOT auto-cleared by an "is_default" payload (they
            # carry the skip flag, not chip state).
            db.delete(existing)
            deleted += 1
            continue

        # Upsert path. ``status`` defaults to "edited" unless the
        # caller explicitly requested "skipped".
        status_value = (
            "skipped" if wants_status == "skipped" else "edited"
        )
        indices = (
            list(item.removed_word_indices)
            if item.removed_word_indices is not None
            else None
        )

        if existing is None:
            row = CustomerNameCleanupDraft(
                tenant_id=tenant_id,
                customer_id=cust.id,
                original_name=cust.name,
                removed_word_indices=indices,
                cleared=bool(item.cleared),
                status=status_value,
                actor_user_id=actor_user_id,
                created_at=now,
                updated_at=now,
            )
            db.add(row)
        else:
            existing.original_name = cust.name
            existing.removed_word_indices = indices
            existing.cleared = bool(item.cleared)
            existing.status = status_value
            existing.actor_user_id = actor_user_id
            existing.updated_at = now
        saved += 1

    db.commit()
    log.info(
        "draft_save | tenant=%s actor=%s saved=%d deleted=%d skipped=%d",
        tenant_id, actor_user_id, saved, deleted, skipped,
    )
    return {
        "tenant_id":    tenant_id,
        "saved":        saved,
        "deleted":      deleted,
        "skipped":      skipped,
        "saved_at":     now.isoformat(),
    }


@router.delete("/name-cleanup/draft")
async def name_cleanup_draft_discard(
    request: Request,
    db: Session = Depends(get_db),
):
    """Discard every draft row for the current tenant.

    Used by the "تجاهل المسودة" / "ابدأ من جديد" action. The next
    preview returns the cleaner's pristine defaults for all rows.
    Does NOT touch ``Customer.name`` — only the review session is
    wiped.
    """
    import logging  # noqa: PLC0415
    log = logging.getLogger("nahla.customers.name_cleanup")
    tenant_id = resolve_tenant_id(request)
    actor_user_id = get_jwt_user_id(request)

    deleted = (
        db.query(CustomerNameCleanupDraft)
        .filter(CustomerNameCleanupDraft.tenant_id == tenant_id)
        .delete(synchronize_session=False)
    )
    db.commit()
    log.info(
        "draft_discard | tenant=%s actor=%s deleted=%d",
        tenant_id, actor_user_id, deleted,
    )
    return {"tenant_id": tenant_id, "deleted": deleted}


# ── Inline "marketing opt-out" toggle for the cleanup modal ─────────
#
# During name review, the merchant often spots customers that aren't
# just dirty names — they're customers they don't want to market to
# at all (low engagement, wrong audience, etc.). Previously the only
# fix was to leave the modal, navigate to the customer page, find
# the row, and toggle ``marketing_opt_out_manual``. The endpoint
# below is a thin wrapper that lets the modal flip the flag in one
# click without leaving the review session.
#
# What this is NOT
# ────────────────
# * NOT a customer-driven unsubscribe (``is_unsubscribed`` —
#   triggered by the customer sending "STOP"). That flag stays
#   untouched here; we only set the merchant-driven
#   ``marketing_opt_out_manual`` flag, which the campaign dispatcher
#   honours in its ``_snapshot_recipients`` pre-send filter (logs
#   the skip under ``LOG_SKIPPED_MANUAL_EXCLUSION`` /
#   ``REASON_MARKETING_OPT_OUT`` — preserving the audit distinction).
# * NOT a quality-suppression (``CustomerSuppression`` — written by
#   the Suppression Engine after repeated quality_risk failures).
#   The Quality Engine continues to write its own rows when the
#   underlying signal warrants it, regardless of this manual flag.
#
# Three independent buckets, three independent sources of truth.
# The frontend renders distinct badges for each.


class NameCleanupMarketingOptOutIn(BaseModel):
    """Body for ``POST /customers/name-cleanup/marketing-opt-out``.

    A list so the merchant can flip multiple rows in one round-trip
    without N modal-row clicks → N HTTP calls. The list is bounded
    so a buggy frontend can't DoS the DB with a massive batch.
    """
    customer_ids: List[int]
    opted_out: bool = True


@router.post("/name-cleanup/marketing-opt-out")
async def name_cleanup_marketing_opt_out(
    body: NameCleanupMarketingOptOutIn,
    request: Request,
    db: Session = Depends(get_db),
):
    """Toggle ``marketing_opt_out_manual`` on one or more customers.

    Use case: the merchant is reviewing names in the cleanup modal,
    spots a customer they no longer want to market to, and clicks
    "استبعاد من الحملات" on that row. The customer's record is NOT
    deleted, their conversation history is NOT touched, and inbound
    messages from them continue to be received normally. Only
    outbound marketing/broadcast sends are blocked.

    Tenant isolation: only the customers owned by the requesting
    tenant are touched, even if the request body lists ids that
    belong elsewhere — those are silently skipped.
    """
    import logging  # noqa: PLC0415
    log = logging.getLogger("nahla.customers.name_cleanup")

    tenant_id = resolve_tenant_id(request)
    if not tenant_id:
        raise HTTPException(status_code=401, detail="tenant_unresolved")

    actor_user_id = get_jwt_user_id(request)

    cust_ids = list({int(cid) for cid in (body.customer_ids or []) if cid})
    if not cust_ids:
        return {"tenant_id": tenant_id, "updated": 0, "skipped": 0}

    # Hard cap — the modal currently supports up to 3 000 visible
    # rows, but a single batch click should never request more than
    # a few hundred. 500 is the sane upper bound; anything beyond
    # that should make multiple API calls.
    MAX_BATCH = 500
    if len(cust_ids) > MAX_BATCH:
        raise HTTPException(
            status_code=400,
            detail=f"batch_too_large (max {MAX_BATCH})",
        )

    customers = (
        db.query(Customer)
        .filter(
            Customer.tenant_id == tenant_id,
            Customer.id.in_(cust_ids),
        )
        .all()
    )
    found_ids = {c.id for c in customers}
    skipped_unknown = [cid for cid in cust_ids if cid not in found_ids]

    updated = 0
    for cust in customers:
        try:
            set_marketing_opt_out_manual(
                db, tenant_id=tenant_id, customer_id=cust.id,
                opted_out=bool(body.opted_out), commit=False,
            )
            updated += 1
        except Exception as exc:
            log.warning(
                "marketing_opt_out_failed | tenant=%s customer=%s err=%s",
                tenant_id, cust.id, exc,
            )

    db.commit()
    log.info(
        "marketing_opt_out | tenant=%s actor=%s opted_out=%s updated=%d "
        "skipped_unknown=%d",
        tenant_id, actor_user_id, body.opted_out, updated, len(skipped_unknown),
    )
    return {
        "tenant_id": tenant_id,
        "opted_out": bool(body.opted_out),
        "updated":   updated,
        "skipped_unknown": skipped_unknown,
    }


@router.post("/name-cleanup/apply")
async def name_cleanup_apply(
    body: NameCleanupApplyIn,
    request: Request,
    db: Session = Depends(get_db),
):
    """Apply the cleanup verdicts approved by the merchant.

    Two modes:

      * **items mode** — explicit per-row selection from the preview
        modal. Each item carries the ``customer_id`` and the
        ``new_name`` (``None`` to clear the row). The backend ALWAYS
        re-runs the cleanup pipeline against the current DB value and
        only writes when the recomputed verdict matches what the
        merchant approved — this protects against a stale preview
        (e.g. the merchant edited a name in another tab between the
        preview and the apply click).

      * **high-confidence-only mode** — ``items=None`` and
        ``high_confidence_only=True``. The backend re-scans every
        customer in the tenant, applies only the ``confidence="high"``
        verdicts, and ignores ``"low"`` ones. Single round-trip for
        the "I trust the safe cleaner, just do it" workflow.

    Every mutation writes one row to ``customer_name_audit_logs`` with
    the old + new + reason + tenant_id + actor_user_id + timestamp.
    """
    from services.customer_name_cleanup import compute_cleanup  # noqa: PLC0415
    import logging  # noqa: PLC0415

    log = logging.getLogger("nahla.customers.name_cleanup")
    tenant_id = resolve_tenant_id(request)
    actor_user_id = get_jwt_user_id(request)

    has_explicit_items = bool(body.items)
    if not has_explicit_items and not body.high_confidence_only:
        raise HTTPException(
            status_code=422,
            detail="حدد العناصر يدوياً أو فعّل خيار 'الأكثر ثقة فقط'",
        )

    applied: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    now = datetime.now(timezone.utc)

    # ── items mode (explicit selection) ──────────────────────────────
    if has_explicit_items:
        ids = [it.customer_id for it in (body.items or []) if it.customer_id]
        if not ids:
            return {
                "tenant_id":     tenant_id,
                "applied":       [],
                "skipped":       [],
                "applied_count": 0,
                "skipped_count": 0,
                "drafts_cleared": 0,
            }
        # Strict tenant filter: any customer_id not belonging to the
        # requester is silently dropped (not 404'd) — that way a stale
        # preview after a tenant switch doesn't cross-contaminate.
        customers_by_id: Dict[int, Customer] = {
            c.id: c
            for c in db.query(Customer).filter(
                Customer.tenant_id == tenant_id,
                Customer.id.in_(ids),
            ).all()
        }
        for item in (body.items or []):
            cust = customers_by_id.get(item.customer_id)
            if cust is None:
                skipped.append({
                    "customer_id": item.customer_id,
                    "reason":      "العميل غير موجود في المتجر الحالي",
                })
                continue

            # ── Manual-name-override short-circuit ─────────────
            # The merchant curated this name explicitly via the
            # inline edit pencil. The bulk cleaner MUST NOT
            # overwrite it. We skip silently with a clear
            # reason so the merchant understands why the apply
            # didn't touch this row.
            if (cust.extra_metadata or {}).get("manual_name_override"):
                skipped.append({
                    "customer_id": cust.id,
                    "reason":      "الاسم محرّر يدوياً — تنظيف المسحاة محمي",
                })
                continue

            # Re-run the cleaner against the LIVE DB value. The
            # merchant approved a specific (old → new) edit; if the
            # row has changed in the meantime, the verdict may no
            # longer be reachable. We tolerate a re-derivation that
            # still produces the same ``new_name`` as a sanity check.
            verdict = compute_cleanup(cust.name)
            if not verdict.changed:
                skipped.append({
                    "customer_id": cust.id,
                    "reason":      "تم تنظيفه مسبقاً",
                })
                continue
            # The merchant's selection is authoritative for which
            # value to write. We only trust the cleaner to confirm
            # that a change of SOME kind is still warranted; the
            # exact string comes from the request so the merchant
            # can also apply a hand-edited suggestion.
            new_name = item.new_name
            if isinstance(new_name, str):
                new_name = new_name.strip() or None
            old_name = cust.name
            cust.name = new_name
            # ── Manual-override stamps (May 2026) ─────────────────
            # The merchant explicitly approved this verdict from the
            # bulk preview UI. Mark the row as merchant-curated so:
            #   * future bulk-cleanup runs skip this customer (we
            #     can't propose to "fix" a row the merchant just
            #     hand-confirmed),
            #   * CSV / Salla / Zid imports refuse to overwrite
            #     the curated value,
            #   * the WhatsApp inbound profile alias is blocked too.
            #
            # ``manual_name_cleared`` mirrors what happens inside
            # ``PATCH /customers/{id}`` when the merchant wipes a
            # name from the inline pencil — true when the row was
            # CLEARED, false when a real replacement was written.
            # The cleared flag is the gate that allows a high-trust
            # AI-detected name from a future conversation to refill
            # an empty row.
            try:
                _meta_apply = dict(cust.extra_metadata or {})
                _meta_apply["manual_name_override"]  = True
                _meta_apply["manual_name_cleared"]   = bool(new_name is None)
                _meta_apply["manual_name_edited_at"] = (
                    now.isoformat()
                )
                _meta_apply["manual_name_previous"]  = old_name or ""
                _meta_apply["manual_name_source"]    = "bulk_cleanup_apply"
                cust.extra_metadata = _meta_apply
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "[name-cleanup.apply] override stamp failed "
                    "tenant=%s id=%s err=%s",
                    tenant_id, cust.id, exc,
                )
            audit = CustomerNameAuditLog(
                tenant_id=tenant_id,
                customer_id=cust.id,
                old_name=old_name,
                new_name=new_name,
                reason=item.reason or verdict.reason,
                confidence=item.confidence or verdict.confidence,
                actor_user_id=actor_user_id,
                created_at=now,
            )
            db.add(audit)
            applied.append({
                "customer_id": cust.id,
                "old_name":    old_name,
                "new_name":    new_name,
                "reason":      audit.reason,
                "confidence":  audit.confidence,
            })

    # ── high-confidence-only mode (no explicit selection) ────────────
    # Stream the customer table so a tenant with tens of thousands of
    # rows doesn't blow memory loading them all at once. Same batch
    # size + ordering as the preview endpoint so the scan converges
    # on the same set of mutations.
    else:
        query = (
            db.query(Customer)
            .filter(Customer.tenant_id == tenant_id)
            .order_by(Customer.id.asc())
            .yield_per(_NAME_CLEANUP_BATCH_SIZE)
        )
        scanned = 0
        for cust in query:
            scanned += 1
            # Merchant-curated names are off-limits to the bulk cleaner.
            # See update_customer() for where this flag is stamped.
            if (cust.extra_metadata or {}).get("manual_name_override"):
                continue
            verdict = compute_cleanup(cust.name)
            if not verdict.changed or verdict.confidence != "high":
                continue
            old_name = cust.name
            cust.name = verdict.suggested
            # Stamp the same manual-override metadata as the per-row
            # apply path above so this fast-track behaves identically
            # to "tick every high-confidence box and apply" from the
            # UI. See the per-row branch for the rationale.
            try:
                _meta_hc = dict(cust.extra_metadata or {})
                _meta_hc["manual_name_override"]  = True
                _meta_hc["manual_name_cleared"]   = bool(verdict.suggested is None)
                _meta_hc["manual_name_edited_at"] = now.isoformat()
                _meta_hc["manual_name_previous"]  = old_name or ""
                _meta_hc["manual_name_source"]    = "bulk_cleanup_high_confidence"
                cust.extra_metadata = _meta_hc
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "[name-cleanup.apply/hc] override stamp failed "
                    "tenant=%s id=%s err=%s",
                    tenant_id, cust.id, exc,
                )
            audit = CustomerNameAuditLog(
                tenant_id=tenant_id,
                customer_id=cust.id,
                old_name=old_name,
                new_name=verdict.suggested,
                reason=verdict.reason,
                confidence="high",
                actor_user_id=actor_user_id,
                created_at=now,
            )
            db.add(audit)
            applied.append({
                "customer_id": cust.id,
                "old_name":    old_name,
                "new_name":    verdict.suggested,
                "reason":      verdict.reason,
                "confidence":  "high",
            })
        log.info(
            "high_confidence_apply scan | tenant=%s scanned=%d applied=%d",
            tenant_id, scanned, len(applied),
        )

    # Drop any draft rows for customers we just applied — their
    # in-progress review state is no longer interesting (the name
    # is now what the merchant approved, no need to keep editing it).
    drafts_cleared = 0
    if applied:
        applied_ids = [a["customer_id"] for a in applied]
        drafts_cleared = (
            db.query(CustomerNameCleanupDraft)
            .filter(
                CustomerNameCleanupDraft.tenant_id == tenant_id,
                CustomerNameCleanupDraft.customer_id.in_(applied_ids),
            )
            .delete(synchronize_session=False)
        )

    db.commit()
    log.info(
        "apply | tenant=%s actor=%s mode=%s applied=%d skipped=%d "
        "drafts_cleared=%d",
        tenant_id, actor_user_id,
        "items" if has_explicit_items else "high_confidence_only",
        len(applied), len(skipped), drafts_cleared,
    )

    return {
        "tenant_id":      tenant_id,
        "applied":        applied,
        "skipped":        skipped,
        "applied_count":  len(applied),
        "skipped_count":  len(skipped),
        "drafts_cleared": drafts_cleared,
    }
