"""Idempotent tenant-scoped normalization of legacy contact/escalation config.

Does not recreate a specific tenant. Does not invent contacts or numbers.
Visibility is derived from prior delivery behavior (default reception /
first ladder step), never from employee role names.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from modules.operations.contact_visibility import (
    CUSTOMER_VISIBLE,
    INTERNAL_ONLY,
    NOTIFY_OR_HANDOFF,
    SHARE_CUSTOMER_CONTACT,
    normalize_visibility,
)

logger = logging.getLogger("nahla.operations.escalation_policy_migration")


def normalize_tenant_escalation_config(db: Any, tenant_id: int) -> Dict[str, Any]:
    from models import BranchContact, BranchEscalationStep, MerchantBranch  # noqa: PLC0415

    summary = {
        "tenant_id": int(tenant_id),
        "contacts_scanned": 0,
        "contacts_visibility_set": 0,
        "steps_linked": 0,
        "steps_action_set": 0,
        "phones_synced": 0,
    }
    branches = (
        db.query(MerchantBranch)
        .filter(MerchantBranch.tenant_id == int(tenant_id))
        .all()
    )
    for branch in branches:
        contacts = (
            db.query(BranchContact)
            .filter(BranchContact.branch_id == int(branch.id))
            .all()
        )
        steps = (
            db.query(BranchEscalationStep)
            .filter(BranchEscalationStep.branch_id == int(branch.id))
            .order_by(
                BranchEscalationStep.escalation_level.asc(),
                BranchEscalationStep.sort_order.asc(),
                BranchEscalationStep.id.asc(),
            )
            .all()
        )
        first_level = min((int(s.escalation_level or 1) for s in steps), default=None)
        first_level_contact_ids = {
            int(s.contact_id)
            for s in steps
            if first_level is not None
            and int(s.escalation_level or 1) == first_level
            and s.contact_id
        }
        by_phone = {
            str(c.phone_e164 or "").strip(): c
            for c in contacts
            if str(c.phone_e164 or "").strip()
        }

        already_authored = bool(getattr(branch, "escalation_policy_json", None))
        for contact in contacts:
            summary["contacts_scanned"] += 1
            current = normalize_visibility(getattr(contact, "customer_visibility", "") or INTERNAL_ONLY)
            if already_authored:
                vis = current
            elif bool(getattr(contact, "is_default_reception", False)):
                vis = CUSTOMER_VISIBLE
            elif int(contact.id) in first_level_contact_ids:
                vis = CUSTOMER_VISIBLE
            else:
                vis = current if current in {CUSTOMER_VISIBLE, INTERNAL_ONLY, "both"} else INTERNAL_ONLY
            if str(getattr(contact, "customer_visibility", "") or "") != vis:
                contact.customer_visibility = vis
                summary["contacts_visibility_set"] += 1

        for step in steps:
            if not step.contact_id:
                linked = by_phone.get(str(step.phone_e164 or "").strip())
                if linked is not None:
                    step.contact_id = int(linked.id)
                    summary["steps_linked"] += 1
            contact = None
            if step.contact_id:
                contact = next((c for c in contacts if int(c.id) == int(step.contact_id)), None)
            if contact is not None:
                if str(step.phone_e164 or "") != str(contact.phone_e164 or ""):
                    step.phone_e164 = contact.phone_e164
                    summary["phones_synced"] += 1
                step.display_name = contact.display_name
                step.role = contact.role
                vis = normalize_visibility(getattr(contact, "customer_visibility", INTERNAL_ONLY))
                desired_action = (
                    SHARE_CUSTOMER_CONTACT
                    if vis in {CUSTOMER_VISIBLE, "both"}
                    else NOTIFY_OR_HANDOFF
                )
                current_action = str(getattr(step, "permitted_action", "") or "").strip()
                should_set_action = (not current_action) or (
                    current_action == SHARE_CUSTOMER_CONTACT and vis == INTERNAL_ONLY
                )
                if should_set_action and current_action != desired_action:
                    step.permitted_action = desired_action
                    summary["steps_action_set"] += 1
                if not str(getattr(step, "trigger_condition", "") or "").strip():
                    step.trigger_condition = "sequence"
    return summary


def normalize_all_tenants(db: Any) -> Dict[str, Any]:
    from models import Tenant  # noqa: PLC0415

    scanned = 0
    changed = 0
    for tenant in db.query(Tenant).all():
        scanned += 1
        summary = normalize_tenant_escalation_config(db, int(tenant.id))
        if (
            summary["contacts_visibility_set"]
            or summary["steps_linked"]
            or summary["steps_action_set"]
            or summary["phones_synced"]
        ):
            changed += 1
            logger.info(
                "[ESCALATION_POLICY_MIGRATION] tenant=%s contacts_set=%s linked=%s actions=%s phones=%s",
                tenant.id,
                summary["contacts_visibility_set"],
                summary["steps_linked"],
                summary["steps_action_set"],
                summary["phones_synced"],
            )
    return {"scanned": scanned, "changed": changed}
