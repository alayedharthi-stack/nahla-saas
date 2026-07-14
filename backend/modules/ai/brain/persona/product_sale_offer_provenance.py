"""Finalize constitutional provenance for product_sale_offer / general_offer_discovery compose."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, MutableMapping, Optional

_TRACKER_KEY = "_product_sale_offer_provenance_tracker"

CONSTITUTIONAL_METADATA_KEYS = (
    "compose_source",
    "response_mode",
    "chosen_path",
    "llm_candidate_present",
    "final_text_transformed",
    "final_transform_reasons",
    "final_customer_text_source",
    "fallback_reason",
    "fallback_action_type",
    "product_sale_offer_compose_active",
    "general_offer_discovery_compose_active",
    "facts_snapshot_id",
)


@dataclass
class _ProductSaleOfferProvenanceTracker:
    candidate: str
    reasons: list[str] = field(default_factory=list)


def _compose_active(result_data: Mapping[str, Any]) -> bool:
    return bool(
        result_data.get("product_sale_offer_compose_active")
        or result_data.get("general_offer_discovery_compose_active")
    )


def begin_product_sale_offer_text_tracking(
    result_data: MutableMapping[str, Any],
    compose_text: str,
) -> bool:
    """Snapshot compose candidate text when product sale compose is active."""
    if not _compose_active(result_data):
        return False
    result_data[_TRACKER_KEY] = _ProductSaleOfferProvenanceTracker(
        candidate=str(compose_text or ""),
    )
    return True


def note_product_sale_offer_text_change(
    result_data: MutableMapping[str, Any],
    *,
    before: str,
    after: str,
    reason: str,
) -> None:
    """Record a post-compose text mutation for product sale provenance."""
    tracker = result_data.get(_TRACKER_KEY)
    if not isinstance(tracker, _ProductSaleOfferProvenanceTracker):
        return
    if (before or "") != (after or ""):
        reason = str(reason or "").strip()
        if reason and reason not in tracker.reasons:
            tracker.reasons.append(reason)


def finalize_product_sale_offer_text_provenance(
    result_data: MutableMapping[str, Any],
    final_text: str,
    *,
    guard_replaced: Optional[Mapping[str, bool]] = None,
) -> None:
    """Write ``final_text_transformed`` / ``final_transform_reasons`` on result data."""
    tracker = result_data.pop(_TRACKER_KEY, None)
    if not isinstance(tracker, _ProductSaleOfferProvenanceTracker):
        return

    reasons = list(tracker.reasons)
    for name, fired in (guard_replaced or {}).items():
        if fired:
            guard_name = str(name or "").strip()
            if guard_name and guard_name not in reasons:
                reasons.append(guard_name)

    candidate = tracker.candidate
    final = str(final_text or "")
    transformed = bool(reasons) or candidate != final
    result_data["final_text_transformed"] = transformed
    result_data["final_transform_reasons"] = reasons if transformed else []

    compose_source = str(result_data.get("compose_source") or "")
    guard_names = [
        str(name or "").strip()
        for name, fired in (guard_replaced or {}).items()
        if fired and str(name or "").strip()
    ]
    if compose_source == "fallback_deterministic":
        result_data["final_customer_text_source"] = "fallback_deterministic"
    elif guard_names and candidate.strip() != final.strip():
        result_data["final_customer_text_source"] = "guard_rewrite"
    elif "chat_dedup_substitution" in reasons:
        result_data["final_customer_text_source"] = "dedup_substitution"
    elif transformed:
        result_data["final_customer_text_source"] = "persona_llm_postprocess"
    else:
        result_data["final_customer_text_source"] = "persona_llm"


def note_product_sale_offer_dedup_substitution(
    target: MutableMapping[str, Any],
    *,
    before: str,
    after: str,
) -> None:
    """Record webhook dedup substitution on constitutional provenance metadata."""
    if not _compose_active(target):
        return
    if (before or "") == (after or ""):
        return
    reasons = [str(r) for r in (target.get("final_transform_reasons") or []) if r]
    if "chat_dedup_substitution" not in reasons:
        reasons.append("chat_dedup_substitution")
    target["final_text_transformed"] = True
    target["final_transform_reasons"] = reasons
    target["final_customer_text_source"] = "dedup_substitution"


def extract_constitutional_metadata(
    source: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    if not isinstance(source, Mapping):
        return {}
    if not _compose_active(source):
        return {}
    return {
        key: source[key]
        for key in CONSTITUTIONAL_METADATA_KEYS
        if source.get(key) is not None
    }


__all__ = [
    "CONSTITUTIONAL_METADATA_KEYS",
    "begin_product_sale_offer_text_tracking",
    "extract_constitutional_metadata",
    "finalize_product_sale_offer_text_provenance",
    "note_product_sale_offer_dedup_substitution",
    "note_product_sale_offer_text_change",
]
