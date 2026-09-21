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
        install_handover_rows(harness)
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


# ── Observability cannot abandon an acknowledged batch (re-review) ───────────


def test_a_persistently_failing_observability_sync_does_not_escape_the_handler(pilot_on):
    """Both provider entry points acknowledge before processing, so an exception
    escaping this handler does not stay local: it abandons the rest of the
    batch — the next message, and the status receipts after it."""
    seen = Seen()

    def always_explode(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("observability is down")

    harness = drive(seen, event_id="wamid.route.10", gates=[patch(
        "modules.ai.brain.persona_ownership.sync_persona_to_turn_trace",
        side_effect=always_explode)])
    # The turn still ran and still belongs to the runtime…
    assert len(seen.runtime_turns) == 1
    assert seen.v2_owner == []
    # …and the handler returned rather than raising out of it.
    assert legacy_rows(harness) == []


def test_the_turn_after_a_failed_sync_is_still_processed(pilot_on):
    """One turn's telemetry failure must not take the next turn with it."""
    def always_explode(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("observability is down")

    for event_id in ("wamid.route.11a", "wamid.route.11b"):
        seen = Seen()
        drive(seen, event_id=event_id, gates=[patch(
            "modules.ai.brain.persona_ownership.sync_persona_to_turn_trace",
            side_effect=always_explode)])
        assert len(seen.runtime_turns) == 1, event_id


# ── A claim is sticky: it silences, it never transfers (F2/F3, re-review) ────


class _BarrierRow:
    """One handover barrier row, in its real shape."""

    def __init__(self, state: str, generation: int = 4) -> None:
        from core.commerce_runtime import handover

        self.tenant_id = TENANT
        self.namespace = handover.NAMESPACE
        self.state = state
        self.generation = generation
        self.opened_at = None
        self.settled_at = None
        self.evidence: Dict[str, Any] = {}


def install_handover_rows(harness: Any, *, barrier: Any = None) -> None:
    """Answer the handover reads with rows, not with a bare mock.

    Only the row *lookup* is a double — the rows are the production shape and
    every line that reads them is production. The durable tables themselves are
    proved on real PostgreSQL in
    ``test_commerce_runtime_pilot_handover_controls_pg.py``.
    """
    from unittest.mock import MagicMock

    ordinary = harness.db.query
    answers = {"HandoverBarrier": barrier, "HandoverWorker": None, "DeferredInbound": None}

    def _query(model: Any, *rest: Any) -> Any:
        name = str(getattr(model, "__name__", ""))
        if name not in answers:
            return ordinary(model, *rest)
        row = answers[name]
        chain = MagicMock()
        for tail in (chain.filter.return_value,
                     chain.filter.return_value.order_by.return_value,
                     chain.filter.return_value.with_for_update.return_value):
            tail.first.return_value = row
            tail.all.return_value = [] if row is None else [row]
            tail.count.return_value = 0
        return chain

    harness.db.query = MagicMock(side_effect=_query)


def draining_barrier(harness: Any, *, generation: int = 4) -> None:
    from core.commerce_runtime import handover

    install_handover_rows(harness, barrier=_BarrierRow(handover.STATE_DRAINING, generation))


def composed(seen: Seen, *, event_id: str, seam_outcome: Any,
             text: str = "عندكم فستان؟", gates: Any = (), barrier: bool = False) -> Any:
    """The dispatcher's real claim, carried into the real merchant handler.

    The claim is established by the production function, against the real guard
    and the real admitted-turn lookup, and handed to the real
    ``_handle_merchant_message`` the way ``_dispatch_message`` hands it — so the
    two halves are composed, not simulated. Only the seam's own answer is
    scripted, which is the thing each case is about. (The dispatcher's *other*
    half — that a claim skips its short circuits — is proved on the real
    ``_dispatch_message`` in ``test_commerce_runtime_pilot_recovery.py``.)
    """
    import asyncio

    from core.inbound_lifecycle import EVENT_MESSAGE_SAVED, inbound_lifecycle_trace, \
        record_lifecycle
    import routers.whatsapp_webhook as webhook
    import services.commerce_runtime_pilot as seam

    claims: List[Any] = []

    async def _seam(**kwargs: Any) -> Any:
        seen.pilot_asked.append(kwargs)
        if isinstance(seam_outcome, BaseException):
            raise seam_outcome
        return seam_outcome

    with ExitStack() as stack:
        harness = stack.enter_context(H.incident_ctx(
            brain_return=H._brain_return(reply=H.GROUNDED_TEXT), script=H._script_accept_all))
        competing(stack, seen)
        for gate in gates:
            stack.enter_context(gate)
        draining_barrier(harness) if barrier else install_handover_rows(harness)

        # The production claim, established for real.
        claims.append(seam.commerce_runtime_claims_inbound(
            harness.db, tenant_id=TENANT, phone_id=H.PHONE_ID, to=CUSTOMER, text=text,
            wa_msg_id=event_id))

        stack.enter_context(patch.object(seam, "maybe_handle_with_commerce_runtime", _seam))
        with inbound_lifecycle_trace(
            provider="meta", phone_number_id=H.PHONE_ID,
            msg={"id": event_id, "type": "text", "text": {"body": text}, "from": CUSTOMER},
        ):
            record_lifecycle(EVENT_MESSAGE_SAVED, conversation_id=42)
            asyncio.run(webhook._handle_merchant_message(
                phone_id=H.PHONE_ID, to=CUSTOMER, text=text, tenant_id=TENANT,
                db=harness.db, wa_msg_id=event_id,
                commerce_runtime_claim=claims[0],
            ))
    harness.claims = claims                              # type: ignore[attr-defined]
    return harness


def assert_withheld(seen: Seen, harness: Any) -> None:
    """Claimed, not executed, and given to nobody else."""
    assert harness.claims and harness.claims[0] is not None      # positively established
    assert seen.v2_owner == []                                   # zero competing executions
    assert legacy_rows(harness) == [] and pilot_rows(harness) == []
    assert harness.provider.calls == [] and harness.catalog_sends == []


def test_a_claim_followed_by_a_guard_exception_silences_rather_than_transfers(pilot_on):
    seen = Seen()
    harness = composed(seen, event_id="wamid.sticky.1",
                       seam_outcome=RuntimeError("the guard exploded"))
    assert_withheld(seen, harness)


def test_a_claim_followed_by_a_guard_refusal_silences_rather_than_transfers(pilot_on):
    import services.commerce_runtime_pilot as seam

    seen = Seen()
    harness = composed(seen, event_id="wamid.sticky.2",
                       seam_outcome=seam.PilotResult(handled=False,
                                                     reason=pg.CONNECTION_NOT_VERIFIED))
    assert_withheld(seen, harness)


def test_a_finished_runtime_turn_whose_lookup_then_fails_is_still_not_transferred(pilot_on,
                                                                                   monkeypatch):
    """Positively identified as finished at the dispatcher; the later lookup
    raises. The turn stays the runtime's and nobody else answers it."""
    from core.commerce_runtime import recovery
    import services.commerce_runtime_pilot as seam

    # Claimed on the strength of a finished admitted turn…
    monkeypatch.setenv(pg.ENV_DRAINING, "true")
    lookups: List[int] = []

    def _lookup(**kwargs: Any) -> Any:
        lookups.append(1)
        if len(lookups) == 1:
            return recovery.AdmittedInbound(tenant_id=TENANT, turn_id=7, conversation_id=3,
                                            provider_message_id="wamid.sticky.3", finished=True)
        raise RuntimeError("the ledger became unreadable")

    monkeypatch.setattr(recovery, "admitted_turn_for", _lookup)
    seen = Seen()
    harness = composed(seen, event_id="wamid.sticky.3",
                       seam_outcome=seam.PilotResult(handled=False,
                                                     reason=pg.PILOT_DRAINING))
    assert harness.claims[0].basis == "admitted_finished"
    assert_withheld(seen, harness)


def test_non_allowlisted_traffic_keeps_its_existing_behaviour(pilot_on, monkeypatch):
    """No claim, so the competing owner runs exactly as it does today."""
    monkeypatch.setenv(pg.ENV_RECIPIENT_ALLOWLIST, "+966500009999")
    seen = Seen()
    harness = composed(seen, event_id="wamid.sticky.4",
                       seam_outcome=None)                        # never reached
    assert harness.claims == [None]
    assert len(seen.v2_owner) == 1                               # legacy owner answered


def test_a_silencing_gate_still_decides_before_the_claim_is_honoured(pilot_on):
    """The claim withholds the turn from other owners; it does not override a
    gate that says this conversation must not be answered at all."""
    seen = Seen()
    harness = composed(seen, event_id="wamid.sticky.5", seam_outcome=None,
                       gates=[patch("core.ai_pause_guard.should_skip_ai",
                                    return_value=(True, "manual_takeover"))])
    assert seen.pilot_asked == []                                # the gate returned first
    assert seen.v2_owner == []
    assert harness.outbound_rows == []


def test_a_drained_tenant_answers_with_nobody_rather_than_with_the_legacy_path(pilot_on):
    """Control B on the real handler: a handover never invites a second owner.

    The barrier is draining and a competing owner is switched on. The dispatcher
    claims the inbound, buffers it, and the handler returns — so the customer is
    not answered by a second runtime while the first may still have a send on
    the wire. Nothing about this is silence *chosen* over an answer: the inbound
    is recorded, and the operator disposes of it before the handover settles.
    """
    import services.commerce_runtime_pilot as seam

    seen = Seen()
    harness = composed(seen, event_id="wamid.drained.1", barrier=True,
                       seam_outcome=seam.PilotResult(
                           handled=True, reason=entry.HANDOVER_BARRIER))
    assert harness.claims[0] is not None
    assert harness.claims[0].basis == "drain_buffered"
    assert_withheld(seen, harness)


def test_a_drain_does_not_change_what_happens_to_traffic_it_never_owned(pilot_on, monkeypatch):
    """The same drained barrier, a recipient outside the allowlist: unchanged."""
    monkeypatch.setenv(pg.ENV_RECIPIENT_ALLOWLIST, "+966500009999")
    seen = Seen()
    harness = composed(seen, event_id="wamid.drained.2", barrier=True, seam_outcome=None)
    assert harness.claims == [None]
    assert len(seen.v2_owner) == 1                               # exactly today's behaviour


def test_a_claim_established_for_another_turn_is_not_honoured(pilot_on):
    """Scope is checked: a claim is about one inbound message, not a mood."""
    import routers.whatsapp_webhook as webhook
    import services.commerce_runtime_pilot as seam

    other = seam.RuntimeClaim(tenant_id=TENANT, recipient="+966500000099",
                              provider_message_id="wamid.someone.else", basis="configured")
    assert webhook._commerce_runtime_claim_holds(
        other, tenant_id=TENANT, to=CUSTOMER, wa_msg_id="wamid.this.one") is False
    assert webhook._commerce_runtime_claim_holds(
        other, tenant_id=TENANT + 1, to=CUSTOMER, wa_msg_id="wamid.someone.else") is False
    assert webhook._commerce_runtime_claim_holds(None, tenant_id=TENANT, to=CUSTOMER,
                                                 wa_msg_id="x") is False
    assert webhook._commerce_runtime_claim_holds(object(), tenant_id=TENANT, to=CUSTOMER,
                                                 wa_msg_id="x") is False


def test_a_claim_for_this_turn_is_honoured_and_an_uncheckable_one_is_held():
    import routers.whatsapp_webhook as webhook
    import services.commerce_runtime_pilot as seam

    mine = seam.RuntimeClaim(tenant_id=TENANT, recipient="+966500000099",
                             provider_message_id="wamid.mine", basis="configured")
    assert webhook._commerce_runtime_claim_holds(
        mine, tenant_id=TENANT, to=CUSTOMER, wa_msg_id="wamid.mine") is True

    class _Uncheckable:
        def applies_to(self, **_kwargs: Any) -> bool:
            raise RuntimeError("cannot check")

    # Establishing a claim is positive; failing to re-check it is not a release.
    assert webhook._commerce_runtime_claim_holds(
        seam.RuntimeClaim(tenant_id=TENANT, recipient="x", provider_message_id="y",
                          basis="configured"),
        tenant_id=TENANT, to=CUSTOMER, wa_msg_id="y") is False   # recipient really differs


# ── COD is an owner too, and it acts before the seam ─────────────────────────
#
# The cash-on-delivery branches are not "another reply path". They transition an
# order and send a customer-visible follow-up, and they sit *above* the pilot
# seam in the merchant handler. An allowlisted "نعم" therefore never reached the
# runtime at all: the order moved and the customer was answered by COD.


def cod_seen() -> Dict[str, List[Any]]:
    return {"classified": [], "handled": [], "followup": []}


def cod_watched(stack: ExitStack, record: Dict[str, List[Any]], *,
                order: Any = None) -> None:
    """Watch the three COD entry points the handler can reach, for real.

    ``classify_cod_reply`` is left alone — "نعم" really is a confirm — so what
    is observed is whether the branch *runs*, not whether it would match.
    """
    import routers.whatsapp_webhook as webhook
    import services.cod_confirmation as cod

    real_classify = cod.classify_cod_reply

    def _classify(text: str) -> Any:
        decision = real_classify(text)
        record["classified"].append({"text": text, "decision": decision})
        return decision

    async def _handle(_db: Any, **kwargs: Any) -> Any:
        record["handled"].append(kwargs)
        return ("confirm", order if order is not None else object())

    async def _followup(**kwargs: Any) -> None:
        record["followup"].append(kwargs)

    stack.enter_context(patch.object(cod, "classify_cod_reply", _classify))
    stack.enter_context(patch.object(cod, "handle_cod_reply", _handle))
    stack.enter_context(patch.object(webhook, "_send_cod_followup_message", _followup))


def drive_cod(seen: Seen, record: Dict[str, List[Any]], *, event_id: str,
              text: str = "نعم", claim: Any = "establish") -> Any:
    """One real inbound "نعم", through the production claim and the real handler."""
    import asyncio

    from core.inbound_lifecycle import EVENT_MESSAGE_SAVED, inbound_lifecycle_trace, \
        record_lifecycle
    import routers.whatsapp_webhook as webhook
    import services.commerce_runtime_pilot as seam

    claims: List[Any] = []

    async def _seam(**kwargs: Any) -> Any:
        seen.pilot_asked.append(kwargs)
        return seam.PilotResult(handled=True, reason=entry.HANDLED, report=report())

    with ExitStack() as stack:
        harness = stack.enter_context(H.incident_ctx(
            brain_return=H._brain_return(reply=H.GROUNDED_TEXT), script=H._script_accept_all))
        competing(stack, seen)
        install_handover_rows(harness)
        cod_watched(stack, record)

        claims.append(
            seam.commerce_runtime_claims_inbound(
                harness.db, tenant_id=TENANT, phone_id=H.PHONE_ID, to=CUSTOMER, text=text,
                wa_msg_id=event_id)
            if claim == "establish" else claim)

        stack.enter_context(patch.object(seam, "maybe_handle_with_commerce_runtime", _seam))
        with inbound_lifecycle_trace(
            provider="meta", phone_number_id=H.PHONE_ID,
            msg={"id": event_id, "type": "text", "text": {"body": text}, "from": CUSTOMER},
        ):
            record_lifecycle(EVENT_MESSAGE_SAVED, conversation_id=42)
            asyncio.run(webhook._handle_merchant_message(
                phone_id=H.PHONE_ID, to=CUSTOMER, text=text, tenant_id=TENANT,
                db=harness.db, wa_msg_id=event_id, commerce_runtime_claim=claims[0],
            ))
    harness.claims = claims                              # type: ignore[attr-defined]
    return harness


def test_the_cod_branch_takes_an_allowlisted_yes_while_the_pilot_is_off(pilot_off):
    """Proof the competing owner is real: it decides, acts and answers."""
    seen, record = Seen(), cod_seen()
    harness = drive_cod(seen, record, event_id="wamid.cod.off", claim=None)
    assert harness.claims == [None]
    assert len(record["handled"]) == 1                   # the order transition ran
    assert len(record["followup"]) == 1                  # and the customer was answered
    assert seen.pilot_asked == []                        # the seam was never reached


def test_an_allowlisted_yes_is_the_runtime_s_and_never_reaches_cod(pilot_on):
    """The claim is honoured above COD: no classification, no action, no send."""
    seen, record = Seen(), cod_seen()
    harness = drive_cod(seen, record, event_id="wamid.cod.claimed")
    assert harness.claims[0] is not None and harness.claims[0].basis == "configured"
    assert record["classified"] == []                    # not even asked
    assert record["handled"] == [] and record["followup"] == []
    assert len(seen.pilot_asked) == 1                    # the runtime got the turn
    assert seen.v2_owner == []


def test_a_non_allowlisted_yes_keeps_todays_cod_behaviour(pilot_on, monkeypatch):
    monkeypatch.setenv(pg.ENV_RECIPIENT_ALLOWLIST, "+966500009999")
    seen, record = Seen(), cod_seen()
    harness = drive_cod(seen, record, event_id="wamid.cod.stranger")
    assert harness.claims == [None]
    assert len(record["handled"]) == 1 and len(record["followup"]) == 1
