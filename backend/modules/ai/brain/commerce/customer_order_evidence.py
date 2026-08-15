"""
Bounded customer-order evidence for LLM reasoning.

Order facts may be available without the order owner owning the turn.
Retrieval is tenant + customer scoped. Line items, totals, actual carrier,
and actual payment come from persisted orders / shipments only — never
from catalog guesses or merchant capability lists.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence

_DEFAULT_LIMIT = 8
_MAX_LINE_ITEMS = 12
_REF_TOKEN_RE = re.compile(r"\d{6,12}")


def _identity_ready(*, phone: Optional[str], customer_id: Any) -> bool:
    if str(phone or "").strip():
        return True
    try:
        return int(customer_id or 0) > 0
    except (TypeError, ValueError):
        return False


def _slim_line_item(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    nested = raw.get("product") if isinstance(raw.get("product"), dict) else {}
    name = str(
        raw.get("name")
        or raw.get("title")
        or raw.get("product_name")
        or raw.get("product_title")
        or nested.get("name")
        or nested.get("title")
        or ""
    ).strip()
    if not name:
        return None
    item: Dict[str, Any] = {"name": name}
    qty = raw.get("quantity") if raw.get("quantity") not in (None, "") else raw.get("qty")
    if qty not in (None, ""):
        try:
            item["quantity"] = int(qty)
        except (TypeError, ValueError):
            item["quantity"] = qty
    variant = str(
        raw.get("variant")
        or raw.get("variant_name")
        or raw.get("sku")
        or nested.get("sku")
        or ""
    ).strip()
    if variant:
        item["variant"] = variant
    return item


def _payment_method_from_order(order: Any) -> str:
    meta = dict(getattr(order, "extra_metadata", None) or {})
    nested = meta.get("payment") if isinstance(meta.get("payment"), dict) else {}
    return str(
        meta.get("payment_method")
        or nested.get("method")
        or nested.get("payment_method")
        or ""
    ).strip()


def _carrier_from_order_meta(order: Any) -> str:
    meta = dict(getattr(order, "extra_metadata", None) or {})
    shipping = meta.get("shipping") if isinstance(meta.get("shipping"), dict) else {}
    company = shipping.get("company") if isinstance(shipping.get("company"), dict) else {}
    return str(
        meta.get("shipping_company")
        or company.get("name")
        or shipping.get("company_name")
        or ""
    ).strip()


def _placed_at_from_order(order: Any) -> str:
    meta = dict(getattr(order, "extra_metadata", None) or {})
    return str(meta.get("created_at") or "").strip()


def _digit_refs(message: str) -> List[str]:
    seen: List[str] = []
    for token in _REF_TOKEN_RE.findall(str(message or "")):
        if token not in seen:
            seen.append(token)
    return seen


def _order_evidence_item(
    order: Any,
    *,
    db: Any,
    tenant_id: int,
) -> Optional[Dict[str, Any]]:
    from core.local_order_resolver import (  # noqa: PLC0415
        _load_shipment_evidence,
        _snapshot_from_order,
    )

    oid = int(getattr(order, "id", 0) or 0)
    if not oid:
        return None
    shipment = _load_shipment_evidence(db, int(tenant_id), oid)
    snap = _snapshot_from_order(order, **shipment)
    items: List[Dict[str, Any]] = []
    for raw in list(getattr(order, "line_items", None) or []):
        if len(items) >= _MAX_LINE_ITEMS:
            break
        slim = _slim_line_item(raw)
        if slim:
            items.append(slim)
    payload: Dict[str, Any] = {
        "order_id": snap.order_id,
        "display_reference": snap.display_reference,
        "external_id": snap.external_id,
        "external_order_number": snap.external_order_number,
        "status": snap.status,
        "source": snap.source,
        "is_open": bool(snap.is_open),
        "line_items": items,
    }
    if snap.total not in (None, ""):
        payload["total"] = snap.total
        payload["currency"] = "SAR"
    carrier = str(snap.carrier or "").strip() or _carrier_from_order_meta(order)
    if carrier:
        payload["carrier"] = carrier
    shipment_status = str(snap.shipment_status or "").strip()
    if shipment_status:
        payload["shipment_status"] = shipment_status
    tracking = str(snap.tracking_number or "").strip()
    if tracking:
        payload["tracking_number"] = tracking
    payment = _payment_method_from_order(order)
    if payment:
        payload["payment_method"] = payment
    placed_at = _placed_at_from_order(order)
    if placed_at:
        payload["placed_at"] = placed_at
    return payload


def last_discussed_order_ref_from_state(state: Any) -> str:
    return str(getattr(state, "last_discussed_order_ref", "") or "").strip()


def stamp_last_discussed_order_ref(state: Any, ref: Any) -> None:
    if state is None:
        return
    token = str(ref or "").strip()
    if token:
        state.last_discussed_order_ref = token


def collect_customer_order_evidence(
    *,
    db: Any = None,
    tenant_id: int = 0,
    phone: Optional[str] = None,
    customer_id: Any = None,
    conversation_id: Any = None,
    message: str = "",
    last_discussed_order_ref: str = "",
    limit: int = _DEFAULT_LIMIT,
) -> Optional[Dict[str, Any]]:
    """Return bounded tenant+customer order evidence, or None if unscoped."""
    if db is None or int(tenant_id or 0) <= 0:
        return None
    if not _identity_ready(phone=phone, customer_id=customer_id):
        return None

    from core.customer_commerce_ledger import (  # noqa: PLC0415
        find_customer_orders_by_references,
        list_customer_order_rows,
        resolve_customer_commerce_profile,
    )

    cap = max(1, min(int(limit or _DEFAULT_LIMIT), 12))
    cid: Optional[int]
    try:
        cid = int(customer_id) if customer_id not in (None, "") else None
    except (TypeError, ValueError):
        cid = None

    profile = resolve_customer_commerce_profile(
        db,
        tenant_id=int(tenant_id),
        conversation_id=conversation_id,
        customer_id=cid,
        phone=phone,
        include_abandoned=False,
        include_cancelled=True,
    )
    rows = list(list_customer_order_rows(
        db,
        tenant_id=int(tenant_id),
        conversation_id=conversation_id,
        customer_id=cid,
        phone=phone,
        include_abandoned=False,
        include_cancelled=True,
        limit=cap,
    ) or [])

    refs = _digit_refs(message)
    if refs:
        extra = list(find_customer_orders_by_references(
            db,
            tenant_id=int(tenant_id),
            customer_id=cid,
            phone=phone,
            references=refs,
        ) or [])
        seen_ids = {int(getattr(row, "id", 0) or 0) for row in rows}
        for row in extra:
            oid = int(getattr(row, "id", 0) or 0)
            if oid and oid not in seen_ids:
                rows.insert(0, row)
                seen_ids.add(oid)

    orders: List[Dict[str, Any]] = []
    seen_order_ids: set[int] = set()
    for row in rows:
        item = _order_evidence_item(row, db=db, tenant_id=int(tenant_id))
        if not item:
            continue
        oid = int(item["order_id"])
        if oid in seen_order_ids:
            continue
        seen_order_ids.add(oid)
        orders.append(item)
        if len(orders) >= cap:
            break

    current = next((row for row in orders if row.get("is_open")), None)
    latest_open = current
    latest = orders[0] if orders else None
    previous = [
        row
        for row in orders
        if current is None
        or int(row.get("order_id") or 0) != int(current.get("order_id") or 0)
    ]

    last_ref = str(last_discussed_order_ref or "").strip()
    referenced = None
    if last_ref:
        referenced = next(
            (
                row
                for row in orders
                if last_ref
                in {
                    str(row.get("display_reference") or ""),
                    str(row.get("external_order_number") or ""),
                    str(row.get("external_id") or ""),
                    str(row.get("order_id") or ""),
                }
            ),
            None,
        )
    if referenced is None and refs:
        ref_set = set(refs)
        referenced = next(
            (
                row
                for row in orders
                if str(row.get("display_reference") or "") in ref_set
                or str(row.get("external_order_number") or "") in ref_set
                or str(row.get("external_id") or "") in ref_set
            ),
            None,
        )

    counts = profile.order_counts
    payload: Dict[str, Any] = {
        "source": "local_orders",
        "tenant_scoped": True,
        "customer_scoped": True,
        "order_count": int(counts.total_orders or 0),
        "open_order_count": int(counts.open_orders or 0),
        "current_order": current,
        "current_open_order": current,
        "latest_open_order": latest_open,
        "latest_order": latest,
        "previous_orders": previous,
        "referenced_order": referenced,
        "orders": orders,
        "roles": {
            "current_order": "first_open_otherwise_none",
            "current_open_order": "alias_of_current_order",
            "latest_open_order": "first_open",
            "latest_order": "newest_including_cancelled",
            "previous_orders": "history_excluding_current_open",
            "referenced_order": "last_discussed_or_explicit_ref",
        },
        "history_truncated": int(counts.total_orders or 0) > len(orders),
        "evidence_quality": {
            "line_items_present": any(bool(row.get("line_items")) for row in orders),
            "carrier_present": any(bool(row.get("carrier")) for row in orders),
            "payment_method_present": any(
                bool(row.get("payment_method")) for row in orders
            ),
        },
    }
    return payload


def customer_order_evidence_available(evidence: Any) -> bool:
    if not isinstance(evidence, dict):
        return False
    return bool(evidence.get("orders") or int(evidence.get("order_count") or 0) > 0)


__all__ = [
    "collect_customer_order_evidence",
    "customer_order_evidence_available",
    "last_discussed_order_ref_from_state",
    "stamp_last_discussed_order_ref",
]
