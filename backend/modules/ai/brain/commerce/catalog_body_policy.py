"""
Catalog message body policy — no fixed marketing intro before native catalog sends.

Meta requires a non-empty interactive body; callers supply contextual copy or
we fall back to a minimal neutral placeholder — never a canned catalog phrase.
"""
from __future__ import annotations

import re

MAX_CATALOG_BODY_LEN = 1024

# Legacy hardcoded intro removed from production paths — kept for detection only.
FORBIDDEN_CATALOG_INTRO_MARKERS = (
    "تفضّل، اختر من الكتالوج",
    "تفضل، اختر من الكتالوج",
    "تفضّل المنتج",
    "تفضل المنتج",
)

_FIXED_POINTER_RE = re.compile(r"👇")


def is_forbidden_catalog_intro(text: str) -> bool:
    body = str(text or "").strip()
    if not body:
        return False
    return any(m in body for m in FORBIDDEN_CATALOG_INTRO_MARKERS)


MINIMAL_CATALOG_BODY = "."
# Meta requires a non-empty interactive body; neutral UI copy (not AI prose).
TECHNICAL_CATALOG_BODY = "اختر المنتجات من القائمة"

_UNSAFE_CATALOG_BODY_MARKERS = (
    "التوفر قيد التحقق",
    "غير متوفر",
    "غير متاح",
    "نفذت الكمية",
    "out of stock",
    "availability",
    "أي نوع تبيه",
    "أي نوع تبي",
    "أي نوع تبغ",
)


def is_minimal_catalog_body(text: str) -> bool:
    body = str(text or "").strip()
    return body in ("", MINIMAL_CATALOG_BODY, TECHNICAL_CATALOG_BODY)


def is_unsafe_catalog_body(text: str) -> bool:
    body = str(text or "").strip()
    if not body:
        return True
    if is_forbidden_catalog_intro(body):
        return True
    try:
        from core.fallback_policy import is_compose_failure_fallback  # noqa: PLC0415

        if is_compose_failure_fallback(body):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional fallback policy import
        pass
    lowered = body.lower()
    return any(m.lower() in lowered for m in _UNSAFE_CATALOG_BODY_MARKERS)


def resolve_catalog_body_text(
    body_text: str = "",
    *,
    context_reply: str = "",
) -> str:
    """Pick catalog interactive body text without injecting fixed marketing copy."""
    for candidate in (body_text, context_reply):
        c = str(candidate or "").strip()
        if not c:
            continue
        if is_unsafe_catalog_body(c):
            continue
        if len(c) > MAX_CATALOG_BODY_LEN:
            return c[: MAX_CATALOG_BODY_LEN - 1] + "…"
        return c
    return TECHNICAL_CATALOG_BODY


def resolve_native_catalog_body_text(
    *,
    context_reply: str = "",
    inbound_customer_message: str = "",
) -> str:
    """Native catalog send body — never echo inbound customer text."""
    inbound = str(inbound_customer_message or "").strip()
    reply = str(context_reply or "").strip()
    if inbound and reply and _norm_body(inbound) == _norm_body(reply):
        reply = ""
    return resolve_catalog_body_text("", context_reply=reply)


def _norm_body(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip())


def has_fixed_catalog_pointer(text: str) -> bool:
    return bool(_FIXED_POINTER_RE.search(str(text or "")))


__all__ = [
    "FORBIDDEN_CATALOG_INTRO_MARKERS",
    "MINIMAL_CATALOG_BODY",
    "TECHNICAL_CATALOG_BODY",
    "has_fixed_catalog_pointer",
    "is_forbidden_catalog_intro",
    "is_minimal_catalog_body",
    "is_unsafe_catalog_body",
    "resolve_catalog_body_text",
    "resolve_native_catalog_body_text",
]
