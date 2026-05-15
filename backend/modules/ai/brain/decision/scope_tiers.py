"""
brain/decision/scope_tiers.py
─────────────────────────────
Hard-only out-of-scope classifier (May 2026 — third revision).

Why this exists
───────────────
* May 2026 #1 incident — the AI leaked DuckDuckGo result dumps into
  customer threads after mishandling "ايهما حساب كهرباء الشقة". We
  gated external research behind an env switch (the real fix) and
  bolted on a 3-tier classifier that intercepted INTENT_GENERAL
  before the merchant brain saw it.

* May 2026 #2 regression — the 3-tier router over-corrected: a
  significant chunk of legitimate honey-adjacent questions ("حبة
  البركة"، "السعال"، "كيف أسوق للعسل"، "إيش الخدمات") were getting
  matched as chitchat / hard tier and never reached the merchant
  brain, which means the KB + catalogue + sales_context for those
  questions was being ignored. Nahla turned into a clownish
  emoji-throwing deflector instead of the smart, KB-aware assistant
  it was the day before.

* This revision — the classifier now has ONE job: detect questions
  that are unambiguously OFF-DOMAIN (electricity bills, real estate,
  programming, legal cases, financial investing, drug dosages, deep
  political topics, war) and return ``TIER_HARD``. EVERYTHING ELSE
  returns ``TIER_PASSTHROUGH`` — the engine falls through to
  ``ACTION_LLM_REPLY`` which runs the full merchant brain with KB,
  catalogue, and sales_context. The brain is the right place to
  answer "حبة البركة معكم؟" or "كيف أسوّق للعسل؟" because it has the
  merchant's real product list and knowledge base in scope.

Honey-relevance principle
─────────────────────────
A keyword that COULD legitimately relate to honey use cases must
NOT live in the HARD list:
  * cough / immunity / colds → traditional honey remedies
  * halal / haram / prophetic medicine → cultural context for honey
  * pregnancy / childbirth → honey + recipes context
  * tea / coffee → honey pairings
We deliberately keep those off the HARD list so the merchant brain
can answer naturally using the KB.

Tier surface (kept stable for downstream callers)
─────────────────────────────────────────────────
* ``TIER_HARD``        — clearly off-domain. Engine emits a polite
                          short deflection via the responder.
* ``TIER_PASSTHROUGH`` — NEW (May 2026 #2). Engine should NOT
                          intercept; the merchant brain handles it.
* ``TIER_CHITCHAT`` / ``TIER_SAFE_FACT`` — preserved as constants
                          for backward import compatibility with
                          earlier callers, but the classifier no
                          longer returns them. The composer keeps a
                          template variant for the HARD case only.

Banned reply phrases (forbidden by merchant feedback May 2026):
    - "هذا خارج نطاق متجرنا"
    - "أنا هنا — قول وش تحتاج"
    - "وأكمل معك"
"""
from __future__ import annotations

import logging
import re
import unicodedata
from typing import Iterable

logger = logging.getLogger("nahla.brain.scope_tiers")

# Tier constants — surfaced as ``Decision.args["tier"]`` so the
# responder can pick the right template path without re-classifying.
TIER_HARD        = "hard"
TIER_PASSTHROUGH = "passthrough"

# Legacy constants — kept for backward import compatibility with
# downstream callers. ``classify_out_of_scope_tier`` no longer
# returns these values; the merchant brain handles those flows.
TIER_CHITCHAT  = "chitchat"
TIER_SAFE_FACT = "safe_fact"


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
# ``TIER_HARD``. The vocab is INTENTIONALLY narrow — bias toward
# false-NEGATIVES, not false-positives. Better to let a borderline
# question reach the merchant brain (which has KB + catalogue +
# sales_context to handle it intelligently) than to short-circuit
# into a deflection that ignores the merchant's actual store.
#
# Honey-adjacent terms that USED to live here and were REMOVED in
# May 2026 #2 because they trigger legitimate KB-driven answers:
#   * cough / immunity / colds / allergy / pregnancy / childbirth
#     → honey is the classic remedy for several of these
#   * halal / haram / fatwa / prophetic medicine
#     → cultural context for honey + traditional recipes
#   * insurance / inheritance / zakat
#     → too broad; "زكاة العسل" is a real merchant question
# If you're tempted to add a keyword to this list, ask yourself:
# "could a honey-shop merchant's KB legitimately answer this?".
# If yes → DON'T add it. Let the brain handle it.

