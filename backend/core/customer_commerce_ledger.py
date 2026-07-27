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
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy import DateTime, and_, asc, case, desc, false, func, literal, or_
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql import expression

from core.local_order_resolver import (
    LocalOrderSnapshot,
    _load_shipment_tracking,
    _phone_lookup_keys,
    _resolve_phone,
    _snapshot_from_order,
)
from utils.phone_utils import format_wa_send_recipient, normalize_phone_compat

logger = logging.getLogger("nahla.customer_commerce_ledger")

_CANCELLED_STATUSES = frozenset({"cancelled", "canceled", "refunded"})
_ABANDONED_STATUSES = frozenset({"abandoned"})
_CLOSED_STATUSES = frozenset({
    "cancelled", "canceled", "abandoned", "delivered", "completed", "complete",
})
_MAX_ORDER_REFERENCE_LIST_LIMIT = 5
_CLOSED_STATUSES_SQL: Tuple[str, ...] = tuple(sorted(_CLOSED_STATUSES))
# Production ``metadata.created_at`` shapes (offset, microsecond+offset, Z).
_PG_CREATED_AT_GUARD = (
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
)


class _LedgerPlacedAtChrono(expression.ColumnElement):
    """Dialect-specific safe parse of ``metadata.created_at`` for ordering."""

    inherit_cache = True

    def __init__(self, raw: Any) -> None:
        self.raw = raw
        self.type = DateTime(timezone=True)


@compiles(_LedgerPlacedAtChrono, "postgresql")
def _compile_ledger_placed_at_chrono_pg(
    element: _LedgerPlacedAtChrono, compiler: Any, **kwargs: Any,
) -> str:
    raw = compiler.process(element.raw, **kwargs)
    guard = f"({raw} ~ '{_PG_CREATED_AT_GUARD}')"
    return f"CASE WHEN {raw} IS NOT NULL AND {guard} THEN ({raw})::timestamptz END"


@compiles(_LedgerPlacedAtChrono, "sqlite")
def _compile_ledger_placed_at_chrono_sqlite(
    element: _LedgerPlacedAtChrono, compiler: Any, **kwargs: Any,
) -> str:
    raw = compiler.process(element.raw, **kwargs)
    return (
        "CASE WHEN "
        f"{raw} IS NOT NULL AND length({raw}) >= 19 "
        f"AND substr({raw}, 5, 1) = '-' AND substr({raw}, 8, 1) = '-' "
        f"AND substr({raw}, 11, 1) = 'T' "
        f"THEN datetime(replace(substr({raw}, 1, 19), 'T', ' ')) "
        "END"
    )


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


def _ledger_phone_sql_keys(phone: str) -> List[str]:
    """
    Build a comparison set for SQL ``IN`` matching on stored customer_info phones.

  Covers E.164, MSISDN, local ``05…``, bare national ``5…``, and the raw input.
    """
    keys: Set[str] = set(_phone_lookup_keys(phone))
    raw = str(phone or "").strip()
    if raw:
        keys.add(raw)
    msisdn = format_wa_send_recipient(raw)
    if msisdn:
        keys.add(msisdn)
        keys.add(f"+{msisdn}")
        if msisdn.startswith("966") and len(msisdn) >= 12:
            keys.add("0" + msisdn[3:])
            keys.add(msisdn[3:])
    e164 = normalize_phone_compat(raw)
    if e164:
        keys.add(e164)
        digits = e164.lstrip("+")
        if digits:
            keys.add(digits)
            if digits.startswith("966") and len(digits) >= 12:
                keys.add("0" + digits[3:])
                keys.add(digits[3:])
    return sorted({k for k in keys if k})


def _order_placed_at_raw_expr(order_model: Any) -> Any:
    """Raw ISO string from ``orders.metadata.created_at`` (may be NULL)."""
    return order_model.extra_metadata["created_at"].as_string()


def _order_placed_at_chrono_expr(order_model: Any) -> Any:
    """
    Chronological instant for ordering.

    PostgreSQL: ``::timestamptz`` behind a regex guard matching observed
    production shapes. SQLite: ``datetime()`` on the date-time prefix only
    (no timezone conversion — see module report).
    """
    return _LedgerPlacedAtChrono(_order_placed_at_raw_expr(order_model))


def _order_placed_at_nulls_last_key(order_model: Any) -> Any:
    """0 = sortable date, 1 = missing/malformed — forces NULLs last portably."""
    chrono = _order_placed_at_chrono_expr(order_model)
    raw = _order_placed_at_raw_expr(order_model)
    return case(
        (or_(raw.is_(None), raw == ""), literal(1)),
        (chrono.is_(None), literal(1)),
        else_=literal(0),
    )


