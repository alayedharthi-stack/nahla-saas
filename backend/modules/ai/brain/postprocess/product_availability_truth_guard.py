"""
modules/ai/brain/postprocess/product_availability_truth_guard.py
────────────────────────────────────────────────────────────────
Block definitive availability claims when structured evidence is unresolved.

Modes (NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE):
  off     — guard disabled
  shadow  — log only; never rewrite (Phase 0)
  enforce — rewrite CONFLICT and UNKNOWN only (Phase 2)
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional, Sequence

from modules.ai.brain.postprocess.product_availability_evidence import (
    EVIDENCE_CONFLICT,
    EVIDENCE_RESOLVED_AVAILABLE,
    EVIDENCE_RESOLVED_UNAVAILABLE,
    EVIDENCE_UNKNOWN,
    EVIDENCE_VARIANT_OPTIONS,
    ProductAvailabilityEvidenceResult,
    evaluate_product_availability_evidence,
)
from modules.ai.brain.postprocess.availability_guard_policy import (  # noqa: PLC0415
    inbound_asks_stock_or_orderability,
    should_block_availability_rewrite,
)

logger = logging.getLogger("nahla.brain.postprocess.product_availability_truth_guard")

_NORMALISE_AR_RE = re.compile(r"[\u064B-\u065F\u0670]")

_POSITIVE_MARKERS = (
    "\u0645\u062a\u0648\u0641\u0631",
    "\u0645\u062a\u0627\u062d",
    "\u0645\u062a\u0627\u062d\u0647",
    "\u0645\u062a\u0648\u0641\u0631\u0647",
    "available",
    "in stock",
    "\u0645\u062a\u0627\u062d \u0644\u0644\u0637\u0644\u0628",
)

_NEGATIVE_MARKERS = (
    "\u063a\u064a\u0631 \u0645\u062a\u0648\u0641\u0631",
    "\u063a\u064a\u0631 \u0645\u062a\u0627\u062d",
    "\u0646\u0641\u062f",
    "\u0646\u0641\u0630",
    "\u0644\u0627 \u064a\u0648\u062c\u062f",
    "unavailable",
    "out of stock",
)

_LEGACY_CONFLICT_REPLY_AR = (
    "\u062a\u0648\u062c\u062f \u0645\u0639\u0644\u0648\u0645\u0627\u062a \u0645\u062a\u0639\u0627\u0631\u0636\u0629 "
    "\u062d\u0648\u0644 \u0627\u0644\u062a\u0648\u0641\u0631 \u0627\u0644\u062d\u0627\u0644\u064a "
    "\u2014 \u064a\u062e\u062a\u0644\u0641 \u062d\u0633\u0628 \u0627\u0644\u0635\u0646\u0641 \u0623\u0648 "
    "\u0627\u0644\u0648\u0632\u0646. \u0623\u064a \u062d\u062c\u0645 \u062a\u0642\u0635\u062f\u061f"
)

# Backward-compat alias for tests/dedup that reference the legacy canned line.
_CONFLICT_REPLY_AR = _LEGACY_CONFLICT_REPLY_AR

_CUSTOMER_FORBIDDEN_AVAILABILITY_PHRASES: tuple[str, ...] = (
    "معلومات متعارضة",
    "تعارض في البيانات",
    "conflict",
    "MISSING_CATALOG_ENTITY",
    "حسب قاعدة المعرفة",
    "حسب الكتالوج",
    "الكتالوج",
)

# Legacy dry follow-up kept for tests asserting migration away from it.
_LEGACY_DRY_VARIANT_CONFLICT_REPLY_AR = "أي حجم يناسبك؟"

_UNKNOWN_REPLY_AR = (
    "\u0645\u0627 \u0646\u0642\u062f\u0631 \u0646\u0624\u0643\u062f \u0627\u0644\u062a\u0648\u0641\u0631 "
    "\u0628\u062f\u0642\u0629 \u0644\u0647\u0630\u0627 \u0627\u0644\u0645\u0646\u062a\u062c."
)

_INBOUND_AVAIL_PREFIX_RE = re.compile(
    r"^(?:\s*(?:هل|عندكم|عندك|عند|في|لديكم|لديك|يوجد|عندنا)\s+)+",
    re.UNICODE | re.IGNORECASE,
)
_INBOUND_AVAIL_SUFFIX_RE = re.compile(
    r"(?:\s*(?:\?|؟|\.)?\s*(?:متوفر|متاح|موجود|available|in stock|عندكم|عندك)\s*)+$",
    re.UNICODE | re.IGNORECASE,
)
_WEIGHT_YEAR_NOISE_RE = re.compile(
    r"(?:\d+(?:[.,]\d+)?\s*)?(?:نصف\s+|ربع\s+)?(?:كilo|كيلo|كيلو|kg|جرام|gram|grams)\b",
    re.UNICODE | re.IGNORECASE,
)
_YEAR_NOISE_RE = re.compile(r"\b20\d{2}\b")

_POSITIVE_OPTIONS_CLAIM_RE = re.compile(
    r"(?:"
    r"عند(?:نا|كم|ك)\s+(?:\S+\s+){0,3}?(?:تشكيل(?:ه|ة)|خيارات)"
    r"|(?:تشكيل(?:ه|ة)|خيارات)\s+متنو(?:ع(?:ه|ة)?|عة)"
    r"|عند(?:نا|كم|ك)\s+(?:\S+\s+){0,3}?متنو(?:ع(?:ه|ة)?|عة)"
    r"|(?:تقدر|ت(?:قدر|قدم(?:ي|ن)?))\s+(?:\S+\s+){0,4}?(?:تختار|تختاري|اخ(?:تي|ت)ار)"
    r"|ب(?:عدة|عدا)\s+خيارات"
    r"|(?:عدة|عدا)\s+(?:انواع|الانواع|الأنواع|خيارات)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_DETERMINISTIC_ALLOW_PATHS = frozenset({
    "notify_me_back_in_stock_ack",
    "kb_availability_facts",
    "variant_pricing",
})

_TITLE_STOP_TOKENS = frozenset({
    "عسل", "انتاج", "إنتاج", "منحل", "منحلنا", "مناحل", "مناحلنا",
    "نجد", "البري", "البلدي", "جرام", "كيلو", "سطل", "وزن",
})


def _distinctive_title_tokens(title: str) -> set[str]:
    from modules.ai.knowledge.product_matcher import normalize_arabic, tokenize  # noqa: PLC0415

    return {
        t
        for t in tokenize(normalize_arabic(title or ""))
        if len(t) >= 3 and t not in _TITLE_STOP_TOKENS
    }


def _line_references_product_title(line: str, title: str) -> bool:
    from modules.ai.knowledge.product_matcher import normalize_arabic  # noqa: PLC0415

    line_norm = normalize_arabic(line or "")
    if not line_norm:
        return False
    title_norm = normalize_arabic(title or "")
    if title_norm and title_norm in line_norm:
        return True
    toks = _distinctive_title_tokens(title)
    if not toks:
        return False
    hits = sum(1 for t in toks if t in line_norm)
    need = 2 if len(toks) >= 2 else 1
    return hits >= need


def strip_non_checkout_catalog_product_lines(
    reply: str,
    catalog_skus: Optional[Sequence[Dict[str, Any]]],
) -> tuple[str, bool]:
    """Remove lines that name catalog SKUs the customer cannot checkout."""
    original = str(reply or "")
    if not original.strip() or not catalog_skus:
        return original, False

    inactive = [
        sku for sku in catalog_skus
        if sku.get("id") is not None and not bool(sku.get("can_checkout"))
    ]
    if not inactive:
        return original, False

    kept: List[str] = []
    removed = False
    for line in original.splitlines():
        drop = any(
            _line_references_product_title(line, str(sku.get("title") or ""))
            for sku in inactive
        )
        if drop:
            removed = True
            continue
        kept.append(line)

    if not removed:
        return original, False
    return "\n".join(kept).strip(), True


_CATALOG_FACT_QA_KINDS = frozenset({"price", "availability"})


def is_catalog_product_fact_answer_exempt(
    *,
    chosen_path: str = "",
    question_kind: str = "",
    catalog_product_ids: Optional[Sequence[Any]] = None,
    checkout_pressure_allowed: Optional[bool] = None,
    surface: str = "",
) -> bool:
    """Allow non-orderable catalog fact lines for narrow price/availability Q&A."""
    if str(chosen_path or "").strip() != "fact_bound_persona_compose":
        return False
    qkind = str(question_kind or "").strip()
    if qkind not in _CATALOG_FACT_QA_KINDS:
        return False
    surf = str(surface or "").strip()
    if surf and surf != "catalog_product_answer":
        return False
    ids = [x for x in (catalog_product_ids or []) if x is not None]
    if not ids:
        return False
    if checkout_pressure_allowed is not False:
        return False
    return True


def product_availability_guard_mode() -> str:
    mode = os.environ.get(
        "NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE", "off",
    ).strip().lower()
    if mode in ("off", "shadow", "enforce"):
        return mode
    if os.environ.get("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD", "").strip().lower() in (
        "1", "true", "yes",
    ):
        return "shadow"
    return "off"


def _norm(text: Optional[str]) -> str:
    if not text or not isinstance(text, str):
        return ""
    t = _NORMALISE_AR_RE.sub("", text)
    t = t.replace("ـ", "")
    t = (
        t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
         .replace("ى", "ي").replace("ة", "ه")
    )
    return t.lower().strip()


def reply_contains_availability_claim(reply: Optional[str]) -> bool:
    return reply_availability_polarity(reply) is not None


def reply_availability_polarity(
    reply: Optional[str],
) -> Optional[Literal["positive", "negative"]]:
    norm = _norm(reply)
    if not norm:
        return None
    # Negative phrases must win — e.g. "غير متوفر" contains "متوفر".
    if any(_norm(m) in norm for m in _NEGATIVE_MARKERS):
        return "negative"
    if any(_norm(m) in norm for m in _POSITIVE_MARKERS):
        return "positive"
    return None


def reply_positive_options_claim(reply: Optional[str]) -> bool:
    """True when reply implies catalog/options availability without «متوفر»."""
    norm = _norm(reply)
    if not norm:
        return False
    return bool(_POSITIVE_OPTIONS_CLAIM_RE.search(norm))


def reply_implied_positive_availability_claim(reply: Optional[str]) -> bool:
    """True when reply asserts availability or selectable options without evidence."""
    return reply_availability_polarity(reply) == "positive" or reply_positive_options_claim(reply)


def _decide_guard_action(
    evidence: ProductAvailabilityEvidenceResult,
    claim_polarity: Optional[str],
) -> str:
    state = evidence.evidence_state
    if state == EVIDENCE_RESOLVED_AVAILABLE and claim_polarity == "positive":
        return "allowed"
    if state == EVIDENCE_VARIANT_OPTIONS and claim_polarity == "positive":
        return "allowed"
    if state == EVIDENCE_RESOLVED_UNAVAILABLE and claim_polarity == "negative":
        return "allowed"
    if state == EVIDENCE_CONFLICT:
        return "rewrite_conflict"
    if state == EVIDENCE_UNKNOWN:
        return "rewrite_unknown"
    if state == EVIDENCE_RESOLVED_AVAILABLE and claim_polarity == "negative":
        return "rewrite_false_negative"
    if state == EVIDENCE_RESOLVED_UNAVAILABLE and claim_polarity == "positive":
        return "rewrite_false_positive"
    return "allowed"


def customer_facing_availability_reply_is_clean(reply: Optional[str]) -> bool:
    """True when outbound availability wording avoids internal/system phrases."""
    text = str(reply or "")
    if not text.strip():
        return True
    lower = text.lower()
    return not any(
        phrase.lower() in lower if phrase.isascii() else phrase in text
        for phrase in _CUSTOMER_FORBIDDEN_AVAILABILITY_PHRASES
    )


def _customer_product_label(title: str) -> str:
    label = _WEIGHT_YEAR_NOISE_RE.sub(" ", title or "")
    label = _YEAR_NOISE_RE.sub(" ", label)
    label = re.sub(r"\s+", " ", label).strip(" -–،,.")
    return label[:48].strip()


def _label_from_inbound_availability_ask(inbound_text: str) -> str:
    from modules.ai.brain.commerce.product_label_hygiene import (  # noqa: PLC0415
        is_non_product_label,
        normalize_label_text,
    )
    from modules.ai.brain.commerce.staff_contact_product_label_guard import (  # noqa: PLC0415
        is_staff_or_contact_label,
    )

    raw = (inbound_text or "").strip()
    if not raw:
        return ""
    if is_staff_or_contact_label(raw):
        return ""
    cleaned = _INBOUND_AVAIL_PREFIX_RE.sub("", raw)
    cleaned = _INBOUND_AVAIL_SUFFIX_RE.sub("", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip("؟? ")
    cleaned = normalize_label_text(cleaned)
    if is_staff_or_contact_label(cleaned):
        return ""
    word_count = len([w for w in cleaned.split() if w])
    if (
        2 <= len(cleaned) <= 40
        and word_count <= 5
        and not is_non_product_label(cleaned)
    ):
        return cleaned
    return ""


def _label_from_family_members(
    members: Sequence[Dict[str, Any]],
    inbound_text: str,
) -> str:
    inbound_label = _label_from_inbound_availability_ask(inbound_text)
    if inbound_label:
        return inbound_label
    if not members:
        return ""
    titles = [str(p.get("title") or "").strip() for p in members if p.get("title")]
    if not titles:
        return ""
    if len(titles) == 1:
        return _customer_product_label(titles[0])
    prefix = os.path.commonprefix(titles).strip()
    if len(prefix) >= 4:
        return _customer_product_label(prefix)
    first = _customer_product_label(titles[0])
    return first or ""


def _product_label_for_reply(
    evidence: ProductAvailabilityEvidenceResult,
    availability_context: Optional[Dict[str, Any]],
    inbound_text: str,
) -> str:
    from modules.ai.brain.commerce.product_label_hygiene import sanitize_product_label  # noqa: PLC0415

    ctx = availability_context or {}
    catalog_by_id = {
        int(p["id"]): p
        for p in (ctx.get("catalog_skus") or [])
        if p.get("id") is not None
    }

    focus = ctx.get("focus_product") or {}
    focus_title = ""
    if isinstance(focus, dict):
        focus_title = sanitize_product_label(str(focus.get("title") or ""))
        if focus_title:
            return focus_title

    pid = evidence.entity.product_id
    if pid is not None and pid in catalog_by_id:
        title = sanitize_product_label(str(catalog_by_id[pid].get("title") or ""))
        if title:
            return title

    family_key = str(evidence.entity.family_key or "").strip()
    if family_key and not family_key.startswith("inbound:"):
        members = [
            p for p in (ctx.get("catalog_skus") or [])
            if str(p.get("family_key") or "") == family_key
        ]
        label = _label_from_family_members(members, inbound_text)
        label = sanitize_product_label(label, fallback=focus_title)
        if label:
            return label

    inbound_label = _label_from_inbound_availability_ask(inbound_text)
    return sanitize_product_label(inbound_label, fallback=focus_title)


def build_operational_availability_conflict_reply(
    evidence: ProductAvailabilityEvidenceResult,
    *,
    availability_context: Optional[Dict[str, Any]] = None,
    inbound_text: str = "",
) -> str:
    """
    Operational-only rewrite when catalog evidence supports a positive claim.

    CONFLICT / UNKNOWN / unresolved entities must never invent «متوفر … بعدة
    خيارات» — personality is applied later by commerce style compose.
    """
    from modules.ai.brain.commerce.staff_contact_product_label_guard import (  # noqa: PLC0415
        should_block_product_availability_rewrite,
    )

    label = _product_label_for_reply(evidence, availability_context, inbound_text)
    if should_block_product_availability_rewrite(
        inbound_text,
        label=label,
        guard_action="rewrite_conflict",
    ):
        logger.info(
            "[PRODUCT_AVAILABILITY_TRUTH_GUARD] blocked_staff_contact_rewrite "
            "inbound=%r label=%r",
            (inbound_text or "")[:80],
            (label or "")[:80],
        )
        return ""

    state = str(getattr(evidence, "evidence_state", "") or "")
    ok_pos = bool(getattr(evidence, "evidence_ok_for_positive", False))

    if state in (EVIDENCE_CONFLICT, EVIDENCE_UNKNOWN):
        return _UNKNOWN_REPLY_AR

    if state == EVIDENCE_RESOLVED_UNAVAILABLE:
        return _UNKNOWN_REPLY_AR

    if state == EVIDENCE_VARIANT_OPTIONS and ok_pos:
        if label:
            return f"متوفر {label} بعدة خيارات."
        return "متوفر بعدة خيارات."

    if state == EVIDENCE_RESOLVED_AVAILABLE and ok_pos:
        if label:
            return f"متوفر {label}."
        return "متوفر."

    return ""


def build_friendly_availability_conflict_reply(
    evidence: ProductAvailabilityEvidenceResult,
    *,
    availability_context: Optional[Dict[str, Any]] = None,
    inbound_text: str = "",
) -> str:
    """Backward-compatible alias — operational facts only."""
    return build_operational_availability_conflict_reply(
        evidence,
        availability_context=availability_context,
        inbound_text=inbound_text,
    )


def _rewrite_for_action(
    action: str,
    *,
    evidence: Optional[ProductAvailabilityEvidenceResult] = None,
    availability_context: Optional[Dict[str, Any]] = None,
    inbound_text: str = "",
) -> str:
    if action in ("rewrite_conflict", "rewrite_unknown", "rewrite_false_positive"):
        if evidence is not None:
            return build_operational_availability_conflict_reply(
                evidence,
                availability_context=availability_context,
                inbound_text=inbound_text,
            )
        return _UNKNOWN_REPLY_AR
    if action == "rewrite_false_negative":
        if (
            evidence is not None
            and evidence.evidence_state == EVIDENCE_RESOLVED_AVAILABLE
            and evidence.evidence_ok_for_positive
        ):
            return build_operational_availability_conflict_reply(
                evidence,
                availability_context=availability_context,
                inbound_text=inbound_text,
            )
        if evidence is not None:
            return build_operational_availability_conflict_reply(
                evidence,
                availability_context=availability_context,
                inbound_text=inbound_text,
            )
        return _UNKNOWN_REPLY_AR
    return ""


def _would_rewrite(action: str, mode: str) -> bool:
    if action == "allowed":
        return False
    if mode == "shadow":
        return True
    if mode == "enforce":
        return action in ("rewrite_conflict", "rewrite_unknown")
    return False


@dataclass(frozen=True)
class ProductAvailabilityTruthGuardResult:
    reply: str
    action: str
    replaced: bool = False
    reason: str = ""
    evidence: Optional[ProductAvailabilityEvidenceResult] = None
    availability_claim_blocked: bool = False
    shadow_mode: bool = False
    would_rewrite: bool = False


def log_product_availability_truth_guard(
    *,
    tenant_id: Optional[int],
    conversation_id: Optional[int],
    evidence_state: str,
    conflict_type: str,
    guard_mode: str,
    guard_action: str,
    would_rewrite: bool,
    entity_resolution_mode: str,
    entity_product_id: Optional[int],
    entity_confidence: float,
    catalog_checkout: Optional[bool],
    kb_polarity: str,
    claim_polarity: str,
    reason: str,
) -> None:
    try:
        logger.info(
            "[PRODUCT_AVAILABILITY_TRUTH_GUARD] tenant_id=%s conversation_id=%s "
            "PRODUCT_AVAILABILITY_EVIDENCE=%s "
            "PRODUCT_AVAILABILITY_CONFLICT=%s "
            "PRODUCT_AVAILABILITY_GUARD_MODE=%s "
            "PRODUCT_AVAILABILITY_GUARD_ACTION=%s "
            "PRODUCT_AVAILABILITY_GUARD_WOULD_REWRITE=%s "
            "entity_resolution_mode=%s entity_product_id=%s "
            "entity_confidence=%.2f catalog_checkout=%s kb_polarity=%s "
            "claim_polarity=%s reason=%s",
            tenant_id,
            conversation_id,
            evidence_state or "-",
            conflict_type or "-",
            guard_mode or "-",
            guard_action or "-",
            "YES" if would_rewrite else "NO",
            entity_resolution_mode or "-",
            entity_product_id if entity_product_id is not None else "-",
            entity_confidence,
            catalog_checkout if catalog_checkout is not None else "-",
            kb_polarity or "-",
            claim_polarity or "-",
            reason or "-",
        )
    except Exception:  # noqa: BLE001
        pass


def apply_product_availability_truth_guard(
    *,
    reply: str,
    availability_context: Optional[Dict[str, Any]] = None,
    inbound_text: str = "",
    chosen_path: str = "",
    decision_topic: str = "",
    tenant_id: Optional[int] = None,
    conversation_id: Optional[int] = None,
    question_kind: str = "",
    catalog_product_ids: Optional[Sequence[Any]] = None,
    checkout_pressure_allowed: Optional[bool] = None,
    surface: str = "",
    invocation_site: str = "unknown",
    turn_token: str = "",
) -> ProductAvailabilityTruthGuardResult:
    from modules.ai.brain.postprocess.product_availability_shadow_telemetry import (  # noqa: PLC0415
        ShadowObservationTimer,
        build_shadow_observation,
        emit_shadow_observation,
    )

    mode = product_availability_guard_mode()
    original = str(reply or "")
    _shadow_timer = ShadowObservationTimer() if mode == "shadow" else None

    def _emit_shadow(
        *,
        evidence_state: str,
        conflict_type: str,
        guard_action: str,
        would_rewrite: bool,
        reason: str,
        customer_text_changed: bool,
    ) -> None:
        if mode != "shadow" or _shadow_timer is None:
            return
        emit_shadow_observation(
            build_shadow_observation(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                invocation_site=invocation_site,
                guard_mode=mode,
                evidence_state=evidence_state,
                conflict_type=conflict_type,
                guard_action=guard_action,
                would_rewrite=would_rewrite,
                reason=reason,
                customer_text_changed=customer_text_changed,
                guard_duration_ms=_shadow_timer.elapsed_ms(),
                turn_token=turn_token,
            )
        )

    if mode == "off":
        return ProductAvailabilityTruthGuardResult(reply=original, action="disabled")

    topic = str(decision_topic or "").strip()
    if topic == "product_knowledge_facts":
        _emit_shadow(
            evidence_state="-",
            conflict_type="-",
            guard_action="allowed_product_knowledge_facts",
            would_rewrite=False,
            reason="product_knowledge_facts_bypass",
            customer_text_changed=False,
        )
        return ProductAvailabilityTruthGuardResult(
            reply=original,
            action="allowed_product_knowledge_facts",
        )
    try:
        from modules.ai.brain.commerce.product_knowledge_or_comparison import (  # noqa: PLC0415
            TOPIC_PRODUCT_KNOWLEDGE_FACTS,
            is_product_knowledge_message,
        )

        if topic == TOPIC_PRODUCT_KNOWLEDGE_FACTS or is_product_knowledge_message(
            inbound_text,
        ):
            _emit_shadow(
                evidence_state="-",
                conflict_type="-",
                guard_action="allowed_product_knowledge_facts",
                would_rewrite=False,
                reason="product_knowledge_facts_bypass",
                customer_text_changed=False,
            )
            return ProductAvailabilityTruthGuardResult(
                reply=original,
                action="allowed_product_knowledge_facts",
            )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional product-knowledge probe
        pass

    from modules.ai.brain.turn_owner_contract import TOPIC_SHIPPING  # noqa: PLC0415

    if topic == TOPIC_SHIPPING:
        _emit_shadow(
            evidence_state="-",
            conflict_type="-",
            guard_action="allowed_shipping_inquiry",
            would_rewrite=False,
            reason="shipping_inquiry_bypass",
            customer_text_changed=False,
        )
        return ProductAvailabilityTruthGuardResult(
            reply=original,
            action="allowed_shipping_inquiry",
            reason="shipping_inquiry_bypass",
        )

    try:
        if not original.strip():
            _emit_shadow(
                evidence_state="-",
                conflict_type="-",
                guard_action="allowed",
                would_rewrite=False,
                reason="empty_reply",
                customer_text_changed=False,
            )
            return ProductAvailabilityTruthGuardResult(reply=original, action="allowed")

        path = str(chosen_path or "").strip()
        if path in _DETERMINISTIC_ALLOW_PATHS:
            _emit_shadow(
                evidence_state="-",
                conflict_type="-",
                guard_action="allowed",
                would_rewrite=False,
                reason=f"deterministic_allow_path:{path}",
                customer_text_changed=False,
            )
            return ProductAvailabilityTruthGuardResult(reply=original, action="allowed")

        catalog_skus = list((availability_context or {}).get("catalog_skus") or [])
        working = original
        stripped_inactive = False
        _catalog_fact_exempt = is_catalog_product_fact_answer_exempt(
            chosen_path=path,
            question_kind=question_kind,
            catalog_product_ids=catalog_product_ids,
            checkout_pressure_allowed=checkout_pressure_allowed,
            surface=surface,
        )
        # Existence listings are not checkout claims. Only strip inactive SKUs
        # when the inbound asked stock/orderability (e.g. «وش المتوفر»).
        _strip_inactive = inbound_asks_stock_or_orderability(inbound_text)
        if mode == "enforce" and not _catalog_fact_exempt and _strip_inactive:
            working, stripped_inactive = strip_non_checkout_catalog_product_lines(
                working, catalog_skus,
            )
        elif mode == "shadow" and not _catalog_fact_exempt and _strip_inactive:
            _, stripped_inactive = strip_non_checkout_catalog_product_lines(
                original, catalog_skus,
            )

        if stripped_inactive and mode == "enforce":
            log_product_availability_truth_guard(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                evidence_state="-",
                conflict_type="-",
                guard_mode=mode,
                guard_action="strip_inactive_catalog_lines",
                would_rewrite=False,
                entity_resolution_mode="-",
                entity_product_id=None,
                entity_confidence=0.0,
                catalog_checkout=None,
                kb_polarity="-",
                claim_polarity="-",
                reason="removed_non_checkout_catalog_product_lines",
            )
            return ProductAvailabilityTruthGuardResult(
                reply=working,
                action="strip_inactive_catalog_lines",
                replaced=True,
                reason="removed_non_checkout_catalog_product_lines",
                availability_claim_blocked=True,
            )

        if _catalog_fact_exempt:
            _emit_shadow(
                evidence_state="-",
                conflict_type="-",
                guard_action="allowed_catalog_product_fact_answer",
                would_rewrite=False,
                reason="catalog_product_fact_answer_exempt",
                customer_text_changed=False,
            )
            return ProductAvailabilityTruthGuardResult(
                reply=working,
                action="allowed_catalog_product_fact_answer",
                replaced=False,
                reason="catalog_product_fact_answer_exempt",
            )

        claim_polarity = reply_availability_polarity(working)
        if claim_polarity is None and reply_positive_options_claim(working):
            # «عندنا خيارات» is an availability claim only when the inbound
            # asked stock/orderability or a specific-subject existence
            # question. Open catalog browse and gift/recommendation turns
            # must not be rewritten into the canned UNKNOWN line.
            inbound_is_availability_ask = inbound_asks_stock_or_orderability(
                inbound_text,
            )
            if not inbound_is_availability_ask:
                try:
                    from modules.ai.brain.commerce.commerce_inquiry_boundary import (  # noqa: PLC0415
                        CommerceTurnKind,
                        classify_commerce_turn_kind,
                    )

                    inbound_is_availability_ask = (
                        classify_commerce_turn_kind(inbound_text)
                        == CommerceTurnKind.AVAILABILITY
                    )
                except Exception:  # noqa: BLE001
                    inbound_is_availability_ask = False
            if inbound_is_availability_ask:
                claim_polarity = "positive"
        evidence = evaluate_product_availability_evidence(
            availability_context=availability_context,
            inbound_text=inbound_text,
        )

        if claim_polarity is None:
            log_product_availability_truth_guard(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                evidence_state=evidence.evidence_state,
                conflict_type=evidence.conflict_type or "-",
                guard_mode=mode,
                guard_action="allowed",
                would_rewrite=stripped_inactive,
                entity_resolution_mode=evidence.entity.resolution_mode,
                entity_product_id=evidence.entity.product_id,
                entity_confidence=evidence.entity.confidence,
                catalog_checkout=evidence.catalog_checkout,
                kb_polarity=evidence.kb_avail_polarity or "-",
                claim_polarity="-",
                reason=(
                    "shadow_would_strip_inactive_catalog_lines"
                    if stripped_inactive
                    else "no_availability_claim_wording"
                ),
            )
            _emit_shadow(
                evidence_state=evidence.evidence_state,
                conflict_type=evidence.conflict_type or "-",
                guard_action="allowed",
                would_rewrite=stripped_inactive,
                reason=(
                    "shadow_would_strip_inactive_catalog_lines"
                    if stripped_inactive
                    else "no_availability_claim_wording"
                ),
                customer_text_changed=False,
            )
            return ProductAvailabilityTruthGuardResult(
                reply=original,
                action="allowed",
                evidence=evidence,
                shadow_mode=(mode == "shadow"),
                would_rewrite=stripped_inactive,
                availability_claim_blocked=stripped_inactive,
            )

        guard_action = _decide_guard_action(evidence, claim_polarity)
        would_rw = _would_rewrite(guard_action, mode) or stripped_inactive

        if should_block_availability_rewrite(
            inbound_text=inbound_text,
            evidence_state=evidence.evidence_state,
            guard_action=guard_action,
            availability_context=availability_context,
        ):
            would_rw = False
            if guard_action.startswith("rewrite"):
                guard_action = "allowed"

        log_product_availability_truth_guard(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            evidence_state=evidence.evidence_state,
            conflict_type=evidence.conflict_type or "-",
            guard_mode=mode,
            guard_action=guard_action,
            would_rewrite=would_rw,
            entity_resolution_mode=evidence.entity.resolution_mode,
            entity_product_id=evidence.entity.product_id,
            entity_confidence=evidence.entity.confidence,
            catalog_checkout=evidence.catalog_checkout,
            kb_polarity=evidence.kb_avail_polarity or "-",
            claim_polarity=claim_polarity or "-",
            reason=evidence.reason,
        )

        if mode == "shadow" or not would_rw:
            _emit_shadow(
                evidence_state=evidence.evidence_state,
                conflict_type=evidence.conflict_type or "-",
                guard_action=guard_action if would_rw else "allowed",
                would_rewrite=would_rw,
                reason=evidence.reason,
                customer_text_changed=False,
            )
            return ProductAvailabilityTruthGuardResult(
                reply=original if mode == "shadow" else working,
                action=guard_action if would_rw else "allowed",
                replaced=False,
                reason=evidence.reason,
                evidence=evidence,
                availability_claim_blocked=would_rw,
                shadow_mode=(mode == "shadow"),
                would_rewrite=would_rw,
            )

        new_reply = _rewrite_for_action(
            guard_action,
            evidence=evidence,
            availability_context=availability_context,
            inbound_text=inbound_text,
        )
        if not str(new_reply or "").strip():
            new_reply = _UNKNOWN_REPLY_AR
        return ProductAvailabilityTruthGuardResult(
            reply=new_reply,
            action=guard_action,
            replaced=True,
            reason=evidence.reason,
            evidence=evidence,
            availability_claim_blocked=True,
            shadow_mode=False,
            would_rewrite=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[PRODUCT_AVAILABILITY_TRUTH_GUARD] guard failed tenant=%s err=%s",
            tenant_id,
            exc,
        )
        _emit_shadow(
            evidence_state="-",
            conflict_type="-",
            guard_action="allowed",
            would_rewrite=False,
            reason="guard_exception_fail_open",
            customer_text_changed=False,
        )
        return ProductAvailabilityTruthGuardResult(reply=original, action="allowed")
