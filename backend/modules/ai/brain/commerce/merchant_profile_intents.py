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
    r"عن\s*(?:المتجر|الشركة)|"
    r"تعريف\s*(?:المتجر|الشركة)|"
    r"قصة\s*(?:المتجر|الشركة)|"
    r"who\s*are\s*you|"
    r"about\s*(?:the\s*)?(?:store|shop|brand)|"
    r"tell\s*me\s*about"
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


def classify_store_profile_topic(message: str) -> Optional[str]:
    """Return answer topic for structured profile questions, else None.

    Topics:
      store_about | store_info | owner_contact | store_currency | store_status
    Social maps to owner_contact (existing FAQ). Open-now returns None.
    """
    text = str(message or "").strip()
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
    if _ABOUT_RE.search(text):
        return "store_about"
    return None


def build_merchant_profile_decision(
    *,
    message: str,
    db: Any = None,
    tenant_id: int = 0,
) -> Optional[Any]:
    """Build a Decision for a profile FAQ/LLM turn, or None if not owned."""
    from modules.ai.brain.decision.actions import (  # noqa: PLC0415
        ACTION_FAQ_REPLY,
        ACTION_LLM_REPLY,
    )
    from modules.ai.brain.types import Decision  # noqa: PLC0415

    topic = classify_store_profile_topic(message)
    if not topic:
        return None

    if topic == "store_about":
        if db is not None and tenant_id:
            try:
                from core.merchant_profile import resolve_merchant_profile  # noqa: PLC0415

                prof = resolve_merchant_profile(db, int(tenant_id))
                if not (prof.description or "").strip():
                    # Structured description absent — allow MKS store_story / persona.
                    return None
            except Exception:  # noqa: silent-ok
                return None
        return Decision(
            action=ACTION_FAQ_REPLY,
            args={"topic": "store_about"},
            reason="customer asked store about — structured description present",
        )

    if topic == "store_info":
        return Decision(
            action=ACTION_FAQ_REPLY,
            args={"topic": "store_info"},
            reason="customer asked store URL / store info",
        )

    if topic == "owner_contact":
        return Decision(
            action=ACTION_FAQ_REPLY,
            args={"topic": "owner_contact"},
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


def should_yield_catalog_for_merchant_profile(
    *,
    intent_name: str = "",
    message: str = "",
) -> bool:
    """Profile FAQ outranks generic catalog browse when classifier matches."""
    if classify_store_profile_topic(message):
        return True
    name = str(intent_name or "").strip()
    return name in {
        "ask_store_info",
        "online_store_inquiry",
        "ask_owner_contact",
    }


__all__ = [
    "build_merchant_profile_decision",
    "classify_store_profile_topic",
    "is_open_now_question",
    "should_yield_catalog_for_merchant_profile",
]
