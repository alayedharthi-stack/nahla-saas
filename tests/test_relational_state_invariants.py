"""
tests/test_relational_state_invariants.py
─────────────────────────────────────────
Architectural invariants for the relational layer (Commit 1 of the
Tenant 33 #49 rollout). These tests pin the merchant directive:

    Relational layer may shape the conversation, but must never
    fabricate business state.

If any of these tests fail, the architectural rule has been broken
and the build must not ship. Renaming or extending the relational
contract is a deliberate decision that requires updating these
tests with a written justification.
"""
from __future__ import annotations

import dataclasses
import inspect
from typing import Any, Dict, List

import pytest

from modules.ai.brain.relational import (
    ARCHITECTURAL_RULE_TEXT,
    BUSINESS_FACT_FIELD_FORBIDDEN_TOKENS,
    ConversationMoment,
    LifecycleStage,
    PostPurchaseWindow,
    RelationalState,
    Sentiment,
    Urgency,
    compute_relational_state,
)
from modules.ai.brain.relational import state as relational_state_module


# ── Invariant 1 — RelationalState carries no business-fact field ────


def test_relational_state_has_no_business_fact_fields() -> None:
    """The strictest invariant of the relational layer.

    A relational verdict may NEVER expose a field whose name implies
    business state (payment / order / shipment / tracking / SKU / …).
    The forbidden-token list is intentionally broad — false positives
    are cheap (rename the field), false negatives are catastrophic.
    """
    fields = dataclasses.fields(RelationalState)
    field_names_lower = [f.name.lower() for f in fields]
    for forbidden in BUSINESS_FACT_FIELD_FORBIDDEN_TOKENS:
        for fname in field_names_lower:
            assert forbidden not in fname, (
                f"RelationalState field {fname!r} contains forbidden "
                f"business-fact token {forbidden!r}. The relational "
                f"layer MUST NOT carry business state. See "
                f"contracts.BUSINESS_FACT_FIELD_FORBIDDEN_TOKENS."
            )


def test_relational_state_field_set_is_minimal() -> None:
    """RelationalState should expose ONLY the dimensions the
    architectural plan specifies. New fields must be added through
    a deliberate commit + this allow-list update."""
    expected = {
        "moment",
        "lifecycle_stage",
        "sentiment",
        "post_purchase_window",
        "urgency",
        "advisory_for_brain",
        "framing_directive",
        "reason",
    }
    actual = {f.name for f in dataclasses.fields(RelationalState)}
    assert actual == expected, (
        f"RelationalState fields drifted: extra={actual - expected}, "
        f"missing={expected - actual}. Update this allow-list AND "
        f"the architectural rule docstring if the change is intended."
    )


# ── Invariant 2 — compute is pure / never raises ────────────────────


def test_compute_relational_state_returns_inert_on_empty_input() -> None:
    rs = compute_relational_state(inbound_text=None)
    assert isinstance(rs, RelationalState)
    assert rs.moment == ConversationMoment.NONE
    assert rs.is_inert()


def test_compute_relational_state_handles_garbage_inputs() -> None:
    """Tolerance contract: no exception on any malformed input."""
    bad_inputs: List[Dict[str, Any]] = [
        {"inbound_text": ""},
        {"inbound_text": "x", "customer_profile": "not-a-dict"},  # type: ignore[dict-item]
        {"inbound_text": "x", "order_state": [1, 2, 3]},          # type: ignore[dict-item]
        {"inbound_text": "x", "conversation_summary": object()},  # type: ignore[dict-item]
        {"inbound_text": "x", "recent_customer_messages": "not-a-list"},  # type: ignore[dict-item]
        {"inbound_text": "x", "last_shipment_event_at": "not-a-datetime"},  # type: ignore[dict-item]
        {"inbound_text": "x", "handoff_signals": "nope"},         # type: ignore[dict-item]
    ]
    for kwargs in bad_inputs:
        rs = compute_relational_state(**kwargs)  # type: ignore[arg-type]
        assert isinstance(rs, RelationalState), kwargs


def test_compute_relational_state_does_not_mutate_inputs() -> None:
    profile = {"total_orders": 3, "rfm_segment": "loyal"}
    order = {"order_status": "awaiting_receipt", "selected_product": {"id": 1}}
    summary = {"sentiment": "neutral", "tags": ["browsing"]}
    history = ["مرحبا", "كم سعر العسل؟"]
    snapshot_profile = dict(profile)
    snapshot_order = dict(order)
    snapshot_summary = {"sentiment": "neutral", "tags": list(summary["tags"])}
    snapshot_history = list(history)

    compute_relational_state(
        inbound_text="ابغى اطلب",
        customer_profile=profile,
        order_state=order,
        conversation_summary=summary,
        recent_customer_messages=history,
    )

    assert profile == snapshot_profile
    assert order == snapshot_order
    assert summary == snapshot_summary
    assert history == snapshot_history


