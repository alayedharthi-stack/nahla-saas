"""
truth_surface/uts_v1.py
──────────────────────
UTS v1 orchestrator — shadow / compare mode only in Phase 2.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence

from .block_builder import build_operational_facts_block
from .collector import collect_uts_v1_facts
from .contract import IntegrityGateReport, TruthSurfaceReport
from .dedup import dedup_uts_v1_facts
from .flags import is_uts_v1_enforce_enabled, is_uts_v1_shadow_enabled
from .integrity_gate import run_integrity_gate_shadow

logger = logging.getLogger("nahla.brain.truth_surface.uts_v1")


@dataclass
class UtsV1ShadowResult:
    manifest: TruthSurfaceReport
    integrity: IntegrityGateReport


def build_uts_v1_manifest(
    reply_state: Any,
    *,
    tenant_id: Optional[int] = None,
    goal_regimen_bundle: Any = None,
    history_messages: Optional[Sequence[Dict[str, Any]]] = None,
) -> UtsV1ShadowResult:
    """Collect, dedup, build block, and run integrity gate — no prompt mutation."""
    raw_facts, ingested = collect_uts_v1_facts(
        reply_state,
        goal_regimen_bundle=goal_regimen_bundle,
    )
    deduped_facts, deduped_count = dedup_uts_v1_facts(raw_facts)
    block = build_operational_facts_block(deduped_facts)
    active_count = sum(
        1 for f in deduped_facts if f.status.value == "active"
    )

    manifest = TruthSurfaceReport(
        tenant_id=tenant_id,
        intent=str(getattr(reply_state, "intent_name", "") or ""),
        stage=str(getattr(reply_state, "stage", "") or ""),
        effective_facts=deduped_facts,
        operational_facts_block=block,
        ingested_surfaces=ingested,
        raw_fact_count=len(raw_facts),
        deduped_count=deduped_count,
        active_fact_count=active_count,
    )

    integrity = run_integrity_gate_shadow(
        deduped_facts,
        reply_state,
        history_messages=history_messages,
    )

    return UtsV1ShadowResult(manifest=manifest, integrity=integrity)


def run_uts_v1_shadow(
    reply_state: Any,
    *,
    tenant_id: Optional[int] = None,
    goal_regimen_bundle: Any = None,
    history_messages: Optional[Sequence[Dict[str, Any]]] = None,
) -> Optional[UtsV1ShadowResult]:
    """
    Build UTS v1 manifest and emit logs when shadow flag is enabled.

    Never mutates reply_state or prompt. Enforce flag is logged only in Phase 2.
    """
    try:
        result = build_uts_v1_manifest(
            reply_state,
            tenant_id=tenant_id,
            goal_regimen_bundle=goal_regimen_bundle,
            history_messages=history_messages,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[UTS_V1_SHADOW] build_failed tenant=%s err=%s",
            tenant_id,
            exc,
        )
        return None

    if is_uts_v1_enforce_enabled():
        logger.info(
            "[UTS_V1_ENFORCE] flag=true but Phase 2 is shadow-only — "
            "prompt not modified tenant=%s",
            tenant_id,
        )

    if not is_uts_v1_shadow_enabled():
        return None

    try:
        payload: Dict[str, Any] = {
            "event": "uts_v1_shadow_audit",
            **result.manifest.to_log_dict(),
            "integrity_gate": result.integrity.to_log_dict(),
        }
        logger.info("[UTS_V1_SHADOW] %s", json.dumps(payload, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[UTS_V1_SHADOW] emit_failed tenant=%s err=%s",
            tenant_id,
            exc,
        )

    return result


__all__ = [
    "UtsV1ShadowResult",
    "build_uts_v1_manifest",
    "run_uts_v1_shadow",
]
