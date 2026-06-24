"""
services/fallback_policy.py
───────────────────────────
"Choose an HONEST fallback reply when the LLM cannot answer."

Root motivation
───────────────
For most of 2025-2026 the WhatsApp webhook fell back to ONE canned
template whenever the Brain pipeline crashed or returned empty:

    "وصلت رسالتك ✅ سيتم الرد عليك في أقرب وقت من فريق المتجر."

This is a LIE for the majority of tenants — there is no human
"فريق المتجر" waiting to reply. The customer's question
("وشلون طريقة توصيل الطلبات عندكم?" — May 2026 production incident)
gets a false promise of escalation, the merchant never sees a
handoff alert, and the customer drops off.

This module replaces the single canned template with a small, honest
classifier:

  * **Informational** ("كيف / وش / متى / كم / وين / هل") →
    "حصل خلل مؤقت 🌷 ممكن تعيد سؤالك بتفاصيل أكثر، أو أعطيني
    كلمات مفتاحية أوضح؟"
    — no human promise, asks for a re-phrase, stays on-domain.

  * **Explicit handoff request** ("أبي محادثة مع موظف / إنسان /
    شخص") → keeps the original handoff template because here a
    handoff genuinely IS the next step.

  * **Generic / unknown** → a neutral retry prompt with no promise:
    "حصل خطأ مؤقت 🙏 ممكن تعيد رسالتك؟"

The classifier is intentionally simple — Arabic keyword matching
with a few normalisation steps. We don't call an LLM to classify
because (a) the LLM is the thing that just failed, (b) we don't
want to add latency to an error path, (c) a simple regex covers
> 90% of real Arabic informational questions seen in production.

Module is pure: no DB, no I/O, deterministic.

Contract for the webhook
────────────────────────
    text, kind, response_goal = choose_safe_fallback(
        inbound_text,
        reason=FALLBACK_REASON_BRAIN_EXCEPTION,
        store_has_live_agent=False,
    )

    * ``text``           — the reply to send (Arabic).
    * ``kind``           — opaque label for telemetry (one of
      ``FALLBACK_KIND_*``).
    * ``response_goal``  — what the system intended; copy into
      ``TurnTrace.response_goal``.

Why expose ``response_goal`` separately
────────────────────────────────────────
``response_goal`` is what we MEANT to do, ``fallback_kind`` is HOW
we phrased it. A merchant dashboard can plot the ratio of
goal=answer/goal=handoff/goal=retry over time to see whether the
Brain is getting better.
"""
from __future__ import annotations

import logging
import os
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple


# ─────────────────────────────────────────────────────────────────────────────
# Public reason / kind / goal vocabularies — keep small and pinned
# ─────────────────────────────────────────────────────────────────────────────

# Reasons callers pass IN to ``choose_safe_fallback``:
FALLBACK_REASON_BRAIN_EXCEPTION = "brain_exception"      # Brain raised
FALLBACK_REASON_BRAIN_SILENT    = "brain_silent"         # Brain returned empty
FALLBACK_REASON_NO_API_KEY      = "no_api_key"           # ANTHROPIC_API_KEY unset
FALLBACK_REASON_OUTER_EXCEPTION = "outer_exception"      # outer try/except

# Kinds returned by the policy (telemetry label):
FALLBACK_KIND_SOFT_RETRY        = "soft_retry"           # informational + retry
FALLBACK_KIND_HANDOFF_ACK       = "handoff_ack"          # explicit handoff request
FALLBACK_KIND_NEUTRAL_RETRY     = "neutral_retry"        # generic retry
FALLBACK_KIND_NO_AI             = "no_ai"                # AI not configured

# Response-goal vocabulary (copied into TurnTrace.response_goal):
GOAL_ANSWER         = "answer"
GOAL_RETRY          = "retry"
GOAL_HANDOFF        = "handoff"
GOAL_ACK            = "ack"
GOAL_SILENT         = "silent"


# ─────────────────────────────────────────────────────────────────────────────
# Arabic text normalisation — strip diacritics + collapse alef variants
# ─────────────────────────────────────────────────────────────────────────────
#
# Customers type Arabic in many shapes: with/without tashkeel, with
# different alef/hamza variants, with Latin Anglicisms ("kif",
# "shlon"). We want all of these to hit the informational classifier
# reliably. The normaliser here mirrors the one in
# ``media_key_registry`` so the two stay consistent.

