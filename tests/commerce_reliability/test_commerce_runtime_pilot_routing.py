"""Where the routing decision sits, proved on the real WhatsApp handler.

A routing decision asked after the paths it is supposed to route around is not
a routing decision: it only ever sees the turns no other owner wanted. These
cases drive the real ``_handle_merchant_message`` through PR #1084's incident
harness — the real gates, the real owners, the real send path down to a scripted
transport — with a competing owner **switched on**, and hold the seam to three
things:

* every gate that can silence a turn still decides before the pilot is asked;
* a competing owner that would otherwise take the turn never runs once the
  pilot has taken it;
* nothing after ``handled`` can hand the customer back, not even a failure in
  observability.

Two things are doubles and are named as such: the commerce runtime turn itself
(``run_commerce_runtime_turn``) and the WhatsApp connection lookup, both proved
elsewhere on real PostgreSQL. What is proved *here* is placement.
"""
from __future__ import annotations

from contextlib import ExitStack
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from core.commerce_runtime import pilot_guard as pg
from core.commerce_runtime import runtime_entry as entry
from tests.commerce_reliability import runtime_support as rs

H = rs.load_pr1084_harness()

TENANT = H.TENANT_ID
CUSTOMER = "966500000099"
MODEL = "model-configured-for-this-pilot"


@pytest.fixture()
def pilot_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(pg.ENV_ENABLED, "true")
    monkeypatch.setenv(pg.ENV_TENANT_ALLOWLIST, str(TENANT))
    monkeypatch.setenv(pg.ENV_RECIPIENT_ALLOWLIST, CUSTOMER)
    monkeypatch.setenv(pg.ENV_MODEL, MODEL)


@pytest.fixture()
def pilot_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(pg.ENV_ENABLED, raising=False)


def report(**overrides: Any) -> entry.TurnReport:
    fields: Dict[str, Any] = {
        "reason": entry.HANDLED, "tenant_id": TENANT, "conversation_id": 42, "turn_id": 1,
        "dispatch_status": "accepted", "provider_message_id": "wamid.RUNTIME",
        "delivery_sequence_id": 1, "reply_text": "رد التشغيل",
    }
    fields.update(overrides)
    return entry.TurnReport(**fields)


class Seen:
    """What each competing owner and the runtime actually did this turn."""

    def __init__(self) -> None:
        self.runtime_turns: List[Dict[str, Any]] = []
        self.v2_owner: List[Dict[str, Any]] = []
        self.pilot_asked: List[Dict[str, Any]] = []


def competing(stack: ExitStack, seen: Seen, *, v2_enabled: bool = True,
              runtime_outcome: Any = None) -> None:
    """Switch on the owner that sits directly after the seam, and watch both."""
    import modules.ai.commerce_agent_v2.ownership as v2_ownership
    import routers.whatsapp_webhook as webhook
    import services.commerce_runtime_pilot as seam

    stack.enter_context(patch.object(
        v2_ownership, "outbound_enabled_for_tenant", lambda _tenant: v2_enabled))

    async def _v2_owner(**kwargs: Any) -> Dict[str, Any]:
        seen.v2_owner.append(kwargs)
        return {"status": "ok", "model": "m", "text_sent": True, "presentations_sent": 0,
                "unsupported_presentations": 0}

    stack.enter_context(patch.object(
        webhook, "_run_and_deliver_commerce_v2_owner", _v2_owner))

    # The connection lookup and the runtime turn are the two doubles.
    stack.enter_context(patch.object(
        pg, "verified_connection", lambda db, **kwargs: (f"wa:{H.PHONE_ID}", "17")))

    def _run_turn(**kwargs: Any) -> Any:
        seen.runtime_turns.append(kwargs)
        if isinstance(runtime_outcome, BaseException):
            raise runtime_outcome
        return runtime_outcome if runtime_outcome is not None else report()

    stack.enter_context(patch.object(entry, "run_commerce_runtime_turn", _run_turn))

    real_seam = seam.maybe_handle_with_commerce_runtime

    async def _watched(**kwargs: Any) -> Any:
        seen.pilot_asked.append(kwargs)
        return await real_seam(**kwargs)

    stack.enter_context(patch.object(seam, "maybe_handle_with_commerce_runtime", _watched))
    # The history read needs no database on this path.
    stack.enter_context(patch.object(seam, "_history_rows", lambda db, **kwargs: []))


def drive(seen: Seen, *, event_id: str, text: str = "عندكم فستان؟",
          gates: Any = (), **kwargs: Any) -> Any:
    """One real inbound turn. ``gates`` are applied *after* the harness's own,
    so a test can make a gate deny where the harness makes it allow."""
    with ExitStack() as stack:
        harness = stack.enter_context(H.incident_ctx(
            brain_return=H._brain_return(reply=H.GROUNDED_TEXT), script=H._script_accept_all))
        competing(stack, seen, **kwargs)
        for gate in gates:
            stack.enter_context(gate)
        H.run_turn(harness, text=text, event_id=event_id, phone=CUSTOMER)
        return harness


