"""Canonical escalation policy runtime — structured steps + live contacts.

Customer semantics stay Brain-owned. This module loads tenant-scoped
policy, resolves live phones from contact_id, and never exposes
internal-only numbers. It does not parse merchant instruction text.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from modules.operations.contact_visibility import (
    CUSTOMER_SHARE_ACTIONS,
    HANDOFF_CONVERSATION,
    INTERNAL_ONLY,
    NOTIFY_OR_HANDOFF,
    SHARE_CUSTOMER_CONTACT,
    customer_facing_phone,
    may_notify_internally,
    may_share_with_customer,
    normalize_action,
    normalize_visibility,
)

logger = logging.getLogger("nahla.operations.escalation_policy_runtime")


@dataclass(frozen=True)
class PolicyStepView:
    order: int
    contact_id: int
    display_name: str
    role: str
    branch_id: int
    permitted_action: str
    trigger_condition: str
    customer_visibility: str
    live_phone_e164: str
    customer_share_allowed: bool
    notify_allowed: bool


@dataclass(frozen=True)
class CanonicalEscalationPolicy:
    tenant_id: int
    branch_id: int
    instruction_text: str
    steps: Tuple[PolicyStepView, ...]
    has_customer_visible_contact: bool
    has_internal_contact: bool

    def shareable_steps(self) -> Tuple[PolicyStepView, ...]:
        return tuple(s for s in self.steps if s.customer_share_allowed and s.live_phone_e164)

    def notify_steps(self) -> Tuple[PolicyStepView, ...]:
        return tuple(s for s in self.steps if s.notify_allowed and not s.customer_share_allowed)

    def first_shareable(self) -> Optional[PolicyStepView]:
        share = self.shareable_steps()
        return share[0] if share else None

    def capability(self) -> Dict[str, Any]:
        return {
            "has_policy": bool(self.steps),
            "has_customer_visible_contact": self.has_customer_visible_contact,
            "has_internal_contact": self.has_internal_contact,
            "share_contact_available": bool(self.shareable_steps()),
            "notify_available": bool(self.notify_steps()) or any(
                s.permitted_action in {NOTIFY_OR_HANDOFF, HANDOFF_CONVERSATION}
                for s in self.steps
            ),
            "handoff_available": any(
                s.permitted_action == HANDOFF_CONVERSATION for s in self.steps
            ),
        }


def _live_contact(db: Any, contact_id: int, tenant_id: int) -> Optional[Any]:
    if db is None or not contact_id or not tenant_id:
        return None
    from models import BranchContact, MerchantBranch  # noqa: PLC0415

    return (
        db.query(BranchContact)
        .join(MerchantBranch, MerchantBranch.id == BranchContact.branch_id)
        .filter(
            BranchContact.id == int(contact_id),
            MerchantBranch.tenant_id == int(tenant_id),
            BranchContact.is_active.is_(True),
        )
        .first()
    )


def load_canonical_policy(
    db: Any,
    tenant_id: int,
    *,
    branch_id: Optional[int] = None,
    message: str = "",
) -> Optional[CanonicalEscalationPolicy]:
    if db is None or not tenant_id:
        return None
    from models import BranchEscalationStep, MerchantBranch  # noqa: PLC0415
    from modules.operations.branch_contact_evidence import (  # noqa: PLC0415
        resolve_branch_for_message,
    )

    branch = None
    if branch_id:
        branch = (
            db.query(MerchantBranch)
            .filter(
                MerchantBranch.id == int(branch_id),
                MerchantBranch.tenant_id == int(tenant_id),
            )
            .first()
        )
    if branch is None:
        resolved = resolve_branch_for_message(db, int(tenant_id), message or "")
        if resolved is None:
            return None
        branch = (
            db.query(MerchantBranch)
            .filter(
                MerchantBranch.id == int(resolved.id),
                MerchantBranch.tenant_id == int(tenant_id),
            )
            .first()
        )
    if branch is None:
        return None

    rows = (
        db.query(BranchEscalationStep)
        .filter(
            BranchEscalationStep.branch_id == int(branch.id),
            BranchEscalationStep.is_active.is_(True),
        )
        .order_by(
            BranchEscalationStep.escalation_level.asc(),
            BranchEscalationStep.sort_order.asc(),
            BranchEscalationStep.id.asc(),
        )
        .all()
    )
    steps: List[PolicyStepView] = []
    for idx, row in enumerate(rows, start=1):
        cid = int(getattr(row, "contact_id", 0) or 0)
        contact = _live_contact(db, cid, int(tenant_id)) if cid else None
        if contact is None:
            continue
        vis = normalize_visibility(getattr(contact, "customer_visibility", INTERNAL_ONLY))
        action = normalize_action(getattr(row, "permitted_action", "") or "")
        share_ok = may_share_with_customer(contact, action=action)
        notify_ok = may_notify_internally(contact, action=action) or action in {
            NOTIFY_OR_HANDOFF, HANDOFF_CONVERSATION,
        }
        live_phone = str(getattr(contact, "phone_e164", "") or "").strip()
        customer_phone = live_phone if share_ok else ""
        steps.append(
            PolicyStepView(
                order=idx,
                contact_id=int(contact.id),
                display_name=str(contact.display_name or ""),
                role=str(contact.role or ""),
                branch_id=int(branch.id),
                permitted_action=action or SHARE_CUSTOMER_CONTACT,
                trigger_condition=str(getattr(row, "trigger_condition", "") or "sequence"),
                customer_visibility=vis,
                live_phone_e164=customer_phone,
                customer_share_allowed=share_ok,
                notify_allowed=bool(notify_ok),
            )
        )

    has_visible = any(s.customer_share_allowed for s in steps)
    has_internal = any(s.customer_visibility in {INTERNAL_ONLY, "both"} and not s.customer_share_allowed for s in steps)
    if not has_internal:
        has_internal = any(
            normalize_visibility(getattr(c, "customer_visibility", "")) == INTERNAL_ONLY
            for c in getattr(branch, "contacts", []) or []
        )

    return CanonicalEscalationPolicy(
        tenant_id=int(tenant_id),
        branch_id=int(branch.id),
        instruction_text=str(getattr(branch, "escalation_instruction_text", "") or ""),
        steps=tuple(steps),
        has_customer_visible_contact=has_visible,
        has_internal_contact=has_internal,
    )


def next_shareable_step(
    policy: CanonicalEscalationPolicy,
    *,
    sent_contact_ids: Sequence[int] = (),
    customer_stated_no_response: bool = False,
    authoritative_nonresponse: bool = False,
) -> Optional[PolicyStepView]:
    """Advance only on customer statement or authoritative event, never elapsed time."""
    shareable = policy.shareable_steps()
    if not shareable:
        return None
    sent = {int(x) for x in sent_contact_ids if int(x or 0)}
    if not sent:
        return shareable[0]
    if not (customer_stated_no_response or authoritative_nonresponse):
        return None
    for step in shareable:
        if step.contact_id not in sent:
            return step
    return None


def resolve_share_action(
    policy: Optional[CanonicalEscalationPolicy],
    *,
    sent_contact_ids: Sequence[int] = (),
    customer_stated_no_response: bool = False,
    authoritative_nonresponse: bool = False,
) -> Dict[str, Any]:
    if policy is None or not policy.steps:
        return {
            "available": False,
            "reason": "no_configured_policy",
            "action": None,
            "contact_id": None,
            "phone_e164": "",
        }
    step = next_shareable_step(
        policy,
        sent_contact_ids=sent_contact_ids,
        customer_stated_no_response=customer_stated_no_response,
        authoritative_nonresponse=authoritative_nonresponse,
    )
    if step is None and not sent_contact_ids:
        return {
            "available": False,
            "reason": "no_customer_visible_contact",
            "action": None,
            "contact_id": None,
            "phone_e164": "",
        }
    if step is None:
        notify = policy.notify_steps()
        if notify:
            return {
                "available": True,
                "reason": "notify_only",
                "action": notify[0].permitted_action,
                "contact_id": notify[0].contact_id,
                "phone_e164": "",
                "ai_remains_active": True,
            }
        return {
            "available": False,
            "reason": "no_further_shareable_step",
            "action": None,
            "contact_id": None,
            "phone_e164": "",
        }
    return {
        "available": True,
        "reason": "share_customer_contact",
        "action": step.permitted_action,
        "contact_id": step.contact_id,
        "phone_e164": step.live_phone_e164,
        "display_name": step.display_name,
        "ai_remains_active": step.permitted_action != HANDOFF_CONVERSATION,
    }


def live_phone_for_contact_id(db: Any, tenant_id: int, contact_id: int) -> str:
    contact = _live_contact(db, int(contact_id), int(tenant_id))
    if contact is None:
        return ""
    return customer_facing_phone(contact)
