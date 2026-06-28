"""
staff_presence_truth_guard.py
─────────────────────────────
Block unproven staff owner/presence/availability claims in outbound replies.
Preserves configured role/name/contact facts only.
"""
from __future__ import annotations

import logging
import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, Optional

from modules.ai.brain.postprocess.staff_presence_evidence import (
    StaffPresenceEvidence,
    build_grounded_staff_reply,
    evaluate_staff_presence_evidence,
)

logger = logging.getLogger("nahla.brain.postprocess.staff_presence_truth_guard")

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")

_FLAG_FALSY = frozenset({"0", "false", "no", "off"})

_OWNER_CLAIM_RE = re.compile(
    r"(?:"
    r"صاحب(?:\s+)?(?:المتجر|المحل|المنش(?:أ|ا)ة|المؤسسة)"
    r"|مالك(?:\s+)?(?:المتجر|المحل|المنش(?:أ|ا)ة|المؤسسة)?"
    r"|owner(?:\s+of)?(?:\s+the)?\s+store"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_PRESENCE_CLAIM_RE = re.compile(
    r"(?:"
    r"موجود|غير\s+موجود|م(?:ا)?\s+موجود|مشغول|متاح(?:\s+الآن)?|"
    r"مو\s+موجود|ما\s+هو\s+موجود|ما\s+هي\s+موجود(?:ه|ة)?|"
    r"بتلاق(?:ه|ها|هم)?|بتقابل(?:ه|ها|هم)?|راح\s+تلاق(?:ه|ها|هم)?|"
    r"في\s+المعرض\s+ال(?:آ|ا)ن|بالمعرض\s+ال(?:آ|ا)ن|"
    r"بتلاق(?:ه|ها|هم)?\s+في\s+(?:المعرض|الفرع|المحل)|"
    r"ب(?:ي|)رد\s+عليك|راح\s+يرد|يستنا(?:ك|ج)?|مستن(?:ي|يك|اك)"
    r")",
    re.UNICODE | re.IGNORECASE,
)


def staff_presence_guard_enabled() -> bool:
    raw = os.getenv("STAFF_PRESENCE_TRUTH_GUARD_ENABLED", "1").strip().lower()
    return raw not in _FLAG_FALSY


def _norm(text: Optional[str]) -> str:
    if not text or not isinstance(text, str):
        return ""
    t = unicodedata.normalize("NFKC", text)
    t = _NORM_RE.sub("", t)
    t = (
        t.replace("\u0623", "\u0627")
        .replace("\u0625", "\u0627")
        .replace("\u0622", "\u0627")
        .replace("\u0649", "\u064a")
        .replace("\u0629", "\u0647")
    )
    return _WS_RE.sub(" ", t).strip()


def _reply_mentions_configured_staff(
    reply: str,
    evidence: StaffPresenceEvidence,
) -> bool:
    norm = _norm(reply)
    if not norm:
        return False
    for rec in evidence.registry_records:
        for token in rec.all_match_tokens():
            if len(token) >= 2 and token in norm:
                return True
    return False


def _should_apply_guard(
    *,
    inbound_text: str,
    reply: str,
    evidence: StaffPresenceEvidence,
) -> bool:
    if evidence.staff_context_active:
        return True
    if _reply_mentions_configured_staff(reply, evidence):
        return True
    return False


def reply_contains_owner_overclaim(
    reply: str,
    *,
    evidence: StaffPresenceEvidence,
) -> bool:
    if not _OWNER_CLAIM_RE.search(_norm(reply)):
        return False
    record = evidence.matched_record
    if record is not None and record.is_owner:
        return False
    return True


def reply_contains_presence_overclaim(
    reply: str,
    *,
    evidence: StaffPresenceEvidence,
) -> bool:
    if evidence.has_availability_evidence:
        return False
    return bool(_PRESENCE_CLAIM_RE.search(_norm(reply)))


def reply_contains_forbidden_staff_claim(
    reply: str,
    *,
    evidence: StaffPresenceEvidence,
) -> bool:
    if not (reply or "").strip():
        return False
    return (
        reply_contains_owner_overclaim(reply, evidence=evidence)
        or reply_contains_presence_overclaim(reply, evidence=evidence)
    )


@dataclass(frozen=True)
class StaffPresenceTruthGuardResult:
    reply: str
    action: str
    replaced: bool = False
    reason: str = ""
    staff_presence_claim_blocked: bool = False
    evidence: Optional[StaffPresenceEvidence] = None


def guard_metadata_patch(result: StaffPresenceTruthGuardResult) -> Dict[str, Any]:
    if not result.staff_presence_claim_blocked:
        return {}
    return {
        "staff_presence_claim_blocked": True,
        "staff_presence_guard_reason": result.reason or "",
    }


def apply_staff_presence_truth_guard(
    *,
    reply: str,
    inbound_text: str = "",
    db: Any = None,
    tenant_id: Optional[int] = None,
    conversation_id: Optional[int] = None,
    state: Any = None,
    store_contact_phone: str = "",
    registry: Any = None,
) -> StaffPresenceTruthGuardResult:
    original = str(reply or "")
    if not staff_presence_guard_enabled() or not original.strip():
        return StaffPresenceTruthGuardResult(reply=original, action="allowed")

    evidence = evaluate_staff_presence_evidence(
        message=inbound_text or "",
        db=db,
        tenant_id=tenant_id,
        store_contact_phone=store_contact_phone,
        state=state,
        registry=registry,
    )

    if not _should_apply_guard(
        inbound_text=inbound_text or "",
        reply=original,
        evidence=evidence,
    ):
        return StaffPresenceTruthGuardResult(
            reply=original,
            action="allowed",
            evidence=evidence,
            reason="staff_context_inactive",
        )

    if not reply_contains_forbidden_staff_claim(original, evidence=evidence):
        logger.info(
            "[STAFF_PRESENCE_TRUTH_GUARD] allowed tenant=%s conv=%s source=%s",
            tenant_id,
            conversation_id,
            evidence.evidence_source or "-",
        )
        return StaffPresenceTruthGuardResult(
            reply=original,
            action="allowed",
            evidence=evidence,
            reason="no_forbidden_staff_claim",
        )

    grounded = build_grounded_staff_reply(evidence, inbound_text=inbound_text or "")
    reason = "owner_overclaim_blocked"
    if reply_contains_presence_overclaim(original, evidence=evidence):
        reason = "presence_overclaim_blocked"

    logger.info(
        "[STAFF_PRESENCE_TRUTH_GUARD] blocked tenant=%s conv=%s reason=%s preview=%r",
        tenant_id,
        conversation_id,
        reason,
        original[:80],
    )
    return StaffPresenceTruthGuardResult(
        reply=grounded,
        action="blocked_staff_presence_overclaim",
        replaced=True,
        reason=reason,
        staff_presence_claim_blocked=True,
        evidence=evidence,
    )


__all__ = [
    "StaffPresenceTruthGuardResult",
    "apply_staff_presence_truth_guard",
    "guard_metadata_patch",
    "reply_contains_forbidden_staff_claim",
    "reply_contains_owner_overclaim",
    "reply_contains_presence_overclaim",
    "staff_presence_guard_enabled",
]