_DIACRITICS_RX = re.compile(r"[\u064B-\u065F\u0670\u0640]")


def _normalise(text: str) -> str:
    if not text:
        return ""
    s = unicodedata.normalize("NFKD", text)
    s = _DIACRITICS_RX.sub("", s)
    s = s.replace("\u0623", "\u0627").replace("\u0625", "\u0627")  # أ / إ → ا
    s = s.replace("\u0622", "\u0627")                                # آ    → ا
    s = s.replace("\u0629", "\u0647")                                # ة    → ه
    s = s.replace("\u0649", "\u064A")                                # ى    → ي
    return s.strip().lower()


# ─────────────────────────────────────────────────────────────────────────────
# Classifier — informational vs handoff vs other
# ─────────────────────────────────────────────────────────────────────────────
#
# Patterns are matched on the NORMALISED text. We deliberately keep
# them short and word-boundary aware so a substring like "كيف" inside
# the middle of an unrelated word doesn't fire (e.g. "تكييف").

# Informational interrogatives that signal "give me a fact".
#
# IMPORTANT: patterns are matched against ``_normalise(text)`` which
# rewrites ى → ي (the standard ortho-normalisation we do everywhere
# else in the codebase). So "متى" in real customer text becomes
# "متي" before matching — every pattern below uses the NORMALISED
# spelling. Keep this comment in sync if the normaliser changes.
_INFORMATIONAL_RX = re.compile(
    r"(?:^|\s)("
    r"كيف|وشلون|شلون|كم|وين|اين|متي|"        # "متي" = normalised "متى"
    r"ايش|اش|وش|"
    r"هل|"
    r"ما هي|ماهي|ما هو|ماهو|ما رايك|"        # "رايك" = normalised "رأيك"
    r"اخبرني|اعطيني|اعرف|"
    r"shlon|kif|wesh|kam|wain"
    r")(?:\s|\?|؟|$)",
    re.UNICODE,
)

# Explicit human-handoff requests — customer EXPLICITLY wants a person.
# Patterns match against the NORMALISED text.
#
# Two tiers:
#   1. STRONG nouns ("موظف" / "انسان" / "بشر" / "موظفه") on a word
#      boundary — extremely high signal in customer Arabic that
#      they want a person. False-positive risk is low because these
#      words rarely appear in shopping conversations (a customer
#      doesn't normally say "I am an employee" mid-checkout).
#   2. Composite verb+noun patterns for English / less-common
#      phrasings ("اتصل بي", "احتاج اكلم", "talk to agent").
_HANDOFF_RX = re.compile(
    r"(?:"
    # Tier 1 — strong human-noun on its own word boundary.
    # Allow an optional Arabic prefix letter (ل / ب / ك / ف) between
    # the boundary and the noun so "حولني لانسان" / "بشخص" / "كموظف"
    # all match. Regex backtracks when the optional prefix would
    # eat the first letter of the noun itself ("بشر").
    r"(?:^|\s)[لبكف]?(?:موظف|موظفه|انسان|بشر|بشري)(?:\s|$|[\.,!\?؟])|"
    # Tier 2 — composite phrasings.
    r"احتاج.{0,7}اكلم|"
    r"اتصل.{0,7}بي|"
    r"تكلم.{0,7}شخص|"
    r"كلم.{0,7}شخص|"
    r"اكلم.{0,7}شخص|"
    # English variants (the tenant's customer base is mostly Arabic,
    # but rare English-speakers should still be detected).
    r"\bhuman\b|\bagent\b|\brepresentative\b|"
    r"talk to (?:an? )?(?:human|agent|person|representative)|"
    r"want (?:to talk to )?(?:a )?human"
    r")",
    re.UNICODE | re.IGNORECASE,
)


def is_informational_question(text: str) -> bool:
    """True iff *text* looks like a direct, factual question.

    Examples that match:
      * "وشلون طريقة التوصيل عندكم؟"
      * "كم سعر العسل؟"
      * "متى يصل الطلب؟"
      * "shlon al-tawseel"

    Examples that DON'T:
      * "السلام عليكم"           — greeting, not a question
      * "ممتاز شكرًا لك"          — closer
      * "أبي أتكلم مع موظف"      — handoff request, not informational
    """
    if not text:
        return False
    norm = _normalise(text)
    if not norm:
        return False
    # An explicit handoff request takes precedence — even if it
    # contains "كيف" as a politeness particle.
    if _HANDOFF_RX.search(norm):
        return False
    return bool(_INFORMATIONAL_RX.search(norm))


