"""Closed contract for read-only Knowledge Base source provenance operator."""
from __future__ import annotations

from typing import Any, Mapping

REPORT_SCHEMA_VERSION = "knowledge_source_provenance_v1"

CODE_COMMAND_INVALID = "command_invalid"
CODE_DATABASE_URL_MISSING = "database_url_missing"
CODE_TENANT_NOT_FOUND = "tenant_not_found"
CODE_DATABASE_ERROR = "database_error"

DIAGNOSTIC_GAP_NOTE = (
    "Live dashboard HTTP (e.g. GET /knowledge/sections) requires a merchant JWT. "
    "This operator substitutes authoritative DB-level provenance for the same tables "
    "and runtime builders the APIs use (no outbound HTTP, no browser, no secrets). "
    "Binding a personal UI session to a Network HAR is intentionally unsupported; "
    "use this CLI (or mint a test JWT via an approved admin diagnostic) instead."
)

API_SURFACE_MAP: dict[str, str] = {
    "knowledge_hub_get_sections": (
        "GET /knowledge/sections → merchant_knowledge_sections"
    ),
    "intelligence_merchant_brain_knowledge": (
        "GET /intelligence/merchant-brain/knowledge → build_merchant_context → "
        "store_knowledge_snapshots + tenant_settings.store_settings"
    ),
    "operations_branches": (
        "Operations Center branches → merchant_branches / branch_contacts / "
        "branch_escalation_steps"
    ),
}

STORE_SETTINGS_LENGTH_FIELDS: tuple[str, ...] = (
    "shipping_policy",
    "payment_policy",
    "return_policy",
    "warranty_policy",
    "delivery_areas",
    "working_hours",
)

MERCHANT_CONTEXT_POLICY_KEYS: tuple[str, ...] = (
    "shipping_policy",
    "payment_policy",
    "return_policy",
    "warranty_policy",
    "delivery_areas",
    "working_hours",
)

REQUIRED_TENANT_REPORT_KEYS: frozenset[str] = frozenset(
    {
        "report_schema_version",
        "tenant_id",
        "tenant_name",
        "api_surface_map",
        "counts",
        "structured_facts_probe",
        "merchant_context_probe",
        "divergence",
        "sample_section_ids",
        "diagnostic_gap_note",
    }
)


def text_length(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value.strip())
    return len(str(value).strip())


def faq_approved_count(store_settings: Mapping[str, Any] | None) -> int:
    if not isinstance(store_settings, Mapping):
        return 0
    faq = store_settings.get("faq_approved")
    if isinstance(faq, list):
        return len(faq)
    return 0


def store_settings_has_policy_or_faq(
    *,
    store_settings: Mapping[str, Any] | None,
    manual_knowledge_base_length: int,
) -> bool:
    if manual_knowledge_base_length > 0:
        return True
    if not isinstance(store_settings, Mapping):
        return False
    if faq_approved_count(store_settings) > 0:
        return True
    return any(text_length(store_settings.get(field)) > 0 for field in STORE_SETTINGS_LENGTH_FIELDS)


def snapshot_has_nonempty_policy_or_shipping(
    policy_summary: Mapping[str, Any] | None,
    shipping_summary: Mapping[str, Any] | None,
) -> bool:
    policy_keys = ("return_policy", "shipping_policy", "support_hours")
    shipping_keys = ("notes", "delivery_areas")
    if isinstance(policy_summary, Mapping):
        for key in policy_keys:
            if text_length(policy_summary.get(key)) > 0:
                return True
        payment_methods = policy_summary.get("payment_methods")
        if isinstance(payment_methods, list) and len(payment_methods) > 0:
            return True
    if isinstance(shipping_summary, Mapping):
        for key in shipping_keys:
            if text_length(shipping_summary.get(key)) > 0:
                return True
        methods = shipping_summary.get("methods")
        if isinstance(methods, list) and len(methods) > 0:
            return True
    return False


def compute_divergence(
    *,
    knowledge_hub_active_sections: int,
    intelligence_store_settings_has_policy_or_faq: bool,
    snapshot_has_nonempty_policy_or_shipping_text: bool,
    structured_facts_nonempty: bool,
    manual_knowledge_base_length: int,
) -> dict[str, bool]:
    knowledge_hub_has_sections = knowledge_hub_active_sections > 0
    divergence = {
        "knowledge_hub_has_sections": knowledge_hub_has_sections,
        "intelligence_store_settings_has_policy_or_faq": (
            intelligence_store_settings_has_policy_or_faq
        ),
        "snapshot_has_nonempty_policy_or_shipping_text": (
            snapshot_has_nonempty_policy_or_shipping_text
        ),
        "structured_facts_nonempty": structured_facts_nonempty,
        "sources_diverge": False,
    }
    hub_empty_other_nonempty = (not knowledge_hub_has_sections) and (
        intelligence_store_settings_has_policy_or_faq
        or snapshot_has_nonempty_policy_or_shipping_text
        or manual_knowledge_base_length > 0
        or structured_facts_nonempty
    )
    hub_nonempty_facts_empty = knowledge_hub_has_sections and not structured_facts_nonempty
    divergence["sources_diverge"] = bool(hub_empty_other_nonempty or hub_nonempty_facts_empty)
    return divergence


def store_settings_field_lengths(store_settings: Mapping[str, Any] | None) -> dict[str, int]:
    settings = store_settings if isinstance(store_settings, Mapping) else {}
    lengths = {field: text_length(settings.get(field)) for field in STORE_SETTINGS_LENGTH_FIELDS}
    lengths["faq_approved_count"] = faq_approved_count(settings)
    return lengths
