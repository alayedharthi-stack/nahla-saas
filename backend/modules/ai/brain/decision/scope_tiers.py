"""
brain/decision/scope_tiers.py
─────────────────────────────
Three-tier classifier for out-of-scope customer questions.

Motivation
──────────
A May 2026 incident leaked DuckDuckGo result dumps into customer
threads after the AI mishandled "ايهما حساب كهرباء الشقة". The first
fix gated external research behind an env switch and routed every
off-domain question to a single canned "هذا خارج نطاق متجرنا" reply.

That made the AI safe but lifeless. The merchant pushed back: Nahla
should still feel HUMAN. A customer asking "وش أخبارك؟" deserves a
short playful reply, not a corporate deflection. Likewise a casual
"كم الساعة؟" should land a joke about coffee + honey, not a refusal.

The tradeoff: keep the no-hallucination / no-web-search guarantee,
but allow a small set of safe, deterministic playful replies for
clearly benign questions.

Three tiers
───────────
* ``TIER_CHITCHAT``  — Casual conversational filler that ANYONE can
                       answer without doing research: greetings (that
                       slipped past INTENT_GREETING), how-are-you,
                       weather small talk, time, mood, polite jokes.
                       Reply: deterministic playful template.
                       Risk: zero (no specific facts asserted).

* ``TIER_SAFE_FACT`` — Simple, public, well-known factoid that does
                       not change frequently and is not politically
                       / medically / legally sensitive. Reply: gated
                       LLM call with a TIGHT prompt that mandates one
                       playful Arabic sentence + honey tie-in + zero
                       URLs / citations. If the LLM is uncertain it
                       MUST joke about not knowing instead of guessing.
                       Risk: bounded (outbound sanitizer scrubs any
                       URL leak; sensitive-topic keywords are
                       already excluded by this classifier).

* ``TIER_HARD``      — Sensitive (medical, legal, financial, deep
                       political), genuinely requires research, or
                       just clearly off-Nahla-scope (electricity
                       bills, apartment construction). Reply:
                       deterministic polite apology that explicitly
                       redirects to honey/orders.
                       Risk: zero.

Public contract
───────────────
* ``classify_out_of_scope_tier(message: str) -> str``
    Returns one of the three TIER_* constants. Always safe — falls
    back to ``TIER_HARD`` on unknown input (conservative default).

Banned reply phrases (forbidden by merchant feedback May 2026):
    - "هذا خارج نطاق متجرنا"
    - "أنا هنا — قول وش تحتاج"
    - "وأكمل معك"
These come from the OLD out-of-scope and dedup fallbacks and must
never appear in any new template added here.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from typing import Iterable

logger = logging.getLogger("nahla.brain.scope_tiers")

# Tier constants — surfaced as ``Decision.args["tier"]`` so the
# responder can pick the right template path without re-classifying.
TIER_CHITCHAT  = "chitchat"
TIER_SAFE_FACT = "safe_fact"
TIER_HARD      = "hard"


# ── Arabic normaliser ────────────────────────────────────────────────────────
# Collapses orthographic variants so patterns match regardless of how
# the customer typed it: ``أ`` / ``إ`` / ``آ`` → ``ا``, ``ى`` → ``ي``,
# ``ة`` → ``ه``. Diacritics stripped. ZWJ/ZWNJ stripped.
_ARABIC_DIACRITICS_RE = re.compile(r"[\u064B-\u065F\u0670\u06D6-\u06ED]")
_ZW_RE = re.compile(r"[\u200B-\u200F\u2028-\u202F\u2060-\u206F]")


def _norm_ar(text: str) -> str:
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", text)
    s = _ZW_RE.sub("", s)
    s = _ARABIC_DIACRITICS_RE.sub("", s)
    s = s.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    s = s.replace("ى", "ي").replace("ة", "ه").replace("ؤ", "و").replace("ئ", "ي")
    return s.lower().strip()


def _any_keyword(text: str, keywords: Iterable[str]) -> bool:
    """Return True if any keyword appears as a whole token (or short
    span) in the normalised text. We use ``in`` rather than word
    boundary regex because Arabic morphology often glues prefixes
    (``ال``, ``ب``, ``و``, ``ف``) to nouns, and full-token matching
    would miss the common cases. The classifier is intentionally
    over-permissive on the HARD bucket — false-positives there just
    mean "polite apology instead of joke", never a leak."""
    norm = _norm_ar(text)
    return any(kw in norm for kw in keywords)


# ── Hard / sensitive topic vocabulary ────────────────────────────────────────
# When ANY of these keywords appear the classifier returns
# ``TIER_HARD`` regardless of other signals. Keep the lists tight but
# inclusive — bias toward false-positives. A polite apology is
# always safer than a playful but uncertain reply on a medical /
# legal / financial / political question.
_HARD_MEDICAL_KW = frozenset({
    # diagnosis / symptoms
    "مرض", "اعراض", "تشخيص", "الم", "وجع", "سرطان", "ضغط", "سكر",
    "قلب", "ربو", "حساسيه", "حمل", "ولاده", "تطعيم", "لقاح",
    # treatment / drugs
    "دواء", "ادويه", "علاج", "وصفه", "روشته", "جرعه", "حبوب", "مضاد",
    "مسكن", "كبسوله", "حقنه", "ابره", "مستشفي", "طبيب", "دكتور", "صيدليه",
})
_HARD_LEGAL_KW = frozenset({
    "قانون", "قضيه", "محكمه", "محامي", "محاميه", "نزاع", "دعوي",
    "حكم", "تقاضي", "شكوي", "بلاغ", "نيابه", "تحقيق", "وكاله شرعيه",
})
_HARD_FINANCIAL_KW = frozenset({
    "استثمار", "اسهم", "سهم", "بورصه", "تداول", "عمله", "كريبتو",
    "عمله رقميه", "بتكوين", "ايثيريوم", "فوركس", "تمويل", "قرض",
    "تسهيلات", "بنك", "فائده", "ربا", "تأمين", "تامين", "ضريبه",
    "زكاه", "ميراث", "ارث",
})
_HARD_POLITICAL_DEEP_KW = frozenset({
    # Deep / opinionated political — note ``من رئيس`` is HANDLED
    # below as a SAFE_FACT pattern, NOT here.
    "حرب", "حروب", "نزاع سياسي", "انتخابات",
    "مظاهرات", "ثوره", "احتلال", "صراع", "اسرائيل", "غزه",
    "حزب", "احزاب", "ديمقراطيه", "ديكتاتور", "نظام الحكم",
})
_HARD_RELIGIOUS_FATWA_KW = frozenset({
    # Fatwa-level questions — Nahla is NOT a religious authority.
    # Polite simple greetings ("السلام عليكم") are caught by
    # INTENT_GREETING upstream and never reach this classifier.
    "حلال", "حرام", "فتوي", "حكم شرعي", "هل يجوز", "يجوز",
    "كفاره", "زنا", "ربا",
})
_HARD_OFF_NAHLA_KW = frozenset({
    # Clear "this is not a sales conversation" topics from the live
    # incident report and the merchant's pushback feedback. Kept
    # short — most of these are not in scope for any honey shop.
    "كهرباء", "كهربا", "فاتوره الكهرباء", "تكييف", "مكيف",
    "شقه", "فيلا", "ايجار", "تأجير", "تاجير", "عقار",
    "تشطيب", "بناء", "دهان", "ديكور",
    "سياره", "سيارات", "قيادة", "رخصه",
    "وظيفه", "توظيف", "راتب", "سيره ذاتيه",
    "برمجه", "كود", "بايثون", "جافا", "html",
})
_HARD_KEYWORDS = (
    _HARD_MEDICAL_KW
    | _HARD_LEGAL_KW
    | _HARD_FINANCIAL_KW
    | _HARD_POLITICAL_DEEP_KW
    | _HARD_RELIGIOUS_FATWA_KW
    | _HARD_OFF_NAHLA_KW
)


# ── Chitchat vocabulary ──────────────────────────────────────────────────────
# Casual conversational filler — short, factually empty, safe to
# answer with a playful canned reply that bridges back to honey.
# Greetings proper ("السلام عليكم" / "كيف حالك") are caught upstream
# by INTENT_GREETING; what falls through here is the SECOND-turn
# small talk or alternate phrasings the rule-based intent missed.
_CHITCHAT_MOOD_KW = frozenset({
    "اخبارك", "اخبارك",      # both forms (with/without ya)
    "كيفك",
    "وش مسوي", "ايش مسوي", "وش تسوي", "ايش تسوي",
    "تمام",
    "موجوده",
})
_CHITCHAT_WEATHER_KW = frozenset({
    "الطقس", "الجو", "الحر", "البرد", "الشمس", "الرياح",
    "ممطر", "مغيم", "مشمس",
})
_CHITCHAT_TIME_KW = frozenset({
    "الساعه", "كم الساعه", "وش الوقت", "ايش الوقت", "الوقت كم",
    "متي", "اي ساعه",
})
_CHITCHAT_PERSONAL_BANTER_KW = frozenset({
    "تحبيني", "تحبني", "احبك", "بحبك",
    "اسمك ايش", "ايش اسمك", "وش اسمك", "من انتي", "من انت",
    "روبوت", "بوت", "ذكاء اصطناعي", "انسانه",
    "هههه", "ههه", "ضحك",
})
_CHITCHAT_KEYWORDS_BY_TOPIC: dict[str, frozenset[str]] = {
    "mood":    _CHITCHAT_MOOD_KW,
    "weather": _CHITCHAT_WEATHER_KW,
    "time":    _CHITCHAT_TIME_KW,
    "banter":  _CHITCHAT_PERSONAL_BANTER_KW,
}


def chitchat_topic(message: str) -> str | None:
    """Return the chitchat sub-topic name (``mood`` / ``weather`` /
    ``time`` / ``banter``) when the message matches one of the known
    chitchat patterns, else ``None``. Used by the responder template
    to pick a topic-appropriate playful reply.
    """
    norm = _norm_ar(message)
    if not norm:
        return None
    for topic, words in _CHITCHAT_KEYWORDS_BY_TOPIC.items():
        if any(w in norm for w in words):
            return topic
    return None


# ── Safe-fact vocabulary ─────────────────────────────────────────────────────
# Patterns that suggest the customer is asking a public, well-known
# factoid that does not require web search. We accept these into the
# SAFE_FACT tier even though we can't deterministically answer them
# — the responder will defer to a TIGHTLY-CONSTRAINED LLM call. The
# outbound sanitiser is the safety net for URL leaks.
#
# Note: anything matching a HARD keyword above is rejected BEFORE
# we get to this list, so "من رئيس امريكا" lands as SAFE_FACT but
# "من حاكم غزه" would land as HARD via the political-deep bucket.
#
# IMPORTANT: The patterns below match against the OUTPUT of
# ``_norm_ar``, not raw user text. The normaliser collapses hamza
# variants (e.g. ``رئيس`` → ``رييس``, ``أمير`` → ``امير``) and
# strips diacritics — so the pattern source has to use the
# post-normalised forms. Putting raw ``رئيس`` here would never
# match a customer who actually typed ``رئيس``.
_SAFE_FACT_INTRO_PATTERNS = [
    # "من رئيس X" / "من ملك X" — public head-of-state question.
    # Note: ``رئيس`` → ``رييس`` after normalisation.
    re.compile(r"(?:^|\s)من\s+(?:هو\s+)?(?:رييس|ملك|امير|سلطان|حاكم)(?:$|\s)"),
    # "ايش عاصمة X" / "وش عاصمة X" / "كم عدد سكان X".
    # ``عاصمة`` → ``عاصمه`` after ``ة`` → ``ه`` normalisation.
    re.compile(r"(?:^|\s)(?:ايش|وش|ما|ماهي|كم)\s+(?:عاصمه|عدد\s+سكان|مساحه|طول|عمر|وزن)(?:$|\s)"),
    # "متى استقلت" / "متى تأسس" — historical factoids that don't change.
    # ``متى`` → ``متي`` ; ``تأسس`` → ``تاسس``.
    re.compile(r"(?:^|\s)متي\s+(?:استقل(?:ت)?|تاسس(?:ت)?|بدا(?:ت)?)(?:$|\s)"),
    # English-language openers — let the LLM handle (e.g. tourists).
    re.compile(r"\bwho\s+is\s+the\s+(?:president|king|leader)\b", re.IGNORECASE),
]


def _matches_safe_fact(message: str) -> bool:
    norm = _norm_ar(message)
    if not norm:
        return False
    return any(p.search(norm) for p in _SAFE_FACT_INTRO_PATTERNS)


# ── Classifier entry point ──────────────────────────────────────────────────
def classify_out_of_scope_tier(message: str) -> str:
    """Classify an out-of-scope message into one of three tiers.

    Order of checks (first match wins):
      1. HARD vocab — sensitive topics OR clear off-Nahla domain.
      2. CHITCHAT vocab — casual filler that has a playful canned reply.
      3. SAFE_FACT patterns — well-known factoid the LLM can answer.
      4. Default → HARD (conservative: polite apology beats a guess).

    Always returns a tier constant. Never raises.
    """
    if not message or not isinstance(message, str):
        return TIER_HARD

    if _any_keyword(message, _HARD_KEYWORDS):
        return TIER_HARD

    topic = chitchat_topic(message)
    if topic is not None:
        return TIER_CHITCHAT

    if _matches_safe_fact(message):
        return TIER_SAFE_FACT

    # Very short messages (<= 3 words, no question mark, no
    # keywords) — treat as chitchat so a casual "تمام" / "اوكي"
    # / "ههههه" / "زين" lands a friendly playful reply instead of
    # a wall-of-text apology.
    word_count = len([w for w in re.split(r"\s+", message.strip()) if w])
    if word_count <= 3 and "؟" not in message and "?" not in message:
        return TIER_CHITCHAT

    return TIER_HARD


__all__ = [
    "TIER_CHITCHAT",
    "TIER_SAFE_FACT",
    "TIER_HARD",
    "classify_out_of_scope_tier",
    "chitchat_topic",
]
