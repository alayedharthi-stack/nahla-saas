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
from sqlalchemy.orm import Session

from core.database import get_db
from core.tenant import get_or_create_tenant, resolve_tenant_id
from models import Customer, CustomerProfile, CustomerSegmentManual
from services.nahla_segments import (
    SEGMENTS as NAHLA_SEGMENTS,
    build_segment_query,
    get_segment as get_nahla_segment,
    list_segments_with_counts,
)
from services.manual_segments import (
    UnknownSegmentError,
    add_manual_segment,
    assert_known_segment,
    customer_ids_with_manual_segment,
    list_manual_segments_bulk,
    list_manual_segments_for_customer,
    remove_manual_segment,
    set_marketing_opt_out_manual,
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
    meta = cust.extra_metadata or {}
    source, source_label = _resolve_customer_source(cust)
    is_unsubscribed:        bool = bool(meta.get("is_unsubscribed"))
    pending_unsubscribe:    bool = bool(meta.get("pending_unsubscribe"))
    marketing_opt_out_manual: bool = bool(meta.get("marketing_opt_out_manual"))
    is_campaign_test_recipient: bool = bool(meta.get("is_campaign_test_recipient"))
    status = str(
        (profile.customer_status if profile and getattr(profile, "customer_status", None) else None)
        or (profile.segment if profile else None)
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
        from sqlalchemy import String, cast, func, or_  # noqa: PLC0415
        is_opted = cast(
            func.coalesce(
                Customer.extra_metadata["marketing_opt_out_manual"].astext
                if hasattr(Customer.extra_metadata, "astext")
                else func.json_extract(Customer.extra_metadata, "$.marketing_opt_out_manual"),
                "false",
            ),
            String,
        ).in_(["true", "1"])
        q = q.filter(is_opted) if marketing_opt_out else q.filter(~is_opted)

    if test_recipient is not None:
        from sqlalchemy import String, cast, func  # noqa: PLC0415
        is_test = cast(
            func.coalesce(
                Customer.extra_metadata["is_campaign_test_recipient"].astext
                if hasattr(Customer.extra_metadata, "astext")
                else func.json_extract(Customer.extra_metadata, "$.is_campaign_test_recipient"),
                "false",
            ),
            String,
        ).in_(["true", "1"])
        q = q.filter(is_test) if test_recipient else q.filter(~is_test)

    if search.strip():
        term = f"%{search.strip()}%"
        q = q.filter(
            (Customer.name.ilike(term)) | (Customer.phone.ilike(term))
        )

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

    if body.name is not None:
        cust.name = body.name
    if body.email is not None:
        cust.email = body.email

    service.ensure_profile(cust, seen_at=datetime.now(timezone.utc))
    service.recompute_profile_for_customer(
        cust.id,
        reason="manual_customer_update",
        commit=True,
        emit_event=True,
    )
    return {"updated": True}


@router.delete("/{customer_id}")
async def delete_customer(customer_id: int, request: Request, db: Session = Depends(get_db)):
    tenant_id = resolve_tenant_id(request)
    cust = db.query(Customer).filter(
        Customer.id == customer_id, Customer.tenant_id == tenant_id,
    ).first()
    if not cust:
        raise HTTPException(status_code=404, detail="العميل غير موجود")

    db.query(CustomerProfile).filter_by(customer_id=cust.id, tenant_id=tenant_id).delete()
    db.delete(cust)
    db.commit()
    return {"deleted": True}


# ── Bulk delete ──────────────────────────────────────────────────────────────

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
        # Wipe all customers for this tenant
        profiles_deleted = (
            db.query(CustomerProfile)
            .filter(CustomerProfile.tenant_id == tenant_id)
            .delete(synchronize_session=False)
        )
        customers_deleted = (
            db.query(Customer)
            .filter(Customer.tenant_id == tenant_id)
            .delete(synchronize_session=False)
        )
        db.commit()
        return {"deleted": customers_deleted, "profiles_deleted": profiles_deleted}

    if not body.ids:
        raise HTTPException(status_code=400, detail="لم يتم تحديد أي عملاء للحذف")

    # Delete only the specified IDs (must belong to this tenant)
    db.query(CustomerProfile).filter(
        CustomerProfile.customer_id.in_(body.ids),
        CustomerProfile.tenant_id == tenant_id,
    ).delete(synchronize_session=False)

    result = db.query(Customer).filter(
        Customer.id.in_(body.ids),
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
        "marketing_opt_out_manual":     bool(meta.get("marketing_opt_out_manual")),
        "marketing_opt_out_manual_at":  meta.get("marketing_opt_out_manual_at"),
        "is_campaign_test_recipient":   bool(meta.get("is_campaign_test_recipient")),
        "campaign_test_recipient_at":   meta.get("campaign_test_recipient_at"),
    }
