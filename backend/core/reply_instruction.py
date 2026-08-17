"""
reply_instruction.py
────────────────────
Structured operational reply contract — decision/constraints are
deterministic; customer-facing Arabic wording is composed by Brain/LLM
under these constraints (Doctrine: operations deterministic, personality
contextual).

Paths that still ship legacy fixed copy attach ``legacy_copy`` as the
fail-closed fallback when constrained compose is disabled or fails.
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# ── Feature flag ─────────────────────────────────────────────────────────────

_FLAG_CONSTRAINED_COMPOSE = "OPERATIONAL_CONSTRAINED_COMPOSE_ENABLED"


def is_operational_constrained_compose_enabled() -> bool:
    """When True (default), pre-Brain operational paths prefer LLM wording."""
    raw = os.getenv(_FLAG_CONSTRAINED_COMPOSE, "true").strip().lower()
    return raw in {"1", "true", "yes", "on"}


# ── Canonical constraint tokens (validator + compose goals) ─────────────────

CONSTRAINT_NO_PAYMENT_CONFIRM = "must_not_confirm_payment"
CONSTRAINT_ASK_FINAL_RECEIPT = "ask_for_final_receipt_after_transfer"
CONSTRAINT_NO_ORDER_STATUS_MUTATION = "do_not_mutate_order_status_in_reply"
CONSTRAINT_NO_SHIPPING_PROMISE = "must_not_promise_shipping_or_completion"
CONSTRAINT_NO_INTERNAL_CONTACT_LEAK = "must_not_leak_internal_agent_phone"
CONSTRAINT_ACK_MEDIA_RECEIVED = "acknowledge_media_received_contextually"
CONSTRAINT_ASK_PARSEABLE_ADDRESS = "ask_for_maps_link_or_short_address"
CONSTRAINT_ASK_PAYMENT_PROOF = "ask_for_payment_proof"
CONSTRAINT_INCLUDE_ORDER_FACTS = "include_structured_order_facts_when_present"
CONSTRAINT_NO_FALSE_HANDOFF = "must_not_claim_staff_handoff_without_evidence"

FORBIDDEN_PAYMENT_CONFIRM_MARKERS: Tuple[str, ...] = (
    "تم تأكيد الدفع",
    "تم استلام الإيصال",
    "وصلنا إيصال التحويل",
    "تم التحقق من التحويل",
    "سيتم تجهيز الطلب",
    "تم استلام الطلب",
)

FORBIDDEN_MERCHANT_PAYMENT_CONFIRM_MARKERS: Tuple[str, ...] = (
    "تم تأكيد الدفع",
    "تم التحقق من التحويل",
    "سيتم تجهيز الطلب",
    "تم استلام الطلب",
)

# ── Path identifiers (align with deterministic_path metadata) ───────────────

PATH_PAYMENT_EVIDENCE_SOFT_ACK = "payment_evidence_soft_ack"
PATH_PAYMENT_CLAIM_ACK = "payment_claim_ack"
PATH_PAYMENT_RECEIPT_ACK = "payment_receipt_ack"
PATH_MAP_IMAGE_ACK = "map_image_ack"
PATH_PAYMENT_METHOD_ACK = "payment_method_ack"
PATH_ADDRESS_INGEST_ACK = "address_ingest_ack"
PATH_ORDER_SLOT_PROMPT = "order_slot_prompt"
PATH_CLEAR_INTENT_FALLBACK = "clear_intent_fallback"

DECISION_KIND_PAYMENT_EVIDENCE = "payment_evidence_detected"
DECISION_KIND_PAYMENT_CLAIM = "payment_claim_unverified"
DECISION_KIND_PAYMENT_RECEIPT = "payment_receipt_received"
DECISION_KIND_MAP_SCREENSHOT = "map_screenshot_received"
DECISION_KIND_PAYMENT_METHOD = "payment_method_selected"
DECISION_KIND_ADDRESS_INGEST = "address_ingested"
DECISION_KIND_ORDER_SLOT = "order_slot_collection"
DECISION_KIND_CLEAR_INTENT = "clear_intent_fallback"

CONSTRAINT_ASK_ORDER_SLOT = "ask_for_next_order_slot"
CONSTRAINT_NO_PRICE_INVENTION = "must_not_invent_prices_or_discounts"
CONSTRAINT_RESPECT_PLATFORM_NEXT_SLOT = "ask_only_platform_next_missing_field"


@dataclass
class ReplyInstruction:
    """Operational decision + constraints for constrained compose."""

    path: str
    decision_kind: str
    facts: Dict[str, Any] = field(default_factory=dict)
    constraints: Tuple[str, ...] = ()
    forbidden_claims: Tuple[str, ...] = ()
    legacy_copy: str = ""
    decision_owner: str = ""
    expression_owner: str = "constrained_compose"
    inbound_text: str = ""
    copy_source: str = "legacy_fixed"

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["constraints"] = list(self.constraints)
        d["forbidden_claims"] = list(self.forbidden_claims)
        return d

    @classmethod
    def from_dict(cls, raw: Optional[Dict[str, Any]]) -> Optional["ReplyInstruction"]:
        if not raw or not isinstance(raw, dict):
            return None
        try:
            return cls(
                path=str(raw.get("path") or ""),
                decision_kind=str(raw.get("decision_kind") or ""),
                facts=dict(raw.get("facts") or {}),
                constraints=tuple(raw.get("constraints") or ()),
                forbidden_claims=tuple(raw.get("forbidden_claims") or ()),
                legacy_copy=str(raw.get("legacy_copy") or ""),
                decision_owner=str(raw.get("decision_owner") or ""),
                expression_owner=str(raw.get("expression_owner") or "constrained_compose"),
                inbound_text=str(raw.get("inbound_text") or ""),
                copy_source=str(raw.get("copy_source") or "legacy_fixed"),
            )
        except Exception:  # noqa: silent-ok — malformed instruction dict → None
            return None


def stamp_reply_metadata(
    instruction: Optional[ReplyInstruction],
    *,
    copy_source: str,
    expression_owner: str = "",
) -> Dict[str, Any]:
    """Build outbound extra_metadata ownership fields."""
    meta: Dict[str, Any] = {
        "copy_source": copy_source,
        "decision_owner": (instruction.decision_owner if instruction else ""),
        "expression_owner": expression_owner or (
            instruction.expression_owner if instruction else "legacy_fixed"
        ),
    }
    if instruction:
        meta["reply_instruction_path"] = instruction.path
        meta["reply_instruction_kind"] = instruction.decision_kind
    return meta


def build_payment_evidence_instruction(
    *,
    pe_status: str,
    pe_reason: str,
    legacy_copy: str,
    summary: Optional[Dict[str, Any]] = None,
    inbound_text: str = "",
) -> ReplyInstruction:
    facts: Dict[str, Any] = {
        "payment_evidence_status": pe_status,
        "payment_evidence_reason": pe_reason,
    }
    if summary:
        facts["selected_product"] = summary.get("selected_product")
        facts["awaiting_payment_receipt"] = summary.get("awaiting_payment_receipt")
        facts["order_status"] = summary.get("order_status")
    constraints: List[str] = [
        CONSTRAINT_NO_PAYMENT_CONFIRM,
        CONSTRAINT_ASK_FINAL_RECEIPT,
        CONSTRAINT_NO_ORDER_STATUS_MUTATION,
        CONSTRAINT_NO_SHIPPING_PROMISE,
        CONSTRAINT_NO_INTERNAL_CONTACT_LEAK,
        CONSTRAINT_ACK_MEDIA_RECEIVED,
    ]
    if pe_status == "pre_transfer_review":
        facts["media_interpretation"] = "pre_transfer_review_screen"
    elif pe_status == "needs_confirmation":
        facts["media_interpretation"] = "bank_or_qr_without_completion_marker"
    return ReplyInstruction(
        path=PATH_PAYMENT_EVIDENCE_SOFT_ACK,
        decision_kind=DECISION_KIND_PAYMENT_EVIDENCE,
        facts=facts,
        constraints=tuple(constraints),
        forbidden_claims=FORBIDDEN_PAYMENT_CONFIRM_MARKERS,
        legacy_copy=legacy_copy,
        decision_owner="core.payment_evidence",
        inbound_text=inbound_text,
    )


def build_map_image_instruction(
    *,
    legacy_copy: str,
    summary: Optional[Dict[str, Any]] = None,
    inbound_text: str = "",
) -> ReplyInstruction:
    facts: Dict[str, Any] = {"media_interpretation": "map_screenshot_no_coordinates"}
    if summary:
        facts["selected_product"] = summary.get("selected_product")
        facts["awaiting_location"] = summary.get("awaiting_location")
    return ReplyInstruction(
        path=PATH_MAP_IMAGE_ACK,
        decision_kind=DECISION_KIND_MAP_SCREENSHOT,
        facts=facts,
        constraints=(
            CONSTRAINT_ASK_PARSEABLE_ADDRESS,
            CONSTRAINT_NO_SHIPPING_PROMISE,
            CONSTRAINT_ACK_MEDIA_RECEIVED,
        ),
        forbidden_claims=FORBIDDEN_PAYMENT_CONFIRM_MARKERS,
        legacy_copy=legacy_copy,
        decision_owner="core.order_flow.map_image",
        inbound_text=inbound_text,
    )


def build_payment_claim_instruction(
    *,
    legacy_copy: str,
    summary: Optional[Dict[str, Any]] = None,
    inbound_text: str = "",
) -> ReplyInstruction:
    facts: Dict[str, Any] = {"claim_verified": False}
    if summary:
        facts.update({
            "selected_product": summary.get("selected_product"),
            "awaiting_payment_receipt": summary.get("awaiting_payment_receipt"),
            "order_status": summary.get("order_status"),
        })
    return ReplyInstruction(
        path=PATH_PAYMENT_CLAIM_ACK,
        decision_kind=DECISION_KIND_PAYMENT_CLAIM,
        facts=facts,
        constraints=(
            CONSTRAINT_NO_PAYMENT_CONFIRM,
            CONSTRAINT_ASK_PAYMENT_PROOF,
            CONSTRAINT_NO_SHIPPING_PROMISE,
        ),
        forbidden_claims=FORBIDDEN_PAYMENT_CONFIRM_MARKERS,
        legacy_copy=legacy_copy,
        decision_owner="core.payment_intent",
        inbound_text=inbound_text,
    )


def build_payment_receipt_instruction(
    *,
    legacy_copy: str,
    summary: Optional[Dict[str, Any]] = None,
    inbound_text: str = "",
) -> ReplyInstruction:
    s = summary or {}
    can_mention = bool(s.get("can_mention_receipt_product"))
    can_address = bool(s.get("can_request_receipt_address"))
    amount_mismatch = bool(s.get("receipt_amount_mismatch"))

    facts: Dict[str, Any] = {"receipt_evidence": "confirmed", "receipt_received": True}
    ev = s.get("receipt_order_evidence") if isinstance(s.get("receipt_order_evidence"), dict) else {}
    if ev.get("receipt_amount") is not None:
        facts["amount"] = ev.get("receipt_amount")
    if ev.get("expected_total") is not None:
        facts["expected_total"] = ev.get("expected_total")

    constraints: List[str] = [
        CONSTRAINT_NO_SHIPPING_PROMISE,
        CONSTRAINT_NO_INTERNAL_CONTACT_LEAK,
        CONSTRAINT_NO_PAYMENT_CONFIRM,
    ]

    if amount_mismatch:
        facts["needs_merchant_amount_review"] = True
        facts["needs_order_linking_or_review"] = True
    elif can_mention:
        facts.update({
            "selected_product": s.get("selected_product"),
            "price": s.get("price"),
            "short_address_code": s.get("short_address_code"),
            "awaiting_payment_receipt": s.get("awaiting_payment_receipt"),
        })
        constraints.append(CONSTRAINT_INCLUDE_ORDER_FACTS)
        if can_address:
            constraints.append(CONSTRAINT_ASK_PARSEABLE_ADDRESS)
    else:
        facts["needs_order_linking_or_review"] = True

    return ReplyInstruction(
        path=PATH_PAYMENT_RECEIPT_ACK,
        decision_kind=DECISION_KIND_PAYMENT_RECEIPT,
        facts=facts,
        constraints=tuple(constraints),
        forbidden_claims=FORBIDDEN_MERCHANT_PAYMENT_CONFIRM_MARKERS,
        legacy_copy=legacy_copy,
        decision_owner="core.order_flow.receipt",
        inbound_text=inbound_text,
    )


def build_address_instruction(
    *,
    legacy_copy: str,
    summary: Optional[Dict[str, Any]] = None,
    address_type: str = "",
    inbound_text: str = "",
    checkout_facts: Optional[Dict[str, Any]] = None,
    missing_fields: Optional[List[str]] = None,
    next_missing_field: Optional[str] = None,
) -> ReplyInstruction:
    facts: Dict[str, Any] = {}
    if summary:
        facts.update({
            "selected_product": summary.get("selected_product"),
            "awaiting_location": summary.get("awaiting_location"),
            "order_status": summary.get("order_status"),
        })
    if address_type:
        facts["delivery_address_type"] = address_type
    if checkout_facts:
        for key, val in checkout_facts.items():
            if val in (None, "", [], {}):
                continue
            facts[key] = val
    facts["missing_fields"] = list(missing_fields or [])
    facts["next_missing_field"] = next_missing_field or "none"
    facts["constrained_compose_decides_slot"] = False
    return ReplyInstruction(
        path=PATH_ADDRESS_INGEST_ACK,
        decision_kind=DECISION_KIND_ADDRESS_INGEST,
        facts=facts,
        constraints=(
            CONSTRAINT_INCLUDE_ORDER_FACTS,
            CONSTRAINT_RESPECT_PLATFORM_NEXT_SLOT,
            CONSTRAINT_NO_SHIPPING_PROMISE,
            CONSTRAINT_NO_INTERNAL_CONTACT_LEAK,
        ),
        forbidden_claims=FORBIDDEN_PAYMENT_CONFIRM_MARKERS,
        legacy_copy=legacy_copy,
        decision_owner="core.order_flow.address",
        inbound_text=inbound_text,
    )


def build_payment_method_instruction(
    *,
    legacy_copy: str,
    payment_method: str = "",
    summary: Optional[Dict[str, Any]] = None,
    inbound_text: str = "",
) -> ReplyInstruction:
    facts: Dict[str, Any] = {}
    if payment_method:
        facts["payment_method"] = payment_method
    if summary:
        facts.update({
            "selected_product": summary.get("selected_product"),
            "order_status": summary.get("order_status"),
        })
    constraints: List[str] = [
        CONSTRAINT_NO_PAYMENT_CONFIRM,
        CONSTRAINT_NO_SHIPPING_PROMISE,
    ]
    if payment_method == "bank_transfer":
        constraints.append(CONSTRAINT_ASK_PAYMENT_PROOF)
    return ReplyInstruction(
        path=PATH_PAYMENT_METHOD_ACK,
        decision_kind=DECISION_KIND_PAYMENT_METHOD,
        facts=facts,
        constraints=tuple(constraints),
        forbidden_claims=FORBIDDEN_PAYMENT_CONFIRM_MARKERS,
        legacy_copy=legacy_copy,
        decision_owner="core.order_flow.payment_method",
        inbound_text=inbound_text,
    )


def build_order_slot_instruction(
    *,
    slot: str,
    legacy_copy: str,
    product: Optional[Dict[str, Any]] = None,
    is_first_ask: bool = True,
    inbound_text: str = "",
) -> ReplyInstruction:
    facts: Dict[str, Any] = {
        "missing_slot": slot,
        "is_first_ask": is_first_ask,
    }
    if product:
        facts["selected_product"] = product.get("title") or product.get("name")
    return ReplyInstruction(
        path=PATH_ORDER_SLOT_PROMPT,
        decision_kind=DECISION_KIND_ORDER_SLOT,
        facts=facts,
        constraints=(
            CONSTRAINT_ASK_ORDER_SLOT,
            CONSTRAINT_INCLUDE_ORDER_FACTS,
            CONSTRAINT_NO_SHIPPING_PROMISE,
            CONSTRAINT_NO_PRICE_INVENTION,
        ),
        forbidden_claims=FORBIDDEN_PAYMENT_CONFIRM_MARKERS,
        legacy_copy=legacy_copy,
        decision_owner="brain.execution.orders",
        inbound_text=inbound_text,
    )


def build_clear_intent_instruction(
    *,
    intent: str,
    legacy_copy: str,
    inbound_text: str = "",
) -> ReplyInstruction:
    return ReplyInstruction(
        path=PATH_CLEAR_INTENT_FALLBACK,
        decision_kind=DECISION_KIND_CLEAR_INTENT,
        facts={"clear_intent": intent},
        constraints=(
            CONSTRAINT_NO_PRICE_INVENTION,
            CONSTRAINT_NO_SHIPPING_PROMISE,
            CONSTRAINT_NO_PAYMENT_CONFIRM,
        ),
        forbidden_claims=FORBIDDEN_PAYMENT_CONFIRM_MARKERS,
        legacy_copy=legacy_copy,
        decision_owner="postprocess.safety_nets.clear_intent",
        inbound_text=inbound_text,
    )


def attach_instruction_to_decision(
    decision: Dict[str, Any],
    instruction: ReplyInstruction,
) -> Dict[str, Any]:
    """Return a shallow copy of *decision* with ``reply_instruction`` attached."""
    out = dict(decision)
    out["reply_instruction"] = instruction.to_dict()
    return out


__all__ = [
    "CONSTRAINT_ACK_MEDIA_RECEIVED",
    "CONSTRAINT_ASK_FINAL_RECEIPT",
    "CONSTRAINT_ASK_PARSEABLE_ADDRESS",
    "CONSTRAINT_ASK_ORDER_SLOT",
    "CONSTRAINT_ASK_PAYMENT_PROOF",
    "CONSTRAINT_RESPECT_PLATFORM_NEXT_SLOT",
    "CONSTRAINT_INCLUDE_ORDER_FACTS",
    "CONSTRAINT_NO_FALSE_HANDOFF",
    "CONSTRAINT_NO_INTERNAL_CONTACT_LEAK",
    "CONSTRAINT_NO_ORDER_STATUS_MUTATION",
    "CONSTRAINT_NO_PAYMENT_CONFIRM",
    "CONSTRAINT_NO_PRICE_INVENTION",
    "CONSTRAINT_NO_SHIPPING_PROMISE",
    "DECISION_KIND_ADDRESS_INGEST",
    "DECISION_KIND_CLEAR_INTENT",
    "DECISION_KIND_ORDER_SLOT",
    "DECISION_KIND_MAP_SCREENSHOT",
    "DECISION_KIND_PAYMENT_CLAIM",
    "DECISION_KIND_PAYMENT_EVIDENCE",
    "DECISION_KIND_PAYMENT_METHOD",
    "DECISION_KIND_PAYMENT_RECEIPT",
    "FORBIDDEN_PAYMENT_CONFIRM_MARKERS",
    "PATH_ADDRESS_INGEST_ACK",
    "PATH_CLEAR_INTENT_FALLBACK",
    "PATH_ORDER_SLOT_PROMPT",
    "PATH_MAP_IMAGE_ACK",
    "PATH_PAYMENT_CLAIM_ACK",
    "PATH_PAYMENT_EVIDENCE_SOFT_ACK",
    "PATH_PAYMENT_METHOD_ACK",
    "PATH_PAYMENT_RECEIPT_ACK",
    "ReplyInstruction",
    "attach_instruction_to_decision",
    "build_address_instruction",
    "build_clear_intent_instruction",
    "build_map_image_instruction",
    "build_order_slot_instruction",
    "build_payment_claim_instruction",
    "build_payment_evidence_instruction",
    "build_payment_method_instruction",
    "build_payment_receipt_instruction",
    "is_operational_constrained_compose_enabled",
    "stamp_reply_metadata",
]
