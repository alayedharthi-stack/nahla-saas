"""
Pack A2 — classify store-profile customer questions into answer topics.

Routing contract:
  - social → owner_contact (reuse existing FAQ surface)
  - currency → LLM over MERCHANT_PROFILE facts (no new FAQ prose)
  - account status (نشط/active) → LLM over MERCHANT_PROFILE.status only
  - open-now (شغال/مفتوح) → NOT profile; leave to working-hours / UNKNOWN
  - Pack B payment/shipping/COD questions → never claimed here
"""
from __future__ import annotations

import re
from typing import Any, Optional

_ABOUT_RE = re.compile(
    r"("
    r"حدثني\s*عن\s*(?:المتجر|الشركة)|"
    r"من\s*أنتم|"
    r"وش\s*متجر(?:كم|ك|نا)?|"
    r"عن\s*(?:المتجر|الشركة)|"
    r"تعريف\s*(?:المتجر|الشركة)|"
    r"who\s*are\s*you|"
    r"about\s*(?:the\s*)?(?:store|shop|brand)|"
    r"tell\s*me\s*about"
    r")",
    re.IGNORECASE,
)

# Explicit long-form story — Pack A3 MKS store_story owns these (not A2 about).
_EXPLICIT_STORY_RE = re.compile(
    r"("
    r"قص[ةه]\s*(?:المتجر|الشركة|البراند)|"
    r"كيف\s*بدأ(?:ت)?\s*(?:قص[ةه]|المتجر)|"
    r"our\s*story|"
    r"how\s*(?:did\s*)?(?:you|the\s*store)\s*start"
    r")",
    re.IGNORECASE,
)

_URL_RE = re.compile(
    r"("
    r"رابط\s*(?:ال)?(?:متجر|موقع)|"
    r"لينك\s*(?:ال)?(?:متجر|موقع)|"
    r"موقع\s*(?:ال)?متجر|"
    r"موقع(?:كم|ك|نا)?\s*(?:ال)?(?:إ|ا)?لكتروني|"
    r"الموقع\s*(?:ال)?(?:إ|ا)?لكتروني|"
    r"store\s*(?:url|link)|"
    r"website"
    r")",
    re.IGNORECASE,
)

_CONTACT_RE = re.compile(
    r"("
    r"كيف\s*أتواصل|"
    r"كيف\s*اتواصل|"
    r"رقم(?:كم|ك)\b|"
    r"رقم\s*(?:التواصل|خدمة\s*العملاء|الواتساب)|"
    r"جوال(?:كم|ك)\b|"
    r"إيميل(?:كم|ك)?|ايميل(?:كم|ك)?|بريد(?:كم|ك)?|"
    r"كيف\s*أتصل|"
    r"contact\s*(?:number|info|email)?|"
    r"\bemail\b"
    r")",
    re.IGNORECASE,
)

# Operational identity / order-phone questions must not become store contact FAQ.
_CONTACT_EXCLUSION_RE = re.compile(
    r"("
    r"طلب(?:ي|نا)|"
    r"المسجل|"
    r"رقم\s*(?:ال)?(?:طلب|شحن|تتبع|حساب|آيبان|ايبان)|"
    r"جوال\s*(?:ال)?(?:عميل|المسجل|الطلب)|"
    r"customer\s*phone|"
    r"order\s*(?:phone|number)"
    r")",
    re.IGNORECASE,
)

_SOCIAL_RE = re.compile(
    r"("
    r"حسابات\s*(?:تواصل|التواصل)|"
    r"انستقرام|إنستغرام|instagram|"
    r"تويتر|twitter|"
    r"سناب|snapchat|"
    r"تيك\s*توك|tiktok|"
    r"سوشيال|social"
    r")",
    re.IGNORECASE,
)

_CURRENCY_RE = re.compile(
    r"("
    r"عمل(?:ة|تكم|ة\s*المتجر)|"
    r"currency|"
    r"بأي\s*عملة"
    r")",
    re.IGNORECASE,
)

# Account/storefront status — NOT open-now operational claim.
_ACCOUNT_STATUS_RE = re.compile(
    r"("
    r"هل\s*(?:المتجر\s*)?(?:نشط|فعال)|"
    r"حالة\s*المتجر|"
    r"store\s*status|"
    r"is\s*(?:the\s*)?store\s*active"
    r")",
    re.IGNORECASE,
)

# Open-now / business-hours operational questions — profile must NOT own these.
_OPEN_NOW_RE = re.compile(
    r"("
    r"هل\s*(?:المتجر\s*)?(?:شغال|مفتوح|فاتح|مسكر|مقفل)|"
    r"شغال(?:ين)?|"
    r"are\s*you\s*open|"
    r"is\s*(?:the\s*)?store\s*open"
    r")",
    re.IGNORECASE,
)

# Pack B must win these turns.
_PACK_B_RE = re.compile(
    r"("
    r"طرق\s*الدفع|وسائل\s*الدفع|"
    r"دفع\s*عند\s*الاستلام|\bcod\b|"
    r"شركات\s*(?:الشحن|التوصيل)|"
    r"payment\s*methods?|shipping\s*compan"
    r")",
    re.IGNORECASE,
)


