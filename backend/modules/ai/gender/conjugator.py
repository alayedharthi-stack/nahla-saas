"""
modules/ai/gender/conjugator.py
───────────────────────────────
Turn a male-default Arabic social reply into a female-coded variant.

Why this exists
───────────────
The existing social-reply templates
(``compose/templates._SOCIAL_*_VARIANTS``) are written in the
masculine, which is Arabic's unmarked default and reads naturally
to either gender. When the upstream detector reports HIGH
confidence that the customer is female, we want the reply to feel
addressed to her — "تسلمين" instead of "تسلم", "يا الغالية" instead
of "يا الغالي" — without rewriting any template or growing the
template count.

This module is a **closed-set token swap**. Strict rules:

* Female-only direction. The masculine form is the default and we
  never touch it. So a low-confidence / male / unknown hint is a
  no-op pass-through.
* Closed swap table. Adding a new swap is a code change with a
  test. We never use heuristic / probabilistic NLP here.
* No new emojis, no new endearments, no new phrases. We only
  conjugate forms that ALREADY appear inside the existing
  template pool.
* Bounded application — the conjugator is wired into ONE call
  site (``ACTION_SOCIAL_REPLY`` in the composer). Sales / KB /
  catalog / platform / out-of-scope flows never see it.

The replacement loop is order-sensitive: longer phrases run BEFORE
shorter prefixes that might overlap (e.g. "يا الغالي" must run
before "يا غالي" so we don't double-swap). Each entry includes a
short comment that names which template variant the source string
comes from, so future template edits can audit at a glance.
"""
from __future__ import annotations

import re
from typing import List, Tuple

from .detector import (
    APPLY_CONFIDENCE_THRESHOLD,
    GENDER_FEMALE,
    GenderHint,
)


# ─────────────────────────────────────────────────────────────────────────────
# Female swap table — closed set
# ─────────────────────────────────────────────────────────────────────────────
#
# Each entry is ``(source_regex, replacement)``. We use regex
# (not plain ``str.replace``) for ONE reason: idempotency. The
# masculine form "تسلم" is a literal substring of the feminine form
# "تسلمين", so a plain replace would turn the second call's input
# back into a corrupted "تسلمينين". A negative lookahead pattern
# (``تسلم(?!ي)``) refuses to match when the female suffix is
# already present, making every pass a fixpoint.
#
# Order matters: longer phrases first so a shorter prefix can't
# steal a match from inside a longer one ("الله يجزاك خير" wins
# over "جزاك الله خير", and "الله يبارك فيك" wins over the bare
# "فيك").

# Lookaheads used below. Pre-compiled so we don't allocate them
# every call. The `_NO_KASRA` guard prevents re-conjugating an
# already-female word ending in ـكِ; `_NO_FEMALE_YA` prevents
# matching the masculine "تسلم" / "تفضل" / "أبشر" when the female
# ـي suffix is already present.
_NO_KASRA = r"(?!ِ)"
_NO_FEMALE_YA = r"(?!ي)"
_NO_FEMALE_TA_YA = r"(?!تي)"   # for past-tense "قصرت" → already "قصرتي"

