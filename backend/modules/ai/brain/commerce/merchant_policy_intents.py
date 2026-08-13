"""
Pack A3 — informational merchant long-form policy / story ownership.

Decision-layer only (no intent-rule edits). Consumes prepared facts / message.
Does NOT open a DB session. Does NOT own Pack B capabilities or Pack A2 profile.
FAQ customer exposure is DEFERRED (no audience/visibility contract).
"""
from __future__ import annotations

import re
from typing import Any, Optional

# Explicit narrative / store story (not ordinary A2 about).
_STORY_RE = re.compile(
    r"("
    r"قص[ةه]\s*(?:المتجر|الشركة|البراند)|"
    r"كيف\s*بدأ(?:ت)?\s*(?:قص[ةه]|المتجر)|"
    r"رحلت(?:كم|نا)|"
    r"our\s*story|"
    r"how\s*(?:did\s*)?(?:you|the\s*store)\s*start"
    r")",
    re.IGNORECASE,
)

_RETURN_POLICY_RE = re.compile(
    r"("
    r"سياس[ةه]\s*(?:ال)?(?:استرجاع|إرجاع|ارجاع|استرداد|استبدال)|"
    r"شروط\s*(?:ال)?(?:استرجاع|إرجاع|ارجاع|استرداد|استبدال)|"
    r"كيف\s*(?:نظام|تتم|يصير)\s*(?:ال)?(?:استرجاع|إرجاع|الاسترداد|الاستبدال)|"
    r"كم\s*(?:سياس[ةه]|مدة)\s*(?:ال)?(?:استرجاع|الاسترداد)|"
    r"(?:هل\s*)?عند(?:كم|ك)\s*(?:إرجاع|ارجاع|استرجاع|استرداد|استبدال)|"
    r"فيه\s*(?:إرجاع|ارجاع|استرجاع)|"
    r"return\s*polic|"
    r"refund\s*polic|"
    r"exchange\s*polic"
    r")",
    re.IGNORECASE,
)

_SHIPPING_POLICY_RE = re.compile(
    r"("
    r"سياس[ةه]\s*(?:ال)?(?:شحن|توصيل)|"
    r"شروط\s*(?:ال)?(?:شحن|توصيل)|"
    r"shipping\s*polic|"
    r"delivery\s*polic"
    r")",
    re.IGNORECASE,
)

_TERMS_RE = re.compile(
    r"("
    r"شروط\s*(?:و|وال)?أ?حكام|"
    r"الشروط\s*(?:و|وال)?أ?حكام|"
    r"شروط\s*(?:الاستخدام|الخدمة|المتجر)|"
    r"terms\s*(?:and\s*)?conditions|"
    r"\bterms\b"
    r")",
    re.IGNORECASE,
)

_PRIVACY_RE = re.compile(
    r"("
    r"سياس[ةه]\s*(?:ال)?خصوصي|"
    r"الخصوصي[ةه]|"
    r"privacy\s*polic"
    r")",
    re.IGNORECASE,
)

_WARRANTY_POLICY_RE = re.compile(
    r"("
    r"سياس[ةه]\s*(?:ال)?ضمان|"
    r"(?:هل\s*)?عند(?:كم|ك)\s*(?:سياس[ةه]\s*)?ضمان|"
    r"فيه\s*ضمان|"
    r"warranty\s*polic|"
    r"do\s*you\s*(?:have\s*)?(?:a\s*)?warranty"
    r")",
    re.IGNORECASE,
)

# Product-specific warranty must not be claimed as merchant-wide policy.
_PRODUCT_WARRANTY_RE = re.compile(
    r"("
    r"هذا\s*(?:ال)?منتج|"
    r"هالمنتج|"
    r"عليه\s*ضمان|"
    r"ضمان\s*(?:على|لهذا)|"
    r"this\s*product|"
    r"product\s*warranty"
    r")",
    re.IGNORECASE,
)

_PACK_B_SHIPPING_COMPANIES_RE = re.compile(
    r"("
    r"شركات\s*(?:الشحن|التوصيل)|"
    r"شركة\s*(?:الشحن|التوصيل)|"
    r"shipping\s*compan|"
    r"which\s*carrier|"
    r"couriers?"
    r")",
    re.IGNORECASE,
)

_PACK_B_PAYMENT_RE = re.compile(
    r"("
    r"طرق\s*الدفع|وسائل\s*الدفع|"
    r"دفع\s*عند\s*الاستلام|\bcod\b|"
    r"payment\s*methods?"
    r")",
    re.IGNORECASE,
)

# FAQ customer exposure deferred — detect only to refuse catalog steal without answering from ops FAQ.
_FAQ_ASK_RE = re.compile(
    r"("
    r"أسئل[ةه]\s*شائع|"
    r"\bfaq\b|"
    r"frequently\s*asked"
    r")",
    re.IGNORECASE,
)


def classify_merchant_policy_topic(message: str) -> Optional[str]:
    """Return Pack A3 knowledge topic or None.

    Topics:
      return_policy | refund_policy | exchange_policy | shipping_policy |
      terms_policy | privacy_policy | warranty | store_story
    FAQ is intentionally never returned (deferred visibility).
    """
    text = str(message or "").strip()
    if not text:
        return None
    if _PACK_B_PAYMENT_RE.search(text):
        return None
    if _PACK_B_SHIPPING_COMPANIES_RE.search(text):
        return None
    if _STORY_RE.search(text):
        return "store_story"
    if _SHIPPING_POLICY_RE.search(text):
        return "shipping_policy"
    if _PRIVACY_RE.search(text):
        return "privacy_policy"
    if _TERMS_RE.search(text):
        return "terms_policy"
    if _WARRANTY_POLICY_RE.search(text) and not _PRODUCT_WARRANTY_RE.search(text):
        return "warranty"
    if _RETURN_POLICY_RE.search(text):
        # Prefer more specific refund/exchange wording when present.
        low = text
        if re.search(r"استرداد|refund", low, re.I):
            return "refund_policy"
        if re.search(r"استبدال|exchange", low, re.I):
            return "exchange_policy"
        return "return_policy"
    return None


