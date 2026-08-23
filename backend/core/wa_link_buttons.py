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

``split_text_for_cta_buttons(text, *, store_domain=None)`` →
    ``list[CtaMessage]`` — split a reply into an ordered sequence of
    WhatsApp messages where every URL gets its OWN CTA button.
    Non-URL paragraphs are returned as plain-text messages (cta=None).
    This is the multi-URL fix for the production bug where the bot
    dumped two product links in the same reply — WhatsApp can only
    lift ONE into a CTA, so the second was rendered as a flat
    non-clickable URL string.

Hard rules
----------
* WhatsApp ``cta_url`` interactive only allows ONE URL button per
  message. ``extract_first_cta_url`` keeps the legacy single-CTA
  shape for callers that don't yet want the split behaviour;
  ``split_text_for_cta_buttons`` is the new contract for any reply
  that might carry multiple product URLs.
* If the body becomes empty after stripping the URL we substitute a
  short context line so WhatsApp doesn't reject the interactive
  payload (``body.text`` is required).
* Button titles are clamped to 20 characters (WhatsApp hard limit).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


def strip_empty_markdown_links(text: str) -> str:
    """Remove empty Markdown link artifacts such as ``[موقع المعرض]()``."""
    body = _EMPTY_MD_LINK_RE.sub("", text or "")
    body = re.sub(r"[ \t]+\n", "\n", body)
    return re.sub(r"\n{3,}", "\n\n", body).strip()


def customer_requested_textual_url(message: str) -> bool:
    """True when the customer asked to copy/see the URL as text."""
    return bool(_TEXTUAL_URL_REQUEST_RE.search(str(message or "")))


def prepare_cta_body_text(
    body: str,
    url: str = "",
    *,
    keep_textual_url: bool = False,
) -> str:
    """Keep one structured CTA as the URL owner unless copy-link was requested."""
    cleaned = strip_empty_markdown_links(body or "")
    if keep_textual_url:
        return cleaned
    extracted = extract_first_cta_url(cleaned)
    if extracted is not None:
        cleaned = str(extracted.cleaned_text or "").strip()
    elif url:
        cleaned = cleaned.replace(url, "").strip()
        cleaned = strip_empty_markdown_links(cleaned)
    return cleaned


# Purchase-channel selector: the Online Store reply button owns the
# canonical storefront URL. When that structured action is present, the
# same URL must not also appear raw in the WhatsApp body.
PURCHASE_CHANNEL_SELECTION_TOPIC = "purchase_channel_selection"
ONLINE_STORE_BUTTON_IDS = frozenset({"checkout_store_link", "online_store"})
_BULLET_ONLY_LINE_RE = re.compile(r"^(?:[-*•–—]|\d+[.)])\s*$")


def _is_purchase_channel_selection_turn(*, topic: str = "", owner: str = "") -> bool:
    return (
        str(topic or "").strip() == PURCHASE_CHANNEL_SELECTION_TOPIC
        or str(owner or "").strip() == PURCHASE_CHANNEL_SELECTION_TOPIC
    )


def _copy_reply_button(button: Any) -> Dict[str, Any]:
    if not isinstance(button, dict):
        return {}
    copied = dict(button)
    reply = button.get("reply")
    if isinstance(reply, dict):
        copied["reply"] = dict(reply)
    return copied


def _button_reply_id(button: Any) -> str:
    if not isinstance(button, dict):
        return ""
    reply = button.get("reply") if isinstance(button.get("reply"), dict) else {}
    return str(button.get("id") or reply.get("id") or "").strip()


def _button_destination_url(button: Any) -> str:
    if not isinstance(button, dict):
        return ""
    reply = button.get("reply") if isinstance(button.get("reply"), dict) else {}
    return str(
        button.get("url")
        or button.get("destination_url")
        or reply.get("url")
        or ""
    ).strip()


def _canonical_store_url_key(url: str) -> str:
    """Host + path identity for the merchant storefront URL. Not a generic strip."""
    raw = (url or "").strip().rstrip(".,:;!?)").rstrip("،")
    if not raw:
        return ""
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if not host:
        return ""
    path = (parsed.path or "").rstrip("/")
    return f"{host}{path}"


def _urls_are_same_storefront(left: str, right: str) -> bool:
    a = _canonical_store_url_key(left)
    b = _canonical_store_url_key(right)
    return bool(a and a == b)


