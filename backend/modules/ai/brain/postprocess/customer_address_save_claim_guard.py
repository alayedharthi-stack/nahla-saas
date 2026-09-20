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

# ``(?:و|ف)?`` is the attached coordinating prefix Arabic writes onto the
# negation particle itself: "ولم يتم حفظ عنوانك" is the same denial as
# "لم يتم حفظ عنوانك". Without it the particle went unrecognised and the
# guard deleted a TRUTHFUL negative — silencing honest wording, which is
# the opposite of what it is for. This is the same structural prefix the
# claim side already accounts for, not a new phrasing to match.
_NEGATION_RES: Tuple[re.Pattern, ...] = (
    re.compile(r"(?:^|\s)(?:و|ف)?(?:لم|لن|ما|مو|مب|ليس|بدون|غير)(?:\s|$)", re.UNICODE),
    re.compile(r"(?:^|\s)(?:و|ف)?لا\s*(?:يوجد|يمكن|نستطيع|زال)", re.UNICODE),
    re.compile(r"\b(?:not|no|never|cannot|can't|couldn't|didn't|won't)\b", re.IGNORECASE),
)


# Punctuation is not the only clause boundary Arabic uses. "تم حفظ عنوانك هل
# تريد إكمال الطلب؟" is one punctuated sentence carrying TWO clauses: a
# completed-action assertion and a question. Judged whole, the trailing
# question mark exempted the assertion — a false claim shipped because of a
# question about something else. So a sentence is split again before an
# interrogative lead, and each clause is judged on its own.
# ``,`` belongs here beside ``،``. Without it "No worries, your address
# was saved" was one clause, and the reassurance's "No" sat close enough
# to the claim to look like it governed it. The Arabic comma was already
# a boundary; its ASCII twin does the same job in the same replies.
#
# And the boundary cannot depend on the SPACE. "No worries,your address
# was saved" is the same sentence with a typo, and requiring whitespace
# after the punctuation handed the claim straight back. So a comma-like
# mark separates clauses wherever it appears, and a sentence-ender does
# so when a letter follows it directly — the digit case is excluded on
# purpose, because ``3.14`` and ``1,000`` are one token, not two clauses.
_SENTENCE_SPLIT = re.compile(
    r"(?<=[.!?؟،,؛;:])\s+"
    r"|\n+"
    # A comma-like mark with nothing after it — unless BOTH sides are
    # digits, which is a thousands separator, not a clause end.
    r"|(?<=[،,؛;])(?![0-9])(?=\S)"
    r"|(?<![0-9])(?<=[،,؛;])(?=[0-9])"
    # ``!``/``?`` run straight into the next clause often enough.
    r"|(?<=[!?؟])(?=[^\W\d_])"
    # A full stop does too, but only with a real word in front of it:
    # ``ر.س`` and ``e.g`` are one token, not two clauses.
    r"|(?<=\w\w\.)(?=[^\W\d_])",
    re.UNICODE,
)
_CLAUSE_SPLIT = re.compile(
    # Before an interrogative lead…
    r"(?=\s(?:و|ف)?(?:هل|وش|ايش|أيش|كيف|متى|متي|وين|أين|اين|ليش|لماذا)\s)"
    r"|(?=\s(?:and\s+|but\s+)?(?:do|did|would|shall|can|could|should|will)"
    r"\s+(?:you|we)\b)"
    # …and before an adversative connective, which starts a new clause the
    # previous clause's negation does not reach: "we did not change your
    # order BUT your address was saved" denies the order change, not the
    # save. These are structural connectives, not intent phrasings.
    r"|(?=\s(?:و?لكن|بس|الا\s+ان|غير\s+ان|but|however|though|yet)\s)",
    re.UNICODE | re.IGNORECASE,
)

# How far a negation reaches. A negation governs the claim it stands next
# to, not every claim later in the clause: "there is no problem AND your
# address was saved" denies the problem. One intervening token covers the
# attached forms ("لم يتم حفظ") without letting a negation reach across a
# whole coordinated statement.
_NEGATION_REACH_TOKENS = 2

# A coordinator between a negation and a claim ends the negation's
# statement, exactly as an attached "و"/"ف" prefix does. "no problem and
# your address was saved" denies the problem; the save is its own
# statement. These are structural connectives — the same class as the
# adversatives above — not intent phrasings, and the list does not grow
# with wording.
_COORDINATOR_GAP_RE = re.compile(
    r"(?:^|\s)(?:and|plus|also|then|so|وايضا|ايضا|كما)(?:\s|$)",
    re.UNICODE | re.IGNORECASE,
)


