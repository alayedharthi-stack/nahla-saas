"""
modules/ai/gender/detector.py
─────────────────────────────
Infer the customer's gender from three signals, in descending order
of reliability:

1. **Verb suffixes in the current inbound message** — Arabic marks
   the feminine pronoun explicitly with the suffixes ـِين / ـين on
   present-tense verbs, ـتِ on past-tense verbs, and ـي on the
   imperative. Detection here uses anchored token patterns to avoid
   false positives from words that merely end in ـي (like "يا
   غالي"). The masculine form is the unmarked default in Arabic,
   so a masculine-only signal is never strong on its own.

2. **Customer name** — a small whitelist of common Gulf Arabic
   first names. The list is intentionally narrow so we don't tag
   ambiguous names (e.g. "نور" can be either gender, so it's NOT
   on either list). The whitelist returns a strong hint only when
   the FIRST token of the name is an unambiguous member.

3. **Sticky prior hint** — once the conversation has classified the
   customer, the result is mirrored into
   :class:`MerchantConversationState` by the pipeline. On
   subsequent turns we honour that hint with a mild decay so a
   single ambiguous turn doesn't unwind a strong earlier signal,
   but a contradicting verb-form signal can still flip the
   classification.

Confidence scale:

* ``0.0``        — no signal / unknown. The conjugator is a no-op.
* ``0.5–0.69``   — weak signal. Saved as state, NOT applied.
* ``0.7``        — application threshold. Conjugator activates.
* ``0.85+``      — strong direct signal from the CURRENT message.
* ``1.0``        — explicit grammatical marker that has no
                   masculine equivalent (e.g. "أبشري", "خبريني").

Why not also detect strong "male"? Arabic's masculine form is the
default — every neutral phrase ("جزاك الله خير", "تسلم", "تفضل")
is masculine. We only need to fire on the *female* signal because
that's the only case the conjugator changes. Naming the male case
explicitly here is still useful because (a) the admin / debug
audit might want to see "we classified this customer as male
based on name" and (b) it keeps the state machine symmetric so a
future feature (e.g. "address male customers as 'يا غالي'
explicitly") can read it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# ─────────────────────────────────────────────────────────────────────────────
# Public dataclass
# ─────────────────────────────────────────────────────────────────────────────

GENDER_MALE = "male"
GENDER_FEMALE = "female"
GENDER_UNKNOWN = "unknown"

# Threshold the conjugator uses. Exposed as a constant so callers /
# tests can reference it without a magic literal.
APPLY_CONFIDENCE_THRESHOLD = 0.70


@dataclass(frozen=True)
class GenderHint:
    """Structured result of one gender-detection call.

    ``value`` is ``"male"`` / ``"female"`` / ``"unknown"``. The
    conjugator only acts on ``"female"`` with
    ``confidence >= APPLY_CONFIDENCE_THRESHOLD``. ``source`` is a
    short tag for logging / observability: ``"verb"``, ``"name"``,
    ``"context"``, or ``"none"``.
    """
    value: str = GENDER_UNKNOWN
    confidence: float = 0.0
    source: str = "none"


_UNKNOWN = GenderHint()


# ─────────────────────────────────────────────────────────────────────────────
# Signal #1: verb-suffix patterns
# ─────────────────────────────────────────────────────────────────────────────
#
# Each pattern is an "anchored" regex — it matches the suffix only
# at the END of a word so we never confuse "خبريني" (female
# imperative) with a non-existent contraction inside a longer word.
# The set is intentionally short: every pattern here MUST be a form
# that has no masculine equivalent.

# Trailing-boundary lookahead used by every Arabic pattern below.
# Python's ``\b`` is Unicode-aware but interacts unpredictably with
# Arabic combining marks (especially diacritics), so we instead
# require an explicit non-letter tail: whitespace, end-of-string,
# or one of the common Arabic / Latin punctuation marks. Using a
# lookahead (rather than consuming the boundary char) keeps the
# match offset clean for any future ``re.findall`` callers.
_TRAIL = r"(?=\s|$|[.,،!?؟…])"

# Imperatives ending in ـي (clearly female, no male equivalent).
# We don't use ``\b`` because Python's word-boundary class is
# unreliable around Arabic combining marks; instead each pattern
# is anchored by requiring a "non-letter" tail (whitespace,
# punctuation, end-of-string) so we never match a prefix inside a
# longer word.
_FEMALE_IMPERATIVE_PATTERNS = [
    r"أبشري" + _TRAIL,
    r"خبريني" + _TRAIL,
    r"قوليلي" + _TRAIL,
    r"قولي" + _TRAIL,         # "قولي لي" / "قولي الأسعار"
    r"تفضلي" + _TRAIL,
    r"أرسلي" + _TRAIL,
    r"ارسلي" + _TRAIL,
    r"اكتبي" + _TRAIL,
    r"اطلبي" + _TRAIL,
    r"اشتري" + _TRAIL,
    r"شوفي" + _TRAIL,
    r"عطيني" + _TRAIL,
    r"أعطيني" + _TRAIL,
    r"اعطيني" + _TRAIL,
    r"حطي" + _TRAIL,
    r"خذي" + _TRAIL,
    r"روحي" + _TRAIL,
    r"جيبي" + _TRAIL,
    r"قومي" + _TRAIL,
    r"كملي" + _TRAIL,
]

# Present-tense forms ending in ـين / ـِين after a Khaleeji-style
# pronoun stem (ت-). These are unambiguously female because the
# corresponding male form ends in ـ (or has no suffix).
_FEMALE_PRESENT_PATTERNS = [
    r"\bتحبين\b",      # do you (f) want
    r"\bتبغين\b",      # do you (f) want (gulf)
    r"\bتريدين\b",     # do you (f) want
    r"\bتقدرين\b",     # can you (f)
    r"\bتشوفين\b",     # do you (f) see
    r"\bتروحين\b",     # do you (f) go
    r"\bتجين\b",       # do you (f) come
    r"\bتعطين\b",      # do you (f) give
    r"\bتأخذين\b",    # do you (f) take
    r"\bتفهمين\b",    # do you (f) understand
    r"\bتعرفين\b",    # do you (f) know
    r"\bتسوين\b",      # do you (f) do (gulf)
    r"\bتحطين\b",      # do you (f) put
    r"\bتاكلين\b",     # do you (f) eat
    r"\bتشربين\b",    # do you (f) drink
    r"\bتنامين\b",     # do you (f) sleep
    r"\bتجلسين\b",    # do you (f) sit / stay
    r"\bتيجين\b",     # do you (f) come (gulf variant)
    r"\bتكونين\b",    # are you (f) being
]

# Past-tense ـتِ — in raw text the kasra is almost always omitted
# (Arabic keyboards don't include it by default), so we instead
# match common past-tense female sentence-initial forms that the
# Khaleeji vernacular spells with ـتي. Conservative: only the
# variants we can verify in production logs.
_FEMALE_PAST_PATTERNS = [
    r"\bحبيتي\b",     # you (f) liked
    r"\bبغيتي\b",     # you (f) wanted (gulf)
    r"\bشفتي\b",       # you (f) saw
    r"\bجيتي\b",       # you (f) came
    r"\bرحتي\b",       # you (f) went
    r"\bسويتي\b",     # you (f) did (gulf)
    r"\bأخذتي\b",      # you (f) took
    r"\bاكلتي\b",     # you (f) ate
    r"\bعرفتي\b",     # you (f) knew
    r"\bأرسلتي\b",     # you (f) sent
    r"\bطلبتي\b",     # you (f) ordered
    r"\bدفعتي\b",     # you (f) paid
    r"\bحطيتي\b",     # you (f) put
    r"\bسمعتي\b",     # you (f) heard
    r"\bفهمتي\b",     # you (f) understood
]

# Object / possessive suffix ـكِ — only matches when the kasra is
# actually written. Production messages rarely include the kasra,
# but when they do it's an unambiguous female signal. We do NOT
# rely on ``\b`` here — Python's word boundary is Unicode-aware
# but interacts unpredictably with Arabic combining marks. Instead
# we require the ـكِ to be followed by whitespace, end-of-string,
# or common Arabic / Latin punctuation (defined at the top of the
# module as ``_TRAIL``).
_FEMALE_OBJECT_PATTERNS = [
    r"كِ" + _TRAIL,          # bare ـكِ at end of word
    r"لكِ" + _TRAIL,         # for you (f)
    r"إليكِ" + _TRAIL,       # to you (f)
    r"عليكِ" + _TRAIL,       # on you (f)
    r"بكِ" + _TRAIL,          # by you (f)
    r"منكِ" + _TRAIL,        # from you (f)
]

_FEMALE_RE = re.compile(
    "|".join(
        _FEMALE_IMPERATIVE_PATTERNS
        + _FEMALE_PRESENT_PATTERNS
        + _FEMALE_PAST_PATTERNS
        + _FEMALE_OBJECT_PATTERNS
    )
)


def _from_verb_suffixes(message: str) -> GenderHint:
    """Look for unambiguous female verb suffixes in *message*.

    Returns a high-confidence female hint when ANY pattern fires.
    Masculine forms are the Arabic default — they don't fire here
    because we can't distinguish "خبرني" (male imperative) from a
    customer of either gender using the masculine-default form.
    """
    if not message:
        return _UNKNOWN
    if _FEMALE_RE.search(message):
        return GenderHint(
            value=GENDER_FEMALE,
            confidence=0.90,
            source="verb",
        )
    return _UNKNOWN


# ─────────────────────────────────────────────────────────────────────────────
# Signal #2: name whitelist
# ─────────────────────────────────────────────────────────────────────────────
#
# Conservative on purpose. Names whose gender is genuinely
# ambiguous (نور / ريم / ملاك / علا — used by both in different
# regions) are intentionally absent. Add more entries only when
# you can verify with confidence in customer-profile data.

_MALE_NAMES = frozenset({
    "محمد", "أحمد", "احمد", "علي", "خالد", "سعد", "فهد",
    "عبدالله", "عبد الله", "عبدالرحمن", "عبد الرحمن",
    "عبدالعزيز", "عبد العزيز", "سعود", "ناصر", "سلطان",
    "ماجد", "تركي", "بدر", "فيصل", "ياسر", "زياد", "طارق",
    "راكان", "نواف", "بندر", "مشاري", "وليد", "عمر", "يوسف",
    "إبراهيم", "ابراهيم", "سامي", "حمد", "حمود", "حسن",
    "حسين", "هشام", "رائد", "أنس", "انس", "زيد", "أيمن",
    "ايمن", "بسام", "حذيفة", "صالح", "عبدالملك", "عبد الملك",
    "عبدالإله", "عبد الإله", "نايف", "مساعد", "متعب",
    "رياض", "سيف", "أسامة", "اسامة", "خلف", "غازي", "كرم",
    "حسام", "مهند", "بدوي", "دخيل", "دخيل الله", "ضيف الله",
})

_FEMALE_NAMES = frozenset({
    "نوره", "نورة", "شهد", "سارة", "ساره", "سارا", "ريم",  # ريم listed as ambiguous in some regions — kept here only on explicit operator request
    "لينا", "دانة", "دانه", "لمى", "لمار", "جوري", "جودي",
    "مريم", "فاطمة", "عائشة", "هند", "هيا", "روان", "رهف",
    "شيخة", "أمل", "منى", "أسماء", "اسماء", "نجلاء", "نجلا",
    "خديجة", "سعاد", "لطيفة", "موضي", "العنود", "مها",
    "نوف", "دلال", "بشرى", "آلاء", "الاء", "عبير", "غاده",
    "غادة", "سميرة", "سميره", "وفاء", "أروى", "اروى",
    "أريج", "اريج", "جواهر", "حصة", "حصه", "رقية", "رقيه",
    "زينب", "سهام", "صفية", "صفيه", "عواطف", "فتحية",
    "فتحيه", "فوزية", "فوزيه", "ليلى", "ليلي", "نادية",
    "ناديه", "نجاة", "نجاه", "هدى", "هيفاء", "وداد", "وضحى",
    "وضحه", "تغريد", "تهاني", "تركية", "تركيه",
})


def _first_token(name: str) -> str:
    if not name:
        return ""
    cleaned = re.sub(r"[\u200e\u200f\ufeff]", "", name).strip()
    return cleaned.split()[0] if cleaned else ""


def _from_name(customer_name: Optional[str]) -> GenderHint:
    """Look up *customer_name*'s first token in the whitelist.

    Returns medium-confidence (0.75) instead of strong because
    names are not perfectly diagnostic — a merchant may have
    nicknamed someone, two siblings share an account, etc. The
    verb-suffix signal (0.90) wins on conflict.
    """
    first = _first_token(customer_name or "")
    if not first:
        return _UNKNOWN
    if first in _FEMALE_NAMES:
        return GenderHint(value=GENDER_FEMALE, confidence=0.75, source="name")
    if first in _MALE_NAMES:
        return GenderHint(value=GENDER_MALE, confidence=0.75, source="name")
    return _UNKNOWN


# ─────────────────────────────────────────────────────────────────────────────
# Signal #3: sticky prior + cascade
# ─────────────────────────────────────────────────────────────────────────────

_PRIOR_DECAY = 0.05
_PRIOR_FLOOR = 0.60


# Explicit first-person gender self-identification. Not name inference.
_EXPLICIT_MALE_PATTERNS = [
    re.compile(r"(?:أنا|انا|إني|اني)\s+(?:رجل|ولد|ذكر)" + _TRAIL, re.UNICODE),
    re.compile(r"(?:ولست|لست)\s+(?:امرأة|امراة|أنثى|انثى)" + _TRAIL, re.UNICODE),
    re.compile(r"(?:مو|مش)\s+(?:امرأة|امراة)" + _TRAIL, re.UNICODE),
]
_EXPLICIT_FEMALE_PATTERNS = [
    re.compile(r"(?:أنا|انا|إني|اني)\s+(?:امرأة|امراة|بنت|أنثى|انثى)" + _TRAIL, re.UNICODE),
    re.compile(r"(?:ولست|لست)\s+(?:رجل|رجلا)" + _TRAIL, re.UNICODE),
]


def _from_explicit_self_identification(message: str) -> GenderHint:
    """Customer-stated gender in the current message. Stronger than names."""
    text = str(message or "").strip()
    if not text:
        return _UNKNOWN
    if any(p.search(text) for p in _EXPLICIT_MALE_PATTERNS):
        return GenderHint(value=GENDER_MALE, confidence=0.95, source="verb")
    if any(p.search(text) for p in _EXPLICIT_FEMALE_PATTERNS):
        return GenderHint(value=GENDER_FEMALE, confidence=0.95, source="verb")
    return _UNKNOWN


def detect_gender(
    message: str,
    customer_name: Optional[str] = None,
    prior_hint: Optional[GenderHint] = None,
) -> GenderHint:
    """Cascade through the signals and return the best hint.

    Order:
      1. Explicit first-person self-identification — strongest.
      2. Verb-suffix signal (current message).
      3. Name whitelist signal — medium.
      4. Prior sticky hint (with mild per-turn decay).

    Conflict resolution: if the verb signal and the name signal
    disagree, we **return unknown** rather than pick a side — the
    customer is more important than the dataset. A real follow-up
    turn will resolve it. Explicit self-identification is never
    overridden by the name whitelist.
    """
    explicit_hint = _from_explicit_self_identification(message or "")
    if explicit_hint.confidence > 0:
        return explicit_hint

    verb_hint = _from_verb_suffixes(message or "")
    name_hint = _from_name(customer_name)

    # Direct verb signal — strongest, but defer to a name conflict.
    if verb_hint.confidence > 0 and name_hint.confidence > 0:
        if verb_hint.value != name_hint.value:
            # Hard ambiguity: refuse to swap. Better safe than wrong.
            return GenderHint(
                value=GENDER_UNKNOWN,
                confidence=0.0,
                source="conflict",
            )
    if verb_hint.confidence > 0:
        return verb_hint
    if name_hint.confidence > 0:
        return name_hint

    # Fall back to sticky prior — when it carries non-zero
    # confidence we ALWAYS surface it (with mild decay), even when
    # the decayed value drops below the conjugator's application
    # threshold. The conjugator owns the on/off decision; the
    # detector's job is to preserve the classification so a future
    # reinforcement turn can re-raise it.
    if (
        prior_hint
        and prior_hint.value in (GENDER_MALE, GENDER_FEMALE)
        and prior_hint.confidence > 0
    ):
        decayed = max(prior_hint.confidence - _PRIOR_DECAY, _PRIOR_FLOOR)
        return GenderHint(
            value=prior_hint.value,
            confidence=decayed,
            source="context",
        )

    return _UNKNOWN


__all__ = [
    "APPLY_CONFIDENCE_THRESHOLD",
    "GENDER_FEMALE",
    "GENDER_MALE",
    "GENDER_UNKNOWN",
    "GenderHint",
    "detect_gender",
]
