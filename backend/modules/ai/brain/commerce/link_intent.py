"""
Dedicated resolver for website vs physical location vs product/payment links.

Runs before physical-location FAQ routing so bare «الموقع» phrasing and
«رابط الموقع» never default to Google Maps when the customer meant the
online store.
"""
from __future__ import annotations

import re
from enum import Enum

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_DIA_RE = re.compile(f"[{_DIA}]+")

# ── Website / online store (specific phrases win over bare «موقع») ─────────
_WEBSITE_EXPLICIT_MARKERS: tuple[str, ...] = (
    "الموقع الالكتروني",
    "الموقع الإلكتروني",
    "موقعكم الالكتروني",
    "موقعكم الإلكتروني",
    "موقعك الالكتروني",
    "موقعك الإلكتروني",
    "موقعنا الالكتروني",
    "موقعنا الإلكتروني",
    "المتجر الالكتروني",
    "المتجر الإلكتروني",
    "رابط الموقع",
    "رابط موقعكم",
    "رابط موقعك",
    "رابط موقعنا",
    "رابط المتجر",
    "رابط متجر",
    "رابط متجركم",
    "رابط متجرك",
    "رابط الشراء",
    "رابط الطلب",
    "الويب سايت",
    "ويب سايت",
    "website",
    "store link",
    "store url",
    "website link",
    "online store",
    "shop link",
    "عندكم متجر",
    "عندك متجر",
    "هل عندكم متجر",
)

_WEBSITE_TOKEN_MARKERS: tuple[str, ...] = (
    "الكتروني",
    "إلكتروني",
    "اونلاين",
    "أونلاين",
    "online",
)

_WEBSITE_CONTEXT_MARKERS: tuple[str, ...] = (
    "رابط",
    "لينك",
    "link",
    "موقع",
    "متجر",
    "website",
    "ويب",
)

# ── Physical shop / Google Maps ──────────────────────────────────────────────
_PHYSICAL_EXPLICIT_MARKERS: tuple[str, ...] = (
    "موقع المتجر",
    "موقع المعرض",
    "موقع المحل",
    "موقع الفرع",
    "وين موقعكم",
    "أين موقعكم",
    "وين موقع",
    "وين الموقع",
    "وين المحل",
    "وين المعرض",
    "وين أنتم",
    "وين انتم",
    "وين فرعكم",
    "وين مقركم",
    "ارسل اللوكيشن",
    "أرسل اللوكيشن",
    "ارسلي اللوكيشن",
    "أرسلي اللوكيشن",
    "ابعث اللوكيشن",
    "أبعث اللوكيشن",
    "اللوكيشن",
    "لوكيشن المحل",
    "لوكيشن المتجر",
    "لوكيشن المعرض",
    "لوكيشن الفرع",
    "عنوان المحل",
    "عنوان المعرض",
    "عنوان الفرع",
    "عنوانكم",
    "google maps",
    "google map",
    "خرائط",
    "الخرائط",
    "خرايط",
    "الخرايط",
    "على الخريطة",
    "رابط الخريطة",
    "رابط الخرايط",
    "رابط اللوكيشن",
    "store location",
    "branch location",
    "where is your shop",
    "where is your branch",
)

_PHYSICAL_WHERE_SITE_RE = re.compile(
    r"(?:^|\s)(?:وين|أين|اين)\s+(?:موقع(?:كم|ك|نا)?|انتم|أنتم|فرع|محل|معرض|مقر)\b",
    re.UNICODE | re.IGNORECASE,
)
_PHYSICAL_SITE_NOUN_RE = re.compile(
    r"(?:^|\s)موقع(?:\s+(?:المتجر|المعرض|المحل|الفرع|كم|ك|نا))\b",
    re.UNICODE | re.IGNORECASE,
)
_SEND_LOCATION_REQUEST_RE = re.compile(
    r"(?:^|\s)(?:ارسل|أرسل|ارسلي|أرسلي|ابعث|أبعث|ابعثلي|أبعثلي|ابي|أبي|ابغى|أبغى)"
    r"\s*(?:لي\s+)?(?:ال)?(?:موقع|عنوان|اللوكيشن)(?:ه|ها|كم|ك)?\b",
    re.UNICODE | re.IGNORECASE,
)

# ── Product URL ──────────────────────────────────────────────────────────────
_PRODUCT_URL_MARKERS: tuple[str, ...] = (
    "رابط المنتج",
    "رابط منتج",
    "رابط هذا المنتج",
    "رابط هالمنتج",
    "product link",
    "product url",
)

_SEND_PRODUCT_LINK_RE = re.compile(
    r"(?:^|\s)(?:ارسل|أرسل|ارسلي|أرسلي|ابعث|أبعث|ابعثلي|أبعثلي|ابي|أبي|ابغى|أبغى)"
    r"\s*(?:لي\s+)?(?:ال)?(?:رابط|لينك|link)\s+(?:ال)?(?:منتج|product)\b",
    re.UNICODE | re.IGNORECASE,
)
_SEND_NAMED_PRODUCT_LINK_RE = re.compile(
    r"(?:^|\s)(?:ارسل|أرسل|ارسلي|أرسلي|ابعث|أبعث|ابعثلي|أبعثلي|ابي|أبي|ابغى|أبغى)"
    r"\s*(?:لي\s+)?(?:ال)?(?:رابط|لينك|link)\s+\S+",
    re.UNICODE | re.IGNORECASE,
)

