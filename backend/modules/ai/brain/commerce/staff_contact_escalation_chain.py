"""
Tiered staff escalation chain — evidence-only, platform-wide.

Escalation order (when configured in merchant KB):

    arrival/showroom → customer_service → admin/owner

No hardcoded staff names. Tier classification uses KB role metadata,
showroom role prose, and owner flags only.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

from modules.ai.brain.commerce.staff_contact_fallback_v0 import (
    StaffChainEntry,
    StaffRoleAliasGraph,
    _SHOWROOM_ROLE_RE,
    _entry_matches_sent,
    _extract_label_near_phone,
    _is_owner_entry,
    _normalize_name_key,
    _normalize_phone_key,
    _section_role,
    extract_staff_chain_from_sections,
    extract_staff_role_aliases_from_sections,
)

logger = logging.getLogger("nahla.brain.staff_contact_escalation_chain")

ContactTier = str  # showroom | customer_service | admin

_TIER_ORDER: Tuple[ContactTier, ...] = (
    "showroom",
    "customer_service",
    "admin",
)

_CS_ROLE_TOKENS = frozenset({
    "customer_service",
    "cs",
    "support",
    "خدمة_العملاء",
    "خدمة العملاء",
    "دعم العملاء",
})

_CS_LABEL_HINTS = (
    "خدمة العملاء",
    "خدمه العملاء",
    "دعم العملاء",
    "customer service",
)


def classify_contact_tier(entry: StaffChainEntry) -> ContactTier:
    """Classify a KB chain entry into an escalation tier."""
    if entry.is_owner or (entry.role or "").strip().lower() == "owner":
        return "admin"

    role = (entry.role or "").strip().lower()
    if role in {"showroom", "seller"}:
        return "showroom"

    label = entry.lookup_name or ""
    norm = _normalize_name_key(label)
    if norm and _SHOWROOM_ROLE_RE.search(label):
        return "showroom"

    if role in _CS_ROLE_TOKENS:
        return "customer_service"

    label_lower = label.lower()
    if any(h.lower() in label_lower for h in _CS_LABEL_HINTS):
        return "customer_service"

    # Non-owner KB contacts without showroom markers → CS tier.
    return "customer_service"


def _find_entry_for_sent_item(
    chain: Sequence[StaffChainEntry],
    item: Dict[str, Any],
) -> Optional[StaffChainEntry]:
    sent_phone = _normalize_phone_key(str(item.get("phone") or ""))
    if sent_phone:
        for entry in chain:
            if _normalize_phone_key(entry.phone) == sent_phone:
                return entry
    sent_name = str(item.get("name") or "").strip()
    if sent_name:
        for entry in chain:
            if _entry_matches_sent(entry, [item]):
                return entry
    return None


def find_last_sent_chain_entry(
    chain: Sequence[StaffChainEntry],
    contacts_sent: Sequence[Dict[str, Any]],
) -> Optional[StaffChainEntry]:
    """Return the chain entry matching the most recent ``staff_contacts_sent`` row."""
    if not chain or not contacts_sent:
        return None

    best: Optional[Tuple[int, StaffChainEntry]] = None
    for item in contacts_sent:
        if not isinstance(item, dict):
            continue
        try:
            turn = int(item.get("turn") or 0)
        except (TypeError, ValueError):
            turn = 0
        entry = _find_entry_for_sent_item(chain, item)
        if entry is None:
            continue
        if best is None or turn >= best[0]:
            best = (turn, entry)
    return best[1] if best else None


def resolve_next_tiered_contact(
    chain: Sequence[StaffChainEntry],
    contacts_sent: Sequence[Dict[str, Any]],
    *,
    allow_admin: bool = True,
) -> Optional[StaffChainEntry]:
    """Pick the next unsent contact following tier order."""
    if not chain:
        return None

    last_entry = find_last_sent_chain_entry(chain, contacts_sent)
    if last_entry is None:
        return None

    last_tier = classify_contact_tier(last_entry)
    last_idx = last_entry.chain_index

    def _first_unsent_in_tier(tier: ContactTier, *, after_index: int = -1) -> Optional[StaffChainEntry]:
        for entry in chain:
            if classify_contact_tier(entry) != tier:
                continue
            if entry.chain_index <= after_index:
                continue
            if _entry_matches_sent(entry, contacts_sent):
                continue
            return entry
        return None

    if last_tier == "showroom":
        nxt = _first_unsent_in_tier("customer_service")
        if nxt is not None:
            return nxt
        if allow_admin:
            return _first_unsent_in_tier("admin")
        return None

    if last_tier == "customer_service":
        nxt = _first_unsent_in_tier("customer_service", after_index=last_idx)
        if nxt is not None:
            return nxt
        if allow_admin:
            return _first_unsent_in_tier("admin")
        return None

    # admin tier — no further escalation
    return None


def log_escalation_chain_resolve(
    *,
    tenant_id: Any = None,
    trigger: str = "",
    last_tier: str = "",
    selected: str = "",
    phone: str = "",
    reason: str = "",
) -> None:
    try:
        logger.info(
            "[ESCALATION_CHAIN] tenant=%s trigger=%s last_tier=%s "
            "selected=%r phone_match=%s reason=%s",
            tenant_id if tenant_id is not None else "-",
            trigger or "-",
            last_tier or "-",
            (selected or "")[:48],
            bool(phone),
            reason or "-",
        )
    except Exception:  # noqa: silent-ok - escalation chain telemetry must not block contact delivery
        pass


__all__ = [
    "classify_contact_tier",
    "find_last_sent_chain_entry",
    "resolve_next_tiered_contact",
    "log_escalation_chain_resolve",
]
