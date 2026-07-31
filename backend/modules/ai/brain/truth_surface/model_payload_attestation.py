"""
model_payload_attestation.py
────────────────────────────
Fail-closed, redacted attestation for what reaches the final model call.

No phones, PII, raw prose, prices, titles, or secrets — ids/refs/counts/keys only.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence

from .contract import TrustedContextSnapshot

_FORBIDDEN_ATTESTATION_KEYS = frozenset({
    "facts",
    "title",
    "body",
    "message",
    "customer_phone",
    "phone",
    "code",
    "customer_name",
    "operational_name",
    "short_address",
    "maps_url",
    "product_url",
    "image_url",
    "cart_url",
    "price",
    "sale_price",
    "regular_price",
    "tracking_number",
    "external_id",
})

_SAFE_RESULT_DATA_KEYS = frozenset({
    "chosen_path",
    "turn_owner",
    "owner_locked",
    "navigator_owner",
    "compose_source",
    "surface",
})


def _domain_fact_counts(snapshot: TrustedContextSnapshot) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for fact in snapshot.facts or []:
        domain = str(getattr(fact.domain, "value", fact.domain) or "unknown")
        counts[domain] = counts.get(domain, 0) + 1
    return counts


def facts_loaded_from_snapshot(
    snapshot: Optional[TrustedContextSnapshot],
) -> Dict[str, Any]:
    """Domain keys / counts / snapshot id — no raw fact payloads."""
    if snapshot is None:
        return {"present": False}
    meta = snapshot.to_metadata()
    return {
        "present": True,
        "facts_snapshot_id": str(meta.get("snapshot_id") or snapshot.snapshot_id or ""),
        "loaded_domains": list(meta.get("loaded_domains") or snapshot.loaded_domains or []),
        "domain_fact_counts": _domain_fact_counts(snapshot),
        "fact_count": int(meta.get("fact_count") or len(snapshot.facts or [])),
    }


def _projection_domains_present(projection: Mapping[str, Any]) -> List[str]:
    domains: List[str] = []
    for key in (
        "product_identity",
        "product_candidates",
        "order",
        "shipment",
        "payment",
        "customer",
        "conversational_reference",
    ):
        value = projection.get(key)
        if isinstance(value, dict) and value:
            domains.append(key)
        elif isinstance(value, list) and value:
            domains.append(key)
    return domains


def facts_reaching_brain_from_projection(
    projection: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Projection presence + domain/product_id/candidate refs only."""
    if not isinstance(projection, Mapping) or not projection:
        return {"present": False}
    identity = projection.get("product_identity")
    product_id = None
    variant_id = None
    if isinstance(identity, dict):
        product_id = identity.get("product_id")
        variant_id = identity.get("variant_id")
    candidates = projection.get("product_candidates")
    candidate_count = len(candidates) if isinstance(candidates, list) else 0
    return {
        "present": True,
        "surface": str(projection.get("surface") or ""),
        "facts_snapshot_id": str(projection.get("facts_snapshot_id") or ""),
        "loaded_domains": list(projection.get("loaded_domains") or []),
        "domains_present": _projection_domains_present(projection),
        "product_id": product_id,
        "variant_id": variant_id,
        "candidate_count": candidate_count,
        "has_order": bool(projection.get("order")),
        "has_shipment": bool(projection.get("shipment")),
        "has_payment": bool(projection.get("payment")),
        "has_customer": bool(projection.get("customer")),
    }


