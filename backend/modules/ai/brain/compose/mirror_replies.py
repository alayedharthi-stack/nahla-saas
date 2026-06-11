"""
modules/ai/brain/compose/mirror_replies.py
──────────────────────────────────────────
Culturally-mirrored social replies — May 2026 #9.

Why this layer exists
─────────────────────
The deterministic ``social_reply()`` template pool rotates through
generic variants per category — which is great for variety but BAD
when the customer's exact phrasing carries a culturally canonical
reciprocal. Example failures from production:

  customer:  "تسلم"            → pool answered: "وياك يا غالي 🌹..."
                                  expected:       "الله يسلمك ..."

  customer:  "بيض الله وجهك"   → pool answered: "الله يبيض وجهك مثل ما
                                                 بيضت وجهنا ..."
                                  expected:       "وجهك أبيض ..." (lighter)

The mirror is not a CANNED corpus, it's a CONTRACT: when the customer
uses one of the closed set of culturally-anchored blessings, the
reply MUST start with the conventional reciprocal of THAT exact
phrasing. Falling back to a random pool entry hurts rapport because
the customer hears it as "we didn't actually read your line".

Design
──────
* Pure function — no DB / no LLM / no logger / no state. Receives the
  raw inbound text and returns either a culturally-mirrored reply
  string OR ``None`` (no mirror match → caller falls back to the
  rotating pool, which still owns the long-tail of phrasings).
* Patterns are CONSERVATIVE and ANCHORED. Each pattern requires a
  hard linguistic marker so unrelated turns ("تسلم لي على الأولاد")
  never trigger.
* Replies stay masculine-by-default because the existing gender
  conjugator (``modules/ai/gender/conjugator.py``) runs AFTER and
  feminises the closed set of suffixes when the gender hint is
  high-confidence female. This module never duplicates that work.
* No emoji bloat — at most ONE rose / heart per reply, matching the
  style of the existing template pool.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────────
# Arabic normaliser — kept private here. Same shape as scope_tiers /
# social_classifier (hamza fold, NFKC, diacritics strip) so the patterns
# below can be written in their canonical form without listing every
# variant.
# ─────────────────────────────────────────────────────────────────────────────
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


# ─────────────────────────────────────────────────────────────────────────────
# Mirror rules — ordered priority. First match wins. Each tuple:
#   (compiled pattern over the NORMALISED text, reciprocal reply string)
#
# A rule may include a plural marker (وجوهكم / تسلمون) that takes
# precedence over its singular sibling, hence the explicit ordering.
# ─────────────────────────────────────────────────────────────────────────────

_MIRROR_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    # ── "بيض الله وجهك" → "وجهك أبيض" ────────────────────────────────
    # Strongest cultural reciprocal — the heavy "مثل ما بيضت وجوهنا"
    # variant in the template pool overstates it for routine praise.
    # We keep the reciprocal humble and let the strong-praise pool
    # carry the heavier version when the rotation lands there.
    (re.compile(r"بيض\s*الله\s*وجوهكم"),
     "وجوهكم أبيض 🌹\nالله يحفظكم ويسعدكم."),
    (re.compile(r"بيض\s*الله\s*وجهك"),
     "وجهك أبيض 🌹\nالله يحفظك ويسعدك."),

    # ── "تسلم" → "الله يسلمك" ────────────────────────────────────────
    # Anchored to standalone forms — we must NOT mirror inside e.g.
    # "تسلم لي على الأولاد" (tasallam → carry my greetings) which
    # carries a different meaning. The patterns require word-end
    # anchoring or a short vocative tail.
    (re.compile(r"^\s*تسلمون\s*[.!،]?\s*$"),
     "الله يسلمكم 🌹"),
    (re.compile(r"^\s*تسلموا?\s*[.!،]?\s*$"),
     "الله يسلمكم 🌹"),
    # Singular: "تسلم" / "تسلمي" / "تسلم يا غالي" / "تسلم لك" /
    # "تسلم كثير". We accept a single courtesy tail token; anything
    # longer falls through.
    (re.compile(
        r"^\s*تسلمي?\s*"
        r"(?:يا\s*(?:غالي|الغالي|طيب|الطيب|كريم|الكريم|"
        r"حبيب|الحبيب|اخ|الاخ|اخت|الاخت))?"
        r"\s*(?:لك|لكم|كثير|كثيرا|جزيلا)?\s*[.!،]?\s*$"
     ),
     "الله يسلمك 🌹"),

    # ── "جزاك الله خير" → "وإياك ..." ────────────────────────────────
    # The conventional Arabic reciprocal is "وإياك"; the pool variant
    # ("وياك يا غالي / الله يجزاك خير") is fine but lands generic. We
    # mirror it more tightly here when the customer used the exact
    # phrase.
    (re.compile(r"جزاكم\s*الله\s*(?:خير|الخير|كل\s*خير)"),
     "وإياكم 🌹\nالله يجزيكم مثل ما دعيتم وأكثر."),
    (re.compile(r"جزاك\s*الله\s*(?:خير|الخير|كل\s*خير)"),
     "وإياك 🌹\nالله يجزيك مثل ما دعيت وأكثر."),

    # ── "الله يعطيك العافية" → "الله يعافيك" ─────────────────────────
    (re.compile(r"الله\s*يعطيكم\s*العافيه"),
     "الله يعافيكم 🤍\nشكراً لذوقكم."),
    (re.compile(r"(?:الله\s*)?يعطيك\s*العافيه"),
     "الله يعافيك 🤍\nشكراً لذوقك."),

    # ── "الله يعافيك" → mirror + light dua ───────────────────────────
    (re.compile(r"^\s*الله\s*يعافيكم\s*[.!،]?\s*$"),
     "وإياكم 🤍\nالله يعافيكم ويسعدكم."),
    (re.compile(r"^\s*الله\s*يعافيك\s*[.!،]?\s*$"),
     "وإياك 🤍\nالله يعافيك ويسعدك."),

    # ── "الله يسعدك" → "يسعد قلبك" ───────────────────────────────────
    (re.compile(r"^\s*الله\s*يسعدكم\s*[.!،]?\s*$"),
     "وإياكم 🤍\nيسعد قلوبكم."),
    (re.compile(r"^\s*الله\s*يسعدك\s*[.!،]?\s*$"),
     "وإياك 🤍\nيسعد قلبك."),

    # ── "ربي يحفظك" → short reciprocal ───────────────────────────────
    (re.compile(r"^\s*ربي\s*يحفظكم\s*[.!،]?\s*$"),
     "ويحفظكم يا رب"),
    (re.compile(r"^\s*ربي\s*يحفظك\s*[.!،]?\s*$"),
     "ويحفظك يا رب"),

    # ── "الله يحفظك" → "ويحفظك" ──────────────────────────────────────
    (re.compile(r"^\s*الله\s*يحفظكم\s*[.!،]?\s*$"),
     "ويحفظكم 🌹\nالله يطول بأعماركم."),
    (re.compile(r"^\s*الله\s*يحفظك\s*[.!،]?\s*$"),
     "ويحفظك 🌹\nالله يطول بعمرك."),

    # ── "الله يبارك فيك" → "وفيك بارك الله" ──────────────────────────
    (re.compile(r"الله\s*يبارك\s*فيكم"),
     "وفيكم بارك الله 🌹"),
    (re.compile(r"الله\s*يبارك\s*ف[يى]ك"),
     "وفيك بارك الله 🌹"),

    # ── "الله يكثر خيرك" → mirror ────────────────────────────────────
    (re.compile(r"الله\s*يكثر\s*خيرك"),
     "وخيرك دائم 🌹\nالله يبارك لك."),

    # ── "ربي يوفقك" / "الله يوفقك" → mirror ──────────────────────────
    (re.compile(r"^\s*(?:ربي|الله)\s*يوفقك\s*[.!،]?\s*$"),
     "وإياك 🌹\nالله يوفقك ويسدد خطاك."),
)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def mirror_reply(inbound_text: str) -> Optional[str]:
    """Return a culturally-mirrored reply for ``inbound_text`` or
    ``None`` if no rule fires.

    A None result means "no canonical mirror exists for this exact
    phrasing — fall back to the rotating template pool".

    The function never raises and is safe to call on any input
    (including ``None`` or empty strings).
    """
    if not inbound_text or not isinstance(inbound_text, str):
        return None
    norm = _norm(inbound_text)
    if not norm:
        return None
    # Length guard — mirror rules are short-message contracts. A long
    # message that happens to contain a blessing token usually carries
    # a commercial intent too; let the pool / brain handle it.
    if len(norm.split()) > 8:
        return None

    for pattern, reply in _MIRROR_RULES:
        if pattern.search(norm):
            return reply
    return None


__all__ = ["mirror_reply"]
