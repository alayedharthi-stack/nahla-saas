"""UTC clocks for the naive Coupon.expires_at column."""
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from core.salla_order_fidelity import _riyadh_tz


def utc_naive(value: datetime) -> datetime:
    """Persist an instant without letting the database session shift it."""
    aware = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    return aware.astimezone(timezone.utc).replace(tzinfo=None)


def provider_expiry_utc_naive(raw):
    """Parse a Salla coupon expiry; naive/date-only values are Riyadh time."""
    if raw in (None, ""):
        return None
    zone = "Asia/Riyadh"
    explicit_zone = isinstance(raw, dict) and bool(raw.get("timezone"))
    if isinstance(raw, dict):
        zone = raw.get("timezone") or zone
        raw = raw.get("date") or raw.get("datetime")
        if not raw:
            raise ValueError("provider_expiry_unverified")
    try:
        source_tz = _riyadh_tz() if zone == "Asia/Riyadh" else ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise ValueError("provider_expiry_unverified") from exc
    try:
        if isinstance(raw, datetime):
            value = raw
        elif isinstance(raw, date):
            value = datetime.combine(raw, time.min)
        else:
            value = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
    except (ValueError, TypeError) as exc:
        raise ValueError("provider_expiry_unverified") from exc
    if value.tzinfo is None:
        value = value.replace(tzinfo=source_tz)
    elif explicit_zone and value.utcoffset() != source_tz.utcoffset(value.replace(tzinfo=None)):
        raise ValueError("provider_expiry_unverified")
    return utc_naive(value)
