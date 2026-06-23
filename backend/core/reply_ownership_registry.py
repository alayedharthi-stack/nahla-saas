"""
reply_ownership_registry.py
─────────────────────────────
Platform-wide inventory of deterministic reply paths and their
recommended migration class under Nahla doctrine.

Class A — keep deterministic decision (evidence/state/routing).
Class B — move all customer-facing copy to Brain/LLM.
Class C — hybrid: deterministic decision + constrained LLM wording.

This module is observability/documentation — no reply text is emitted.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple

CLASS_A = "A"
CLASS_B = "B"
CLASS_C = "C"


@dataclass(frozen=True)
class ReplyPathEntry:
    path: str
    layer: str  # pre_brain | inside_brain | postprocess | fallback
    copy_source: str
    why_exists: str
    migration_class: str
    keep_decision: bool = True


REPLY_PATH_REGISTRY: Tuple[ReplyPathEntry, ...] = (
    # ── Pre-Brain webhook short-circuits ─────────────────────────────────
    ReplyPathEntry(
        "payment_evidence_soft_ack",
        layer="pre_brain",
        copy_source="core.payment_evidence.compose_payment_evidence_reply",
        why_exists="Block false payment confirmation for ambiguous bank/QR media",
        migration_class=CLASS_C,
    ),
    ReplyPathEntry(
        "payment_claim_ack",
        layer="pre_brain",
        copy_source="core.payment_intent.compose_payment_claim_ack",
        why_exists="Text-only payment claim without receipt media",
        migration_class=CLASS_C,
    ),
    ReplyPathEntry(
        "payment_receipt_ack",
        layer="pre_brain",
        copy_source="core.order_flow._compose_receipt_ack",
        why_exists="Confirmed receipt evidence + order state advance",
        migration_class=CLASS_C,
    ),
    ReplyPathEntry(
        "map_image_ack",
        layer="pre_brain",
        copy_source="core.order_flow.maybe_handle_map_image_inbound",
        why_exists="Map screenshot lacks coordinates — ask parseable address",
        migration_class=CLASS_C,
    ),
    ReplyPathEntry(
        "address_ingest_ack",
        layer="pre_brain",
        copy_source="core.order_flow.compose_address_reply",
        why_exists="Address ingested during WA checkout short-circuit",
        migration_class=CLASS_C,
    ),
    ReplyPathEntry(
        "payment_method_ack",
        layer="pre_brain",
        copy_source="core.order_flow.maybe_handle_payment_method_selection_inbound",
        why_exists="Payment method selected during WA checkout",
        migration_class=CLASS_C,
    ),
    ReplyPathEntry(
        "order_slot_prompt",
        layer="inside_brain",
        copy_source="compose/responder needs_collection + templates.collect_order_details",
        why_exists="Collect required checkout fields deterministically",
        migration_class=CLASS_C,
    ),
    ReplyPathEntry(
        "structured_admin_contact",
        layer="pre_brain",
        copy_source="structured_admin_contact_policy",
        why_exists="Evidence-backed configured admin contact delivery",
        migration_class=CLASS_A,
    ),
    ReplyPathEntry(
        "pre_brain_handoff:*",
        layer="pre_brain",
        copy_source="core.handoff_detector constants",
        why_exists="Explicit human handoff before Brain",
        migration_class=CLASS_C,
    ),
    ReplyPathEntry(
        "layer0:*",
        layer="pre_brain",
        copy_source="persona_template_engine / templates",
        why_exists="Zero-LLM fast path for greeting/social/FAQ",
        migration_class=CLASS_B,
    ),
    ReplyPathEntry(
        "staff_contact_policy",
        layer="pre_brain",
        copy_source="staff_contact_evidence",
        why_exists="Configured staff contact evidence delivery",
        migration_class=CLASS_A,
    ),
    ReplyPathEntry(
        "location_link_policy",
        layer="pre_brain",
        copy_source="location_link_policy",
        why_exists="Maps URL only when configured",
        migration_class=CLASS_A,
    ),
    # ── Inside Brain templates ───────────────────────────────────────────
    ReplyPathEntry(
        "template:social_reply",
        layer="inside_brain",
        copy_source="compose/templates.social_reply pools",
        why_exists="Rotating social/courtesy variants (cost shortcut)",
        migration_class=CLASS_B,
    ),
    ReplyPathEntry(
        "template:persona_social",
        layer="inside_brain",
        copy_source="compose/persona_template_engine pools",
        why_exists="No-LLM persona greeting/social",
        migration_class=CLASS_B,
    ),
    ReplyPathEntry(
        "template:order_slots",
        layer="inside_brain",
        copy_source="execution/orders._MISSING_FIELD_PROMPTS_AR (legacy fallback)",
        why_exists="Legacy slot prompts when constrained compose disabled",
        migration_class=CLASS_C,
    ),
    ReplyPathEntry(
        "template:catalog_discovery",
        layer="inside_brain",
        copy_source="catalog/discovery_presenter",
        why_exists="Numbered catalog evidence presentation",
        migration_class=CLASS_C,
    ),
    # ── Postprocess guards (copy replacement) ────────────────────────────
    ReplyPathEntry(
        "guard:payment_reply",
        layer="postprocess",
        copy_source="postprocess/payment_reply_guard constants",
        why_exists="Strip false payment confirmation from LLM output",
        migration_class=CLASS_C,
    ),
    ReplyPathEntry(
        "guard:shipment_truth",
        layer="postprocess",
        copy_source="postprocess/shipment_truth_guard",
        why_exists="Block false shipped/completed claims",
        migration_class=CLASS_C,
    ),
    ReplyPathEntry(
        "safety_net:clear_intent_fallback",
        layer="postprocess",
        copy_source="postprocess/safety_nets._CLEAR_INTENT_REPLIES",
        why_exists="Replace generic timeout when intent is clear",
        migration_class=CLASS_C,
    ),
    # ── System fallbacks ───────────────────────────────────────────────────
    ReplyPathEntry(
        "fallback:compose_error",
        layer="fallback",
        copy_source="core.fallback_policy",
        why_exists="Last resort when LLM compose fails",
        migration_class=CLASS_A,
    ),
)


def lookup_path(path: str) -> ReplyPathEntry | None:
    needle = (path or "").strip()
    if not needle:
        return None
    for entry in REPLY_PATH_REGISTRY:
        spec = entry.path
        if spec.endswith(":*") and needle.startswith(spec[:-1]):
            return entry
        if spec == needle:
            return entry
    return None


def registry_by_class(migration_class: str) -> List[ReplyPathEntry]:
    return [e for e in REPLY_PATH_REGISTRY if e.migration_class == migration_class]


def registry_summary() -> Dict[str, int]:
    counts: Dict[str, int] = {CLASS_A: 0, CLASS_B: 0, CLASS_C: 0}
    for entry in REPLY_PATH_REGISTRY:
        counts[entry.migration_class] = counts.get(entry.migration_class, 0) + 1
    return counts


__all__ = [
    "CLASS_A",
    "CLASS_B",
    "CLASS_C",
    "REPLY_PATH_REGISTRY",
    "ReplyPathEntry",
    "lookup_path",
    "registry_by_class",
    "registry_summary",
]