_FEMALE_PATTERNS_RAW: List[Tuple[str, str]] = [
    # ── Vocative endearments (THANKS / BASMALA / COMPLIMENT) ────────
    # "يا الغالي" must run BEFORE "يا غالي" (same prefix), and both
    # refuse to match when "ية" already follows.
    (r"يا الغالي(?!ة)",  "يا الغالية"),
    (r"يا غالي(?!ة)",    "يا الغالية"),

    # ── Long blessing compounds (run first to avoid sub-matches) ────
    (r"الله يجزاك خير" + _NO_KASRA,  "الله يجزاكِ خير"),
    (r"جزاك الله خير"  + _NO_KASRA,  "جزاكِ الله خير"),
    (r"الله يبارك فيك" + _NO_KASRA,  "الله يبارك فيكِ"),
    (r"الله يبيض وجهك" + _NO_KASRA,  "الله يبيض وجهكِ"),
    (r"الله يطول بعمرك"+ _NO_KASRA,  "الله يطول بعمركِ"),
    (r"الله يحييك"     + _NO_KASRA,  "الله يحييكِ"),
    (r"الله يعافيك"    + _NO_KASRA,  "الله يعافيكِ"),
    (r"الله يسعدك"     + _NO_KASRA,  "الله يسعدكِ"),
    (r"الله يكرمك"     + _NO_KASRA,  "الله يكرمكِ"),
    (r"الله يخليك"     + _NO_KASRA,  "الله يخليكِ"),
    (r"الله يحفظك"     + _NO_KASRA,  "الله يحفظكِ"),
    (r"ربي يعافيك"     + _NO_KASRA,  "ربي يعافيكِ"),
    (r"يعافيك ربي"     + _NO_KASRA,  "يعافيكِ ربي"),
    (r"حياك الله"      + _NO_KASRA,  "حياكِ الله"),
    (r"تشرفنا فيك"     + _NO_KASRA,  "تشرفنا فيكِ"),

    # ── Imperatives ─────────────────────────────────────────────────
    (r"تسلم"   + _NO_FEMALE_YA, "تسلمين"),
    (r"تفضل"   + _NO_FEMALE_YA, "تفضلي"),
    (r"أبشر"   + _NO_FEMALE_YA, "أبشري"),
    (r"خبرني",                   "خبريني"),

    # ── Standalone "ـك" verbs / nouns ───────────────────────────────
    # Run AFTER the long compounds above so a phrase like "الله يعافيك"
    # is already conjugated and these singletons only fire on bare
    # remaining occurrences (e.g. "ويسعدك" → "ويسعدكِ"). Each pattern
    # carries the no-kasra lookahead so a second pass is a fixpoint.
    (r"يحييك"  + _NO_KASRA, "يحييكِ"),
    (r"يعافيك" + _NO_KASRA, "يعافيكِ"),
    (r"يسعدك"  + _NO_KASRA, "يسعدكِ"),
    (r"يكرمك"  + _NO_KASRA, "يكرمكِ"),
    (r"يخليك"  + _NO_KASRA, "يخليكِ"),
    (r"يحفظك"  + _NO_KASRA, "يحفظكِ"),
    (r"بعمرك"  + _NO_KASRA, "بعمركِ"),

    # ── Compliments / acknowledgements (COMPLIMENT_VARIANTS) ────────
    (r"ما قصرت" + _NO_FEMALE_TA_YA,  "ما قصرتي"),
    (r"بحسن ظنك" + _NO_KASRA,        "بحسن ظنكِ"),
    (r"من لطفك"  + _NO_KASRA,         "من لطفكِ"),
    (r"لذوقك"    + _NO_KASRA,         "لذوقكِ"),
    (r"من ذوقك"  + _NO_KASRA,         "من ذوقكِ"),
    (r"إحساسك"   + _NO_KASRA,         "إحساسكِ"),
    (r"احساسك"   + _NO_KASRA,         "احساسكِ"),

    # ── GENERAL_COURTESY — "وش اللي تحتاجه" ─────────────────────────
    (r"وش اللي تحتاجه(?!ـ|ي)", "وش اللي تحتاجينه"),
]

# Pre-compile once at module load. Failing to compile here is a
# programmer error — let the exception escape so the test suite
# catches it instead of silently degrading at runtime.
_FEMALE_PATTERNS: List[Tuple["re.Pattern[str]", str]] = [
    (re.compile(src), dst) for src, dst in _FEMALE_PATTERNS_RAW
]


def apply_gender_to_social_reply(
    reply: str,
    hint: GenderHint,
    *,
    min_confidence: float = APPLY_CONFIDENCE_THRESHOLD,
) -> str:
    """Return *reply* with female-coded forms when the hint is
    confident female; otherwise return *reply* unchanged.

    Parameters
    ----------
    reply
        Output of the social-reply template. Treated as plain text;
        emojis / newlines pass through untouched.
    hint
        :class:`GenderHint` produced by :func:`detector.detect_gender`.
    min_confidence
        Override the application threshold. Defaults to the
        module-level :data:`APPLY_CONFIDENCE_THRESHOLD` (0.70).
        Lowering it is not recommended in production — it raises the
        false-positive rate without improving real precision.

    Notes
    -----
    Male / unknown hints are explicitly NO-OP (Arabic's masculine
    is the unmarked default — the existing template is already
    correct). Empty inputs return empty. We never modify length,
    emojis, or punctuation; this is strictly a token swap.
    """
    if not reply:
        return reply
    if hint is None:
        return reply
    if hint.value != GENDER_FEMALE:
        return reply
    if hint.confidence < min_confidence:
        return reply

    out = reply
    for pattern, dst in _FEMALE_PATTERNS:
        out = pattern.sub(dst, out)
    return out


__all__ = [
    "apply_gender_to_social_reply",
]
