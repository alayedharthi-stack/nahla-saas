"""
brain/relational/dedup_suppression.py
─────────────────────────────────────
Relational/seasonal-aware suppression gate for the Webhook Outbound
Dedup Guard. Wave 3 hotfix (May 2026), commit W3.2.

Why this module exists
──────────────────────
Production audit on Tenant 33 during Eid season surfaced a category
error in the post-Brain dedup pipeline: when a customer sent two
religious greetings ("بارك الله فيك" / "الله يحفظك") in a row, the
Brain composed two warm replies and the Webhook Outbound Dedup Guard
substituted the second one with the canned line:

    "هذي نفس الإجابة قبل قليل — قلي على وجه التحديد إيش الناقص."

That line is correct for transactional loops ("كم السعر؟" → same
price → again → "what specifically do you want?"). It is wrong for
ritual exchanges, where high lexical overlap is *what makes the
exchange correct*.

What this module is
───────────────────
A pure decision layer. Given:
  * the customer's inbound text,
  * the relational moment computed upstream (may be empty when the
    relational layer is OFF),
  * the dedup overlap score (for telemetry only),
it returns a typed decision:

    DedupSuppressionDecision(suppress: bool, reason: str, ...)

The Webhook Outbound Dedup Guard (only call site) reads
``decision.suppress`` and either skips its substitution (gate fired)
or runs the legacy path (gate inert).

What this module is NOT
───────────────────────
* NOT a Brain prompt overlay.
* NOT a state-mutation layer (no DB, no I/O, no side effects).
* NOT a generic "disable dedup" switch — see the BLOCK list below.
* NOT wired into the AI Pause Guard / Loop Detector. W3.4 is deferred.
* NOT tenant-specific. The kill switch
  ``RELATIONAL_DEDUP_SUPPRESSION_ENABLED`` is global.

Architectural rule (pinned)
───────────────────────────
    The gate may suppress the *post-Brain dedup substitution*. It
    must never alter Brain output text, conversation state, or any
    other safety-net / payment / order / handoff path.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any, FrozenSet, Optional, Tuple

from .moments import ConversationMoment

logger = logging.getLogger("nahla.relational")

# ── Kill switch ─────────────────────────────────────────────────────
# Default OFF. When OFF, the gate is inert: every call returns a
# decision with ``suppress=False`` and ``reason='flag_off'`` and the
# legacy dedup substitution path runs unchanged.

_FLAG_NAME = "RELATIONAL_DEDUP_SUPPRESSION_ENABLED"


def is_relational_dedup_suppression_enabled() -> bool:
    """Read the kill switch. Always returns ``False`` if the env
    var is missing / unparseable / set to a falsy value.

    Falsy values (case-insensitive): ``""``, ``"0"``, ``"false"``,
    ``"off"``, ``"no"``.
    """
    raw = os.getenv(_FLAG_NAME, "")
    if not raw:
        return False
    return raw.strip().lower() not in ("0", "false", "off", "no", "")


# ── Arabic normaliser (local copy to avoid coupling state.py) ──────
# Mirrors :func:`relational.state._normalise_arabic` exactly.

_AR_DIACRITICS_RE = re.compile(r"[\u064B-\u065F\u0670]")


def _normalise_arabic(text: Optional[str]) -> str:
    if not text:
        return ""
    try:
        t = _AR_DIACRITICS_RE.sub("", str(text))
        t = t.replace("ـ", "")
        t = (
            t.replace("أ", "ا")
             .replace("إ", "ا")
             .replace("آ", "ا")
             .replace("ى", "ي")
             .replace("ة", "ه")
        )
        return t.lower().strip()
    except Exception:
        return ""


# ── Marker phrase sets ─────────────────────────────────────────────
# Closed, deliberate vocabularies. We do NOT broaden these without
# production telemetry showing a real miss; the goal is to suppress
# dedup ONLY where the relationship demands it.
#
# All entries are pre-normalised at module import time so the
# matcher does not pay normalisation cost per call.

# Pure religious supplication / blessing / ritual formulas. NO
# bare gratitude tokens here — those go through GRATITUDE_GENERIC.
_RELIGIOUS_RITUAL_PHRASES_RAW: Tuple[str, ...] = (
    # Blessings / supplications
    "بارك الله فيك", "بارك الله فيكم", "بارك الله",
    "الله يبارك", "الله يبارك فيك", "الله يبارك فيكم",
    "الله يحفظك", "الله يحفظكم", "الله يرعاك", "الله يرعاكم",
    "الله يوفقك", "الله يوفقكم", "وفقك الله", "وفقكم الله",
    "ربي يحفظك", "ربي يحفظكم", "ربي يجزاك", "ربي يجزاكم",
    "في حفظ الله", "في رعاية الله", "في امان الله",
    "الله يطول في عمرك", "الله يطول عمرك",
    "الله يجعله في ميزان حسناتك", "الله يجعلها في ميزان حسناتك",
    # Hadith-style ritual responses
    "وفيك بارك", "وفيكم بارك", "وانتم بخير", "وانت من اهله",
    # Praise / glorification
    "الحمد لله", "ما شاء الله", "اللهم بارك", "اللهم صلي",
    "صلى الله عليه وسلم", "سبحان الله", "لا حول ولا قوه الا بالله",
    # Acceptance prayers (cross over to Eid; deliberate overlap)
    "تقبل الله", "تقبل الله منا ومنكم",
)

# Seasonal greetings — Eid, Ramadan, year-rollover, religious
# congratulations. "كل عام وأنت بخير" type phrases.
_SEASONAL_GREETING_PHRASES_RAW: Tuple[str, ...] = (
    "كل عام وانت بخير", "كل عام وانتم بخير",
    "كل سنه وانت بخير", "كل سنه وانتم بخير",
    "كل عام وانت", "كل سنه وانت",
    "عيد سعيد", "عيد مبارك", "عيدكم مبارك",
    "عيد فطر مبارك", "عيد اضحى مبارك", "عيد الفطر", "عيد الاضحى",
    "عساكم من عواده", "عسانا وعساكم من عواده",
    "تقبل الله طاعتكم", "تقبل الله صيامكم", "تقبل الله منا ومنكم",
    "رمضان كريم", "رمضان مبارك",
    "اعاده الله علينا وعليكم باليمن والبركات",
    "happy eid", "eid mubarak", "ramadan mubarak", "ramadan kareem",
)


def _normalise_phrases(raw: Tuple[str, ...]) -> FrozenSet[str]:
    return frozenset(p for p in (_normalise_arabic(s) for s in raw) if p)


RELIGIOUS_RITUAL_MARKERS: FrozenSet[str] = _normalise_phrases(
    _RELIGIOUS_RITUAL_PHRASES_RAW
)
SEASONAL_GREETING_MARKERS: FrozenSet[str] = _normalise_phrases(
    _SEASONAL_GREETING_PHRASES_RAW
)


def _text_matches_any(norm: str, markers: FrozenSet[str]) -> Optional[str]:
    """Return the first marker that appears in ``norm``, or ``None``.
    Pure substring containment — markers are pre-normalised so this
    is locale-safe."""
    if not norm or not markers:
        return None
    for m in markers:
        if m and m in norm:
            return m
    return None


def text_indicates_religious_ritual(inbound_text: Optional[str]) -> bool:
    """Public convenience for the classifier in :mod:`state`."""
    norm = _normalise_arabic(inbound_text or "")
    return _text_matches_any(norm, RELIGIOUS_RITUAL_MARKERS) is not None


def text_indicates_seasonal_greeting(inbound_text: Optional[str]) -> bool:
    """Public convenience for the classifier in :mod:`state`."""
    norm = _normalise_arabic(inbound_text or "")
    return _text_matches_any(norm, SEASONAL_GREETING_MARKERS) is not None


# ── Suppression eligibility tables ─────────────────────────────────
# Read by ``should_suppress_dedup_substitution``. Closed sets — adding
# a member is an architectural decision, not a quick fix.

# Moments that ALLOW suppression. The Brain reply on these turns is
# inherently relational; lexical overlap with the previous outbound
# is not a loop signal.
_SUPPRESSION_ELIGIBLE_MOMENTS: FrozenSet[ConversationMoment] = frozenset({
    ConversationMoment.SOCIAL_CHECK_IN,
    ConversationMoment.GRATITUDE_GENERIC,
    ConversationMoment.PRAISE_POST_DELIVERY,
    ConversationMoment.RELIGIOUS_RITUAL_EXCHANGE,
    ConversationMoment.SEASONAL_GREETING,
})

# Moments that BLOCK suppression even when the inbound matches a
# religious / seasonal marker. Loops on these turns are real loops
# and must surface to the dedup substitution / handoff paths.
_SUPPRESSION_BLOCKED_MOMENTS: FrozenSet[ConversationMoment] = frozenset({
    ConversationMoment.TRANSACTIONAL_ACTIVE,
    ConversationMoment.COMPLAINT_PRODUCT_QUALITY,
    ConversationMoment.COMPLAINT_SHIPPING_DELAY,
    ConversationMoment.COMPLAINT_GENERIC,
    ConversationMoment.RECOVERY_AFTER_FAILURE,
    ConversationMoment.ESCALATION_REQUEST,
})


# ── Decision dataclass ─────────────────────────────────────────────


@dataclass(frozen=True)
class DedupSuppressionDecision:
    """Verdict of :func:`should_suppress_dedup_substitution`.

    Fields
    ------
    suppress:
        ``True`` iff the call site MUST skip its dedup substitution
        and pass the Brain's reply through unchanged.
    reason:
        Stable short token explaining the decision. Used for log
        greppability (``reason=religious_ritual_exchange`` etc.).
    moment_token:
        The relational moment this decision was made under, as a
        string token (``""`` if unknown). Mirrors the value used
        elsewhere in ``[CX]`` log lines.
    matched_marker:
        Specific marker phrase that fired the text-backstop path,
        if any. ``""`` when the decision was made on the moment alone
        (or when no suppression fires).
    flag_enabled:
        Whether the kill switch was on at the time of evaluation.
        Helpful for forensic logs that want to distinguish "gate
        evaluated and decided no" from "gate was off".
    """

    suppress: bool
    reason: str
    moment_token: str = ""
    matched_marker: str = ""
    flag_enabled: bool = False


# Stable reason tokens (also used by the log helper).
REASON_FLAG_OFF = "flag_off"
REASON_MOMENT_ELIGIBLE = "moment_eligible"
REASON_MOMENT_BLOCKS = "moment_blocks_suppression"
REASON_RELIGIOUS_TEXT = "religious_marker_text"
REASON_SEASONAL_TEXT = "seasonal_marker_text"
REASON_NO_SIGNAL = "no_marker"
REASON_INTERNAL_ERROR = "internal_error"


# ── The pure gate function ─────────────────────────────────────────


def should_suppress_dedup_substitution(
    *,
    inbound_text: Optional[str],
    relational_moment: Optional[Any] = None,
    overlap: float = 0.0,
) -> DedupSuppressionDecision:
    """Pure, never-raising gate.

    Returns a :class:`DedupSuppressionDecision`. The call site reads
    only ``decision.suppress``; the rest is for telemetry.

    Parameters
    ----------
    inbound_text:
        The customer's current message body. ``None`` / empty is
        valid — the gate falls through to the moment branch only.
    relational_moment:
        A :class:`ConversationMoment`, its string value (``"praise_post_delivery"``),
        or ``None``. ``None`` / unknown values are treated as "no
        relational signal" and the gate falls back to the text-marker
        backstop.
    overlap:
        Dedup overlap score (0–1). Carried in the decision for
        telemetry only — does NOT influence the decision (the call
        site has already decided overlap is hard-tier when it consults
        us).
    """
    flag_enabled = is_relational_dedup_suppression_enabled()
    moment_token = _coerce_moment_token(relational_moment)

    # Kill switch off → inert.
    if not flag_enabled:
        return DedupSuppressionDecision(
            suppress=False,
            reason=REASON_FLAG_OFF,
            moment_token=moment_token,
            flag_enabled=False,
        )

    try:
        return _evaluate_unsafe(
            inbound_text=inbound_text,
            moment_token=moment_token,
            flag_enabled=flag_enabled,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[CX] dedup_suppression evaluator raised; falling back to "
            "legacy path: %s", exc,
        )
        return DedupSuppressionDecision(
            suppress=False,
            reason=REASON_INTERNAL_ERROR,
            moment_token=moment_token,
            flag_enabled=flag_enabled,
        )


def _coerce_moment_token(value: Any) -> str:
    """Accept a :class:`ConversationMoment`, its ``.value`` string,
    or arbitrary stringy input. Return the canonical token or ``""``."""
    if value is None:
        return ""
    if isinstance(value, ConversationMoment):
        return value.value
    raw = str(value or "").strip().lower()
    if not raw or raw == "none":
        return ""
    # Validate against the closed enum so a typo never leaks into
    # the decision. Unknown tokens collapse to "" (no signal).
    for m in ConversationMoment:
        if m.value == raw:
            return m.value
    return ""


def _evaluate_unsafe(
    *,
    inbound_text: Optional[str],
    moment_token: str,
    flag_enabled: bool,
) -> DedupSuppressionDecision:
    norm = _normalise_arabic(inbound_text or "")

    # 1) Block list — even if a marker fires, do NOT suppress.
    blocked_moment = _moment_in(moment_token, _SUPPRESSION_BLOCKED_MOMENTS)
    if blocked_moment is not None:
        return DedupSuppressionDecision(
            suppress=False,
            reason=REASON_MOMENT_BLOCKS,
            moment_token=moment_token,
            flag_enabled=flag_enabled,
        )

    # 2) Eligible-moment branch.
    if _moment_in(moment_token, _SUPPRESSION_ELIGIBLE_MOMENTS) is not None:
        return DedupSuppressionDecision(
            suppress=True,
            reason=REASON_MOMENT_ELIGIBLE,
            moment_token=moment_token,
            flag_enabled=flag_enabled,
        )

    # 3) Text-marker backstop. Only fires when no transactional /
    #    complaint moment was set above. Religious markers first
    #    (Eid season inbound is often both religious AND seasonal —
    #    we report whichever fires first deterministically).
    religious_hit = _text_matches_any(norm, RELIGIOUS_RITUAL_MARKERS)
    if religious_hit:
        return DedupSuppressionDecision(
            suppress=True,
            reason=REASON_RELIGIOUS_TEXT,
            moment_token=moment_token,
            matched_marker=religious_hit,
            flag_enabled=flag_enabled,
        )
    seasonal_hit = _text_matches_any(norm, SEASONAL_GREETING_MARKERS)
    if seasonal_hit:
        return DedupSuppressionDecision(
            suppress=True,
            reason=REASON_SEASONAL_TEXT,
            moment_token=moment_token,
            matched_marker=seasonal_hit,
            flag_enabled=flag_enabled,
        )

    # 4) Default — no signal, fall through to legacy substitution.
    return DedupSuppressionDecision(
        suppress=False,
        reason=REASON_NO_SIGNAL,
        moment_token=moment_token,
        flag_enabled=flag_enabled,
    )


def _moment_in(token: str, members: FrozenSet[ConversationMoment]) -> Optional[ConversationMoment]:
    if not token:
        return None
    for m in members:
        if m.value == token:
            return m
    return None


# ── Telemetry ──────────────────────────────────────────────────────


def log_dedup_suppression(
    *,
    decision: DedupSuppressionDecision,
    tenant_id: Any = None,
    conversation_id: Any = None,
    overlap: float = 0.0,
    would_have_replaced: bool = False,
) -> None:
    """Emit the canonical ``[CX] dedup_suppression`` log line.

    Always called by the wiring site, regardless of decision —
    operators want to grep both the suppress and legacy outcomes
    when measuring impact. The kill switch state is encoded in
    ``decision.flag_enabled``; the log line itself is unconditional
    (you can't audit what isn't logged).
    """
    try:
        decision_token = "suppress" if decision.suppress else "legacy"
        logger.info(
            "[CX] dedup_suppression decision=%s reason=%s moment=%s "
            "matched_marker=%r overlap=%.2f would_have_replaced=%s "
            "flag_enabled=%s tenant_id=%s conversation_id=%s",
            decision_token,
            decision.reason,
            decision.moment_token or "",
            decision.matched_marker or "",
            float(overlap or 0.0),
            str(bool(would_have_replaced)).lower(),
            str(bool(decision.flag_enabled)).lower(),
            tenant_id if tenant_id is not None else "",
            conversation_id if conversation_id is not None else "",
        )
    except Exception:
        # Never let telemetry sink the request.
        pass


__all__ = [
    "DedupSuppressionDecision",
    "RELIGIOUS_RITUAL_MARKERS",
    "SEASONAL_GREETING_MARKERS",
    "REASON_FLAG_OFF",
    "REASON_MOMENT_ELIGIBLE",
    "REASON_MOMENT_BLOCKS",
    "REASON_RELIGIOUS_TEXT",
    "REASON_SEASONAL_TEXT",
    "REASON_NO_SIGNAL",
    "REASON_INTERNAL_ERROR",
    "is_relational_dedup_suppression_enabled",
    "log_dedup_suppression",
    "should_suppress_dedup_substitution",
    "text_indicates_religious_ritual",
    "text_indicates_seasonal_greeting",
]