def is_explicit_handoff_request(text: str) -> bool:
    """True iff the customer explicitly asked to talk to a human."""
    if not text:
        return False
    return bool(_HANDOFF_RX.search(_normalise(text)))


# ─────────────────────────────────────────────────────────────────────────────
# Honest fallback texts — pinned strings + rationale next to each
# ─────────────────────────────────────────────────────────────────────────────

# Stays close to the original handoff template — only used when the
# customer ACTUALLY asked for a human.
_TEXT_HANDOFF_ACK = (
    "وصلت رسالتك ✅ سيتم الرد عليك في أقرب وقت من فريق المتجر."
)

# Informational + LLM failed. We tell the truth: there was a glitch,
# and we ask the customer to re-send. No false promise of human
# escalation. Emoji is intentional (matches the rest of the tenant
# voice).
#
# May 2026 #18 — production regression rollback. The previous wording
# was:
#
#     "حصل خلل تقني بسيط 🌷 ممكن تعيد سؤالك بتفاصيل أكثر؟ "
#     "(عن المنتج / السعر / التوصيل / الدفع)"
#
# That copy fired whenever Brain raised on an informational ask
# (anything starting with وش / كيف / كم / هل / متى / وين etc.). Two
# problems showed up in production:
#
#   1. The "بتفاصيل أكثر" wording IMPLIES the customer was vague.
#      For clear asks like "وش عندكم عسل" / "وشلون طريقة التوصيل"
#      / "كم مدة التوصيل" the customer's intent is already obvious
#      — telling them to "give more detail" is wrong-footed and
#      makes the AI feel dumber than it actually is.
#
#   2. The four-topic parenthetical "(عن المنتج / السعر / التوصيل
#      / الدفع)" offers a fixed menu that doesn't even match what
#      the customer asked about. The merchant's rule was:
#
#         "لا يجوز أن يسقط الرد إلى 'وش تقصد؟' إلا إذا كانت
#          الرسالة الحالية نفسها غامضة."
#
#      A clear shipping ask shouldn't get a "pick a topic" menu —
#      it should get a shipping answer (the intent-aware path) or
#      a friendly retry that doesn't insinuate vagueness.
#
# This wording strips both pathologies. It stays honest about the
# glitch but does NOT accuse the customer of being unclear and does
# NOT push a topic menu in their face. The kind label
# (``FALLBACK_KIND_SOFT_RETRY``) is preserved for telemetry so we
# can still tell informational-ask retries apart from generic
# retries in the [TURN] log.
_TEXT_SOFT_RETRY = (
    "حصل خلل تقني بسيط 🌷 ممكن تعيد رسالتك؟"
)

# Generic — when the question is neither clearly informational nor a
# handoff. Stays neutral, no promise, asks for a re-send.
_TEXT_NEUTRAL_RETRY = (
    "حصل خطأ مؤقت 🙏 ممكن تعيد رسالتك؟"
)

# No AI configured at all (legacy path / missing API key). Honest
# about the situation without panicking the customer.
_TEXT_NO_AI = (
    "وصلت رسالتك 🌷 فريق المتجر راح يتواصل معك قريبًا."
)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class FallbackDecision:
    text:          str
    kind:          str        # one of FALLBACK_KIND_*
    response_goal: str        # one of GOAL_*
    rationale:     str = ""   # human-readable diagnostic (logged only)
    metadata:      Dict[str, Any] = field(default_factory=dict)


def handoff_promise_allowed(
    *,
    store_has_live_agent: bool = False,
    escalation_evidence_ok: bool = False,
) -> bool:
    """Human handoff promises require live agent or structured escalation evidence."""
    return bool(store_has_live_agent or escalation_evidence_ok)


