"""Parse Salla nested and flat datetime values to aware UTC."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

_EN_MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}

try:
    from zoneinfo import ZoneInfo
except Exception:  # noqa: BLE001
    ZoneInfo = None  # type: ignore[misc, assignment]

SALLA_DEFAULT_TZ = "Asia/Riyadh"


def _riyadh_tz() -> timezone:
    if ZoneInfo is not None:
        try:
            return ZoneInfo(SALLA_DEFAULT_TZ)  # type: ignore[return-value]
        except Exception:
            pass
    return timezone(timedelta(hours=3))


def _resolve_tz(tz_name: str) -> timezone:
    name = str(tz_name or SALLA_DEFAULT_TZ).strip() or SALLA_DEFAULT_TZ
    if ZoneInfo is not None:
        try:
            return ZoneInfo(name)  # type: ignore[return-value]
        except Exception:
            pass
    if name == SALLA_DEFAULT_TZ:
        return _riyadh_tz()
    return timezone.utc


def _iso_variants(text: str) -> list[str]:
    variants = [text.replace("Z", "+00:00").replace("z", "+00:00")]
    variants.append(text.replace(" ", "T", 1))
    if "." in text:
        variants.append(text.split(".", 1)[0].replace(" ", "T", 1))
    return variants


def _is_explicit_utc_marker(text: str) -> bool:
    lowered = text.lower()
    return lowered.endswith("z") or lowered.endswith("+00:00") or lowered.endswith("-00:00")


def _has_fixed_offset(text: str) -> bool:
  tail = text[-6:]
  return len(tail) >= 6 and tail[0] in "+-" and tail[3] == ":"


def _parse_text_to_utc(text: str, tz_name: str) -> Optional[datetime]:
    text = str(text or "").strip()
    if not text:
        return None
    if _is_explicit_utc_marker(text):
        for variant in _iso_variants(text):
            try:
                dt = datetime.fromisoformat(variant)
            except ValueError:
                continue
            if dt.tzinfo is not None:
                return dt.astimezone(timezone.utc)
            return dt.replace(tzinfo=timezone.utc)
        return None
    if _has_fixed_offset(text):
        for variant in _iso_variants(text):
            try:
                dt = datetime.fromisoformat(variant)
            except ValueError:
                continue
            if dt.tzinfo is not None:
                return dt.astimezone(timezone.utc)
        return None
    for variant in _iso_variants(text):
        try:
            dt = datetime.fromisoformat(variant)
        except ValueError:
            continue
        if dt.tzinfo is not None:
            return dt.astimezone(timezone.utc)
        local_tz = _resolve_tz(tz_name)
        return dt.replace(tzinfo=local_tz).astimezone(timezone.utc)
    return None


def parse_salla_js_envelope_datetime(value: Any) -> Optional[datetime]:
    """Parse Salla webhook envelope created_at (JS GMT offset), locale-independent."""
    text = str(value or "").strip()
    parts = text.split()
    if len(parts) != 6 or not parts[5].startswith("GMT"):
        return None
    month = _EN_MONTHS.get(parts[1])
    offset_raw = parts[5][3:]
    if month is None or len(offset_raw) != 5 or offset_raw[0] not in "+-":
        return None
    try:
        day = int(parts[2])
        year = int(parts[3])
        hh, mm, ss = (int(x) for x in parts[4].split(":"))
        sign = 1 if offset_raw[0] == "+" else -1
        off_h = int(offset_raw[1:3])
        off_m = int(offset_raw[3:5])
    except (TypeError, ValueError):
        return None
    tz = timezone(timedelta(hours=sign * off_h, minutes=sign * off_m))
    return datetime(year, month, day, hh, mm, ss, tzinfo=tz).astimezone(timezone.utc)


def parse_salla_datetime_to_utc(value: Any) -> Optional[datetime]:
    """Return timezone-aware UTC datetime or None when input is invalid."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, dict):
        date_raw = value.get("date") or value.get("iso") or value.get("formatted")
        if not date_raw:
            return None
        tz_name = str(value.get("timezone") or SALLA_DEFAULT_TZ).strip() or SALLA_DEFAULT_TZ
        return _parse_text_to_utc(str(date_raw), tz_name)
    if isinstance(value, str):
        js_dt = parse_salla_js_envelope_datetime(value)
        if js_dt is not None:
            return js_dt
        # Flat naive strings follow the existing store-sync contract: UTC.
        return _parse_text_to_utc(value, "UTC")
    return None


def salla_datetime_to_utc_iso(value: Any) -> str:
    dt = parse_salla_datetime_to_utc(value)
    if dt is None:
        return ""
    return dt.astimezone(timezone.utc).replace(tzinfo=None).isoformat()


def salla_datetime_to_naive_utc(value: Any) -> Optional[datetime]:
    dt = parse_salla_datetime_to_utc(value)
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


__all__ = [
    "parse_salla_datetime_to_utc",
    "parse_salla_js_envelope_datetime",
    "salla_datetime_to_naive_utc",
    "salla_datetime_to_utc_iso",
]
