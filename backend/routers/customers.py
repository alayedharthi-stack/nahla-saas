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
from models import Customer, CustomerProfile
from services.nahla_segments import (
    build_segment_query,
    get_segment as get_nahla_segment,
    list_segments_with_counts,
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


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _days_since(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    target = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return max(0, (datetime.now(timezone.utc) - target).days)


def _serialize_customer(cust: Customer, profile: Optional[CustomerProfile]) -> Dict[str, Any]:
    meta = cust.extra_metadata or {}
    source, source_label = _resolve_customer_source(cust)
    status = str(
        (profile.customer_status if profile and getattr(profile, "customer_status", None) else None)
        or (profile.segment if profile else None)
        or "lead"
    )
    rfm_segment = str(
        (profile.rfm_segment if profile and getattr(profile, "rfm_segment", None) else None)
        or ("lead" if status == "lead" else "regulars")
    )

    result: Dict[str, Any] = {
        "id": cust.id,
        "name": cust.name or "",
        "phone": cust.phone or "",
        "email": cust.email or "",
        "source": source,
        "source_label": source_label,
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
            "Optional Nahla segment key (e.g. 'vip', 'dormant', 'no_purchase_60'). "
            "Filters the list to ONLY customers belonging to that segment, using "
            "the same canonical SQL as the campaign wizard."
        ),
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
        q = build_segment_query(seg_key, db, tenant_id, require_reachable=False)
        if q is None:
            # Defensive — get_nahla_segment said yes but builder said no
            raise HTTPException(status_code=422, detail=f"شريحة غير معروفة: {seg_key}")
    else:
        q = db.query(Customer).filter(Customer.tenant_id == tenant_id)

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

    return {
        "customers": [_serialize_customer(c, profiles.get(c.id)) for c in rows],
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

    return _serialize_customer(cust, profile)


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
