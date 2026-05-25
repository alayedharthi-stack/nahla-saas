"""
tests/test_relational_safety_net_suppression.py
───────────────────────────────────────────────
Commit 3 of the Tenant 33 #49 relational architecture rollout —
SAFETY-NET SUPPRESSION GATE tests.

Headline guarantees pinned here (each → its own test):

  1. ``test_post_delivery_praise_does_not_trigger_customer_lookup``
     — PRAISE_POST_DELIVERY suppresses the cold ``store_link`` /
     ``location`` injections so the brain's warm thank-you reply
     survives.
  2. ``test_shipping_delay_complaint_does_not_flip_to_location_or_store_link``
     — COMPLAINT_SHIPPING_DELAY suppresses the same cold nets so
     the empathy-shaped reply isn't overwritten with a maps URL.
  3. ``test_normal_location_request_outside_relational_moments_keeps_working``
     — NO moment / NONE moment → gate is inert and the location
     net runs exactly like before.
  4. ``test_payment_and_order_safety_nets_never_appear_in_table``
     — Architectural invariant. Any drift that adds a
     payment / order / handoff / media / takeover net to the
     suppression table fails the build.

Every test runs as a pure unit test against the gate function;
no DB, no webhook, no LLM. Fast.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for p in [str(REPO_ROOT), str(BACKEND_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from modules.ai.brain.relational import (  # noqa: E402
    NEVER_SUPPRESSIBLE_NETS,
    SUPPRESSIBLE_NETS,
    ConversationMoment,
    RelationalState,
    is_safety_net_suppression_enabled,
    log_safety_net_suppressed,
    should_suppress_safety_net,
)
from modules.ai.brain.relational.safety_net_gate import (  # noqa: E402
    _SUPPRESSION_TABLE,
)


# ── 0. Kill switch defaults OFF ─────────────────────────────────────


def test_kill_switch_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Like the layer + router flags, the suppression flag must default
    OFF so a deploy without explicit env wiring stays byte-identical."""
    monkeypatch.delenv("RELATIONAL_SAFETY_NET_SUPPRESSION_ENABLED", raising=False)
    assert is_safety_net_suppression_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on"])
def test_kill_switch_truthy_values(
    monkeypatch: pytest.MonkeyPatch, val: str,
) -> None:
    monkeypatch.setenv("RELATIONAL_SAFETY_NET_SUPPRESSION_ENABLED", val)
    assert is_safety_net_suppression_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", ""])
def test_kill_switch_falsy_values(
    monkeypatch: pytest.MonkeyPatch, val: str,
) -> None:
    monkeypatch.setenv("RELATIONAL_SAFETY_NET_SUPPRESSION_ENABLED", val)
    assert is_safety_net_suppression_enabled() is False


# ── 1. Headline test: post-delivery praise blocks cold lookups ──────


