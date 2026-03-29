"""
CommercePermissionGuard
────────────────────────
Final gate before any AI action is returned to the caller.

Receives the list of already-policy-gated actions (from PolicyGuard)
and further filters them through the tenant's CommercePermissionSet.

Responsibilities:
  1. Block hardcoded-forbidden actions unconditionally (delete_*, cancel_paid_*, etc.)
  2. Check configurable permission flags per tenant (can_cancel_orders, etc.)
  3. Return an annotated list with permission_result: permitted | denied

This guard does NOT read the database.
It receives a pre-loaded CommercePermissionSet from the route handler.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from .permissions import HARDCODED_FORBIDDEN, CommercePermissionSet

logger = logging.getLogger("ai-orchestrator.commerce_permission_guard")


def gate(
    policy_gated_actions: List[Dict[str, Any]],
    permissions: CommercePermissionSet,
) -> List[Dict[str, Any]]:
    """
    Filter policy-gated actions through the tenant's commerce permission set.

    Accepts the output of PolicyGuard.validate_actions() and adds
    permission_result + permission_notes to each entry.

    An action is executed-eligible only when:
      - policy_result is "approved" or "modified"  (not "blocked")
      - permission_result is "permitted"

    Returns the same list with permission fields appended.
    """
    results: List[Dict[str, Any]] = []

    for action in policy_gated_actions:
        action_type   = action.get("type", "")
        policy_result = action.get("policy_result", "blocked")

        # Already blocked by PolicyGuard — propagate as-is, still add permission fields
        if policy_result == "blocked":
            results.append({
                **action,
                "permission_result": "n/a",
                "permission_notes":  "action was blocked upstream by PolicyGuard",
            })
            continue

        # Hardcoded-forbidden check (before any DB/config check)
        if action_type in HARDCODED_FORBIDDEN:
            reason = permissions.denial_reason(action_type)
            logger.warning(
                f"CommercePermissionGuard DENIED hardcoded-forbidden: "
                f"tenant={permissions.tenant_id} action={action_type}"
            )
            results.append({
                **action,
                "permission_result": "denied",
                "permission_notes":  reason,
                "final_payload":     None,
            })
            continue

        # Configurable permission check
        if permissions.is_permitted(action_type):
            results.append({
                **action,
                "permission_result": "permitted",
                "permission_notes":  "",
            })
        else:
            reason = permissions.denial_reason(action_type)
            logger.info(
                f"CommercePermissionGuard denied: tenant={permissions.tenant_id} "
                f"action={action_type} reason={reason}"
            )
            results.append({
                **action,
                "permission_result": "denied",
                "permission_notes":  reason,
                "final_payload":     None,
            })

    return results


def load_permissions(tenant_id: int) -> CommercePermissionSet:
    """
    Load tenant commerce permissions from the database.
    Falls back to safe defaults if no record exists.
    """
    import os
    import sys

    _REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    if _REPO_ROOT not in sys.path:
        sys.path.insert(0, _REPO_ROOT)

    from database.models import CommercePermissions
    from database.session import SessionLocal

    db = SessionLocal()
    try:
        row = db.query(CommercePermissions).filter(
            CommercePermissions.tenant_id == tenant_id
        ).first()

        if not row:
            # No record — return safe defaults
            return CommercePermissionSet(tenant_id=tenant_id)

        return CommercePermissionSet(
            tenant_id=tenant_id,
            can_create_orders=bool(row.can_create_orders),
            can_create_checkout_links=bool(row.can_create_checkout_links),
            can_send_payment_links=bool(row.can_send_payment_links),
            can_apply_coupons=bool(row.can_apply_coupons),
            can_auto_generate_coupons=bool(row.can_auto_generate_coupons),
            can_cancel_orders=bool(row.can_cancel_orders),
        )
    finally:
        db.close()