def _customer_info_phone_sql_match(order_model: Any, keys: List[str]) -> Any:
    if not keys:
        return false()
    fields = (
        order_model.customer_info["phone"].as_string(),
        order_model.customer_info["mobile"].as_string(),
        order_model.customer_info["shipping_phone"].as_string(),
    )
    return or_(*[field.in_(keys) for field in fields])


def _ledger_customer_match_clause(
    order_model: Any,
    *,
    tenant_id: int,
    customer_id: Optional[int],
    phone_keys: List[str],
) -> Any:
    tenant_clause = order_model.tenant_id == int(tenant_id)
    link_clauses: List[Any] = []
    if customer_id:
        link_clauses.append(order_model.customer_id == int(customer_id))
    if phone_keys:
        link_clauses.append(_customer_info_phone_sql_match(order_model, phone_keys))
    if not link_clauses:
        return and_(tenant_clause, false())
    return and_(tenant_clause, or_(*link_clauses))


def _ledger_abandoned_sql(order_model: Any) -> Any:
    return or_(
        order_model.is_abandoned.is_(True),
        func.lower(func.coalesce(order_model.status, "")).in_(tuple(_ABANDONED_STATUSES)),
    )


def _ledger_cancelled_sql(order_model: Any) -> Any:
    return func.lower(func.coalesce(order_model.status, "")).in_(tuple(_CANCELLED_STATUSES))


def _ledger_visibility_clause(
    match_clause: Any,
    order_model: Any,
    *,
    include_abandoned: bool,
    include_cancelled: bool,
) -> Any:
    clauses: List[Any] = [match_clause]
    if not include_abandoned:
        clauses.append(~_ledger_abandoned_sql(order_model))
    if not include_cancelled:
        clauses.append(~_ledger_cancelled_sql(order_model))
    return and_(*clauses)


def _ledger_order_by_clauses(order_model: Any) -> Tuple[Any, ...]:
    chrono = _order_placed_at_chrono_expr(order_model)
    return (
        asc(_order_placed_at_nulls_last_key(order_model)),
        desc(chrono),
        desc(order_model.id),
    )


def _ledger_open_status_sql(order_model: Any) -> Any:
    return func.lower(func.coalesce(order_model.status, "")).notin_(_CLOSED_STATUSES_SQL)


def _query_ledger_orders(
    db: Any,
    *,
    visible_where: Any,
    order_model: Any,
    limit: Optional[int] = None,
    extra_filters: Tuple[Any, ...] = (),
) -> List[Any]:
    where = visible_where
    for extra in extra_filters:
        where = and_(where, extra)
    query = (
        db.query(order_model)
        .filter(where)
        .order_by(*_ledger_order_by_clauses(order_model))
    )
    if limit is not None:
        query = query.limit(int(limit))
    return query.all()


def _ledger_aggregate_counts(
    db: Any,
    *,
    match_clause: Any,
    visible_where: Any,
    order_model: Any,
) -> OrderCounts:
    total_orders = (
        db.query(func.count(order_model.id)).filter(visible_where).scalar() or 0
    )
    abandoned_carts = (
        db.query(func.count(order_model.id))
        .filter(match_clause, _ledger_abandoned_sql(order_model))
        .scalar()
        or 0
    )
    cancelled_orders = (
        db.query(func.count(order_model.id))
        .filter(match_clause, _ledger_cancelled_sql(order_model))
        .scalar()
        or 0
    )
    open_where = and_(visible_where, _ledger_open_status_sql(order_model))
    open_orders = (
        db.query(func.count(order_model.id)).filter(open_where).scalar() or 0
    )
    return OrderCounts(
        total_orders=int(total_orders),
        open_orders=int(open_orders),
        cancelled_orders=int(cancelled_orders),
        abandoned_carts=int(abandoned_carts),
    )


def _ledger_source_counts(
    db: Any,
    *,
    visible_where: Any,
    order_model: Any,
) -> Dict[str, int]:
    rows = (
        db.query(order_model.source, func.count(order_model.id))
        .filter(visible_where)
        .group_by(order_model.source)
        .all()
    )
    sources: Dict[str, int] = {}
    for source, count in rows:
        key = str(source or "unknown").strip().lower() or "unknown"
        sources[key] = int(count or 0)
    return sources


def _ledger_tracking_available(
    db: Any,
    *,
    tenant_id: int,
    match_clause: Any,
    order_model: Any,
) -> bool:
    if db is None:
        return False
    try:
        from models import OrderShipment  # noqa: PLC0415

        row = (
            db.query(OrderShipment.id)
            .join(order_model, OrderShipment.order_id == order_model.id)
            .filter(
                OrderShipment.tenant_id == int(tenant_id),
                match_clause,
            )
            .limit(1)
            .first()
        )
        return row is not None
    except Exception:  # noqa: BLE001
        return False


