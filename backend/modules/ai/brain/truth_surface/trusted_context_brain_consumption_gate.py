"""
trusted_context_brain_consumption_gate.py
─────────────────────────────────────────
Fail-closed gate for trusted-context Brain/Compose projection consumption.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from ..types import BrainContext
from .contract import TrustedContextSnapshot
from .flags import is_trusted_context_brain_projection_enabled
from .trusted_context import current_trusted_context
from .model_payload_attestation import facts_reaching_brain_from_projection
from .trusted_context_brain_projection import (
    TrustedContextBrainProjectionError,
    project_trusted_context_brain_facts,
)


def maybe_trusted_context_brain_projection(
    *,
    snapshot: Optional[TrustedContextSnapshot],
    tenant_id: int,
    customer_phone: str,
    conversation_id: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """
    Return scoped Brain/Compose projection when activation checks pass.

    Fail-closed: returns ``None`` on any gate failure.
    """
    if not is_trusted_context_brain_projection_enabled():
        return None
    if snapshot is None:
        return None
    try:
        return project_trusted_context_brain_facts(
            snapshot=snapshot,
            tenant_id=tenant_id,
            customer_phone=customer_phone,
            conversation_id=conversation_id,
        )
    except TrustedContextBrainProjectionError:
        return None


def attach_trusted_context_brain_projection(ctx: BrainContext) -> Optional[Dict[str, Any]]:
    """Attach scoped projection to ``BrainContext`` when available."""
    projection = maybe_trusted_context_brain_projection(
        snapshot=current_trusted_context(),
        tenant_id=int(getattr(ctx, "tenant_id", 0) or 0),
        customer_phone=str(getattr(ctx, "customer_phone", "") or ""),
        conversation_id=getattr(ctx, "conversation_id", None),
    )
    if projection is not None:
        ctx.trusted_context_projection = projection
    return projection


def safe_trusted_context_brain_projection_trace_metadata(
    result_or_error: Any,
) -> Dict[str, Any]:
    """Safe trace metadata for logs — no raw fact payloads."""
    if isinstance(result_or_error, dict):
        brain_facts = facts_reaching_brain_from_projection(result_or_error)
        return {
            "status": "ok",
            "surface": brain_facts.get("surface") or str(result_or_error.get("surface") or ""),
            "facts_snapshot_id": brain_facts.get("facts_snapshot_id")
            or str(result_or_error.get("facts_snapshot_id") or ""),
            "loaded_domains": list(brain_facts.get("loaded_domains") or []),
            "domains_present": list(brain_facts.get("domains_present") or []),
            "has_product_identity": "product_identity" in (brain_facts.get("domains_present") or []),
            "product_id": brain_facts.get("product_id"),
            "variant_id": brain_facts.get("variant_id"),
            "product_candidate_count": int(brain_facts.get("candidate_count") or 0),
            "has_order": bool(brain_facts.get("has_order")),
            "has_shipment": bool(brain_facts.get("has_shipment")),
            "facts_reaching_brain": brain_facts,
        }
    if isinstance(result_or_error, TrustedContextBrainProjectionError):
        return {
            "status": "error",
            "stage": "trusted_context_brain_projection",
            "error_class": type(result_or_error).__name__,
        }
    if isinstance(result_or_error, Exception):
        return {
            "status": "error",
            "stage": "gate",
            "error_class": type(result_or_error).__name__,
        }
    return {
        "status": "error",
        "stage": "gate",
        "error_class": "Unknown",
    }


__all__ = [
    "attach_trusted_context_brain_projection",
    "maybe_trusted_context_brain_projection",
    "safe_trusted_context_brain_projection_trace_metadata",
]
