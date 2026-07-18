"""Finalize constitutional provenance for track_order_need_order_number compose."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, MutableMapping, Optional

_TRACKER_KEY = "_track_order_need_identifiers_provenance_tracker"

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
    "track_order_need_identifiers_compose_active",
    "track_order_need_identifiers",
)


@dataclass
class _TrackOrderNeedIdentifiersProvenanceTracker:
    candidate: str
    reasons: list[str] = field(default_factory=list)


def _compose_active(result_data: Mapping[str, Any]) -> bool:
    return bool(result_data.get("track_order_need_identifiers_compose_active"))


def begin_track_order_need_identifiers_text_tracking(
    result_data: MutableMapping[str, Any],
    compose_text: str,
) -> bool:
    if not _compose_active(result_data):
        return False
    result_data[_TRACKER_KEY] = _TrackOrderNeedIdentifiersProvenanceTracker(
        candidate=str(compose_text or ""),
    )
    return True


def finalize_track_order_need_identifiers_text_provenance(
    result_data: MutableMapping[str, Any],
    final_text: str,
    *,
    guard_replaced: Optional[Mapping[str, bool]] = None,
) -> None:
    tracker = result_data.pop(_TRACKER_KEY, None)
    if not isinstance(tracker, _TrackOrderNeedIdentifiersProvenanceTracker):
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
        result_data["final_customer_text_source"] = "llm_postprocess"
    elif compose_source == "llm":
        result_data["final_customer_text_source"] = "llm"


def note_track_order_need_identifiers_dedup_substitution(
    target: MutableMapping[str, Any],
    *,
    before: str,
    after: str,
) -> None:
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
    "begin_track_order_need_identifiers_text_tracking",
    "extract_constitutional_metadata",
    "finalize_track_order_need_identifiers_text_provenance",
    "note_track_order_need_identifiers_dedup_substitution",
]