def pilot_rows(harness: Any) -> List[Dict[str, Any]]:
    return [r for r in harness.outbound_rows
            if (r["extra_metadata"] or {}).get("chosen_path") == "commerce_runtime_pilot"]


def legacy_rows(harness: Any) -> List[Dict[str, Any]]:
    return [r for r in harness.outbound_rows
            if (r["extra_metadata"] or {}).get("chosen_path") != "commerce_runtime_pilot"]


# ── The decision is reached before any owner answers ─────────────────────────


def test_the_pilot_takes_the_turn_ahead_of_an_enabled_competing_owner(pilot_on):
    seen = Seen()
    harness = drive(seen, event_id="wamid.route.1")
    assert len(seen.runtime_turns) == 1                      # the commerce runtime ran
    assert seen.v2_owner == []                               # and the V2 owner did not
    assert legacy_rows(harness) == []                        # nothing legacy persisted
    assert len(pilot_rows(harness)) == 1                     # exactly the runtime's own reply
    assert harness.catalog_sends == []
    assert harness.provider.calls == []                      # the runtime's send is its own


def test_the_same_competing_owner_does_take_the_turn_while_the_pilot_is_off(pilot_off):
    """Proof the competitor is really enabled: with the pilot off it answers."""
    seen = Seen()
    drive(seen, event_id="wamid.route.2")
    assert seen.runtime_turns == []
    assert len(seen.v2_owner) == 1


def test_the_turn_reaches_the_pilot_with_the_conversation_the_handler_resolved(pilot_on):
    seen = Seen()
    drive(seen, event_id="wamid.route.3")
    asked = seen.pilot_asked[0]
    assert asked["tenant_id"] == TENANT and asked["to"] == CUSTOMER
    assert asked["ai_gate_skipped"] is False
    assert getattr(asked["convo"], "id", None) == seen.runtime_turns[0]["conversation_id"]


# ── Every silencing gate still decides first ─────────────────────────────────


def test_a_tenant_without_billing_access_is_silent_and_the_pilot_is_never_asked(pilot_on):
    seen = Seen()
    harness = drive(seen, event_id="wamid.route.4",
                    gates=[patch("core.billing.has_billing_access", return_value=False)])
    assert seen.pilot_asked == [] and seen.runtime_turns == []
    assert harness.outbound_rows == []


def test_a_tenant_at_its_conversation_quota_is_silent_and_the_pilot_is_never_asked(pilot_on):
    from types import SimpleNamespace

    seen = Seen()
    denied = SimpleNamespace(allowed=False, used_total=100, limit=100, reason="plan_limit")
    harness = drive(seen, event_id="wamid.route.5",
                    gates=[patch("core.wa_usage.check_limit", return_value=denied)])
    assert seen.pilot_asked == [] and seen.runtime_turns == []
    assert harness.outbound_rows == []


def test_a_paused_conversation_never_reaches_the_pilot(pilot_on):
    seen = Seen()
    drive(seen, event_id="wamid.route.6",
          gates=[patch("core.ai_pause_guard.should_skip_ai",
                       return_value=(True, "manual_takeover"))])
    assert seen.pilot_asked == [] and seen.runtime_turns == []


# ── Nothing after the route is taken may hand the turn back ──────────────────


def test_observability_failing_after_the_route_was_taken_does_not_reopen_legacy(pilot_on):
    """The observability sync on the seam's own return path is the one that runs
    immediately after ``handled``. It fails here, once, exactly there."""
    seen = Seen()
    raised: List[bool] = []

    def _explode_once_the_route_is_taken(*_a: Any, **_k: Any) -> None:
        if seen.pilot_asked and not raised:
            raised.append(True)
            raise RuntimeError("observability is down")

    harness = drive(seen, event_id="wamid.route.7", gates=[patch(
        "modules.ai.brain.persona_ownership.sync_persona_to_turn_trace",
        side_effect=_explode_once_the_route_is_taken)])
    assert raised == [True]                                  # it really did fail
    assert len(seen.runtime_turns) == 1
    assert seen.v2_owner == []                               # legacy stayed closed
    assert legacy_rows(harness) == []


def test_a_runtime_turn_that_fails_outright_still_keeps_the_turn(pilot_on):
    """A failed commerce-runtime turn is a failed turn, not a handover."""
    seen = Seen()
    drive(seen, event_id="wamid.route.8", runtime_outcome=RuntimeError("runtime exploded"))
    assert len(seen.runtime_turns) == 1
    assert seen.v2_owner == []


def test_a_turn_the_runtime_refused_before_running_goes_to_the_legacy_owner(pilot_on,
                                                                            monkeypatch):
    """A refusal is not a handover mid-turn: the runtime never started."""
    monkeypatch.setenv(pg.ENV_RECIPIENT_ALLOWLIST, "+966500000001")   # not this customer
    seen = Seen()
    drive(seen, event_id="wamid.route.9")
    assert seen.runtime_turns == []
    assert len(seen.v2_owner) == 1