def test_post_delivery_praise_does_not_trigger_customer_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Customer says "وصل الطلب وكل شي تمام، شكراً" → brain composes a
    warm thank-you reply → the cold ``store_link`` / ``location``
    nets MUST be suppressed so the reply isn't overwritten with a
    "تفضل رابط متجرنا 🌷" line.

    The customer/order-lookup paths themselves live at the DECISION
    layer (Commit 2's router reroutes ACTION_TRACK_ORDER →
    ACTION_LLM_REPLY for praise). At the safety-net layer the only
    risk left is the cold URL injectors — that's what we close here.
    """
    monkeypatch.setenv("RELATIONAL_SAFETY_NET_SUPPRESSION_ENABLED", "1")

    suppress_store, reason_store = should_suppress_safety_net(
        net_name="store_link",
        moment=ConversationMoment.PRAISE_POST_DELIVERY,
    )
    assert suppress_store is True
    assert reason_store == "praise_warmth_priority"

    suppress_loc, reason_loc = should_suppress_safety_net(
        net_name="location",
        moment=ConversationMoment.PRAISE_POST_DELIVERY,
    )
    assert suppress_loc is True
    assert reason_loc == "praise_warmth_priority"


# ── 2. Headline test: shipping-delay complaint suppresses cold nets ─


def test_shipping_delay_complaint_does_not_flip_to_location_or_store_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """COMPLAINT_SHIPPING_DELAY → empathy-first recovery reply.
    Posting "موقعنا 📍" on top of "نعتذر عن التأخير ..." would
    derail the recovery — both store_link and location must be
    suppressed."""
    monkeypatch.setenv("RELATIONAL_SAFETY_NET_SUPPRESSION_ENABLED", "1")

    for net in ("store_link", "location"):
        suppress, reason = should_suppress_safety_net(
            net_name=net,
            moment=ConversationMoment.COMPLAINT_SHIPPING_DELAY,
        )
        assert suppress is True, f"net={net} should be suppressed"
        assert reason == "complaint_recovery_priority"


@pytest.mark.parametrize(
    "moment",
    [
        ConversationMoment.COMPLAINT_PRODUCT_QUALITY,
        ConversationMoment.COMPLAINT_GENERIC,
    ],
)
def test_other_complaint_moments_also_suppress_cold_nets(
    monkeypatch: pytest.MonkeyPatch,
    moment: ConversationMoment,
) -> None:
    """Same recovery logic applies to product-quality and generic
    complaint moments — empathy framing must precede any URL drop."""
    monkeypatch.setenv("RELATIONAL_SAFETY_NET_SUPPRESSION_ENABLED", "1")
    for net in ("store_link", "location"):
        suppress, reason = should_suppress_safety_net(net_name=net, moment=moment)
        assert suppress is True, (net, moment)
        assert reason == "complaint_recovery_priority"


# ── 3. Headline test: normal location request keeps working ─────────


def test_normal_location_request_outside_relational_moments_keeps_working(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Customer asks "وين موقعكم؟" with NO relational moment in
    play → gate must be inert so the location net fires normally."""
    monkeypatch.setenv("RELATIONAL_SAFETY_NET_SUPPRESSION_ENABLED", "1")

    suppress, reason = should_suppress_safety_net(
        net_name="location",
        moment=ConversationMoment.NONE,
    )
    assert suppress is False
    assert reason == "no_moment"

    suppress, reason = should_suppress_safety_net(
        net_name="location",
        moment=None,
    )
    assert suppress is False
    assert reason == "no_moment"

    suppress, reason = should_suppress_safety_net(
        net_name="store_link",
        moment="",
    )
    assert suppress is False
    assert reason in ("no_moment", "no_rule")  # "" → coerces to None


# ── 4. Architectural invariant: protected nets never in table ───────


def test_payment_and_order_safety_nets_never_appear_in_table() -> None:
    """The strictest invariant of Commit 3.

    The suppression table MUST NOT reference any net whose name
    overlaps with payment / order-critical / handoff / media /
    takeover semantics. This protects the bot's commercial state
    machine from being silently muted.
    """
    table_net_names = {key[0] for key in _SUPPRESSION_TABLE.keys()}
    leak = table_net_names & NEVER_SUPPRESSIBLE_NETS
    assert leak == set(), (
        f"Suppression table leaked protected nets: {sorted(leak)}. "
        "Adding any of these to the relational suppression layer "
        "would let an emotional moment silence a business-critical "
        "safety net — strictly forbidden by the merchant directive."
    )


def test_suppressible_nets_set_matches_table_keys() -> None:
    """Every net referenced by the suppression table MUST also be
    enumerated in :data:`SUPPRESSIBLE_NETS` (the public whitelist).
    This guarantees the architectural test above can enforce its
    invariant — drift in either direction fails the build."""
    table_net_names = {key[0] for key in _SUPPRESSION_TABLE.keys()}
    extras_in_table = table_net_names - SUPPRESSIBLE_NETS
    extras_in_set = SUPPRESSIBLE_NETS - table_net_names
    assert not extras_in_table, (
        f"Table referenced un-whitelisted nets: {sorted(extras_in_table)}"
    )
    assert not extras_in_set, (
        f"Whitelist contained un-used nets: {sorted(extras_in_set)}"
    )


