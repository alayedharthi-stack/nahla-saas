"""
truth_surface/integrity_gate.py
───────────────────────────────
Prompt Integrity Gate — Phase 2 shadow only. Measures, never blocks.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Set

from .contract import (
    EffectiveFact,
    EffectiveFactStatus,
    IntegrityGateReport,
    TruthSurface,
    UTS_V1_INGEST_SURFACES,
)
from .inventory import build_truth_surface_inventory


_EXTERNAL_SURFACES: frozenset[TruthSurface] = frozenset(
    s for s in TruthSurface if s not in UTS_V1_INGEST_SURFACES
) - frozenset({
    TruthSurface.BRAIN_STATE_JSON,
    TruthSurface.OVERLAY_FACTS_FALLBACK,
    TruthSurface.TENANT_OVERLAY_LEGACY,
    TruthSurface.FULL_MERCHANT_CONTEXT_LATENT,
    TruthSurface.LEGACY_BUILD_AI_CONTEXT,
    TruthSurface.LEGACY_TENANT_OVERLAY,
    TruthSurface.HIGH_PRIORITY_PRECEDENCE,
})


def run_integrity_gate_shadow(
    manifest_facts: List[EffectiveFact],
    reply_state: Any,
    *,
    history_messages: Optional[Sequence[Dict[str, Any]]] = None,
) -> IntegrityGateReport:
    """Compare UTS manifest against parallel operational surfaces in the prompt path."""
    manifest_keys: Set[str] = {
        f.fact_key for f in manifest_facts if f.status == EffectiveFactStatus.ACTIVE
    }

    duplicate_keys: List[str] = []
    conflict_keys: List[str] = []
    for f in manifest_facts:
        if f.status == EffectiveFactStatus.DEDUPED and f.fact_key not in duplicate_keys:
            duplicate_keys.append(f.fact_key)
        if f.status == EffectiveFactStatus.CONFLICT and f.fact_key not in conflict_keys:
            conflict_keys.append(f.fact_key)

    inventory = build_truth_surface_inventory(
        reply_state,
        history_messages=history_messages or [],
    )

    external_surfaces: Set[str] = set()
    external_fact_count = 0
    leakage_chat = 0
    leakage_brain_json = 0
    leakage_coupon = 0
    leakage_store = 0
    for fact in inventory.facts:
        if fact.surface in _EXTERNAL_SURFACES:
            external_surfaces.add(fact.surface.value)
            external_fact_count += 1
        if fact.surface == TruthSurface.CHAT_HISTORY:
            leakage_chat += 1
        if fact.surface == TruthSurface.MERCHANT_CONTEXT_AI_SETTINGS:
            leakage_brain_json += 1
        if fact.surface == TruthSurface.COUPON_POLICY:
            leakage_coupon += 1
        if fact.surface == TruthSurface.STORE_KNOWLEDGE:
            leakage_store += 1

    # BrainStateJSON aggregate is always present on LLM turns — count JSON-embedded
    # operational fields not ingested by UTS v1 (proxy via ai_settings + store_knowledge).
    for presence in inventory.surfaces_active:
        if presence.surface == TruthSurface.BRAIN_STATE_JSON and presence.active:
            leakage_brain_json = max(leakage_brain_json, 1)

    return IntegrityGateReport(
        duplicate_fact_keys=len(set(duplicate_keys)),
        conflicting_fact_values=len(set(conflict_keys)),
        external_operational_surfaces_count=len(external_surfaces),
        external_operational_facts_count=external_fact_count,
        duplicate_keys=sorted(set(duplicate_keys)),
        conflict_keys=sorted(set(conflict_keys)),
        external_surfaces=sorted(external_surfaces),
        leakage_chat_history=leakage_chat,
        leakage_brain_state_json=leakage_brain_json,
        leakage_coupon_policy=leakage_coupon,
        leakage_store_knowledge=leakage_store,
    )


__all__ = ["run_integrity_gate_shadow"]