def _assert_no_handoff_promise_without_evidence(
    text: str,
    *,
    store_has_live_agent: bool,
    escalation_evidence_ok: bool,
) -> tuple[str, dict]:
    """Return safe text + metadata when a template would over-promise."""
    meta: dict = {}
    if handoff_promise_allowed(
        store_has_live_agent=store_has_live_agent,
        escalation_evidence_ok=escalation_evidence_ok,
    ):
        return text, meta
    try:
        from core.outbound_sanitizer import contains_handoff_promise  # noqa: PLC0415

        if contains_handoff_promise(text):
            meta = {
                "handoff_promise_blocked": True,
                "escalation_evidence_ok": False,
                "fallback_text_sanitized": True,
            }
            return _TEXT_NEUTRAL_RETRY, meta
    except Exception:  # noqa: BLE001
        pass
    return text, meta


def choose_safe_fallback(
    inbound_text: str,
    *,
    reason: str,
    store_has_live_agent: bool = False,
    escalation_evidence_ok: bool = False,
) -> FallbackDecision:
    """Choose a fallback reply that is honest about the situation.

    Parameters
    ----------
    inbound_text:
        The customer's actual message — drives the classifier.
    reason:
        Why we're falling back (one of ``FALLBACK_REASON_*``). Used
        ONLY to disambiguate the "no AI" path from the "AI tried and
        failed" paths. Future reasons (timeout, rate-limit) can be
        added without changing the call sites.
    store_has_live_agent:
        When True the merchant has a human team actively monitoring
        the inbox. We can honestly promise a human reply for explicit
        handoff requests in that case. Default False — most tenants
        are AI-only, and promising a human we don't have is the
        original bug. Wired in from tenant settings; the webhook
        passes it in.
    escalation_evidence_ok:
        When True structured escalation evidence exists (handoff
        session, notification, pre-brain handoff path). Human promises
        are allowed only with this flag or ``store_has_live_agent``.

    Returns
    -------
    FallbackDecision — text + telemetry labels.
    """

    # ── Special-case: no API key / AI fully disabled ─────────────
    if reason == FALLBACK_REASON_NO_API_KEY:
        safe_text, meta = _assert_no_handoff_promise_without_evidence(
            _TEXT_NO_AI,
            store_has_live_agent=store_has_live_agent,
            escalation_evidence_ok=escalation_evidence_ok,
        )
        return FallbackDecision(
            text          = safe_text,
            kind          = FALLBACK_KIND_NO_AI,
            response_goal = GOAL_ACK if safe_text == _TEXT_NO_AI else GOAL_RETRY,
            rationale     = (
                "AI disabled — informing customer the team will reach out"
                if safe_text == _TEXT_NO_AI
                else "AI disabled — human promise blocked without escalation evidence"
            ),
            metadata      = meta,
        )

    # ── Explicit handoff request → honest handoff ack ────────────
    if is_explicit_handoff_request(inbound_text):
        if store_has_live_agent:
            return FallbackDecision(
                text          = _TEXT_HANDOFF_ACK,
                kind          = FALLBACK_KIND_HANDOFF_ACK,
                response_goal = GOAL_HANDOFF,
                rationale     = "customer asked for human + tenant has live agent",
                metadata      = {"escalation_evidence_ok": True, "handoff_requested": True},
            )
        if escalation_evidence_ok:
            softened = "وصلت رسالتك 🌷 راح نتواصل معك في أقرب وقت ممكن."
            safe_text, meta = _assert_no_handoff_promise_without_evidence(
                softened,
                store_has_live_agent=False,
                escalation_evidence_ok=True,
            )
            return FallbackDecision(
                text          = safe_text,
                kind          = FALLBACK_KIND_HANDOFF_ACK,
                response_goal = GOAL_HANDOFF,
                rationale     = "customer asked for human + escalation evidence present",
                metadata      = {**meta, "handoff_requested": True, "escalation_evidence_ok": True},
            )
        safe_text, meta = _assert_no_handoff_promise_without_evidence(
            "وصلت رسالتك 🌷 راح نتواصل معك في أقرب وقت ممكن.",
            store_has_live_agent=False,
            escalation_evidence_ok=False,
        )
        return FallbackDecision(
            text          = safe_text,
            kind          = FALLBACK_KIND_HANDOFF_ACK,
            response_goal = GOAL_RETRY,
            rationale     = (
                "customer asked for human but no escalation evidence → "
                "neutral retry, no human promise"
            ),
            metadata      = {
                **meta,
                "handoff_requested": True,
                "handoff_promise_blocked": True,
                "escalation_evidence_ok": False,
            },
        )

    # ── Informational question + LLM failure → soft retry ────────
    if is_informational_question(inbound_text):
        return FallbackDecision(
            text          = _TEXT_SOFT_RETRY,
            kind          = FALLBACK_KIND_SOFT_RETRY,
            response_goal = GOAL_RETRY,
            rationale     = "informational ask + LLM unavailable → ask to rephrase, no false handoff",
        )

    # ── Everything else → neutral retry ──────────────────────────
    return FallbackDecision(
        text          = _TEXT_NEUTRAL_RETRY,
        kind          = FALLBACK_KIND_NEUTRAL_RETRY,
        response_goal = GOAL_RETRY,
        rationale     = "unclassified turn + LLM unavailable → neutral retry",
    )


