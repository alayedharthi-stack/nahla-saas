"""
modules/ai/brain/postprocess/customer_address_save_claim_guard.py
─────────────────────────────────────────────────────────────────
Block outbound "your address is saved / adopted as your delivery address"
claims that no committed customer-address evidence supports.

Claim Rule (AGENTS.md): if the system claims something happened, evidence
or state must exist. A conversation-state write, a bridge skip, an
unavailable capability, an ambiguous identity or a failed commit are all
narrower than a durable customer-address save — and none of them may be
promoted into one by later composition or humanization.

The guard never authors customer-facing prose. It removes the unsupported
claim and leaves the rest of the LLM's reply intact, the same mechanic as
``shipment_truth_guard``.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Optional, Tuple

from core.customer_address_persistence_evidence import (
    AddressPersistenceScope,
    CustomerAddressPersistenceEvidence,
)

logger = logging.getLogger("nahla.brain.postprocess.customer_address_save_claim_guard")

CLAIM_KIND_SAVED = "address_saved"
CLAIM_KIND_ADOPTED = "address_adopted_as_default"

_DIA = re.compile(r"[ً-ٰٟ]")
_WS = re.compile(r"\s+")


def _norm(text: str) -> str:
    if not text:
        return ""
    value = unicodedata.normalize("NFKC", text)
    value = _DIA.sub("", value)
    value = (
        value.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
        .replace("ى", "ي").replace("ة", "ه")
    )
    return _WS.sub(" ", value).strip().lower()


# "the address is saved / stored / registered with us"
_SAVED_RES: Tuple[re.Pattern, ...] = (
    re.compile(
        r"(?:تم|تمت)\s*(?:حفظ|تسجيل|تخزين|اضافه|إضافة)\s*(?:ال)?عنوان",
        re.UNICODE | re.IGNORECASE,
    ),
    re.compile(
        r"(?:حفظنا|سجلنا|خزنا|خزننا|اضفنا|أضفنا)\s*(?:لك|لكم)?\s*(?:ال)?عنوان",
        re.UNICODE | re.IGNORECASE,
    ),
    re.compile(
        r"(?:ال)?عنوان\s*(?:محفوظ|مسجل|مخزن|انحفظ|اتسجل)",
        re.UNICODE | re.IGNORECASE,
    ),
    re.compile(
        r"(?:address|it)\s+(?:is|has\s+been|was)\s+saved",
        re.UNICODE | re.IGNORECASE,
    ),
    re.compile(r"saved\s+(?:your\s+)?address", re.UNICODE | re.IGNORECASE),
)

# "…as your default / permanent delivery address"
_ADOPTED_RES: Tuple[re.Pattern, ...] = (
    re.compile(
        r"(?:عنوان(?:ك|كم)?)\s*(?:ال)?(?:افتراضي|اساسي|أساسي|الدائم|دائم)",
        re.UNICODE | re.IGNORECASE,
    ),
    re.compile(
        r"(?:ال)?عنوان\s*(?:ال)?(?:افتراضي|اساسي|أساسي)\s*(?:عندنا|لديكم|لك|لكم)?",
        re.UNICODE | re.IGNORECASE,
    ),
    re.compile(
        r"(?:تم|تمت)\s*(?:اعتماد|إعتماد|تثبيت)\s*(?:ال)?عنوان",
        re.UNICODE | re.IGNORECASE,
    ),
    re.compile(
        r"(?:default|permanent)\s+(?:delivery\s+)?address",
        re.UNICODE | re.IGNORECASE,
    ),
)


def detect_address_save_claim_kinds(reply: str) -> Tuple[str, ...]:
    text = (reply or "").strip()
    if not text:
        return ()
    norm = _norm(text)
    kinds: list[str] = []
    if any(pattern.search(norm) for pattern in _SAVED_RES):
        kinds.append(CLAIM_KIND_SAVED)
    if any(pattern.search(norm) for pattern in _ADOPTED_RES):
        kinds.append(CLAIM_KIND_ADOPTED)
    return tuple(kinds)


def _chunk_has_claim(chunk: str, kinds: Tuple[str, ...]) -> bool:
    found = detect_address_save_claim_kinds(chunk)
    return any(kind in kinds for kind in found)


def strip_unsupported_address_save_sentences(
    reply: str,
    kinds: Tuple[str, ...],
) -> str:
    """Remove the sentences carrying the unsupported claim; keep the rest."""
    raw = (reply or "").strip()
    if not raw or not kinds:
        return raw
    kept: list[str] = []
    for chunk in re.split(r"(?<=[.!?؟،])\s+|\n+", raw):
        part = chunk.strip().rstrip("،,.")
        if part and not _chunk_has_claim(part, kinds):
            kept.append(part)
    return " ".join(kept).strip()


@dataclass(frozen=True)
class AddressSaveClaimGuardResult:
    reply: str
    action: str
    replaced: bool = False
    reason: str = ""
    blocked_claims: Tuple[str, ...] = ()
    scrubbed_empty: bool = False
    evidence_scope: str = AddressPersistenceScope.NONE.value


def apply_customer_address_save_claim_guard(
    *,
    reply: str,
    evidence: Optional[CustomerAddressPersistenceEvidence] = None,
    tenant_id: Optional[int] = None,
    conversation_id: Optional[int] = None,
) -> AddressSaveClaimGuardResult:
    """Fail-open guard: only ever removes claims the evidence cannot carry."""
    try:
        original = str(reply or "")
        if not original.strip():
            return AddressSaveClaimGuardResult(reply=original, action="allowed")

        claims = detect_address_save_claim_kinds(original)
        scope = (
            evidence.scope.value
            if evidence is not None
            else AddressPersistenceScope.NONE.value
        )
        if not claims:
            return AddressSaveClaimGuardResult(
                reply=original,
                action="allowed",
                reason="no_address_save_claims",
                evidence_scope=scope,
            )

        allows_saved = evidence is not None and evidence.allows_saved_address_claim()
        allows_adopted = evidence is not None and evidence.allows_adopted_address_claim()

        blocked: list[str] = []
        if CLAIM_KIND_SAVED in claims and not allows_saved:
            blocked.append(CLAIM_KIND_SAVED)
        if CLAIM_KIND_ADOPTED in claims and not allows_adopted:
            blocked.append(CLAIM_KIND_ADOPTED)

        if not blocked:
            return AddressSaveClaimGuardResult(
                reply=original,
                action="allowed",
                reason=(evidence.reason if evidence is not None else ""),
                evidence_scope=scope,
            )

        kinds = tuple(blocked)
        scrubbed = strip_unsupported_address_save_sentences(original, kinds)
        logger.info(
            "[CUSTOMER_ADDRESS_SAVE_CLAIM_GUARD] blocked tenant=%s conversation=%s "
            "scope=%s claims=%s reason=%s",
            tenant_id,
            conversation_id,
            scope,
            kinds,
            evidence.reason if evidence is not None else "no_evidence",
        )
        return AddressSaveClaimGuardResult(
            reply=scrubbed,
            action="blocked_unsupported_address_save_claim",
            replaced=(scrubbed != original),
            reason=(evidence.reason if evidence is not None else "no_evidence"),
            blocked_claims=kinds,
            scrubbed_empty=not scrubbed.strip(),
            evidence_scope=scope,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[CUSTOMER_ADDRESS_SAVE_CLAIM_GUARD] guard failed tenant=%s err=%s",
            tenant_id,
            exc,
        )
        return AddressSaveClaimGuardResult(reply=str(reply or ""), action="allowed")


def resolve_and_apply_customer_address_save_claim_guard(
    *,
    db: Any,
    reply: str,
    tenant_id: Optional[int],
    customer_id: Optional[int],
    conversation_id: Optional[int] = None,
) -> AddressSaveClaimGuardResult:
    """Resolve committed evidence for the customer, then guard the reply.

    The evidence is read from the database, never from the turn's own state
    or from what a writer claimed to have done.
    """
    if not detect_address_save_claim_kinds(reply):
        return AddressSaveClaimGuardResult(reply=str(reply or ""), action="allowed")

    from core.customer_address_persistence_evidence import (  # noqa: PLC0415
        resolve_customer_address_persistence_evidence,
    )

    evidence = resolve_customer_address_persistence_evidence(
        db,
        tenant_id=tenant_id,
        customer_id=customer_id,
    )
    return apply_customer_address_save_claim_guard(
        reply=reply,
        evidence=evidence,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
    )


__all__ = [
    "AddressSaveClaimGuardResult",
    "CLAIM_KIND_ADOPTED",
    "CLAIM_KIND_SAVED",
    "apply_customer_address_save_claim_guard",
    "detect_address_save_claim_kinds",
    "resolve_and_apply_customer_address_save_claim_guard",
    "strip_unsupported_address_save_sentences",
]
