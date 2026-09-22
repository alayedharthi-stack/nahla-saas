"""Tenant-scoped Salla shipment tracking ingestion.

Coverage is intentionally narrow: authenticated Salla Merchant API shipment
records and their ``GET /shipments/{id}/tracking`` histories only.  We do not
call carrier websites, construct tracking URLs, or look up a tracking number on
its own.  Salla may omit station/city fields; those fields are exposed only
when the tracking event explicitly contains them.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Optional
from urllib.parse import urlsplit

from services.salla_datetime import parse_salla_datetime_to_utc

TRACKING_SOURCE_SALLA = "salla_merchant_api"
TRACKING_AVAILABLE = "available"
TRACKING_NO_DATA = "no_tracking_data"
TRACKING_REFRESH_FAILED = "source_refresh_failed"
TRACKING_STORED_REFRESH_FAILED = "stored_data_refresh_failed"


@dataclass(frozen=True)
class TrackingRefreshResult:
    state: str
    refreshed: bool
    shipment_id: Optional[str] = None
    error_code: Optional[str] = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_text(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def _safe_display_tracking_url(value: Any) -> Optional[str]:
    """Allow only a credential-free HTTPS link supplied by Salla.

    This service never fetches the link (therefore does not follow redirects),
    but rejecting credentials and non-HTTPS schemes keeps an untrusted carrier
    value from becoming an unsafe consumer-facing URL.
    """
    url = _as_text(value)
    if not url or len(url) > 300:
        return None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return None
    return url


def _event_location(event: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    """Read location facts only from explicit structured source fields."""
    station = _as_text(event.get("station"))
    city = _as_text(event.get("city"))
    location = event.get("location")
    if isinstance(location, dict):
        station = station or _as_text(location.get("station") or location.get("name"))
        city = city or _as_text(location.get("city"))
    return station, city


def _event_from_raw(event: Any) -> Optional[dict[str, Any]]:
    if not isinstance(event, dict):
        return None
    status = _as_text(event.get("status"))
    note = _as_text(event.get("note") or event.get("description"))
    occurred_at = parse_salla_datetime_to_utc(
        event.get("create_at") or event.get("created_at") or event.get("occurred_at")
    )
    station, city = _event_location(event)
    if not any((status, note, occurred_at, station, city)):
        return None
    result: dict[str, Any] = {
        "status": status,
        "note": note,
        "occurred_at": occurred_at.isoformat() if occurred_at else None,
    }
    # Do not infer geography from free-text notes or another part of the order.
    if station:
        result["station"] = station
    if city:
        result["city"] = city
    return result


def _event_time(event: Optional[dict[str, Any]]) -> Optional[datetime]:
    if not event:
        return None
    return parse_salla_datetime_to_utc(event.get("occurred_at"))


def _latest_event(history: Iterable[Any]) -> Optional[dict[str, Any]]:
    candidates = [parsed for raw in history if (parsed := _event_from_raw(raw))]
    if not candidates:
        return None
    # Salla currently returns newest-first, but source timestamps—not arrival
    # order—determine freshness. Untimed events cannot supersede a timed event.
    timed = [event for event in candidates if _event_time(event) is not None]
    if timed:
        return max(timed, key=lambda event: _event_time(event) or datetime.min.replace(tzinfo=timezone.utc))
    return candidates[0]


def _shipment_candidate(raw: dict[str, Any]) -> Optional[dict[str, Any]]:
    shipment_id = _as_text(raw.get("id"))
    order_id = _as_text(raw.get("order_id"))
    if not shipment_id or not order_id:
        return None
    event = _latest_event(raw.get("history") or [])
    status = _as_text((event or {}).get("status")) or _as_text(raw.get("status"))
    source_event_at = _event_time(event) or parse_salla_datetime_to_utc(
        raw.get("actual_delivered_at") or raw.get("updated_at") or raw.get("created_at")
    )
    return {
        "shipment_id": shipment_id,
        "order_id": order_id,
        "status": status or "shipment_created",
        "carrier": _as_text(raw.get("courier_name") or raw.get("external_company_name")),
        "tracking_number": _as_text(raw.get("tracking_number") or raw.get("shipping_number")),
        # The link comes from authenticated Salla data and is display-only. It
        # is never fetched or followed by this service, so it cannot create an
        # SSRF path (including through redirects or embedded credentials).
        "tracking_url": _safe_display_tracking_url(raw.get("tracking_link")),
        "latest_event": event,
        "source_event_at": source_event_at,
    }


def _tracking_meta(order: Any) -> dict[str, Any]:
    meta = getattr(order, "extra_metadata", None) or {}
    return dict(meta) if isinstance(meta, dict) else {}


def _stamp_order_refresh(
    order: Any,
    *,
    state: str,
    observed_via: str,
    error_code: Optional[str] = None,
) -> None:
    meta = _tracking_meta(order)
    old = meta.get("salla_tracking")
    tracking = dict(old) if isinstance(old, dict) else {}
    now = _now()
    tracking.update({
        "data_source": TRACKING_SOURCE_SALLA,
        "state": state,
        "observed_via": observed_via,
        "last_attempt_at": now.isoformat(),
    })
    if error_code:
        tracking["last_refresh_error_code"] = error_code
        tracking["last_refresh_failed_at"] = now.isoformat()
    else:
        tracking["last_successful_verification_at"] = now.isoformat()
        tracking.pop("last_refresh_error_code", None)
        tracking.pop("last_refresh_failed_at", None)
    meta["salla_tracking"] = tracking
    order.extra_metadata = meta


def _existing_event_time(shipment: Any) -> Optional[datetime]:
    return parse_salla_datetime_to_utc(getattr(shipment, "source_event_at", None))


def _apply_candidate(db: Any, *, tenant_id: int, order: Any, candidate: dict[str, Any]) -> tuple[Any, bool]:
    from models import OrderShipment  # noqa: PLC0415

    # Never trust the carrier's tracking number as an identifier.  These two
    # predicates are the only route from the API request to a local shipment.
    shipment = (
        db.query(OrderShipment)
        .filter(
            OrderShipment.tenant_id == int(tenant_id),
            OrderShipment.order_id == int(order.id),
        )
        .with_for_update()
        .one_or_none()
    )
    claimed_elsewhere = (
        db.query(OrderShipment)
        .filter(
            OrderShipment.tenant_id == int(tenant_id),
            OrderShipment.tracking_data_source == TRACKING_SOURCE_SALLA,
            OrderShipment.external_shipment_id == candidate["shipment_id"],
        )
        .one_or_none()
    )
    if claimed_elsewhere is not None and (
        shipment is None or claimed_elsewhere.id != shipment.id
    ):
        raise RuntimeError("external_shipment_owned_by_another_order")

    if shipment is None:
        shipment = OrderShipment(
            tenant_id=int(tenant_id),
            order_id=int(order.id),
            provider="salla",
            status=candidate["status"],
        )
        db.add(shipment)

    incoming_at = candidate.get("source_event_at")
    current_at = _existing_event_time(shipment)
    # A delayed webhook or duplicate poll must not replace a newer event/status.
    has_untimed_salla_event = bool(
        current_at is None
        and getattr(shipment, "tracking_data_source", None) == TRACKING_SOURCE_SALLA
        and getattr(shipment, "latest_event", None) is not None
    )
    newer = (
        (current_at is None and not has_untimed_salla_event)
        or incoming_at is not None and (current_at is None or incoming_at > current_at)
    )
    if newer:
        shipment.status = candidate["status"]
        shipment.latest_event = candidate["latest_event"]
        shipment.source_event_at = incoming_at

    shipment.tracking_data_source = TRACKING_SOURCE_SALLA
    shipment.external_shipment_id = candidate["shipment_id"]
    shipment.carrier = candidate["carrier"] or shipment.carrier
    shipment.tracking_number = candidate["tracking_number"] or shipment.tracking_number
    shipment.tracking_url = candidate["tracking_url"] or shipment.tracking_url
    shipment.last_verified_at = _now()
    return shipment, newer


async def refresh_order_tracking(
    db: Any,
    *,
    tenant_id: int,
    order: Any,
    adapter: Any,
    observed_via: str,
    shipment_id: Optional[str] = None,
) -> TrackingRefreshResult:
    """Refresh one local order from Salla and retain the newest source event."""
    if int(getattr(order, "tenant_id", -1)) != int(tenant_id):
        raise ValueError("tenant_order_mismatch")
    external_order_id = _as_text(getattr(order, "external_id", None))
    if not external_order_id:
        _stamp_order_refresh(order, state=TRACKING_NO_DATA, observed_via=observed_via)
        return TrackingRefreshResult(TRACKING_NO_DATA, refreshed=True)
    if not hasattr(adapter, "get_shipments") or not hasattr(adapter, "get_shipment_tracking"):
        _stamp_order_refresh(
            order, state=TRACKING_REFRESH_FAILED, observed_via=observed_via,
            error_code="salla_shipping_read_unsupported",
        )
        return TrackingRefreshResult(TRACKING_REFRESH_FAILED, refreshed=False, error_code="salla_shipping_read_unsupported")

    try:
        refs: list[dict[str, Any]]
        if shipment_id:
            refs = [{"id": str(shipment_id)}]
        else:
            listed = await adapter.get_shipments(order_id=external_order_id)
            refs = [item for item in (listed or []) if isinstance(item, dict)]
        tracked: list[dict[str, Any]] = []
        for ref in refs:
            ref_id = _as_text(ref.get("id"))
            if not ref_id:
                continue
            raw = await adapter.get_shipment_tracking(ref_id)
            if not isinstance(raw, dict):
                continue
            candidate = _shipment_candidate(raw)
            # A Salla event must still prove that the returned shipment belongs
            # to this exact tenant-scoped local order before any data is saved.
            if candidate is None or candidate["order_id"] != external_order_id:
                continue
            if _as_text(raw.get("type")) not in (None, "shipment"):
                continue
            tracked.append(candidate)
    except Exception as exc:  # preserve old data; do not present it as fresh
        from core.coupon_log_privacy import safe_exception_class  # noqa: PLC0415

        from models import OrderShipment  # noqa: PLC0415

        has_stored = (
            db.query(OrderShipment.id)
            .filter(
                OrderShipment.tenant_id == int(tenant_id),
                OrderShipment.order_id == int(order.id),
                OrderShipment.tracking_data_source == TRACKING_SOURCE_SALLA,
            )
            .first()
            is not None
        )
        state = TRACKING_STORED_REFRESH_FAILED if has_stored else TRACKING_REFRESH_FAILED
        error_code = safe_exception_class(exc)
        _stamp_order_refresh(order, state=state, observed_via=observed_via, error_code=error_code)
        return TrackingRefreshResult(state, refreshed=False, error_code=error_code)

    if not tracked:
        _stamp_order_refresh(order, state=TRACKING_NO_DATA, observed_via=observed_via)
        return TrackingRefreshResult(TRACKING_NO_DATA, refreshed=True)

    # Existing schema intentionally supports one current shipment per order.
    # For multiple forward Salla shipments we surface the most recently dated
    # source event; return shipments are excluded above.
    candidate = max(
        tracked,
        key=lambda item: item["source_event_at"] or datetime.min.replace(tzinfo=timezone.utc),
    )
    try:
        shipment, _ = _apply_candidate(db, tenant_id=tenant_id, order=order, candidate=candidate)
    except Exception as exc:
        from core.coupon_log_privacy import safe_exception_class  # noqa: PLC0415

        _stamp_order_refresh(
            order,
            state=TRACKING_REFRESH_FAILED,
            observed_via=observed_via,
            error_code=safe_exception_class(exc),
        )
        return TrackingRefreshResult(TRACKING_REFRESH_FAILED, refreshed=False, error_code=safe_exception_class(exc))

    _stamp_order_refresh(order, state=TRACKING_AVAILABLE, observed_via=observed_via)
    return TrackingRefreshResult(TRACKING_AVAILABLE, refreshed=True, shipment_id=str(shipment.id))


async def refresh_tracking_from_salla_event(
    db: Any,
    *,
    tenant_id: int,
    adapter: Any,
    payload: dict[str, Any],
    observed_via: str,
) -> TrackingRefreshResult:
    """Handle Salla shipment webhooks using only provider shipment/order ids."""
    from models import Order  # noqa: PLC0415

    external_order_id = _as_text(payload.get("order_id"))
    shipment_id = _as_text(payload.get("id"))
    if not external_order_id or not shipment_id:
        return TrackingRefreshResult(TRACKING_NO_DATA, refreshed=False, error_code="shipment_event_missing_identifiers")
    order = (
        db.query(Order)
        .filter(Order.tenant_id == int(tenant_id), Order.external_id == external_order_id)
        .one_or_none()
    )
    if order is None:
        return TrackingRefreshResult(TRACKING_NO_DATA, refreshed=False, error_code="shipment_event_order_not_found")
    return await refresh_order_tracking(
        db,
        tenant_id=tenant_id,
        order=order,
        adapter=adapter,
        observed_via=observed_via,
        shipment_id=shipment_id,
    )


async def refresh_tenant_tracking(
    db: Any,
    *,
    tenant_id: int,
    adapter: Any,
    from_date: Optional[str],
    observed_via: str,
) -> dict[str, int]:
    """Poll Salla forward shipments once, then refresh their exact orders.

    The poller uses this after the normal order poll.  Listing is bounded by
    Salla's documented ``from_date`` filter; each resulting shipment is still
    verified against its tenant-scoped local order before persistence.
    """
    if not hasattr(adapter, "get_shipments"):
        return {"scanned": 0, "available": 0, "no_data": 0, "failed": 1}
    try:
        listed = await adapter.get_shipments(from_date=from_date)
    except Exception:
        return {"scanned": 0, "available": 0, "no_data": 0, "failed": 1}

    seen: set[tuple[str, str]] = set()
    stats = {"scanned": 0, "available": 0, "no_data": 0, "failed": 0}
    for item in listed or []:
        if not isinstance(item, dict):
            continue
        shipment_id = _as_text(item.get("id"))
        order_id = _as_text(item.get("order_id"))
        if not shipment_id or not order_id or (shipment_id, order_id) in seen:
            continue
        seen.add((shipment_id, order_id))
        stats["scanned"] += 1
        result = await refresh_tracking_from_salla_event(
            db,
            tenant_id=tenant_id,
            adapter=adapter,
            payload={"id": shipment_id, "order_id": order_id},
            observed_via=observed_via,
        )
        if result.state == TRACKING_AVAILABLE:
            stats["available"] += 1
        elif result.refreshed:
            stats["no_data"] += 1
        else:
            stats["failed"] += 1
    return stats


__all__ = [
    "TRACKING_AVAILABLE",
    "TRACKING_NO_DATA",
    "TRACKING_REFRESH_FAILED",
    "TRACKING_SOURCE_SALLA",
    "TRACKING_STORED_REFRESH_FAILED",
    "TrackingRefreshResult",
    "refresh_order_tracking",
    "refresh_tracking_from_salla_event",
    "refresh_tenant_tracking",
]