# Backwards-compat convenience for callers that only want the text:
def fallback_text(
    inbound_text: str,
    *,
    reason: str,
    store_has_live_agent: bool = False,
) -> Tuple[str, str]:
    d = choose_safe_fallback(
        inbound_text,
        reason=reason,
        store_has_live_agent=store_has_live_agent,
    )
    return d.text, d.kind


# ─────────────────────────────────────────────────────────────────────────────
# Intent-aware fallback — "current-turn dominance" (May 2026 #17)
# ─────────────────────────────────────────────────────────────────────────────
#
# Pure ``choose_safe_fallback`` produces a *generic* retry ("ممكن تعيد
# سؤالك بتفاصيل أكثر؟") which is the right call when we have NO idea
# what the customer asked. But when the rule classifier IS confident
# about the current turn — e.g. it matches ``INTENT_ASK_SHIPPING`` with
# confidence 0.90 — falling back to a "what do you mean?" reply is
# strictly worse than answering directly from store knowledge.
#
# This module exposes a higher-level wrapper, ``choose_intent_aware_
# fallback``, that:
#
#   1. Looks at the rule classifier's top-k candidates for the current
#      turn.
#   2. If the winning intent is one we have a DETERMINISTIC handler
#      for (currently: ``INTENT_ASK_SHIPPING``) AND confidence is at
#      or above the rules-only threshold, renders a real answer from
#      the supplied knowledge dict.
#   3. Otherwise delegates to plain ``choose_safe_fallback``.
#
# The behavioural rule the merchant stated:
#
#     "إذا كان current turn confidence أعلى من threshold،
#      فامنع clarification fallback حتى لو كانت المحادثة السابقة noisy."
#
# is encoded as the ``min_confidence`` argument (default 0.85 — matches
# the rules-only short-circuit threshold in ``intent.classifier``).
#
# We deliberately do NOT depend on the full Brain pipeline here: the
# whole point is to produce a sensible answer EVEN when Brain crashed
# or returned empty. So we re-use only the lightweight ``rules.match_
# top_k`` regex layer, plus the existing deterministic template
# ``modules.ai.brain.compose.templates.faq_shipping``.

# Confidence below this means "the regex isn't sure" → defer to the
# standard retry copy. Pinned to match
# ``modules.ai.brain.intent.classifier.RULES_ONLY_THRESHOLD``.
INTENT_AWARE_MIN_CONFIDENCE = 0.85

FALLBACK_KIND_INTENT_DETERMINISTIC = "intent_deterministic"


