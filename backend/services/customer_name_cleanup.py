"""
services/customer_name_cleanup.py
─────────────────────────────────
Bulk customer-name cleanup tool (admin-driven, tenant-scoped).

This module powers the **"تنظيف أسماء العملاء"** button on the customers
page. A merchant clicks it, the backend computes a *preview* of every
problematic name in the tenant, and the merchant explicitly approves
which ones to apply (per-row checkbox, or "apply high-confidence only").

The cleanup is **one-shot** — once the merchant approves, we mutate
``Customer.name`` directly and write an audit row to
``customer_name_audit_logs``. Campaigns and templates then read the
cleaned value verbatim; there is **no runtime sanitizer** doing it
again at send time (single source of truth = the stored value).

Pipeline (per name)
───────────────────
1. Strip emojis + decorative punctuation.
2. Collapse whitespace.
3. Detect "phone-only" → suggest ``None`` (caller will clear the row).
4. Detect "no letters / pure noise" → suggest ``None``.
5. Split on whitespace, drop commercial / descriptive stopwords
   (``عميل``, ``customer``, ``guest`` …), preserve patronymic
   prefixes (``أبو``, ``أم``, ``عبد``, ``بن``, ``آل`` …).
6. If the leftover is too short or has no letters → suggest ``None``.
7. Otherwise return the cleaned string.

Confidence levels
─────────────────
``high``
    The change is *mechanical* (stopword removal only) and unambiguous,
    OR the input is phone-only / pure-noise and the suggestion is
    ``None``. Safe to apply automatically via the
    "Apply high-confidence only" shortcut.

``low``
    The change required dropping non-alphabetic content from the middle
    of the string, or the result is suspiciously short (single token of
    ≤ 2 chars). Requires explicit per-row merchant approval.

Examples
────────
    "أيمن الجهني عميل"       → "أيمن الجهني"          (high)
    "Majed عميل"             → "Majed"                (high)
    "عميل"                   → None (clear)           (high)
    "عميل تعديل 238"         → None (clear)           (high — no letters left)
    "+966551234567"          → None (clear)           (high — phone-only)
    "أبو خالد"               → "أبو خالد"             (no-op, untouched)
    "عبد الرحمن"             → "عبد الرحمن"           (no-op, patronymic preserved)
    "اللهم ارفع عنا الوباء"  → None (clear)           (low — religious phrase)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# ── Stopword tokens (commercial / descriptive — never a real name) ───────────
_STOP_TOKENS_AR = frozenset({
    "عميل", "عميلة", "عملاء",
    # Common misspelling of عميلة (typed with ه instead of ة). Shows up
    # often in WhatsApp push-name imports because the keyboard auto-
    # complete drops the marbouta. Treat as a stopword.
    "عميله",
    "زبون", "زبونة", "زبونه", "زبائن",
    "ضيف", "ضيفة", "ضيفه", "ضيوف",
    "متجر",
    # Descriptive qualifiers commonly used as a placeholder name in imports.
    "جديد", "جديدة",
    "مؤقت", "مؤقتة",
    "تجريبي", "تجريبية",
    # Edit / revision markers — merchants use "تعديل 238" as a memo
    # field for a row that needs revisiting. Never a real name.
    "تعديل", "تعديلات",
    # Gregorian month names — almost always a date stamp, not a name.
    # We deliberately do NOT include Hijri months (رمضان is a real
    # given name; شعبان too) — those stay through.
    "يناير", "فبراير", "مارس", "أبريل", "ابريل",
    "مايو", "يونيو", "يونيه", "يوليو", "يوليه",
    "أغسطس", "اغسطس", "سبتمبر", "أكتوبر", "اكتوبر",
    "نوفمبر", "ديسمبر",
})

_STOP_TOKENS_EN = frozenset({
    "customer", "customers", "cust",
    "guest",    "guests",
    "user",     "users",
    "client",   "clients",
    "buyer",    "buyers",
    "shopper",  "shoppers",
    "anonymous", "anon",
    "unknown",  "unk",
    "test",     "demo", "sample",
    "n/a",      "na",   "none", "null",
})


# ── Source / channel tokens (May 2026) ───────────────────────────────────────
# Names that are actually a marketing-source label, not a person.
# Imports from offline campaigns frequently end up with rows like
# "تيك", "تيك توك", "TikTok", "سامي تيك", "بنك". These tokens are
# stripped out the same way stopwords are — and if the entire name
# consists of nothing but source tokens, the row is classified as
# ``source_label_name`` and cleared.
#
# Tokens are stored in the *normalised* form (alef variants collapsed
# to ``ا``, ta-marbuta to ``ه``, yeh-with-dots to ``ي``) so a single
# entry catches the obvious orthographic variants. Latin entries are
# lower-cased for comparison.
_SOURCE_TOKENS = frozenset({
    # TikTok family
    "تيك", "توك", "تيكتوك",
    "tik", "tok", "tiktok",
    "tt",
    # Snapchat family
    "سناب", "سنابشات",
    "snap", "snapchat",
    # Instagram family
    "انستا", "انستقرام", "انستجرام",
    "insta", "instagram", "instagrm", "ig",
    # Facebook / X / Telegram
    "فيس", "فيسبوك",
    "facebook", "fb",
    "twitter", "tweet",
    "تلجرام", "تلقرام",
    "telegram", "tg",
    # WhatsApp itself appearing as a source label on imports
    "واتس", "واتساب", "الواتس",
    "whatsapp", "whatsap", "wa", "wts", "wapp",
    # YouTube
    "يوتيوب",
    "youtube", "yt",
    # Generic offline-marketing source labels
    "بنك",         # frequent suffix on Saudi imports — "X بنك"
    "حمله",        # normalised حملة
    "قناه",        # normalised قناة
    "موقع",
    "مصدر",
    "اعلان",       # normalised إعلان
    "شركه",        # normalised شركة (when used as a marketing label)
})


# ── Location tokens (May 2026) ────────────────────────────────────────────────
# Cities, regions, and country-tier markers that show up as a "name"
# value on bulk imports — usually next to a ``"من"`` preposition.
# Stored in the normalised form (see _SOURCE_TOKENS above).
_LOCATION_TOKENS = frozenset({
    # Major Saudi cities
    "الرياض", "رياض",
    "جده", "الجده",        # normalised جدة
    "مكه", "المكه",        # normalised مكة
    "المدينه", "مدينه",    # normalised المدينة / مدينة
    "المنوره", "منوره",    # normalised المنورة — used in "المدينة المنورة"
    "الطائف", "طائف",
    "الدمام", "دمام",
    "الخبر", "خبر",
    "الاحساء", "احساء",
    "القصيم", "قصيم",
    "بريده", "بريدة",      # normalised
    "ابها", "أبها",
    "خميس", "مشيط",        # "خميس مشيط"
    "حائل",  "نجران", "تبوك", "جازان", "الجوف", "الباحه",
    "ينبع",  "الخرج",  "عرعر",
    # Regions / cardinal directions
    "الجنوب", "الشمال",
    "الشرقيه",             # normalised الشرقية
    "الغربيه",             # normalised الغربية
    "الوسطى", "الوسطي",
    # Country-tier markers
    "الخارج", "خارج",
    "السعوديه",            # normalised
    "الامارات",
    "الكويت", "البحرين", "قطر", "عمان",
    "مصر",  "اليمن", "العراق", "الاردن", "سوريا", "لبنان",
})


# ── Title / honorific tokens (May 2026) ──────────────────────────────────────
# Professional titles that are NEVER a name on their own. We drop
# these before classifying so "Eng تيك" → both tokens stripped →
# ``source_label_name`` instead of accidentally proposing "Eng" as
# the cleaned name. Real names with a title prefix (e.g. "د. سامي")
# survive because we still keep the non-title tokens.
_TITLE_TOKENS = frozenset({
    # Latin titles
    "eng", "engr", "engineer",
    "mr", "mrs", "ms", "miss",
    "dr", "prof", "prf",
    # Arabic titles (full words)
    "مهندس", "مهندسه",
    "دكتور", "دكتوره",
    "استاذ",  "استاذه",
    "شيخ",    "شيخه",
})


# ── Literal placeholder phrases (May 2026) ───────────────────────────────────
# Whole-string matches that immediately clear the row regardless of
# tokenisation. Compared on the *normalised* form (see _normalise_arabic
# below) and case-insensitive.
_PLACEHOLDER_LITERALS = frozenset({
    "بدون اسم", "بدون اسم.", "بدون_اسم",
    "لا اسم", "لا يوجد اسم", "لا يوجد",
    "no name", "noname", "no_name",
    "anonymous", "anon",
    "unknown", "unk",
    "n/a", "na", "none", "null",
})


# ── Preposition tokens (May 2026) ────────────────────────────────────────────
# Tokens that anchor a non-name expression. "من" + city → location.
# Dropping the preposition lets the location check fire on the rest
# of the string.
_PREPOSITION_TOKENS = frozenset({
    "من", "في", "الى",
})


# Categories surfaced to the dashboard for the per-reason filter.
# These strings are stable contract — the dashboard renders filter
# chips keyed by these literals (see ``dashboard/src/api/customers``).
CATEGORY_SOURCE             = "source_label_name"
CATEGORY_LOCATION           = "location_label_name"
CATEGORY_PLACEHOLDER        = "placeholder_name"
CATEGORY_GENERIC_BAD        = "generic_bad_name"
CATEGORY_SUSPICIOUS_SUFFIX  = "suspicious_suffix"
CATEGORY_OTHER              = "other"
CATEGORY_NONE               = ""    # changed=False, no category

ALL_CATEGORIES = (
    CATEGORY_SOURCE,
    CATEGORY_LOCATION,
    CATEGORY_PLACEHOLDER,
    CATEGORY_GENERIC_BAD,
    CATEGORY_SUSPICIOUS_SUFFIX,
    CATEGORY_OTHER,
)


# Arabic-letter normalisation used ONLY for stopword/token matching.
# The *original* spelling is preserved in the verdict — we never
# mutate the stored value here.
_ARABIC_NORMALISE_RE = re.compile(r"[\u064B-\u065F\u0670]")  # diacritics


def _normalise_arabic(token: str) -> str:
    """Collapse common Arabic orthographic variants so the token
    set stays small."""
    t = token
    t = _ARABIC_NORMALISE_RE.sub("", t)
    t = t.replace("ـ", "")  # tatweel
    t = (
        t.replace("أ", "ا")
         .replace("إ", "ا")
         .replace("آ", "ا")
         .replace("ى", "ي")
         .replace("ة", "ه")
    )
    return t

# Patronymic + honorific prefixes — MUST be preserved as part of a compound
# name. ``أبو خالد`` and ``عبد الرحمن`` would otherwise be wrecked by token
# filters that strip "single-name leftovers".
_PROTECTED_PREFIXES = frozenset({
    "أبو", "أبا", "أبي",
    "أم",  "أما", "أمي",
    "عبد",
    "آل",
    "ابن", "ابنة",
    "بن",  "بنت",
})

# Religious / non-human phrases that show up as the "name" field after a
# WhatsApp push-name auto-import. Match anywhere in the raw string. If
# ANY phrase hits, we clear the name — these are NEVER a person's name,
# they're status messages people set on WhatsApp.
_NONHUMAN_PHRASES = (
    "اللهم",
    "الحمدلله", "الحمد لله",
    "بسم الله",
    "ماشاء الله", "ما شاء الله",
    "سبحان الله",
    "لا اله",
    "أستغفر الله",
    "صلى الله",
    "اشتقت",
    "للبيع",
    "متوفر",
    "تواصل",
    "خصم",
    "عرض ",
)


# ── Latin-gibberish detection ─────────────────────────────────────────────────
# Catches random keyboard-mash names that come from WhatsApp display names
# or bad imports: "ohguhu/hghjgkv/", "agffsggg88", "xzxqklmnn", etc.
# Strategy:
#   1. If the string has NO Arabic characters and is all-Latin (after stripping
#      punctuation/digits), check for gibberish:
#      a. Consecutive consonant runs ≥ 4 (e.g. "hghjgkv" → run of 7)
#      b. Vowel ratio < 0.15 (almost no vowels for its length)
#      c. No spaces + short length (single token ≤ 12 chars, all consonants)
# We intentionally allow common short Latin names (Ali, Sam, Sara, Omar …).

_LATIN_VOWELS = frozenset("aeiouAEIOU")
_LATIN_ALPHA_RE = re.compile(r"[a-zA-Z]")
_ARABIC_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]")
_STRIP_NON_ALPHA_RE = re.compile(r"[^a-zA-Z]")


def _looks_latin_gibberish(raw: str) -> bool:
    """Return True if the name looks like random Latin keyboard mashing
    and is very unlikely to be a real human name.

    Only fires when the entire string has *no Arabic characters* and
    consists primarily of Latin letters. Arabic names that happen to
    contain Latin fragments (e.g. "Salla 3") are left to other checks.
    """
    stripped = raw.strip()
    if not stripped:
        return False
    # Must have at least one Latin letter
    if not _LATIN_ALPHA_RE.search(stripped):
        return False
    # Skip if string contains Arabic — this is a mixed name, not Latin gibberish
    if _ARABIC_RE.search(stripped):
        return False

    # Isolate only the Latin alpha characters for analysis
    latin_only = _STRIP_NON_ALPHA_RE.sub("", stripped).lower()
    if len(latin_only) < 3:
        return False

    # Heuristic A: vowel ratio too low
    vowel_count = sum(1 for ch in latin_only if ch in _LATIN_VOWELS)
    vowel_ratio = vowel_count / len(latin_only)
    if vowel_ratio < 0.10 and len(latin_only) >= 5:
        return True

    # Heuristic B: consecutive consonant run ≥ 4
    max_consonant_run = 0
    current_run = 0
    for ch in latin_only:
        if ch not in _LATIN_VOWELS:
            current_run += 1
            if current_run > max_consonant_run:
                max_consonant_run = current_run
        else:
            current_run = 0
    if max_consonant_run >= 4 and len(latin_only) >= 5:
        return True

    # Heuristic C: single token (no spaces in original), digits mixed in,
    # and the digit ratio check already passed — but check extra:
    # e.g. "agffsggg88" → alpha part "agffsggg" has vowel_ratio = 1/8 = 0.125
    # → already caught by heuristic A.

    return False


# ── Cleanup primitives ────────────────────────────────────────────────────────
_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001F9FF"  # symbols & pictographs
    "\U0001F600-\U0001F64F"   # emoticons
    "\U0001F680-\U0001F6FF"   # transport
    "\U0001FA00-\U0001FAFF"   # symbols & pictographs ext-A
    "\U00002600-\U000026FF"   # misc symbols
    "\U00002700-\U000027BF"   # dingbats
    "]+",
    flags=re.UNICODE,
)

# Decorative punctuation that's never part of a human name. We KEEP
# ``-``, ``'``, ``.`` because legit names use them (``Al-Sayed``,
# ``D'Angelo``, ``Mohd.``).
_BAD_PUNCT_RE = re.compile(r"[!@#$%^&*()_=+\[\]{}|\\/<>?\":;,~`«»“”‘’]+")
_MULTISPACE_RE = re.compile(r"\s+")

# Phone-detection regex: a value is "phone-only" if (after removing the
# optional leading +, spaces, and dashes) it is ALL digits and at least
# 7 characters. We do not require the +966 prefix because merchants
# also import local 05XX numbers as the "name".
_PHONE_LIKE_RE = re.compile(r"^[+]?[\d\s\-()]{7,}$")
# Digit ratio threshold — if more than this fraction of the raw string
# is digits, we treat it as phone-y noise and clear it. Real names with
# a stray digit (rare) survive at 0.4.
_DIGIT_RATIO_THRESHOLD = 0.4


@dataclass(frozen=True)
class CleanResult:
    """The verdict for one customer name.

    Attributes
    ----------
    suggested:
        ``None`` means "clear the row — there is no name here". A
        non-empty string is the cleaned replacement. Equal to ``old``
        when no change is needed (callers can short-circuit on
        ``changed=False``).
    reason:
        Short Arabic explanation of WHY the change is being suggested.
        Surfaced verbatim in the preview UI and stored in the audit log.
    confidence:
        ``"high"`` → safe to bulk-apply.
        ``"low"`` → requires per-row merchant approval.
    changed:
        ``True`` iff ``suggested != old``. Names with ``changed=False``
        are excluded from the preview entirely.
    category:
        Coarse-grained reason bucket, drives the per-reason filter
        chips in the dashboard. One of the ``CATEGORY_*`` literals at
        the top of this module. ``""`` when ``changed=False``.
    """
    old:        Optional[str]
    suggested:  Optional[str]
    reason:     str
    confidence: str   # "high" | "low"
    changed:    bool
    category:   str = CATEGORY_NONE


def _strip_definite_article(token: str) -> str:
    """Map ``العميل`` → ``عميل`` for stopword matching. Returns the
    original token if removing ``ال`` would leave fewer than two
    characters."""
    if len(token) > 3 and token.startswith("ال"):
        return token[2:]
    return token


def _has_letters(s: str) -> bool:
    return any(ch.isalpha() for ch in s)


def _digit_ratio(s: str) -> float:
    if not s:
        return 0.0
    digits = sum(1 for ch in s if ch.isdigit())
    return digits / len(s)


def _looks_phone_only(raw: str) -> bool:
    """Return True if ``raw`` is essentially a phone number masquerading
    as a name. Accepts both ``+966...`` and ``05...`` forms, with
    arbitrary internal whitespace / dashes / parens."""
    candidate = raw.strip()
    if not candidate:
        return False
    # Match if the whole string is phone-shaped...
    if _PHONE_LIKE_RE.match(candidate):
        # And contains at least 7 digits (so a 3-letter name doesn't trip
        # the regex via spaces / dashes).
        digits_only = "".join(ch for ch in candidate if ch.isdigit())
        return len(digits_only) >= 7
    return False


def _looks_literal_placeholder(raw: str) -> bool:
    """Return True if the entire raw value (after normalisation) is
    one of the canonical "no real name here" placeholders."""
    if not raw:
        return False
    candidate = _MULTISPACE_RE.sub(" ", raw).strip().lower()
    if not candidate:
        return False
    normalised = _normalise_arabic(candidate)
    return (candidate in _PLACEHOLDER_LITERALS
            or normalised in _PLACEHOLDER_LITERALS)


def _looks_nonhuman_phrase(raw: str) -> bool:
    """Return True if ``raw`` contains one of the known religious /
    promotional phrases that show up as a fake "name" via WhatsApp push
    names. We match case-insensitively after normalising spaces."""
    if not raw:
        return False
    normalised = _MULTISPACE_RE.sub(" ", raw).strip().lower()
    for phrase in _NONHUMAN_PHRASES:
        if phrase.lower() in normalised:
            return True
    return False


def compute_cleanup(raw: Optional[str]) -> CleanResult:
    """Compute a cleanup verdict for one raw customer name.

    Always returns a ``CleanResult``; callers filter by ``changed``
    if they want only the preview-worthy entries.
    """
    # ── Trivially-bad inputs ────────────────────────────────────────
    if raw is None:
        return CleanResult(
            old=None, suggested=None, reason="",
            confidence="high", changed=False,
            category=CATEGORY_NONE,
        )
    if not isinstance(raw, str):
        return CleanResult(
            old=str(raw), suggested=None, reason="قيمة غير نصية",
            confidence="high", changed=True,
            category=CATEGORY_GENERIC_BAD,
        )

    original = raw
    stripped = raw.strip()
    if not stripped:
        # Empty/whitespace-only — already effectively cleared. Not
        # surfaced in the preview.
        return CleanResult(
            old=original, suggested=None, reason="",
            confidence="high", changed=False,
            category=CATEGORY_NONE,
        )

    # ── Literal placeholder ("بدون اسم" / "unknown" / …) → clear ──
    # This must run BEFORE the phone-only check so something like
    # ``"بدون اسم"`` gets the placeholder category instead of falling
    # through to the digit / stopword path.
    if _looks_literal_placeholder(stripped):
        return CleanResult(
            old=original, suggested=None,
            reason="قيمة عامة (مثل: بدون اسم / unknown)",
            confidence="high", changed=True,
            category=CATEGORY_PLACEHOLDER,
        )

    # ── Phone-only → clear ────────────────────────────────────────
    if _looks_phone_only(stripped):
        return CleanResult(
            old=original, suggested=None,
            reason="القيمة رقم جوال وليست اسماً",
            confidence="high", changed=True,
            category=CATEGORY_PLACEHOLDER,
        )

    # ── Latin gibberish (keyboard mash / fake names) → clear ──────
    if _looks_latin_gibberish(stripped):
        return CleanResult(
            old=original, suggested=None,
            reason="اسم عشوائي غير حقيقي (حروف لاتينية بلا معنى)",
            confidence="high", changed=True,
            category=CATEGORY_GENERIC_BAD,
        )

    # ── Religious / promotional phrase → clear ────────────────────
    if _looks_nonhuman_phrase(stripped):
        return CleanResult(
            old=original, suggested=None,
            reason="عبارة غير اسمية",
            confidence="low", changed=True,
            category=CATEGORY_GENERIC_BAD,
        )

    # ── Heavy-digit ratio (e.g. "عميل 238") → clear ───────────────
    if _digit_ratio(stripped) >= _DIGIT_RATIO_THRESHOLD:
        return CleanResult(
            old=original, suggested=None,
            reason="نسبة كبيرة من الأرقام داخل الاسم",
            confidence="high", changed=True,
            category=CATEGORY_PLACEHOLDER,
        )

    # ── Tokenised stopword stripping ──────────────────────────────
    s = _EMOJI_RE.sub(" ", stripped)
    s = _BAD_PUNCT_RE.sub(" ", s)
    s = _MULTISPACE_RE.sub(" ", s).strip()
    if not s:
        return CleanResult(
            old=original, suggested=None,
            reason="لا يحتوي حروف بعد إزالة الرموز",
            confidence="high", changed=True,
            category=CATEGORY_GENERIC_BAD,
        )

    tokens = s.split(" ")
    kept: list[str] = []
    dropped: list[str] = []
    had_digits_removed   = False
    had_stopword_removed = False
    had_source_removed   = False    # "تيك" / "TikTok"
    had_location_removed = False    # "الرياض" / "من المدينة"
    had_title_removed    = False    # "Eng" / "م."
    had_prep_removed     = False    # "من"

    for token in tokens:
        if not token:
            continue
        # Drop pure-digit tokens (the "238" in "عميل تعديل 238").
        if token.isdigit():
            dropped.append(token)
            had_digits_removed = True
            continue
        bare       = _strip_definite_article(token)
        normalised = _normalise_arabic(bare).lower()
        token_lc   = token.lower()

        if token in _PROTECTED_PREFIXES or bare in _PROTECTED_PREFIXES:
            kept.append(token)
            continue
        if bare in _STOP_TOKENS_AR:
            dropped.append(token)
            had_stopword_removed = True
            continue
        if token_lc in _STOP_TOKENS_EN or bare.lower() in _STOP_TOKENS_EN:
            dropped.append(token)
            had_stopword_removed = True
            continue

        # ── New token classes (May 2026) ──────────────────────────
        # Order matters: titles before source so "Eng تيك" classifies
        # the source token correctly. Prepositions are checked last
        # so a name accidentally containing "من" inside doesn't get
        # mangled.
        if token_lc in _TITLE_TOKENS or normalised in _TITLE_TOKENS:
            dropped.append(token)
            had_title_removed = True
            continue
        if token_lc in _SOURCE_TOKENS or normalised in _SOURCE_TOKENS:
            dropped.append(token)
            had_source_removed = True
            continue
        if normalised in _LOCATION_TOKENS or bare in _LOCATION_TOKENS:
            dropped.append(token)
            had_location_removed = True
            continue
        if normalised in _PREPOSITION_TOKENS or bare in _PREPOSITION_TOKENS:
            dropped.append(token)
            had_prep_removed = True
            continue

        # Single-char leftovers are almost always punctuation residue.
        if len(token) == 1:
            dropped.append(token)
            continue
        kept.append(token)

    cleaned = _MULTISPACE_RE.sub(" ", " ".join(kept)).strip()

    # ── "Noise heuristic" — when BOTH a stopword and a digit were
    # stripped and only a single weak token survives, treat the row
    # as a placeholder rather than a real name. Examples this catches:
    #
    #   "عميل يونيو 20 88"  → kept=[]               → clear (already)
    #   "عميل تعديل 238"    → kept=[]               → clear (already)
    #   "Majed عميل 238"   → kept=["Majed"]        → KEEP — only digits
    #                                                were stripped, no
    #                                                fully-noisy context.
    #   "محمد 2024"        → kept=["محمد"]         → KEEP — no stopword
    #                                                was stripped, just
    #                                                a date suffix.
    #
    # The rule is: stopwords WERE the structural noise; digits compound
    # it. If we removed both AND we're left with a single token, the
    # original input was a placeholder, not a name. Confidence stays
    # "high" because every signal points the same way.
    if (
        had_stopword_removed
        and had_digits_removed
        and len(kept) <= 1
    ):
        return CleanResult(
            old=original, suggested=None,
            reason="عبارة غير اسمية (كلمات وصفية + أرقام)",
            confidence="high", changed=True,
            category=CATEGORY_PLACEHOLDER,
        )

    # ── Choose a coarse category for the dashboard filter ─────────
    # Priority order matters: we want the *most informative* bucket
    # to win — if BOTH a source token AND a location token were
    # stripped we surface the source because that's what the merchant
    # is most likely searching for ("delete all 'تيك توك' rows").
    def _classify_dropped() -> str:
        if had_source_removed:
            return CATEGORY_SOURCE
        if had_location_removed or had_prep_removed:
            return CATEGORY_LOCATION
        if had_stopword_removed or had_title_removed:
            return CATEGORY_GENERIC_BAD
        if had_digits_removed:
            return CATEGORY_PLACEHOLDER
        return CATEGORY_OTHER

    # ── Decide the final verdict ──────────────────────────────────
    if not cleaned or not _has_letters(cleaned):
        # Everything got dropped → clear the row, pick the category
        # based on what was dropped. "تيك" alone → source_label_name;
        # "من الرياض" → location_label_name; "Eng" → generic_bad_name.
        category = _classify_dropped() if dropped else CATEGORY_GENERIC_BAD
        if category == CATEGORY_SOURCE:
            reason = "الاسم يبدو مصدراً تسويقياً (مثل: تيك توك / TikTok)"
        elif category == CATEGORY_LOCATION:
            reason = "الاسم يبدو موقعاً جغرافياً (مدينة / منطقة)"
        elif category == CATEGORY_PLACEHOLDER:
            reason = "قيمة عامة بدون اسم حقيقي"
        else:
            reason = (
                "لا يبقى اسم حقيقي بعد إزالة الكلمات التجارية"
                if dropped else "لا يحتوي حروف"
            )
        return CleanResult(
            old=original, suggested=None,
            reason=reason,
            confidence="high", changed=True,
            category=category,
        )

    if cleaned == stripped:
        return CleanResult(
            old=original, suggested=cleaned,
            reason="", confidence="high", changed=False,
            category=CATEGORY_NONE,
        )

    # The change was a stopword-strip — high confidence.
    # If we additionally pulled digits out of the middle, downgrade
    # to "low" because the merchant might want to keep an internal ID.
    reason_bits = []
    if dropped:
        descriptive = [d for d in dropped if not d.isdigit()]
        if descriptive:
            reason_bits.append(
                "إزالة كلمات زائدة: " + ", ".join(sorted(set(descriptive)))
            )
        if had_digits_removed:
            reason_bits.append("إزالة أرقام داخلية")
    reason = " — ".join(reason_bits) or "تنظيف بسيط"

    confidence = "low" if had_digits_removed else "high"
    # Single-token leftovers ≤ 2 chars are not a confident name (could
    # be an initial). Downgrade so the merchant approves manually.
    if len(cleaned.split(" ")) == 1 and len(cleaned) <= 2:
        confidence = "low"

    # If we stripped a source/location/title token but a real name
    # survived ("سامي الزهراني تيك" → "سامي الزهراني"), the row
    # belongs in the ``suspicious_suffix`` bucket — that's exactly
    # the case the merchant searches for when they want to mass-fix
    # "X تيك" → "X". Stopword-only edits keep the generic ``other``
    # bucket so they don't crowd the filter chip.
    if had_source_removed or had_location_removed:
        category = CATEGORY_SUSPICIOUS_SUFFIX
    elif had_title_removed:
        # Title-only edits ("د. سامي" → "سامي") are also suspicious-
        # suffix material — same UX bucket.
        category = CATEGORY_SUSPICIOUS_SUFFIX
    elif had_stopword_removed:
        category = CATEGORY_OTHER
    else:
        category = CATEGORY_OTHER

    return CleanResult(
        old=original, suggested=cleaned, reason=reason,
        confidence=confidence, changed=True,
        category=category,
    )


__all__ = [
    "CleanResult", "compute_cleanup",
    "CATEGORY_SOURCE", "CATEGORY_LOCATION", "CATEGORY_PLACEHOLDER",
    "CATEGORY_GENERIC_BAD", "CATEGORY_SUSPICIOUS_SUFFIX",
    "CATEGORY_OTHER", "CATEGORY_NONE", "ALL_CATEGORIES",
]
