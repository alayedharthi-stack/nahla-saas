"""
ActionExecutionGuard
─────────────────────
The final gate in the AI Orchestrator pipeline.

Receives the fully-gated action list (after PolicyGuard + CommercePermissionGuard)
and computes a definitive execution decision for each action.

Architecture rule:
  Claude AI does not execute commerce actions.
  It proposes them. This guard decides whether they become executable.

Decision rule — an action is executable ONLY when ALL of:
  1. policy_result ∈ {"approved", "modified"}   (PolicyGuard passed)
  2. permission_result == "permitted"            (CommercePermissionGuard passed)

blocked_reason is always the FIRST failing gate's message, so callers know
exactly which layer blocked the action and why.

Outputs per action:
  executable: bool
  blocked_reason: str | None     — None when executable=True
  execution_stage: str           — "none" | "policy" | "permission"

Logs (at INFO for normal flow, WARNING for forbidden attempts):
  EXECUTABLE      — action cleared all gates
  BLOCKED_POLICY  — PolicyGuard blocked it
  BLOCKED_PERMISSION — CommercePermissionGuard denied it
  SKIPPED         — already blocked upstream, nothing new to log
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger("ai-orchestrator.execution")

# Policy results that allow execution to proceed
_POLICY_PASS = {"approved", "modified"}


@dataclass
class ExecutionDecision:
    """Final execution verdict for a single proposed action."""
    action_type: str
    executable: bool
    blocked_reason: Optional[str]   # None when executable=True
    execution_stage: str            # "none" | "policy" | "permission"
    # Pass-through fields for the full action record
    policy_result: str
    policy_notes: str
    permission_result: str
    permission_notes: str
    final_payload: Optional[Dict[str, Any]]


def decide(
    fully_gated_actions: List[Dict[str, Any]],
    tenant_id: int,
) -> List[Dict[str, Any]]:
    """
    Compute execution decisions for all actions.

    Accepts the output of CommercePermissionGuard.gate() and adds:
      - executable: bool
      - blocked_reason: str | None
      - execution_stage: str

    Returns the same list with these fields appended.
    The caller uses this list directly in the API response.
    """
    results: List[Dict[str, Any]] = []

    for action in fully_gated_actions:
        action_type       = action.get("type", "unknown")
        policy_result     = action.get("policy_result", "blocked")
        policy_notes      = action.get("policy_notes", "")
        permission_result = action.get("permission_result", "n/a")
        permission_notes  = action.get("permission_notes", "")
        final_payload     = action.get("final_payload")

        decision = _evaluate(
            action_type, policy_result, policy_notes,
            permission_result, permission_notes, final_payload,
            tenant_id,
        )

        results.append({
            **action,
            "executable":       decision.executable,
            "blocked_reason":   decision.blocked_reason,
            "execution_stage":  decision.execution_stage,
        })

    return results


def _evaluate(
    action_type: str,
    policy_result: str,
    policy_notes: str,
    permission_result: str,
    permission_notes: str,
    final_payload: Optional[Dict],
    tenant_id: int,
) -> ExecutionDecision:

    # ── Gate 1: PolicyGuard ───────────────────────────────────────────────────
    if policy_result not in _POLICY_PASS:
        reason = f"policy: {policy_notes}" if policy_notes else f"policy: result={policy_result}"
        logger.info(
            f"BLOCKED_POLICY | tenant={tenant_id} action={action_type} | {policy_notes}"
        )
        return ExecutionDecision(
            action_type=action_type,
            executable=False,
            blocked_reason=reason,
            execution_stage="policy",
            policy_result=policy_result,
            policy_notes=policy_notes,
            permission_result=permission_result,
            permission_notes=permission_notes,
            final_payload=None,
        )

    # ── Gate 2: CommercePermissionGuard ───────────────────────────────────────
    if permission_result != "permitted":
        reason = (
            f"permission: {permission_notes}" if permission_notes
            else f"permission: result={permission_result}"
        )
        log_fn = logger.warning if permission_result == "denied" else logger.info
        log_fn(
            f"BLOCKED_PERMISSION | tenant={tenant_id} action={action_type} | {permission_notes}"
        )
        return ExecutionDecision(
            action_type=action_type,
            executable=False,
            blocked_reason=reason,
            execution_stage="permission",
            policy_result=policy_result,
            policy_notes=policy_notes,
            permission_result=permission_result,
            permission_notes=permission_notes,
            final_payload=None,
        )

    # ── All gates passed ──────────────────────────────────────────────────────
    logger.info(
        f"EXECUTABLE | tenant={tenant_id} action={action_type} | "
        f"policy={policy_result} permission={permission_result}"
    )
    return ExecutionDecision(
        action_type=action_type,
        executable=True,
        blocked_reason=None,
        execution_stage="none",
        policy_result=policy_result,
        policy_notes=policy_notes,
        permission_result=permission_result,
        permission_notes=permission_notes,
        final_payload=final_payload,
    )
