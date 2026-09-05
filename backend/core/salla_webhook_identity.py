"""Deterministic Salla webhook persist identity.

Exact provider retries with the same transition facts stay one row.
Later distinct status or payment transitions for the same order do not.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Optional

from store_integration.lifecycle_normalization import (
    canonicalize_provider_timestamp,
    normalize_status_slug,
)


def _provider_event_id(payload: Mapping[str, Any]) -> str:
    for key in ("event_id", "webhook_event_id", "webhook_id"):
        text = str(payload.get(key) or "").strip()
        if text:
            return text
    return ""


def _payment_slug(value: Any) -> str:
    if isinstance(value, dict):
        return normalize_status_slug(
            value.get("status") or value.get("slug") or value.get("name")
        )
    return normalize_status_slug(value)


def _updated_at(payload: Mapping[str, Any], data: Mapping[str, Any]) -> str:
    for blob in (data, payload):
        for key in ("updated_at", "updated_date", "modified_at", "date"):
            canonical = canonicalize_provider_timestamp(blob.get(key))
            if canonical:
                return canonical
    return ""


def build_salla_webhook_external_event_id(
    *,
    event_type: Optional[str],
    parsed_payload: Optional[Mapping[str, Any]],
    provider_prefix: str = "salla",
) -> Optional[str]:
    """Build persist identity for one Salla webhook delivery.

    Preference order:
      1. Provider webhook/event id (exact replay of the same delivery)
      2. Deterministic digest of event type + entity + status + payment + updated_at
    """
    if not parsed_payload or not event_type:
        return None
    if not isinstance(parsed_payload, Mapping):
        return None

    prefix = str(provider_prefix or "salla").strip() or "salla"
    provider_event_id = _provider_event_id(parsed_payload)
    if provider_event_id:
        return f"{prefix}:{event_type}:{provider_event_id}"

    data = parsed_payload.get("data") or {}
    if not isinstance(data, Mapping):
        data = {}
    entity_id = data.get("id")
    if entity_id is None:
        return None

    status = normalize_status_slug(data.get("status"))
    payment = _payment_slug(data.get("payment") or data.get("payment_status"))
    updated_at = _updated_at(parsed_payload, data)
    canonical = json.dumps(
        {
            "event": str(event_type),
            "entity_id": str(entity_id),
            "status": status,
            "payment": payment,
            "updated_at": updated_at,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
    return f"{prefix}:{event_type}:{entity_id}:{digest}"


__all__ = ["build_salla_webhook_external_event_id"]