def _build_shipping_answer(shipping_info: dict) -> str:
    """Render a real shipping answer from store knowledge.

    ``shipping_info`` is the dict the webhook builds from
    ``core.store_knowledge.build_merchant_context``. We accept a
    plain dict (not a typed snapshot) so the function stays usable
    from any caller — tests can pass a minimal dict with just
    ``shipping_policy`` and we still produce a reasonable answer.

    When the merchant has NOT configured any shipping info, we
    return an honest line that asks ONE concrete question
    ("لأي مدينة تبغى التوصيل؟") instead of the generic four-topic
    clarification — exactly the behaviour the merchant asked for.
    """
    methods = shipping_info.get("shipping_methods") or shipping_info.get("methods") or []
    notes   = (shipping_info.get("shipping_notes") or shipping_info.get("notes") or "").strip()
    policy  = (shipping_info.get("shipping_policy") or shipping_info.get("policy") or "").strip()
    areas   = shipping_info.get("delivery_areas") or shipping_info.get("areas") or []
    hours   = (shipping_info.get("support_hours") or shipping_info.get("hours") or "").strip()

    have_anything = bool(methods or notes or policy or areas)

    if not have_anything:
        # No configured shipping knowledge → ask one focused question
        # rather than the four-topic generic retry. This is the
        # "current-turn dominance" behaviour the merchant required:
        # the customer already picked the topic (delivery), so don't
        # offer them the topic menu again.
        return (
            "بالنسبة للتوصيل 🌷 لأي مدينة تبغى التوصيل؟ "
            "أعطني المدينة (الرياض / جدة / الدمام / ...) ورح أتحقق لك "
            "من الخيارات المتاحة."
        )

    lines: list[str] = ["بالنسبة للتوصيل 🌷"]
    if policy:
        lines.append(f"- سياسة الشحن: {policy}")
    if methods:
        if isinstance(methods, (list, tuple)):
            lines.append(f"- طرق الشحن: {', '.join(str(m) for m in methods if m)}")
        else:
            lines.append(f"- طرق الشحن: {methods}")
    if notes:
        lines.append(f"- ملاحظات: {notes}")
    if areas:
        if isinstance(areas, (list, tuple)):
            joined = ", ".join(str(a) for a in areas if a)
            if joined:
                lines.append(f"- مناطق التوصيل: {joined}")
        else:
            lines.append(f"- مناطق التوصيل: {areas}")
    if hours:
        lines.append(f"- ساعات الدعم: {hours}")
    lines.append("لو حابب أكمل الطلب أعطني المنتج والمدينة.")
    return "\n".join(lines)


