"""
operational_expression.py
───────────────────────────
Behavioral compose goals for operational turns — constraints and facts
only, no canned Arabic customer copy.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping

from core.reply_instruction import (
    CONSTRAINT_ACK_MEDIA_RECEIVED,
    CONSTRAINT_ASK_FINAL_RECEIPT,
    CONSTRAINT_ASK_PARSEABLE_ADDRESS,
    CONSTRAINT_ASK_PAYMENT_PROOF,
    CONSTRAINT_ASK_ORDER_SLOT,
    CONSTRAINT_INCLUDE_ORDER_FACTS,
    CONSTRAINT_NO_PRICE_INVENTION,
    CONSTRAINT_NO_INTERNAL_CONTACT_LEAK,
    CONSTRAINT_NO_ORDER_STATUS_MUTATION,
    CONSTRAINT_NO_PAYMENT_CONFIRM,
    CONSTRAINT_NO_SHIPPING_PROMISE,
    CONSTRAINT_RESPECT_PLATFORM_NEXT_SLOT,
    DECISION_KIND_ADDRESS_INGEST,
    DECISION_KIND_CLEAR_INTENT,
    DECISION_KIND_MAP_SCREENSHOT,
    DECISION_KIND_ORDER_SLOT,
    DECISION_KIND_PAYMENT_CLAIM,
    DECISION_KIND_PAYMENT_EVIDENCE,
    DECISION_KIND_PAYMENT_METHOD,
    DECISION_KIND_PAYMENT_RECEIPT,
    ReplyInstruction,
)

_CONSTRAINT_LABELS: Dict[str, str] = {
    CONSTRAINT_NO_PAYMENT_CONFIRM: (
        "Do NOT confirm payment, receipt received, or order completion."
    ),
    CONSTRAINT_ASK_FINAL_RECEIPT: (
        "Ask the customer to send the final bank transfer receipt after "
        "they complete the transfer (if applicable to the media)."
    ),
    CONSTRAINT_NO_ORDER_STATUS_MUTATION: (
        "Do NOT claim you changed order status or marked the order paid."
    ),
    CONSTRAINT_NO_SHIPPING_PROMISE: (
        "Do NOT promise shipping, preparation, or delivery timing."
    ),
    CONSTRAINT_NO_INTERNAL_CONTACT_LEAK: (
        "Do NOT mention internal agent phone numbers or staff names "
        "unless provided in structured facts."
    ),
    CONSTRAINT_ACK_MEDIA_RECEIVED: (
        "Acknowledge you received their file/image in a natural, contextual "
        "way — reference what they sent when facts allow (bank screen, QR, "
        "transfer review), not a generic one-size-fits-all line."
    ),
    CONSTRAINT_ASK_PARSEABLE_ADDRESS: (
        "Ask for a Google Maps share link OR the 4-letter+4-digit national "
        "short address — explain the map screenshot alone is not enough."
    ),
    CONSTRAINT_ASK_PAYMENT_PROOF: (
        "Ask for payment proof (receipt image/PDF) without confirming payment."
    ),
    CONSTRAINT_INCLUDE_ORDER_FACTS: (
        "Include the structured order facts provided below accurately "
        "(product, price, address fields) — do not invent values."
    ),
    CONSTRAINT_ASK_ORDER_SLOT: (
        "Ask naturally for the next missing checkout field (name, city, "
        "address link, payment method, etc.) — one field at a time."
    ),
    CONSTRAINT_NO_PRICE_INVENTION: (
        "Do NOT invent prices, discounts, payment methods, or shipping costs."
    ),
    CONSTRAINT_RESPECT_PLATFORM_NEXT_SLOT: (
        "The platform owns the next checkout field. Ask only for "
        "next_missing_field. If next_missing_field is none, do not ask for "
        "name, phone, city, address, or any other checkout field."
    ),
}


def _format_constraints(constraints: tuple[str, ...]) -> str:
    lines: List[str] = []
    for c in constraints:
        label = _CONSTRAINT_LABELS.get(c, c)
        lines.append(f"- {label}")
    return "\n".join(lines) if lines else "- Stay honest and concise."


def _format_facts(facts: Mapping[str, Any]) -> str:
    if not facts:
        return "(none)"
    parts: List[str] = []
    for key, val in facts.items():
        if val is None or val == "":
            continue
        parts.append(f"- {key}: {val}")
    return "\n".join(parts) if parts else "(none)"


def compose_operational_expression_goal(instruction: ReplyInstruction) -> str:
    """Return LLM response_goal text for an operational ReplyInstruction."""
    kind = instruction.decision_kind
    facts_block = _format_facts(instruction.facts)
    constraints_block = _format_constraints(instruction.constraints)

    context_note = ""
    if kind == DECISION_KIND_PAYMENT_EVIDENCE:
        status = instruction.facts.get("payment_evidence_status", "")
        context_note = (
            f"Payment evidence was detected but NOT confirmed (status={status}). "
            "The customer sent payment-related media that is not a final receipt."
        )
    elif kind == DECISION_KIND_PAYMENT_CLAIM:
        context_note = (
            "The customer claimed they paid/transferred but no receipt media "
            "is attached and payment is NOT verified."
        )
    elif kind == DECISION_KIND_PAYMENT_RECEIPT:
        context_note = (
            "Payment receipt evidence is confirmed — acknowledge receipt and "
            "continue the order flow using the structured facts."
        )
    elif kind == DECISION_KIND_MAP_SCREENSHOT:
        context_note = (
            "The customer sent a map screenshot during an active order. "
            "Coordinates cannot be extracted from the screenshot."
        )
    elif kind == DECISION_KIND_ADDRESS_INGEST:
        nxt = str(instruction.facts.get("next_missing_field") or "none").strip() or "none"
        if nxt == "none":
            context_note = (
                "The customer's delivery location was persisted during checkout. "
                "Acknowledge using the structured facts. "
                "The platform next_missing_field is none — do not ask for any "
                "checkout field."
            )
        else:
            context_note = (
                "The customer's delivery location was persisted during checkout. "
                "Acknowledge using the structured facts. "
                f"The platform owns next_missing_field={nxt}. Ask only for that "
                "field. Do not invent other missing fields."
            )
    elif kind == DECISION_KIND_PAYMENT_METHOD:
        context_note = (
            "The customer selected a payment method during checkout. "
            "Confirm the choice and give the next step (e.g. send receipt "
            "after bank transfer) without confirming payment."
        )
    elif kind == DECISION_KIND_ORDER_SLOT:
        slot = instruction.facts.get("missing_slot", "")
        context_note = (
            f"During checkout, ask for the next missing field ({slot or 'unknown'}). "
            "Keep the ask focused on one slot."
        )
    elif kind == DECISION_KIND_CLEAR_INTENT:
        intent = instruction.facts.get("clear_intent", "")
        required = instruction.facts.get("required_delivery", "")
        context_note = (
            f"The customer's message had a clear intent ({intent or 'unknown'}) but the "
            "draft reply was a generic timeout/apology or weak fallback. "
            "Compose a natural helpful reply that addresses that intent directly. "
            "Do not ask them to repeat an already-clear question."
        )
        if required:
            context_note += f" Required delivery mode: {required}."

    forbidden = instruction.forbidden_claims
    forbidden_block = ""
    if forbidden:
        forbidden_block = (
            "Forbidden claims (must NOT appear in your reply):\n"
            + "\n".join(f"- {m}" for m in forbidden)
            + "\n"
        )

    return (
        f"operational_expression — {context_note}\n"
        f"Structured facts (must respect; do not invent beyond these):\n"
        f"{facts_block}\n\n"
        f"Operational constraints:\n{constraints_block}\n\n"
        f"{forbidden_block}"
        "Compose a natural Saudi Arabic WhatsApp reply in Nahla's warm tone. "
        "Keep it concise (1–3 short lines). "
        "Do NOT use rigid template pools or generic «وصلني الملف» unless "
        "it fits the actual media context. "
        "Do NOT add customer-service closers («كيف أقدر أخدمك», «تحت أمرك»). "
        "Do NOT use [PRODUCT:…] or [MEDIA_KEY:…] markers."
    )


__all__ = ["compose_operational_expression_goal"]
