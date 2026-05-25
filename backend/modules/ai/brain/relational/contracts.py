"""
brain/relational/contracts.py
─────────────────────────────
Code-enforced architectural rules for the relational layer.

The merchant directive (Tenant 33, May 2026) is unambiguous:

    Relational layer may shape the conversation,
    but must never fabricate business state.

    یعني:
        * لا یؤكد دفع
        * لا یؤكد شحن
        * لا یؤكد طلب
        * لا یخترع tracking
        * لا یتجاوز facts

    هو فقط:
        * یضبط tone
        * framing
        * empathy
        * suppression
        * prioritization

This module turns that directive into something a unit test can
fail on. Two enforcement mechanisms live here:

  1. ``BUSINESS_FACT_FIELD_FORBIDDEN_TOKENS`` — a list of substrings
     that may NEVER appear in a relational dataclass field name.
     The architectural test uses ``dataclasses.fields()`` on
     ``RelationalState`` and asserts no field name matches.

  2. ``RELATIONAL_LAYER_PERMITTED_OUTPUTS`` — the WHITELIST of
     things the relational layer is allowed to influence. Every
     downstream consumer (decision engine, prompt overlay, safety
     net gate) must restrict its mutations to one of these tokens.
     Tests in later commits will assert no other token gets
     mutated based on a moment.

Why pin this so strictly? The relational layer sees inbound text
+ history + customer profile. If a careless future change let it
emit, e.g., ``order_status="paid"`` based on a praise moment, we
would have rebuilt the exact bug the May 2026 audit closed.
The contract turns "never do that" into "the build fails if you
try".
"""
from __future__ import annotations

from typing import FrozenSet, Tuple


# ── 1. Forbidden field-name substrings ──────────────────────────────
# Case-insensitive substring match. If ANY field on
# ``RelationalState`` (or any future relational dataclass) contains
# one of these tokens, the architectural test fails. The list is
# intentionally broad — false positives are cheap (rename the
# field), false negatives are catastrophic.
BUSINESS_FACT_FIELD_FORBIDDEN_TOKENS: Tuple[str, ...] = (
    # Payment-side state
    "payment",
    "paid",
    "receipt",
    "invoice",
    "iban",
    "amount",
    "balance",
    "transfer",
    "refund",
    # Order-side state
    "order_id",
    "order_status",
    "order_total",
    "order_paid",
    "cart",
    "checkout",
    "voucher",
    # Shipment-side state
    "tracking",
    "shipment",
    "shipped",
    "delivered_at",
    "carrier",
    "awb",
    "courier",
    # Catalog-side state
    "sku",
    "product_id",
    "stock",
    "inventory",
    # Customer-identity / fact state
    "phone_number",  # a relational verdict has no business with phone digits
    "email_address",
    "national_id",
)


# ── 2. Whitelist of permitted relational influences ─────────────────
# Downstream consumers (decision engine, prompt overlay, safety net
# gate) read the relational verdict and may only adjust one of
# these dimensions. Adding a new dimension to the whitelist is a
# deliberate architectural decision that requires a follow-up
# commit and a regression test.
#
# These tokens are also useful as a stable vocabulary for the
# ``[CX]`` log line: every relational influence recorded in the
# log must be one of these.
RELATIONAL_LAYER_PERMITTED_OUTPUTS: FrozenSet[str] = frozenset({
    # Allowed influences on the brain prompt overlay
    "advisory_for_brain",       # plain-English description of moment
    "framing_directive",        # short non-imperative tone hint
    "tone_preference",          # tenant-tone overlay (e.g. warmer)
    # Allowed influences on the decision engine
    "preferred_action",         # SUGGEST a different ACTION_*; engine still owns the final pick
    "suppress_action",          # forbid an action this turn (e.g. customer-lookup on praise)
    "deprioritise_action",      # de-rank an action without forbidding
    # Allowed influences on safety nets
    "suppress_safety_net",      # name of a net that must not fire this turn
    "preserve_brain_warmth",    # signal to wrappers to NOT strip greeting / blessing
})


# ── 3. Forbidden side-effects ────────────────────────────────────────
# The relational layer's compute function MUST be pure. These names
# are tracked here so a future architectural test can assert they
# are NOT called from inside ``compute_relational_state`` (e.g. via
# import-graph analysis).
FORBIDDEN_SIDE_EFFECT_SYMBOLS: Tuple[str, ...] = (
    "apply_state_patch",
    "save_message",
    "_post_wa",
    "send_template",
    "create_handoff_session",
    "create_order",
    "mutate_brain_state",
)


# ── 4. Architectural-rule docstring (single source of truth) ────────
# Mirrored in the module docstring of ``state.py``. If the rule
# wording ever changes, change it here and let the tests pin the
# new copy.
ARCHITECTURAL_RULE_TEXT: str = (
    "Relational layer may shape the conversation, but must never "
    "fabricate business state. It may only influence tone, framing, "
    "empathy expression, transactional-artifact suppression, and "
    "action prioritisation."
)


__all__ = [
    "BUSINESS_FACT_FIELD_FORBIDDEN_TOKENS",
    "RELATIONAL_LAYER_PERMITTED_OUTPUTS",
    "FORBIDDEN_SIDE_EFFECT_SYMBOLS",
    "ARCHITECTURAL_RULE_TEXT",
]