_HARD_MEDICAL_KW = frozenset({
    # Strict diagnosis / prescription / professional-care signals
    # that DO require a real doctor. Casual mentions of symptoms
    # ("التهاب حلق") are intentionally NOT here — those can be
    # honey-relevant ("ملعقة عسل دافي").
    "تشخيص", "روشته", "جرعه", "جرعات", "وصفه طبيه",
    "حبوب",       # specifically pharmaceutical pills, not honey caps
    "كبسوله", "حقنه", "ابره طبيه",
    "مضاد حيوي", "مسكن", "تخدير", "تطعيم", "لقاح",
    "صيدليه", "روشته طبيه",
    "تحاليل", "اشعه",
})
_HARD_LEGAL_KW = frozenset({
    "قانون", "قضيه قانونيه", "محكمه", "محامي", "محاميه",
    "دعوي", "تقاضي", "نيابه عامه", "تحقيق جنائي",
    "وكاله شرعيه", "كاتب عدل",
})
_HARD_FINANCIAL_KW = frozenset({
    "استثمار", "اسهم", "بورصه", "تداول", "كريبتو",
    "عمله رقميه", "بتكوين", "ايثيريوم", "فوركس",
    "قرض", "تسهيلات بنكيه", "تمويل عقاري",
})
_HARD_POLITICAL_DEEP_KW = frozenset({
    # ONLY deep / opinionated political. Casual factoid questions
    # ("من رئيس امريكا") fall through to the brain — it's a small
    # well-known fact a sales assistant can deflect gracefully.
    "حرب", "حروب", "نزاع سياسي", "انتخابات",
    "مظاهرات", "ثوره", "احتلال", "صراع",
    "حزب", "احزاب", "ديمقراطيه", "ديكتاتور", "نظام الحكم",
})
_HARD_OFF_NAHLA_KW = frozenset({
    # Topics that no honey-shop KB will ever cover. Keep tight.
    "كهرباء", "كهربا", "فاتوره الكهرباء", "تكييف", "مكيف",
    "شقه", "فيلا", "ايجار", "تأجير", "عقار", "عقارات",
    "تشطيب", "بناء", "دهان", "ديكور",
    "سياره", "سيارات", "قياده", "رخصه قياده",
    "وظيفه", "توظيف", "راتب", "سيره ذاتيه",
    "برمجه", "بايثون", "جافا سكربت", "html", "كود برمجي",
    "ويندوز", "لينكس", "اندرويد", "ايفون اصلاح",
})
_HARD_KEYWORDS = (
    _HARD_MEDICAL_KW
    | _HARD_LEGAL_KW
    | _HARD_FINANCIAL_KW
    | _HARD_POLITICAL_DEEP_KW
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
    """Decide whether a message is unambiguously off-domain.

    Returns:
      * ``TIER_HARD``        — the message contains a strict
                               off-domain keyword (electricity bill,
                               apartment construction, legal case,
                               stock investment, drug dosage, war,
                               etc.). The engine emits a polite
                               canned deflection.
      * ``TIER_PASSTHROUGH`` — everything else, INCLUDING honey-
                               adjacent questions ("حبة البركة"،
                               "السعال"، "كيف أسوّق للعسل")، casual
                               chitchat ("كم الساعة"، "وش أخبارك"),
                               safe factoids ("من رئيس أمريكا")، and
                               anything that COULD be answered using
                               the merchant's KB / catalogue / sales
                               context. The engine falls through to
                               ``ACTION_LLM_REPLY`` so the merchant
                               brain (which has all that context) can
                               compose a natural, KB-aware reply.

    Never raises.
    """
    if not message or not isinstance(message, str):
        return TIER_PASSTHROUGH

    if _any_keyword(message, _HARD_KEYWORDS):
        return TIER_HARD

    return TIER_PASSTHROUGH


__all__ = [
    "TIER_HARD",
    "TIER_PASSTHROUGH",
    "TIER_CHITCHAT",    # legacy alias kept for downstream imports
    "TIER_SAFE_FACT",   # legacy alias kept for downstream imports
    "classify_out_of_scope_tier",
    "chitchat_topic",
]
