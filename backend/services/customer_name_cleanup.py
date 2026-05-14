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
    # NOTE (May 2026): Gregorian month names used to live here so a
    # row like ``"أيمن نوفمبر"`` would auto-strip ``نوفمبر``. They've
    # been MOVED to ``_MONTH_TOKENS_AR`` (further down) so the cleanup
    # loop can distinguish "merchant typed a date stamp suffix" from
    # "merchant typed an unrelated stopword" — the former now lands
    # in ``suspicious_suffix`` at LOW confidence (manual review),
    # because the same suffix arrives in compound import codes
    # (``"نوفمبر26"``, ``"اكتوبر_27"``) that should be CLEARED rather
    # than reduced to the leading first name.
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
    "test",     "tests", "testing", "tester",
    "demo",     "sample",
    "n/a",      "na",   "none", "null",
})


# Single-word Arabic placeholders (single-token equivalent of the
# multi-word ``_PLACEHOLDER_LITERALS`` further down). Treated like
# stop-tokens: when the entire name reduces to just these, the row
# is cleared. Compared against the normalised form.
_STOP_TOKENS_AR_EXTRA = frozenset({
    "محتمل",        # placeholder used by some CSV imports
    "مجهول",        # "unknown" in Arabic
    "تجريبي",       # "test" — also exists in _STOP_TOKENS_AR
    "اختبار",       # "test"
})


# ── Month tokens (May 2026) ──────────────────────────────────────────────────
# Date-stamp suffixes are wildly common in imported CSV rows that
# came from CRM exports / ad-campaign segmentation. We track them in
# a DEDICATED set (not in _STOP_TOKENS_AR) so the cleanup loop can:
#
#   * Detect single-token month tokens that are alone or paired with
#     a digit suffix (``"نوفمبر26"``, ``"اكتوبر 27"``) → clear.
#   * When a real first name precedes the month (``"أيمن نوفمبر"``,
#     ``"خالد March24"``) → propose stripping just the month, but
#     route to ``suspicious_suffix`` at LOW confidence so the merchant
#     reviews each row. The suffix MIGHT have been a campaign tag the
#     merchant wants to preserve (or the row might be a true junk
#     import the merchant prefers to clear entirely).
#
# Hijri months ``رمضان`` and ``شعبان`` are STILL legitimate Saudi
# personal names — we deliberately omit them from the bare-token set.
# They are matched only inside the compound-code regex below (where
# the digit suffix disambiguates: ``"رمضان1447"`` is a campaign tag,
# ``"رمضان"`` is a person).
_MONTH_TOKENS_AR = frozenset({
    "يناير", "فبراير", "مارس",
    "ابريل", "أبريل", "أپريل",
    "مايو",
    "يونيو", "يونيه",  "يوليو", "يوليه",
    "اغسطس", "أغسطس",
    "سبتمبر", "ستمبر",
    "اكتوبر", "أكتوبر",
    "نوفمبر", "نوفمبير",
    "ديسمبر", "ديسمبير",
})

_MONTH_TOKENS_EN = frozenset({
    "jan",  "january",
    "feb",  "february",
    "mar",  "march",
    "apr",  "april",
    "may",
    "jun",  "june",
    "jul",  "july",
    "aug",  "august",
    "sep",  "sept", "september",
    "oct",  "october",
    "nov",  "november",
    "dec",  "december",
})

# Hijri months that should match ONLY in compound-code form (e.g.
# ``"رمضان1447"``) — never as bare tokens. Used by the regex pre-
# check, not by the per-token loop.
_HIJRI_MONTH_STEMS_AR = frozenset({
    "محرم",  "صفر",
    "ربيع",  "جمادي", "جمادى",
    "رجب",   "شعبان",
    "رمضان", "شوال",
    "ذو",    # "ذو القعدة" / "ذو الحجة" — match the stem only
    "القعده", "القعدة",
    "الحجه", "الحجة",
})

# Pre-built alternation pattern strings — assembled once at import
# time and reused by the regex matcher below.
_ALL_MONTHS_AR_RE_PART = "|".join(
    sorted(_MONTH_TOKENS_AR | _HIJRI_MONTH_STEMS_AR, key=len, reverse=True)
)
_ALL_MONTHS_EN_RE_PART = "|".join(
    sorted(_MONTH_TOKENS_EN, key=len, reverse=True)
)

