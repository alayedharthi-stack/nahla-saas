"""Answer the turn when the address boundary cannot stand behind its reply.

The OrderFlowV2 branch answers pre-Brain and returns, so when its address
truth guard refuses a reply — the claim is unsupported, the evidence is
unreadable, or the guard could not run at all — there is nobody left to
say anything. Returning silently was safe and wrong: the customer asked
a question and got nothing back.

So the turn is recovered through the composition route the Brain already
uses. Nothing here authors customer prose: it hands the composer the
trusted facts the platform owns and asks for wording, exactly as
``ACTION_SELECT_PURCHASE_CHANNEL`` does when it rewrites itself into
``ACTION_LLM_REPLY``. The composed text is then put back through the same
truth guard, because a recovery that restates the unsupported claim is
not a recovery.

Only after a GENUINE composition failure does the approved emergency
fallback speak — which is the condition ``EX-FALLBACK-GENERIC-001``
actually describes, and the reason the earlier "fallback" on this path
was untruthful: it declared a compose failure where nothing had composed.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger("nahla.order_flow_v2.address_reply_recovery")

# Routing identifiers handed to the composer, not instructions to it. They
# name WHICH turn this is so the composer can pick its own wording; they
# carry no customer-facing prose.
RECOVERY_TOPIC = "customer_delivery_address"
RECOVERY_RESPONSE_GOAL = "collect_delivery_address"

# The composer is given a bounded slice of the turn. A recovery that hangs
# is a turn that goes unanswered, which is the failure being repaired.
RECOVERY_TIMEOUT_SECONDS = 8.0

FALLBACK_ACTION_TYPE = "order_flow_v2_address_reply"
FALLBACK_REASON_COMPOSE_FAILED = "address_reply_compose_failed"
FALLBACK_REASON_UNSUPPORTED_AFTER_COMPOSE = "address_reply_unsupported_after_compose"
FALLBACK_REASON_UNVERIFIABLE_AFTER_COMPOSE = "address_reply_unverifiable_after_compose"


@dataclass
class RecoveredReply:
    """What the turn ends up saying, and where every word came from."""

    text: str = ""
    compose_source: str = ""
    response_mode: str = ""
    llm_candidate_present: bool = False
    compose_attempted: bool = False
    fallback_reason: str = ""
    fallback_action_type: str = ""
    chosen_path: str = FALLBACK_ACTION_TYPE
    final_transform_reasons: list = field(default_factory=list)

    @property
    def spoke(self) -> bool:
        return bool(str(self.text or "").strip())

    def as_metadata(self) -> Dict[str, Any]:
        meta: Dict[str, Any] = {
            "compose_source": self.compose_source,
            "response_mode": self.response_mode or self.compose_source,
            "chosen_path": self.chosen_path,
            "llm_candidate_present": bool(self.llm_candidate_present),
            "address_claim_compose_attempted": bool(self.compose_attempted),
            "final_text_transformed": True,
            "final_transform_reasons": list(self.final_transform_reasons),
            "final_customer_text_source": self.compose_source,
            "address_reply_recovered": True,
        }
        if self.fallback_reason:
            meta["fallback_reason"] = self.fallback_reason
            meta["fallback_action_type"] = self.fallback_action_type or FALLBACK_ACTION_TYPE
        return meta


def _emergency_fallback_text() -> str:
    """The approved minimal line — used only after a real compose failure."""
    from core.fallback_policy import (  # noqa: PLC0415
        empty_reply_fallback,
        operational_compose_error_fallback,
    )

    text = str(operational_compose_error_fallback() or "").strip()
    return text or str(empty_reply_fallback() or "").strip()


def _build_compose_inputs(
    *,
    tenant_id: int,
    customer_phone: str,
    message: str,
    conversation: Any,
    known_facts: Optional[Dict[str, Any]],
):
    """A decision, a result and a context the existing composer accepts.

    The facts are the platform's own — the fields it already holds about
    this order and this customer's saved addresses. Nothing invented is
    added, and no claim about a save is included, because the guard just
    refused exactly that.
    """
    from modules.ai.brain.decision.actions import ACTION_LLM_REPLY  # noqa: PLC0415
    from modules.ai.brain.types import (  # noqa: PLC0415
        ActionResult,
        BrainContext,
        CommerceFacts,
        Decision,
        Intent,
        MerchantConversationState,
    )

    facts = {k: v for k, v in (known_facts or {}).items() if v not in (None, "")}
    decision = Decision(
        action=ACTION_LLM_REPLY,
        args={
            "topic": RECOVERY_TOPIC,
            "response_goal": RECOVERY_RESPONSE_GOAL,
        },
    )
    result = ActionResult(success=True, data={"trusted_facts": facts})
    ctx = BrainContext(
        tenant_id=int(tenant_id),
        customer_phone=str(customer_phone or ""),
        message=str(message or ""),
        intent=Intent(name=RECOVERY_RESPONSE_GOAL, confidence=1.0),
        state=MerchantConversationState(),
        facts=CommerceFacts(),
        customer_id=int(getattr(conversation, "customer_id", 0) or 0) or None,
        conversation_id=int(getattr(conversation, "id", 0) or 0) or None,
    )
    return decision, result, ctx


def _default_composer() -> Any:
    from modules.ai.brain.compose.responder import DefaultComposer  # noqa: PLC0415

    return DefaultComposer()


async def compose_address_recovery_reply(
    db: Any,
    *,
    tenant_id: int,
    conversation: Any,
    customer_phone: str,
    message: str = "",
    known_facts: Optional[Dict[str, Any]] = None,
    turn_ref: str = "",
    composer: Any = None,
    timeout_seconds: float = RECOVERY_TIMEOUT_SECONDS,
) -> RecoveredReply:
    """Compose an honest answer for a turn whose reply was refused.

    Composition is attempted FIRST — the order AGENTS.md requires of any
    emergency fallback — and its output is revalidated by the same truth
    guard before it can reach the customer.
    """
    reasons = ["customer_address_save_claim_guard"]
    candidate = ""
    attempted = False
    try:
        engine = composer if composer is not None else _default_composer()
        decision, result, ctx = _build_compose_inputs(
            tenant_id=tenant_id,
            customer_phone=customer_phone,
            message=message,
            conversation=conversation,
            known_facts=known_facts,
        )
        attempted = True
        candidate = str(
            await asyncio.wait_for(
                engine.compose(decision, result, ctx), timeout=timeout_seconds
            )
            or ""
        ).strip()
    except Exception:  # noqa: BLE001  # noqa: silent-ok — a genuine compose failure is what the approved emergency fallback exists for, and it is recorded as one below
        logger.warning(
            "[ORDER_FLOW_V2] address recovery compose failed tenant=%s turn=%s",
            tenant_id,
            turn_ref,
            exc_info=True,
        )
        candidate = ""

    if not candidate:
        return RecoveredReply(
            text=_emergency_fallback_text(),
            compose_source="fallback_deterministic",
            response_mode="fallback_deterministic",
            llm_candidate_present=False,
            compose_attempted=attempted,
            fallback_reason=FALLBACK_REASON_COMPOSE_FAILED,
            fallback_action_type=FALLBACK_ACTION_TYPE,
            final_transform_reasons=reasons + ["address_reply_recovery_compose_failed"],
        )

    guarded, verdict = _revalidate(
        candidate, db=db, tenant_id=tenant_id, conversation=conversation,
        turn_ref=turn_ref,
    )
    if verdict == "ok" and str(guarded or "").strip():
        transformed = guarded.strip() != candidate
        return RecoveredReply(
            text=guarded.strip(),
            compose_source="llm",
            response_mode="llm",
            llm_candidate_present=True,
            compose_attempted=True,
            final_transform_reasons=(reasons if transformed else []),
        )

    # The composer spoke, but what it said could not be stood behind
    # either — it restated the claim, or nothing was able to judge it.
    # Sending unverified text is the thing this whole path exists to
    # prevent, so the approved minimal line speaks. Composition WAS
    # attempted and a candidate DID exist; the metadata says both, and
    # says which of the two happened.
    reason = (
        FALLBACK_REASON_UNVERIFIABLE_AFTER_COMPOSE
        if verdict == "unverifiable"
        else FALLBACK_REASON_UNSUPPORTED_AFTER_COMPOSE
    )
    return RecoveredReply(
        text=_emergency_fallback_text(),
        compose_source="fallback_deterministic",
        response_mode="fallback_deterministic",
        llm_candidate_present=True,
        compose_attempted=True,
        fallback_reason=reason,
        fallback_action_type=FALLBACK_ACTION_TYPE,
        final_transform_reasons=reasons + [reason],
    )


def _revalidate(
    text: str, *, db: Any, tenant_id: int, conversation: Any, turn_ref: str
):
    """Put the composed candidate through the same truth guard.

    Returns ``(text, verdict)`` where verdict is ``ok`` (the candidate
    stands), ``unsupported`` (the guard removed it all) or
    ``unverifiable`` (nothing could judge it). A recovery that cannot be
    verified is no better than the reply it replaced, so only ``ok``
    reaches the customer.
    """
    try:
        from modules.ai.order_flow_v2.outbound_guards import (  # noqa: PLC0415
            ADDRESS_CLAIM_SUPPRESS_KEY,
            apply_order_flow_v2_outbound_guards,
        )

        sink: Dict[str, Any] = {}
        guarded = apply_order_flow_v2_outbound_guards(
            text,
            db=db,
            tenant_id=int(tenant_id),
            conversation_id=int(getattr(conversation, "id", 0) or 0) or None,
            conversation=conversation,
            turn_ref=turn_ref,
            provenance_sink=sink,
            order_prep={},
        )
        if sink.get(ADDRESS_CLAIM_SUPPRESS_KEY):
            suppressed_for = str(sink.get("address_save_claim_suppress_reason") or "")
            return "", (
                "unsupported" if suppressed_for == "removal_left_no_reply"
                else "unverifiable"
            )
        return guarded, "ok"
    except Exception:  # noqa: BLE001  # noqa: silent-ok — an unverifiable candidate falls to the approved fallback above, never to the customer
        logger.warning(
            "[ORDER_FLOW_V2] address recovery revalidation failed tenant=%s turn=%s",
            tenant_id,
            turn_ref,
            exc_info=True,
        )
        return "", "unverifiable"


__all__ = [
    "FALLBACK_ACTION_TYPE",
    "FALLBACK_REASON_COMPOSE_FAILED",
    "FALLBACK_REASON_UNSUPPORTED_AFTER_COMPOSE",
    "FALLBACK_REASON_UNVERIFIABLE_AFTER_COMPOSE",
    "RecoveredReply",
    "compose_address_recovery_reply",
]
