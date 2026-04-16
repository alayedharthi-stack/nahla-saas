"""
routers/product_interests.py
─────────────────────────────
"Notify me when back in stock" waitlist API.

Two callers exist:

  1. The merchant's storefront (a small JS widget on the product detail
     page) calls POST /products/{product_id}/notify-me with the visitor's
     phone number while the product is sold out.
  2. The AI sales agent calls the same endpoint internally when a
     customer asks for a product that has stock_quantity == 0.

The row is consumed by services/store_sync.py — when the next product
sync detects a 0 → >0 transition, the engine fans out one
`product_back_in_stock` AutomationEvent per still-pending row.

Routes
──────
  POST /products/{product_id}/notify-me   — register interest
  GET  /products/{product_id}/notify-me   — list pending interests (admin)
  GET  /product-interests/metrics         — totals (pending / notified)
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.database import get_db
from core.tenant import get_or_create_tenant, resolve_tenant_id
from models import Customer, Product, ProductInterest
from services.customer_intelligence import (
    CustomerIntelligenceService,
    normalize_phone,
)

router = APIRouter(tags=["ProductInterests"])


class NotifyMeIn(BaseModel):
    customer_phone: str
    customer_name: Optional[str] = None
    source: Optional[str] = "widget"


def _interest_to_dict(i: ProductInterest) -> Dict[str, Any]:
    return {
        "id":              i.id,
        "product_id":      i.product_id,
        "customer_id":     i.customer_id,
        "customer_phone":  i.customer_phone,
        "source":          i.source,
        "notified":        bool(i.notified),
        "notified_at":     i.notified_at.isoformat() if i.notified_at else None,
        "created_at":      i.created_at.isoformat() if i.created_at else None,
    }


@router.post("/products/{product_id}/notify-me")
async def register_product_interest(
    product_id: int,
    body: NotifyMeIn,
    request: Request,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """
    Register a notify-me request. Idempotent: if the same customer already
    has a pending interest row for this product, returns it untouched
    rather than 409-ing — the storefront is allowed to retry safely.
    """
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    phone = (body.customer_phone or "").strip()
    if not phone:
        raise HTTPException(status_code=422, detail="customer_phone is required")

    product: Optional[Product] = (
        db.query(Product)
        .filter(Product.id == product_id, Product.tenant_id == tenant_id)
        .first()
    )
    if not product:
        raise HTTPException(status_code=404, detail="product_not_found")

    # Route through the canonical customer dedupe path so storefront
    # signups, AI-sales orders, and store-sync customers all collapse to
    # the same Customer row when they share a normalised phone.
    intel = CustomerIntelligenceService(db, tenant_id)
    customer = intel.upsert_customer_identity(
        phone=phone,
        name=body.customer_name or phone,
        source=body.source or "widget",
        extra_metadata={"source": body.source or "widget", "via": "notify_me"},
        seen_at=datetime.now(timezone.utc),
    )
    if customer is None:
        raise HTTPException(status_code=400, detail="invalid_customer")
    db.flush()

    normalized = normalize_phone(phone) or phone

    # Idempotent: pending row for (tenant, product, customer) already?
    existing = (
        db.query(ProductInterest)
        .filter(
            ProductInterest.tenant_id  == tenant_id,
            ProductInterest.product_id == product_id,
            ProductInterest.customer_id == customer.id,
            ProductInterest.notified   == False,  # noqa: E712
        )
        .first()
    )
    if existing:
        return {"interest": _interest_to_dict(existing), "created": False}

    interest = ProductInterest(
        tenant_id      = tenant_id,
        product_id     = product_id,
        customer_id    = customer.id,
        customer_phone = normalized,
        source         = body.source or "widget",
        notified       = False,
        created_at     = datetime.now(timezone.utc),
        extra_metadata = {"source": body.source or "widget"},
    )
    db.add(interest)
    db.commit()
    db.refresh(interest)
    return {"interest": _interest_to_dict(interest), "created": True}


@router.get("/products/{product_id}/notify-me")
async def list_product_interests(
    product_id: int,
    request: Request,
    db: Session = Depends(get_db),
    pending_only: bool = True,
) -> Dict[str, Any]:
    """List interest rows for a product. Used by the merchant dashboard."""
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    q = (
        db.query(ProductInterest)
        .filter(
            ProductInterest.tenant_id  == tenant_id,
            ProductInterest.product_id == product_id,
        )
    )
    if pending_only:
        q = q.filter(ProductInterest.notified == False)  # noqa: E712
    rows = q.order_by(ProductInterest.created_at.desc()).limit(500).all()
    return {
        "product_id": product_id,
        "count":      len(rows),
        "interests":  [_interest_to_dict(r) for r in rows],
    }


@router.get("/product-interests/metrics")
async def product_interests_metrics(
    request: Request,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Tenant-wide totals so the dashboard can show waitlist health at a glance."""
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)

    base = db.query(ProductInterest).filter(ProductInterest.tenant_id == tenant_id)
    total    = base.count()
    pending  = base.filter(ProductInterest.notified == False).count()  # noqa: E712
    notified = base.filter(ProductInterest.notified == True).count()   # noqa: E712
    return {
        "total":    total,
        "pending":  pending,
        "notified": notified,
    }
