"""
coupon_sync_visibility.py
─────────────────────────
Shared helpers for Salla coupon import taxonomy and dashboard sync visibility.

Phase 1 only: metadata evidence + display normalization — no push/retry logic.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional, Tuple

SOURCE_LABEL_AR: Dict[str, str] = {
    "system": "نظام",
    "manual": "يدوي",
    "imported": "مستورد من سلة",
}

SYNC_BADGE_AR: Dict[str, str] = {
    "synced": "متزامن مع سلة",
    "not_pushed": "لم يُرسل إلى سلة",
    "failed": "فشل الإرسال",
    "imported": "مستورد من سلة",
}

_NAHALA_SYSTEM_SOURCES = frozenset({"auto", "pool", "automation", "system", "promotion"})
_IMPORT_META_SOURCES = frozenset({"salla", "zid", "imported"})
_DASHBOARD_MANUAL_SOURCES = frozenset({"dashboard", "manual"})
_VALID_SOURCE_TYPES = frozenset({"manual", "system", "imported"})


def extract_salla_coupon_id(raw: Dict[str, Any]) -> Optional[str]:
    for key in ("id", "coupon_id", "salla_coupon_id"):
        val = raw.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    return None


def _is_nahla_system_pool_coupon(
    source_type: Optional[str],
    existing_meta: Optional[Dict[str, Any]],
) -> bool:
    if source_type != "system":
        return False
    src = str((existing_meta or {}).get("source") or "").lower()
    return src in _NAHALA_SYSTEM_SOURCES


def is_nahla_origin_coupon(
    source_type: Optional[str],
    existing_meta: Optional[Dict[str, Any]],
) -> bool:
    """Nahla-originated coupons that must not be reclassified as Salla imports."""
    if source_type == "manual":
        src = str((existing_meta or {}).get("source") or "").lower()
        return src in _DASHBOARD_MANUAL_SOURCES or not src
    return _is_nahla_system_pool_coupon(source_type, existing_meta)


def is_nahla_system_coupon(
    source_type: Optional[str],
    existing_meta: Optional[Dict[str, Any]],
) -> bool:
    """Store-sync hook: preserve Nahla origin metadata on Salla reconcile."""
    return is_nahla_origin_coupon(source_type, existing_meta)


def build_salla_import_metadata(
    raw: Dict[str, Any],
    normalised: Dict[str, Any],
    synced_at: datetime,
) -> Dict[str, Any]:
    """Full import taxonomy for coupons created from a Salla list pull."""
    meta = dict(normalised)
    meta["source"] = "salla"
    meta["salla_synced"] = True
    meta["sync_status"] = "synced"
    meta["sync_direction"] = "salla_to_nahla"
    meta["last_synced_at"] = synced_at.isoformat()
    salla_id = extract_salla_coupon_id(raw)
    if salla_id:
        meta["salla_coupon_id"] = salla_id
        meta["external_id"] = salla_id
    return meta


def build_salla_reconcile_metadata(
    raw: Dict[str, Any],
    synced_at: datetime,
    existing_meta: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Sync evidence only — preserves Nahla origin metadata such as source=pool."""
    meta: Dict[str, Any] = {
        "salla_synced": True,
        "sync_status": "synced",
        "last_synced_at": synced_at.isoformat(),
    }
    prior_synced = (existing_meta or {}).get("salla_synced") is True
    prior_source = str((existing_meta or {}).get("source") or "").lower()
    if prior_synced and prior_source in _NAHALA_SYSTEM_SOURCES:
        meta["sync_direction"] = "nahla_to_salla"
    else:
        meta["sync_direction"] = "salla_to_nahla_seen"
    salla_id = extract_salla_coupon_id(raw)
    if salla_id:
        meta["salla_coupon_id"] = salla_id
        meta["external_id"] = salla_id
    return meta


def merge_salla_import_metadata(
    existing: Optional[Dict[str, Any]],
    import_meta: Dict[str, Any],
    *,
    preserve_origin: bool = False,
) -> Dict[str, Any]:
    merged = dict(existing or {})
    origin_source = merged.get("source") if preserve_origin else None
    merged.update(import_meta)
    if preserve_origin and origin_source is not None:
        merged["source"] = origin_source
    return merged