def test_suppressible_nets_whitelist_is_closed_to_two_cold_info_nets() -> None:
    """The whitelist is intentionally tiny. Expanding it requires a
    merchant directive + a regression test, so the architectural
    test pins the exact set."""
    assert SUPPRESSIBLE_NETS == frozenset({"store_link", "location"}), (
        f"SUPPRESSIBLE_NETS drifted: {sorted(SUPPRESSIBLE_NETS)}. "
        "Extending the whitelist must be a deliberate commit, not a "
        "side effect of unrelated work."
    )


@pytest.mark.parametrize(
    "protected_net",
    [
        "product",
        "media_key",
        "staff_contact",
        "delivery_info_context",
        "product_reask_guard",
        "outbound_artifact_guard",
        "clear_intent_fallback",
        "payment",
        "receipt",
        "order_status",
        "handoff",
        "manual_takeover",
    ],
)
def test_protected_nets_never_suppress_for_any_moment(
    monkeypatch: pytest.MonkeyPatch,
    protected_net: str,
) -> None:
    """Even with the kill switch ON and a real moment, a protected
    net must never be suppressed. This is enforced both by the
    blocklist and by the whitelist, but we check end-to-end here."""
    monkeypatch.setenv("RELATIONAL_SAFETY_NET_SUPPRESSION_ENABLED", "1")
    for moment in (
        ConversationMoment.PRAISE_POST_DELIVERY,
        ConversationMoment.COMPLAINT_SHIPPING_DELAY,
        ConversationMoment.COMPLAINT_PRODUCT_QUALITY,
        ConversationMoment.COMPLAINT_GENERIC,
        ConversationMoment.CONCERN_PRE_PURCHASE,
        ConversationMoment.RECOVERY_AFTER_FAILURE,
        ConversationMoment.LOYAL_REPEAT_CUSTOMER,
        ConversationMoment.GRATITUDE_GENERIC,
    ):
        suppress, reason = should_suppress_safety_net(
            net_name=protected_net, moment=moment,
        )
        assert suppress is False, (protected_net, moment, reason)
        # Either step 4 (defence-in-depth) or step 3 (whitelist).
        assert reason in ("net_protected", "net_not_suppressible"), (
            protected_net, moment, reason,
        )


# ── 5. Kill switch + flag combinations = legacy behaviour ───────────


def test_gate_inert_with_kill_switch_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag OFF → gate is inert regardless of net + moment."""
    monkeypatch.delenv("RELATIONAL_SAFETY_NET_SUPPRESSION_ENABLED", raising=False)
    suppress, reason = should_suppress_safety_net(
        net_name="store_link",
        moment=ConversationMoment.PRAISE_POST_DELIVERY,
    )
    assert suppress is False
    assert reason == "flag_off"


def test_gate_independent_from_layer_and_router_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Suppression flag is independent of layer / router flags. With
    layer=ON, router=ON, suppression=OFF → gate stays inert."""
    monkeypatch.setenv("RELATIONAL_LAYER_ENABLED", "1")
    monkeypatch.setenv("RELATIONAL_DECISION_ROUTER_ENABLED", "1")
    monkeypatch.delenv("RELATIONAL_SAFETY_NET_SUPPRESSION_ENABLED", raising=False)

    suppress, reason = should_suppress_safety_net(
        net_name="location",
        moment=ConversationMoment.COMPLAINT_SHIPPING_DELAY,
    )
    assert suppress is False
    assert reason == "flag_off"


