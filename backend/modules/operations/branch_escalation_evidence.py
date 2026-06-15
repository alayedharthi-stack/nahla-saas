"""
Structured branch escalation evidence — Operations Center (PR-A).

Per-branch escalation ladders read directly from ``branch_escalation_steps``.
No KB parsing, no LLM tier guessing when structured data is configured.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Sequence, Tuple

from modules.ai.brain.commerce.staff_contact_fallback_v0 import StaffChainEntry
from modules.operations.branch_contact_evidence import (
    resolve_branch_for_message,
    structured_branch_contacts_enabled,
)

logger = logging.getLogger("nahla.operations.branch_escalation_evidence")

_ADMIN_ROLE_TOKENS = frozenset({
    "admin",
    "owner",
    "management",
    "الإدارة",
})


def _is_admin_role(role: str) -> bool:
    key = (role or "").strip().lower()
    return key in _ADMIN_ROLE_TOKENS


def load_structured_escalation_steps(
    db: Any,
    branch_id: int,
) -> Tuple[Any, ...]:
    if db is None or not branch_id:
        return ()
    try:
        from database.models import BranchEscalationStep  # noqa: PLC0415

        rows = (
            db.query(BranchEscalationStep)
            .filter(
                BranchEscalationStep.branch_id == int(branch_id),
                BranchEscalationStep.is_active.is_(True),
            )
            .order_by(
                BranchEscalationStep.escalation_level.asc(),
                BranchEscalationStep.sort_order.asc(),
                BranchEscalationStep.id.asc(),
            )
            .all()
        )
        return tuple(rows)
    except Exception as exc:  # noqa: silent-ok - escalation query failure degrades to empty chain
        logger.debug(
            "branch_escalation_evidence.load_steps failed branch=%s err=%s",
            branch_id,
            exc,
        )
        return ()


def load_structured_escalation_chain(
    db: Any,
    tenant_id: int,
    message: str = "",
) -> Tuple[StaffChainEntry, ...]:
    """Return escalation ladder as ``StaffChainEntry`` rows for runtime wiring."""
    if not structured_branch_contacts_enabled():
        return ()

    branch = resolve_branch_for_message(db, int(tenant_id or 0), message)
    if branch is None:
        return ()

    steps = load_structured_escalation_steps(db, branch.id)
    if not steps:
        return ()

    chain: list[StaffChainEntry] = []
    for idx, step in enumerate(steps):
        phone = str(getattr(step, "phone_e164", "") or "").strip()
        if not phone:
            continue
        role = str(getattr(step, "role", "") or "").strip().lower()
        display = str(getattr(step, "display_name", "") or "").strip()
        is_owner = _is_admin_role(role)
        chain.append(
            StaffChainEntry(
                lookup_name=display,
                phone=phone,
                section_id=int(getattr(step, "id", 0) or 0),
                kind="structured_escalation",
                is_owner=is_owner,
                chain_index=idx,
                role=role,
            )
        )

    if chain:
        logger.info(
            "[BRANCH_ESCALATION_EVIDENCE] chain tenant=%s branch_id=%s steps=%d",
            tenant_id,
            branch.id,
            len(chain),
        )
    return tuple(chain)


def resolve_next_structured_escalation(
    chain: Sequence[StaffChainEntry],
    contacts_sent: Sequence[Dict[str, Any]],
    *,
    allow_admin: bool = True,
) -> Optional[StaffChainEntry]:
    """Advance linearly along structured escalation steps after last sent contact."""
    if not chain or not contacts_sent:
        return None

    from modules.ai.brain.commerce.staff_contact_escalation_chain import (  # noqa: PLC0415
        find_last_sent_chain_entry,
    )
    from modules.ai.brain.commerce.staff_contact_fallback_v0 import (  # noqa: PLC0415
        _entry_matches_sent,
    )

    last_entry = find_last_sent_chain_entry(chain, contacts_sent)
    if last_entry is None:
        return None

    start_idx = last_entry.chain_index + 1
    for entry in chain:
        if entry.chain_index < start_idx:
            continue
        if not allow_admin and entry.is_owner:
            continue
        if _entry_matches_sent(entry, contacts_sent):
            continue
        return entry
    return None


__all__ = [
    "load_structured_escalation_chain",
    "load_structured_escalation_steps",
    "resolve_next_structured_escalation",
]