# ── Payment link ───────────────────────────────────────────────────────────────
_PAYMENT_LINK_MARKERS: tuple[str, ...] = (
    "رابط الدفع",
    "رابط دفع",
    "رابط السداد",
    "رابط سداد",
    "checkout link",
    "payment link",
    "أبغى أدفع",
    "ابغى ادفع",
    "ابي ادفع",
    "أبي أدفع",
    "أكمل الدفع",
    "اكمل الدفع",
    "إتمام الدفع",
    "اتمام الدفع",
)


class LinkIntentType(str, Enum):
    WEBSITE_URL = "website_url"
    PHYSICAL_LOCATION = "physical_location"
    PRODUCT_URL = "product_url"
    PAYMENT_LINK = "payment_link"
    UNKNOWN_LINK = "unknown_link"


def _normalise(text: str) -> str:
    if not text:
        return ""
    t = text.strip().lower()
    t = _DIA_RE.sub("", t)
    t = (
        t.replace("\u0623", "\u0627")
        .replace("\u0625", "\u0627")
        .replace("\u0622", "\u0627")
        .replace("\u0649", "\u064a")
    )
    t = re.sub(r"[؟?,،.!:;\-\u060c]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(n in text for n in needles)


def _looks_like_website_url_request(norm: str) -> bool:
    if not norm:
        return False
    if _contains_any(norm, _WEBSITE_EXPLICIT_MARKERS):
        return True
    if _contains_any(norm, _WEBSITE_TOKEN_MARKERS) and _contains_any(
        norm, _WEBSITE_CONTEXT_MARKERS
    ):
        return True
    if norm in {"ويب", "website"}:
        return True
    return False


def _looks_like_physical_location_request(norm: str, raw: str) -> bool:
    if not norm:
        return False
    if _looks_like_website_url_request(norm):
        return False
    if _SEND_LOCATION_REQUEST_RE.search(norm):
        return True
    if _contains_any(norm, _PHYSICAL_EXPLICIT_MARKERS):
        return True
    if _PHYSICAL_WHERE_SITE_RE.search(norm):
        return True
    if _PHYSICAL_SITE_NOUN_RE.search(norm):
        return True
    # City/region suffix after «موقعكم» — physical branch ask.
    if re.search(
        r"(?:^|\s)موقع(?:كم|ك|نا)\s+(?:في|ب)\s+\S+",
        norm,
        re.UNICODE | re.IGNORECASE,
    ):
        return True
    return False


def _looks_like_product_url_request(norm: str) -> bool:
    if not norm:
        return False
    if _contains_any(norm, _PRODUCT_URL_MARKERS):
        return True
    if _SEND_PRODUCT_LINK_RE.search(norm):
        return True
    if _SEND_NAMED_PRODUCT_LINK_RE.search(norm) and "منتج" not in norm:
        # «ارسل رابط الطلح» — product name follows «رابط».
        if not _looks_like_website_url_request(norm) and not _contains_any(
            norm, _PAYMENT_LINK_MARKERS
        ):
            return True
    return False


def _looks_like_payment_link_request(norm: str) -> bool:
    if not norm:
        return False
    return _contains_any(norm, _PAYMENT_LINK_MARKERS)


def is_explicit_direct_location_request(message: str) -> bool:
    """
    True for unambiguous physical-location asks that should send maps directly.

    Skips maps-vs-contact disambiguation (e.g. «موقع المعرض», «وين موقعكم؟»).
    Ambiguous arrival-only phrasing (e.g. «أبي أجيكم») may still disambiguate.
    """
    from .link_intent_media_source_guard import link_intent_message  # noqa: PLC0415

    raw = link_intent_message(message or "")
    norm = _normalise(raw)
    if not norm or not _looks_like_physical_location_request(norm, raw):
        return False
    if _SEND_LOCATION_REQUEST_RE.search(norm):
        return True
    if _PHYSICAL_WHERE_SITE_RE.search(norm):
        return True
    if _PHYSICAL_SITE_NOUN_RE.search(norm):
        return True
    if _contains_any(norm, _PHYSICAL_EXPLICIT_MARKERS):
        return True
    return False


def resolve_inbound_link_intent(message: str) -> LinkIntentType:
    """Classify link intent from customer-authored inbound text only."""
    from .link_intent_media_source_guard import link_intent_message  # noqa: PLC0415

    return resolve_link_intent(link_intent_message(message or ""))


def resolve_link_intent(message: str) -> LinkIntentType:
    """Classify link-related customer messages deterministically."""
    raw = message or ""
    try:
        from core.inbound_url_spans import (  # noqa: PLC0415
            semantic_text_excluding_url_spans,
        )

        raw = semantic_text_excluding_url_spans(raw)
    except Exception:
        raw = message or ""
    norm = _normalise(raw)
    if not norm:
        return LinkIntentType.UNKNOWN_LINK

    if _looks_like_payment_link_request(norm):
        return LinkIntentType.PAYMENT_LINK

    if _looks_like_product_url_request(norm):
        return LinkIntentType.PRODUCT_URL

    if _looks_like_website_url_request(norm):
        return LinkIntentType.WEBSITE_URL

    if _looks_like_physical_location_request(norm, raw):
        return LinkIntentType.PHYSICAL_LOCATION

    return LinkIntentType.UNKNOWN_LINK


def compose_website_url_reply(store_url: str) -> str:
    """Operational store-link reply — URL when configured, honest none otherwise."""
    url = str(store_url or "").strip()
    if url:
        return url
    return (
        "ما عندي رابط المتجر الإلكتروني محفوظ في النظام حالياً."
    )


__all__ = [
    "LinkIntentType",
    "compose_website_url_reply",
    "is_explicit_direct_location_request",
    "resolve_inbound_link_intent",
    "resolve_link_intent",
]
