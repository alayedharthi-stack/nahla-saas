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
from datetime import datetime, timezone
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


def canonicalize_provider_timestamp(value: Any) -> Optional[str]:
    """
    Canonical UTC timestamp for transition identity only.

    Parses ISO-8601 provider timestamps, normalizes to
    ``YYYY-MM-DDTHH:MM:SS.ffffffZ``. Naive timestamps are rejected — they are
    not assumed to be local or server time. Invalid strings return ``None`` so
    identity uses the documented deterministic fallback (never wall-clock).
    """
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("date") or value.get("iso") or value.get("formatted")
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    utc = dt.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond:06d}Z"


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


def resolve_customer_relevant_state(
    *,
    provider: str,
    raw_status: Any,
) -> str:
    """Adapter-owned current-state bucket used in semantic delivery identity."""
    slug = normalize_status_slug(raw_status)
    registry = _load_adapter_registry()
    adapter_cls = registry.get(str(provider or "").strip().lower())
    if adapter_cls is not None:
        fn = getattr(adapter_cls, "normalize_lifecycle_customer_state", None)
        if callable(fn):
            try:
                bucket = fn(raw_status)
            except Exception:
                logger.exception(
                    "[LifecycleNorm] customer-state normalizer failed provider=%s",
                    str(provider or "").strip().lower(),
                )
                return slug
            text = normalize_status_slug(bucket)
            if text:
                return text
    return slug


def extract_provider_order_version(
    *,
    normalized_order: Optional[Mapping[str, Any]] = None,
    raw_payload: Optional[Mapping[str, Any]] = None,
) -> Optional[str]:
    """
    Audit-only Salla/order version. Not used as the semantic delivery key
    because webhook vs poller payloads often carry observation timestamps
    in ``updated_at`` rather than a shared transition id.
    """
    candidates: list[Any] = []
    order_map = dict(normalized_order or {})
    payload = dict(raw_payload or {})
    nested_order = payload.get("order") if isinstance(payload.get("order"), dict) else {}
    nested_data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    for source in (order_map, nested_order, nested_data, payload):
        if not isinstance(source, dict):
            continue
        for key in ("order_updated_at", "salla_updated_at"):
            if source.get(key):
                candidates.append(source.get(key))
    for value in candidates:
        canonical = canonicalize_provider_timestamp(value)
        if canonical:
            return canonical
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


def build_transition_identity(
    *,
    provider: str,
    external_order_id: str,
    raw_previous_status: Optional[str],
    raw_current_status: str,
    raw_payload: Optional[Mapping[str, Any]] = None,
    business_intent: Optional[Any] = None,
    prior_customer_state: Optional[str] = None,
    normalized_order: Optional[Mapping[str, Any]] = None,
) -> Tuple[str, str]:
    """
    Build stable ``(source_event_id, transition_version)`` for ledger idempotency.

    RAW observer identity (previous_status, webhook event_id, observation
    timestamps) is audit-only. Semantic customer-delivery identity hashes:

      provider + external_order_id + business_intent + customer_state
      + persisted prior_customer_state

    ``raw_previous_status`` must not split webhook vs poller views of the
    same actual transition. A later legitimate recurrence is a different
    ``prior_customer_state`` (the last customer-relevant state Nahla already
    notified), not a different observer previous_status.

    Provider ``updated_at`` / event ids remain audit-only.
    """
    provider_key = str(provider or "").strip().lower()
    ext_id = str(external_order_id or "").strip()
    current_state = resolve_customer_relevant_state(
        provider=provider_key,
        raw_status=raw_current_status,
    )
    intent_value = None
    if business_intent is not None:
        intent_value = getattr(business_intent, "value", None) or str(business_intent)
        intent_value = str(intent_value).strip() or None
    prior = normalize_status_slug(prior_customer_state) or None
    # Observer previous_status is mapping evidence only — never a ledger key.
    _observer_previous_status = raw_previous_status
    del _observer_previous_status

    semantic_payload = {
        "provider": provider_key,
        "external_order_id": ext_id,
        "business_intent": intent_value,
        "customer_state": current_state or None,
        "prior_customer_state": prior,
    }
    canonical = json.dumps(
        semantic_payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    source_event_id = f"sem:{digest}"
    transition_version = digest
    return source_event_id, transition_version


__all__ = [
    "ExternalLifecycleTransition",
    "LifecycleIntentNormalizer",
    "build_transition_identity",
    "canonicalize_provider_timestamp",
    "extract_provider_order_version",
    "normalize_external_lifecycle_intent",
    "normalize_status_slug",
    "resolve_customer_relevant_state",
    "resolve_lifecycle_intent_normalizer",
]
