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


# Two semantic classes, deliberately kept to two. These are not a phrase
# blacklist to grow: each pattern covers a verb family plus the Arabic
# attached-pronoun forms of "address" (عنوان / عنوانك / عنوانكم / العنوان),
# which is why the earlier, narrower versions missed "عنوانك محفوظ" and the
# combined "…واعتماده…".
_ADDRESS = r"(?:ال)?عنوان(?:ك|كم|نا|هم|ه|ها)?"

# Class 1 — "the address was saved / stored / registered".
_SAVED_RES: Tuple[re.Pattern, ...] = (
    re.compile(
        rf"(?:تم|تمت)\s*(?:حفظ|تسجيل|تخزين|اضافه|اضافة)\s*{_ADDRESS}",
        re.UNICODE | re.IGNORECASE,
    ),
    re.compile(
        rf"(?:حفظنا|سجلنا|خزنا|خزننا|اضفنا)\s*(?:لك|لكم)?\s*{_ADDRESS}",
        re.UNICODE | re.IGNORECASE,
    ),
    re.compile(
        rf"{_ADDRESS}\s*(?:محفوظ|مسجل|مخزن|انحفظ|اتسجل)",
        re.UNICODE | re.IGNORECASE,
    ),
    re.compile(
        r"(?:address|it)\s+(?:is|has\s+been|was)\s+saved",
        re.UNICODE | re.IGNORECASE,
    ),
    re.compile(r"saved\s+(?:your\s+)?address", re.UNICODE | re.IGNORECASE),
)

# Class 2 — "…adopted / set as the delivery or default address".
_ADOPTED_RES: Tuple[re.Pattern, ...] = (
    re.compile(
        rf"{_ADDRESS}\s*(?:ال)?(?:افتراضي|اساسي|الدائم|دائم)",
        re.UNICODE | re.IGNORECASE,
    ),
    re.compile(
        rf"(?:تم|تمت)\s*(?:اعتماد|تثبيت|اختيار)\s*{_ADDRESS}",
        re.UNICODE | re.IGNORECASE,
    ),
    # "…واعتماده للتوصيل" — the adoption rides an attached pronoun rather
    # than repeating the noun, which the noun-anchored patterns miss.
    re.compile(
        r"(?:و)?(?:اعتماد|تثبيت|اختيار)(?:ه|ها|هم)\s*(?:ك|ل)?\s*"
        rf"(?:{_ADDRESS}|للتوصيل|كعنوان)",
        re.UNICODE | re.IGNORECASE,
    ),
    re.compile(
        rf"(?:اعتمدنا|ثبتنا)\s*(?:لك|لكم)?\s*{_ADDRESS}",
        re.UNICODE | re.IGNORECASE,
    ),
    re.compile(
        r"(?:default|permanent)\s+(?:delivery\s+)?address",
        re.UNICODE | re.IGNORECASE,
    ),
)

# A successful-action ASSERTION is the only thing this guard judges. A
# question ("do you want us to…?") and a negation ("your address was NOT
# saved") are truthful LLM text that happen to contain the same words;
# deleting them was the guard turning honest wording into silence.
_INTERROGATIVE_RES: Tuple[re.Pattern, ...] = (
    re.compile(r"[?؟]", re.UNICODE),
    re.compile(r"(?:^|\s)(?:هل|وش|ايش|كيف|متي|وين|هل\s*تريد)(?:\s|$)", re.UNICODE),
    re.compile(r"(?:^|\s)(?:do|did|would|shall|can|should)\s+(?:you|we)\b", re.IGNORECASE),
)

_NEGATION_RES: Tuple[re.Pattern, ...] = (
    re.compile(r"(?:^|\s)(?:لم|لن|ما|مو|مب|ليس|بدون|غير)(?:\s|$)", re.UNICODE),
    re.compile(r"(?:^|\s)لا\s*(?:يوجد|يمكن|نستطيع|زال)", re.UNICODE),
    re.compile(r"\b(?:not|no|never|cannot|can't|couldn't|didn't|won't)\b", re.IGNORECASE),
)


def is_successful_action_assertion(text: str) -> bool:
    """False for questions and negations — they assert no completed action."""
    norm = _norm(text)
    if not norm:
        return False
    if any(p.search(norm) for p in _INTERROGATIVE_RES):
        return False
    if any(p.search(norm) for p in _NEGATION_RES):
        return False
    return True


def detect_address_save_claim_kinds(reply: str) -> Tuple[str, ...]:
    """Claim kinds asserted as COMPLETED actions, per sentence.

    Judged sentence by sentence: one sentence asking a question does not
    excuse another one asserting a save, and a negated sentence is not a
    claim at all.
    """
    text = (reply or "").strip()
    if not text:
        return ()
    kinds: list[str] = []
    for sentence in _sentences(text):
        if not is_successful_action_assertion(sentence):
            continue
        norm = _norm(sentence)
        if CLAIM_KIND_SAVED not in kinds and any(p.search(norm) for p in _SAVED_RES):
            kinds.append(CLAIM_KIND_SAVED)
        if CLAIM_KIND_ADOPTED not in kinds and any(p.search(norm) for p in _ADOPTED_RES):
            kinds.append(CLAIM_KIND_ADOPTED)
    return tuple(kinds)


def _sentences(text: str) -> list:
    return [c.strip() for c in re.split(r"(?<=[.!?؟،])\s+|\n+", text) if c.strip()]


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
    attempt: Any = None,
) -> AddressSaveClaimGuardResult:
    """Resolve committed evidence for THIS turn's operation, then guard.

    ``attempt`` names the save/adoption the turn actually performed. It is
    the only thing that can support a save claim: the evidence is read back
    from the database on an independent connection, bound to that operation's
    address and revision. With no attempt — the normal turn, which performs
    no address save — nothing supports such a claim, so one is removed.
    """
    if not detect_address_save_claim_kinds(reply):
        return AddressSaveClaimGuardResult(reply=str(reply or ""), action="allowed")

    from core.customer_address_persistence_evidence import (  # noqa: PLC0415
        NO_OPERATION,
        resolve_customer_address_persistence_evidence,
    )

    evidence = resolve_customer_address_persistence_evidence(
        db,
        tenant_id=tenant_id,
        customer_id=customer_id,
        attempt=attempt if attempt is not None else NO_OPERATION,
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