def should_mark_imported_source_type(
    existing_source_type: Optional[str],
    existing_meta: Optional[Dict[str, Any]],
) -> bool:
    """Preserve Nahla-originated coupons when the same code is re-synced from Salla."""
    return not is_nahla_origin_coupon(existing_source_type, existing_meta)


def resolve_coupon_source_type(
    *,
    column_source_type: Optional[str],
    meta: Dict[str, Any],
    origin: str,
) -> str:
    """Single source of truth for manual/system/imported filter chips and counts."""
    meta_source = str(meta.get("source") or "").lower()
    col = str(column_source_type or "").strip().lower()

    if meta_source in _DASHBOARD_MANUAL_SOURCES:
        return "manual"

    if col == "imported" or (
        meta_source in _IMPORT_META_SOURCES
        and str(meta.get("sync_direction") or "").lower() == "salla_to_nahla"
    ):
        return "imported"

    if is_nahla_system_coupon(col or None, meta) or col == "system":
        return "system"

    if meta_source in _NAHALA_SYSTEM_SOURCES:
        return "system"

    if origin in ("automation", "promotion", "vip", "widget"):
        return "system"

    if col in _VALID_SOURCE_TYPES:
        return col

    return "manual"


def compute_source_type_counts(coupons: list[Dict[str, Any]]) -> Dict[str, int]:
    counts = {"all": len(coupons), "system": 0, "manual": 0, "imported": 0}
    for coupon in coupons:
        source_type = str(coupon.get("source_type") or "manual")
        if source_type in counts:
            counts[source_type] += 1
    return counts


def _coerce_used_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text == "true":
        return True
    if text == "false":
        return False
    return None


def _has_numeric_usage_count(meta: Dict[str, Any]) -> bool:
    usage_count = meta.get("usage_count")
    if usage_count is None:
        return False
    text = str(usage_count).strip()
    if not text:
        return False
    try:
        float(text.replace(",", "."))
        return True
    except ValueError:
        return False


def normalize_coupon_usage_display(meta: Dict[str, Any]) -> Tuple[int, int]:
    """Display-only normalization for pool coupons using metadata.used."""
    if "used" in meta and not _has_numeric_usage_count(meta):
        used = _coerce_used_bool(meta.get("used"))
        if used is not None:
            return (1 if used else 0, 1)

    usages = int(meta.get("usage_count") or 0)
    limit = int(meta.get("usage_limit") or 0)
    return usages, limit


def derive_source_label(source_type: str, meta: Dict[str, Any]) -> str:
    if source_type == "imported":
        return SOURCE_LABEL_AR["imported"]
    if source_type == "system":
        return SOURCE_LABEL_AR["system"]
    if str(meta.get("source") or "").lower() == "salla":
        return SOURCE_LABEL_AR["imported"]
    return SOURCE_LABEL_AR.get(source_type, SOURCE_LABEL_AR["manual"])


def derive_sync_badge(source_type: str, meta: Dict[str, Any]) -> str:
    sync_status = str(meta.get("sync_status") or "").lower()
    if sync_status == "failed":
        return "failed"

    if source_type == "imported":
        return "imported"

    salla_synced = meta.get("salla_synced") is True or sync_status == "synced"
    if salla_synced and source_type == "system":
        return "synced"

    if (
        str(meta.get("source") or "").lower() == "salla"
        and str(meta.get("sync_direction") or "").lower() == "salla_to_nahla"
    ):
        return "imported"

    if salla_synced:
        return "synced"

    return "not_pushed"


def derive_coupon_sync_visibility(
    *,
    source_type: str,
    meta: Dict[str, Any],
) -> Dict[str, Any]:
    sync_badge = derive_sync_badge(source_type, meta)
    sync_status = meta.get("sync_status")
    salla_synced = bool(
        meta.get("salla_synced") is True
        or str(sync_status or "").lower() == "synced"
    )
    return {
        "source_type": source_type,
        "source_label": derive_source_label(source_type, meta),
        "salla_synced": salla_synced,
        "sync_status": sync_status,
        "sync_error": meta.get("sync_error"),
        "last_synced_at": meta.get("last_synced_at"),
        "salla_coupon_id": meta.get("salla_coupon_id") or meta.get("external_id"),
        "sync_direction": meta.get("sync_direction"),
        "sync_badge": sync_badge,
        "sync_badge_label": SYNC_BADGE_AR.get(sync_badge, SYNC_BADGE_AR["not_pushed"]),
    }
