"""
backend/services/media_key_registry.py
──────────────────────────────────────
The canonical registry of well-known media keys.

Why a registry (and not just free-form strings)?
────────────────────────────────────────────────
The platform contract is: when the LLM emits
``[MEDIA_KEY:payment_rajhi_barcode]``, the resolver must find
*the* row whose ``media_key='payment_rajhi_barcode'`` for the
current tenant. If every merchant invents their own keys
("rajhi", "RajhiQR", "بنك_الراجحي"), the AI can never reliably
emit a marker that resolves.

So we ship a **closed set** of well-known keys with stable
Arabic labels for the UI dropdown. The merchant picks from the
list when uploading; the AI prompt instructs Claude to use only
these exact keys; the resolver matches exactly.

The registry is intentionally small — only assets whose meaning
is universal across stores (payment, QR, usage video,
certificates, location). Anything store-specific stays free-form
and goes through the existing relevance ranker.

Adding a new key
────────────────
1. Pick an ``intent`` family (payment / shipping / store /
   product_meta / legal).
2. Append a :class:`MediaKey` to ``REGISTRY``.
3. The UI auto-picks it up (suggestions are sourced from this
   module via ``GET /intelligence/ai-media/keys``).
4. Drop a hint in the system prompt about what triggers it
   (see ``_TRIGGER_HINTS`` below — surfaced to Claude verbatim).

Do NOT remove keys that have ever shipped to production — even
unused, the column is namespaced per tenant so the registry
serves as the authoritative "what does this key mean?" doc for
support.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


# ──────────────────────────────────────────────────────────────────
# Types
# ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MediaKey:
    """One registered media key.

    Attributes
    ──────────
    key            stable slug — what the column stores and what
                   the LLM emits. snake_case, ASCII only.
    label_ar       what the merchant sees in the UI dropdown.
    description_ar one-line hint for the UI.
    intent         family grouping (payment / shipping / store /
                   product_meta / legal). Used by the UI for
                   visual sectioning + by Claude prompting so
                   "send the bank media" can pick *any* key in
                   the ``payment`` family if exact match fails.
    expected_media_type  what we expect the merchant to upload
                   (image / video / document). The resolver
                   warns if the actual row's ``media_type``
                   diverges — useful for catching merchants who
                   upload a PDF where an image was expected.
    triggers       keyword tokens that suggest this key. Used by
                   ``find_key_for_query`` so a merchant typing
                   "أرسل لي باركود الراجحي" in chat (with no
                   marker emitted) can still get the right
                   asset via a deterministic post-LLM fallback.
                   ALL tokens are lowercased + Arabic
                   diacritics-stripped before comparison.
    fallback_text  what to send when the merchant hasn't
                   uploaded this asset yet. Often just a phone
                   number or a one-line text instruction. The
                   resolver returns this verbatim so the
                   conversation never goes silent.
    """

    key: str
    label_ar: str
    description_ar: str
    intent: str
    expected_media_type: str
    triggers: Tuple[str, ...]
    fallback_text: Optional[str] = None


# ──────────────────────────────────────────────────────────────────
# Registry
# ──────────────────────────────────────────────────────────────────
#
# NOTE: keys are intentionally split per-bank rather than a single
# "payment_qr" with a `bank` tag, because the merchant uploads each
# bank separately and the dropdown UX is clearer one-per-row.
# When adding a new bank just append below — no other code changes
# needed.

REGISTRY: Tuple[MediaKey, ...] = (
    # ── Payment / banking QR + barcodes ──────────────────────────
    # NOTE on trigger lists below — May 2026 #20 expansion:
    # The lists were widened to cover the realistic ways a Saudi
    # customer references each rail on WhatsApp. Substrings stay
    # short and content-bearing (no proclitics) — the matcher in
    # ``find_key_for_query`` does substring + Arabic
    # normalisation so "للراجحي" → "للراجحي" still contains
    # "راجحي". Adding "تحويل" or "حوالة" + bank name covers
    # the "أبي أحول للراجحي" / "كيف أحول للأهلي" framings that
    # don't literally use the word "باركود".
    MediaKey(
        key="payment_rajhi_barcode",
        label_ar="باركود الراجحي",
        description_ar="صورة باركود التحويل للراجحي (QR / Mosaic)",
        intent="payment",
        expected_media_type="image",
        triggers=(
            "راجحي", "rajhi", "alrajhi", "alrahji",
            "ar rajhi", "ar-rajhi", "ابو دانه",
            "بنك الراجحي", "للراجحي", "حساب الراجحي",
            "تحويل الراجحي", "تحويل للراجحي", "حوالة راجحي",
            "باركود الراجحي", "qr الراجحي", "كيو ار الراجحي",
            "كيوار الراجحي", "qr راجحي",
        ),
    ),
    MediaKey(
        key="payment_alahli_barcode",
        label_ar="باركود الأهلي",
        description_ar="صورة باركود التحويل للبنك الأهلي السعودي (SNB)",
        intent="payment",
        expected_media_type="image",
        triggers=(
            "اهلي", "ahli", "alahli", "al-ahli", "al ahli",
            "snb", "saudi national bank", "national bank",
            "بنك الاهلي", "للأهلي", "للاهلي", "حساب الاهلي",
            "تحويل الاهلي", "تحويل للاهلي", "حوالة اهلي",
            "باركود الأهلي", "باركود الاهلي", "qr الاهلي",
        ),
    ),
    MediaKey(
        key="payment_barq_barcode",
        label_ar="باركود برق",
        description_ar="صورة باركود حساب برق (Barq)",
        intent="payment",
        expected_media_type="image",
        triggers=(
            "برق", "بارق", "barq",
            "بنك برق", "حساب برق", "تحويل برق", "للبرق",
            "باركود برق", "qr برق",
        ),
    ),
    MediaKey(
        key="payment_stcpay_qr",
        label_ar="رمز STC Pay",
        description_ar="رمز QR لمحفظة STC Pay",
        intent="payment",
        expected_media_type="image",
        triggers=(
            "stc pay", "stcpay", "stc-pay", "stc",
            "إس تي سي باي", "اس تي سي باي", "اس تي سي",
            "محفظة stc", "محفظة اس تي سي",
            "كيو ار stc", "qr stc", "qr stcpay",
            "تحويل stc", "تحويل ستي سي",
        ),
    ),
    MediaKey(
        key="payment_mobilypay_qr",
        label_ar="رمز Mobily Pay",
        description_ar="رمز QR لمحفظة Mobily Pay",
        intent="payment",
        expected_media_type="image",
        triggers=(
            "mobily", "mobilypay", "mobily pay", "mobily-pay",
            "موبايلي", "موبايلي باي", "محفظة موبايلي",
            "تحويل موبايلي", "كيو ار موبايلي", "qr موبايلي",
        ),
    ),
    MediaKey(
        key="payment_bank_transfer_image",
        label_ar="صورة بيانات التحويل البنكي",
        description_ar="صورة تحتوي على بيانات الحساب البنكي الكاملة (للعملاء الذين يطلبون الآيبان)",
        intent="payment",
        expected_media_type="image",
        triggers=(
            "آيبان", "ايبان", "iban", "حساب بنكي",
            "بيانات الحساب", "تحويل بنكي",
        ),
    ),

    # ── Store info & customer-facing assets ──────────────────────
    MediaKey(
        key="store_location_image",
        label_ar="صورة الموقع / المعرض",
        description_ar="صورة المتجر أو المعرض أو خريطة الموقع",
        intent="store",
        expected_media_type="image",
        triggers=(
            "الموقع", "العنوان", "خريطة", "المعرض",
            "المحل", "وين موقعكم", "مكانكم",
        ),
    ),
    MediaKey(
        key="shipping_instruction_image",
        label_ar="صورة تعليمات الشحن",
        description_ar="صورة توضح خطوات الشحن / التغليف / التوصيل",
        intent="shipping",
        expected_media_type="image",
        triggers=(
            "الشحن", "التوصيل", "متى توصل", "كم تأخذ الشحنة",
            "تعليمات الشحن",
        ),
    ),

    # ── Product meta (general — not catalog-specific) ────────────
    MediaKey(
        key="product_usage_video",
        label_ar="فيديو طريقة الاستخدام",
        description_ar="فيديو عام يشرح طريقة استخدام منتجات المتجر",
        intent="product_meta",
        expected_media_type="video",
        triggers=(
            "كيف استخدم", "طريقة الاستخدام", "فيديو شرح",
            "شرح المنتج", "مقطع",
        ),
    ),
    MediaKey(
        key="product_usage_image",
        label_ar="صورة تعليمات الاستخدام",
        description_ar="صورة توضيحية لخطوات استخدام المنتج",
        intent="product_meta",
        expected_media_type="image",
        triggers=(
            "صورة الاستخدام", "تعليمات",
        ),
    ),

    # ── Trust signals / legal ────────────────────────────────────
    MediaKey(
        key="certificate_image",
        label_ar="صورة شهادة / اعتماد",
        description_ar="شهادة جودة / اعتماد رسمي / ISO / SFDA",
        intent="legal",
        expected_media_type="image",
        triggers=(
            "شهادة", "اعتماد", "iso", "sfda", "هيئة الغذاء",
            "موثق", "موثوق",
        ),
    ),
    MediaKey(
        key="review_screenshot",
        label_ar="صورة تقييم عميل",
        description_ar="لقطة شاشة لتقييم / مراجعة عميل سابق",
        intent="legal",
        expected_media_type="image",
        triggers=(
            "تقييمات", "تجارب", "مراجعات", "ترشيح عملاء",
            "تجارب عملاء",
        ),
    ),
)


# ──────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────


_BY_KEY: Dict[str, MediaKey] = {mk.key: mk for mk in REGISTRY}


def all_keys() -> List[MediaKey]:
    """Return the full registry (preserve declaration order)."""
    return list(REGISTRY)


def get(key: str) -> Optional[MediaKey]:
    """Fetch a registry entry by key. Returns ``None`` for
    unknown keys — callers must tolerate this (legacy uploads
    can have any string in ``media_key``)."""
    if not key:
        return None
    return _BY_KEY.get(key.strip().lower())


def is_valid_key(key: str) -> bool:
    """Whether ``key`` is in the canonical registry. The intake
    endpoint uses this to reject typos at upload time."""
    return get(key) is not None


# ──────────────────────────────────────────────────────────────────
# Heuristic — pick a key from free-form Arabic text
# ──────────────────────────────────────────────────────────────────
#
# Used as a deterministic safety net for the case where Claude
# fails to emit a marker but the customer's message clearly
# names a payment method or asset family. Mirrors the prior-art
# ``find_best_payment_asset`` helper at ``core/ai_libraries.py``.

# Strip Arabic tashkeel/diacritics + ASCII case-fold for matching.
_DIACRITICS_RE = re.compile(r"[\u064B-\u0652\u0670\u0640]")
_WS_RE = re.compile(r"\s+")


# Common Arabic letter variants we collapse before substring
# matching. Without this, "أهلي" / "اهلي" / "إهلي" all look
# different even though they are the same word — and the customer
# might prefix the bank name ("للأهلي") in ways the registry
# triggers can't predict. We deliberately keep this set minimal:
# only forms that change the letter SHAPE, not the meaning.
_ARABIC_LETTER_MAP = str.maketrans({
    "أ": "ا",
    "إ": "ا",
    "آ": "ا",
    "ٱ": "ا",
    "ى": "ي",
    "ئ": "ي",
    "ؤ": "و",
    "ة": "ه",
})


def normalize_text(text: str) -> str:
    """Public alias for :func:`_normalize`.

    Other modules (notably
    :mod:`services.payment_media_autolink`) need the exact same
    Arabic normalisation rules used by trigger matching here, so
    expose the helper instead of having every caller reimplement
    it slightly differently. Keep ``_normalize`` as the
    file-local hot path; this is just a thin re-export.
    """
    return _normalize(text)


def _normalize(text: str) -> str:
    """Light Arabic-aware normalisation for trigger matching.

    We:
      * strip Arabic diacritics + tatweel,
      * collapse the four alef variants (أ / إ / آ / ٱ → ا),
      * collapse ي/ى/ئ + و/ؤ + ة/ه,
      * lowercase ASCII,
      * collapse whitespace.

    We deliberately do NOT strip the ``ال`` definite article or
    the proclitic ``ل``/``ب``/``ف``/``و`` prefixes — those are
    short and stripping them would over-match. Instead, the
    triggers are short content nouns (``راجحي``, ``اهلي``,
    ``برق``) that naturally appear as substrings of inflected
    forms (``للأهلي`` → ``للاهلي`` which contains ``اهلي``).
    """
    if not text:
        return ""
    s = text.lower().strip()
    s = _DIACRITICS_RE.sub("", s)
    s = s.translate(_ARABIC_LETTER_MAP)
    s = _WS_RE.sub(" ", s)
    return s


# ──────────────────────────────────────────────────────────────────
# Generic payment-barcode disambiguation (May 2026 #21)
# ──────────────────────────────────────────────────────────────────
#
# Production data showed that Saudi customers often ask for the
# payment QR with a BARE generic noun and skip the bank name entirely:
#
#   "QR"                       "كيو آر"
#   "qr"                       "كيوار"
#   "باركود"                   "رمز الدفع"
#   "بار كود"                  "رمز التحويل"
#                              "رمز السداد"
#
# Adding these as triggers on a specific bank slug
# (``payment_rajhi_barcode``) would over-fire — tenants that only
# accept Alahli would also resolve "QR" to a Rajhi key they don't
# own. That's the same class of bug ``detect_payment_media_key``
# avoids on the link side.
#
# So we expose a *tenant-agnostic* matcher that simply answers
# "did the query mention a generic payment-barcode noun WITHOUT
# naming a specific bank?". The runtime resolver
# (:func:`services.media_resolver.resolve_for_query`) then combines
# this with a per-tenant single-asset count: if the merchant has
# exactly ONE active ``payment_*_barcode`` / ``payment_*_qr``
# media uploaded, the resolver attaches it; otherwise it bails and
# the LLM gets to disambiguate.
_GENERIC_PAYMENT_BARCODE_TRIGGERS: Tuple[str, ...] = (
    # Latin "QR"
    "qr",
    # Arabic transliterations of "QR" — different spellings the
    # customer actually types on a phone keyboard.
    "كيو ار",
    "كيو آر",
    "كيوار",
    # The plain noun "باركود" (and its split spelling) — the
    # most common phrasing among older customers.
    "باركود",
    "بار كود",
    # "رمز" + payment verb. These wouldn't false-positive on the
    # word "رمز" alone because the matcher requires both tokens.
    "رمز الدفع",
    "رمز التحويل",
    "رمز السداد",
)


# Pre-normalised at import time — saves a pass over the same
# strings on every inbound message.
_NORMALISED_GENERIC_PAYMENT_TRIGGERS: Tuple[str, ...] = tuple(
    filter(None, (_normalize(t) for t in _GENERIC_PAYMENT_BARCODE_TRIGGERS))
)


def is_generic_payment_barcode_query(query: str) -> bool:
    """True iff ``query`` mentions a generic payment-barcode noun
    AND no specific bank trigger applies.

    Callers in :mod:`services.media_resolver` use this as the
    gating signal for the "single uploaded barcode" fallback. We
    DELIBERATELY suppress the generic match when a specific bank
    trigger also fires — that case is already handled by the
    primary :func:`find_key_for_query` path and the bank-named
    asset should win.

    Returns ``False`` for the empty string, for queries that
    don't contain any generic noun, and for queries that ALSO
    contain a specific bank name (e.g. "أبي qr الراجحي").
    """
    if not query:
        return False
    needle = _normalize(query)
    if not needle:
        return False
    if not any(t in needle for t in _NORMALISED_GENERIC_PAYMENT_TRIGGERS):
        return False
    # If a specific bank trigger ALSO matches, defer to that path.
    if find_key_for_query(query) is not None:
        return False
    return True


def find_key_for_query(query: str) -> Optional[str]:
    """Best-effort: pick the registry key whose triggers most
    strongly match ``query``.

    Returns ``None`` when no trigger matches — callers must NOT
    invent a key. Used by the post-LLM safety net only; the
    primary path is the LLM emitting the marker directly.

    Scoring is intentionally simple: longest-trigger-match wins.
    "باركود الراجحي" (3 chars overlap with "راجحي") beats a
    bare "راجحي" hit if both keys' triggers overlap, because
    the longer match implies tighter intent.
    """
    if not query:
        return None
    needle = _normalize(query)
    best_key: Optional[str] = None
    best_len = 0
    for mk in REGISTRY:
        for trig in mk.triggers:
            t = _normalize(trig)
            if t and t in needle and len(t) > best_len:
                best_key = mk.key
                best_len = len(t)
    return best_key


# ──────────────────────────────────────────────────────────────────
# Prompt-side formatting
# ──────────────────────────────────────────────────────────────────


def format_keys_for_prompt(available_keys: List[str]) -> str:
    """Render the subset of registry keys actually uploaded by
    the merchant into an LLM-readable Arabic block.

    Only keys for which the merchant has actually uploaded an
    asset are surfaced — Claude can't emit a marker for something
    that doesn't exist. The resolver still has a registry-level
    fallback text for missing keys (see ``MediaKey.fallback_text``),
    used by the deterministic post-LLM path.

    Format (one line per key, easy for Claude to scan):

        - [MEDIA_KEY:<slug>] → <label_ar>: <description_ar>

    The caller is responsible for embedding this block under a
    clear header like "أدوات الوسائط المتوفرة في هذا المتجر:".
    """
    if not available_keys:
        return ""
    lines: List[str] = []
    seen: set = set()
    for k in available_keys:
        if not k or k in seen:
            continue
        seen.add(k)
        mk = get(k)
        if not mk:
            continue
        lines.append(
            f"- [MEDIA_KEY:{mk.key}] → {mk.label_ar}: {mk.description_ar}"
        )
    return "\n".join(lines)


__all__ = [
    "MediaKey",
    "REGISTRY",
    "all_keys",
    "get",
    "is_valid_key",
    "find_key_for_query",
    "is_generic_payment_barcode_query",
    "format_keys_for_prompt",
    "normalize_text",
]
