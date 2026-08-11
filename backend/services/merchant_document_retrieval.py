"""
Pack A1 — relevance-gated retrieval for imported long-form merchant documents.

Contract:
  - tenant_id filter mandatory
  - active + not deleted only
  - imported Salla CMS documents only (source=imported, origin=salla)
  - max 1–2 sections per turn
  - hard character cap
  - custom pages are never treated as policy truth
  - Pack B capability questions (payment methods / shipping companies / COD)
    do not trigger document retrieval
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

MAX_SECTIONS_PER_TURN = 2
HARD_CHARACTER_CAP = 3500

_STORY_RE = re.compile(
    r"("
    r"قص[ةه]\s*(?:المتجر|الشركة)|"
    r"من\s*أنتم|"
    r"حدثني\s*عن\s*(?:المتجر|الشركة)|"
    r"عن\s*(?:المتجر|الشركة)|"
    r"who\s*are\s*you|"
    r"about\s*(?:the\s*)?(?:store|shop|brand)|"
    r"our\s*story|"
    r"tell\s*me\s*about\s*(?:the\s*)?(?:store|shop)"
    r")",
    re.IGNORECASE,
)

_RETURN_RE = re.compile(
    r"("
    r"استرجاع|"
    r"إرجاع|"
    r"ارجاع|"
    r"استبدال|"
    r"استرداد|"
    r"return|"
    r"refund|"
    r"exchange"
    r")",
    re.IGNORECASE,
)

_SHIPPING_POLICY_RE = re.compile(
    r"("
    r"سياسة\s*(?:الشحن|التوصيل)|"
    r"shipping\s*polic|"
    r"delivery\s*polic|"
    r"شروط\s*(?:الشحن|التوصيل)"
    r")",
    re.IGNORECASE,
)

_TERMS_RE = re.compile(
    r"("
    r"شروط\s*(?:المتجر|الاستخدام|الخدمة)|"
    r"أحكام|"
    r"terms|"
    r"conditions"
    r")",
    re.IGNORECASE,
)

_PRIVACY_RE = re.compile(
    r"("
    r"خصوصي|"
    r"privacy|"
    r"حماية\s*البيانات"
    r")",
    re.IGNORECASE,
)

_WARRANTY_RE = re.compile(
    r"("
    r"ضمان|"
    r"warranty|"
    r"guarantee"
    r")",
    re.IGNORECASE,
)

# Structured profile questions — prefer /store/info facts, skip long-form.
_STRUCTURED_PROFILE_RE = re.compile(
    r"("
    r"وين\s*(?:موقعكم|الموقع)|"
    r"أين\s*(?:موقعكم|الموقع)|"
    r"كيف\s*أتواصل|"
    r"رقم(?:كم|كم\s*؟)|"
    r"جوالكم|"
    r"إيميل|"
    r"ايميل|"
    r"رابط\s*(?:المتجر|الموقع)|"
    r"متى\s*(?:تفتحون|دوامكم|أوقات)|"
    r"ساعات\s*(?:العمل|الدوام)|"
    r"where\s*(?:are\s*you|is\s*the\s*store)|"
    r"how\s*(?:can\s*i\s*)?contact|"
    r"store\s*(?:url|link|hours)|"
    r"working\s*hours|"
    r"opening\s*hours"
    r")",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RetrievedMerchantDocument:
    section_id: int
    kind: str
    title: str
    body: str
    content_hash: str
    provenance: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MerchantDocumentRetrievalResult:
    sections: Tuple[RetrievedMerchantDocument, ...]
    total_chars: int
    matched_intent: str
    truncated: bool


def detect_document_retrieval_intent(message: str) -> Optional[str]:
    """Return retrieval intent key, or None when long-form retrieval is skipped."""
    text = str(message or "").strip()
    if not text:
        return None

    # Pack B domain separation — capability FAQ owns these turns.
    try:
        from modules.ai.brain.commerce.merchant_capability_faq import (  # noqa: PLC0415
            is_merchant_payment_methods_question,
            is_merchant_shipping_companies_question,
        )
        if is_merchant_payment_methods_question(text):
            return None
        if is_merchant_shipping_companies_question(text):
            return None
    except Exception:  # noqa: silent-ok — Pack B FAQ detectors are optional; fall through to Pack A intent matching
        pass

    if _STRUCTURED_PROFILE_RE.search(text):
        return None

    if _PRIVACY_RE.search(text):
        return "privacy_policy"
    if _TERMS_RE.search(text):
        return "terms_policy"
    if _SHIPPING_POLICY_RE.search(text):
        return "shipping_policy"
    if _RETURN_RE.search(text):
        return "return_family"
    if _WARRANTY_RE.search(text):
        return "warranty"
    if _STORY_RE.search(text):
        return "store_story"
    return None


def _kinds_for_intent(intent: str) -> Tuple[str, ...]:
    if intent == "store_story":
        return ("store_story",)
    if intent == "return_family":
        return ("return_policy", "refund_policy", "exchange_policy")
    if intent == "shipping_policy":
        return ("shipping_policy",)
    if intent == "terms_policy":
        return ("terms_policy",)
    if intent == "privacy_policy":
        return ("privacy_policy",)
    if intent == "warranty":
        return ("warranty",)
    return ()


def retrieve_merchant_documents(
    db: Any,
    tenant_id: int,
    message: str,
    *,
    max_sections: int = MAX_SECTIONS_PER_TURN,
    hard_character_cap: int = HARD_CHARACTER_CAP,
) -> MerchantDocumentRetrievalResult:
    """Retrieve 0–N relevant imported document sections for one turn."""
    empty = MerchantDocumentRetrievalResult(
        sections=(),
        total_chars=0,
        matched_intent="",
        truncated=False,
    )
    if db is None or not tenant_id:
        return empty

    intent = detect_document_retrieval_intent(message)
    if not intent:
        return empty

    kinds = _kinds_for_intent(intent)
    if not kinds:
        return empty

    try:
        from models import MerchantKnowledgeSection  # noqa: PLC0415
        from core.knowledge import (  # noqa: PLC0415
            apply_ai_visible_kb_query_filters,
            is_imported_document_section,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[PackA1.retrieval] import failed: %s", exc)
        return empty

    try:
        rows = (
            apply_ai_visible_kb_query_filters(
                db.query(MerchantKnowledgeSection)
            )
            .filter(
                MerchantKnowledgeSection.tenant_id == int(tenant_id),
                MerchantKnowledgeSection.kind.in_(kinds),
                MerchantKnowledgeSection.source == "imported",
            )
            .order_by(
                MerchantKnowledgeSection.priority.asc(),
                MerchantKnowledgeSection.updated_at.desc(),
            )
            .limit(20)
            .all()
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[PackA1.retrieval] query failed tenant=%s: %s",
            tenant_id, exc,
        )
        return empty

    # custom is never policy truth; also require salla origin provenance.
    eligible = [
        r for r in rows
        if is_imported_document_section(r)
        and str(getattr(r, "kind", "") or "").strip().lower() != "custom"
    ]

    cap = max(1, min(int(max_sections or MAX_SECTIONS_PER_TURN), MAX_SECTIONS_PER_TURN))
    char_cap = max(200, int(hard_character_cap or HARD_CHARACTER_CAP))

    selected: List[RetrievedMerchantDocument] = []
    total = 0
    truncated = False
    for row in eligible[:cap]:
        body = str(getattr(row, "body", "") or "").strip()
        if not body:
            continue
        meta = getattr(row, "metadata_json", None) or {}
        if not isinstance(meta, dict):
            meta = {}
        remaining = char_cap - total
        if remaining <= 0:
            truncated = True
            break
        if len(body) > remaining:
            body = body[:remaining]
            truncated = True
        content_hash = str(meta.get("content_hash") or "")
        selected.append(
            RetrievedMerchantDocument(
                section_id=int(getattr(row, "id", 0) or 0),
                kind=str(getattr(row, "kind", "") or ""),
                title=str(getattr(row, "title", "") or ""),
                body=body,
                content_hash=content_hash,
                provenance={
                    "source": "imported",
                    "origin": meta.get("origin") or "salla",
                    "source_type": meta.get("source_type") or "cms_page",
                    "external_page_id": meta.get("salla_page_id") or meta.get("external_page_id"),
                    "tenant_id": int(tenant_id),
                    "doc_ref": f"mks:{getattr(row, 'id', None)}",
                },
            )
        )
        total += len(body)
        if truncated:
            break

    return MerchantDocumentRetrievalResult(
        sections=tuple(selected),
        total_chars=total,
        matched_intent=intent,
        truncated=truncated,
    )


def format_retrieved_documents_for_prompt(
    result: MerchantDocumentRetrievalResult,
) -> str:
    """Format retrieved docs for Brain prompt injection (facts only, no prose invention)."""
    if not result.sections:
        return ""
    parts: List[str] = [
        "وثائق المتجر المسترجعة (مصدر موثوق — اقتبس منها فقط عند الإجابة):",
    ]
    for idx, doc in enumerate(result.sections, start=1):
        title = doc.title or doc.kind
        parts.append(
            f"### وثيقة {idx}: {title}\n"
            f"(kind={doc.kind}, doc_ref={doc.provenance.get('doc_ref')})\n"
            f"{doc.body}"
        )
    return "\n\n".join(parts).strip()
