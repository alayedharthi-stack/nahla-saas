"""URL → WhatsApp CTA-button normaliser for AI replies.

Goal: when the merchant brain (or any other reply pipeline) produces a
text reply that embeds a long URL, lift the most-relevant URL out of
the body and ship it as a single ``cta_url`` interactive button. The
reply body keeps the natural-language pitch, the button keeps the
link — far more professional on WhatsApp than dumping a 200-character
checkout URL inline.

Public API
----------
``classify_url(url, *, store_domain=None)`` →
    ``UrlClassification`` (kind + button_title + url_normalised).

``extract_first_cta_url(text, *, store_domain=None)`` →
    ``Optional[CtaExtraction]`` — the first URL in ``text`` plus the
    cleaned body (URL + dangling colon/hyphen stripped). Returns
    ``None`` when no convertible URL is present, so the caller falls
    back to a plain text send.

Hard rules
----------
* WhatsApp ``cta_url`` interactive only allows ONE URL button. We
  therefore lift only the first URL we deem "important". Subsequent
  URLs stay in the body untouched.
* If the body becomes empty after stripping the URL we substitute a
  short context line so WhatsApp doesn't reject the interactive
  payload (``body.text`` is required).
* Button titles are clamped to 20 characters (WhatsApp hard limit).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Greedy enough for WhatsApp links but stops at whitespace / common
# punctuation so we don't swallow trailing colons or RTL marks.
_URL_RE = re.compile(
    r"https?://[^\s\u0600-\u06FF\u061B\u061F<>\"]+",
    re.IGNORECASE,
)

UrlKind = str  # one of: product | payment | tracking | location | general

# Domain hints
_PAYMENT_HOST_HINTS = (
    "tap.company", "tap.payments", "checkout.tap.company",
    "paytabs.", "hyperpay.", "mpgs.", "moyasar.", "myfatoorah.",
    "stripe.com", "checkout.stripe.com", "pay.salla.sa",
    "paddle.com", "pay.zid", "pay.zidsa.co",
)
_TRACKING_HOST_HINTS = (
    "aramex.", "smsa.", "spl.com.sa", "j-t.com", "jt-express",
    "fastlo.", "shippa.", "imile.", "barq.", "tabadul.",
    "fedex.", "dhl.", "ups.com", "track.shipa",
)
_LOCATION_HOST_HINTS = (
    "maps.google.", "goo.gl/maps", "maps.app.goo.gl",
    "google.com/maps", "g.co/kgs", "waze.com",
)

_PRODUCT_PATH_HINTS = ("/products/", "/product/", "/p/", "/item/")
_PAYMENT_PATH_HINTS = ("/checkout", "/pay", "/payment", "/cart/checkout")
_TRACKING_PATH_HINTS = ("/track", "/tracking", "/shipment")

# Default Arabic button titles (≤ 20 chars to satisfy WhatsApp).
_DEFAULT_TITLES: dict[UrlKind, str] = {
    "product":  "عرض المنتج",
    "payment":  "إتمام الدفع",
    "tracking": "تتبع الطلب",
    "location": "موقع المتجر",
    "general":  "فتح الرابط",
}


@dataclass(frozen=True)
class UrlClassification:
    kind: UrlKind
    button_title: str
    url: str
    domain: str


@dataclass(frozen=True)
class CtaExtraction:
    cleaned_text: str
    classification: UrlClassification


def _truncate_title(title: str, limit: int = 20) -> str:
    title = (title or "").strip()
    if len(title) <= limit:
        return title or "فتح الرابط"
    return title[: limit - 1].rstrip() + "…"


def _domain_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _looks_like_store_product(url: str, store_domain: Optional[str]) -> bool:
    parsed = urlparse(url)
    path = (parsed.path or "").lower()
    host = (parsed.hostname or "").lower()
    if any(h in path for h in _PRODUCT_PATH_HINTS):
        return True
    if store_domain:
        sd = store_domain.lower().lstrip(".")
        if host.endswith(sd) and any(h in path for h in _PRODUCT_PATH_HINTS):
            return True
    return False


def classify_url(url: str, *, store_domain: Optional[str] = None) -> UrlClassification:
    """Classify *url* into one of the WhatsApp CTA categories.

    The order matters: payment beats product (a product page can also
    contain ``/checkout``), tracking and location are unambiguous.
    """
    url = (url or "").strip().rstrip(".,:;!?)").rstrip("،")
    domain = _domain_of(url)
    path = ""
    try:
        path = (urlparse(url).path or "").lower()
    except Exception:
        path = ""

    kind: UrlKind = "general"
    if any(h in domain for h in _PAYMENT_HOST_HINTS) or any(p in path for p in _PAYMENT_PATH_HINTS):
        kind = "payment"
    elif any(h in domain for h in _LOCATION_HOST_HINTS):
        kind = "location"
    elif any(h in domain for h in _TRACKING_HOST_HINTS) or any(p in path for p in _TRACKING_PATH_HINTS):
        kind = "tracking"
    elif _looks_like_store_product(url, store_domain):
        kind = "product"

    title = _truncate_title(_DEFAULT_TITLES.get(kind, "فتح الرابط"))
    return UrlClassification(kind=kind, button_title=title, url=url, domain=domain)


def _strip_url_from_text(text: str, url: str) -> str:
    """Remove ``url`` from ``text`` and tidy up dangling punctuation.

    We:
      * delete the URL
      * collapse double spaces
      * strip a trailing "حسب التالي:", " — ", " - ", or stray ":" the
        AI may have used to introduce the link
      * trim leading/trailing whitespace on each retained line
    """
    if not text or not url:
        return text or ""

    cleaned = text.replace(url, "")

    # Common Arabic / English connector lines the AI uses to introduce a URL.
    cleaned = re.sub(r"(?:^|\s)(?:هذا\s+الرابط[\s:،-]*|الرابط\s*:?[\s:،-]*|اضغط\s+هنا[\s:،-]*|من\s+هنا[\s:،-]*|here[\s:,-]*|link[\s:,-]*)$",
                     "", cleaned, flags=re.IGNORECASE | re.MULTILINE)

    # Strip lines that became "blank :" / " — " after URL removal.
    lines = []
    for line in cleaned.splitlines():
        stripped = line.strip()
        if not stripped:
            lines.append("")
            continue
        # Trailing punctuation-only after URL removal.
        stripped = re.sub(r"[\s\u00A0]*[:\u061B\u061F،,;\-—–]+[\s\u00A0]*$", "", stripped)
        if stripped:
            lines.append(stripped)
    out = "\n".join(lines).strip()
    # Collapse 3+ blank lines to one.
    out = re.sub(r"\n{3,}", "\n\n", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    return out


def extract_first_cta_url(
    text: str,
    *,
    store_domain: Optional[str] = None,
) -> Optional[CtaExtraction]:
    """Pull the *first* URL out of ``text`` and return cleaned body +
    classification. Returns ``None`` when no URL is found.

    The caller decides whether to actually lift that URL into a CTA
    button (e.g. only when ``classification.kind != "general"`` for
    high-value links, or always for any URL — both policies are
    reasonable; the brain will pick the policy that fits its prompt).
    """
    if not text:
        return None
    match = _URL_RE.search(text)
    if not match:
        return None
    raw_url = match.group(0).rstrip(".,:;!?)\u061B\u061F،")
    if not raw_url:
        return None

    classification = classify_url(raw_url, store_domain=store_domain)
    cleaned = _strip_url_from_text(text, raw_url)
    if not cleaned:
        # Provide a minimal context line so WhatsApp doesn't reject the
        # interactive body. Tailor by kind for a slightly nicer UX.
        cleaned = {
            "product":  "تفاصيل المنتج 👇",
            "payment":  "إتمام الدفع من هنا 👇",
            "tracking": "تتبّع طلبك 👇",
            "location": "موقع المتجر 👇",
        }.get(classification.kind, "اضغط على الزر للمتابعة 👇")
    return CtaExtraction(cleaned_text=cleaned, classification=classification)


__all__ = [
    "UrlClassification",
    "CtaExtraction",
    "classify_url",
    "extract_first_cta_url",
]
