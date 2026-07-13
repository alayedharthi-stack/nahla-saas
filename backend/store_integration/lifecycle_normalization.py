"""
Provider-neutral external-store lifecycle transition contract (PR 2C).

Adapters own raw-status → BusinessIntent mapping. Core code must not branch
on provider names when resolving intents.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping, Optional, Tuple

from core.commerce_lifecycle.intents import BusinessIntent

logger = logging.getLogger("nahla.store_integration.lifecycle_normalization")

LifecycleIntentNormalizer = Callable[
    [Optional[str], str, Mapping[str, Any]],
    Optional[BusinessIntent],
]


@dataclass(frozen=True)
class ExternalLifecycleTransition:
    """Structured transition facts — no customer prose or stored URLs."""

    provider: str
    raw_previous_status: Optional[str]
    raw_current_status: str
    business_intent: BusinessIntent
    source_event_id: str
    transition_version: str
    order_id: int
    occurred_at: Optional[datetime]
    evidence_present: Tuple[str, ...]
    normalization_reason: str


def normalize_status_slug(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("slug") or value.get("name") or "").strip().lower()
    return str(value or "").strip().lower()


def _load_adapter_registry() -> dict[str, type]:
    try:
        import store_adapters.salla_adapter  # noqa: F401, PLC0415
    except ImportError:
        pass
    from store_integration.registry import _ADAPTER_REGISTRY  # noqa: PLC0415

    return dict(_ADAPTER_REGISTRY)


def resolve_lifecycle_intent_normalizer(
    provider: str,
) -> Optional[LifecycleIntentNormalizer]:
    """Return adapter-owned normalizer without branching on provider in callers."""
    registry = _load_adapter_registry()
    adapter_cls = registry.get(str(provider or "").strip().lower())
    if adapter_cls is None:
        return None
    fn = getattr(adapter_cls, "normalize_lifecycle_business_intent", None)
    if callable(fn):
        return fn
    return None


def normalize_external_lifecycle_intent(
    *,
    provider: str,
    raw_previous_status: Optional[str],
    raw_current_status: str,
    normalized_order: Mapping[str, Any],
) -> Tuple[Optional[BusinessIntent], str]:
    """
    Resolve BusinessIntent via the registered adapter.

    Returns ``(intent, normalization_reason)``. Unknown or no-op transitions
    return ``(None, reason)``.
    """
    prev = normalize_status_slug(raw_previous_status)
    curr = normalize_status_slug(raw_current_status)
    if not curr:
        return None, "missing_current_status"
    if prev and prev == curr:
        return None, "same_status_no_transition"

    normalizer = resolve_lifecycle_intent_normalizer(provider)
    if normalizer is None:
        return None, "adapter_normalizer_unavailable"

    try:
        intent = normalizer(raw_previous_status, raw_current_status, normalized_order)
    except Exception:
        logger.exception(
            "[LifecycleNorm] adapter normalizer failed provider=%s",
            str(provider or "").strip().lower(),
        )
        return None, "adapter_normalizer_error"

    if intent is None:
        return None, "unmapped_transition"
    return intent, "adapter_mapped"


def _extract_provider_updated_at(raw_payload: Optional[Mapping[str, Any]]) -> Optional[str]:
    if not raw_payload:
        return None
    for key in ("updated_at", "updated_date", "modified_at"):
        val = raw_payload.get(key)
        if isinstance(val, dict):
            val = val.get("date") or val.get("iso") or val.get("formatted")
        text = str(val or "").strip()
        if text:
            return text
    return None


def _extract_provider_event_id(raw_payload: Optional[Mapping[str, Any]]) -> Optional[str]:
    if not raw_payload:
        return None
    for key in ("event_id", "webhook_event_id", "webhook_id"):
        val = raw_payload.get(key)
        text = str(val or "").strip()
        if text:
            return text
    return None


def _extract_external_update_version(raw_payload: Optional[Mapping[str, Any]]) -> Optional[str]:
    if not raw_payload:
        return None
    for key in ("version", "update_id", "revision"):
        val = raw_payload.get(key)
        text = str(val or "").strip()
        if text:
            return text
    return None


def build_transition_identity(
    *,
    provider: str,
    external_order_id: str,
    raw_previous_status: Optional[str],
    raw_current_status: str,
    raw_payload: Optional[Mapping[str, Any]] = None,
) -> Tuple[str, str]:
    """
    Build stable ``(source_event_id, transition_version)`` for ledger idempotency.

    ``source_event_id`` priority:
      1. provider webhook/event id
      2. external order update id/version
      3. deterministic digest from provider + order id + statuses + updated_at

    ``transition_version`` uses provider ``updated_at`` when present; otherwise a
    deterministic digest of the transition components (never wall-clock alone).
    """
    provider_key = str(provider or "").strip().lower()
    ext_id = str(external_order_id or "").strip()
    prev = normalize_status_slug(raw_previous_status) or None
    curr = normalize_status_slug(raw_current_status)
    updated_at = _extract_provider_updated_at(raw_payload)

    event_id = _extract_provider_event_id(raw_payload)
    if event_id:
        source_event_id = event_id
    else:
        update_version = _extract_external_update_version(raw_payload)
        if update_version:
            source_event_id = f"upd:{update_version}"
        else:
            fallback_payload = {
                "provider": provider_key,
                "external_order_id": ext_id,
                "raw_previous_status": prev,
                "raw_current_status": curr,
                "provider_updated_at": updated_at,
            }
            canonical = json.dumps(
                fallback_payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            )
            source_event_id = f"ext:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"

    if updated_at:
        version_payload = {
            "provider_updated_at": updated_at,
            "raw_previous_status": prev,
            "raw_current_status": curr,
        }
        canonical = json.dumps(
            version_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        transition_version = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    else:
        version_payload = {
            "provider": provider_key,
            "external_order_id": ext_id,
            "raw_previous_status": prev,
            "raw_current_status": curr,
            "source_event_id": source_event_id,
        }
        canonical = json.dumps(
            version_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        transition_version = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    return source_event_id, transition_version


__all__ = [
    "ExternalLifecycleTransition",
    "LifecycleIntentNormalizer",
    "build_transition_identity",
    "normalize_external_lifecycle_intent",
    "normalize_status_slug",
    "resolve_lifecycle_intent_normalizer",
]
