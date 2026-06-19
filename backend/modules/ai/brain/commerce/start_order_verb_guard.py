"""
commerce/start_order_verb_guard.py
──────────────────────────────────
Platform-wide guard: bare order-start phrases must not become product queries.

Phrases like «ابي اطلب» / «ابغى أشتري» express purchase intent only — the
order verb is not a catalog SKU. Without this guard, text-pattern extraction
treats «اطلب» as ``product_query`` and emits catalog no-match copy.
"""
from __future__ import annotations

import re
import unicodedata

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")

# Order verbs / purchase-intent tokens — never catalog product names.
ORDER_VERB_TOKENS = frozenset({
    "اطلب", "اشتري", "طلب", "order", "buy", "purchase",
    "اخذ", "آخذ", "استلم", "شراء",
})

_START_PREFIX_RE = re.compile(
    r"^(?:"
    r"ابغ[ىي]|أبغ[ىي]|اب[ىي]|أب[ىي]|اريد|أريد|بغيت|ودي|"
    r"حاب|حابب|عطني|عطيني|بدي"
    r")\s+",
    re.UNICODE | re.IGNORECASE,
)

_ORDER_VERB_PREFIX_RE = re.compile(
    r"^(?:"
    r"اطلب|أطلب|اشتري|أشتري|آخذ|اخذ|استلم|order|buy|purchase"
    r")"
    r"(?:\s+(?:من(?:كم|ك)?|ل(?:كم|ك)))?"
    r"\s*",
    re.UNICODE | re.IGNORECASE,
)

# Full-message bare start-order shapes (normalized surface).
_BARE_START_ORDER_RE = re.compile(
    r"^(?:"
    r"(?:ابغ[ىي]|أبغ[ىي]|اب[ىي]|أب[ىي]|اريد|أريد|بغيت|ودي|حاب|حابب|بدي|عطني|عطيني)"
    r"\s*(?:اطلب|أطلب|اشتري|أشتري|order|buy)?"
    r"|(?:حاب|حابب)\s+(?:اطلب|أطلب|اشتري|أشتري)"
    r"|(?:اطلب|أطلب|اشتري|أشتري)\s*(?:من(?:كم|ك)?|ل(?:كم|ك))?"
    r")"
    r"\s*[\?؟!\.،,]*$"
    ,
    re.UNICODE | re.IGNORECASE,
)

_FILLER_TOKENS = frozenset({
    "شي", "شيء", "منتج", "بضاعه", "بضاعة", "حاجه", "حاجة", "طلب",
})


def _norm(text: str) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text).lower())
    t = _NORM_RE.sub("", t)
    t = (
        t.replace("\u0623", "\u0627")
        .replace("\u0625", "\u0627")
        .replace("\u0622", "\u0627")
        .replace("\u0649", "\u064a")
        .replace("\u0629", "\u0647")
    )
    return _WS_RE.sub(" ", t).strip()


def is_order_verb_token(token: str) -> bool:
    return _norm(token) in ORDER_VERB_TOKENS


def is_order_verb_only_query(query: str) -> bool:
    """True when every token in *query* is an order verb / purchase filler."""
    tokens = [t for t in _norm(query).split() if t]
    if not tokens:
        return False
    return all(t in ORDER_VERB_TOKENS or t in _FILLER_TOKENS for t in tokens)


def is_bare_start_order_phrase(message: str) -> bool:
    """True when the inbound is only a purchase-intent opener — no product name."""
    raw = (message or "").strip()
    if not raw or len(raw) > 64:
        return False
    norm = _norm(raw)
    if not norm:
        return False
    if _BARE_START_ORDER_RE.match(norm):
        return True
    # «ابي اطلب» with optional trailing punctuation already covered; also
    # accept normalized exact token pairs the regex may miss after NFKC.
    tokens = norm.split()
    if len(tokens) <= 2 and all(
        t in ORDER_VERB_TOKENS
        or t in _FILLER_TOKENS
        or t in {
            "ابي", "ابغى", "اريد", "بغيت", "ودي", "حاب", "حابب", "بدي",
            "عطني", "عطيني", "منكم", "منك", "لكم", "لك",
        }
        for t in tokens
    ):
        return any(t in ORDER_VERB_TOKENS for t in tokens)
    return False


def _has_product_substance(candidate: str) -> bool:
    norm = _norm(candidate)
    if not norm or is_order_verb_only_query(norm):
        return False
    tokens = [t for t in norm.split() if t and t not in _FILLER_TOKENS]
    return any(len(t) >= 2 and t not in ORDER_VERB_TOKENS for t in tokens)


def extract_start_order_product_query(message: str) -> str:
    """
    Product name after an order-start prefix, or ``""`` when none / bare opener.

    Examples:
      «ابي اطلب»           → ``""``
      «ابغى أشتري»         → ``""``
      «ابي اطلب عسل طلح»   → ``عسل طلح``
    """
    raw = (message or "").strip()
    if not raw or is_bare_start_order_phrase(raw):
        return ""

    rest = raw
    stripped = False
    prefix = _START_PREFIX_RE.match(rest)
    if prefix:
        rest = rest[prefix.end():].strip()
        stripped = True

    verb = _ORDER_VERB_PREFIX_RE.match(rest)
    if verb:
        rest = rest[verb.end():].strip()
        stripped = True

    # Only consume text after a recognized order-start prefix/verb — never
    # treat an unrelated full message (e.g. «من أنت») as a product query.
    if not stripped:
        return ""

    rest = rest.strip(" ؟?!.,،")
    if rest and _has_product_substance(rest):
        return rest
    return ""


__all__ = [
    "ORDER_VERB_TOKENS",
    "extract_start_order_product_query",
    "is_bare_start_order_phrase",
    "is_order_verb_only_query",
    "is_order_verb_token",
]
