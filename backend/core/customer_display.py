"""
core/customer_display.py
────────────────────────
Customer-facing display-name helpers.

⚠ IMPORTANT (May 2026 policy change)
────────────────────────────────────
We previously did **runtime sanitisation** at every greeting site —
campaigns, automation templates, the AI prompt builder — stripping
``"عميل"``, ``"customer"``, etc. before the value hit a ``{{1}}``
slot. That created a two-layer cleanup problem:

  * Layer 1 (this file) ran on every send.
  * Layer 2 (the new bulk **"تنظيف أسماء العملاء"** tool on the
    customers page) ran once when the merchant approved it.

Two layers caused conflicts: a name cleaned by Layer 2 could still
end up rewritten by Layer 1 with subtly different rules, and the
behaviour was hard to debug. The decision (May 2026) is:

  **The merchant-controlled bulk tool is the SOLE source of truth.**

  * If ``Customer.name`` is set and is a real name (not phone-shaped),
    callers use it **verbatim**.
  * If it's ``NULL`` / empty / whitespace-only / phone-shaped, callers
    fall back to the static greeting :data:`DEFAULT_FALLBACK_NAME`.
  * Dashboard ``display_name`` may show the phone for row identification;
    that value must **never** be passed into this helper — only
    ``Customer.name`` belongs in campaigns / automations / AI greetings.
  * No runtime mutation at send time — the value the merchant sees
    in the customers page is the value Meta receives.

Tokenised cleanup logic (``sanitize_display_customer_name``) still
lives here, but it is now used **only** by the bulk admin tool
(``backend/services/customer_name_cleanup.py`` re-implements its
own copy with phone-detection and confidence scoring; this module's
sanitiser is retained for any historical caller that still imports
it, but is no longer wired into the dispatcher / templates / AI
prompt). New callers MUST use
:func:`display_name_passthrough_or_fallback`.
"""
from __future__ import annotations

import re
from typing import Optional

# ── Default fallback ──────────────────────────────────────────────────────────
# Warmer than ``"عميلنا العزيز"`` per merchant feedback (May 2026): "الغالي"
# tests better on WhatsApp where the tone is closer to a corner shop than
# a customer-support hotline.
DEFAULT_FALLBACK_NAME = "عميلنا الغالي"


