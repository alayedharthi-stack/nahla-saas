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
) -> Tuple[str, str]:
    """
    Build stable ``(source_event_id, transition_version)`` for ledger idempotency.

    Semantic identity (not webhook event-id) so StoreSync + webhook + retries
    of the same prev→curr transition collapse to one ledger key.

    ``source_event_id`` / ``transition_version`` both hash:
      provider + external_order_id + previous_status + current_status

    Provider ``updated_at`` and webhook ``event_id`` are audit-only — they must
    not split the same customer-relevant transition into duplicate deliveries.
    A genuinely new transition is a different prev→curr pair.
    """
    provider_key = str(provider or "").strip().lower()
    ext_id = str(external_order_id or "").strip()
    prev = normalize_status_slug(raw_previous_status) or None
    curr = normalize_status_slug(raw_current_status)

    semantic_payload = {
        "provider": provider_key,
        "external_order_id": ext_id,
        "raw_previous_status": prev,
        "raw_current_status": curr,
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
    "normalize_external_lifecycle_intent",
    "normalize_status_slug",
    "resolve_lifecycle_intent_normalizer",
]