def _elide_exact_storefront_url_tokens(text: str, destination: str) -> str:
    """Remove complete URL tokens whose storefront identity matches destination.

    Does not treat the canonical host as a prefix of a longer product/payment
    path on the same host.
    """
    if not text or not destination:
        return text or ""
    spans: list[tuple[int, int]] = []
    for match in _URL_RE.finditer(text):
        raw_url = match.group(0).rstrip(".,:;!?)\u061B\u061F،")
        if not raw_url or not _urls_are_same_storefront(raw_url, destination):
            continue
        spans.append((match.start(), match.start() + len(raw_url)))
    if not spans:
        return text
    parts: list[str] = []
    last = 0
    for start, end in spans:
        parts.append(text[last:start])
        last = end
    parts.append(text[last:])
    return "".join(parts)


def _tidy_body_after_store_url_elision(text: str) -> str:
    body = strip_empty_markdown_links(text or "")
    lines: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        if _BULLET_ONLY_LINE_RE.match(stripped):
            continue
        stripped = re.sub(r"[\s\u00A0]*[:،,;\-—–]+[\s\u00A0]*$", "", stripped)
        if not stripped or _BULLET_ONLY_LINE_RE.match(stripped):
            continue
        lines.append(stripped)
    out = "\n".join(lines).strip()
    out = re.sub(r"\n{3,}", "\n\n", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    return out


def whatsapp_reply_buttons_payload(buttons: Sequence[Any]) -> list[Dict[str, Any]]:
    """WhatsApp Cloud API reply buttons: ``type`` + ``reply.{id,title}`` only."""
    out: list[Dict[str, Any]] = []
    for button in list(buttons or [])[:3]:
        if not isinstance(button, dict):
            continue
        reply = button.get("reply") if isinstance(button.get("reply"), dict) else {}
        out.append(
            {
                "type": str(button.get("type") or "reply"),
                "reply": {
                    "id": str(reply.get("id") or ""),
                    "title": str(reply.get("title") or ""),
                },
            }
        )
    return out


def prepare_purchase_channel_selector_presentation(
    *,
    body: str,
    buttons: Sequence[Any],
    topic: str = "",
    owner: str = "",
    canonical_store_url: str = "",
) -> tuple[str, list[Dict[str, Any]]]:
    """Presentation/wire: drop a duplicate canonical store URL from the body.

    Gates (all required):
      * topic/owner is ``purchase_channel_selection``
      * an Online Store interactive button is present
      * that button's destination equals the tenant canonical ``store_url``

    Only that matching storefront URL is elided. Other URLs stay. Button
    ids/titles are unchanged; the store button keeps ``url`` set to the
    canonical store URL for the action payload.
    """
    copied = [_copy_reply_button(b) for b in list(buttons or []) if isinstance(b, dict)]
    original = body or ""
    if not _is_purchase_channel_selection_turn(topic=topic, owner=owner):
        return original, copied

    canonical = str(canonical_store_url or "").strip()
    store_indexes = [
        i for i, button in enumerate(copied)
        if _button_reply_id(button) in ONLINE_STORE_BUTTON_IDS
    ]
    if not store_indexes:
        return original, copied

    if canonical:
        for idx in store_indexes:
            if not _button_destination_url(copied[idx]):
                copied[idx]["url"] = canonical

    destination = _button_destination_url(copied[store_indexes[0]])
    if not destination or not canonical:
        return original, copied
    if not _urls_are_same_storefront(destination, canonical):
        return original, copied

    if not original.strip():
        return original, copied

    cleaned = _elide_exact_storefront_url_tokens(original, destination)
    cleaned = _tidy_body_after_store_url_elision(cleaned)
    if not cleaned:
        return original, copied
    return cleaned, copied

# Greedy enough for WhatsApp links but stops at whitespace / common
# punctuation so we don't swallow trailing colons or RTL marks.
_URL_RE = re.compile(
    r"https?://[^\s\u0600-\u06FF\u061B\u061F<>\"]+",
    re.IGNORECASE,
)
_EMPTY_MD_LINK_RE = re.compile(r"\[[^\]]+\]\(\s*\)")
_TEXTUAL_URL_REQUEST_RE = re.compile(
    r"(?:انسخ|نسخ|كنص|copy(?:\s+(?:the\s+)?)?(?:link|url)|as\s+text)",
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
    # ``store`` is reserved for store homepages (e.g. "https://shop.com/"
    # or "https://x.salla.sa") — the FAQ store_info template ships
    # ONLY the bare URL so this title is what the customer sees on
    # the WhatsApp CTA button after the wire-layer normaliser lifts it.
    "store":    "افتح المتجر",
    "general":  "فتح الرابط",
}

# Known storefront platforms — used to detect "this is a store homepage"
# when the URL has no product / payment / tracking path. Order doesn't
# matter; we only need ONE match to flip to the ``store`` classification.
_STOREFRONT_HOST_HINTS = (
    "salla.sa", "salla.com", "zid.sa", "zid.store",
    "shopify.com", "myshopify.com",
)


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
    elif _looks_like_store_home(url, store_domain):
        # Store homepage (no product path, hostname matches the merchant
        # domain or a known storefront platform). Lifts into a clean
        # "افتح المتجر" CTA button instead of the generic "فتح الرابط".
        kind = "store"

    title = _truncate_title(_DEFAULT_TITLES.get(kind, "فتح الرابط"))
    return UrlClassification(kind=kind, button_title=title, url=url, domain=domain)


def _looks_like_store_home(url: str, store_domain: Optional[str]) -> bool:
    """Heuristic for "this URL is the store homepage, not a deep link".

    Trigger when ANY of the following hold:
      * the path is empty or just ``/`` (true homepage), and
      * the hostname matches either the merchant-configured store domain
        or a known storefront platform (Salla / Zid / Shopify).

    Deep links (``/products/...``, ``/checkout``, ``/track``) are already
    handled earlier in ``classify_url`` and never reach this helper.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").strip().rstrip("/")
    if path and path not in ("", "/"):
        return False
    if not host:
        return False
    if store_domain:
        sd = store_domain.lower().lstrip(".")
        if host == sd or host.endswith("." + sd):
            return True
    return any(h in host for h in _STOREFRONT_HOST_HINTS)


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


# Default body text emitted when a URL's surrounding label is empty
# after stripping. Tuned per CTA kind so the customer sees a coherent
# one-liner above each WhatsApp button. Used by BOTH the legacy
# ``extract_first_cta_url`` helper and the new multi-URL splitter.
_DEFAULT_BODY_BY_KIND: dict[UrlKind, str] = {
    "product":  "تفاصيل المنتج 👇",
    "payment":  "إتمام الدفع من هنا 👇",
    "tracking": "تتبّع طلبك 👇",
    "location": "موقع المتجر 👇",
    "store":    "هذا متجرنا 🌷",
}


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
    text = strip_empty_markdown_links(text)
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
        cleaned = _DEFAULT_BODY_BY_KIND.get(
            classification.kind, "اضغط على الزر للمتابعة 👇"
        )
    return CtaExtraction(cleaned_text=cleaned, classification=classification)


# ── Multi-URL splitter ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class CtaMessage:
    """One element in the ordered output of ``split_text_for_cta_buttons``.

    * ``body`` — the text WhatsApp will render in the message body.
                 Always non-empty (we substitute a per-kind default
                 when the surrounding label is missing).
    * ``cta``  — the URL classification to lift into a ``cta_url``
                 interactive button. ``None`` means "send as plain
                 text" (used for the intro paragraph and any trailing
                 follow-up question after the last URL).
    """
    body: str
    cta: Optional[UrlClassification]


def _strip_url_from_line(line: str, url: str) -> str:
    """Tighter version of ``_strip_url_from_text`` for a single line.

    Operates on a one-line input (the line that physically contains
    the URL) and returns the surrounding text with the URL excised
    plus dangling colons / hyphens cleaned up. Empty string when the
    line was URL-only.
    """
    if not line or not url:
        return (line or "").strip()
    stripped = line.replace(url, "").strip()
    # Trim trailing label punctuation the LLM uses ("سمر الحجاز:" →
    # "سمر الحجاز"). Keep it lightweight; the heavy cleaner is
    # ``_strip_url_from_text`` for multi-line bodies.
    stripped = re.sub(r"[\s\u00A0]*[:\u061B\u061F،,;\-—–]+[\s\u00A0]*$", "", stripped)
    return stripped


def split_text_for_cta_buttons(
    text: str,
    *,
    store_domain: Optional[str] = None,
) -> list[CtaMessage]:
    """Split *text* into an ordered list of WhatsApp messages where
    every URL gets its own CTA button.

    Algorithm (deliberately conservative — when in doubt we keep the
    original single-message shape):

      1. **No URL** → one ``CtaMessage(body=text, cta=None)``.
      2. **Single URL** → defer to ``extract_first_cta_url``. Output
         is one ``CtaMessage`` carrying the cleaned body + CTA. This
         path is byte-identical to the legacy webhook flow so
         single-product replies are not perturbed by this change.
      3. **Multiple URLs** → walk paragraphs (blank-line separated):
         each paragraph that contains exactly one URL becomes one CTA
         message; paragraphs with no URL become plain-text messages;
         paragraphs with multiple URLs are further split per-line.
         For each URL we attribute the surrounding label (the line
         the URL sits on, plus any non-URL lines above it within the
         same paragraph) as the message body.

    Per the merchant UX spec: ONE product = ONE message = ONE CTA.
    """
    if not text or not text.strip():
        return [CtaMessage(body=(text or "").strip(), cta=None)]

    all_urls = list(_URL_RE.finditer(text))
    if not all_urls:
        return [CtaMessage(body=text.strip(), cta=None)]

    if len(all_urls) == 1:
        # Preserve byte-identical behaviour with the legacy single-CTA
        # path so the existing webhook flow + tests don't move.
        ext = extract_first_cta_url(text, store_domain=store_domain)
        if ext is None:
            return [CtaMessage(body=text.strip(), cta=None)]
        return [CtaMessage(body=ext.cleaned_text, cta=ext.classification)]

    # Multi-URL: split into paragraphs (blank-line separated), then
    # further split paragraphs that pack >1 URL on consecutive lines.
    messages: list[CtaMessage] = []
    paragraphs = re.split(r"\n\s*\n+", text.strip())
    for paragraph in paragraphs:
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        urls_in_para = list(_URL_RE.finditer(paragraph))
        if not urls_in_para:
            messages.append(CtaMessage(body=paragraph, cta=None))
            continue
        if len(urls_in_para) == 1:
            ext = extract_first_cta_url(paragraph, store_domain=store_domain)
            if ext is None:
                messages.append(CtaMessage(body=paragraph, cta=None))
            else:
                messages.append(
                    CtaMessage(body=ext.cleaned_text, cta=ext.classification)
                )
            continue

        # Multiple URLs in one paragraph. We attribute each URL to
        # the line it sits on, prepending any non-URL lines that
        # appeared just above it within the same paragraph (the LLM's
        # label like "سمر الحجاز:").
        lines = paragraph.split("\n")
        pending_label_parts: list[str] = []
        for line in lines:
            line_urls = list(_URL_RE.finditer(line))
            if not line_urls:
                # Non-URL line — accumulate as label for the next URL.
                stripped = line.strip()
                if stripped:
                    pending_label_parts.append(stripped)
                continue
            # Process every URL on this line. Most lines have at most
            # one URL but the LLM can also write "name1 URL1 name2 URL2"
            # — in that case we use the line as-is for the FIRST URL
            # and emit default-bodies for the rest so each still gets
            # its own CTA.
            first_url_raw = line_urls[0].group(0).rstrip(".,:;!?)\u061B\u061F،")
            first_cls = classify_url(first_url_raw, store_domain=store_domain)
            first_body_label = _strip_url_from_line(line, first_url_raw)
            label_parts = [p for p in pending_label_parts if p]
            if first_body_label:
                label_parts.append(first_body_label)
            body = (
                "\n".join(label_parts).strip()
                or _DEFAULT_BODY_BY_KIND.get(first_cls.kind, "اضغط على الزر للمتابعة 👇")
            )
            messages.append(CtaMessage(body=body, cta=first_cls))
            pending_label_parts = []
            for extra_match in line_urls[1:]:
                extra_url_raw = extra_match.group(0).rstrip(".,:;!?)\u061B\u061F،")
                extra_cls = classify_url(extra_url_raw, store_domain=store_domain)
                messages.append(CtaMessage(
                    body=_DEFAULT_BODY_BY_KIND.get(
                        extra_cls.kind, "اضغط على الزر للمتابعة 👇"
                    ),
                    cta=extra_cls,
                ))
        # Any trailing label-only lines without a URL after the last
        # URL in this paragraph → emit as plain-text follow-up.
        if pending_label_parts:
            trailing = "\n".join(pending_label_parts).strip()
            if trailing:
                messages.append(CtaMessage(body=trailing, cta=None))

    # Final safety: if every paragraph somehow collapsed to nothing
    # (shouldn't happen but defend in depth), at least send the
    # original text so the customer isn't left silent.
    if not messages:
        return [CtaMessage(body=text.strip(), cta=None)]
    return messages


__all__ = [
    "UrlClassification",
    "CtaExtraction",
    "CtaMessage",
    "ONLINE_STORE_BUTTON_IDS",
    "PURCHASE_CHANNEL_SELECTION_TOPIC",
    "classify_url",
    "extract_first_cta_url",
    "prepare_cta_body_text",
    "prepare_purchase_channel_selector_presentation",
    "split_text_for_cta_buttons",
    "whatsapp_reply_buttons_payload",
]