# Compound month-code regex (May 2026)
# ────────────────────────────────────
# Catches the import-code patterns that arrive in CRM exports / ad
# imports. All variants are anchored with ``^…$`` against the
# normalised string, so partial occurrences inside a longer name
# ("نوفمبر26 خالد") will NOT trip these — they survive as a token
# pair and route through the per-token loop instead.
#
# Patterns (case-insensitive, after Arabic normalisation):
#   1. ``<arabic_month><digits>``            "نوفمبر26"
#   2. ``<digits><arabic_month>``            "26نوفمبر"
#   3. ``<arabic_month><sep><digits>``       "نوفمبر_26"  "نوفمبر-26"
#   4. ``<latin_month><digits>``             "Jan2025"  "Mar24"
#   5. ``<digits><latin_month>``             "24Mar"
#   6. ``<latin_month><sep><digits>``        "aug_26"   "sep-27"
#   7. ``<arabic_month><space><digits>``     "نوفمبر 26" (whole string)
#   8. ``<latin_month><space><digits>``      "March 24" (whole string)
_MONTH_CODE_RE = re.compile(
    rf"^(?:"
    rf"(?:{_ALL_MONTHS_AR_RE_PART})[\s_\-./]*\d{{2,4}}"
    rf"|\d{{2,4}}[\s_\-./]*(?:{_ALL_MONTHS_AR_RE_PART})"
    rf"|(?:{_ALL_MONTHS_EN_RE_PART})[\s_\-./]*\d{{2,4}}"
    rf"|\d{{2,4}}[\s_\-./]*(?:{_ALL_MONTHS_EN_RE_PART})"
    rf")$",
    flags=re.IGNORECASE,
)

# Compact ``<month><digits>`` matcher used inside the per-token loop
# to catch ``"oct2025"`` / ``"نوفمبر26"`` when they appear next to a
# real first name (whole-string regex above won't fire because the
# string contains a real name token too).
_MONTH_TOKEN_COMPACT_RE = re.compile(
    rf"^(?:"
    rf"(?:{_ALL_MONTHS_EN_RE_PART})|(?:{_ALL_MONTHS_AR_RE_PART})"
    rf")[\s_\-./]*\d{{2,4}}$",
    flags=re.IGNORECASE,
)


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
    # Search / ads platforms (May 2026)
    "جوجل",
    "google", "googl", "ggl",
    "ads",  "adwords",
    "بنق",  "bing",
    "yahoo", "ياهو",
    # Generic offline-marketing source labels
    "بنك",         # frequent suffix on Saudi imports — "X بنك"
    "حمله",        # normalised حملة
    "قناه",        # normalised قناة
    "موقع",
    "مصدر",
    "اعلان",       # normalised إعلان
    "شركه",        # normalised شركة (when used as a marketing label)
})


# ── Location tokens (May 2026 — disambiguated) ───────────────────────────────
# False-positive risk
# ────────────────────
# Many Saudi cities collide with COMMON personal names when the
# definite article is stripped:
#
#   ``الرياض``  → city, suspicious
#   ``رياض``    → personal name (e.g. "محمد رياض" / "رياض أحمد")
#
#   ``المكه``   → city
#   ``مكي``     → personal name — different token, NOT in either set
#
#   ``المدينه`` → city
#   ``مدني``    → personal name / nisba — different token, NOT here
#
# Until May 2026 we stripped the leading ``ال`` before matching, which
# caused ``"محمد رياض"`` to be flagged as containing a city. We now
# keep TWO disjoint sets and switch off the definite-article strip
# for location matching:
#
#   ``_LOCATION_TOKENS_STRICT``      → city / region forms whose
#                                       *bare* (no-ال) form is NEVER
#                                       a personal name. Match the
#                                       token verbatim after Arabic
#                                       normalisation — no ``ال``-strip.
#                                       Includes country-tier markers.
#   ``_LOCATION_TOKENS_DEFINITE``    → city forms that REQUIRE the
#                                       definite article (or other
#                                       unambiguous prefix) before
#                                       counting as a location. The
#                                       bare form would shadow a real
#                                       Saudi name and must not match.
#
# All entries are stored in the normalised form produced by
# ``_normalise_arabic`` (alef variants → ا, ى → ي, ة → ه, diacritics
# stripped). Matching is exact set membership against the normalised
# token — no substring / contains check.
_LOCATION_TOKENS_DEFINITE = frozenset({
    # Cities whose bare form is a real Saudi given name → match
    # only when the ``ال`` is present.
    "الرياض",                  # "رياض"  is a male name
    "الجده", "جده",            # normalised "جدة" — "جودة"/"جود" stay clean
    "المكه",                   # "مكه" can be a place; bare keep for safety
    "المدينه",                 # "مدينة"; "مدني" never matches (different token)
    "المنوره",                 # "المنورة"
    "الطائف",                  # bare "طائف" is too generic
    "الدمام",                  # bare "دمام" rarely a name, but keep strict
    "الخبر",                   # bare "خبر" = "news" — keep strict
    "الاحساء",
    "القصيم",                  # bare "قصيم" could be a name
    "الجوف",
    "الباحه",                  # normalised "الباحة"
    "الخرج",
    "ابها", "أبها",
    "بريده", "بريدة",
})

