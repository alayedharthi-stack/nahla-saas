"""Closed reply-metadata export contract for Brain compose boundaries."""
from __future__ import annotations

from typing import Any, Dict, Mapping, MutableMapping, Optional, Tuple

from modules.ai.compose.constitutional_policy import (
    APPROVED_COMPOSE_SOURCES,
    FALLBACK_METADATA_KEYS,
    REQUIRED_REPLY_METADATA_KEYS,
)

_DETERMINISTIC_COMPOSE_SOURCES = frozenset(
    {
        "fallback_deterministic",
        "merchant_template",
        "meta_template",
        "legal_exact_text",
        "security_exact_text",
    }
)
_LLM_COMPOSE_SOURCES = frozenset({"llm", "persona_llm"})

BRAIN_REPLY_METADATA_EXPORT_KEYS: Tuple[str, ...] = (
    *REQUIRED_REPLY_METADATA_KEYS,
    "final_customer_text_source",
)

PERSONA_ROUTE_PROVENANCE_FIELDS: Tuple[str, ...] = (
    "route_provider",
    "route_model",
    "route_tier",
    "route_source",
    "route_provider_configured",
    "compose_attempt",
)

PERSONA_INTEGRATION_PASS_THROUGH_KEYS: Tuple[str, ...] = (
    "knowledge_source",
    "kb_section_ids",
    "question_kind",
    "catalog_product_id",
    "catalog_product_ids",
    "variant_ids",
    "price_source",
    "availability_source",
    "category_scope",
    "allowed_category",
    "catalog_search_query",
    "search_result_count",
    "checkout_pressure_allowed",
    "catalog_fact_products",
    "catalog_fact_products_len",
    "catalog_fact_product_ids",
    "catalog_fact_price_values",
    "catalog_fact_rebuild_source",
    *BRAIN_REPLY_METADATA_EXPORT_KEYS,
    "trusted_coupon_offer_compose_active",
    "customer_conditional_coupon_compose_active",
    "customer_conditional_coupon_general_llm_fallthrough",
    "conditional_coupon_guard_failed_reason",
    "general_offer_discovery_compose_active",
    "product_sale_offer_compose_active",
    "track_order_need_identifiers_compose_active",
    "track_order_need_identifiers",
    "facts_snapshot_id",
)


def approved_compose_source(value: object) -> str:
    src = str(value or "").strip()
    if src in APPROVED_COMPOSE_SOURCES:
        return src
    return ""


def map_persona_nested_source_to_compose_source(nested_source: object) -> str:
    """Map ``persona_compose.source`` only when it is a closed approved source."""
    return approved_compose_source(nested_source)


def apply_persona_nested_compose_source_to_event(
    event: MutableMapping[str, Any],
    persona_compose: Optional[Mapping[str, Any]],
) -> None:
    """Apply nested persona source without inferring from reply text or upgrading deterministic."""
    if not isinstance(persona_compose, Mapping):
        return
    mapped = map_persona_nested_source_to_compose_source(persona_compose.get("source"))
    if not mapped:
        return
    existing = approved_compose_source(event.get("compose_source"))
    if not existing:
        event["compose_source"] = mapped
        return
    if existing in _DETERMINISTIC_COMPOSE_SOURCES and mapped in _LLM_COMPOSE_SOURCES:
        return


def stamp_general_llm_compose_metadata(
    target: MutableMapping[str, Any],
    *,
    llm_candidate: str,
    chosen_path: str = "llm",
    response_mode: str = "llm",
) -> None:
    """Stamp constitutional metadata at the general ``_llm_compose`` producer boundary."""
    candidate = str(llm_candidate or "")
    target["compose_source"] = "llm"
    target["response_mode"] = str(response_mode or "llm").strip() or "llm"
    target["chosen_path"] = str(chosen_path or "llm").strip() or "llm"
    target["llm_candidate_present"] = bool(candidate.strip())
    target["final_text_transformed"] = False
    target["final_transform_reasons"] = []
    target["final_customer_text_source"] = "llm"
    target["fallback_reason"] = ""
    target["fallback_action_type"] = ""


def extract_persona_route_provenance(
    brain_persona_compose_event: Optional[Mapping[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Export bounded persona route fields from nested ``persona_compose`` only."""
    if not isinstance(brain_persona_compose_event, Mapping):
        return None
    persona_compose = brain_persona_compose_event.get("persona_compose")
    if not isinstance(persona_compose, Mapping):
        return None
    if "compose_attempt" not in persona_compose:
        return None

    exported: Dict[str, Any] = {}
    for key in PERSONA_ROUTE_PROVENANCE_FIELDS:
        if key not in persona_compose:
            return None
        value = persona_compose[key]
        if key == "route_provider_configured":
            if type(value) is not bool:
                return None
            exported[key] = value
            continue
        token = str(value or "").strip()
        if key == "compose_attempt" and not token:
            return None
        exported[key] = token
    return exported


def extract_reply_metadata_export(
    result_data: Optional[Mapping[str, Any]],
    *,
    chosen_path: str = "",
) -> Dict[str, Any]:
    """Extract structured reply metadata from compose ``result.data`` for Brain export."""
    if not isinstance(result_data, Mapping):
        return {}

    exported: Dict[str, Any] = {}
    for key in BRAIN_REPLY_METADATA_EXPORT_KEYS:
        if key in result_data and result_data.get(key) is not None:
            exported[key] = result_data[key]

    compose_source = approved_compose_source(exported.get("compose_source"))
    if not compose_source:
        exported.pop("compose_source", None)
    if compose_source == "fallback_deterministic":
        for key in FALLBACK_METADATA_KEYS:
            if key in result_data and result_data.get(key) is not None:
                exported[key] = result_data[key]

    if not compose_source:
        pc = result_data.get("persona_compose")
        if isinstance(pc, Mapping):
            mapped = map_persona_nested_source_to_compose_source(pc.get("source"))
            if mapped:
                exported["compose_source"] = mapped

    authoritative_path = str(chosen_path or "").strip()
    if authoritative_path:
        exported["chosen_path"] = authoritative_path
    return exported


__all__ = [
    "BRAIN_REPLY_METADATA_EXPORT_KEYS",
    "PERSONA_INTEGRATION_PASS_THROUGH_KEYS",
    "PERSONA_ROUTE_PROVENANCE_FIELDS",
    "apply_persona_nested_compose_source_to_event",
    "approved_compose_source",
    "extract_persona_route_provenance",
    "extract_reply_metadata_export",
    "map_persona_nested_source_to_compose_source",
    "stamp_general_llm_compose_metadata",
]