def is_open_now_question(message: str) -> bool:
    return bool(_OPEN_NOW_RE.search(str(message or "")))


def _contact_store_keyword_haystack(message: str) -> str:
    """Keyword haystack for contact/store classifiers. Raw message is unchanged."""
    from core.inbound_url_spans import semantic_text_excluding_url_spans  # noqa: PLC0415

    return semantic_text_excluding_url_spans(message)


def _trusted_http_url(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        from modules.ai.brain.commerce.storefront_product_url import (  # noqa: PLC0415
            is_trusted_merchant_http_url,
        )
    except Exception:  # noqa: BLE001
        return raw if raw.lower().startswith(("http://", "https://")) else ""
    if is_trusted_merchant_http_url(raw):
        return raw
    return ""


def _inbound_url_texts(message: str) -> list[str]:
    from core.inbound_url_spans import extract_inbound_url_spans  # noqa: PLC0415

    return extract_inbound_url_spans(message)


def _reject_inbound_cta(url: str, inbound_spans: list[str]) -> str:
    from core.inbound_url_spans import url_matches_inbound_span  # noqa: PLC0415

    trusted = _trusted_http_url(url)
    if not trusted:
        return ""
    if url_matches_inbound_span(trusted, inbound_spans):
        return ""
    return trusted


def authorized_profile_cta_url(
    *,
    topic: str,
    message: str,
    facts: Any = None,
    merchant_context: Any = None,
) -> str:
    """Tenant-authorized CTA destination, never the customer-supplied URL.

    store_info → merchant store_url only.
    owner_contact → matching configured social URL when a social_links key
    appears in the keyword haystack; otherwise empty (no store-link substitute).
    """
    inbound = _inbound_url_texts(message)
    haystack = _contact_store_keyword_haystack(message)
    mc = merchant_context if isinstance(merchant_context, dict) else {}
    mp = mc.get("merchant_profile") if isinstance(mc, dict) else None
    mp = mp if isinstance(mp, dict) else {}

    if topic == "store_info":
        store_url = ""
        if facts is not None:
            store_url = str(getattr(facts, "store_url", "") or "").strip()
        if not store_url:
            store_url = str(mp.get("domain") or "").strip()
        return _reject_inbound_cta(store_url, inbound)

    if topic != "owner_contact":
        return ""

    social = {}
    if facts is not None:
        raw_social = getattr(facts, "merchant_profile_social_links", None)
        if isinstance(raw_social, dict):
            social = raw_social
    if not social:
        raw_social = mp.get("social_links")
        if isinstance(raw_social, dict):
            social = raw_social
    haystack_l = haystack.lower()
    for key, val in social.items():
        key_l = str(key or "").strip().lower()
        if key_l and key_l in haystack_l:
            return _reject_inbound_cta(str(val or ""), inbound)
    return ""


def classify_store_profile_topic(message: str) -> Optional[str]:
    """Return answer topic for structured profile questions, else None.

    Topics:
      store_about | store_info | owner_contact | store_currency | store_status
    Social maps to owner_contact (existing FAQ). Open-now returns None.
    Keyword matching uses URL-span-excluded haystack; raw *message* is kept.
    """
    text = _contact_store_keyword_haystack(message)
    if not text:
        return None
    if _PACK_B_RE.search(text):
        return None
    if is_open_now_question(text):
        # Working-hours / open-now evidence owns this — not account status.
        return None
    if _CURRENCY_RE.search(text):
        return "store_currency"
    if _ACCOUNT_STATUS_RE.search(text):
        return "store_status"
    if _SOCIAL_RE.search(text):
        return "owner_contact"
    if _CONTACT_EXCLUSION_RE.search(text):
        return None
    if _CONTACT_RE.search(text):
        return "owner_contact"
    if _URL_RE.search(text):
        return "store_info"
    # Explicit story questions are Pack A3 MKS — do not claim as A2 about.
    if _EXPLICIT_STORY_RE.search(text):
        return None
    if _ABOUT_RE.search(text):
        return "store_about"
    return None


def prepared_store_description(
    *,
    facts: Any = None,
    merchant_context: Any = None,
    store_description: str = "",
) -> str:
    """Read structured description from prepared turn facts (no DB)."""
    desc = str(store_description or "").strip()
    if desc:
        return desc
    if facts is not None:
        desc = str(getattr(facts, "store_description", "") or "").strip()
        if desc:
            return desc
    mc = merchant_context if isinstance(merchant_context, dict) else {}
    mp = mc.get("merchant_profile") if isinstance(mc, dict) else None
    if isinstance(mp, dict):
        return str(mp.get("description") or "").strip()
    return ""


def build_merchant_profile_decision(
    *,
    message: str,
    store_description: str = "",
    facts: Any = None,
    merchant_context: Any = None,
) -> Optional[Any]:
    """Build a Decision for a profile FAQ/LLM turn, or None if not owned.

    Decision consumes prepared CommerceFacts / merchant_context only.
    Do NOT pass or open a DB session here.
    """
    from modules.ai.brain.decision.actions import (  # noqa: PLC0415
        ACTION_FAQ_REPLY,
        ACTION_LLM_REPLY,
    )
    from modules.ai.brain.types import Decision  # noqa: PLC0415

    topic = classify_store_profile_topic(message)
    if not topic:
        return None

    if topic == "store_about":
        desc = prepared_store_description(
            facts=facts,
            merchant_context=merchant_context,
            store_description=store_description,
        )
        if not desc:
            # Structured description absent — allow MKS store_story / persona.
            return None
        return Decision(
            action=ACTION_FAQ_REPLY,
            args={"topic": "store_about"},
            reason="customer asked store about — structured description present",
        )

    if topic == "store_info":
        return llm_store_info_decision(
            message=message,
            facts=facts,
            merchant_context=merchant_context,
        )

    if topic == "owner_contact":
        cta = authorized_profile_cta_url(
            topic="owner_contact",
            message=message,
            facts=facts,
            merchant_context=merchant_context,
        )
        return Decision(
            action=ACTION_LLM_REPLY,
            args={
                "topic": "owner_contact",
                "topic_hint": "merchant_profile",
                "profile_surface": "merchant_profile",
                "question_kind": "owner_contact",
                "authorized_cta_url": cta,
                "response_goal": (
                    "Answer the contact / social-channel question using only "
                    "trusted merchant_profile phone, email, and social_links. "
                    "If a requested channel is not configured, say so. "
                    "Do not invent URLs, phones, or emails. "
                    "Do not use any customer-supplied URL. "
                    "Do not substitute the store URL unless that is the "
                    "configured channel the customer asked for."
                ),
            },
            reason="customer asked contact/social — structured profile channels only",
        )

    if topic == "store_currency":
        return Decision(
            action=ACTION_LLM_REPLY,
            args={
                "topic": "merchant_profile_currency",
                "topic_hint": "merchant_profile",
                "profile_surface": "merchant_profile",
                "question_kind": "currency",
                "response_goal": (
                    "Answer the store currency question using only "
                    "trusted merchant_profile.currency when status is "
                    "KNOWN_VALUE. If UNKNOWN, say the currency is not "
                    "configured. Do not invent a currency."
                ),
            },
            reason="customer asked store currency — MERCHANT_PROFILE facts only",
        )

    if topic == "store_status":
        return Decision(
            action=ACTION_LLM_REPLY,
            args={
                "topic": "merchant_profile_status",
                "topic_hint": "merchant_profile",
                "profile_surface": "merchant_profile",
                "question_kind": "account_status",
                "response_goal": (
                    "Disclose only the known merchant_profile.status "
                    "account/storefront field value when KNOWN_VALUE. "
                    "Do NOT claim the store is currently open/closed "
                    "or invent working hours. If status is UNKNOWN, "
                    "say the account status is not configured."
                ),
            },
            reason=(
                "customer asked account/storefront status — disclose "
                "MERCHANT_PROFILE.status only, never open-now inference"
            ),
        )

    return None


def llm_store_info_decision(
    *,
    message: str,
    facts: Any = None,
    merchant_context: Any = None,
    reason: str = "customer asked store URL / store info",
    confidence: float = 0.90,
) -> Any:
    """Model-owned store-link Decision with out-of-band authorized CTA."""
    from modules.ai.brain.decision.actions import ACTION_LLM_REPLY  # noqa: PLC0415
    from modules.ai.brain.types import Decision  # noqa: PLC0415

    cta = authorized_profile_cta_url(
        topic="store_info",
        message=message,
        facts=facts,
        merchant_context=merchant_context,
    )
    return Decision(
        action=ACTION_LLM_REPLY,
        args={
            "topic": "store_info",
            "topic_hint": "merchant_profile",
            "profile_surface": "merchant_profile",
            "question_kind": "store_url",
            "authorized_cta_url": cta,
            "response_goal": (
                "Answer the store URL / website question using only "
                "trusted merchant_profile.domain / store_url when known. "
                "If unknown, say the store link is not configured. "
                "Do not invent a URL. Do not use any customer-supplied URL."
            ),
        },
        reason=reason,
        confidence=confidence,
    )


def should_yield_catalog_for_merchant_profile(
    *,
    intent_name: str = "",
    message: str = "",
) -> bool:
    """Profile FAQ outranks generic catalog browse when classifier matches."""
    if classify_store_profile_topic(message):
        return True
    name = str(intent_name or "").strip()
    if name not in {
        "ask_store_info",
        "online_store_inquiry",
        "ask_owner_contact",
    }:
        return False
    # Intent name alone is not enough: a URL-only inbound must not yield
    # catalog just because a social token sat inside the hostname.
    return bool(_contact_store_keyword_haystack(message))


__all__ = [
    "authorized_profile_cta_url",
    "build_merchant_profile_decision",
    "classify_store_profile_topic",
    "is_open_now_question",
    "llm_store_info_decision",
    "prepared_store_description",
    "should_yield_catalog_for_merchant_profile",
]
