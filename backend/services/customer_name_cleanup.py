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
    """
    old:        Optional[str]
    suggested:  Optional[str]
    reason:     str
    confidence: str   # "high" | "low"
    changed:    bool


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
        )
    if not isinstance(raw, str):
        return CleanResult(
            old=str(raw), suggested=None, reason="قيمة غير نصية",
            confidence="high", changed=True,
        )

    original = raw
    stripped = raw.strip()
    if not stripped:
        # Empty/whitespace-only — already effectively cleared. Not
        # surfaced in the preview.
        return CleanResult(
            old=original, suggested=None, reason="",
            confidence="high", changed=False,
        )

    # ── Phone-only → clear ────────────────────────────────────────
    if _looks_phone_only(stripped):
        return CleanResult(
            old=original, suggested=None,
            reason="القيمة رقم جوال وليست اسماً",
            confidence="high", changed=True,
        )

    # ── Religious / promotional phrase → clear ────────────────────
    if _looks_nonhuman_phrase(stripped):
        return CleanResult(
            old=original, suggested=None,
            reason="عبارة غير اسمية",
            confidence="low", changed=True,
        )

    # ── Heavy-digit ratio (e.g. "عميل 238") → clear ───────────────
    if _digit_ratio(stripped) >= _DIGIT_RATIO_THRESHOLD:
        return CleanResult(
            old=original, suggested=None,
            reason="نسبة كبيرة من الأرقام داخل الاسم",
            confidence="high", changed=True,
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
        )

    tokens = s.split(" ")
    kept: list[str] = []
    dropped: list[str] = []
    had_digits_removed = False
    had_stopword_removed = False

    for token in tokens:
        if not token:
            continue
        # Drop pure-digit tokens (the "238" in "عميل تعديل 238").
        if token.isdigit():
            dropped.append(token)
            had_digits_removed = True
            continue
        bare = _strip_definite_article(token)
        if token in _PROTECTED_PREFIXES or bare in _PROTECTED_PREFIXES:
            kept.append(token)
            continue
        if bare in _STOP_TOKENS_AR:
            dropped.append(token)
            had_stopword_removed = True
            continue
        if token.lower() in _STOP_TOKENS_EN or bare.lower() in _STOP_TOKENS_EN:
            dropped.append(token)
            had_stopword_removed = True
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
        )

    # ── Decide the final verdict ──────────────────────────────────
    if not cleaned or not _has_letters(cleaned):
        return CleanResult(
            old=original, suggested=None,
            reason=(
                "لا يبقى اسم حقيقي بعد إزالة الكلمات التجارية"
                if dropped else "لا يحتوي حروف"
            ),
            confidence="high", changed=True,
        )

    if cleaned == stripped:
        return CleanResult(
            old=original, suggested=cleaned,
            reason="", confidence="high", changed=False,
        )

    # The change was a stopword-strip — high confidence.
    # If we additionally pulled digits out of the middle, downgrade
    # to "low" because the merchant might want to keep an internal ID.
    reason_bits = []
    if dropped:
        descriptive = [d for d in dropped if not d.isdigit()]
        if descriptive:
            reason_bits.append(
                "إزالة كلمات تجارية: " + ", ".join(sorted(set(descriptive)))
            )
        if had_digits_removed:
            reason_bits.append("إزالة أرقام داخلية")
    reason = " — ".join(reason_bits) or "تنظيف بسيط"

    confidence = "low" if had_digits_removed else "high"
    # Single-token leftovers ≤ 2 chars are not a confident name (could
    # be an initial). Downgrade so the merchant approves manually.
    if len(cleaned.split(" ")) == 1 and len(cleaned) <= 2:
        confidence = "low"

    return CleanResult(
        old=original, suggested=cleaned, reason=reason,
        confidence=confidence, changed=True,
    )


__all__ = ["CleanResult", "compute_cleanup"]
