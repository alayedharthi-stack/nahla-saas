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

import re
import unicodedata
from dataclasses import dataclass
from typing import Tuple


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
# and we ask the customer to re-phrase. No false promise of human
# escalation. Emoji is intentional (matches the rest of the tenant
# voice).
_TEXT_SOFT_RETRY = (
    "حصل خلل تقني بسيط 🌷 ممكن تعيد سؤالك بتفاصيل أكثر؟ "
    "(عن المنتج / السعر / التوصيل / الدفع)"
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


def choose_safe_fallback(
    inbound_text: str,
    *,
    reason: str,
    store_has_live_agent: bool = False,
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

    Returns
    -------
    FallbackDecision — text + telemetry labels.
    """

    # ── Special-case: no API key / AI fully disabled ─────────────
    if reason == FALLBACK_REASON_NO_API_KEY:
        return FallbackDecision(
            text          = _TEXT_NO_AI,
            kind          = FALLBACK_KIND_NO_AI,
            response_goal = GOAL_ACK,
            rationale     = "AI disabled — informing customer the team will reach out",
        )

    # ── Explicit handoff request → honest handoff ack ────────────
    if is_explicit_handoff_request(inbound_text):
        if store_has_live_agent:
            return FallbackDecision(
                text          = _TEXT_HANDOFF_ACK,
                kind          = FALLBACK_KIND_HANDOFF_ACK,
                response_goal = GOAL_HANDOFF,
                rationale     = "customer asked for human + tenant has live agent",
            )
        # Customer asked for a human but the tenant has no live
        # team. Still acknowledge — but soften the wording so we
        # don't promise an immediate human reply.
        return FallbackDecision(
            text          = "وصلت رسالتك 🌷 راح نتواصل معك في أقرب وقت ممكن.",
            kind          = FALLBACK_KIND_HANDOFF_ACK,
            response_goal = GOAL_HANDOFF,
            rationale     = "customer asked for human, tenant has no live agent → softened",
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


__all__ = [
    "FALLBACK_KIND_HANDOFF_ACK",
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
    "choose_safe_fallback",
    "fallback_text",
    "is_explicit_handoff_request",
    "is_informational_question",
]
