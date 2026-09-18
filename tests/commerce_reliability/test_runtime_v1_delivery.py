"""Runtime delivery tests — real merchant handler, scripted brain, fake provider.

Every test drives ``_handle_merchant_message`` through PR #1084's incident
harness (imported unchanged) and judges the structured evidence with the
reliability evaluator. This is isolated application-boundary
characterisation: persistence is mocked (not durable delivery recording),
fixture labels are fixture identity (not knowledge-binding validation), and
the seam exposes no guardrail execution records; those dimensions are
reported NOT EVALUATED. Tests that observe a recorded baseline defect raise
the exact manifest marker only when the recorded cause is observed exactly;
any other cause fails on its own.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests.commerce_reliability import reliability_evaluator as ev
from tests.commerce_reliability import runtime_support as rs

H = rs.load_pr1084_harness()

TENANT = H.TENANT_ID
FIXTURES = ("provider:scripted_httpx", "brain:scripted")


def _expect(terminal: str, **overrides) -> ev.TurnExpectation:
    base = dict(
        tenant_id=TENANT, terminal=terminal, require_evidence_refs=True,
        required_fixture_bindings=FIXTURES, max_accepted_sends=1,
    )
    base.update(overrides)
    return ev.TurnExpectation(**base)


def _pick_buttons():
    return H.build_standard_pick_buttons(H._candidates())


# ── Current runtime behaviour (V1 owner) ─────────────────────────────────────


def test_v1_owner_accepted_send_reaches_closed_terminal() -> None:
    script = rs.RecordingScript(H._script_accept_all)
    with H.incident_ctx(
        brain_return=H._brain_return(reply=H.GROUNDED_TEXT, buttons=_pick_buttons()), script=script,
    ) as harness, rs.capture_persistence(harness) as rows:
        trace = H.run_turn(harness, text=H.INPUT_BROAD, event_id="wamid.in.rel.accepted")
    evidence = rs.build_turn_evidence(harness, trace, script, rows)
    verdict = ev.evaluate_turn(_expect(ev.TERMINAL_PROVIDER_ACCEPTED), evidence)
    assert verdict.passed, (verdict.blockers, evidence.notes)
    assert evidence.terminal_source == "lifecycle:end_ok+wamid"
    assert verdict.facts["transport_outcome"] == ev.TRANSPORT_ACCEPTED
    assert evidence.fallback_kind == ev.FALLBACK_NONE, evidence.notes["provenance"]
    assert len(script.responses) == 1 and script.responses[0]["type"] == "interactive"
    assert evidence.persisted_outbound and all(r["tenant_id"] == TENANT for r in evidence.persisted_outbound)
    # Dimensions this characterisation does not evaluate are reported, not implied.
    assert set(rs.DELIVERY_NOT_EVALUATED) <= set(verdict.facts["not_evaluated"])


def test_v1_owner_missing_provider_message_id_is_explicit_failure() -> None:
    def _accepted_without_wamid(payload, n):
        return H._Resp(200, {"messaging_product": "whatsapp", "messages": []})

    script = rs.RecordingScript(_accepted_without_wamid)
    with H.incident_ctx(
        brain_return=H._brain_return(reply=H.GROUNDED_TEXT, buttons=_pick_buttons()), script=script,
    ) as harness, rs.capture_persistence(harness) as rows:
        trace = H.run_turn(harness, text=H.INPUT_BROAD, event_id="wamid.in.rel.nowamid")
    evidence = rs.build_turn_evidence(harness, trace, script, rows)
    verdict = ev.evaluate_turn(_expect(ev.TERMINAL_EXPLICIT_FAILURE), evidence)
    assert verdict.passed, (verdict.blockers, evidence.notes)
    assert evidence.accepted_wamids == []
    assert evidence.terminal_source == f"lifecycle:{ev.LIFECYCLE_END_DELIVERY_FAILED}"
    # The legacy lifecycle says "failed"; the transport outcome stays UNKNOWN.
    assert verdict.facts["transport_outcome"] == ev.TRANSPORT_UNKNOWN
    assert len(script.responses) == 1, script.responses  # no second dispatch after the ambiguous 200
    assert harness.last_outcome.get("product_reply_outcome") == "ambiguous_provider_outcome"


def test_v1_owner_ambiguous_timeout_is_explicit_failure_without_resend() -> None:
    script = rs.RecordingScript(H._script_timeout)
    with H.incident_ctx(
        brain_return=H._brain_return(reply=H.GROUNDED_TEXT, buttons=_pick_buttons()), script=script,
    ) as harness, rs.capture_persistence(harness) as rows:
        trace = H.run_turn(harness, text=H.INPUT_BROAD, event_id="wamid.in.rel.timeout")
    evidence = rs.build_turn_evidence(harness, trace, script, rows)
    verdict = ev.evaluate_turn(_expect(ev.TERMINAL_EXPLICIT_FAILURE), evidence)
    assert verdict.passed, (verdict.blockers, evidence.notes)
    # One dispatch that may have reached the customer: never retried blindly.
    assert len(script.responses) == 1 and script.responses[0]["error"] == "ReadTimeout"
    assert verdict.facts["transport_outcome"] == ev.TRANSPORT_UNKNOWN  # legacy "failed" is not a known outcome
    assert harness.last_outcome.get("product_reply_outcome") == "ambiguous_provider_outcome"


def test_v1_owner_definitive_rejection_recovers_once_with_grounded_text() -> None:
    script = rs.RecordingScript(H._script_reject_interactive)
    with H.incident_ctx(
        brain_return=H._brain_return(reply=H.GROUNDED_TEXT, buttons=_pick_buttons()), script=script,
    ) as harness, rs.capture_persistence(harness) as rows:
        trace = H.run_turn(harness, text=H.INPUT_BROAD, event_id="wamid.in.rel.rejected")
    evidence = rs.build_turn_evidence(harness, trace, script, rows)
    verdict = ev.evaluate_turn(_expect(ev.TERMINAL_PROVIDER_ACCEPTED), evidence)
    assert verdict.passed, (verdict.blockers, evidence.notes)
    assert evidence.fallback_kind == ev.FALLBACK_EXPECTED_DELIVERY_RECOVERY, evidence.notes["provenance"]
    assert [r["type"] for r in script.responses] == ["interactive", "text"]
    assert [r["status"] for r in script.responses] == [400, 200]  # definitive rejection, then one text
    assert verdict.facts["transport_outcome"] == ev.TRANSPORT_ACCEPTED
    assert evidence.terminal_source == f"lifecycle:{ev.LIFECYCLE_END_DELIVERY_RECOVERED}+wamid"
    # The recovered text is grounded in the verified candidates only.
    body = harness.provider.text_bodies()[-1]
    assert any(c["title"] in body for c in H._candidates())


def test_duplicate_inbound_event_id_produces_no_second_send() -> None:
    script = rs.RecordingScript(H._script_accept_all)
    with H.incident_ctx(
        brain_return=H._brain_return(reply=H.GROUNDED_TEXT), script=script,
    ) as harness, rs.capture_persistence(harness) as rows:
        first = H.run_turn(harness, text=H.INPUT_BROAD, event_id="wamid.in.rel.dup")
        replay = H.run_turn(harness, text=H.INPUT_BROAD, event_id="wamid.in.rel.dup")
    evidence = rs.build_turn_evidence(harness, replay, script, rows)
    verdict = ev.evaluate_turn(_expect(ev.TERMINAL_PROVIDER_ACCEPTED, max_accepted_sends=1), evidence)
    assert verdict.passed, (verdict.blockers, evidence.notes)
    assert len(script.responses) == 1, script.responses
    assert evidence.accepted_wamids == ["wamid.accepted.1"]
    assert first.final_token in (ev.LIFECYCLE_END_OK, ev.LIFECYCLE_END_DELIVERY_RECOVERED)


def test_turn_touches_only_its_own_tenant() -> None:
    script = rs.RecordingScript(H._script_accept_all)
    with H.incident_ctx(
        brain_return=H._brain_return(reply=H.GROUNDED_TEXT, buttons=_pick_buttons()), script=script,
    ) as harness, rs.capture_persistence(harness) as rows:
        trace = H.run_turn(harness, text=H.INPUT_BROAD, event_id="wamid.in.rel.tenant")
    evidence = rs.build_turn_evidence(harness, trace, script, rows)
    assert evidence.tenant_ids_touched, "no tenant-bearing evidence recorded"
    assert set(evidence.tenant_ids_touched) == {TENANT}
    verdict = ev.evaluate_turn(_expect(ev.TERMINAL_PROVIDER_ACCEPTED), evidence)
    assert verdict.passed, verdict.blockers
    # The same evidence attributed to another tenant is rejected by the evaluator.
    foreign = ev.evaluate_turn(_expect(ev.TERMINAL_PROVIDER_ACCEPTED, tenant_id=TENANT + 1), evidence)
    assert not foreign.passed and any(b.startswith("tenant_mismatch:") for b in foreign.blockers)


# ── Recorded baseline defect (RB-02) ─────────────────────────────────────────


def test_guard_emptied_more_request_does_not_resend_shown_candidates(baseline) -> None:
    script = rs.RecordingScript(H._script_accept_all)
    with H.incident_ctx(
        brain_return=H._brain_return(reply=""), script=script, guard_empties_reply=True,
    ) as harness, rs.capture_persistence(harness) as rows:
        trace = H.run_turn(harness, text="وش غيرها؟", event_id="wamid.in.rel.more")
    evidence = rs.build_turn_evidence(harness, trace, script, rows)
    verdict = ev.evaluate_turn(_expect(ev.TERMINAL_PROVIDER_ACCEPTED), evidence)
    assert verdict.passed, (verdict.blockers, evidence.notes)  # delivery itself closes correctly
    assert evidence.fallback_kind == ev.FALLBACK_EXPECTED_DELIVERY_RECOVERY, evidence.notes["provenance"]
    shown = {c["title"] for c in H._candidates()}
    bodies = harness.provider.text_bodies()
    resent_shown = [b for b in bodies if any(t in b for t in shown)]
    if bodies and resent_shown:
        baseline.defect(
            "RB-02", "recovery_resends_shown_candidates",
            bodies=len(bodies), final_token=trace.final_token,
        )
    assert bodies and not resent_shown


# ── Recorded baseline defect (UC-01): reproduced current implementation defect ─


def test_v2_owner_rejected_send_reaches_closed_terminal(baseline) -> None:
    from modules.ai.commerce_agent_v2.output import CommerceReply  # noqa: PLC0415

    def _reject_text(payload, n):
        if payload.get("type") == "text":
            return H._Resp(400, {"error": {
                "message": "(#131047) Re-engagement message", "type": "OAuthException", "code": 131047,
            }})
        return H.accepted(f"wamid.accepted.{n}")

    scripted_result = SimpleNamespace(
        reply=CommerceReply(text="عندنا فستان سهرة أزرق بسعر 250 ريال"),
        status="completed", model="scripted-model", sdk_trace_id="trace-scripted",
    )
    script = rs.RecordingScript(_reject_text)
    with H.incident_ctx(brain_return=H._brain_return(reply=H.GROUNDED_TEXT), script=script) as harness, \
         rs.capture_persistence(harness) as rows, \
         patch("modules.ai.commerce_agent_v2.ownership.outbound_enabled_for_tenant", return_value=True), \
         patch("modules.ai.commerce_agent_v2.runner.run_commerce_agent", new=AsyncMock(return_value=scripted_result)), \
         patch("modules.ai.commerce_agent_v2.context.CommerceAgentContext.from_trusted_scope",
               return_value=SimpleNamespace(evidence={})), \
         patch("modules.ai.commerce_agent_v2.shadow.persist_commerce_agent_result", return_value=None):
        trace = H.run_turn(harness, text=H.INPUT_BROAD, event_id="wamid.in.rel.v2")
    evidence = rs.build_turn_evidence(harness, trace, script, rows)
    assert [r["status"] for r in script.responses] == [400], script.responses  # the provider rejected the text
    verdict = ev.evaluate_turn(_expect(ev.TERMINAL_EXPLICIT_FAILURE), evidence)
    # Unrelated tenant / evidence / dispatch-safety failures fail on their own:
    # only the exact recorded observation raises the allowance marker.
    observation = rs.classify_uc01_observation(
        blockers=verdict.blockers, final_token=trace.final_token, accepted_wamids=evidence.accepted_wamids,
        persisted_outbound_count=len(evidence.persisted_outbound), outcome_count=len(harness.outcomes),
        transport_outcome=verdict.facts["transport_outcome"],
    )
    assert not observation.startswith("changed_cause:"), (observation, verdict.blockers, evidence.notes)
    if observation == "recorded_defect":
        baseline.defect(
            "UC-01", "v2_owner_delivery_failure_ends_with_inferred_end_ok",
            blockers=list(verdict.blockers), transport_outcome=verdict.facts["transport_outcome"],
        )
    assert verdict.passed, verdict.blockers