def _resolve_ledger_scope(
    db: Any,
    *,
    tenant_id: int,
    customer_id: Optional[int],
    phone: Optional[str],
) -> Tuple[CustomerIdentity, Optional[int], List[str]]:
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
    customer_id_eff = identity.customer_id or customer_id
    phone_keys = _ledger_phone_sql_keys(resolved_phone or str(phone or ""))
    return identity, customer_id_eff, phone_keys


def _ledger_clauses(
    order_model: Any,
    *,
    tenant_id: int,
    customer_id_eff: Optional[int],
    phone_keys: List[str],
    include_abandoned: bool,
    include_cancelled: bool,
) -> Tuple[Any, Any]:
    match_clause = _ledger_customer_match_clause(
        order_model,
        tenant_id=int(tenant_id),
        customer_id=customer_id_eff,
        phone_keys=phone_keys,
    )
    visible_where = _ledger_visibility_clause(
        match_clause,
        order_model,
        include_abandoned=include_abandoned,
        include_cancelled=include_cancelled,
    )
    return match_clause, visible_where


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
    if db is None:
        return CustomerCommerceProfile(
            customer_identity=CustomerIdentity(customer_id=customer_id, phone=str(phone or "")),
            order_counts=OrderCounts(),
        )

    from models import Order  # noqa: PLC0415

    identity, customer_id_eff, phone_keys = _resolve_ledger_scope(
        db,
        tenant_id=int(tenant_id),
        customer_id=customer_id,
        phone=phone,
    )
    match_clause, visible_where = _ledger_clauses(
        Order,
        tenant_id=int(tenant_id),
        customer_id_eff=customer_id_eff,
        phone_keys=phone_keys,
        include_abandoned=include_abandoned,
        include_cancelled=include_cancelled,
    )

    order_counts = _ledger_aggregate_counts(
        db,
        match_clause=match_clause,
        visible_where=visible_where,
        order_model=Order,
    )
    sources = _ledger_source_counts(
        db, visible_where=visible_where, order_model=Order,
    )

    latest_rows = _query_ledger_orders(
        db,
        visible_where=visible_where,
        order_model=Order,
        limit=1,
    )
    latest_open_rows = _query_ledger_orders(
        db,
        visible_where=visible_where,
        order_model=Order,
        limit=1,
        extra_filters=(_ledger_open_status_sql(Order),),
    )

    latest_snapshots = _to_snapshots(db, int(tenant_id), latest_rows)
    latest_open_snapshots = _to_snapshots(db, int(tenant_id), latest_open_rows)
    evidence_rows = _query_ledger_orders(
        db,
        visible_where=visible_where,
        order_model=Order,
        limit=25,
    )
    evidence_snapshots = _to_snapshots(db, int(tenant_id), evidence_rows)

    profile = CustomerCommerceProfile(
        customer_identity=identity,
        order_counts=order_counts,
        latest_order=latest_snapshots[0] if latest_snapshots else None,
        latest_open_order=latest_open_snapshots[0] if latest_open_snapshots else None,
        sources=sources,
        evidence_quality=EvidenceQuality(
            payment_totals_verified=False,
            tracking_available=_ledger_tracking_available(
                db,
                tenant_id=int(tenant_id),
                match_clause=match_clause,
                order_model=Order,
            ),
            line_items_complete=_line_items_complete(evidence_snapshots),
        ),
    )
    logger.debug(
        "[CUSTOMER_COMMERCE_LEDGER] tenant=%s phone=*%s total=%d open=%d "
        "cancelled=%d abandoned=%d",
        tenant_id,
        (identity.phone or "")[-4:],
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

    Tenant isolation and customer matching happen in SQL before LIMIT.
    Only ``display_reference`` from each snapshot is customer-safe for outbound replies.
    """
    _ = conversation_id
    effective_limit = max(1, min(int(limit or 5), _MAX_ORDER_REFERENCE_LIST_LIMIT))
    if db is None:
        return []

    from models import Order  # noqa: PLC0415

    _identity, customer_id_eff, phone_keys = _resolve_ledger_scope(
        db,
        tenant_id=int(tenant_id),
        customer_id=customer_id,
        phone=phone,
    )
    _match_clause, visible_where = _ledger_clauses(
        Order,
        tenant_id=int(tenant_id),
        customer_id_eff=customer_id_eff,
        phone_keys=phone_keys,
        include_abandoned=include_abandoned,
        include_cancelled=include_cancelled,
    )

    rows = _query_ledger_orders(
        db,
        visible_where=visible_where,
        order_model=Order,
        limit=effective_limit,
    )
    return _to_snapshots(db, int(tenant_id), rows)


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
    "_ledger_phone_sql_keys",
    "list_recent_order_snapshots",
    "resolve_customer_commerce_profile",
]