def _sentences(text: str) -> list:
    return [c.strip() for c in _SENTENCE_SPLIT.split(text) if c.strip()]


def _clauses(text: str) -> list:
    """Sentences, then interrogative leads — the boundaries meaning follows."""
    out: list = []
    for sentence in _sentences(text):
        for clause in _CLAUSE_SPLIT.split(sentence):
            part = clause.strip()
            if part:
                out.append(part)
    return out


def is_interrogative(text: str) -> bool:
    """True when this clause ASKS rather than asserts."""
    norm = _norm(text)
    if not norm:
        return False
    return any(p.search(norm) for p in _INTERROGATIVE_RES)


def _negated_before(norm: str, start: int) -> bool:
    """True when a negation GOVERNS the claim, not merely precedes it.

    Position alone is not enough, in either direction:

    * "عنوانك محفوظ بدون أي مشكلة" asserts the save and then says it went
      smoothly — "بدون" negates "مشكلة", so a negation AFTER the claim
      never governs it.
    * "لا يوجد أي مشكلة وتم حفظ عنوانك" denies the problem and then
      asserts the save — the negation is before the claim but governs a
      different statement, and the attached "و" marks where that statement
      ends.

    So a negation must stand immediately before the claim, with at most
    one intervening token, and must not be cut off from it by a
    coordinating "و"/"ف" that starts the claim's own statement. No phrase
    list can make that distinction; only scope can.
    """
    if start > 0 and norm[start - 1] in ("و", "ف"):
        # The claim carries its own coordinating prefix, so it is a new
        # statement and an earlier negation does not reach into it.
        return False
    for pattern in _NEGATION_RES:
        for match in pattern.finditer(norm):
            if match.start() >= start:
                break
            if match.end() > start:
                continue
            gap = norm[match.end():start].strip()
            if _COORDINATOR_GAP_RE.search(gap):
                # The claim opens its own statement; the negation stopped
                # at the end of the previous one.
                continue
            if len(gap.split()) <= _NEGATION_REACH_TOKENS:
                return True
    return False


def _claim_kinds_in_clause(clause: str) -> Tuple[str, ...]:
    """Claim kinds this single clause asserts as completed."""
    if is_interrogative(clause):
        return ()
    norm = _norm(clause)
    if not norm:
        return ()
    kinds: list[str] = []
    for kind, patterns in (
        (CLAIM_KIND_SAVED, _SAVED_RES),
        (CLAIM_KIND_ADOPTED, _ADOPTED_RES),
    ):
        for pattern in patterns:
            match = pattern.search(norm)
            if match and not _negated_before(norm, match.start()):
                kinds.append(kind)
                break
    return tuple(kinds)


def is_successful_action_assertion(text: str) -> bool:
    """False for questions and for text whose claim is negated."""
    norm = _norm(text)
    if not norm:
        return False
    if is_interrogative(text):
        return False
    for patterns in (_SAVED_RES, _ADOPTED_RES):
        for pattern in patterns:
            match = pattern.search(norm)
            if match:
                return not _negated_before(norm, match.start())
    return not any(p.search(norm) for p in _NEGATION_RES)


def detect_address_save_claim_kinds(reply: str) -> Tuple[str, ...]:
    """Claim kinds asserted as COMPLETED actions, per clause.

    Judged clause by clause: one clause asking a question does not excuse
    another one asserting a save, and a negated clause is not a claim.
    """
    text = (reply or "").strip()
    if not text:
        return ()
    kinds: list[str] = []
    for clause in _clauses(text):
        for kind in _claim_kinds_in_clause(clause):
            if kind not in kinds:
                kinds.append(kind)
    return tuple(kinds)


def _chunk_has_claim(chunk: str, kinds: Tuple[str, ...]) -> bool:
    return any(kind in kinds for kind in _claim_kinds_in_clause(chunk))


