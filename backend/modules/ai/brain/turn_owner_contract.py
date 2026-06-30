"""Current-turn owner contract for post-decision reply protection.

This module intentionally stays small and dependency-light. It does not
choose owners or reorder the decision engine; it summarizes the final
``Decision`` into flags that later compose/postprocess layers can honor.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Mapping, Optional, Tuple

logger = logging.getLogger("nahla.brain.turn_owner_contract")


TOPIC_HEALTH_ADVISORY = "health_advisory_product_safety"
TOPIC_COLD_SHIPPING = "cold_shipping_inquiry"
TOPIC_STOREFRONT = "storefront_self_checkout"
TOPIC_SHIPPING = "shipping_inquiry"
TOPIC_PRODUCT_KNOWLEDGE = "product_knowledge_facts"
PAYMENT_TOPICS = frozenset({
    "payment_receipt_received",
    "payment_evidence_pending_review",
})

POSTPROCESS_CATALOG_GROUNDING = "catalog_grounding"
POSTPROCESS_MEDICAL_CLAIM_REWRITE = "medical_claim_rewrite"
POSTPROCESS_PRODUCT_BENEFIT_REWRITE = "product_benefit_rewrite"
POSTPROCESS_PRODUCT_ORDERING_PROMPT = "product_ordering_prompt"
POSTPROCESS_STAFF_CONTACT = "staff_contact"
POSTPROCESS_SHOWROOM = "showroom"
POSTPROCESS_ORDER_SLOT_REPLAY = "order_slot_replay"


@dataclass(frozen=True)
class TurnOwnerContract:
    owner: Optional[str] = None
    topic: Optional[str] = None
    action: Optional[str] = None
    protected_final_reply: bool = False

    block_catalog_push: bool = False
    block_staff_contact: bool = False
    block_showroom_location: bool = False
    pause_order_slot_collection: bool = False
    block_product_ordering_prompt: bool = False
    block_product_benefit_rewrite: bool = False
    block_medical_claim_rewrite: bool = False

    allowed_postprocess: FrozenSet[str] = field(default_factory=frozenset)
    blocked_postprocess: FrozenSet[str] = field(default_factory=frozenset)

    def blocks(self, postprocess_name: str) -> bool:
        return postprocess_name in self.blocked_postprocess

    def allows(self, postprocess_name: str) -> bool:
        return postprocess_name in self.allowed_postprocess

    def to_metadata(self) -> Dict[str, Any]:
        return {
            "owner": self.owner,
            "topic": self.topic,
            "action": self.action,
            "protected_final_reply": self.protected_final_reply,
            "block_catalog_push": self.block_catalog_push,
            "block_staff_contact": self.block_staff_contact,
            "block_showroom_location": self.block_showroom_location,
            "pause_order_slot_collection": self.pause_order_slot_collection,
            "block_product_ordering_prompt": self.block_product_ordering_prompt,
            "block_product_benefit_rewrite": self.block_product_benefit_rewrite,
            "block_medical_claim_rewrite": self.block_medical_claim_rewrite,
            "allowed_postprocess": sorted(self.allowed_postprocess),
            "blocked_postprocess": sorted(self.blocked_postprocess),
        }


@dataclass(frozen=True)
class PrebrainRouteContract:
    """Current-turn route contract available before brain decision."""

    owner: Optional[str] = None
    topic: Optional[str] = None
    protected_final_reply: bool = False
    block_catalog_push: bool = False
    block_staff_contact: bool = False
    block_showroom_location: bool = False
    suppress_reason: str = ""

    def suppresses_branch_routing(self) -> bool:
        return bool(
            self.suppress_reason
            or self.block_staff_contact
            or self.block_showroom_location
        )

    def to_metadata(self) -> Dict[str, Any]:
        return {
            "owner": self.owner,
            "topic": self.topic,
            "protected_final_reply": self.protected_final_reply,
            "block_catalog_push": self.block_catalog_push,
            "block_staff_contact": self.block_staff_contact,
            "block_showroom_location": self.block_showroom_location,
            "suppress_reason": self.suppress_reason,
        }


def summarize_turn_owner_contract(contract: TurnOwnerContract) -> Dict[str, Any]:
    """Log-safe summary for telemetry and turn metadata."""
    return {
        "owner": contract.owner,
        "topic": contract.topic,
        "action": contract.action,
        "protected_final_reply": contract.protected_final_reply,
        "block_catalog_push": contract.block_catalog_push,
        "block_staff_contact": contract.block_staff_contact,
        "block_showroom_location": contract.block_showroom_location,
        "pause_order_slot_collection": contract.pause_order_slot_collection,
        "block_product_ordering_prompt": contract.block_product_ordering_prompt,
        "blocked_postprocess": sorted(contract.blocked_postprocess),
    }


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _infer_owner(*, topic: str, action: str, args: Mapping[str, Any]) -> Optional[str]:
    owner = _clean(
        args.get("commerce_entry_owner")
        or args.get("turn_owner")
        or args.get("owner")
        or args.get("order_channel_route_kind")
    )
    if owner:
        return owner
    if topic == TOPIC_HEALTH_ADVISORY:
        return "health_advisory"
    if topic in PAYMENT_TOPICS:
        return "payment_evidence"
    if topic in {TOPIC_COLD_SHIPPING, TOPIC_STOREFRONT, TOPIC_SHIPPING}:
        return "commerce_order_channel"
    if topic == TOPIC_PRODUCT_KNOWLEDGE:
        return "product_knowledge"
    if action == "catalog_navigate" or args.get("catalog_delivery_kind"):
        return "commerce_entry_catalog_delivery"
    if action == "propose_draft_order":
        return "ordering"
    if action == "search_products":
        return "catalog_search"
    return None


def _topic_from_decision(decision: Any, args: Mapping[str, Any]) -> str:
    return _clean(args.get("topic") or args.get("decision_topic"))


def build_turn_owner_contract(
    decision: Any,
    ctx: Any = None,
) -> TurnOwnerContract:
    _ = ctx
    args = dict(getattr(decision, "args", None) or {})
    action = _clean(getattr(decision, "action", "") or "")
    topic = _topic_from_decision(decision, args)
    owner = _infer_owner(topic=topic, action=action, args=args)

    block_catalog_push = _truthy(
        args.get("block_catalog_push")
        or args.get("block_catalog_escalation")
    )
    block_staff_contact = _truthy(args.get("block_staff_contact"))
    block_showroom_location = _truthy(args.get("block_showroom_location"))
    pause_order_slot_collection = _truthy(args.get("pause_order_slot_collection"))
    block_product_ordering_prompt = _truthy(
        args.get("block_product_ordering_prompt")
        or args.get("block_whatsapp_quick_order")
    )
    block_product_benefit_rewrite = _truthy(args.get("block_product_benefit_rewrite"))
    block_medical_claim_rewrite = _truthy(args.get("block_medical_claim_rewrite"))
    protected_final_reply = _truthy(args.get("protected_final_reply"))

    allowed = set()
    blocked = set()

    if topic == TOPIC_HEALTH_ADVISORY:
        protected_final_reply = True
        block_catalog_push = True
        block_staff_contact = True
        block_showroom_location = True
        pause_order_slot_collection = True
        block_product_ordering_prompt = True
        allowed.update({"medical_claim_scrub", "style_cleanup"})
        blocked.update({
            POSTPROCESS_CATALOG_GROUNDING,
            POSTPROCESS_STAFF_CONTACT,
            POSTPROCESS_SHOWROOM,
            POSTPROCESS_ORDER_SLOT_REPLAY,
            POSTPROCESS_PRODUCT_ORDERING_PROMPT,
        })

    elif topic in PAYMENT_TOPICS:
        protected_final_reply = True
        block_catalog_push = True
        block_staff_contact = True
        block_showroom_location = True
        pause_order_slot_collection = True
        block_product_ordering_prompt = True
        block_product_benefit_rewrite = True
        block_medical_claim_rewrite = True
        blocked.update({
            POSTPROCESS_CATALOG_GROUNDING,
            POSTPROCESS_STAFF_CONTACT,
            POSTPROCESS_SHOWROOM,
            POSTPROCESS_ORDER_SLOT_REPLAY,
            POSTPROCESS_PRODUCT_ORDERING_PROMPT,
            POSTPROCESS_PRODUCT_BENEFIT_REWRITE,
            POSTPROCESS_MEDICAL_CLAIM_REWRITE,
        })

    elif topic in {TOPIC_COLD_SHIPPING, TOPIC_SHIPPING}:
        protected_final_reply = True
        block_product_ordering_prompt = True
        block_product_benefit_rewrite = True
        block_medical_claim_rewrite = True
        blocked.update({
            POSTPROCESS_PRODUCT_ORDERING_PROMPT,
            POSTPROCESS_PRODUCT_BENEFIT_REWRITE,
            POSTPROCESS_MEDICAL_CLAIM_REWRITE,
        })

    elif topic == TOPIC_STOREFRONT:
        protected_final_reply = True
        block_product_ordering_prompt = True
        block_product_benefit_rewrite = True
        block_medical_claim_rewrite = True
        blocked.update({
            POSTPROCESS_PRODUCT_ORDERING_PROMPT,
            POSTPROCESS_PRODUCT_BENEFIT_REWRITE,
            POSTPROCESS_MEDICAL_CLAIM_REWRITE,
        })

    elif topic == TOPIC_PRODUCT_KNOWLEDGE:
        protected_final_reply = True
        block_catalog_push = True
        block_staff_contact = True
        block_product_ordering_prompt = True
        blocked.update({
            POSTPROCESS_CATALOG_GROUNDING,
            POSTPROCESS_STAFF_CONTACT,
            POSTPROCESS_PRODUCT_ORDERING_PROMPT,
        })

    elif owner in {"commerce_entry_catalog", "commerce_entry_catalog_delivery"}:
        protected_final_reply = True
        block_product_benefit_rewrite = True
        block_medical_claim_rewrite = True
        blocked.update({
            POSTPROCESS_PRODUCT_BENEFIT_REWRITE,
            POSTPROCESS_MEDICAL_CLAIM_REWRITE,
            POSTPROCESS_ORDER_SLOT_REPLAY,
        })

    return TurnOwnerContract(
        owner=owner,
        topic=topic or None,
        action=action or None,
        protected_final_reply=protected_final_reply,
        block_catalog_push=block_catalog_push,
        block_staff_contact=block_staff_contact,
        block_showroom_location=block_showroom_location,
        pause_order_slot_collection=pause_order_slot_collection,
        block_product_ordering_prompt=block_product_ordering_prompt,
        block_product_benefit_rewrite=block_product_benefit_rewrite,
        block_medical_claim_rewrite=block_medical_claim_rewrite,
        allowed_postprocess=frozenset(allowed),
        blocked_postprocess=frozenset(blocked),
    )


def turn_owner_contract_from_metadata(
    inbound_metadata: Optional[Mapping[str, Any]],
) -> Optional[TurnOwnerContract]:
    meta = dict(inbound_metadata or {})
    raw = meta.get("turn_owner_contract")
    if isinstance(raw, TurnOwnerContract):
        return raw
    if not isinstance(raw, Mapping):
        if not any(k in meta for k in ("decision_topic", "topic", "block_catalog_push")):
            return None
        raw = {
            "topic": meta.get("decision_topic") or meta.get("topic"),
            "block_catalog_push": meta.get("block_catalog_push"),
            "block_staff_contact": meta.get("block_staff_contact"),
            "block_showroom_location": meta.get("block_showroom_location"),
            "pause_order_slot_collection": meta.get("pause_order_slot_collection"),
        }
    data = dict(raw)
    return TurnOwnerContract(
        owner=_clean(data.get("owner")) or None,
        topic=_clean(data.get("topic")) or None,
        action=_clean(data.get("action")) or None,
        protected_final_reply=_truthy(data.get("protected_final_reply")),
        block_catalog_push=_truthy(data.get("block_catalog_push")),
        block_staff_contact=_truthy(data.get("block_staff_contact")),
        block_showroom_location=_truthy(data.get("block_showroom_location")),
        pause_order_slot_collection=_truthy(data.get("pause_order_slot_collection")),
        block_product_ordering_prompt=_truthy(data.get("block_product_ordering_prompt")),
        block_product_benefit_rewrite=_truthy(data.get("block_product_benefit_rewrite")),
        block_medical_claim_rewrite=_truthy(data.get("block_medical_claim_rewrite")),
        allowed_postprocess=frozenset(data.get("allowed_postprocess") or ()),
        blocked_postprocess=frozenset(data.get("blocked_postprocess") or ()),
    )


def get_turn_owner_contract(
    *,
    ctx: Any = None,
    inbound_metadata: Optional[Mapping[str, Any]] = None,
) -> Optional[TurnOwnerContract]:
    contract = getattr(ctx, "turn_owner_contract", None) if ctx is not None else None
    if isinstance(contract, TurnOwnerContract):
        return contract
    return turn_owner_contract_from_metadata(inbound_metadata)


def attach_turn_owner_contract(ctx: Any, contract: TurnOwnerContract) -> None:
    if ctx is None:
        return
    ctx.turn_owner_contract = contract  # type: ignore[attr-defined]


def build_prebrain_route_contract(
    *,
    message: str = "",
    inbound_metadata: Optional[Mapping[str, Any]] = None,
    state: Any = None,
) -> PrebrainRouteContract:
    """Infer current-turn route protections available before brain decide."""
    meta = dict(inbound_metadata or {})

    try:
        from modules.ai.brain.commerce.payment_evidence_turn_route import (  # noqa: PLC0415
            TOPIC_PAYMENT_EVIDENCE_PENDING,
            TOPIC_PAYMENT_RECEIPT_RECEIVED,
            inbound_metadata_has_payment_evidence,
        )

        if inbound_metadata_has_payment_evidence(meta):
            pe = str(meta.get("payment_evidence_status") or "").strip().lower()
            topic = (
                TOPIC_PAYMENT_EVIDENCE_PENDING
                if pe == "pre_transfer_review"
                else TOPIC_PAYMENT_RECEIPT_RECEIVED
            )
            return PrebrainRouteContract(
                owner="payment_evidence",
                topic=topic,
                protected_final_reply=True,
                block_catalog_push=True,
                block_staff_contact=True,
                block_showroom_location=True,
                suppress_reason="payment_evidence_current_turn",
            )
    except Exception as exc:  # noqa: BLE001  # noqa: silent-ok — payment probe must not block routes
        logger.debug("[TURN_OWNER_CONTRACT] payment prebrain probe skipped err=%s", exc)

    try:
        from modules.ai.brain.commerce.health_advisory_product_safety import (  # noqa: PLC0415
            TOPIC_HEALTH_ADVISORY,
            classify_health_advisory,
        )

        if classify_health_advisory(message or "", state=state).matched:
            return PrebrainRouteContract(
                owner="health_advisory",
                topic=TOPIC_HEALTH_ADVISORY,
                protected_final_reply=True,
                block_catalog_push=True,
                block_staff_contact=True,
                block_showroom_location=True,
                suppress_reason="health_advisory_current_turn",
            )
    except Exception as exc:  # noqa: BLE001  # noqa: silent-ok — health probe must not block routes
        logger.debug("[TURN_OWNER_CONTRACT] health prebrain probe skipped err=%s", exc)

    return PrebrainRouteContract()


def should_suppress_prebrain_branch_routing(
    *,
    message: str = "",
    inbound_metadata: Optional[Mapping[str, Any]] = None,
    state: Any = None,
    prebrain_contract: Optional[PrebrainRouteContract] = None,
) -> Tuple[bool, PrebrainRouteContract]:
    contract = prebrain_contract or build_prebrain_route_contract(
        message=message,
        inbound_metadata=inbound_metadata,
        state=state,
    )
    if contract.suppresses_branch_routing():
        return True, contract
    return False, contract


__all__ = [
    "POSTPROCESS_CATALOG_GROUNDING",
    "POSTPROCESS_MEDICAL_CLAIM_REWRITE",
    "POSTPROCESS_PRODUCT_BENEFIT_REWRITE",
    "POSTPROCESS_PRODUCT_ORDERING_PROMPT",
    "PrebrainRouteContract",
    "TurnOwnerContract",
    "attach_turn_owner_contract",
    "build_prebrain_route_contract",
    "build_turn_owner_contract",
    "get_turn_owner_contract",
    "should_suppress_prebrain_branch_routing",
    "summarize_turn_owner_contract",
    "turn_owner_contract_from_metadata",
]
