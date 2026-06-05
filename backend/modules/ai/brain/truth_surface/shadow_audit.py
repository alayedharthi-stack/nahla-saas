"""
truth_surface/shadow_audit.py
─────────────────────────────
Phase 1 — Shadow Audit Mode: measure only, never block or mutate prompts.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional, Sequence

from .contract import TruthSurfaceInventory
from .flags import is_truth_surface_shadow_enabled
from .inventory import build_truth_surface_inventory

logger = logging.getLogger("nahla.brain.truth_surface.shadow")


def run_truth_surface_shadow_audit(
    reply_state: Any,
    *,
    tenant_id: Optional[int] = None,
    history_messages: Optional[Sequence[Dict[str, Any]]] = None,
    goal_regimen_bundle: Any = None,
    sales_context: Any = None,
    full_merchant_context: Optional[Dict[str, Any]] = None,
) -> Optional[TruthSurfaceInventory]:
    """
    Build inventory and emit ``[TRUTH_SURFACE_SHADOW]`` when flag enabled.

    Returns the inventory (for tests); never raises; never mutates inputs.
    """
    try:
        inventory = build_truth_surface_inventory(
            reply_state,
            tenant_id=tenant_id,
            history_messages=history_messages,
            goal_regimen_bundle=goal_regimen_bundle,
            sales_context=sales_context,
            full_merchant_context=full_merchant_context,
        )
    except Exception as exc:  # noqa: BLE001 — telemetry must not break turns
        logger.warning(
            "[TRUTH_SURFACE_SHADOW] inventory_failed tenant=%s err=%s",
            tenant_id,
            exc,
        )
        return None

    if not is_truth_surface_shadow_enabled():
        return None

    try:
        payload = inventory.to_log_dict()
        payload["event"] = "truth_surface_shadow_audit"
        logger.info("[TRUTH_SURFACE_SHADOW] %s", json.dumps(payload, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[TRUTH_SURFACE_SHADOW] emit_failed tenant=%s err=%s",
            tenant_id,
            exc,
        )

    return inventory


__all__ = ["run_truth_surface_shadow_audit"]
