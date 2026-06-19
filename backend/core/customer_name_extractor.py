"""
core/customer_name_extractor.py
───────────────────────────────
Conservative, regex-driven extractor for "the customer told us their
name" patterns in inbound WhatsApp text.

Why a separate module
---------------------
The full AI pipeline (``modules/ai/brain``) already extracts a
``customer_name`` slot from inbound messages when the LLM thinks it
spotted a name. That slot drives ``OrderPreparationState.
customer_first_name`` — i.e. it's used for ORDER creation. It is NOT
used to update the canonical ``Customer.name`` row in the DB.

We need a second, narrower channel that:
  * Runs on every inbound text (not only during the order funnel).
  * Triggers ONLY on unambiguous self-identification patterns —
    "اسمي محمد", "أنا اسمي عبدالله", "معك فهد" — never on bare
    "أنا …" (complaints, jokes, requests) or incidental name
    mentions elsewhere in the sentence.
  * Returns a single, clean name string with a confidence label so
    the caller can decide whether to write to ``Customer.name``.

What gets accepted
------------------
We extract a name from an inbound message ONLY when:

  1. The message matches one of the explicit self-introduction
     patterns (anchored at the start of the message or directly
     after common particles).
  2. The captured group is 2–4 tokens long.
  3. Every captured token is letters-only (Arabic letters or Latin
     letters), no digits, no emoji, no punctuation.
  4. No token is on the stopword list (commercial labels,
     conversational fillers).
  5. Total length is 2–60 characters.

The function returns ``None`` for everything else. We bias HEAVILY
toward false-negatives — a missed name is a non-event (the merchant
can still type it from the inline editor), while a wrong name
adoption ("أنا عاجلاً" → ``Customer.name = "عاجلاً"``) is
embarrassing AND hard to roll back because it triggers the
``manual_name_override`` flag.

Public API
==========

  from core.customer_name_extractor import (
      extract_high_confidence_name,
      ExtractedName,
  )

  res = extract_high_confidence_name("اسمي محمد العتيبي")
  if res:
      # res.value     = "محمد العتيبي"
      # res.pattern   = "اسمي"
      # res.confidence= "high"
      ...

Crash-safe: never raises, returns ``None`` on any unexpected input.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("nahla.customer_name_extractor")


# ── Anchors ─────────────────────────────────────────────────────────
#
# Each anchor is a phrase that USUALLY introduces a name when it sits
# at the very start of the message (after optional Arabic article
# articles + leading punctuation). We anchor at start-of-string so
# casual use of the same word inside a longer sentence does NOT
# trigger an extraction.
#
# We deliberately do NOT include bare "أنا …" — "أنا" appears in
# countless non-naming contexts ("أنا أبغى", "أنا انضحك علي",
# "أنا وصلت"). Only an explicit "أنا اسمي …" intro is allowed.
_NAME_PATTERNS = [
    # "اسمي محمد"             | "اسمي محمد العتيبي"
    (
        re.compile(
            r"^\s*(?:يا\s+)?(?:هلا\s+)?(?:اسمي|إسمي|اسمى|إسمى)"
            r"\s+(?P<name>[\u0600-\u06FF\u0750-\u077Fa-zA-Z][\u0600-\u06FF\u0750-\u077Fa-zA-Z\s]{1,58})\s*$"
        ),
        "اسمي",
    ),
    # "أنا اسمي عبدالله" — explicit self-ID only; never bare "أنا …"
    (
        re.compile(
            r"^\s*(?:انا|أنا)\s+(?:اسمي|إسمي|اسمى|إسمى)"
            r"\s+(?P<name>[\u0600-\u06FF\u0750-\u077Fa-zA-Z][\u0600-\u06FF\u0750-\u077Fa-zA-Z\s]{1,58})\s*$"
        ),
        "أنا اسمي",
    ),
    # "معك محمد" — corner-shop self-intro
    (
        re.compile(
            r"^\s*(?:معك|معاك|معاكم)"
            r"\s+(?P<name>[\u0600-\u06FF\u0750-\u077F][\u0600-\u06FF\u0750-\u077F\s]{1,28})\s*$"
        ),
        "معك",
    ),
    # "اكتبني محمد" / "سجلني فهد"
    (
        re.compile(
            r"^\s*(?:اكتبني|سجلني|اسجلني|اسجل\s+اسمي)"
            r"\s+(?P<name>[\u0600-\u06FF\u0750-\u077F][\u0600-\u06FF\u0750-\u077F\s]{1,28})\s*$"
        ),
        "اكتبني",
    ),
    # "اسم العميل: محمد" / "الاسم: محمد"
    (
        re.compile(
            r"^\s*(?:الاسم|اسم|الإسم|اسم\s+العميل|اسم\s+المستلم)"
            r"\s*[:\-—]\s*(?P<name>[\u0600-\u06FF\u0750-\u077Fa-zA-Z][\u0600-\u06FF\u0750-\u077Fa-zA-Z\s]{1,58})\s*$"
        ),
        "الاسم:",
    ),
    # Latin "my name is ..." / "I am ..."
    (
        re.compile(
            r"^\s*(?:my\s+name\s+is|i\s*am|i'm|name)"
            r"\s+(?P<name>[A-Za-z][A-Za-z\s.\-']{1,58})\s*$",
            re.IGNORECASE,
        ),
        "my_name_is",
    ),
]


# Tokens that look like names structurally but are conversational
# fillers, commercial labels, or status words. If ANY of these survive
# in the captured token list, we reject the extraction. The list is
# the same shape as ``customer_display._STOP_TOKENS_AR`` plus a few
# anchors we cannot allow as names ("نعم", "لا", "حسنا"…).
_BLOCKED_TOKENS = frozenset({
    # Conversational fillers / answers
    "نعم", "لا", "ايوه", "ايوا", "أيوه", "اوكي", "اوك", "تمام",
    "حسنا", "حسناً", "طيب", "ابشر", "ابشري", "اكيد", "اكيدي",
    "ممتاز", "تسلم", "تسلمي", "شكرا", "شكراً", "والله",
    "السلام", "عليكم", "مرحبا", "هلا", "هاي",
    # Commercial / role labels
    "عميل", "عميلة", "عملاء", "زبون", "زبونة",
    "ضيف", "ضيفة", "متجر",
    "customer", "user", "guest", "client", "buyer",
    # Status / questions that match the regex anchor
    "وش", "ايش", "كيف", "متى", "وين", "ليش",
    "بكم", "بمت", "كم", "السعر", "الطلب",
    # Polite responses that show up after أنا
    "تمامة", "موجود", "موجودة", "متوفر", "متوفرة",
    "جاهز", "جاهزة", "حاضر", "حاضرة",
    "في", "هنا", "بعيد", "قريب",
    # Common verbs / clauses that follow "أنا" and are NEVER a name.
    # All forms here are the normalized output (alef/yeh/teh-marbuta
    # mapped) — we compare against the normalised token.
    "ابغي", "ابغى", "ابي", "اريد", "محتاج", "محتاجه",
    "ودي", "اشتي", "اشتري", "اطلب", "اسال", "اشكر",
    "متضايق", "متضايقه", "زعلان", "زعلانه", "تعبان", "تعبانه",
    "موجوده", "متاكد", "متاكده", "مستعجل", "مستعجله",
    "مسافر", "مسافره", "خارج", "داخل",
    "من", "الى", "عند", "بعد", "قبل", "مع",
    "محل", "زبون",
    # ── Verbs / complaints / request context (Jun 2026 P0) ─────────────
    # Production: "انا انضحك علي" → Customer.name = "انضحك عليه".
    # Normalised forms only — see ``_normalize_arabic``.
    "انضحك", "انضحكك", "انضح", "امزح", "امزحك",
    "اشتريت", "اشتري", "طلبت", "دفعت", "ارسلت",
    "مشكله", "مشكل", "شكوى", "شكوي",
    "شحنه", "الشحنه", "شحنة", "الشحنة",
    # Conversation / complaint fragments (Jun 2026 P0)
    "ايه", "اية", "وقف", "شغلتنا", "شغلنا", "النحلة", "نحلة",
    # ── Arrival / presence verbs (May 2026 hotfix) ────────────────────
    # Real cases from production: "أنا وصلت" / "وصلت" / "أنا جاي"
    # / "جايه الحين" got stored as the customer's name (we ended up
    # calling them "أبوي وصلت" in subsequent turns). These are status
    # statements, never personal names. Normalised forms only — see
    # ``_normalize_arabic`` for the mapping (alef-hamza → alef,
    # ta-marbuta → ha, etc.).
    "وصلت", "وصل", "وصلنا", "وصلتي", "وصلتو",
    "جاي", "جايه", "جاية", "جايين", "جاي الحين", "جايه الحين",
    "راجع", "راجعه", "راجعة", "رايح", "رايحه", "رايحة",
    "طالع", "طالعه", "طالعة", "نازل", "نازله", "نازلة",
    "اقرب", "قريب", "بعيد",
    "بانتظار", "بانتظارك", "منتظر", "منتظره", "منتظرك",
    # Single-word arrival/presence statements that arrive WITHOUT
    # the "أنا" prefix (e.g. a bare "وصلت") — these match other
    # anchors (الاسم: …) only when typed deliberately, so the
    # blocklist is the right place to catch them.
    "هلا", "اهلا", "اهلين", "السلام",
})


# Honorifics / prefixes that are LEGAL parts of a Saudi name. We
# preserve these as part of the captured value but require AT LEAST
# one non-prefix token alongside them so we never store a bare
# "أبو" / "عبد".
_NAME_PREFIXES = frozenset({
    "أبو", "أبا", "أبي", "ابو", "ابا", "ابي",
    "أم", "أما", "أمي", "ام", "اما", "امي",
    "عبد",
    "آل", "ال",
    "بن", "ابن", "ابنة", "بنت",
})


_TOKEN_MIN_LEN = 2
_TOTAL_MAX_TOKENS = 4
_TOTAL_MIN_LEN = 2
_TOTAL_MAX_LEN = 60


@dataclass
class ExtractedName:
    """Result of a successful extraction."""
    value:      str       # e.g. "محمد العتيبي"
    pattern:    str       # which anchor matched ("اسمي" / "أنا" / …)
    confidence: str       # "high" only for now; reserved for future


def _is_pure_letter_token(token: str) -> bool:
    """Reject tokens with any digit, punctuation, emoji, or symbol.
    Allow Arabic letters, Latin letters, and the apostrophe inside
    Latin names (D'Angelo). Tashkīl marks pass through."""
    if not token:
        return False
    for ch in token:
        if ch.isspace():
            return False
        if ch.isdigit():
            return False
        # Common Saudi name punctuation: apostrophe + hyphen.
        if ch in ("'", "-", "."):
            continue
        # Arabic letters block
        if "\u0600" <= ch <= "\u06FF":
            continue
        # Arabic supplement
        if "\u0750" <= ch <= "\u077F":
            continue
        # Latin letters
        if ch.isalpha():
            continue
        return False
    return True


_ANA_INTRO_RE = re.compile(
    r"^\s*(?:انا|أنا)\s+(?:اسمي|إسمي|اسمى|إسمى)\s+",
    re.UNICODE,
)
_ANA_BARE_RE = re.compile(r"^\s*(?:انا|أنا)\s+", re.UNICODE)


def _message_starts_with_bare_ana(message: str) -> bool:
    """True when the message opens with ``أنا`` without an explicit
    name-intro marker (``أنا اسمي …``). Such messages are never
    name introductions — they are complaints, jokes, or requests."""
    if not _ANA_BARE_RE.match(message):
        return False
    return not bool(_ANA_INTRO_RE.match(message))


def _normalize_arabic(token: str) -> str:
    """Light Arabic normalisation used ONLY for stopword comparison.
    We do NOT mutate the stored value — the customer's exact
    spelling is what gets written to the DB."""
    t = token
    t = t.replace("ـ", "")           # tatweel
    t = re.sub(r"[\u064B-\u065F\u0670]", "", t)  # diacritics
    t = (
        t.replace("أ", "ا")
         .replace("إ", "ا")
         .replace("آ", "ا")
         .replace("ى", "ي")
         .replace("ة", "ه")
    )
    return t


def extract_high_confidence_name(message: str) -> Optional[ExtractedName]:
    """Return the customer's self-declared name when the message
    matches one of the conservative patterns above. ``None`` for
    everything else.

    Never raises. Logs at debug level on every successful match so
    we can audit false positives from production traffic.
    """
    if not message or not isinstance(message, str):
        return None
    txt = message.strip()
    if not txt:
        return None
    if len(txt) > 200:
        # Long messages are almost never single-purpose name
        # introductions. Avoid wasting regex passes — and avoid
        # the rare case where a long message coincidentally
        # starts with "اسمي ...".
        return None
    if _message_starts_with_bare_ana(txt):
        return None

    for pattern, label in _NAME_PATTERNS:
        m = pattern.match(txt)
        if not m:
            continue
        raw_name = (m.group("name") or "").strip()
        raw_name = re.sub(r"\s+", " ", raw_name)
        if not raw_name:
            continue
        if not (_TOTAL_MIN_LEN <= len(raw_name) <= _TOTAL_MAX_LEN):
            continue

        tokens = raw_name.split(" ")
        if not tokens or len(tokens) > _TOTAL_MAX_TOKENS:
            continue

        ok = True
        non_prefix_tokens = 0
        for tok in tokens:
            if not _is_pure_letter_token(tok):
                ok = False
                break
            norm = _normalize_arabic(tok).lower()
            if norm in _BLOCKED_TOKENS:
                ok = False
                break
            if len(tok) < _TOKEN_MIN_LEN and tok.lower() not in _NAME_PREFIXES:
                ok = False
                break
            if tok not in _NAME_PREFIXES and _normalize_arabic(tok) not in _NAME_PREFIXES:
                non_prefix_tokens += 1
        if not ok:
            continue
        if non_prefix_tokens == 0:
            # A bare "أبو" / "عبد" alone is not a name.
            continue

        result = ExtractedName(
            value=raw_name,
            pattern=label,
            confidence="high",
        )
        logger.info(
            "[NAME_EXTRACTOR] match pattern=%s raw=%r → name=%r",
            label, message[:80], raw_name,
        )
        return result

    return None
