"""
brain/intent/social_classifier.py
─────────────────────────────────
Deterministic detector for social / courtesy / religious messages.

Why this layer exists
─────────────────────
Gulf-Arabic conversational norms are deeply social. Customers
routinely send messages that carry NO commercial intent and need a
short, culturally-appropriate human reply rather than a product
pitch:

  * "جزاك الله خير"           — thanks
  * "بيّض الله وجهك"          — blessing
  * "اللهم صل وسلم على نبينا" — prophet invocation
  * "بسم الله الرحمن الرحيم"  — basmala
  * "كفو"، "ما قصرت"، "والنعم" — compliments
  * "يعطيك العافية"           — daily courtesy

The merchant brain's sales-oriented system prompt was either staying
silent on these turns (no actionable goal) or worse, derailing into
"هل ترغب في عسل اليوم؟" — which feels robotic and breaks rapport.

The fix is NOT to teach the brain prompt about every social phrase
(that scales poorly and is fragile). It is to ROUTE these messages
to a dedicated action that emits a short canned reply by category,
exactly the way ``INTENT_GREETING`` already works for "السلام عليكم".

Public contract
───────────────
``classify_social(message: str) -> SocialMatch | None``

Returns ``SocialMatch(category=str, confidence=float)`` when the
message is dominantly social, else ``None``. The classifier is
intentionally CONSERVATIVE — false-positives would route a real
product question into a canned "وياك يا غالي" line, which would be
worse than the current silence. We only fire when the message is
short and the social signal is unambiguous; longer messages with
embedded social phrases ("شكرا لك، أريد عسل سدر") flow through to
the brain so the commercial half is honoured.

Confidence: 0.94 (chosen to beat INTENT_ASK_PRODUCT 0.82, INTENT_ASK_
PRICE 0.90, INTENT_START_ORDER 0.88, and INTENT_HESITATION 0.85 in
the rule chain's confidence-ranked merge).

Pattern source form
───────────────────
All patterns are written against the OUTPUT of ``_norm_ar``, which
collapses hamza variants (أ/إ/آ → ا), ئ → ي, ة → ه, ؤ → و, strips
ZWJ/ZWNJ and diacritics, and lowercases. This is the same pattern
the scope_tiers module uses — keeping the convention consistent
prevents "رئيس" → "رييس" surprises.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Optional


# ── Social categories ────────────────────────────────────────────────────────
SOCIAL_THANKS               = "thanks"
SOCIAL_BLESSING             = "blessing"
SOCIAL_PROPHET_INVOCATION   = "prophet_invocation"
SOCIAL_BASMALA              = "basmala"
SOCIAL_COMPLIMENT           = "compliment"
SOCIAL_GENERAL_COURTESY     = "general_courtesy"
# May 2026 #8 — distinct bucket for HEAVY, explicit praise like
# "بيض الله وجهك" / "ما قصرت" / "كفو" / "رفعت رأسنا". Carved out
# from generic ``compliment`` so the response pool can deploy a
# reciprocal-heavy reply ("الله يبيض وجهك مثل ما بيضت وجهنا") ONLY
# when the customer used a trigger that warrants it. The previous
# template pool was leaking the heavy compliment onto routine
# blessing/thanks turns, which felt over-the-top.
SOCIAL_STRONG_PRAISE        = "strong_praise"


@dataclass(frozen=True)
class SocialMatch:
    category: str
    confidence: float


# ── Arabic normaliser (same as scope_tiers — kept private to this module
#    to avoid a dependency on the decision layer). ───────────────────────
_DIACRITICS_RE = re.compile(r"[\u064B-\u065F\u0670\u06D6-\u06ED]")
_ZW_RE         = re.compile(r"[\u200B-\u200F\u2028-\u202F\u2060-\u206F]")


def _norm(text: str) -> str:
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", text)
    s = _ZW_RE.sub("", s)
    s = _DIACRITICS_RE.sub("", s)
    s = s.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    s = s.replace("ى", "ي").replace("ة", "ه").replace("ؤ", "و").replace("ئ", "ي")
    return s.lower().strip()


# ── Length / shape guard ─────────────────────────────────────────────────────
# Social classification only fires on SHORT, dominantly-social messages.
# A long message ("شكرا لك على الرد السريع، أريد عسل سدر بكميات كبيرة
# للتوزيع على الأقارب") contains a thanks phrase but is fundamentally a
# product request — we let the brain handle it. Threshold tuned to ~14
# whitespace tokens which fits even the long prophet-invocation verse
# "إن الله وملائكته يصلون على النبي يا أيها الذين آمنوا صلوا عليه وسلموا
# تسليما" (14 tokens).
_MAX_TOKENS_FOR_SOCIAL_DOMINANCE = 14


def _word_count(message: str) -> int:
    return len([w for w in re.split(r"\s+", message.strip()) if w])


# ── Prophet invocation — most specific, check first ──────────────────────────
# The full Qur'anic verse and its conventional response should ALWAYS
# classify as a prophet invocation regardless of length — these are
# never commercial. Hence we run this check before the length guard.
_PROPHET_INVOCATION_PATTERNS = [
    # Full or partial صلى الله عليه وسلم and its abbreviated form ﷺ
    re.compile(r"صلي\s*الله\s*عليه\s*و?سلم"),
    re.compile(r"\uFDFA"),  # ﷺ glyph
    # Qur'anic verse (Surat al-Ahzab 56) and its parts
    re.compile(r"ان\s*الله\s*و?ملايكته\s*يصلون\s*علي\s*النبي"),
    re.compile(r"يا\s*ايها\s*الذين\s*امنوا\s*صلوا\s*عليه"),
    # "اللهم صل وسلم على نبينا محمد" and variants
    re.compile(r"اللهم\s*صل\s*و?(?:بارك\s*و?)?سلم\s*علي"),
    re.compile(r"اللهم\s*صلي\s*و?سلم\s*علي"),
    # Standalone "اللهم صل على محمد"
    re.compile(r"اللهم\s*صل[ي]?\s*علي\s*(?:محمد|نبينا)"),
]


def _is_prophet_invocation(norm: str) -> bool:
    return any(p.search(norm) for p in _PROPHET_INVOCATION_PATTERNS)


# ── Basmala / Qur'an opening phrases ─────────────────────────────────────────
_BASMALA_PATTERNS = [
    re.compile(r"^بسم\s*الله(?:\s*الرحمن\s*الرحيم)?\s*$"),
    re.compile(r"^بسم\s*الله\s*الرحمن\s*الرحيم"),
]


def _is_basmala(norm: str) -> bool:
    return any(p.search(norm) for p in _BASMALA_PATTERNS)


# ── Thanks vocabulary (Arabic + English) ─────────────────────────────────────
# Substring matches against the normalised form. We allow these to
# appear anywhere in a SHORT message — once the length guard passes
# the message is dominantly social by definition.
_THANKS_KEYWORDS = (
    # Direct thanks
    "شكرا", "شكراً",  # the latter survives norm because tanwin is in diacritics range
    "مشكور", "مشكوره", "مشكورين", "مشكورات",
    "تسلم", "تسلمي", "تسلمو", "تسلمون",
    "يسلموا", "يسلمو",
    # Religious thanks
    "جزاك الله",
    "الله يجزاك",
    "الله يجزيك",
    "ربي يجزاك",
    # English
    "thank you", "thanks", "thx ", " thx", "tysm",
    " ty ", "^ty$",  # standalone "ty" handled via regex below
    "appreciate",
)
_TY_STANDALONE_RE = re.compile(r"^\s*ty\s*$")


def _is_thanks(norm: str) -> bool:
    if _TY_STANDALONE_RE.match(norm):
        return True
    return any(kw in norm for kw in _THANKS_KEYWORDS)


# ── Blessings (دعاء) ─────────────────────────────────────────────────────────
# Wishes of well-being / longevity / spiritual reward. These are
# conversational dua, NOT requests for service. We do NOT include the
# specifically "thanks-shaped" phrases here ("جزاك الله خير" is in
# THANKS) — that's a stylistic split, not a semantic one.
_BLESSING_KEYWORDS = (
    "يعطيك العافيه", "يعطيكم العافيه", "الله يعافيك", "الله يعافيكم",
    "الله يسعدك", "الله يسعدكم", "الله يفرحك",
    "الله يطول عمرك", "الله يطول بعمرك", "الله يبقيك",
    "بيض الله وجهك", "بيض الله وجوهكم",
    "الله يبارك لك", "الله يبارك فيك", "الله يبارك عليك",
    "الله يبارك لكم", "الله يبارك فيكم",
    "ربي يحفظك", "الله يحفظك", "الله يحفظكم",
    "رحم الله والديك", "الله يرحم والديك", "الله يرحم لي والديك",
    "الله يرضي عليك", "ربي يوفقك", "الله يوفقك", "الله يوفقكم",
    "الله يكتب لك الاجر", "الله يكثر خيرك",
)


def _is_blessing(norm: str) -> bool:
    return any(kw in norm for kw in _BLESSING_KEYWORDS)


# ── Strong praise (May 2026 #8) ──────────────────────────────────────────────
# Heavy, explicit praise tokens that warrant a heavy reciprocal reply
# ("الله يبيض وجهك مثل ما بيضت وجهنا"). Kept TIGHT on purpose: a
# trigger here makes the responder pick from the strong-praise pool
# AND unlocks the high-priority prompt allowance for the same phrase.
# Anything not in this set must NOT receive the heavy compliment —
# the customer would feel the over-reaction.
_STRONG_PRAISE_KEYWORDS = (
    "بيض الله وجهك", "بيض الله وجوهكم", "بيضت وجهنا", "بيضتو وجهنا",
    "بيضتم وجهنا", "بيضتي وجهنا", "بيضتوا وجوهنا",
    "ما قصرت", "ماقصرت", "ما قصرتو", "ماقصرتو", "ما قصرتم", "ماقصرتم",
    "كفو", "كفوو", "كفوكم", "كفوكن",
    "رفعت راسي", "رفعتم راسي", "رفعتو راسي",
    "رفعت راسنا", "رفعتم راسنا", "رفعتو راسنا",
    "خدمه كبيره", "خدمه كبيرة", "خدمة كبيرة", "خدمة كبيره",
    "خدمتنا خدمه كبيره", "خدمتنا خدمة كبيرة",
    "والله ما قصرت", "والله ماقصرت",
)


def _is_strong_praise(norm: str) -> bool:
    """High-confidence detector for EXPLICIT heavy praise.

    Only fires on the closed trigger set — bare "شكرا" / "تسلم" /
    "الله يجزاك" do NOT count. The pool consumer relies on this
    being conservative so the heavy reciprocal reply never lands on
    a casual thanks/blessing turn (the May 2026 #8 regression).
    """
    return any(kw in norm for kw in _STRONG_PRAISE_KEYWORDS)


# ── Compliments / "you've done well" ─────────────────────────────────────────
# Tightened May 2026 #8 — the strong-praise tokens above are now their
# OWN category. Generic compliments here stay routed to the lighter
# compliment pool. We deliberately leave overlapping words ("كفو" /
# "ما قصرت") out of THIS list now; if they appear the strong-praise
# branch wins because it runs first.
_COMPLIMENT_KEYWORDS = (
    "والنعم", "نعم الرد",
    "سلمت", "سلمتي", "سلمتو",
    "احسنت", "احسنتم", "ابدعت", "ابدعتو", "ابدعتم",
    "ممتاز", "روعه", "تحفه",
    "ما شاء الله",
    "تبارك الله",
    "زين", "زينه", "حلو",   # standalone-style only via length guard below
)


def _is_compliment(norm: str) -> bool:
    return any(kw in norm for kw in _COMPLIMENT_KEYWORDS)


# ── General courtesy ─────────────────────────────────────────────────────────
# Catch-all for short polite phrases that aren't pure greetings but
# clearly don't ask for anything: "حياك", "هلا والله", "تكفى",
# "الله يحييك", "حياكم الله", short reassurances ("لا يهمك" / "تمام").
# This category exists so the rules layer can short-circuit instead of
# falling through to the LLM brain, which is the regression we're
# fixing.
_GENERAL_COURTESY_KEYWORDS = (
    "حياك", "حياكم", "حيا الله", "الله يحييك",
    "هلا والله", "هلا وغلا",
    "تكفي", "تكفا", "تكفون",
    "لا يهمك", "لايهمك", "لا تشيل هم",
    "خير ان شاء الله",   # post-norm form of "خير إن شاء الله"
    "بالعكس", "بالعفو",  # "you're welcome"
    "اهلين", "يا هلا",
)


def _is_general_courtesy(norm: str) -> bool:
    return any(kw in norm for kw in _GENERAL_COURTESY_KEYWORDS)


# ── Disqualifiers — if the message ALSO contains commercial intent,
#    we don't short-circuit. The brain handles the mixed turn so the
#    commercial half is honoured. Conservative: leans toward letting
#    the brain see anything ambiguous. ────────────────────────────────────
_COMMERCIAL_DISQUALIFIERS = (
    "ابغي", "ابغى", "ابي", "اريد",       # buying verbs
    "ابحث", "دور", "وين", "فين",           # search verbs
    "سعر", "تكلفه", "بكم", "كم سعر",      # price asks
    "اطلب", "اشتري", "خذ لي",              # ordering verbs
    "ادفع", "رابط الدفع",                  # payment
    "كيلو", "نصف كيلو", "جرام", "علبه",   # quantity / packaging
    "شحن", "توصيل",                        # shipping context
    # NOTE: "نحلة" deliberately NOT here — it's the platform name,
    # which the platform classifier handles separately. A social
    # message that just says "شكرا نحلة" should still classify as
    # SOCIAL.
)


# May 2026 #8 — relational signals that DOMINATE a courtesy phrase. A
# message like "الله يسعدك، ما خلص اللي عندنا من أول" mixes a blessing
# with a clear deferral; the dominant content is the deferral, not the
# blessing. Treating it as plain social would dispatch a generic
# blessing reply (and historically the heavy "بيض الله وجهك" pool)
# instead of letting the stance layer + LLM honour the deferred frame.
# Listing these as disqualifiers makes the social classifier yield to
# the brain pipeline on mixed turns, which then routes through
# detect_stance and STANCE_DEFERRED. Conservative tokens only — we
# never want to disqualify a pure blessing.
_RELATIONAL_NON_SOCIAL_SIGNALS = (
    # "Still have some / hasn't finished" (leftover inventory).
    "ماخلص", "ما خلص", "ماخلصت", "ما خلصت",
    "باقي عندي", "باقي عندنا", "باقي منه عندي", "باقي منه عندنا",
    "لسه عندي", "لسه عندنا", "لسا عندي", "لسا عندنا",
    "مازال عندي", "ما زال عندي", "لازال عندي", "لا زال عندي",
    "متبقي عندي", "متبقي عندنا",
    # "Next time / later" — explicit future deferral.
    "المره الجايه", "المره القادمه", "المرات الجايه", "المرات القادمه",
    "ان شاء الله المره", "انشالله المره",
    "بعدين ان شاء الله", "لاحقا ان شاء الله",
    # Explicit "not now" combos.
    "مو الحين", "مش الحين", "ليس الان",
    # Active support signals (already in stance support_request but we
    # want the social classifier to defer here too).
    "ما وصل الطلب", "ماوصل الطلب", "تاخر الطلب", "تأخر الطلب",
    "فيه مشكله", "عندي مشكله", "مكسور", "تالف",
)


def _has_commercial_signal(norm: str) -> bool:
    return any(kw in norm for kw in _COMMERCIAL_DISQUALIFIERS)


def _has_relational_non_social_signal(norm: str) -> bool:
    """Return True when a courtesy phrase is paired with a dominant
    deferral / support signal. Lets the brain pipeline handle the
    relational frame end-to-end instead of canning a template reply
    that ignores the real intent."""
    return any(kw in norm for kw in _RELATIONAL_NON_SOCIAL_SIGNALS)


# ── Public entry point ───────────────────────────────────────────────────────
def classify_social(message: str) -> Optional[SocialMatch]:
    """Classify a message as social / courtesy / religious or return
    ``None`` if no dominant social signal is detected.

    The function never raises and is safe to call on any input. The
    rule chain caller relies on this being O(1) in message length (it
    is — all checks are bounded substring scans).
    """
    if not message or not isinstance(message, str):
        return None
    norm = _norm(message)
    if not norm:
        return None

    # 1. Prophet invocation — bypasses the length guard because the
    #    canonical verse is long and never commercial.
    if _is_prophet_invocation(norm):
        return SocialMatch(category=SOCIAL_PROPHET_INVOCATION, confidence=0.97)

    # 2. Basmala — also bypasses the length guard.
    if _is_basmala(norm):
        return SocialMatch(category=SOCIAL_BASMALA, confidence=0.95)

    # 3. Everything else requires SHORT + no commercial signal +
    #    no dominant relational signal (deferred / support).
    if _word_count(message) > _MAX_TOKENS_FOR_SOCIAL_DOMINANCE:
        return None
    if _has_commercial_signal(norm):
        return None
    if _has_relational_non_social_signal(norm):
        # Mixed turn: courtesy + deferral / support. Yield to the
        # brain pipeline so the stance detector reads it as
        # DEFERRED / SUPPORT_REQUEST and the LLM honours the real
        # frame instead of canning a generic blessing reply.
        return None

    # Strong praise — the heavy reciprocal pool ("الله يبيض وجهك ...")
    # is reserved for this category ONLY. Checked BEFORE generic
    # compliment so explicit tokens (كفو / ما قصرت / بيض الله وجهك)
    # never fall into the lighter pool.
    if _is_strong_praise(norm):
        return SocialMatch(category=SOCIAL_STRONG_PRAISE, confidence=0.95)
    if _is_thanks(norm):
        return SocialMatch(category=SOCIAL_THANKS, confidence=0.94)
    if _is_blessing(norm):
        return SocialMatch(category=SOCIAL_BLESSING, confidence=0.94)
    if _is_compliment(norm):
        return SocialMatch(category=SOCIAL_COMPLIMENT, confidence=0.94)
    if _is_general_courtesy(norm):
        return SocialMatch(category=SOCIAL_GENERAL_COURTESY, confidence=0.92)

    return None


__all__ = [
    "SOCIAL_THANKS",
    "SOCIAL_BLESSING",
    "SOCIAL_PROPHET_INVOCATION",
    "SOCIAL_BASMALA",
    "SOCIAL_COMPLIMENT",
    "SOCIAL_GENERAL_COURTESY",
    "SOCIAL_STRONG_PRAISE",
    "SocialMatch",
    "classify_social",
]