def strip_unsupported_address_save_sentences(
    reply: str,
    kinds: Tuple[str, ...],
) -> str:
    """Remove the clauses carrying the unsupported claim; keep the rest.

    Clause granularity keeps the honest half of a mixed reply: the question
    in "تم حفظ عنوانك هل تريد إكمال الطلب؟" survives, only the unsupported
    assertion goes.
    """
    raw = (reply or "").strip()
    if not raw or not kinds:
        return raw
    kept: list[str] = []
    for chunk in _clauses(raw):
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
    recompose_allowed: bool = True

    @property
    def requires_grounded_recompose(self) -> bool:
        """The claim WAS the whole reply, so removing it left nothing.

        Sending an empty string is silence, and restoring the original
        sends the false claim. Neither is acceptable, so the turn asks for
        one more natural composition — the same contract
        ``product_claim_grounding_guard`` uses.
        """
        return bool(self.scrubbed_empty and self.replaced and self.recompose_allowed)


ADDRESS_CLAIM_FALLBACK_ACTION = "address_save_claim_failed_compose"
ADDRESS_CLAIM_FALLBACK_REASON = "address_save_claim_unsupported_after_recompose"


def apply_customer_address_save_claim_guard(
    *,
    reply: str,
    evidence: Optional[CustomerAddressPersistenceEvidence] = None,
    tenant_id: Optional[int] = None,
    conversation_id: Optional[int] = None,
    allow_recompose: bool = True,
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
            recompose_allowed=bool(allow_recompose),
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
    allow_recompose: bool = True,
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
        allow_recompose=allow_recompose,
    )


async def invoke_authorized_address_claim_recompose(
    composer: Any,
    decision: Any,
    result: Any,
    ctx: Any,
) -> Tuple[str, bool, int]:
    """Invoke the existing Composer ONCE, so the turn can still speak.

    Composition is attempted before any deterministic line — the order
    AGENTS.md requires of an emergency fallback. Nothing here authors
    prose: it asks the same composer that produced the original candidate
    for another one, which the second guard pass then revalidates.
    """
    calls = 0
    text: Any = None
    failed = False
    try:
        from core.turn_latency import safe_compose_role_scope  # noqa: PLC0415

        with safe_compose_role_scope("address_save_claim_grounding_recompose"):
            calls += 1
            text = await composer.compose(decision, result, ctx)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — turn latency fail-open
        if calls == 0:
            try:
                calls += 1
                text = await composer.compose(decision, result, ctx)
            except Exception:  # noqa: BLE001
                failed = True
                text = ""
        elif not str(text or "").strip():
            failed = True
            text = ""
    if not str(text or "").strip():
        failed = True
        text = str(text or "")
    return str(text or ""), bool(failed), int(calls)


def apply_address_claim_failed_compose_fallback(result_data: Any) -> str:
    """Last resort after a genuine compose failure: the platform's own line.

    Uses ``core.fallback_policy`` — the existing approved emergency
    fallback (``EX-FALLBACK-GENERIC-001``), not a new address-specific
    sentence. It carries no address facts, so it cannot restate the claim
    that was just removed. The metadata records that this is a fallback and
    why, which is what makes it auditable in production.
    """
    from core.fallback_policy import (  # noqa: PLC0415
        empty_reply_fallback,
        operational_compose_error_fallback,
    )

    text = str(operational_compose_error_fallback() or "").strip()
    if not text:
        text = str(empty_reply_fallback() or "").strip()
    if not isinstance(result_data, dict):
        return text
    result_data["compose_source"] = "fallback_deterministic"
    result_data["response_mode"] = "fallback_deterministic"
    result_data["chosen_path"] = ADDRESS_CLAIM_FALLBACK_ACTION
    result_data["fallback_reason"] = ADDRESS_CLAIM_FALLBACK_REASON
    result_data["fallback_action_type"] = ADDRESS_CLAIM_FALLBACK_ACTION
    result_data["final_customer_text_source"] = "fallback_deterministic"
    result_data["llm_candidate_present"] = True
    result_data["address_save_claim_constitutional_fallback"] = True
    reasons = [
        str(r)
        for r in (result_data.get("final_transform_reasons") or [])
        if str(r or "").strip()
    ]
    if "customer_address_save_claim_guard" not in reasons:
        reasons.append("customer_address_save_claim_guard")
    result_data["final_transform_reasons"] = reasons
    result_data["final_text_transformed"] = True
    return text


