"""
Structured branch escalation evidence — Operations Center (PR-A).

Per-branch escalation ladders read directly from ``branch_escalation_steps``.
No KB parsing, no LLM tier guessing when structured data is configured.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from modules.ai.brain.commerce.staff_contact_fallback_v0 import StaffChainEntry
from modules.operations.branch_contact_evidence import (
    resolve_branch_for_message,
    structured_branch_contacts_enabled,
    tenant_has_structured_branch_data,
)

logger = logging.getLogger("nahla.operations.branch_escalation_evidence")

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")

_ADMIN_ROLE_TOKENS = frozenset({
    "admin",
    "owner",
    "management",
    "الإدارة",
    "ادارة",
    "مدير",
    "manager",
})

MSG_STRUCTURED_ADMIN_NOT_CONFIGURED = "ما وجدت جهة إدارة مهيأة حالياً."

# Direct management/owner asks — platform-wide phrase library (no hardcoded names).
_DIRECT_ADMIN_REQUEST_RE = re.compile(
    r"(?:"
    r"(?:ابي|أبي|ابغى|أبغى|اريد|أريد|بدي|ودي)\s*(?:ال)?(?:"
    r"ادارة|إدارة|الاداره|الإداره|مدير|المدير|مسؤول|المسؤول|مالك|المالك"
    r")"
    r"|(?:اكلم|أكلم|اتكلم|أتكلم|تواصل|اتواصل|كلم|أكلم)\s*(?:مع\s*)?(?:ال)?(?:"
    r"ادارة|إدارة|الاداره|الإداره|مدير|المدير|مسؤول|المسؤول|مالك|المالك"
    r")"
    r"|(?:^|\s)(?:ال)?(?:"
    r"ادارة|إدارة|الاداره|الإداره|مدير|المدير|مسؤول|المسؤول|مالك|المالك"
    r")(?:\s|[?.!،]|$)"
    r")",
    re.IGNORECASE | re.UNICODE,
)


def _norm(text: str) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text).lower())
    t = _NORM_RE.sub("", t)
    t = (
        t.replace("\u0623", "\u0627")
        .replace("\u0625", "\u0627")
        .replace("\u0622", "\u0627")
        .replace("\u0649", "\u064a")
        .replace("\u0629", "\u0647")
    )
    return _WS_RE.sub(" ", t).strip()


def _token_present_in_text(text_norm: str, token_norm: str) -> bool:
    if not text_norm or not token_norm or token_norm not in text_norm:
        return False
    idx = 0
    while True:
        pos = text_norm.find(token_norm, idx)
        if pos < 0:
            return False
        before = text_norm[pos - 1] if pos > 0 else " "
        after_pos = pos + len(token_norm)
        after = text_norm[after_pos] if after_pos < len(text_norm) else " "
        if before.isspace() and after.isspace():
            return True
        idx = pos + 1


def is_direct_admin_contact_request(message: str) -> bool:
    """True when the customer explicitly asks for management/owner contact."""
    raw = (message or "").strip()
    if not raw:
        return False
    return bool(_DIRECT_ADMIN_REQUEST_RE.search(_norm(raw)))


@dataclass(frozen=True)
class StructuredAdminResolution:
    found: bool
    entries: Tuple[StaffChainEntry, ...] = ()
    reason: str = ""
    branch_id: int = 0
    escalation_level: int = 0


def _admin_tier_entries(
    steps: Sequence[Any],
    chain: Sequence[StaffChainEntry],
) -> List[StaffChainEntry]:
    """Return admin-tier contacts from structured escalation steps."""
    by_section_id = {int(e.section_id or 0): e for e in chain if e.section_id}
    admin_steps = [
        s for s in steps
        if _is_admin_role(str(getattr(s, "role", "") or ""))
    ]
    return [
        by_section_id[int(getattr(s, "id", 0) or 0)]
        for s in admin_steps
        if int(getattr(s, "id", 0) or 0) in by_section_id
    ]


def _message_requests_structured_admin(
    message: str,
    admin_entries: Sequence[StaffChainEntry],
) -> bool:
    if is_direct_admin_contact_request(message):
        return True
    norm = _norm(message)
    for entry in admin_entries:
        name_norm = _norm(entry.lookup_name or "")
        if len(name_norm) >= 2 and _token_present_in_text(norm, name_norm):
            return True
    return False


def _select_admin_entries_for_message(
    message: str,
    admin_entries: Sequence[StaffChainEntry],
) -> Tuple[StaffChainEntry, ...]:
    norm = _norm(message)
    named: List[StaffChainEntry] = []
    for entry in admin_entries:
        name_norm = _norm(entry.lookup_name or "")
        if len(name_norm) >= 2 and _token_present_in_text(norm, name_norm):
            named.append(entry)
    if named:
        return tuple(named)
    if is_direct_admin_contact_request(message):
        return tuple(admin_entries)
    return ()


def resolve_direct_admin_from_structured_chain(
    db: Any,
    tenant_id: int,
    message: str = "",
) -> Optional[StructuredAdminResolution]:
    """Resolve admin-tier contacts for a direct management/owner request."""
    if not structured_branch_contacts_enabled():
        return None
    if db is None or not tenant_id:
        return None
    if not tenant_has_structured_branch_data(db, int(tenant_id)):
        return None

    branch = resolve_branch_for_message(db, int(tenant_id), message or "")
    if branch is None:
        return None

    steps = load_structured_escalation_steps(db, branch.id)
    chain = load_structured_escalation_chain(db, int(tenant_id), message or "")
    if not steps or not chain:
        return None

    admin_entries = _admin_tier_entries(steps, chain)
    if not _message_requests_structured_admin(message, admin_entries):
        return None

    selected = _select_admin_entries_for_message(message, admin_entries)
    level = 0
    if selected:
        step_ids = {int(e.section_id or 0) for e in selected}
        levels = [
            int(getattr(s, "escalation_level", 0) or 0)
            for s in steps
            if int(getattr(s, "id", 0) or 0) in step_ids
        ]
        level = max(levels) if levels else 0

    if not selected:
        logger.info(
            "[STRUCTURED_ADMIN_REQUEST] missing_admin_contact tenant=%s "
            "branch_id=%s level=%s contacts=0 reason=no_admin_steps",
            tenant_id,
            branch.id,
            level,
        )
        return StructuredAdminResolution(
            found=False,
            reason="no_admin_steps",
            branch_id=int(branch.id),
            escalation_level=level,
        )

    logger.info(
        "[STRUCTURED_ADMIN_REQUEST] matched tenant=%s branch_id=%s level=%s "
        "contacts=%d names=%s",
        tenant_id,
        branch.id,
        level,
        len(selected),
        [e.lookup_name for e in selected],
    )
    return StructuredAdminResolution(
        found=True,
        entries=selected,
        reason="structured_admin_direct",
        branch_id=int(branch.id),
        escalation_level=level,
    )


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
        from models import BranchEscalationStep  # noqa: PLC0415

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
    "MSG_STRUCTURED_ADMIN_NOT_CONFIGURED",
    "StructuredAdminResolution",
    "is_direct_admin_contact_request",
    "load_structured_escalation_chain",
    "load_structured_escalation_steps",
    "resolve_direct_admin_from_structured_chain",
    "resolve_next_structured_escalation",
]
