"""
turn/observability.py
─────────────────────
Production observation helpers for Turn Arbiter shadow/enforce rollout.

Read-only — grep-friendly log fields and outcome classification.
"""
from __future__ import annotations

from typing import Any, Optional

from .contract import TurnShadowTelemetry

OUTCOME_SUCCESS = "success"
OUTCOME_FALSE_ENFORCE = "false_enforce"
OUTCOME_MISSED_MISMATCH = "missed_mismatch"
OUTCOME_COMPOSER_TONE_ISSUE = "composer_tone_issue"
OUTCOME_NO_MISMATCH = "no_mismatch"
OUTCOME_SHADOW_ONLY = "shadow_only"

GREP_PATTERNS = {
    "shadow_all": "[TURN_ARBITER_SHADOW]",
    "enforce_all": "[TURN_ARBITER_ENFORCE] enforced=true",
    "checkout_vs_support": "mismatch_type=checkout_vs_support",
    "checkout_vs_discovery": "mismatch_type=checkout_vs_discovery",
    "staff_vs_persona": "mismatch_type=staff_vs_persona",
    "owner_brief_compose": "[TURN_OWNER_BRIEF_COMPOSE]",
    "outcome_log": "[TURN_ARBITER_OUTCOME]",
}


def classify_turn_outcome(
    telemetry: TurnShadowTelemetry,
    *,
    enforced: bool = False,
    compose_used_brief: bool = False,
    reply_text: Optional[str] = None,
) -> str:
    """
    Classify one turn for rescue rollout review.

    Categories:
    - success: mismatch detected and (enforced or brief-guided compose applied)
    - false_enforce: enforced but owners were compatible or mismatch not allowlisted
    - missed_mismatch: owner_mismatch but not enforced (enforce off or not allowlisted)
    - composer_tone_issue: enforced/brief present but reply still looks template-heavy
    - no_mismatch: owners aligned
    - shadow_only: telemetry only, no enforce/brief compose
    """
    if not telemetry.owner_mismatch:
        return OUTCOME_NO_MISMATCH

    if not enforced:
        return OUTCOME_MISSED_MISMATCH

    if enforced and telemetry.mismatch_type == "none":
        return OUTCOME_FALSE_ENFORCE

    if compose_used_brief or enforced:
        if _looks_template_heavy(reply_text):
            return OUTCOME_COMPOSER_TONE_ISSUE
        return OUTCOME_SUCCESS

    return OUTCOME_SHADOW_ONLY


def _looks_template_heavy(reply_text: Optional[str]) -> bool:
    """Heuristic only for ops classification — not a guard."""
    if not reply_text:
        return False
    text = reply_text.strip()
    if len(text) < 20:
        return False
    template_markers = (
        "ما المدينة التي سيصلها الطلب",
        "يرجى إرسال",
        "كود الخصم المتاح",
        "يسعدنا خدمتك",
        "نحن هنا لمساعدتك",
    )
    hits = sum(1 for m in template_markers if m in text)
    return hits >= 2


def outcome_log_fields(
    telemetry: TurnShadowTelemetry,
    *,
    outcome: str,
    enforced: bool = False,
    compose_used_brief: bool = False,
) -> dict[str, Any]:
    """Flat dict for structured outcome logging."""
    return {
        "outcome": outcome,
        "mismatch_type": telemetry.mismatch_type,
        "proposed_owner": telemetry.proposed_owner,
        "legacy_owner": telemetry.legacy_owner,
        "legacy_action": telemetry.legacy_action,
        "enforced": enforced,
        "compose_used_brief": compose_used_brief,
        "reply_goal": telemetry.reply_goal,
        "compose_mode": telemetry.compose_mode,
        "confidence": telemetry.confidence,
        "should_suspend_stale_state": telemetry.should_suspend_stale_state,
    }


def log_turn_outcome(
    logger: Any,
    *,
    tenant_id: Any,
    telemetry: TurnShadowTelemetry,
    enforced: bool = False,
    compose_used_brief: bool = False,
    reply_preview: str = "",
) -> str:
    """Emit a single grep-friendly outcome line for production review."""
    outcome = classify_turn_outcome(
        telemetry,
        enforced=enforced,
        compose_used_brief=compose_used_brief,
        reply_text=reply_preview,
    )
    fields = outcome_log_fields(
        telemetry,
        outcome=outcome,
        enforced=enforced,
        compose_used_brief=compose_used_brief,
    )
    logger.info(
        "[TURN_ARBITER_OUTCOME] tenant=%s outcome=%s mismatch_type=%s "
        "proposed_owner=%s legacy_owner=%s enforced=%s compose_brief=%s "
        "reply_goal=%s compose_mode=%s preview=%r",
        tenant_id,
        fields["outcome"],
        fields["mismatch_type"],
        fields["proposed_owner"],
        fields["legacy_owner"],
        str(fields["enforced"]).lower(),
        str(fields["compose_used_brief"]).lower(),
        fields["reply_goal"],
        fields["compose_mode"],
        (reply_preview or "")[:80],
    )
    return outcome


__all__ = [
    "GREP_PATTERNS",
    "OUTCOME_COMPOSER_TONE_ISSUE",
    "OUTCOME_FALSE_ENFORCE",
    "OUTCOME_MISSED_MISMATCH",
    "OUTCOME_NO_MISMATCH",
    "OUTCOME_SHADOW_ONLY",
    "OUTCOME_SUCCESS",
    "classify_turn_outcome",
    "log_turn_outcome",
    "outcome_log_fields",
]