_LOCATION_TOKENS_STRICT = frozenset({
    # Cities/regions whose bare form is unambiguous — never a
    # common personal name.
    "حائل",  "نجران", "تبوك", "جازان",
    "ينبع",  "عرعر",
    "خميس", "مشيط",            # "خميس مشيط" — both halves match
    # Regional / cardinal markers (always preceded by ال in practice).
    "الجنوب", "الشمال",
    "الشرقيه",
    "الغربيه",
    "الوسطى", "الوسطي",
    # Country-tier markers
    "الخارج", "خارج",
    "السعوديه",
    "الامارات",
    "الكويت", "البحرين", "قطر",
    # NOTE: "عمان" omitted — collides with the Saudi male name عمّان/عُمان
    # is rare but "عمان" the country is rarely written this way without
    # context. We skip it to err on the side of keeping real names.
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
    "غير معروف", "غير_معروف", "غير معروفه",
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


def _looks_month_code(raw: str) -> bool:
    """Return True if the entire raw value matches a month-code
    pattern (``"نوفمبر26"`` / ``"Jan2025"`` / ``"aug_26"`` /
    ``"رمضان 1447"``). These are CRM/ad-campaign segmentation tags
    that should NEVER be stored as a customer name.

    Match is regex-anchored against the Arabic-normalised lowercased
    string — partial occurrences inside a longer name ("خالد
    نوفمبر26") will not trip this; the per-token loop handles those
    via ``_MONTH_TOKENS_*`` + suspicious_suffix routing.
    """
    if not raw or not isinstance(raw, str):
        return False
    candidate = _MULTISPACE_RE.sub(" ", raw).strip()
    if not candidate:
        return False
    normalised = _normalise_arabic(candidate)
    if not normalised:
        return False
    return bool(_MONTH_CODE_RE.match(normalised))


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

    # ── Month-code (May 2026) ─────────────────────────────────────
    # ``"نوفمبر26"`` / ``"Jan2025"`` / ``"aug_26"`` / ``"رمضان1447"``
    # patterns are CRM-export / ad-campaign segmentation tags. Match
    # the WHOLE-STRING regex first so we catch the compact compound
    # forms (no space between month and digits) — the per-token loop
    # would otherwise miss them because ``"نوفمبر26"`` is a single
    # token that doesn't equal any stored month token. Names that
    # contain a month alongside a real first name fall through to the
    # token loop (``"خالد نوفمبر"`` → suspicious_suffix, low).
    if _looks_month_code(stripped):
        return CleanResult(
            old=original, suggested=None,
            reason="كود شهري/زمني (تصنيف حملة أو استيراد)",
            confidence="high", changed=True,
            category=CATEGORY_GENERIC_BAD,
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
    had_month_removed    = False    # "نوفمبر" / "March" / Hijri stem

    for token in tokens:
        if not token:
            continue
        # Drop pure-digit tokens (the "238" in "عميل تعديل 238").
        if token.isdigit():
            dropped.append(token)
            had_digits_removed = True
            continue
        # Three normalised forms used by the matchers below:
        #   ``bare``       — token with the leading definite article
        #                     stripped (``العميل`` → ``عميل``). Used
        #                     ONLY for stopword / title / source /
        #                     preposition matching where ``ال`` is
        #                     irrelevant.
        #   ``norm_full``  — full Arabic normalisation WITHOUT
        #                     stripping ``ال``. This is what the
        #                     location matcher uses so ``"الرياض"``
        #                     (city) matches but ``"رياض"`` (the
        #                     personal name) does NOT.
        #   ``norm_bare``  — full normalisation + ``ال`` strip. Used
        #                     for stop / source / title matching where
        #                     the article is noise.
        bare       = _strip_definite_article(token)
        norm_full  = _normalise_arabic(token).lower()
        norm_bare  = _normalise_arabic(bare).lower()
        token_lc   = token.lower()

        if token in _PROTECTED_PREFIXES or bare in _PROTECTED_PREFIXES:
            kept.append(token)
            continue
        if bare in _STOP_TOKENS_AR or norm_bare in _STOP_TOKENS_AR:
            dropped.append(token)
            had_stopword_removed = True
            continue
        if norm_bare in _STOP_TOKENS_AR_EXTRA:
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
        if token_lc in _TITLE_TOKENS or norm_bare in _TITLE_TOKENS:
            dropped.append(token)
            had_title_removed = True
            continue
        if token_lc in _SOURCE_TOKENS or norm_bare in _SOURCE_TOKENS:
            dropped.append(token)
            had_source_removed = True
            continue
        # ── Location matching (May 2026 — disambiguated) ──────────
        # Strict-form cities (e.g. "تبوك", "خميس", "الجنوب") match
        # against the full normalisation regardless of the article.
        # Definite-required cities (e.g. "الرياض", "المدينه") match
        # ONLY when the article is present in the original token, so
        # bare "رياض" / "مدينة" — both common given names — never
        # trip the matcher. Substring / contains matching is NOT used
        # anywhere: the only criterion is exact set membership of the
        # normalised token.
        if norm_full in _LOCATION_TOKENS_STRICT:
            dropped.append(token)
            had_location_removed = True
            continue
        if norm_full in _LOCATION_TOKENS_DEFINITE:
            dropped.append(token)
            had_location_removed = True
            continue
        if norm_bare in _PREPOSITION_TOKENS or bare in _PREPOSITION_TOKENS:
            dropped.append(token)
            had_prep_removed = True
            continue

        # ── Month tokens (May 2026) ───────────────────────────────
        # Standalone Gregorian month tokens (Arabic + Latin). Hijri
        # month *stems* are matched ONLY in compound-code form (handled
        # by the whole-string regex pre-check above), never here — we
        # don't want ``"رمضان"`` / ``"شعبان"`` (legitimate personal
        # names) auto-stripped. The dedicated ``had_month_removed``
        # flag routes the verdict into ``suspicious_suffix`` at LOW
        # confidence when a real first name survives.
        if (
            norm_bare in _MONTH_TOKENS_AR
            or token_lc in _MONTH_TOKENS_EN
            or bare.lower() in _MONTH_TOKENS_EN
        ):
            dropped.append(token)
            had_month_removed = True
            continue

        # ── Latin-alpha + digit-suffix compound token (May 2026) ──
        # Single tokens like ``"campaign27"`` / ``"order2024"`` /
        # ``"oct2025"`` that survive splitting because there's no
        # whitespace separator. We only strip them when the alpha
        # prefix is a KNOWN month (catching ``"oct2025"``) — generic
        # alpha+digit tokens (``"campaign27"``) stay as-is to avoid
        # mangling product SKUs / business names. The whole-string
        # regex above already catches the case where the entire name
        # IS a month-code; this branch handles month-code tokens
        # SIDE-BY-SIDE with a real first name (``"خالد oct2025"``).
        if _MONTH_TOKEN_COMPACT_RE.match(norm_bare):
            dropped.append(token)
            had_month_removed = True
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
        # Month tokens — when the entire name reduces to month bits
        # (``"نوفمبر 26"`` after the token split, or just ``"March"``
        # alone) we mark it as a campaign-tag style bad name. The
        # compact-token case (``"نوفمبر26"``) is already handled by
        # the whole-string regex which bypasses this path entirely.
        if had_month_removed:
            return CATEGORY_GENERIC_BAD
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
        elif category == CATEGORY_GENERIC_BAD and had_month_removed:
            reason = "كود شهري/زمني (تصنيف حملة أو استيراد)"
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
        # Suspicious-suffix edits ALWAYS require manual review (May
        # 2026 policy). A name like ``"Google Ads خالد"`` would
        # otherwise auto-strip "Google" / "Ads" at high confidence
        # and silently store "خالد" — but the merchant might prefer
        # to clear the row entirely, or to keep the source marker
        # if it's actually part of a real business name. Forcing
        # ``low`` puts the row in the "needs review" lane so the
        # bulk "Apply high-confidence only" shortcut never touches
        # it.
        confidence = "low"
    elif had_month_removed:
        # ``"خالد نوفمبر"`` / ``"محمد March24"`` — a campaign-tag
        # suffix sits beside what looks like a real first name. Same
        # policy as source / location suffixes: route to the
        # suspicious_suffix bucket at LOW confidence so the merchant
        # decides whether to drop the month, clear the row entirely
        # (true junk import) or keep the row as-is (the suffix is
        # actually meaningful for that merchant).
        category = CATEGORY_SUSPICIOUS_SUFFIX
        confidence = "low"
    elif had_title_removed:
        # Title-only edits ("د. سامي" → "سامي") are also suspicious-
        # suffix material — same UX bucket, same review-required gate.
        category = CATEGORY_SUSPICIOUS_SUFFIX
        confidence = "low"
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
