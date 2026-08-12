"""
Post-compose guard: block unsupported policy/story specificity on UNKNOWN A3 turns.

Active only when:
  policy_surface == merchant_knowledge_section
  AND merchant_policy_status == UNKNOWN
  AND retrieval_count == 0

Trips on unsupported *specificity* (coverage assertions, fee/cost rules,
windows/durations, narrative story fabrication) — not phrase blacklists.

Primary action: strip inventing sentences. If the reply is empty after scrub,
use the audited emergency fallback with compose_source=fallback_deterministic.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger("nahla.brain.postprocess.merchant_knowledge_unknown_truth_guard")

# Audited emergency fallback — factual absence only; not the primary path.
_EMERGENCY_FALLBACK_AR = "ما عندي معلومات مؤكدة عن هذا الموضوع حاليًا."

_HONEST_ABSENCE_RE = re.compile(
    r"("
    r"ما\s*عند[يى]|ما\s*عندنا|غير\s*متاح|غير\s*متوفر|مو\s*محفوظ|"
    r"لا\s*تتوفر|ما\s*أقدر\s*أأكد|غير\s*مؤكد|مو\s*واضحة|ما\s*وصلتني|"
    r"ما\s*عندي\s*تفاصيل|ما\s*عندنا\s*تفاصيل|غير\s*متوفرة|"
    r"not\s+available|do\s+not\s+have|cannot\s+confirm|no\s+confirmed"
    r")",
    re.UNICODE | re.IGNORECASE,
)

# Unsupported specificity — structural claim shapes, not banned phrases.
_SPECIFICITY_RES: Tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "duration_window",
        re.compile(
            r"("
            r"\d+\s*(?:يوم|أيام|ساعة|ساعات|أسبوع|أسابيع)|"
            r"(?:خلال|مدة)\s*\d+|"
            r"(?:ضمان|استرجاع|استرداد|استبدال)\s*(?:لمدة|خلال)\s*\d+"
            r")",
            re.UNICODE | re.IGNORECASE,
        ),
    ),
    (
        "fee_or_cost_rule",
        re.compile(
            r"("
            r"تكلفة\s*(?:ال)?شحن|"
            r"رسوم\s*(?:ال)?شحن|"
            r"(?:الشحن|التوصيل).{0,40}(?:يعتمد|تعتمد).{0,40}(?:مدينة|المدينة)|"
            r"(?:مجاني|بدون\s*رسوم).{0,20}(?:شحن|توصيل)|"
            r"\d+\s*(?:ريال|ر\.?\s*س)"
            r")",
            re.UNICODE | re.IGNORECASE,
        ),
    ),
    (
        "coverage_or_availability_assertion",
        re.compile(
            r"("
            r"(?:الشحن|التوصيل)\s*(?:متوفر|متاح)|"
            r"(?:متوفر|متاح)\s*(?:للمدن|للمناطق|في\s*كل)|"
            r"المدن\s*(?:و|و\s*)?المناطق\s*(?:ال)?مدعوم|"
            r"شركات\s*(?:ال)?شحن\s*(?:ال)?متاح"
            r")",
            re.UNICODE | re.IGNORECASE,
        ),
    ),
    (
        "condition_list",
        re.compile(
            r"("
            r"بشرط\s*أن|"
            r"شروط\s*(?:ال)?(?:استرجاع|استبدال|ضمان)|"
            r"يجب\s*(?:أن\s*)?(?:يكون|تحتفظ)|"
            r"خلال\s*\d+.+(?:من\s*تاريخ|بعد\s*الاستلام)"
            r")",
            re.UNICODE | re.IGNORECASE,
        ),
    ),
    (
        "fabricated_store_story",
        re.compile(
            r"("
            r"منصة\s*متخصص|"
            r"تأسس(?:نا|ت)?|"
            r"قصتنا|"
            r"نحن\s*هنا\s*لتلبية|"
            r"تقديم\s*تجربة\s*تسوق|"
            r"مجموعة\s*متنوعة\s*من\s*(?:ال)?منتجات|"
            r"منذ\s*\d+\s*(?:سنة|عام)|"
            r"our\s+story|founded\s+in"
            r")",
            re.UNICODE | re.IGNORECASE,
        ),
    ),
)


@dataclass
class MerchantKnowledgeUnknownTruthGuardResult:
    reply: str
    scrubbed: bool = False
    replaced_with_fallback: bool = False
    reasons: List[str] = field(default_factory=list)
    claim_kinds: List[str] = field(default_factory=list)
    compose_source: Optional[str] = None
    fallback_reason: Optional[str] = None
    fallback_action_type: Optional[str] = None


def _retrieval_count(decision_args: Mapping[str, Any], known_facts: Mapping[str, Any]) -> int:
    for source in (decision_args, known_facts):
        raw = source.get("retrieval_count")
        if raw is None:
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            continue
    return 0


def should_apply_merchant_knowledge_unknown_truth_guard(
    *,
    decision_args: Optional[Mapping[str, Any]] = None,
    known_facts: Optional[Mapping[str, Any]] = None,
) -> bool:
    args = dict(decision_args or {})
    facts = dict(known_facts or {})
    topic = str(args.get("topic") or "")
    surface = str(args.get("policy_surface") or "")
    if surface != "merchant_knowledge_section" and not topic.startswith(
        "merchant_knowledge_"
    ):
        return False
    status = str(args.get("merchant_policy_status") or "").strip().upper()
    if status != "UNKNOWN":
        # store_story may only set store_story_status; treat missing as UNKNOWN
        # only when topic is merchant_knowledge_*.
        if status and status != "UNKNOWN":
            return False
        if not status:
            status = "UNKNOWN"
    return _retrieval_count(args, facts) == 0


def detect_unsupported_specificity_kinds(text: str) -> List[str]:
    raw = text or ""
    if not raw.strip():
        return []
    if _HONEST_ABSENCE_RE.search(raw) and not any(
        pat.search(raw) for _kind, pat in _SPECIFICITY_RES[:4]
    ):
        # Honest absence without shipping/policy specificity — allow narrative-free
        # short replies. Still trip fabricated_store_story if mixed in.
        kinds: List[str] = []
        for kind, pat in _SPECIFICITY_RES:
            if kind == "fabricated_store_story" and pat.search(raw):
                kinds.append(kind)
        return kinds
    kinds = []
    for kind, pat in _SPECIFICITY_RES:
        if pat.search(raw):
            kinds.append(kind)
    return kinds


def _chunk_has_kind(chunk: str, kinds: Sequence[str]) -> bool:
    found = detect_unsupported_specificity_kinds(chunk)
    return any(k in found for k in kinds)


def strip_unsupported_specificity_sentences(
    reply: str,
    kinds: Optional[Sequence[str]] = None,
) -> str:
    raw = (reply or "").strip()
    active = list(kinds or detect_unsupported_specificity_kinds(raw))
    if not raw or not active:
        return raw
    kept: List[str] = []
    for chunk in re.split(r"(?<=[.!?؟،])\s+|\n+", raw):
        part = chunk.strip().rstrip("،,.")
        if part and not _chunk_has_kind(part, active):
            kept.append(part)
    return " ".join(kept).strip()


def apply_merchant_knowledge_unknown_truth_guard(
    text: str,
    *,
    decision_args: Optional[Mapping[str, Any]] = None,
    known_facts: Optional[Mapping[str, Any]] = None,
    tenant_id: Optional[int] = None,
    conversation_id: Optional[int] = None,
) -> MerchantKnowledgeUnknownTruthGuardResult:
    raw = text or ""
    if not should_apply_merchant_knowledge_unknown_truth_guard(
        decision_args=decision_args,
        known_facts=known_facts,
    ):
        return MerchantKnowledgeUnknownTruthGuardResult(reply=raw)

    kinds = detect_unsupported_specificity_kinds(raw)
    if not kinds:
        return MerchantKnowledgeUnknownTruthGuardResult(reply=raw)

    scrubbed = strip_unsupported_specificity_sentences(raw, kinds)
    # If scrub left an honest absence line, keep it.
    if scrubbed.strip() and (
        _HONEST_ABSENCE_RE.search(scrubbed)
        or not detect_unsupported_specificity_kinds(scrubbed)
    ):
        logger.info(
            "[MERCHANT_KNOWLEDGE_UNKNOWN_TRUTH_GUARD] scrubbed tenant=%s "
            "conversation_id=%s kinds=%s",
            tenant_id,
            conversation_id,
            ",".join(kinds),
        )
        return MerchantKnowledgeUnknownTruthGuardResult(
            reply=scrubbed,
            scrubbed=True,
            reasons=["unsupported_specificity_scrubbed"],
            claim_kinds=list(kinds),
        )

    # Emergency fallback after scrub emptied or left inventing residue.
    logger.info(
        "[MERCHANT_KNOWLEDGE_UNKNOWN_TRUTH_GUARD] fallback tenant=%s "
        "conversation_id=%s kinds=%s",
        tenant_id,
        conversation_id,
        ",".join(kinds),
    )
    return MerchantKnowledgeUnknownTruthGuardResult(
        reply=_EMERGENCY_FALLBACK_AR,
        scrubbed=True,
        replaced_with_fallback=True,
        reasons=["unsupported_specificity_fallback"],
        claim_kinds=list(kinds),
        compose_source="fallback_deterministic",
        fallback_reason="merchant_policy_unknown_claim",
        fallback_action_type="merchant_knowledge_unknown_honesty",
    )


__all__ = [
    "apply_merchant_knowledge_unknown_truth_guard",
    "detect_unsupported_specificity_kinds",
    "should_apply_merchant_knowledge_unknown_truth_guard",
    "strip_unsupported_specificity_sentences",
    "MerchantKnowledgeUnknownTruthGuardResult",
]
