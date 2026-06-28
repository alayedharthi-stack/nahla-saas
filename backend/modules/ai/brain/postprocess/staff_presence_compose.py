"""
staff_presence_compose.py
─────────────────────────
Attach staff presence constraints to compose goals before LLM generation.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from modules.ai.brain.postprocess.staff_presence_evidence import (
    derive_allowed_staff_facts,
    derive_forbidden_staff_claims,
    evaluate_staff_presence_evidence,
    staff_presence_compose_overlay,
)

logger = logging.getLogger("nahla.brain.postprocess.staff_presence_compose")


def enrich_decision_args_for_staff_presence_compose(
    decision: Any,
    *,
    db: Any = None,
    tenant_id: Optional[int] = None,
    message: str = "",
    state: Any = None,
    store_contact_phone: str = "",
) -> None:
    args = getattr(decision, "args", None)
    if not isinstance(args, dict):
        return
    try:
        evidence = evaluate_staff_presence_evidence(
            message=message or "",
            db=db,
            tenant_id=tenant_id,
            store_contact_phone=store_contact_phone,
            state=state,
        )
        overlay = staff_presence_compose_overlay(evidence)
        if not overlay:
            return
        args["staff_presence_compose_overlay"] = overlay
        args["allowed_staff_facts"] = derive_allowed_staff_facts(evidence)
        args["forbidden_staff_claims"] = derive_forbidden_staff_claims(evidence)
        args["staff_presence_evidence_source"] = evidence.evidence_source
    except Exception:  # noqa: BLE001
        logger.exception("[STAFF_PRESENCE_COMPOSE] enrich_failed tenant=%s", tenant_id)


__all__ = ["enrich_decision_args_for_staff_presence_compose"]
