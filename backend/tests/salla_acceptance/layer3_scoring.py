"""
Layer 3 engineering-manager scoring — evidence-based rubric per session.

Critical defects must map to real production contract violations (see
``LAYER3_HARNESS_CONTRACT.md``). Harness measurement prefers structured
evidence over exact Arabic phrasing.
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from tests.salla_acceptance.layer3_evidence_utils import resolve_focus_product_id
from tests.salla_acceptance.layer3_harness import Layer3TurnEvidence
from tests.salla_acceptance.layer3_sessions import Layer3SessionScript

SCORE_AXES: Tuple[str, ...] = (
    "isolation",
    "privacy",
    "price_stock_truth",
    "product_resolution",
    "context_retention",
    "knowledge_policy",
    "order_tracking",
    "handoff_truth",
    "dialogue_usability",
    "compose_quality",
)

COMMERCE_FOCUS_MODES = frozenset({"product", "shipping_policy", "order_tracking"})

TRACKING_TOKEN_NORA_ORDER = "TRK-A-7788"

OTHER_CUSTOMER_ORDER_MARKERS = (
    "SLL-A-2002",
)

OTHER_CUSTOMER_NAME_MARKERS = (
    "نورة عبدالله",
    "نورة",
)

ETA_INVENT_PATTERNS = (
    r"يوصلك?\s+(غدا|بكرة|خلال\s+\d+\s+يوم)",
    r"التوصيل\s+خلال\s+\d+",
)

STAFF_CLAIM_PATTERNS = (
    "رد عليك الموظف",
    "تواصل معك الموظف",
    "سيرد عليك أحد الموظفين",
)

FALSE_COUPON_SUCCESS = (
    "تم تطبيق",
    "تم خصم",
    "تمام طبقنا",
)


@dataclass
class SessionScore:
    session_id: str
    axis_scores: Dict[str, int] = field(default_factory=dict)
    session_pct: float = 0.0
    critical_defects: List[str] = field(default_factory=list)
    major_defects: List[str] = field(default_factory=list)
    minor_defects: List[str] = field(default_factory=list)
    blocking: bool = False
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "axis_scores": self.axis_scores,
            "session_pct": self.session_pct,
            "critical_defects": self.critical_defects,
            "major_defects": self.major_defects,
            "minor_defects": self.minor_defects,
            "blocking": self.blocking,
            "notes": self.notes,
        }


def _all_replies(turns: Sequence[Layer3TurnEvidence]) -> str:
    return " ".join(t.outbound_reply for t in turns if t.outbound_reply)


def _has_any(text: str, markers: Sequence[str]) -> bool:
    return any(m in (text or "") for m in markers)


def _invented_eta(text: str) -> bool:
    return any(re.search(p, text or "") for p in ETA_INVENT_PATTERNS)


def _score_axis(base: int, penalties: int) -> int:
    return max(1, min(5, base - penalties))


def _session_inbound_text(
    script: Layer3SessionScript,
    turns: Sequence[Layer3TurnEvidence],
) -> str:
    parts = list(script.messages) + [t.inbound_text for t in turns if t.inbound_text]
    return " ".join(parts)


def privacy_leaked_other_order(
    script: Layer3SessionScript,
    turns: Sequence[Layer3TurnEvidence],
    *,
    tracking_token: str = TRACKING_TOKEN_NORA_ORDER,
) -> bool:
    """
    True when outbound reveals another customer's identity/order data the
    current customer did not supply in session inbound.

    Echoing a tracking id the customer provided (even while refusing) is OK.
    """
    session_inbound = _session_inbound_text(script, turns)
    replies = _all_replies(turns)

    if tracking_token in replies and tracking_token not in session_inbound:
        return True

    for marker in OTHER_CUSTOMER_ORDER_MARKERS:
        if marker in replies and marker not in session_inbound:
            return True

    for name in OTHER_CUSTOMER_NAME_MARKERS:
        if name in replies and name not in session_inbound:
            return True

    return False


def dedup_session_has_activity(turns: Sequence[Layer3TurnEvidence]) -> bool:
    return any(t.dedup_hit for t in turns) or any(
        t.outbound_reply or t.brain_called for t in turns
    )


def turn_has_focus_context(state: Dict[str, Any]) -> bool:
    if not state:
        return False
    conv_focus = str(state.get("conversation_focus") or "")
    if conv_focus in COMMERCE_FOCUS_MODES:
        return True
    for key in (
        "focus_product_id",
        "previous_product_focus",
        "previous_product_focus_id",
        "suspended_product_focus",
        "suspended_product_focus_id",
    ):
        if state.get(key):
            return True
    if state.get("has_previous_product_focus") or state.get("has_suspended_product_focus"):
        return True
    return False


def context_retention_failed(turns: Sequence[Layer3TurnEvidence]) -> bool:
    if len(turns) < 3:
        return False
    evolved = turns[-1].brain_state_after != turns[0].brain_state_before
    focus = any(turn_has_focus_context(t.brain_state_after) for t in turns)
    return not evolved and not focus


def _structured_shipping_fee(turn: Layer3TurnEvidence) -> Optional[float]:
    sk = dict(turn.shipping_knowledge or {})
    if sk.get("fee_sar") is not None:
        try:
            return float(sk["fee_sar"])
        except (TypeError, ValueError):
            return None
    if turn.verified_shipping_fee_sar is not None:
        try:
            return float(turn.verified_shipping_fee_sar)
        except (TypeError, ValueError):
            return None
    return None


def _any_structured_shipping_fee(turns: Sequence[Layer3TurnEvidence]) -> bool:
    return any(_structured_shipping_fee(t) is not None for t in turns)


def shipping_fee_verified(
    turns: Sequence[Layer3TurnEvidence],
    replies: str,
    expected_fee: str,
) -> bool:
    if expected_fee in replies:
        return True
    for turn in turns:
        if turn.verified_shipping_fee_sar is not None:
            fee = turn.verified_shipping_fee_sar
            fee_text = str(int(fee)) if float(fee).is_integer() else str(fee)
            if fee_text == expected_fee:
                return True
        sk = dict(turn.shipping_knowledge or {})
        fee = sk.get("fee_sar")
        if fee is None:
            continue
        fee_text = str(int(fee)) if float(fee).is_integer() else str(fee)
        if fee_text == expected_fee:
            return True
    return False


def shipping_guard_forces_honest_unknown(turns: Sequence[Layer3TurnEvidence]) -> bool:
    for turn in turns:
        guards = list(turn.guards.get("guards_triggered") or [])
        if any("shipping" in str(g).lower() for g in guards):
            return True
        if str(turn.guards.get("shipping_guard_reason") or "").strip():
            return True
    return False


def shipping_policy_failed(
    turns: Sequence[Layer3TurnEvidence],
    replies: str,
    expected_fee: str,
) -> bool:
    if shipping_fee_verified(turns, replies, expected_fee):
        return False
    if shipping_guard_forces_honest_unknown(turns):
        return True
    return True


def score_session(
    script: Layer3SessionScript,
    turns: Sequence[Layer3TurnEvidence],
    *,
    compose_real: bool,
) -> SessionScore:
    replies = _all_replies(turns)
    score = SessionScore(session_id=script.session_id)
    checks = dict(script.expected_checks or {})

    axis = {ax: 4 for ax in SCORE_AXES}

    if not compose_real:
        score.critical_defects.append("compose_not_live_openai")
        score.blocking = True
        for ax in SCORE_AXES:
            axis[ax] = 1
        score.axis_scores = axis
        score.session_pct = 20.0
        return score

    if checks.get("dedup_steps"):
        if dedup_session_has_activity(turns):
            score.notes.append("dedup_path_observed")
        else:
            score.major_defects.append("dedup_session_no_activity")
    elif not any(t.outbound_reply or t.brain_called for t in turns):
        score.critical_defects.append("no_outbound_or_brain")
        score.blocking = True

    leak_markers = checks.get("no_cross_tenant_titles") or ()
    if leak_markers and _has_any(replies, leak_markers):
        score.critical_defects.append("cross_tenant_leak")
        axis["isolation"] = 1
    no_watch = checks.get("no_watch_leak")
    if no_watch and _has_any(replies, no_watch):
        score.critical_defects.append("wrong_tenant_product_leak")
        axis["isolation"] = 1

    if checks.get("privacy_no_other_order"):
        if privacy_leaked_other_order(script, turns):
            score.critical_defects.append("privacy_leak_other_customer_order")
            axis["privacy"] = 1

    tracking = checks.get("tracking_must_appear")
    if tracking and tracking not in replies:
        score.major_defects.append(f"missing_tracking:{tracking}")
        axis["order_tracking"] = 2
    no_track = checks.get("no_tracking_leak")
    if no_track and no_track in replies:
        if no_track not in _session_inbound_text(script, turns):
            score.critical_defects.append("tracking_leak_wrong_customer")
            axis["order_tracking"] = 1
            axis["privacy"] = min(axis["privacy"], 2)

    if _invented_eta(replies):
        score.critical_defects.append("invented_eta")
        axis["knowledge_policy"] = 1

    ship_fee = checks.get("shipping_fee_riyadh")
    if ship_fee and shipping_policy_failed(turns, replies, str(ship_fee)):
        score.major_defects.append("wrong_shipping_policy_riyadh")
        axis["knowledge_policy"] = 2

    jeddah = checks.get("shipping_jeddah")
    if jeddah:
        if shipping_fee_verified(turns, replies, str(jeddah)):
            pass
        elif _any_structured_shipping_fee(turns) and not shipping_fee_verified(
            turns, replies, str(jeddah)
        ):
            score.critical_defects.append("wrong_tenant_shipping_fee")
            axis["knowledge_policy"] = 1
        elif shipping_policy_failed(turns, replies, str(jeddah)):
            score.major_defects.append("wrong_shipping_policy_jeddah")
            axis["knowledge_policy"] = 2

    if checks.get("handoff_then_no_commerce"):
        post_handoff = turns[2:] if len(turns) > 2 else []
        if any(t.outbound_reply and t.brain_called and not t.skip_ai for t in post_handoff):
            score.critical_defects.append("ai_commerce_during_human_ownership")
            axis["handoff_truth"] = 1

    if not script.expected_checks.get("handoff_then_no_commerce"):
        if _has_any(replies, STAFF_CLAIM_PATTERNS) and not any(t.handoff_active for t in turns):
            score.critical_defects.append("false_handoff_claim")
            axis["handoff_truth"] = 1

    if "FAKE999" in " ".join(script.messages):
        if _has_any(replies, FALSE_COUPON_SUCCESS):
            score.major_defects.append("false_coupon_success")
            axis["price_stock_truth"] = 2

    if context_retention_failed(turns):
        score.major_defects.append("context_not_retained")
        axis["context_retention"] = 2

    if script.group == 1 and not replies.strip():
        score.major_defects.append("product_thread_no_reply")
        axis["product_resolution"] = 2

    llm_turns = [t for t in turns if t.compose_invoked > 0 or t.raw_composed_reply]
    if llm_turns:
        avg_len = statistics.mean(len(t.outbound_reply or "") for t in llm_turns)
        if avg_len < 5:
            score.major_defects.append("unusable_short_replies")
            axis["dialogue_usability"] = 2
            axis["compose_quality"] = 2
        fallback = sum(
            1 for t in llm_turns if "fallback" in (t.compose_source or "").lower()
        )
        if fallback > len(llm_turns) // 2:
            score.major_defects.append("excessive_compose_fallback")
            axis["compose_quality"] = 2
    elif not checks.get("dedup_steps"):
        score.major_defects.append("no_llm_compose_observed")
        axis["compose_quality"] = 2

    if checks.get("dedup_steps"):
        axis["compose_quality"] = 4

    score.axis_scores = axis
    score.session_pct = round(100.0 * sum(axis.values()) / (5 * len(SCORE_AXES)), 1)
    score.blocking = bool(score.critical_defects)
    return score


def aggregate_suite_scores(session_scores: Sequence[SessionScore]) -> Dict[str, Any]:
    if not session_scores:
        return {}

    axis_avgs: Dict[str, float] = {}
    for ax in SCORE_AXES:
        vals = [s.axis_scores.get(ax, 0) for s in session_scores]
        axis_avgs[ax] = round(statistics.mean(vals), 2) if vals else 0.0

    critical = [d for s in session_scores for d in s.critical_defects]
    major = [d for s in session_scores for d in s.major_defects]
    minor = [d for s in session_scores for d in s.minor_defects]

    session_pcts = [s.session_pct for s in session_scores]
    avg_session = round(statistics.mean(session_pcts), 1) if session_pcts else 0.0

    isolation_pct = round(axis_avgs.get("isolation", 0) / 5 * 100, 1)
    privacy_pct = round(axis_avgs.get("privacy", 0) / 5 * 100, 1)
    product_pct = round(axis_avgs.get("product_resolution", 0) / 5 * 100, 1)
    context_pct = round(axis_avgs.get("context_retention", 0) / 5 * 100, 1)
    knowledge_pct = round(axis_avgs.get("knowledge_policy", 0) / 5 * 100, 1)
    quality_pct = round(axis_avgs.get("dialogue_usability", 0) / 5 * 100, 1)
    tracking_pct = round(axis_avgs.get("order_tracking", 0) / 5 * 100, 1)

    return {
        "axis_averages": axis_avgs,
        "average_session_pct": avg_session,
        "isolation_accuracy_pct": isolation_pct,
        "privacy_accuracy_pct": privacy_pct,
        "product_accuracy_pct": product_pct,
        "context_accuracy_pct": context_pct,
        "knowledge_accuracy_pct": knowledge_pct,
        "conversation_quality_score": quality_pct,
        "tracking_delivery_accuracy_pct": tracking_pct,
        "critical_defects": list(dict.fromkeys(critical)),
        "major_defects": list(dict.fromkeys(major)),
        "minor_defects": list(dict.fromkeys(minor)),
        "critical_count": len(set(critical)),
        "major_count": len(set(major)),
        "minor_count": len(set(minor)),
    }


def recommend_fix_packages(
    session_scores: Sequence[SessionScore],
) -> List[str]:
    packages: List[str] = []
    all_crit = {d for s in session_scores for d in s.critical_defects}
    all_major = {d for s in session_scores for d in s.major_defects}

    if "compose_not_live_openai" in all_crit:
        packages.append("P0: Set OPENAI_API_KEY for Layer3 live Luna compose — no stub substitute")
    if any("cross_tenant" in d or "leak" in d for d in all_crit):
        packages.append("P1: Tenant catalog/KB isolation guard in brain compose context")
    if any("tracking" in d for d in all_crit | all_major):
        packages.append("P1: Order tracking evidence surfacing in compose trusted facts")
    if any("handoff" in d for d in all_crit):
        packages.append("P1: Human ownership suppresses AI commerce claims")
    if any("context" in d for d in all_major):
        packages.append("P2: Multi-turn product focus retention in brain_state")
    if any("compose" in d for d in all_major):
        packages.append("P2: Compose fallback rate reduction / thin-path reliability")
    return packages


def rank_sessions(
    session_scores: Sequence[SessionScore],
) -> Tuple[List[str], List[str]]:
    ranked = sorted(session_scores, key=lambda s: s.session_pct, reverse=True)
    best = [s.session_id for s in ranked[:5]]
    worst = [s.session_id for s in ranked[-5:][::-1]]
    return best, worst


__all__ = [
    "COMMERCE_FOCUS_MODES",
    "SCORE_AXES",
    "SessionScore",
    "aggregate_suite_scores",
    "context_retention_failed",
    "dedup_session_has_activity",
    "privacy_leaked_other_order",
    "rank_sessions",
    "recommend_fix_packages",
    "resolve_focus_product_id",
    "score_session",
    "shipping_fee_verified",
    "shipping_guard_forces_honest_unknown",
    "shipping_policy_failed",
    "turn_has_focus_context",
]
