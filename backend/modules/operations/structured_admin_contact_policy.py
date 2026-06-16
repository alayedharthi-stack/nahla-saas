"""
Direct admin / L3 structured escalation — Operations Center only.

When a customer explicitly asks for management/owner contact and the
merchant configured admin steps in Operations Center, deliver those
contacts deterministically — never KB, never LLM refusal.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, List, Optional, Sequence, Tuple

from modules.ai.brain.commerce.staff_contact_fallback_v0 import StaffChainEntry
from modules.operations.branch_contact_evidence import (
    structured_branch_contacts_enabled,
)
from modules.operations.branch_escalation_evidence import (
    MSG_STRUCTURED_ADMIN_NOT_CONFIGURED,
    is_direct_admin_contact_request,
    resolve_direct_admin_from_structured_chain,
)

logger = logging.getLogger("nahla.operations.structured_admin_contact_policy")


@dataclass(frozen=True)
class StructuredAdminContactDecision:
    reply_text: str
    call_targets: Tuple[Any, ...] = ()
    deliver_contact: bool = False
    reason: str = ""
    branch_id: int = 0
    escalation_level: int = 0
    contact_count: int = 0
    skip_brain: bool = True


def _build_call_targets(entries: Sequence[StaffChainEntry]) -> Tuple[Any, ...]:
    from modules.ai.brain.commerce.staff_contact_evidence import (  # noqa: PLC0415
        build_staff_call_target,
        resolve_contact_display_name,
    )

    targets: List[Any] = []
    for entry in entries:
        target = build_staff_call_target(
            lookup_name=entry.lookup_name,
            phone=entry.phone,
            role=entry.role,
        )
        if target is not None:
            targets.append(target)
    return tuple(targets)


def _build_deliver_reply(entries: Sequence[StaffChainEntry]) -> str:
    from modules.ai.brain.commerce.staff_contact_evidence import (  # noqa: PLC0415
        resolve_contact_display_name,
    )

    if not entries:
        return MSG_STRUCTURED_ADMIN_NOT_CONFIGURED
    if len(entries) == 1:
        label = resolve_contact_display_name(
            entries[0].lookup_name,
            role=entries[0].role,
            fallback="الإدارة",
        )
        return f"تقدر تتواصل مع {label}."
    labels = [
        resolve_contact_display_name(e.lookup_name, role=e.role, fallback="الإدارة")
        for e in entries
    ]
    return "تقدر تتواصل مع " + " أو ".join(labels) + "."


def evaluate_structured_admin_contact_policy(
    db: Any,
    *,
    tenant_id: int,
    message: str,
) -> Optional[StructuredAdminContactDecision]:
    """Pre-brain short-circuit for direct admin/management contact asks."""
    if not structured_branch_contacts_enabled():
        return None

    resolution = resolve_direct_admin_from_structured_chain(
        db,
        int(tenant_id or 0),
        message or "",
    )
    if resolution is None:
        return None

    if resolution.found and resolution.entries:
        targets = _build_call_targets(resolution.entries)
        if not targets:
            logger.info(
                "[STRUCTURED_ADMIN_REQUEST] missing_admin_contact tenant=%s "
                "branch_id=%s reason=phone_normalize_failed",
                tenant_id,
                resolution.branch_id,
            )
            return StructuredAdminContactDecision(
                reply_text=MSG_STRUCTURED_ADMIN_NOT_CONFIGURED,
                deliver_contact=False,
                reason="phone_normalize_failed",
                branch_id=resolution.branch_id,
                escalation_level=resolution.escalation_level,
            )
        logger.info(
            "[STRUCTURED_ADMIN_REQUEST] sent tenant=%s branch_id=%s level=%s "
            "contacts=%d names=%s",
            tenant_id,
            resolution.branch_id,
            resolution.escalation_level,
            len(targets),
            [getattr(t, "name", "") for t in targets],
        )
        return StructuredAdminContactDecision(
            reply_text=_build_deliver_reply(resolution.entries),
            call_targets=targets,
            deliver_contact=True,
            reason=resolution.reason,
            branch_id=resolution.branch_id,
            escalation_level=resolution.escalation_level,
            contact_count=len(targets),
        )

    logger.info(
        "[STRUCTURED_ADMIN_REQUEST] missing_admin_contact tenant=%s branch_id=%s "
        "reason=%s",
        tenant_id,
        resolution.branch_id,
        resolution.reason,
    )
    return StructuredAdminContactDecision(
        reply_text=MSG_STRUCTURED_ADMIN_NOT_CONFIGURED,
        deliver_contact=False,
        reason=resolution.reason or "missing_admin_contact",
        branch_id=resolution.branch_id,
        escalation_level=resolution.escalation_level,
    )


__all__ = [
    "StructuredAdminContactDecision",
    "evaluate_structured_admin_contact_policy",
]
