"""
customer_commerce_ledger.py
───────────────────────────
Unified local customer commerce read model — platform-wide, source-agnostic.

Adapters import/sync only. AI and merchant surfaces read operational truth
from local ``orders`` (+ shipments for evidence flags only in later phases).

Phase 1: order counts + latest / latest-open summaries. No payment totals,
purchased-products list, addresses, or shipment replies.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Set

from core.local_order_resolver import (
    LocalOrderSnapshot,
    _fetch_tenant_orders_for_customer,
    _is_open_status,
    _load_shipment_tracking,
    _resolve_phone,
    _snapshot_from_order,
)

logger = logging.getLogger("nahla.customer_commerce_ledger")

_CANCELLED_STATUSES = frozenset({"cancelled", "canceled", "refunded"})
_ABANDONED_STATUSES = frozenset({"abandoned"})
_MAX_ORDER_REFERENCE_LIST_LIMIT = 5


@dataclass(frozen=True)
class CustomerIdentity:
    customer_id: Optional[int] = None
    display_name: str = ""
    phone: str = ""


@dataclass(frozen=True)
class OrderCounts:
    total_orders: int = 0
    open_orders: int = 0
    cancelled_orders: int = 0
    abandoned_carts: int = 0


@dataclass(frozen=True)
class EvidenceQuality:
    payment_totals_verified: bool = False
    tracking_available: bool = False
    line_items_complete: bool = False


@dataclass(frozen=True)
class CustomerCommerceProfile:
    customer_identity: CustomerIdentity
    order_counts: OrderCounts
    latest_order: Optional[LocalOrderSnapshot] = None
    latest_open_order: Optional[LocalOrderSnapshot] = None
    sources: Dict[str, int] = field(default_factory=dict)
    evidence_quality: EvidenceQuality = field(default_factory=EvidenceQuality)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        if self.latest_order is not None:
            data["latest_order"] = _snapshot_dict(self.latest_order)
        else:
            data["latest_order"] = None
        if self.latest_open_order is not None:
            data["latest_open_order"] = _snapshot_dict(self.latest_open_order)
        else:
            data["latest_open_order"] = None
        return data


def _snapshot_dict(snap: LocalOrderSnapshot) -> Dict[str, Any]:
    return {
        "order_id": snap.order_id,
        "external_id": snap.external_id,
        "external_order_number": snap.external_order_number,
        "display_reference": snap.display_reference,
        "status": snap.status,
        "source": snap.source,
        "total": snap.total,
        "tracking_number": snap.tracking_number,
        "is_open": snap.is_open,
    }


def _is_cancelled_status(status: str) -> bool:
    return str(status or "").strip().lower() in _CANCELLED_STATUSES


def _is_abandoned_order(order: Any) -> bool:
    if bool(getattr(order, "is_abandoned", False)):
        return True
    return str(getattr(order, "status", "") or "").strip().lower() in _ABANDONED_STATUSES


def _load_customer_identity(
    db: Any,
    *,
    tenant_id: int,
    customer_id: Optional[int],
    phone: str,
) -> CustomerIdentity:
    if db is None:
        return CustomerIdentity(customer_id=customer_id, phone=phone)
    try:
        from models import Customer  # noqa: PLC0415

        row = None
        if customer_id:
            row = (
                db.query(Customer)
                .filter(
                    Customer.tenant_id == int(tenant_id),
                    Customer.id == int(customer_id),
                )
                .first()
            )
        if row is None and phone:
            resolved = _resolve_phone(
                db, tenant_id=int(tenant_id), customer_id=None, phone=phone,
            )
            keys = {resolved, phone}
            for key in list(keys):
                if not key:
                    continue
                row = (
                    db.query(Customer)
                    .filter(
                        Customer.tenant_id == int(tenant_id),
                        Customer.normalized_phone == key,
                    )
                    .first()
                )
                if row:
                    break
                row = (
                    db.query(Customer)
                    .filter(
                        Customer.tenant_id == int(tenant_id),
                        Customer.phone == key,
                    )
                    .first()
                )
                if row:
                    break
        if row is None:
            return CustomerIdentity(customer_id=customer_id, phone=phone)
        return CustomerIdentity(
            customer_id=int(getattr(row, "id", 0) or 0) or customer_id,
            display_name=str(getattr(row, "name", "") or "").strip(),
            phone=str(
                getattr(row, "normalized_phone", None)
                or getattr(row, "phone", None)
                or phone
                or ""
            ).strip(),
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — identity probe must not break ledger
        return CustomerIdentity(customer_id=customer_id, phone=phone)


def _line_items_complete(snapshots: List[LocalOrderSnapshot]) -> bool:
    if not snapshots:
        return False
    for snap in snapshots:
        items = list(snap.line_items or [])
        if not items:
            return False
        for it in items:
            if not isinstance(it, dict):
                return False
            name = str(
                it.get("name") or it.get("title") or it.get("product_name") or ""
            ).strip()
            if not name:
                return False
    return True


def resolve_customer_commerce_profile(
    db: Any,
    *,
    tenant_id: int,
    conversation_id: Optional[int] = None,
    customer_id: Optional[int] = None,
    phone: Optional[str] = None,
    include_abandoned: bool = False,
    include_cancelled: bool = True,
) -> CustomerCommerceProfile:
    """
    Build a structured customer commerce profile from local ``orders`` only.

    ``conversation_id`` is accepted for API symmetry; phase 1 does not
  narrow history to a single conversation.
    """
    _ = conversation_id
    resolved_phone = _resolve_phone(
        db,
        tenant_id=int(tenant_id),
        customer_id=customer_id,
        phone=phone,
    )
    identity = _load_customer_identity(
        db,
        tenant_id=int(tenant_id),
        customer_id=customer_id,
        phone=resolved_phone or str(phone or ""),
    )

    rows = _fetch_tenant_orders_for_customer(
        db,
        tenant_id=int(tenant_id),
        phone=resolved_phone,
        customer_id=identity.customer_id or customer_id,
        limit=200,
    )

    snapshots_all = _to_snapshots(db, int(tenant_id), rows)
    sources: Dict[str, int] = {}
    abandoned_carts = 0
    cancelled_orders = 0
    open_orders = 0
    counted_rows: List[Any] = []

    for row, snap in zip(rows, snapshots_all):
        src = str(getattr(row, "source", "") or "unknown").strip().lower() or "unknown"
        abandoned = _is_abandoned_order(row)
        cancelled = _is_cancelled_status(snap.status)

        if abandoned:
            abandoned_carts += 1
        if cancelled:
            cancelled_orders += 1

        include = True
        if abandoned and not include_abandoned:
            include = False
        if cancelled and not include_cancelled:
            include = False
        if not include:
            continue

        counted_rows.append(row)
        sources[src] = int(sources.get(src, 0)) + 1
        if snap.is_open and not abandoned and not cancelled:
            open_orders += 1

    counted_snapshots = _to_snapshots(db, int(tenant_id), counted_rows)
    latest_order = counted_snapshots[0] if counted_snapshots else None
    latest_open = next((s for s in counted_snapshots if s.is_open), None)

    tracking_available = any(str(s.tracking_number or "").strip() for s in snapshots_all)

    profile = CustomerCommerceProfile(
        customer_identity=identity,
        order_counts=OrderCounts(
            total_orders=len(counted_rows),
            open_orders=open_orders,
            cancelled_orders=cancelled_orders,
            abandoned_carts=abandoned_carts,
        ),
        latest_order=latest_order,
        latest_open_order=latest_open,
        sources=dict(sources),
        evidence_quality=EvidenceQuality(
            payment_totals_verified=False,
            tracking_available=tracking_available,
            line_items_complete=_line_items_complete(counted_snapshots),
        ),
    )
    logger.debug(
        "[CUSTOMER_COMMERCE_LEDGER] tenant=%s phone=*%s total=%d open=%d "
        "cancelled=%d abandoned=%d",
        tenant_id,
        (resolved_phone or "")[-4:],
        profile.order_counts.total_orders,
        profile.order_counts.open_orders,
        profile.order_counts.cancelled_orders,
        profile.order_counts.abandoned_carts,
    )
    return profile


def list_recent_order_snapshots(
    db: Any,
    *,
    tenant_id: int,
    conversation_id: Optional[int] = None,
    customer_id: Optional[int] = None,
    phone: Optional[str] = None,
    include_abandoned: bool = False,
    include_cancelled: bool = True,
    limit: int = 5,
) -> List[LocalOrderSnapshot]:
    """
    Return recent order snapshots for a tenant-scoped customer lookup.

    Tenant isolation is enforced on every retrieval path. Only ``display_reference``
    from each snapshot is customer-safe for outbound replies.
    """
    _ = conversation_id
    effective_limit = max(1, min(int(limit or 5), _MAX_ORDER_REFERENCE_LIST_LIMIT))
    resolved_phone = _resolve_phone(
        db,
        tenant_id=int(tenant_id),
        customer_id=customer_id,
        phone=phone,
    )
    identity = _load_customer_identity(
        db,
        tenant_id=int(tenant_id),
        customer_id=customer_id,
        phone=resolved_phone or str(phone or ""),
    )

    rows = _fetch_tenant_orders_for_customer(
        db,
        tenant_id=int(tenant_id),
        phone=resolved_phone,
        customer_id=identity.customer_id or customer_id,
        # Same window as resolve_customer_commerce_profile: the helper caps
        # tenant-wide rows in SQL then filters by customer in Python, so a
        # smaller limit can miss recent matches when other customers' orders
        # fill the top of the tenant's id-desc window.
        limit=200,
    )
    snapshots_all = _to_snapshots(db, int(tenant_id), rows)
    filtered: List[LocalOrderSnapshot] = []

    for row, snap in zip(rows, snapshots_all):
        abandoned = _is_abandoned_order(row)
        cancelled = _is_cancelled_status(snap.status)
        include = True
        if abandoned and not include_abandoned:
            include = False
        if cancelled and not include_cancelled:
            include = False
        if not include:
            continue
        filtered.append(snap)
        if len(filtered) >= effective_limit:
            break

    return filtered


def _to_snapshots(
    db: Any,
    tenant_id: int,
    orders: List[Any],
) -> List[LocalOrderSnapshot]:
    out: List[LocalOrderSnapshot] = []
    for row in orders:
        oid = int(getattr(row, "id", 0) or 0)
        tracking = _load_shipment_tracking(db, tenant_id, oid)
        out.append(_snapshot_from_order(row, tracking_number=tracking))
    return out


__all__ = [
    "CustomerCommerceProfile",
    "CustomerIdentity",
    "EvidenceQuality",
    "OrderCounts",
    "list_recent_order_snapshots",
    "resolve_customer_commerce_profile",
]