def is_informational_policy_or_story_question(message: str) -> bool:
    return classify_merchant_policy_topic(message) is not None


def is_deferred_faq_customer_question(message: str) -> bool:
    """True when customer asked FAQ but A3 must not expose ops/style FAQ rows."""
    text = str(message or "").strip()
    if not text:
        return False
    if classify_merchant_policy_topic(text):
        return False
    return bool(_FAQ_ASK_RE.search(text))


def should_yield_catalog_for_merchant_policy(
    *,
    intent_name: str = "",
    message: str = "",
) -> bool:
    """False when catalog must yield to merchant-wide policy/story ownership."""
    del intent_name
    if classify_merchant_policy_topic(message):
        return False
    if is_deferred_faq_customer_question(message):
        # Do not catalog-search; also do not answer from unsafe FAQ rows.
        return False
    return True


def _policy_status_from_facts(facts: Any, kind: str) -> str:
    if facts is None:
        return "UNKNOWN"
    # Preferred: projected merchant_policy nested map on known_facts / ctx.
    mp = getattr(facts, "merchant_policy", None)
    if isinstance(mp, dict):
        row = mp.get(kind) if isinstance(mp.get(kind), dict) else None
        if row:
            return str(row.get("status") or "UNKNOWN")
    # Flat prepared attributes used in tests.
    flat = getattr(facts, f"policy_{kind}_status", None)
    if flat:
        return str(flat)
    return "UNKNOWN"


def _policy_doc_ref_from_facts(facts: Any, kind: str) -> Optional[str]:
    if facts is None:
        return None
    mp = getattr(facts, "merchant_policy", None)
    if isinstance(mp, dict):
        row = mp.get(kind) if isinstance(mp.get(kind), dict) else None
        if row and row.get("doc_ref"):
            return str(row.get("doc_ref"))
    flat = getattr(facts, f"policy_{kind}_doc_ref", None)
    if flat:
        return str(flat)
    return None


def build_merchant_policy_decision(
    *,
    message: str,
    facts: Any = None,
    merchant_context: Any = None,
    question_kind: Optional[str] = None,
) -> Optional[Any]:
    """Build ACTION_LLM_REPLY for informational long-form knowledge, or None."""
    del merchant_context  # reserved for future prepared-context enrichment
    from modules.ai.brain.decision.actions import ACTION_LLM_REPLY  # noqa: PLC0415
    from modules.ai.brain.types import Decision  # noqa: PLC0415

    text = str(message or "").strip()
    if not text:
        return None

    if is_deferred_faq_customer_question(text) and not question_kind:
        return Decision(
            action=ACTION_LLM_REPLY,
            args={
                "topic": "merchant_knowledge_faq_deferred",
                "policy_surface": "merchant_knowledge_section",
                "question_kind": "faq",
                "knowledge_kind": "faq",
                "merchant_policy_status": "UNKNOWN",
                "faq_visibility": "deferred",
                "response_goal": (
                    "Customer asked about FAQs, but customer-facing FAQ "
                    "visibility is deferred. Do not invent FAQ content and "
                    "do not dump internal ops/style instructions. Honestly "
                    "say you do not have a confirmed public FAQ list to share."
                ),
            },
            reason="Pack A3 FAQ customer exposure deferred — no visibility contract",
        )

    topic = question_kind or classify_merchant_policy_topic(text)
    if not topic:
        return None

    status = "UNKNOWN"
    doc_ref = None
    if topic == "store_story":
        # Story uses MKS presence via retrieval; status hint optional.
        status = str(getattr(facts, "store_story_status", "") or "UNKNOWN")
        doc_ref = getattr(facts, "store_story_doc_ref", None)
    else:
        status = _policy_status_from_facts(facts, topic)
        doc_ref = _policy_doc_ref_from_facts(facts, topic)

    if status not in {"KNOWN_PRESENT", "UNKNOWN"}:
        status = "UNKNOWN"

    if status == "KNOWN_PRESENT":
        response_goal = (
            f"Answer the customer's informational {topic} question using ONLY "
            f"the retrieved MerchantKnowledgeSection body for this tenant. "
            f"Cite no invented windows, fees, or conditions beyond that body."
        )
    else:
        response_goal = (
            f"No authoritative {topic} document is confirmed for this merchant. "
            f"Honestly say the information is not available. Do NOT invent "
            f"days, fees, windows, warranty periods, shipping durations, or legal terms."
        )

    return Decision(
        action=ACTION_LLM_REPLY,
        args={
            "topic": f"merchant_knowledge_{topic}",
            "policy_surface": "merchant_knowledge_section",
            "question_kind": topic,
            "knowledge_kind": topic,
            "merchant_policy_status": status,
            "doc_ref": doc_ref,
            "response_goal": response_goal,
            "block_catalog_navigation": True,
        },
        reason=f"Pack A3 informational knowledge ownership topic={topic} status={status}",
    )


__all__ = [
    "build_merchant_policy_decision",
    "classify_merchant_policy_topic",
    "is_deferred_faq_customer_question",
    "is_informational_policy_or_story_question",
    "should_yield_catalog_for_merchant_policy",
]
