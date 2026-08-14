"""
core/local_order_resolver.py
────────────────────────────
Unified read model for local ``orders`` — platform-wide, source-agnostic.

Adapters import/sync only. AI replies and tracking read from here first.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Set

from sqlalchemy import or_

from utils.phone_utils import format_wa_send_recipient, normalize_phone_compat

logger = logging.getLogger("nahla.local_order_resolver")

_CLOSED_STATUSES = frozenset({
    "cancelled", "canceled", "abandoned", "delivered", "completed", "complete",
})
_PAID_STATUSES = frozenset({
    "paid", "completed", "complete", "confirmed", "delivered",
    "delivering", "shipped", "out_for_delivery", "fulfilled",
})
_SHIPPED_STATUSES = frozenset({
    "shipped", "delivered", "out_for_delivery", "delivering", "in_transit",
})

@dataclass(frozen=True)
class LocalOrderSnapshot:
    """Lightweight view of a local Order row for AI / tool consumers."""

    order_id: int
    external_id: str
    external_order_number: str
    status: str
    source: str
    total: Optional[str]
    customer_name: str
    line_items: List[Dict[str, Any]]
    tracking_number: str = ""
    tracking_url: str = ""
    carrier: str = ""
    shipment_status: str = ""

    @property
    def display_reference(self) -> str:
        ref = str(self.external_order_number or "").strip()
        if ref:
            return ref
        ext = str(self.external_id or "").strip()
        return ext

    @property
    def is_open(self) -> bool:
        return _is_open_status(self.status)

    @property
    def is_paid(self) -> bool:
        return _is_paid_status(self.status)

    @property
    def is_shipped(self) -> bool:
        return _is_shipped_status(self.status) or bool(self.tracking_number)


@dataclass(frozen=True)
class CustomerOrderContext:
    active_whatsapp_draft: Optional[LocalOrderSnapshot]
    latest_open_order: Optional[LocalOrderSnapshot]
    latest_paid_order: Optional[LocalOrderSnapshot]
    latest_shipped_order: Optional[LocalOrderSnapshot]
    orders_by_priority: List[LocalOrderSnapshot]
    selected_order: Optional[LocalOrderSnapshot]
    selected_reason: str


def _is_open_status(status: str) -> bool:
    s = str(status or "").strip().lower()
    return bool(s) and s not in _CLOSED_STATUSES


def _is_paid_status(status: str) -> bool:
    return str(status or "").strip().lower() in _PAID_STATUSES


def _is_shipped_status(status: str) -> bool:
    return str(status or "").strip().lower() in _SHIPPED_STATUSES


def _phone_lookup_keys(phone: str) -> Set[str]:
    raw = str(phone or "").strip()
    keys: Set[str] = set()
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
    return {k for k in keys if k}


def _order_matches_phone(order: Any, keys: Set[str]) -> bool:
    if not keys:
        return False
    info = dict(getattr(order, "customer_info", None) or {})
    for field in ("phone", "mobile", "shipping_phone"):
        val = str(info.get(field) or "").strip()
        if not val:
            continue
        if val in keys:
            return True
        msisdn = format_wa_send_recipient(val)
        if msisdn and msisdn in keys:
            return True
        e164 = normalize_phone_compat(val)
        if e164 and e164 in keys:
            return True
    return False


def _snapshot_from_order(
    order: Any,
    *,
    tracking_number: str = "",
    tracking_url: str = "",
    carrier: str = "",
    shipment_status: str = "",
) -> LocalOrderSnapshot:
    items = list(getattr(order, "line_items", None) or [])
    if not isinstance(items, list):
        items = []
    return LocalOrderSnapshot(
        order_id=int(getattr(order, "id", 0) or 0),
        external_id=str(getattr(order, "external_id", "") or ""),
        external_order_number=str(getattr(order, "external_order_number", "") or ""),
        status=str(getattr(order, "status", "") or ""),
        source=str(getattr(order, "source", "") or ""),
        total=getattr(order, "total", None),
        customer_name=str(getattr(order, "customer_name", "") or ""),
        line_items=[dict(x) for x in items if isinstance(x, dict)],
        tracking_number=str(tracking_number or ""),
        tracking_url=str(tracking_url or ""),
        carrier=str(carrier or ""),
        shipment_status=str(shipment_status or ""),
    )


def _load_shipment_evidence(db: Any, tenant_id: int, order_id: int) -> Dict[str, str]:
    if db is None or not order_id:
        return {
            "tracking_number": "",
            "tracking_url": "",
            "carrier": "",
            "shipment_status": "",
        }
    try:
        from models import OrderShipment  # noqa: PLC0415

        row = (
            db.query(OrderShipment)
            .filter(
                OrderShipment.tenant_id == int(tenant_id),
                OrderShipment.order_id == int(order_id),
            )
            .order_by(OrderShipment.id.desc())
            .first()
        )
        if row is None:
            return {
                "tracking_number": "",
                "tracking_url": "",
                "carrier": "",
                "shipment_status": "",
            }
        meta = dict(getattr(row, "extra_metadata", None) or {})
        tracking_url = str(
            meta.get("tracking_url")
            or getattr(row, "label_url", None)
            or ""
        ).strip()
        return {
            "tracking_number": str(getattr(row, "tracking_number", "") or "").strip(),
            "tracking_url": tracking_url,
            "carrier": str(getattr(row, "provider", "") or "").strip(),
            "shipment_status": str(getattr(row, "status", "") or "").strip(),
        }
    except Exception:  # noqa: BLE001
        return {
            "tracking_number": "",
            "tracking_url": "",
            "carrier": "",
            "shipment_status": "",
        }


def _load_shipment_tracking(db: Any, tenant_id: int, order_id: int) -> str:
    return _load_shipment_evidence(db, tenant_id, order_id).get("tracking_number", "")


def _resolve_phone(
    db: Any,
    *,
    tenant_id: int,
    customer_id: Optional[int],
    phone: Optional[str],
) -> str:
    if phone:
        return str(phone).strip()
    if db is None or not customer_id:
        return ""
    try:
        from models import Customer  # noqa: PLC0415

        row = (
            db.query(Customer)
            .filter(
                Customer.tenant_id == int(tenant_id),
                Customer.id == int(customer_id),
            )
            .first()
        )
        if row is None:
            return ""
        return str(
            getattr(row, "normalized_phone", None)
            or getattr(row, "phone", None)
            or ""
        ).strip()
    except Exception:  # noqa: BLE001
        return ""


def _fetch_tenant_orders_for_customer(
    db: Any,
    *,
    tenant_id: int,
    phone: str,
    customer_id: Optional[int],
    limit: int = 50,
) -> List[Any]:
    """Load this customer's orders with tenant+identity filter before LIMIT.

    Do not fetch a tenant-wide newest-N window and then filter in Python —
    other customers' newer rows would hide this customer's history.
    """
    if db is None:
        return []
    try:
        from models import Order  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return []

    keys = list(_phone_lookup_keys(phone))
    clauses: List[Any] = []
    if customer_id:
        try:
            cid = int(customer_id)
        except (TypeError, ValueError):
            cid = 0
        if cid:
            clauses.append(Order.customer_id == cid)
    if keys:
        clauses.append(
            or_(
                Order.customer_info["phone"].as_string().in_(keys),
                Order.customer_info["mobile"].as_string().in_(keys),
                Order.customer_info["shipping_phone"].as_string().in_(keys),
            )
        )
    if not clauses:
        return []
    return (
        db.query(Order)
        .filter(Order.tenant_id == int(tenant_id), or_(*clauses))
        .order_by(Order.id.desc())
        .limit(max(int(limit or 50), 10))
        .all()
    )


def _find_active_whatsapp_draft(
    db: Any,
    *,
    tenant_id: int,
    conversation_id: Optional[int],
) -> Optional[Any]:
    if db is None or not conversation_id:
        return None
    try:
        from models import Order  # noqa: PLC0415
        from services.nahla_order_bridge import (  # noqa: PLC0415
            is_open_wa_draft_order,
            nahla_wa_external_id,
        )

        prefix = nahla_wa_external_id(int(tenant_id), int(conversation_id))
        candidates = (
            db.query(Order)
            .filter(
                Order.tenant_id == int(tenant_id),
                Order.source == "whatsapp",
                Order.external_id.like(f"{prefix}%"),
            )
            .order_by(Order.id.desc())
            .limit(20)
            .all()
        )
        if not candidates:
            return None

        open_rows = [o for o in candidates if is_open_wa_draft_order(o)]
        if open_rows:
            return open_rows[0]

        for row in candidates:
            status = str(getattr(row, "status", "") or "").lower()
            if status not in {"cancelled", "canceled", "abandoned"}:
                return row
        return None
    except Exception as exc:  # noqa: BLE001
        logger.debug("active_whatsapp_draft lookup failed: %s", exc)
        return None


def _match_explicit_order_number(
    orders: Sequence[Any],
    order_number: str,
) -> Optional[Any]:
    needle = str(order_number or "").strip().lstrip("#")
    if not needle:
        return None
    for row in orders:
        ext_num = str(getattr(row, "external_order_number", "") or "").strip().lstrip("#")
        ext_id = str(getattr(row, "external_id", "") or "").strip()
        if needle.lower() in {ext_num.lower(), ext_id.lower()}:
            return row
        if needle.isdigit() and str(getattr(row, "id", "")) == needle:
            return row
    return None


def _to_snapshots(
    db: Any,
    tenant_id: int,
    orders: Sequence[Any],
) -> List[LocalOrderSnapshot]:
    out: List[LocalOrderSnapshot] = []
    for row in orders:
        oid = int(getattr(row, "id", 0) or 0)
        evidence = _load_shipment_evidence(db, tenant_id, oid)
        out.append(_snapshot_from_order(row, **evidence))
    return out


def _pick_latest(
    snapshots: Sequence[LocalOrderSnapshot],
    *,
    predicate,
) -> Optional[LocalOrderSnapshot]:
    for snap in snapshots:
        if predicate(snap):
            return snap
    return None


def _build_priority_list(
    snapshots: Sequence[LocalOrderSnapshot],
    *,
    active_draft: Optional[LocalOrderSnapshot],
) -> List[LocalOrderSnapshot]:
    seen: Set[int] = set()
    ordered: List[LocalOrderSnapshot] = []

    def _add(snap: Optional[LocalOrderSnapshot]) -> None:
        if snap is None or not snap.order_id or snap.order_id in seen:
            return
        seen.add(snap.order_id)
        ordered.append(snap)

    _add(active_draft)
    for snap in snapshots:
        if snap.is_open:
            _add(snap)
    for snap in snapshots:
        _add(snap)
    return ordered


def _order_number_explicitly_supplied(order_number: Optional[str]) -> bool:
    return bool(str(order_number or "").strip().lstrip("#"))


def _select_order(
    *,
    intent: Optional[str],
    active_draft: Optional[LocalOrderSnapshot],
    explicit: Optional[LocalOrderSnapshot],
    latest_open: Optional[LocalOrderSnapshot],
    latest_shipped: Optional[LocalOrderSnapshot],
    latest_paid: Optional[LocalOrderSnapshot],
    priority_list: Sequence[LocalOrderSnapshot],
    order_number_was_explicitly_supplied: bool = False,
) -> tuple[Optional[LocalOrderSnapshot], str]:
    if order_number_was_explicitly_supplied:
        if explicit is not None:
            return explicit, "explicit_order_number"
        return None, "explicit_order_number_not_found"
    if explicit is not None:
        return explicit, "explicit_order_number"
    if active_draft is not None:
        return active_draft, "active_whatsapp_draft"
    if latest_open is not None:
        return latest_open, "latest_open_order"

    intent_key = str(intent or "").strip().lower()
    if intent_key == "track_order" and latest_shipped is not None:
        return latest_shipped, "latest_shipped_order"
    if latest_paid is not None:
        return latest_paid, "latest_paid_order"
    if priority_list:
        return priority_list[0], "most_recent_order"
    return None, "none"


def resolve_customer_order_context(
    db: Any,
    *,
    tenant_id: int,
    conversation_id: Optional[int] = None,
    customer_id: Optional[int] = None,
    phone: Optional[str] = None,
    intent: Optional[str] = None,
    order_number: Optional[str] = None,
) -> CustomerOrderContext:
    """
    Resolve customer order context from local ``orders`` only.

    Parameters
    ----------
    intent:
        ``order_number`` | ``track_order`` | None
    order_number:
        Optional explicit reference from slots / customer message.
    """
    resolved_phone = _resolve_phone(
        db, tenant_id=int(tenant_id), customer_id=customer_id, phone=phone,
    )
    draft_row = _find_active_whatsapp_draft(
        db, tenant_id=int(tenant_id), conversation_id=conversation_id,
    )
    customer_rows = _fetch_tenant_orders_for_customer(
        db,
        tenant_id=int(tenant_id),
        phone=resolved_phone,
        customer_id=customer_id,
    )

    # Merge draft row into customer set (conversation may be only link).
    merged_rows: List[Any] = []
    seen_ids: Set[int] = set()
    for row in [draft_row, *customer_rows]:
        if row is None:
            continue
        oid = int(getattr(row, "id", 0) or 0)
        if oid and oid not in seen_ids:
            seen_ids.add(oid)
            merged_rows.append(row)

    snapshots = _to_snapshots(db, int(tenant_id), merged_rows)
    active_draft = (
        _snapshot_from_order(
            draft_row,
            **_load_shipment_evidence(
                db,
                int(tenant_id),
                int(getattr(draft_row, "id", 0) or 0),
            ),
        )
        if draft_row is not None
        else None
    )

    explicit_row = _match_explicit_order_number(merged_rows, str(order_number or ""))
    explicit = (
        _snapshot_from_order(
            explicit_row,
            **_load_shipment_evidence(
                db,
                int(tenant_id),
                int(getattr(explicit_row, "id", 0) or 0),
            ),
        )
        if explicit_row is not None
        else None
    )

    latest_open = _pick_latest(snapshots, predicate=lambda s: s.is_open)
    latest_paid = _pick_latest(snapshots, predicate=lambda s: s.is_paid)
    latest_shipped = _pick_latest(snapshots, predicate=lambda s: s.is_shipped)
    priority_list = _build_priority_list(snapshots, active_draft=active_draft)

    explicit_supplied = _order_number_explicitly_supplied(order_number)
    selected, selected_reason = _select_order(
        intent=intent,
        active_draft=active_draft,
        explicit=explicit,
        latest_open=latest_open,
        latest_shipped=latest_shipped,
        latest_paid=latest_paid,
        priority_list=priority_list,
        order_number_was_explicitly_supplied=explicit_supplied,
    )

    return CustomerOrderContext(
        active_whatsapp_draft=active_draft,
        latest_open_order=latest_open,
        latest_paid_order=latest_paid,
        latest_shipped_order=latest_shipped,
        orders_by_priority=priority_list,
        selected_order=selected,
        selected_reason=selected_reason,
    )


def _line_item_display_name(item: Dict[str, Any]) -> str:
    nested = item.get("product") if isinstance(item.get("product"), dict) else {}
    return str(
        item.get("name")
        or item.get("title")
        or item.get("product_name")
        or item.get("product_title")
        or nested.get("name")
        or nested.get("title")
        or ""
    ).strip()


def local_order_to_track_payload(snapshot: LocalOrderSnapshot) -> Dict[str, Any]:
    """Shape expected by ``TrackOrderHandler`` / compose templates."""
    items = []
    for it in snapshot.line_items:
        name = _line_item_display_name(it)
        items.append({
            "name": name,
            "title": name,
            "product_name": name,
            "quantity": it.get("quantity", 1),
        })
    from core.order_status_label import order_status_label_ar  # noqa: PLC0415

    status = str(snapshot.status or "").strip()
    shipping_status = str(snapshot.shipment_status or "").strip()
    if not shipping_status and snapshot.is_shipped:
        shipping_status = "shipped"
    return {
        "id": snapshot.order_id,
        "reference_id": snapshot.display_reference,
        "status": status,
        "status_label_ar": order_status_label_ar(status, source=snapshot.source),
        "total": snapshot.total,
        "currency": "SAR",
        "items": items,
        "source": snapshot.source,
        "tracking_number": snapshot.tracking_number,
        "tracking_url": snapshot.tracking_url,
        "carrier": snapshot.carrier,
        "shipping_status": shipping_status,
        "shipment_status": shipping_status,
        "local_resolver": True,
    }


def has_local_orders(ctx: CustomerOrderContext) -> bool:
    return bool(
        ctx.selected_order
        or ctx.orders_by_priority
        or ctx.active_whatsapp_draft
        or ctx.latest_open_order
    )


__all__ = [
    "CustomerOrderContext",
    "LocalOrderSnapshot",
    "has_local_orders",
    "local_order_to_track_payload",
    "resolve_customer_order_context",
]