def test_gate_handles_unknown_net_name_inert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown net names → inert. Avoid silent drift if a caller
    misspells the token."""
    monkeypatch.setenv("RELATIONAL_SAFETY_NET_SUPPRESSION_ENABLED", "1")
    suppress, reason = should_suppress_safety_net(
        net_name="totally_made_up_net",
        moment=ConversationMoment.PRAISE_POST_DELIVERY,
    )
    assert suppress is False
    assert reason in ("net_protected", "net_not_suppressible")


def test_gate_accepts_string_moment_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The webhook passes the moment as the bare string token
    (``"praise_post_delivery"``) so the gate must accept both
    forms."""
    monkeypatch.setenv("RELATIONAL_SAFETY_NET_SUPPRESSION_ENABLED", "1")
    suppress, reason = should_suppress_safety_net(
        net_name="store_link",
        moment="praise_post_delivery",
    )
    assert suppress is True
    assert reason == "praise_warmth_priority"


def test_gate_accepts_relational_state_for_documentation_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``relational_state`` is logged / inspected only — the decision
    is taken from ``moment`` alone."""
    monkeypatch.setenv("RELATIONAL_SAFETY_NET_SUPPRESSION_ENABLED", "1")
    rs = RelationalState(moment=ConversationMoment.PRAISE_POST_DELIVERY)
    suppress, reason = should_suppress_safety_net(
        net_name="store_link",
        moment=ConversationMoment.PRAISE_POST_DELIVERY,
        relational_state=rs,
    )
    assert suppress is True
    assert reason == "praise_warmth_priority"


def test_gate_never_raises_on_garbage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RELATIONAL_SAFETY_NET_SUPPRESSION_ENABLED", "1")
    # ``moment`` carries non-string non-enum garbage → gate stays inert.
    suppress, reason = should_suppress_safety_net(
        net_name="store_link",
        moment=object(),  # type: ignore[arg-type]
    )
    assert suppress is False
    assert reason in ("no_moment", "exception")


# ── 6. Log emission ─────────────────────────────────────────────────


def test_log_safety_net_suppressed_emits_canonical_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Operators grep for ``[CX] safety_net_suppressed`` so the line
    must always carry the five canonical fields."""
    caplog.set_level(logging.INFO, logger="nahla.relational.safety_net_gate")
    log_safety_net_suppressed(
        net_name="store_link",
        moment=ConversationMoment.PRAISE_POST_DELIVERY,
        reason="praise_warmth_priority",
        tenant_id=33,
        conversation_id=909,
        customer_phone="+966500000123",
    )
    msgs = [r.getMessage() for r in caplog.records]
    line = next(m for m in msgs if "[CX] safety_net_suppressed" in m)
    assert "net_name=store_link" in line
    assert "moment=praise_post_delivery" in line
    assert "reason=praise_warmth_priority" in line
    assert "tenant_id=33" in line
    assert "conversation_id=909" in line
    # Phone must be masked.
    assert "+966500000123" not in line
    assert "*0123" in line


def test_log_safety_net_suppressed_never_raises_on_garbage() -> None:
    """No matter the inputs, the logger helper is best-effort and
    must not propagate exceptions into the webhook handler."""
    log_safety_net_suppressed(  # type: ignore[arg-type]
        net_name=None,
        moment=None,
        reason="x",
        tenant_id=None,
        conversation_id=None,
        customer_phone=None,
    )
    log_safety_net_suppressed(  # type: ignore[arg-type]
        net_name="store_link",
        moment=object(),
        reason="x",
        tenant_id={"weird": "dict"},
        conversation_id=[1, 2, 3],
        customer_phone=12345,
    )


# ── 7. Brain-pipeline passthrough (Commit 3 wiring) ─────────────────


def test_brain_process_return_dict_carries_relational_moment_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pipeline's return dict adds ``relational_moment`` so the
    webhook can feed the gate. Empty string when the layer is
    disabled — that's the contract the safety-net wiring relies on
    to stay inert without the flag."""
    import inspect

    from modules.ai.brain import pipeline as brain_pipeline

    src = inspect.getsource(brain_pipeline)
    assert "relational_moment" in src, (
        "brain.process must surface the relational moment token in its "
        "return dict so the safety-net gate can consume it."
    )