def test_compute_relational_state_is_deterministic() -> None:
    kwargs = dict(
        inbound_text="شكرا وصل العسل ممتاز",
        intent_name="general",
        social_category="strong_praise",
        customer_profile={"total_orders": 2},
    )
    a = compute_relational_state(**kwargs)  # type: ignore[arg-type]
    b = compute_relational_state(**kwargs)  # type: ignore[arg-type]
    assert a == b


# ── Invariant 3 — compute does not call forbidden side-effect symbols


def test_compute_function_source_has_no_state_mutation_symbols() -> None:
    """Static guard: scan the source of the compute function for
    side-effect symbols. The relational layer must remain a pure
    classifier; if a future change wires it into ``apply_state_patch``
    or any other persistence symbol, this test fails."""
    src = inspect.getsource(relational_state_module._compute_unsafe)
    forbidden = (
        "apply_state_patch",
        "save_message",
        "_post_wa",
        "send_template",
        "create_handoff_session",
        "create_order",
        "mutate_brain_state",
    )
    for sym in forbidden:
        assert sym not in src, (
            f"compute_relational_state source references forbidden "
            f"side-effect symbol {sym!r}. The relational layer MUST "
            f"remain pure."
        )


# ── Invariant 4 — log helper never raises ───────────────────────────


def test_log_relational_state_never_raises_on_garbage() -> None:
    from modules.ai.brain.relational import log_relational_state

    log_relational_state(tenant_id=None, phone=None, state=RelationalState())  # type: ignore[arg-type]
    log_relational_state(tenant_id="abc", phone="", state=RelationalState())
    log_relational_state(
        tenant_id=33, phone="+966500000000",
        state=RelationalState(moment=ConversationMoment.PRAISE_POST_DELIVERY),
        extra={"any": "thing"},
    )


# ── Invariant 5 — architectural rule text stays canonical ───────────


def test_architectural_rule_text_canonical_phrasing() -> None:
    """If the merchant rewords the rule, change it in ONE place
    (contracts.ARCHITECTURAL_RULE_TEXT) and update this assertion.
    This pins the wording so a silent change is impossible."""
    expected_phrase = (
        "Relational layer may shape the conversation, but must never "
        "fabricate business state."
    )
    assert expected_phrase in ARCHITECTURAL_RULE_TEXT


# ── Invariant 6 — moment / lifecycle / sentiment enums are closed ───


def test_moment_lifecycle_sentiment_post_purchase_urgency_are_closed_enums() -> None:
    for enum_cls in (
        ConversationMoment,
        LifecycleStage,
        Sentiment,
        PostPurchaseWindow,
        Urgency,
    ):
        for member in enum_cls:
            assert isinstance(member.value, str), (
                f"{enum_cls.__name__}.{member.name} value must be a "
                f"stable string for log greppability."
            )


# ── Invariant 7 — the headline test the merchant directive demands ──


def test_relational_layer_never_confirms_payment_or_shipping_state() -> None:
    """HEADLINE TEST per the merchant directive (Tenant 33 #49):

        "أهم regression test أريدها:
         test_relational_layer_never_confirms_payment_or_shipping_state"

    Every conceivable trigger that could mislead the relational
    layer into asserting payment or shipping state is checked here.
    The verdict object must NEVER expose anything that a downstream
    consumer could read as 'payment confirmed' / 'order paid' /
    'shipment delivered'.
    """
    triggers = [
        "حولت لكم المبلغ على الحساب",       # text claim of transfer
        "دفعت كاش للمندوب",                  # payment claim
        "وصلت الشحنة الحمد لله",            # delivery claim
        "تم تسليم الطلب اليوم",             # delivery claim
        "الطلب مدفوع وجاهز للشحن",          # combined claim
        "أرسلت لكم إيصال التحويل",          # receipt claim
    ]
    for text in triggers:
        rs = compute_relational_state(
            inbound_text=text,
            order_state={"order_status": "awaiting_receipt"},
        )
        assert isinstance(rs, RelationalState)
        # No advisory string asserts business state in imperative form.
        bad_phrases = (
            "confirm the payment",
            "تم استلام المبلغ",
            "تم استلام الإيصال",
            "تم تأكيد الدفع",
            "تم تأكيد الشحن",
        )
        for phrase in bad_phrases:
            assert phrase not in (rs.advisory_for_brain or "").lower(), (
                f"advisory_for_brain leaked a business-state assertion "
                f"for trigger {text!r}: {rs.advisory_for_brain!r}"
            )
        # The dataclass exposes no field whose NAME implies
        # business state — proven by
        # test_relational_state_has_no_business_fact_fields above.
        # We re-assert here so this headline test is self-contained.
        for f in dataclasses.fields(rs):
            for tok in BUSINESS_FACT_FIELD_FORBIDDEN_TOKENS:
                assert tok not in f.name.lower()


@pytest.mark.parametrize("moment", list(ConversationMoment))
def test_every_moment_has_a_stable_log_token(moment: ConversationMoment) -> None:
    """Every moment value must be a non-empty string — operators
    grep ``moment=...`` in production logs."""
    assert isinstance(moment.value, str) and moment.value