def stamp_address_claim_fallback_provenance(
    sink: Any,
    *,
    fallback_reason: str = ADDRESS_CLAIM_FALLBACK_REASON,
) -> None:
    """Record that the delivered text is the platform's, not the model's.

    Used at a boundary that substitutes the emergency fallback directly —
    the last-line guard, which runs after composition and has no composer
    left to ask. Leaving ``compose_source=llm`` there would make the audit
    trail claim the customer read the model's words when they did not.
    """
    if not isinstance(sink, dict):
        return
    sink["compose_source"] = "fallback_deterministic"
    sink["response_mode"] = "fallback_deterministic"
    sink["chosen_path"] = ADDRESS_CLAIM_FALLBACK_ACTION
    sink["fallback_reason"] = str(fallback_reason or ADDRESS_CLAIM_FALLBACK_REASON)
    sink["fallback_action_type"] = ADDRESS_CLAIM_FALLBACK_ACTION
    sink["final_customer_text_source"] = "fallback_deterministic"
    sink["llm_candidate_present"] = True
    sink["address_save_claim_constitutional_fallback"] = True
    reasons = [
        str(r)
        for r in (sink.get("final_transform_reasons") or [])
        if str(r or "").strip()
    ]
    if "customer_address_save_claim_guard" not in reasons:
        reasons.append("customer_address_save_claim_guard")
    sink["final_transform_reasons"] = reasons
    sink["final_text_transformed"] = True


def finalize_address_claim_after_authorized_recompose(
    *,
    second_pass: AddressSaveClaimGuardResult,
    recomposed_reply: str,
    result_data: Any,
    compose_failed: bool = False,
) -> str:
    """Resolve the second pass; never leave the turn empty or untruthful.

    Three outcomes, in order: the recomposed text survived the guard and is
    sent; the recomposed text was scrubbed to something shorter but still
    truthful and nonempty, and that is sent; composition failed or was
    scrubbed away again, and only then does the emergency fallback speak.
    """
    if compose_failed and not str(recomposed_reply or "").strip():
        return apply_address_claim_failed_compose_fallback(result_data)
    resolved = str(second_pass.reply or "").strip()
    if resolved:
        return resolved
    return apply_address_claim_failed_compose_fallback(result_data)


def stamp_address_claim_guard_provenance(
    result_data: Any,
    guard_result: AddressSaveClaimGuardResult,
    *,
    recompose_requested: bool = False,
    recompose_performed: bool = False,
) -> None:
    """Record what this guard did to the final customer text."""
    if not isinstance(result_data, dict):
        return
    result_data["address_save_claim_guard_action"] = guard_result.action
    result_data["address_save_claim_guard_scope"] = guard_result.evidence_scope
    result_data["address_save_claim_guard_reason"] = guard_result.reason
    result_data["address_save_claim_guard_blocked"] = list(guard_result.blocked_claims)
    if recompose_requested:
        result_data["address_save_claim_recompose_requested"] = True
    if recompose_performed:
        result_data["address_save_claim_recompose_performed"] = True
    if guard_result.replaced:
        reasons = [
            str(r)
            for r in (result_data.get("final_transform_reasons") or [])
            if str(r or "").strip()
        ]
        if "customer_address_save_claim_guard" not in reasons:
            reasons.append("customer_address_save_claim_guard")
        result_data["final_transform_reasons"] = reasons
        result_data["final_text_transformed"] = True


def resolve_outbound_after_address_claim_scrub(
    *,
    guard_result: AddressSaveClaimGuardResult,
    empty_reply_fallback_text: str,
) -> Tuple[str, bool]:
    """Resolve outbound at a boundary with no composer left to ask.

    Returns ``(final_reply, suppress_send)``. The last-line guard runs
    after composition is over, so there is no second candidate to request:
    a truthful platform line is sent rather than silence, and the send is
    only suppressed when even that is unavailable.
    """
    reply = str(guard_result.reply or "")
    if not guard_result.scrubbed_empty or reply.strip():
        return reply, False
    fallback = str(empty_reply_fallback_text or "").strip()
    if fallback:
        return fallback, False
    return "", True


__all__ = [
    "ADDRESS_CLAIM_FALLBACK_ACTION",
    "ADDRESS_CLAIM_FALLBACK_REASON",
    "AddressSaveClaimGuardResult",
    "CLAIM_KIND_ADOPTED",
    "CLAIM_KIND_SAVED",
    "apply_customer_address_save_claim_guard",
    "detect_address_save_claim_kinds",
    "resolve_and_apply_customer_address_save_claim_guard",
    "apply_address_claim_failed_compose_fallback",
    "finalize_address_claim_after_authorized_recompose",
    "invoke_authorized_address_claim_recompose",
    "resolve_outbound_after_address_claim_scrub",
    "stamp_address_claim_fallback_provenance",
    "stamp_address_claim_guard_provenance",
    "strip_unsupported_address_save_sentences",
]
