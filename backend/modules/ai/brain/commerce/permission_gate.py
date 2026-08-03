"""
Brain action → commerce permission gate helpers.
"""
from __future__ import annotations

import logging
from typing import Dict, Optional

from modules.ai.commerce.permissions import CommercePermissionSet
from modules.ai.brain.decision.actions import (
    ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_SEND_PAYMENT_LINK,
    ACTION_SUGGEST_COUPON,
)
from modules.ai.brain.types import BrainContext

logger = logging.getLogger("nahla.brain.commerce.permission_gate")

BRAIN_ACTION_TO_PERMISSION: Dict[str, str] = {
    ACTION_PROPOSE_DRAFT_ORDER: "create_draft_order",
    ACTION_SEND_PAYMENT_LINK: "send_payment_link",
    ACTION_SUGGEST_COUPON: "apply_coupon",
}


def permissions_for_context(ctx: BrainContext) -> CommercePermissionSet:
    perms = getattr(ctx, "commerce_permissions", None)
    if perms is not None:
        return perms
    return CommercePermissionSet(tenant_id=int(ctx.tenant_id or 0))


def deny_reason_for_brain_action(ctx: BrainContext, action: str) -> Optional[str]:
    perm_action = BRAIN_ACTION_TO_PERMISSION.get(action)
    if not perm_action:
        return None
    perms = permissions_for_context(ctx)
    if perms.is_permitted(perm_action):
        return None
    reason = perms.denial_reason(perm_action)
    logger.info(
        "[brain.permission_gate] denied tenant=%s action=%s perm_action=%s "
        "source=%s reason=%s",
        ctx.tenant_id,
        action,
        perm_action,
        getattr(ctx, "permission_source", ""),
        reason,
    )
    return reason or f"store permission denied for action '{action}'"