def _candidate_ids_and_order_from_projection(
    projection: Optional[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    if not isinstance(projection, Mapping):
        return []
    rows = projection.get("product_candidates")
    if not isinstance(rows, list):
        return []
    out: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        entry: Dict[str, Any] = {}
        if row.get("ref") is not None:
            entry["ref"] = row.get("ref")
        if row.get("product_id") is not None:
            entry["product_id"] = row.get("product_id")
        if row.get("variant_id") is not None:
            entry["variant_id"] = row.get("variant_id")
        if entry:
            out.append(entry)
    return out


def candidate_ids_and_order_from_sources(
    *,
    projection: Optional[Mapping[str, Any]] = None,
    known_facts: Optional[Mapping[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Ordered candidate ids + refs only."""
    if isinstance(projection, Mapping) and projection:
        rows = _candidate_ids_and_order_from_projection(projection)
        if rows:
            return rows
    if isinstance(known_facts, Mapping):
        wired = known_facts.get("trusted_context_projection")
        if isinstance(wired, Mapping):
            return _candidate_ids_and_order_from_projection(wired)
    return []


def selected_product_and_variant_ids(
    selected_product: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Selected product + variant ids only."""
    if not isinstance(selected_product, Mapping) or not selected_product:
        return {"present": False}
    out: Dict[str, Any] = {"present": True}
    product_id = selected_product.get("product_id") or selected_product.get("id")
    if product_id is not None:
        out["product_id"] = product_id
    variant_id = selected_product.get("variant_id")
    if variant_id is not None:
        out["variant_id"] = variant_id
    if len(out) == 1:
        return {"present": False}
    return out


def facts_reaching_compose_from_known_facts(
    known_facts: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Known_facts projection keys / selected product ids present — no prose."""
    if not isinstance(known_facts, Mapping) or not known_facts:
        return {"present": False, "known_facts_keys": []}
    keys = sorted(str(key) for key in known_facts.keys())
    projection = known_facts.get("trusted_context_projection")
    projection_present = isinstance(projection, Mapping) and bool(projection)
    out: Dict[str, Any] = {
        "present": True,
        "known_facts_keys": keys,
        "trusted_context_projection_present": projection_present,
    }
    if projection_present:
        out["projection_domains_present"] = _projection_domains_present(projection)
        brain = facts_reaching_brain_from_projection(projection)
        out["projection_product_id"] = brain.get("product_id")
        out["projection_variant_id"] = brain.get("variant_id")
        out["projection_candidate_count"] = brain.get("candidate_count", 0)
    return out


def history_window_from_context(
    *,
    history: Optional[Sequence[Any]] = None,
    recent_turns: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Message count / chars only — never content."""
    history_count = len(history or [])
    history_chars = 0
    for turn in history or []:
        if isinstance(turn, Mapping):
            history_chars += len(str(turn.get("body") or ""))
        else:
            history_chars += len(str(turn or ""))
    recent = list(recent_turns or [])
    recent_chars = sum(len(str(item or "")) for item in recent)
    return {
        "history_message_count": history_count,
        "history_total_chars": history_chars,
        "recent_turns_count": len(recent),
        "recent_turns_total_chars": recent_chars,
    }


def tool_results_used_from_decision_result(
    *,
    decision_action: str = "",
    result_data: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Action names / safe result keys only — no payloads."""
    data = dict(result_data or {})
    safe_values = {
        key: data[key]
        for key in _SAFE_RESULT_DATA_KEYS
        if key in data and data[key] not in (None, "", [], {})
    }
    return {
        "decision_action": str(decision_action or ""),
        "result_data_key_count": len(data),
        "result_data_keys": sorted(str(key) for key in data.keys()),
        **safe_values,
    }


def model_and_route_from_compose_route(
    route: Optional[Any],
    *,
    premium_allowed: Optional[bool] = None,
) -> Dict[str, Any]:
    """Model name + route flags — no premium escalation."""
    if route is None:
        return {"present": False}
    out: Dict[str, Any] = {
        "present": True,
        "model": str(getattr(route, "model", "") or ""),
        "tier": str(getattr(route, "tier", "") or ""),
        "provider": str(getattr(route, "provider", "") or ""),
        "enforced": bool(getattr(route, "enforced", False)),
        "reason": str(getattr(route, "reason", "") or ""),
        "provider_hint": str(getattr(route, "provider_hint", "") or ""),
    }
    if premium_allowed is not None:
        out["premium_allowed"] = bool(premium_allowed)
    return out


def slim_compose_payload_fingerprint(
    slim_state: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Top-level slim compose keys + trusted projection ids only."""
    if not isinstance(slim_state, Mapping) or not slim_state:
        return {"present": False}
    known = slim_state.get("known_facts")
    known_keys: List[str] = []
    projection_product_id = None
    projection_variant_id = None
    projection_candidate_count = 0
    if isinstance(known, Mapping):
        known_keys = sorted(str(key) for key in known.keys())
        projection = known.get("trusted_context_projection")
        if isinstance(projection, Mapping):
            brain = facts_reaching_brain_from_projection(projection)
            projection_product_id = brain.get("product_id")
            projection_variant_id = brain.get("variant_id")
            projection_candidate_count = int(brain.get("candidate_count") or 0)
    selected = slim_state.get("selected_product")
    selected_ids = selected_product_and_variant_ids(
        selected if isinstance(selected, Mapping) else None,
    )
    return {
        "present": True,
        "state_top_level_keys": sorted(str(key) for key in slim_state.keys()),
        "known_facts_keys": known_keys,
        "projection_product_id": projection_product_id,
        "projection_variant_id": projection_variant_id,
        "projection_candidate_count": projection_candidate_count,
        "selected_product": selected_ids,
    }


def build_model_payload_attestation(
    *,
    stage: str,
    snapshot: Optional[TrustedContextSnapshot] = None,
    brain_projection: Optional[Mapping[str, Any]] = None,
    known_facts: Optional[Mapping[str, Any]] = None,
    selected_product: Optional[Mapping[str, Any]] = None,
    history: Optional[Sequence[Any]] = None,
    recent_turns: Optional[Sequence[str]] = None,
    decision_action: str = "",
    result_data: Optional[Mapping[str, Any]] = None,
    compose_route: Optional[Any] = None,
    premium_allowed: Optional[bool] = None,
    slim_compose_state: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Unified redacted model-input attestation for a pipeline stage."""
    projection = brain_projection
    if projection is None and isinstance(known_facts, Mapping):
        wired = known_facts.get("trusted_context_projection")
        if isinstance(wired, Mapping):
            projection = wired
    attestation: Dict[str, Any] = {
        "stage": str(stage or ""),
        "facts_loaded": facts_loaded_from_snapshot(snapshot),
        "facts_reaching_brain": facts_reaching_brain_from_projection(projection),
        "facts_reaching_compose": facts_reaching_compose_from_known_facts(known_facts),
        "candidate_ids_and_order": candidate_ids_and_order_from_sources(
            projection=projection,
            known_facts=known_facts,
        ),
        "selected_product_and_variant": selected_product_and_variant_ids(selected_product),
        "history_window": history_window_from_context(
            history=history,
            recent_turns=recent_turns,
        ),
        "tool_results_used": tool_results_used_from_decision_result(
            decision_action=decision_action,
            result_data=result_data,
        ),
        "model_and_route": model_and_route_from_compose_route(
            compose_route,
            premium_allowed=premium_allowed,
        ),
    }
    if slim_compose_state is not None:
        attestation["slim_compose_fingerprint"] = slim_compose_payload_fingerprint(
            slim_compose_state,
        )
    return attestation


def _collect_forbidden_keys(value: Any, *, path: str = "") -> List[str]:
    leaks: List[str] = []
    if isinstance(value, Mapping):
        for key, nested in value.items():
            key_str = str(key)
            full = f"{path}.{key_str}" if path else key_str
            if key_str in _FORBIDDEN_ATTESTATION_KEYS:
                leaks.append(full)
            leaks.extend(_collect_forbidden_keys(nested, path=full))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            leaks.extend(_collect_forbidden_keys(nested, path=f"{path}[{index}]"))
    return leaks


def assert_attestation_redacted(attestation: Mapping[str, Any]) -> None:
    """Fail-closed test helper — raises ValueError when unsafe keys appear."""
    leaks = _collect_forbidden_keys(attestation)
    if leaks:
        raise ValueError(f"attestation contains forbidden keys: {', '.join(leaks)}")


__all__ = [
    "assert_attestation_redacted",
    "build_model_payload_attestation",
    "candidate_ids_and_order_from_sources",
    "facts_loaded_from_snapshot",
    "facts_reaching_brain_from_projection",
    "facts_reaching_compose_from_known_facts",
    "history_window_from_context",
    "model_and_route_from_compose_route",
    "selected_product_and_variant_ids",
    "slim_compose_payload_fingerprint",
    "tool_results_used_from_decision_result",
]
