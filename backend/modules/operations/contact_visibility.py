"""Contact action permission contract — Operations Center.

Visibility is an explicit merchant-set field. It is never inferred from
employee role (owner/management must not auto-become customer-visible).
"""
from __future__ import annotations

from typing import Any, FrozenSet

CUSTOMER_VISIBLE = "customer_visible"
INTERNAL_ONLY = "internal_only"
BOTH = "both"

VALID_VISIBILITY: FrozenSet[str] = frozenset({CUSTOMER_VISIBLE, INTERNAL_ONLY, BOTH})

SHARE_CUSTOMER_CONTACT = "share_customer_contact"
WHATSAPP_CTA = "whatsapp_cta"
NOTIFY_OR_HANDOFF = "notify_or_handoff"
HANDOFF_CONVERSATION = "handoff_conversation"

VALID_ACTIONS: FrozenSet[str] = frozenset({
    SHARE_CUSTOMER_CONTACT,
    WHATSAPP_CTA,
    NOTIFY_OR_HANDOFF,
    HANDOFF_CONVERSATION,
})

CUSTOMER_SHARE_ACTIONS: FrozenSet[str] = frozenset({
    SHARE_CUSTOMER_CONTACT,
    WHATSAPP_CTA,
})

INTERNAL_ACTIONS: FrozenSet[str] = frozenset({
    NOTIFY_OR_HANDOFF,
    HANDOFF_CONVERSATION,
})


UNSPECIFIED = ""


def normalize_visibility(raw: Any) -> str:
    key = str(raw or "").strip().lower()
    if not key:
        return UNSPECIFIED
    if key in {"customer_visible_contact", "share", "yes", "true", "1"}:
        return CUSTOMER_VISIBLE
    if key in {"internal_escalation_only", "internal", "no", "false", "0"}:
        return INTERNAL_ONLY
    if key in VALID_VISIBILITY:
        return key
    return UNSPECIFIED


def normalize_action(raw: Any) -> str:
    key = str(raw or "").strip().lower()
    aliases = {
        "share": SHARE_CUSTOMER_CONTACT,
        "share_number": SHARE_CUSTOMER_CONTACT,
        "whatsapp": WHATSAPP_CTA,
        "notify": NOTIFY_OR_HANDOFF,
        "notify_only": NOTIFY_OR_HANDOFF,
        "handoff": HANDOFF_CONVERSATION,
        "transfer": HANDOFF_CONVERSATION,
    }
    if key in aliases:
        return aliases[key]
    if key in VALID_ACTIONS:
        return key
    return SHARE_CUSTOMER_CONTACT


def visibility_of(record: Any) -> str:
    if record is None:
        return INTERNAL_ONLY
    if isinstance(record, dict):
        return normalize_visibility(
            record.get("customer_visibility") or record.get("visibility"),
        )
    return normalize_visibility(getattr(record, "customer_visibility", None))


def may_share_with_customer(record: Any, *, action: str = "") -> bool:
    """True when the platform may give phone / WhatsApp CTA to the customer.

    Explicit ``internal_only`` never shares. Explicit visible/both share.
    Unspecified legacy records stay shareable so existing reception
    delivery is preserved until the merchant confirms a new policy.
    """
    vis = visibility_of(record)
    if vis == INTERNAL_ONLY:
        return False
    act = normalize_action(action) if action else SHARE_CUSTOMER_CONTACT
    if action and act not in CUSTOMER_SHARE_ACTIONS:
        return False
    return vis in {CUSTOMER_VISIBLE, BOTH, UNSPECIFIED}


def may_notify_internally(record: Any, *, action: str = "") -> bool:
    vis = visibility_of(record)
    if vis not in {INTERNAL_ONLY, BOTH}:
        return vis == CUSTOMER_VISIBLE and normalize_action(action) in INTERNAL_ACTIONS
    if action:
        return normalize_action(action) in INTERNAL_ACTIONS or vis in {INTERNAL_ONLY, BOTH}
    return True


def default_action_for_visibility(visibility: str) -> str:
    vis = normalize_visibility(visibility)
    if vis in {CUSTOMER_VISIBLE, BOTH, UNSPECIFIED}:
        return SHARE_CUSTOMER_CONTACT
    return NOTIFY_OR_HANDOFF


def customer_facing_phone(record: Any, *, action: str = "") -> str:
    """Live phone only when customer share is permitted. Never invent."""
    if not may_share_with_customer(record, action=action):
        return ""
    if isinstance(record, dict):
        return str(
            record.get("phone_e164")
            or record.get("whatsapp_e164")
            or record.get("phone")
            or "",
        ).strip()
    return str(
        getattr(record, "phone_e164", "")
        or getattr(record, "whatsapp_e164", "")
        or getattr(record, "phone", "")
        or "",
    ).strip()