def choose_intent_aware_fallback(
    inbound_text: str,
    *,
    reason: str,
    store_has_live_agent: bool = False,
    escalation_evidence_ok: bool = False,
    shipping_info: dict | None = None,
    min_confidence: float = INTENT_AWARE_MIN_CONFIDENCE,
) -> FallbackDecision:
    """Choose a fallback respecting the current-turn intent.

    Behavioural rule (May 2026 #17):

      * IF the rule classifier matches an intent we have a
        deterministic responder for AND the confidence is at or
        above ``min_confidence`` → return that deterministic answer.
        This SUPPRESSES the generic "ممكن تعيد سؤالك بتفاصيل أكثر؟
        (عن المنتج / السعر / التوصيل / الدفع)" retry that lies to
        the customer about what we understood.

      * OTHERWISE → delegate to :func:`choose_safe_fallback`.

    Parameters
    ----------
    inbound_text:
        Customer's message — feeds the rule classifier.
    reason:
        One of ``FALLBACK_REASON_*``. Passed through to
        ``choose_safe_fallback`` when we delegate.
    store_has_live_agent:
        Forwarded to ``choose_safe_fallback`` for the handoff-ack
        path. Has no effect on the deterministic-intent path.
    shipping_info:
        Optional dict the webhook builds from store knowledge. When
        the deterministic shipping path fires, this dict is what we
        render. Pass ``None`` if you don't have shipping info — the
        responder will emit an honest "which city?" question.
    min_confidence:
        Threshold for "trust the regex enough to skip the generic
        retry". Default mirrors the rules-only short-circuit
        threshold.

    Returns
    -------
    FallbackDecision identical in shape to :func:`choose_safe_fallback`,
    so the webhook can swap the call without changing downstream
    logging / telemetry.
    """
    # Import lazily — keeps this module importable from environments
    # that don't have the Brain package available (e.g. small test
    # harnesses).
    try:
        from modules.ai.brain.intent import rules as _rules  # noqa: PLC0415
        from modules.ai.brain.types import INTENT_ASK_SHIPPING  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        # Brain package missing → fall back to plain policy.
        return choose_safe_fallback(
            inbound_text, reason=reason,
            store_has_live_agent=store_has_live_agent,
            escalation_evidence_ok=escalation_evidence_ok,
        )

    # Always defer the no-API-key path to the standard policy — no
    # deterministic intent answer makes sense when the AI is fully
    # disabled.
    if reason == FALLBACK_REASON_NO_API_KEY:
        return choose_safe_fallback(
            inbound_text, reason=reason,
            store_has_live_agent=store_has_live_agent,
            escalation_evidence_ok=escalation_evidence_ok,
        )

    try:
        from modules.ai.brain.commerce.product_ordering_prompt import (  # noqa: PLC0415
            build_short_honey_order_clarify_reply,
            is_short_honey_order_request,
        )

        if is_short_honey_order_request(inbound_text or ""):
            return FallbackDecision(
                text=build_short_honey_order_clarify_reply(inbound_text or ""),
                kind=FALLBACK_KIND_INTENT_DETERMINISTIC,
                response_goal=GOAL_ANSWER,
                rationale=(
                    "short_honey_order_request → deterministic order clarify "
                    "instead of neutral_retry"
                ),
            )
    except Exception:  # noqa: BLE001
        pass

    try:
        candidates = _rules.match_top_k(inbound_text or "", k=3)
    except Exception:  # noqa: BLE001
        candidates = []

    if candidates:
        best_conf, best_intent = candidates[0]
        # Current-turn dominance gate: high-confidence intent BLOCKS
        # the generic clarification retry even when prior context
        # was noisy. The rules layer is agnostic to history — that's
        # the property we want here.
        if best_conf >= min_confidence and best_intent.name == INTENT_ASK_SHIPPING:
            text = _build_shipping_answer(shipping_info or {})
            return FallbackDecision(
                text          = text,
                kind          = FALLBACK_KIND_INTENT_DETERMINISTIC,
                response_goal = GOAL_ANSWER,
                rationale     = (
                    f"intent={best_intent.name} conf={best_conf:.2f} "
                    f"≥ min={min_confidence:.2f} → deterministic shipping "
                    f"answer instead of soft_retry"
                ),
            )

    # No confident deterministic intent → standard policy decision.
    return choose_safe_fallback(
        inbound_text, reason=reason,
        store_has_live_agent=store_has_live_agent,
        escalation_evidence_ok=escalation_evidence_ok,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Structured fallback telemetry (May 2026 #42)
# ─────────────────────────────────────────────────────────────────────────────
#
# When a customer sees "حصل خطأ مؤقت 🙏 ممكن تعيد رسالتك؟" we MUST be able
# to answer in one log search:
#
#   * which tenant / conversation / sender hit the fallback?
#   * which inbound message id / type triggered it?
#   * which classifier intent was active?
#   * which orchestration STAGE produced the failure (brain pipeline?
#     outer try/except? media normaliser? pre-brain handoff send?)?
#   * which exception class + message was raised?
#   * which build (git sha) was running?
#
# Without this every fallback is a black box — we get a customer
# complaint with a timestamp and have to grep generic ``[Merchant/Brain]``
# error lines and pray they correlate. The merchant explicitly asked
# for a single greppable marker covering all of the above.
#
# Pure logging — never raises, never blocks the reply path.

# Stage vocabulary — pinned strings so log search is deterministic.
STAGE_BRAIN_EXCEPTION       = "brain_exception"
STAGE_OUTER_EXCEPTION       = "outer_exception"
STAGE_NORMALIZER_EXCEPTION  = "normalizer_exception"
STAGE_PRE_BRAIN_HANDOFF     = "pre_brain_handoff_send"
STAGE_MEDIA_FALLBACK        = "media_fallback"
STAGE_NO_AI                 = "no_ai_configured"

_FALLBACK_LOGGER = logging.getLogger("nahla.fallback")


def _resolve_git_sha() -> str:
    """Best-effort build-sha resolver.

    Honours the same env-var precedence as ``routers.health._read_git_head_sha``
    so the [AI_TEMP_ERROR_FALLBACK] line agrees with ``GET /version``.
    Returns ``"unknown"`` when no signal is available — never raises.
    """
    sha = (
        os.getenv("RAILWAY_GIT_COMMIT_SHA")
        or os.getenv("GIT_SHA")
        or os.getenv("SOURCE_COMMIT")
        or os.getenv("COMMIT_SHA")
        or ""
    )
    sha = sha.strip()
    if sha:
        return sha[:12]
    return "unknown"


def _safe(value: Any, default: str = "-") -> str:
    """Render ``value`` as a short, single-line string for log output.

    Strips newlines / tabs and clips to 200 chars so a noisy exception
    message can't blow up a single-line greppable log entry.
    """
    if value is None or value == "":
        return default
    s = str(value).replace("\n", " ").replace("\r", " ").replace("\t", " ")
    return s[:200] if len(s) > 200 else s


def emit_temp_error_fallback_log(
    *,
    tenant_id: Any,
    conversation_id: Any = None,
    sender: str = "",
    inbound_msg_id: str = "",
    msg_type: str = "",
    intent: str = "",
    stage: str = "",
    exception: Optional[BaseException] = None,
    exception_class: str = "",
    error_message: str = "",
    fallback_kind: str = "",
    response_goal: str = "",
    extra: Optional[dict] = None,
) -> None:
    """Emit the ``[AI_TEMP_ERROR_FALLBACK]`` structured log line.

    Wired in at every site that may send the generic temporary-error
    fallback to the customer:
      * Brain pipeline raised → safe-reply send.
      * Outer try/except → final fallback send.
      * Media normaliser raised → persist-only placeholder.
      * Pre-brain handoff guard ack-send raised.

    Never raises; logging failures are swallowed.

    Format
    ------
    ``[AI_TEMP_ERROR_FALLBACK] tenant_id=... conversation_id=...
    sender=... inbound_msg_id=... msg_type=... intent=... stage=...
    exception_class=... error_message=... fallback_kind=...
    response_goal=... git_sha=...``
    """
    try:
        if exception is not None and not exception_class:
            exception_class = exception.__class__.__name__
        if exception is not None and not error_message:
            error_message = str(exception)

        masked_sender = sender or ""
        if masked_sender and len(masked_sender) > 6:
            masked_sender = masked_sender[:3] + "***" + masked_sender[-3:]

        parts = [
            f"tenant_id={_safe(tenant_id)}",
            f"conversation_id={_safe(conversation_id)}",
            f"sender={_safe(masked_sender)}",
            f"inbound_msg_id={_safe(inbound_msg_id)}",
            f"msg_type={_safe(msg_type)}",
            f"intent={_safe(intent)}",
            f"stage={_safe(stage)}",
            f"exception_class={_safe(exception_class)}",
            f"error_message={_safe(error_message)}",
            f"fallback_kind={_safe(fallback_kind)}",
            f"response_goal={_safe(response_goal)}",
            f"git_sha={_safe(_resolve_git_sha())}",
        ]
        if extra:
            for k, v in extra.items():
                parts.append(f"{_safe(k)}={_safe(v)}")
        _FALLBACK_LOGGER.warning("[AI_TEMP_ERROR_FALLBACK] %s", " ".join(parts))
    except Exception:  # noqa: BLE001 — observability MUST NOT crash the reply path
        try:
            _FALLBACK_LOGGER.debug(
                "[AI_TEMP_ERROR_FALLBACK] emit failed (suppressed)",
                exc_info=True,
            )
        except Exception:  # noqa: BLE001  # noqa: silent-ok — fallback emit must not crash reply path
            pass


__all__ = [
    "FALLBACK_KIND_HANDOFF_ACK",
    "FALLBACK_KIND_INTENT_DETERMINISTIC",
    "FALLBACK_KIND_NEUTRAL_RETRY",
    "FALLBACK_KIND_NO_AI",
    "FALLBACK_KIND_SOFT_RETRY",
    "FALLBACK_REASON_BRAIN_EXCEPTION",
    "FALLBACK_REASON_BRAIN_SILENT",
    "FALLBACK_REASON_NO_API_KEY",
    "FALLBACK_REASON_OUTER_EXCEPTION",
    "FallbackDecision",
    "GOAL_ACK",
    "GOAL_ANSWER",
    "GOAL_HANDOFF",
    "GOAL_RETRY",
    "GOAL_SILENT",
    "INTENT_AWARE_MIN_CONFIDENCE",
    "STAGE_BRAIN_EXCEPTION",
    "STAGE_MEDIA_FALLBACK",
    "STAGE_NO_AI",
    "STAGE_NORMALIZER_EXCEPTION",
    "STAGE_OUTER_EXCEPTION",
    "STAGE_PRE_BRAIN_HANDOFF",
    "choose_intent_aware_fallback",
    "choose_safe_fallback",
    "emit_temp_error_fallback_log",
    "fallback_text",
    "handoff_promise_allowed",
    "is_explicit_handoff_request",
    "is_informational_question",
]