# ── Stopword tokens ───────────────────────────────────────────────────────────
# Commercial / descriptive labels that are *never* human names in our
# corpus. Compared after stripping the Arabic definite article and
# lower-casing the Latin tokens.
_STOP_TOKENS_AR = frozenset({
    "عميل", "عميلة", "عملاء",
    "زبون", "زبونة", "زبائن",
    "ضيف", "ضيفة", "ضيوف",
    "متجر",  # "ضيف المتجر" → both tokens drop → fallback
    # Descriptive qualifiers commonly used as a placeholder
    # name in CSV imports.
    "جديد", "جديدة",
    "مؤقت", "مؤقتة",
    "تجريبي", "تجريبية",
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

# Patronymic + honorific prefixes we MUST preserve. ``أبو``, ``أم``,
# ``عبد``, ``آل``, ``ابن``/``بن``, ``ابنة``/``بنت`` only ever appear as
# part of a compound name; dropping the second token (a real first
# name) would leave only the prefix and break the greeting.
_PROTECTED_PREFIXES = frozenset({
    "أبو", "أبا", "أبي",
    "أم",  "أما", "أمي",
    "عبد",
    "آل",
    "ابن", "ابنة",
    "بن",  "بنت",
})


# ── Cleanup primitives ────────────────────────────────────────────────────────
# Wide pictograph / emoji ranges. Keep this conservative — only ranges
# that are emoji proper. Letters and digits are untouched.
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

# Decorative punctuation that is never part of a human name. We
# deliberately KEEP ``-``, ``'``, and ``.`` because they show up in
# legitimate names (``Al-Sayed``, ``D'Angelo``, ``Mohd.``).
_BAD_PUNCT_RE = re.compile(r"[!@#$%^&*()_=+\[\]{}|\\/<>?\":;,~`«»“”‘’]+")

_MULTISPACE_RE = re.compile(r"\s+")

# Phone-shaped values are valid dashboard row labels after a manual
# name clear, but must never become a campaign/AI greeting slot.
_PHONE_LIKE_RE = re.compile(r"^[+]?[\d\s\-()]{7,}$")

_INVALID_DISPLAY_NAMES = frozenset({".", "—", "-", "..", "..."})


def is_valid_customer_display_name(raw: Optional[str]) -> bool:
    """Operational display names must not be blank, dot placeholders, or phones."""
    if raw is None or not isinstance(raw, str):
        return False
    text = raw.strip()
    if not text or text in _INVALID_DISPLAY_NAMES:
        return False
    if looks_like_phone_personalization_name(text):
        return False
    return any(ch.isalpha() for ch in text) or len(text.split()) >= 2


def looks_like_phone_personalization_name(raw: Optional[str]) -> bool:
    """True when ``raw`` looks like a phone number, not a human name.

    Used by the personalization layer only — dashboard list rendering
    may still show the phone via ``display_name_for_customer``.
    """
    if raw is None or not isinstance(raw, str):
        return False
    candidate = raw.strip()
    if not candidate:
        return False
    if _PHONE_LIKE_RE.match(candidate):
        digits_only = "".join(ch for ch in candidate if ch.isdigit())
        return len(digits_only) >= 7
    return False


def _strip_definite_article(token: str) -> str:
    """Remove a leading ``ال`` from a token so ``العميل`` is matched
    against the same stopword set as ``عميل``. Returns the original
    token if it would leave fewer than two characters."""
    if len(token) > 3 and token.startswith("ال"):
        return token[2:]
    return token


def _looks_like_real_name(s: str) -> bool:
    """Cheap heuristic: after cleaning, is this plausibly a name we
    can put in a greeting? Rejects empty strings, single characters,
    and strings that contain no letters at all (pure digits / pure
    punctuation residue)."""
    if not s:
        return False
    if len(s) < 2:
        return False
    return any(ch.isalpha() for ch in s)


# ── Public API ────────────────────────────────────────────────────────────────


def sanitize_display_customer_name(raw: Optional[str]) -> Optional[str]:
    """Return a polished, display-safe version of ``raw`` or ``None``.

    ``None`` means "we could not extract a confident human name from
    this value — caller should use a fallback greeting instead". This
    is deliberately three-state (good / bad / use-fallback) rather
    than always returning a string so a caller that *can* render a
    different sentence (e.g. omit the greeting line entirely) has
    the option.

    The cleaning pipeline is:

    1. strip emojis + decorative punctuation;
    2. collapse whitespace;
    3. split on whitespace and drop tokens that match the
       commercial-token stoplist (Arabic + English, case- and
       ``ال``-prefix-insensitive);
    4. keep all patronymic/honorific prefixes intact even when
       a literal stopword (``العميل``) sits in front of them;
    5. reject the result if it has no letters or is shorter than
       two characters.

    See module docstring for the supported examples.
    """
    if raw is None or not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None

    s = _EMOJI_RE.sub(" ", s)
    s = _BAD_PUNCT_RE.sub(" ", s)
    s = _MULTISPACE_RE.sub(" ", s).strip()
    if not s:
        return None

    kept: list[str] = []
    for token in s.split(" "):
        if not token:
            continue
        bare = _strip_definite_article(token)
        # Protected prefix wins over everything else — even if the
        # bare form somehow matched a stoplist entry. There is no
        # overlap today, but the rule keeps future stoplist edits
        # safe.
        if token in _PROTECTED_PREFIXES or bare in _PROTECTED_PREFIXES:
            kept.append(token)
            continue
        if bare in _STOP_TOKENS_AR:
            continue
        if token.lower() in _STOP_TOKENS_EN or bare.lower() in _STOP_TOKENS_EN:
            continue
        # Drop single-character tokens that survive — they are
        # almost always punctuation residue (``"n/a"`` → ``"n a"``
        # after slash-stripping, and neither ``n`` nor ``a`` is a
        # real name worth greeting by). Real one-letter "initials"
        # belong on a passport form, not a marketing template.
        if len(token) == 1:
            continue
        kept.append(token)

    cleaned = _MULTISPACE_RE.sub(" ", " ".join(kept)).strip()
    if not _looks_like_real_name(cleaned):
        return None
    return cleaned


def display_name_passthrough_or_fallback(
    raw: Optional[str],
    fallback: str = DEFAULT_FALLBACK_NAME,
) -> str:
    """Greeting-ready version of ``Customer.name`` — **no sanitisation**.

    This is the runtime personalization helper for campaigns,
    automations, and AI greetings. Its job is to decide between
    "use the stored official name" and "fall back to the static
    greeting"; it does NOT strip stopwords or read dashboard
    ``display_name``.

    Rules:
      * ``None`` / non-string         → fallback.
      * Empty / whitespace-only       → fallback.
      * Phone-shaped (``+966…``, digits-only, etc.) → fallback.
      * Anything else                 → ``raw.strip()`` verbatim.

    Cleanup of badly-imported names is done **once**, up front,
    via the bulk admin tool on the customers page.
    """
    if raw is None or not isinstance(raw, str):
        return fallback
    cleaned = raw.strip()
    if not cleaned or looks_like_phone_personalization_name(cleaned):
        return fallback
    return cleaned


def personalization_customer_name_or_fallback(
    raw: Optional[str],
    fallback: str = DEFAULT_FALLBACK_NAME,
) -> str:
    """Explicit alias for campaign/template/AI personalization slots.

    Callers must pass ``Customer.name`` only — never dashboard
    ``display_name``.
    """
    return display_name_passthrough_or_fallback(raw, fallback=fallback)


# ── Deprecated aliases (kept for back-compat with older imports) ─────
#
# The bulk admin tool replaced runtime sanitisation. These names now
# point at the passthrough helper so any caller that still imports
# the old symbol gets the new behaviour (use the stored name verbatim,
# fall back to the static greeting on empty). For NEW code, import
# ``display_name_passthrough_or_fallback`` directly — its name makes
# the contract obvious at the call site.
display_customer_name_or_fallback = display_name_passthrough_or_fallback
display_customer_name = display_name_passthrough_or_fallback
