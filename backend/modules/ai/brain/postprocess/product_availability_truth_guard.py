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
from typing import Any, Dict, Literal, Optional

from modules.ai.brain.postprocess.product_availability_evidence import (
    EVIDENCE_CONFLICT,
    EVIDENCE_RESOLVED_AVAILABLE,
    EVIDENCE_RESOLVED_UNAVAILABLE,
    EVIDENCE_UNKNOWN,
    ProductAvailabilityEvidenceResult,
    evaluate_product_availability_evidence,
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

_CONFLICT_REPLY_AR = (
    "\u062a\u0648\u062c\u062f \u0645\u0639\u0644\u0648\u0645\u0627\u062a \u0645\u062a\u0639\u0627\u0631\u0636\u0629 "
    "\u062d\u0648\u0644 \u0627\u0644\u062a\u0648\u0641\u0631 \u0627\u0644\u062d\u0627\u0644\u064a "
    "\u2014 \u064a\u062e\u062a\u0644\u0641 \u062d\u0633\u0628 \u0627\u0644\u0635\u0646\u0641 \u0623\u0648 "
    "\u0627\u0644\u0648\u0632\u0646. \u0623\u064a \u062d\u062c\u0645 \u062a\u0642\u0635\u062f\u061f"
)

_UNKNOWN_REPLY_AR = (
    "\u0645\u0627 \u0623\u0642\u062f\u0631 \u0623\u0623\u0643\u062f \u0627\u0644\u062a\u0648\u0641\u0631 "
    "\u0627\u0644\u062d\u0627\u0644\u064a \u0628\u062f\u0642\u0629 \u2014 \u0623\u064a \u0645\u0646\u062a\u062c "
    "\u0623\u0648 \u0648\u0632\u0646 \u062a\u0642\u0635\u062f\u061f"
)

_DETERMINISTIC_ALLOW_PATHS = frozenset({
    "notify_me_back_in_stock_ack",
})


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


def _decide_guard_action(
    evidence: ProductAvailabilityEvidenceResult,
    claim_polarity: Optional[str],
) -> str:
    state = evidence.evidence_state
    if state == EVIDENCE_RESOLVED_AVAILABLE and claim_polarity == "positive":
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


def _rewrite_for_action(action: str) -> str:
    if action == "rewrite_conflict":
        return _CONFLICT_REPLY_AR
    if action == "rewrite_unknown":
        return _UNKNOWN_REPLY_AR
    if action in ("rewrite_false_negative", "rewrite_false_positive"):
        return _CONFLICT_REPLY_AR
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
    tenant_id: Optional[int] = None,
    conversation_id: Optional[int] = None,
) -> ProductAvailabilityTruthGuardResult:
    mode = product_availability_guard_mode()
    original = str(reply or "")

    if mode == "off":
        return ProductAvailabilityTruthGuardResult(reply=original, action="disabled")

    try:
        if not original.strip():
            return ProductAvailabilityTruthGuardResult(reply=original, action="allowed")

        path = str(chosen_path or "").strip()
        if path in _DETERMINISTIC_ALLOW_PATHS:
            return ProductAvailabilityTruthGuardResult(reply=original, action="allowed")

        claim_polarity = reply_availability_polarity(original)
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
                would_rewrite=False,
                entity_resolution_mode=evidence.entity.resolution_mode,
                entity_product_id=evidence.entity.product_id,
                entity_confidence=evidence.entity.confidence,
                catalog_checkout=evidence.catalog_checkout,
                kb_polarity=evidence.kb_avail_polarity or "-",
                claim_polarity="-",
                reason="no_availability_claim_wording",
            )
            return ProductAvailabilityTruthGuardResult(
                reply=original,
                action="allowed",
                evidence=evidence,
            )

        guard_action = _decide_guard_action(evidence, claim_polarity)
        would_rw = _would_rewrite(guard_action, mode)

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
            return ProductAvailabilityTruthGuardResult(
                reply=original,
                action=guard_action if would_rw else "allowed",
                replaced=False,
                reason=evidence.reason,
                evidence=evidence,
                availability_claim_blocked=would_rw,
                shadow_mode=(mode == "shadow"),
                would_rewrite=would_rw,
            )

        new_reply = _rewrite_for_action(guard_action)
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
        return ProductAvailabilityTruthGuardResult(reply=original, action="allowed")
