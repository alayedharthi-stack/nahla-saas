"""The OrderFlowV2 validation harness — capture, observation and evidence.

What is under test here is the VALIDATION apparatus, not the address path
it validates. The address path has its own regressions; these prove that
the apparatus cannot lie about it:

* an internal-E2E run either captures the outbound or stops the turn —
  there is no third outcome and no route to ``provider_send_message``;
* the facts a model-bound call carried are read at the boundary where the
  call happens, not reconstructed from what the turn ended up looking
  like;
* the captured payload, the persisted outbound metadata and the
  presentation receipt are bound to one turn, or the evidence is refused;
* ordinary and recovery are told apart by what executed, and the surface
  is a separate dimension, so an ordinary turn with nothing to offer is
  not mistaken for a recovery;
* the new evidence fields are signed and tamper-evident, and historical
  v2 artifacts still verify.

The provider adapter is substituted; the owner, composer, guards,
recovery and serializer are the real ones. No provider is contacted.

INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
PERSONA_CHANGED=NO
PHRASE_MAP_CHANGED=NO
KEYWORD_ROUTER_CHANGED=NO
CUSTOMER_REGEX_CHANGED=NO
"""
from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Any, Dict

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from core.acceptance_compose_observer import (  # noqa: E402
    STAGE_ORDINARY,
    STAGE_RECOVERY,
    compose_stage,
    model_bound_observation,
    observe_model_bound_call,
    record_model_bound_outcome,
    recorded_model_bound_calls,
)
from core.acceptance_execution_context import (  # noqa: E402
    InternalE2EOutboundCaptureUnavailable,
    capture_outbound_payload,
    captured_outbound_payloads,
    internal_conversational_e2e_context,
    outbound_capture_sink,
)
from services.internal_conversational_e2e_contract import (  # noqa: E402
    CODE_ADDRESS_EVIDENCE_INCOMPLETE,
    CODE_ADDRESS_EXPECTATION_UNMET,
    CODE_ADDRESS_EXPECTATIONS_MISSING,
    CODE_ADDRESS_FINAL_SOURCE_UNESTABLISHED,
    CODE_ADDRESS_CONTENT_UNBOUND,
    CODE_ADDRESS_TIMING_UNCORRELATED,
    CODE_ADDRESS_INJECTION_NOT_EXECUTED,
    CODE_ADDRESS_RECEIPT_MISMATCH,
    CODE_ADDRESS_EVIDENCE_MISSING,
    CODE_ADDRESS_EVIDENCE_UNBOUND,
    CODE_MODEL_CALL_EVIDENCE_INCOMPLETE,
    EVIDENCE_SCHEMA_VERSION,
    EVIDENCE_SCHEMA_VERSIONS_SUPPORTED,
    PATH_ORDINARY,
    PATH_RECOVERY,
    PATH_UNRESOLVED,
    SURFACE_BUTTONS,
    SURFACE_LIST,
    SURFACE_NONE,
    SURFACE_TEXT,
    address_turn_evidence_blockers,
    classify_execution_path,
    delivered_surface,
    sign_session_evidence,
    verify_session_evidence,
)

SESSION_ID = "11111111-2222-4333-8444-555555555555"
TENANT_ID = 4242
EVIDENCE_KEY = "unit-test-evidence-key"


def _context(tenant_id: int = TENANT_ID):
    return internal_conversational_e2e_context(
        session_id=SESSION_ID, tenant_id=tenant_id, allow_llm_inference=False,
    )


# ── Capture is fail-closed ────────────────────────────────────────────────


def test_capture_is_inert_without_an_acceptance_context():
    """Production is unchanged: no context, no capture, no interference.

    The empty return is what lets ``_post_wa`` fall through to its normal
    dispatch. Anything else here would change every production send.
    """
    assert (
        capture_outbound_payload(
            egress_kind="whatsapp_provider",
            operation="send_message",
            tenant_id=TENANT_ID,
            phone_id="pid",
            payload={"to": "x"},
        )
        == ""
    )


def test_a_valid_sink_captures_and_mints_one_delivery_id():
    captured: list[Any] = []
    with _context(), outbound_capture_sink(captured.append):
        delivery_id = capture_outbound_payload(
            egress_kind="whatsapp_provider",
            operation="send_message",
            tenant_id=TENANT_ID,
            phone_id="pid",
            payload={"to": "x", "text": {"body": "hello"}},
        )
    assert delivery_id.startswith("captured.")
    assert len(captured) == 1
    assert captured[0].delivery_id == delivery_id
    assert captured[0].to_audit_dict()["transport"] == "captured"
    # The payload is recorded as given, so the receipt can be derived
    # from what actually left rather than from what was intended.
    assert captured[0].payload["text"]["body"] == "hello"


@pytest.mark.parametrize(
    "install_sink,tenant,expected_reason",
    [
        (False, TENANT_ID, "capture_sink_absent"),
        (True, TENANT_ID + 1, "tenant_mismatch"),
        (True, 0, "requested_tenant_invalid"),
    ],
)
def test_capture_refuses_rather_than_dispatching(install_sink, tenant, expected_reason):
    """An unusable capture stops the turn. It never falls through.

    This is the whole point of the affordance: a run that cannot record
    what it sent must not be allowed to send it for real instead.
    """
    def _run() -> None:
        capture_outbound_payload(
            egress_kind="whatsapp_provider",
            operation="send_message",
            tenant_id=tenant,
            phone_id="pid",
            payload={"to": "x"},
        )

    with _context():
        if install_sink:
            with outbound_capture_sink(lambda record: None):
                with pytest.raises(InternalE2EOutboundCaptureUnavailable) as err:
                    _run()
        else:
            with pytest.raises(InternalE2EOutboundCaptureUnavailable) as err:
                _run()
    assert err.value.reason == expected_reason
    assert err.value.to_audit_dict()["transport"] == "not_captured"


def test_a_sink_that_raises_is_a_refusal_not_a_dispatch():
    def _explode(record: Any) -> None:
        raise RuntimeError("isolated sink failure")

    with _context(), outbound_capture_sink(_explode):
        with pytest.raises(InternalE2EOutboundCaptureUnavailable) as err:
            capture_outbound_payload(
                egress_kind="whatsapp_provider",
                operation="send_message",
                tenant_id=TENANT_ID,
                phone_id="pid",
                payload={"to": "x"},
            )
    assert err.value.reason == "capture_sink_failed"


def test_a_non_callable_sink_is_refused():
    with _context(), outbound_capture_sink("not-a-callable"):  # type: ignore[arg-type]
        with pytest.raises(InternalE2EOutboundCaptureUnavailable) as err:
            capture_outbound_payload(
                egress_kind="whatsapp_provider",
                operation="send_message",
                tenant_id=TENANT_ID,
                phone_id="pid",
                payload={"to": "x"},
            )
    assert err.value.reason == "capture_sink_absent"


def test_captures_do_not_leak_between_turns():
    """Two turns in one session never see each other's payloads."""
    first: list[Any] = []
    second: list[Any] = []
    with _context():
        with outbound_capture_sink(first.append):
            capture_outbound_payload(
                egress_kind="whatsapp_provider",
                operation="send_message",
                tenant_id=TENANT_ID,
                phone_id="pid",
                payload={"turn": 1},
            )
            assert len(captured_outbound_payloads()) == 1
        with outbound_capture_sink(second.append):
            assert captured_outbound_payloads() == ()
            capture_outbound_payload(
                egress_kind="whatsapp_provider",
                operation="send_message",
                tenant_id=TENANT_ID,
                phone_id="pid",
                payload={"turn": 2},
            )
            assert len(captured_outbound_payloads()) == 1
    assert len(first) == 1 and len(second) == 1
    assert first[0].payload["turn"] == 1 and second[0].payload["turn"] == 2
    # And nothing survives the block that installed it.
    assert captured_outbound_payloads() == ()


def test_concurrent_tasks_capture_into_their_own_sinks():
    """Context isolation holds across asyncio tasks, not just blocks."""
    async def _turn(marker: int, sink: list) -> None:
        with outbound_capture_sink(sink.append):
            await asyncio.sleep(0)
            capture_outbound_payload(
                egress_kind="whatsapp_provider",
                operation="send_message",
                tenant_id=TENANT_ID,
                phone_id="pid",
                payload={"turn": marker},
            )

    async def _main() -> None:
        a: list[Any] = []
        b: list[Any] = []
        with _context():
            await asyncio.gather(_turn(1, a), _turn(2, b))
        assert [r.payload["turn"] for r in a] == [1]
        assert [r.payload["turn"] for r in b] == [2]

    asyncio.run(_main())


# ── Observation at the boundary ───────────────────────────────────────────


def _metadata(goal: str, facts: Dict[str, Any]) -> Dict[str, Any]:
    return {"brain_state": {"response_goal": goal, "known_facts": dict(facts)}}


def test_observation_is_inert_without_an_acceptance_context():
    assert observe_model_bound_call(context_metadata=_metadata("g", {})) == -1
    assert recorded_model_bound_calls() == ()


def test_each_model_bound_call_is_recorded_with_its_declared_stage():
    """The stage comes from the branch that ran, the facts from the call."""
    with _context(), model_bound_observation():
        with compose_stage(STAGE_ORDINARY, collection_field="city", turn_ref="t1"):
            first = observe_model_bound_call(
                context_metadata=_metadata(
                    "collect_delivery_city",
                    {
                        "missing_field": "city",
                        "delivery_address_status": "accepted",
                        "google_maps_url": "https://maps.example/?q=24.7,46.6",
                    },
                )
            )
            record_model_bound_outcome(first, candidate_present=True)
        with compose_stage(STAGE_RECOVERY, collection_field="city", turn_ref="t1"):
            second = observe_model_bound_call(
                context_metadata=_metadata(
                    "collect_delivery_city",
                    {"missing_field": "city", "delivery_address_status": "accepted"},
                )
            )
            record_model_bound_outcome(
                second, candidate_present=False, fallback_reason="provider_call_raised",
            )
        calls = [c.to_dict() for c in recorded_model_bound_calls()]

    assert [c["call_index"] for c in calls] == [0, 1]
    assert [c["stage"] for c in calls] == [STAGE_ORDINARY, STAGE_RECOVERY]
    assert all(c["turn_ref"] == "t1" for c in calls)
    assert all(c["collection_field"] == "city" for c in calls)
    assert all(c["response_goal"] == "collect_delivery_city" for c in calls)
    assert all(c["missing_field"] == "city" for c in calls)
    assert all(c["delivery_address_status"] == "accepted" for c in calls)
    assert all(c["observed_at"] == "orchestrator_adapter" for c in calls)
    assert calls[0]["outcome_recorded"] and calls[1]["outcome_recorded"]
    assert calls[0]["candidate_present"] is True
    assert calls[1]["candidate_present"] is False


def test_the_maps_reference_is_recorded_as_presence_only():
    """Its presence is the operational fact; its value is customer data."""
    url = "https://maps.example/?q=24.7136,46.6753"
    with _context(), model_bound_observation():
        with compose_stage(STAGE_ORDINARY, collection_field="city", turn_ref="t1"):
            observe_model_bound_call(
                context_metadata=_metadata(
                    "collect_delivery_city", {"google_maps_url": url},
                )
            )
        recorded = [c.to_dict() for c in recorded_model_bound_calls()]
    assert recorded[0]["has_accepted_maps_reference"] is True
    assert url not in repr(recorded)


def test_observations_do_not_leak_between_turns():
    with _context():
        with model_bound_observation():
            with compose_stage(STAGE_ORDINARY, turn_ref="t1"):
                observe_model_bound_call(context_metadata=_metadata("a", {}))
            assert len(recorded_model_bound_calls()) == 1
        with model_bound_observation():
            assert recorded_model_bound_calls() == ()


# ── Path classification is separate from presentation ─────────────────────


@pytest.mark.parametrize(
    "provenance,expected",
    [
        ({"address_reply_composed": True}, PATH_ORDINARY),
        ({"address_claim_compose_attempted": False}, PATH_ORDINARY),
        ({"address_reply_recovered": True}, PATH_RECOVERY),
        ({"address_claim_send_suppressed": True}, PATH_RECOVERY),
        ({}, PATH_UNRESOLVED),
    ],
)
def test_the_path_comes_from_execution_not_from_the_surface(provenance, expected):
    assert classify_execution_path(provenance) == expected


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"interactive": {"type": "button"}}, SURFACE_BUTTONS),
        ({"interactive": {"type": "list"}}, SURFACE_LIST),
        ({"text": {"body": "x"}}, SURFACE_TEXT),
        ({}, SURFACE_NONE),
    ],
)
def test_the_surface_is_its_own_dimension(payload, expected):
    assert delivered_surface(payload) == expected


def test_an_ordinary_turn_with_no_choices_is_still_ordinary():
    """The misclassification this separation exists to prevent.

    A customer with no saved addresses gets an ordinary composed question
    carrying no choices. Reading the path off the surface would file that
    as a recovery and inflate the recovery rate with healthy turns.
    """
    provenance = {"address_reply_composed": True, "address_claim_compose_attempted": True}
    assert classify_execution_path(provenance) == PATH_ORDINARY
    assert delivered_surface({"text": {"body": "…"}}) == SURFACE_TEXT


# ── Address evidence completeness ─────────────────────────────────────────


CITY_EXPECTATIONS: Dict[str, Any] = {
    "collection_field": "city",
    "response_goal": "collect_delivery_city",
    "missing_field": "city",
    "delivery_address_status": "accepted",
    "requires_accepted_maps_reference": True,
}


def _model_call(**overrides: Any) -> Dict[str, Any]:
    call = {
        "call_index": 0,
        "compose_source": "fake-provider",
        "fallback_reason": "",
        "outcome_pending": False,
        "stage": STAGE_ORDINARY,
        "collection_field": "city",
        "turn_ref": "probe.city.0",
        "observed_at": "orchestrator_adapter",
        "response_goal": "collect_delivery_city",
        "missing_field": "city",
        "delivery_address_status": "accepted",
        "has_accepted_maps_reference": True,
        "address_bound": True,
        "outcome_recorded": True,
        "candidate_present": True,
    }
    call.update(overrides)
    return call


DELIVERY_ID = "captured." + "a" * 32


def _address_turn(**overrides: Any) -> Dict[str, Any]:
    record = {
        "turn_ref": "probe.city.0",
        "outbound_metadata_turn_ref": "probe.city.0",
        "outbound_message_id": "4242",
        "outbound_message_row_verified": True,
        "transport": "captured",
        "delivery_ids": [DELIVERY_ID],
        "captured_payload_digest": "sha256:" + "b" * 64,
        "captured_payload_digest_verified": True,
        # Two artifacts of one reply: the text capture observed leaving,
        # and the body the outbound writer persisted.
        "captured_text_digest": "sha256:" + "c" * 64,
        "persisted_body_digest": "sha256:" + "c" * 64,
        "captured_persisted_representation": "interactive_body",
        "captured_persisted_content_match": True,
        "content_binding_reason": "",
        "execution_path": PATH_ORDINARY,
        "delivered_surface": SURFACE_LIST,
        "failure_injection": "none",
        "injection_state": {"kind": "none", "site": "", "fired": 0, "armed": False},
        "compose_entered": True,
        "collection_field": "city",
        "receipt_action_ids": ["nahla_addr_select:offer:1"],
        "receipt_address_ids": ["1"],
        "recorded_action_ids": ["1"],
        "recorded_offer_id": "offer",
        "recorded_offer_delivery_ref": DELIVERY_ID,
        "outbound_provenance": {"compose_source": "llm", "address_reply_composed": True},
        "model_bound_calls": [_model_call()],
        "turn_timing": {
            "turn_id": "turn-1",
            "message_id": "probe.city.0",
            "conversation_id": 7,
            "tenant_id": 3,
            "total_turn_ms": 12,
            "llm_call_count": 1,
        },
        "turn_timing_expected": {
            "turn_id": "turn-1",
            "message_id": "probe.city.0",
            "conversation_id": 7,
            "tenant_id": 3,
        },
        "turn_timing_unavailable": False,
    }
    record.update(overrides)
    return record


def _blockers(record: Dict[str, Any], **kw: Any) -> list:
    return address_turn_evidence_blockers(
        record,
        expects_address_turn=kw.pop("expects_address_turn", True),
        expectations=kw.pop("expectations", CITY_EXPECTATIONS),
    )


def test_a_complete_address_record_is_accepted():
    assert _blockers(_address_turn()) == []


def test_an_optional_field_does_not_permit_silent_absence():
    """``address_turn`` is optional in the schema, never optional in fact."""
    assert _blockers(None, expects_address_turn=False) == []
    assert _blockers(None) == [CODE_ADDRESS_EVIDENCE_MISSING]
    assert _blockers({}) == [CODE_ADDRESS_EVIDENCE_MISSING]


def test_a_turn_that_states_no_expectations_cannot_be_accepted():
    """"Some string is present" is not an assertion.

    Without a declared field and goal, nothing the evidence contains can
    contradict anything — which is how a city turn asserting a missing
    delivery address was accepted.
    """
    assert CODE_ADDRESS_EXPECTATIONS_MISSING in _blockers(
        _address_turn(), expectations={},
    )


# The four conditions the review demonstrated still returned no blockers.


def test_a_wire_offer_the_platform_never_recorded_is_rejected():
    """Offered address 1, recorded address 999 — a receipt for nothing."""
    assert CODE_ADDRESS_RECEIPT_MISMATCH in _blockers(
        _address_turn(receipt_address_ids=["1"], recorded_action_ids=["999"]),
    )


def test_a_city_turn_asserting_a_missing_delivery_address_is_rejected():
    """The contradiction the whole harness exists to catch."""
    wrong = _address_turn(
        model_bound_calls=[
            _model_call(
                response_goal="collect_delivery_address",
                missing_field="delivery_address",
                delivery_address_status="",
                has_accepted_maps_reference=False,
            )
        ],
    )
    assert CODE_ADDRESS_EXPECTATION_UNMET in _blockers(wrong)


def test_an_undeclared_stage_is_rejected():
    """``unspecified`` means no branch declared itself; the path is unknown."""
    assert CODE_MODEL_CALL_EVIDENCE_INCOMPLETE in _blockers(
        _address_turn(model_bound_calls=[_model_call(stage="unspecified")]),
    )


def test_missing_outcome_or_provenance_is_rejected():
    assert CODE_MODEL_CALL_EVIDENCE_INCOMPLETE in _blockers(
        _address_turn(model_bound_calls=[_model_call(outcome_recorded=False)]),
    )
    assert CODE_ADDRESS_FINAL_SOURCE_UNESTABLISHED in _blockers(
        _address_turn(outbound_provenance={}),
    )


def test_a_declared_injection_that_never_fired_is_rejected():
    """A label is not a mechanism check."""
    assert CODE_ADDRESS_INJECTION_NOT_EXECUTED in _blockers(
        _address_turn(
            failure_injection="provider_error",
            injection_state={
                "kind": "provider_error", "site": "orchestrator_adapter",
                "fired": 0, "armed": True,
            },
        ),
    )
    assert CODE_ADDRESS_INJECTION_NOT_EXECUTED not in _blockers(
        _address_turn(
            failure_injection="provider_error",
            injection_state={
                "kind": "provider_error", "site": "orchestrator_adapter",
                "fired": 1, "armed": True,
            },
        ),
    )


def test_a_reconstructed_observation_is_not_an_observation():
    assert CODE_MODEL_CALL_EVIDENCE_INCOMPLETE in _blockers(
        _address_turn(
            model_bound_calls=[_model_call(observed_at="final_state_reconstruction")],
        ),
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"outbound_metadata_turn_ref": ""},
        {"outbound_metadata_turn_ref": "probe.other.9"},
        {"turn_ref": ""},
        {"captured_payload_digest": ""},
        {"outbound_message_id": ""},
        {"delivery_ids": []},
    ],
)
def test_three_artifacts_that_cannot_be_tied_together_prove_nothing(overrides):
    assert CODE_ADDRESS_EVIDENCE_UNBOUND in _blockers(_address_turn(**overrides))


def test_a_model_call_from_another_turn_breaks_the_binding():
    assert CODE_ADDRESS_EVIDENCE_UNBOUND in _blockers(
        _address_turn(model_bound_calls=[_model_call(turn_ref="probe.other.9")]),
    )


def test_an_unresolved_path_is_rejected():
    assert CODE_ADDRESS_EVIDENCE_INCOMPLETE in _blockers(
        _address_turn(execution_path=PATH_UNRESOLVED),
    )


def test_a_truthful_no_compose_turn_keeps_its_fallback_evidence():
    """Compose could not be ENTERED: zero model calls is the truth.

    That outcome must stay reportable — provided the record says so with
    the fallback provenance, rather than simply omitting everything.
    """
    honest = _address_turn(
        compose_entered=False,
        model_bound_calls=[],
        outbound_provenance={
            "compose_source": "fallback_deterministic",
            "fallback_reason": "address_reply_compose_unavailable",
            "address_claim_compose_attempted": False,
        },
    )
    assert _blockers(honest) == []

    silent = _address_turn(
        compose_entered=False,
        model_bound_calls=[],
        outbound_provenance={"compose_source": "llm"},
    )
    assert CODE_MODEL_CALL_EVIDENCE_INCOMPLETE in _blockers(silent)


# ── Signing, tamper rejection, v2 compatibility ───────────────────────────


def test_the_new_address_fields_are_signed_and_tamper_evident():
    payload = {
        "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
        "session_id": SESSION_ID,
        "turn_results": [{"address_turn": _address_turn()}],
    }
    signed = sign_session_evidence(payload, key=EVIDENCE_KEY)
    assert verify_session_evidence(signed, key=EVIDENCE_KEY)

    tampered = dict(signed)
    tampered["turn_results"] = [
        {"address_turn": _address_turn(execution_path=PATH_RECOVERY)}
    ]
    assert not verify_session_evidence(tampered, key=EVIDENCE_KEY)

    swapped = dict(signed)
    swapped["turn_results"] = [
        {
            "address_turn": _address_turn(
                model_bound_calls=[
                    {
                        **_address_turn()["model_bound_calls"][0],
                        "response_goal": "collect_delivery_address",
                    }
                ]
            )
        }
    ]
    assert not verify_session_evidence(swapped, key=EVIDENCE_KEY)


def test_historical_v2_artifacts_still_verify():
    """The verifier never reads the content version, so v2 is unaffected.

    A schema bump that silently invalidated every stored artifact would
    destroy the evidence it was meant to extend.
    """
    assert "internal_conversational_e2e_evidence_v2" in EVIDENCE_SCHEMA_VERSIONS_SUPPORTED
    v2 = {
        "evidence_schema_version": "internal_conversational_e2e_evidence_v2",
        "session_id": SESSION_ID,
        "turn_results": [{"status": "evaluated"}],
    }
    signed = sign_session_evidence(v2, key=EVIDENCE_KEY)
    assert verify_session_evidence(signed, key=EVIDENCE_KEY)
    assert signed["evidence_schema_version"].endswith("_v2")


def test_a_foreign_key_cannot_verify_an_artifact():
    signed = sign_session_evidence({"session_id": SESSION_ID}, key=EVIDENCE_KEY)
    assert not verify_session_evidence(signed, key="another-key")


# ── The real send boundary ────────────────────────────────────────────────
#
# Everything above tests the apparatus in isolation. These run the ACTUAL
# ``_post_wa`` — the same sanitizer, the same gate, the same dedup
# decision — with a recording stub standing in for the provider call, so
# "never reaches dispatch" is observed rather than asserted structurally.


def _sandbox_db():
    from sqlalchemy import JSON, create_engine  # noqa: PLC0415
    from sqlalchemy.dialects.postgresql import JSONB  # noqa: PLC0415
    from sqlalchemy.orm import sessionmaker  # noqa: PLC0415

    from models import Base, Customer, Tenant  # noqa: PLC0415

    engine = create_engine("sqlite:///:memory:")
    saved = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, orig in saved:
        col.type = orig
    db = sessionmaker(bind=engine)()
    # Tenant 1 is the platform tenant and is hard-denied by the identity
    # gate, so the probe tenant must never be the first row.
    db.add(Tenant(name="platform-placeholder", is_active=False))
    db.flush()
    tenant = Tenant(name=f"of2-probe-{uuid.uuid4().hex[:8]}", is_active=True)
    db.add(tenant)
    db.flush()
    db.add(
        Customer(
            tenant_id=tenant.id,
            phone="966500000000",
            normalized_phone="966500000000",
            acquisition_channel="probe",
        )
    )
    db.commit()
    return db, tenant


def _recording_provider(calls: list):
    async def _never_dispatch(*args: Any, **kwargs: Any):
        calls.append(kwargs)
        raise AssertionError("provider_send_message must never be reached")

    return _never_dispatch


def _run_post_wa(*, sink_list, tenant_id, db, provider_calls, context=True):
    """Drive the real ``_post_wa`` once, with the provider stubbed out."""
    from unittest.mock import patch  # noqa: PLC0415

    import routers.whatsapp_webhook as webhook  # noqa: PLC0415

    # A unique body per probe: outbound dedup is part of the real
    # boundary and would otherwise suppress the second identical send in
    # the same process, hiding whichever branch the test is about.
    payload = {
        "to": "966500000000",
        "type": "text",
        "text": {"body": f"probe body {uuid.uuid4().hex}"},
    }
    result_sink: Dict[str, Any] = {}

    async def _main():
        with patch.object(
            webhook, "provider_send_message", _recording_provider(provider_calls)
        ):
            if context is None:
                return await webhook._post_wa(
                    "pid", payload, tenant_id, "store", db, _result_sink=result_sink,
                )
            with _context(tenant_id):
                if sink_list is None:
                    return await webhook._post_wa(
                        "pid", payload, tenant_id, "store", db,
                        _result_sink=result_sink,
                    )
                with outbound_capture_sink(sink_list.append):
                    return await webhook._post_wa(
                        "pid", payload, tenant_id, "store", db,
                        _result_sink=result_sink,
                    )

    return asyncio.run(_main()), result_sink


def test_the_real_send_boundary_captures_instead_of_dispatching():
    db, tenant = _sandbox_db()
    captured: list = []
    provider_calls: list = []
    ok, sink = _run_post_wa(
        sink_list=captured, tenant_id=tenant.id, db=db, provider_calls=provider_calls,
    )
    assert ok is True
    assert provider_calls == [], "no provider call may be attempted"
    assert len(captured) == 1
    assert sink["wamid"] == captured[0].delivery_id
    assert sink["transport"] == "captured"
    # The payload recorded is the one the sanitizer produced, so a receipt
    # derived from it is a receipt for what actually left.
    assert sink["sent_payload"]["to"] == "966500000000"
    assert captured[0].payload["text"]["body"].startswith("probe body ")


def test_the_real_send_boundary_refuses_when_capture_is_unavailable():
    """Missing sink under an acceptance context: the turn stops here.

    This is the case the previous proposal got wrong. Removing the
    acceptance context does not demonstrate the guarantee — it just
    restores production behaviour. What has to be shown is that an
    internal-E2E run WITHOUT a usable sink refuses, rather than quietly
    dispatching for real.
    """
    db, tenant = _sandbox_db()
    provider_calls: list = []
    with pytest.raises(InternalE2EOutboundCaptureUnavailable) as err:
        _run_post_wa(
            sink_list=None, tenant_id=tenant.id, db=db, provider_calls=provider_calls,
        )
    assert err.value.reason == "capture_sink_absent"
    assert provider_calls == [], "the refusal must precede any dispatch attempt"


def test_without_an_acceptance_context_the_boundary_dispatches_as_before():
    """Production is untouched: dispatch is attempted exactly as it was.

    The recording stub raises on contact, so reaching it is what proves
    the capture never intercepts an ordinary send.
    """
    db, tenant = _sandbox_db()
    provider_calls: list = []
    ok, _sink = _run_post_wa(
        sink_list=None, tenant_id=tenant.id, db=db,
        provider_calls=provider_calls, context=None,
    )
    assert ok is False
    assert len(provider_calls) == 1, "the ordinary path must still reach dispatch"


# ── Runner and operator paths ─────────────────────────────────────────────
#
# The suite above exercises the pieces. These drive ``run_sandbox_of2_turn``
# itself, and the operator's scenario gate, because a harness whose own
# entry point is never executed proves nothing about a run.


def _of2_request(**overrides: Any):
    from services.internal_conversational_e2e_harness import (  # noqa: PLC0415
        SandboxOf2TurnRequest,
    )

    base = dict(
        session_id=SESSION_ID,
        scenario_id="city_recovery",
        turn_index=0,
        tenant_id=TENANT_ID,
        customer_phone="966500000000",
        phone_id="internal-direct-code-probe",
        text="الرياض",
        conversation=None,
        allowed_tenants=frozenset({TENANT_ID}),
        evidence_hmac_key=EVIDENCE_KEY,
        runtime_revision="abcdef1",
        database_identity_fingerprint="sha256:" + "0" * 64,
        network_attestation_id="att-1",
        llm_allowed_hosts=("api.example",),
        turn_ref="probe.city.0",
        allow_llm_inference=True,
        expects_address_turn=True,
        expectations=dict(CITY_EXPECTATIONS),
    )
    base.update(overrides)
    return SandboxOf2TurnRequest(**base)


class _Conversation:
    def __init__(self, tenant_id: int, conversation_id: int = 1):
        self.tenant_id = tenant_id
        self.id = conversation_id
        self.extra_metadata: Dict[str, Any] = {}


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({"tenant_id": 1, "allowed_tenants": frozenset({1})}, "tenant_1_hard_denied"),
        ({"tenant_id": 9999}, "tenant_not_allowlisted"),
        ({"conversation": _Conversation(TENANT_ID + 5)}, "conversation_tenant_mismatch"),
        ({"network_attestation_id": ""}, "sandbox_execution_attestation_incomplete"),
        ({"allow_llm_inference": False}, "llm_inference_not_explicitly_enabled"),
        ({"turn_ref": "not a safe ref!"}, "turn_ref_invalid"),
    ],
)
def test_the_runner_refuses_before_the_handler(overrides, expected):
    """Identity is enforced, not merely consulted.

    ``validate_explicit_tenant_id`` RETURNS blockers rather than raising.
    Calling it and discarding the result let tenant 1 and unallowlisted
    tenants through this gate; and the call itself used a keyword the
    function does not accept, so every request died with a TypeError
    before reaching the handler at all.
    """
    from services.internal_conversational_e2e_harness import (  # noqa: PLC0415
        _validate_of2_request,
    )

    fields = {
        "conversation": _Conversation(overrides.get("tenant_id", TENANT_ID)),
        **overrides,
    }
    request = _of2_request(**fields)
    with pytest.raises(ValueError) as err:
        _validate_of2_request(request)
    assert str(err.value) == expected


def test_a_valid_request_passes_the_runner_gate_and_reaches_the_handler():
    from services.internal_conversational_e2e_harness import (  # noqa: PLC0415
        run_sandbox_of2_turn,
    )

    db, tenant = _sandbox_db()
    convo = _Conversation(tenant.id)
    reached: list = []

    async def _handler(*args: Any, **kwargs: Any) -> None:
        reached.append(args)

    outcome = asyncio.run(
        run_sandbox_of2_turn(
            db=db,
            request=_of2_request(
                tenant_id=tenant.id,
                allowed_tenants=frozenset({tenant.id}),
                conversation=convo,
            ),
            handler=_handler,
        )
    )
    assert reached, "the handler must actually be invoked"
    # A handler that did nothing produces no capture and no model call, so
    # the evidence refuses it rather than reporting a PASS.
    assert outcome.evidence["verdict"] == "fail"
    assert CODE_ADDRESS_EVIDENCE_UNBOUND in outcome.evidence["blockers"]


def test_a_no_op_handler_cannot_report_a_pass():
    """The defect a defaulted ``expects_address_turn`` allowed.

    With the flag omitted, a handler that captured nothing and called no
    model still returned ``verdict=pass``. The operator now refuses a
    scenario that omits it, and the evidence refuses the record.
    """
    from services.internal_conversational_e2e_harness import (  # noqa: PLC0415
        run_sandbox_of2_turn,
    )

    db, tenant = _sandbox_db()

    async def _noop(*args: Any, **kwargs: Any) -> None:
        return None

    outcome = asyncio.run(
        run_sandbox_of2_turn(
            db=db,
            request=_of2_request(
                tenant_id=tenant.id,
                allowed_tenants=frozenset({tenant.id}),
                conversation=_Conversation(tenant.id),
            ),
            handler=_noop,
        )
    )
    assert outcome.evidence["verdict"] == "fail"
    assert outcome.evidence["address_turn"]["transport"] == "not_captured"
    assert outcome.evidence["address_turn"]["model_bound_calls"] == []


def test_a_real_egress_denial_is_serialized_not_crashed():
    """``EgressDenialAudit`` is a dataclass; ``to_audit_dict`` is the exception's.

    Calling the exception's method on the dataclass made any turn that
    triggered a genuine sandbox denial die with an AttributeError instead
    of reporting the denial.
    """
    from core.acceptance_execution_context import deny_external_egress  # noqa: PLC0415
    from services.internal_conversational_e2e_harness import (  # noqa: PLC0415
        run_sandbox_of2_turn,
    )

    db, tenant = _sandbox_db()

    async def _handler(*args: Any, **kwargs: Any) -> None:
        try:
            deny_external_egress(
                egress_kind="shipping", operation="create_shipment",
                tenant_id=tenant.id,
            )
        except Exception:  # the runtime catches its own denials
            pass

    outcome = asyncio.run(
        run_sandbox_of2_turn(
            db=db,
            request=_of2_request(
                tenant_id=tenant.id,
                allowed_tenants=frozenset({tenant.id}),
                conversation=_Conversation(tenant.id),
                expected_denials=(("shipping", "create_shipment"),),
            ),
            handler=_handler,
        )
    )
    audits = outcome.evidence["denial_audits"]
    assert len(audits) == 1
    assert audits[0]["egress_kind"] == "shipping"
    assert audits[0]["code"] == "internal_e2e_egress_denied"
    # Declared and observed, so neither blocker fires.
    assert "expected_egress_denial_missing" not in outcome.evidence["blockers"]
    assert "unexpected_egress_denial" not in outcome.evidence["blockers"]


def test_an_undeclared_denial_is_reported_as_unexpected():
    from core.acceptance_execution_context import deny_external_egress  # noqa: PLC0415
    from services.internal_conversational_e2e_harness import (  # noqa: PLC0415
        run_sandbox_of2_turn,
    )

    db, tenant = _sandbox_db()

    async def _handler(*args: Any, **kwargs: Any) -> None:
        try:
            deny_external_egress(
                egress_kind="salla_integration", operation="read_customer",
                tenant_id=tenant.id,
            )
        except Exception:
            pass

    outcome = asyncio.run(
        run_sandbox_of2_turn(
            db=db,
            request=_of2_request(
                tenant_id=tenant.id,
                allowed_tenants=frozenset({tenant.id}),
                conversation=_Conversation(tenant.id),
            ),
            handler=_handler,
        )
    )
    assert "unexpected_egress_denial" in outcome.evidence["blockers"]


# ── Injection actually fires at the seam it names ─────────────────────────


def test_a_declared_injection_installs_a_real_fault_at_the_adapter():
    """``failure_injection`` was a label copied into evidence.

    Nothing was installed, so ``none``, ``provider_error``,
    ``provider_timeout`` and ``guard_boundary`` all produced the same
    result and no mechanism was ever confirmed. Each now fires at the
    seam it names, and records that it did.
    """
    from core.acceptance_failure_injection import (  # noqa: PLC0415
        InjectedProviderFailure,
        arm_failure_injection,
        injection_state,
        maybe_inject_provider_failure,
    )

    with _context():
        with arm_failure_injection("provider_error"):
            with pytest.raises(InjectedProviderFailure):
                maybe_inject_provider_failure()
            state = injection_state()
        assert state["kind"] == "provider_error"
        assert state["site"] == "orchestrator_adapter"
        assert state["fired"] == 1

        with arm_failure_injection("provider_timeout"):
            with pytest.raises(asyncio.TimeoutError):
                maybe_inject_provider_failure()
            assert injection_state()["fired"] == 1

        # ``none`` arms nothing, so the boundary is untouched.
        with arm_failure_injection("none"):
            maybe_inject_provider_failure()
            assert injection_state()["armed"] is False


def test_a_guard_injection_fires_only_at_the_guard_seam():
    from core.acceptance_failure_injection import (  # noqa: PLC0415
        InjectedGuardFailure,
        arm_failure_injection,
        maybe_inject_guard_failure,
        maybe_inject_provider_failure,
    )

    with _context(), arm_failure_injection("guard_boundary"):
        # Not the adapter's fault to raise.
        maybe_inject_provider_failure()
        with pytest.raises(InjectedGuardFailure):
            maybe_inject_guard_failure()


def test_injection_is_inert_in_production():
    """No acceptance context, no fault — whatever a caller passes."""
    from core.acceptance_failure_injection import (  # noqa: PLC0415
        arm_failure_injection,
        maybe_inject_guard_failure,
        maybe_inject_provider_failure,
    )

    with arm_failure_injection("provider_error"):
        maybe_inject_provider_failure()
        maybe_inject_guard_failure()


def test_an_injected_provider_error_reaches_the_real_composer_chain():
    """Through the real composer, adapter and observation, end to end."""
    from unittest.mock import patch  # noqa: PLC0415

    import modules.ai.orchestrator.adapter as adapter  # noqa: PLC0415
    from core.acceptance_failure_injection import (  # noqa: PLC0415
        arm_failure_injection,
        injection_state,
    )
    from modules.ai.order_flow_v2.address_reply_recovery import (  # noqa: PLC0415
        compose_address_turn_reply,
    )

    pipeline_calls: list = []

    def _should_not_run(request: Any):
        pipeline_calls.append(request)
        raise AssertionError("the injected fault must precede the provider call")

    convo = _Conversation(TENANT_ID)
    convo.extra_metadata = {"brain_state": {"stage": "checkout", "order_prep": {}}}

    async def _main():
        with patch.object(adapter._pipeline, "run", _should_not_run):
            with _context(), model_bound_observation(), arm_failure_injection(
                "provider_error"
            ):
                with compose_stage(
                    STAGE_ORDINARY, collection_field="city", turn_ref="probe.city.0"
                ):
                    result = await compose_address_turn_reply(
                        None,
                        tenant_id=TENANT_ID,
                        conversation=convo,
                        customer_phone="966500000000",
                        message="الرياض",
                        known_facts={
                            "missing_field": "city",
                            "delivery_address_status": "accepted",
                            "google_maps_url": "https://maps.example/?q=1,2",
                        },
                        turn_ref="probe.city.0",
                        response_goal="collect_delivery_city",
                    )
                return result, [c.to_dict() for c in recorded_model_bound_calls()], dict(
                    injection_state()
                )

    result, calls, state = asyncio.run(_main())
    assert pipeline_calls == [], "the provider must never be reached"
    assert state["fired"] >= 1, "the injection has to have executed"
    # Every attempt is recorded and every one failed — the distinction
    # the ordinary/recovery accounting depends on. The composer makes a
    # second, legacy-path attempt of its own after the first fault; both
    # are observed rather than one being lost.
    assert len(calls) >= 1
    assert all(c["candidate_present"] is False for c in calls)
    assert calls[0]["response_goal"] == "collect_delivery_city"
    assert calls[0]["missing_field"] == "city"
    assert calls[0]["delivery_address_status"] == "accepted"
    assert calls[0]["has_accepted_maps_reference"] is True
    assert calls[0]["stage"] == STAGE_ORDINARY
    # And the composer reports its own failure honestly.
    assert result.compose_source == "fallback_deterministic"


# ── Captured sends complete the dedup lifecycle ───────────────────────────


def test_a_repeated_captured_send_sees_a_finished_first_attempt():
    """The first captured success used to leave a reservation in flight.

    ``check_outbound_send`` reserves the key before dispatch; capture
    returned before ``record_outbound_result``, so an identical second
    send — two consecutive fallback lines, say — observed an unfinished
    attempt instead of completed local processing.
    """
    from unittest.mock import patch  # noqa: PLC0415

    import routers.whatsapp_webhook as webhook  # noqa: PLC0415

    db, tenant = _sandbox_db()
    captured: list = []
    provider_calls: list = []
    recipient = f"96650{uuid.uuid4().int % 10_000_000:07d}"
    payload = {
        "to": recipient,
        "type": "text",
        "text": {"body": "one constant fallback line"},
    }
    first_sink: Dict[str, Any] = {}
    second_sink: Dict[str, Any] = {}

    async def _main():
        with patch.object(
            webhook, "provider_send_message", _recording_provider(provider_calls)
        ):
            with _context(tenant.id), outbound_capture_sink(captured.append):
                ok1 = await webhook._post_wa(
                    "pid", dict(payload), tenant.id, "store", db,
                    _result_sink=first_sink,
                )
                ok2 = await webhook._post_wa(
                    "pid", dict(payload), tenant.id, "store", db,
                    _result_sink=second_sink,
                )
        return ok1, ok2

    ok1, ok2 = asyncio.run(_main())
    assert provider_calls == []
    assert ok1 is True
    # A completed success, deduplicated — not an in-flight refusal.
    assert ok2 is True
    assert second_sink["duplicate_suppressed"] is True
    assert second_sink["wamid"] == captured[0].delivery_id
    assert len(captured) == 1


def test_a_capture_refusal_releases_the_dedup_reservation():
    """A refused send must not leave the key reserved for the next one."""
    from unittest.mock import patch  # noqa: PLC0415

    import routers.whatsapp_webhook as webhook  # noqa: PLC0415

    db, tenant = _sandbox_db()
    provider_calls: list = []
    captured: list = []
    recipient = f"96650{uuid.uuid4().int % 10_000_000:07d}"
    payload = {
        "to": recipient,
        "type": "text",
        "text": {"body": "refused then retried"},
    }

    async def _main():
        with patch.object(
            webhook, "provider_send_message", _recording_provider(provider_calls)
        ):
            with _context(tenant.id):
                # No sink: the send is refused, and the reservation it
                # took must be released on the way out.
                with pytest.raises(InternalE2EOutboundCaptureUnavailable):
                    await webhook._post_wa(
                        "pid", dict(payload), tenant.id, "store", db,
                    )
                # The retry now reaches capture rather than dedup.
                with outbound_capture_sink(captured.append):
                    return await webhook._post_wa(
                        "pid", dict(payload), tenant.id, "store", db,
                    )

    assert asyncio.run(_main()) is True
    assert len(captured) == 1, "the retry must reach capture, not a stale reservation"
    assert provider_calls == []


# ── C2: binding is verified, not shaped ───────────────────────────────────
#
# Each of these returned NO blockers before. A shape test — "starts with
# sha256:", "is a non-empty string" — is not a binding.


def test_an_action_id_naming_another_offer_and_address_is_rejected():
    """The wire ids must resolve to the addresses both sides claim.

    Comparing only ``receipt_address_ids`` against ``recorded_action_ids``
    left the ids that actually left unchecked, so an action naming a
    different offer AND a different address passed.
    """
    assert CODE_ADDRESS_RECEIPT_MISMATCH in _blockers(
        _address_turn(receipt_action_ids=["nahla_addr_select:different:999"]),
    )


def test_ids_from_two_different_showings_are_rejected():
    """One payload answers one question."""
    assert CODE_ADDRESS_RECEIPT_MISMATCH in _blockers(
        _address_turn(
            receipt_action_ids=[
                "nahla_addr_select:offer:1",
                "nahla_addr_select:other:2",
            ],
            receipt_address_ids=["1", "2"],
            recorded_action_ids=["1", "2"],
        ),
    )


def test_a_malformed_action_id_is_rejected():
    assert CODE_ADDRESS_RECEIPT_MISMATCH in _blockers(
        _address_turn(receipt_action_ids=["not-an-action-id"]),
    )


@pytest.mark.parametrize(
    "overrides,label",
    [
        ({"outbound_message_id": ""}, "no row id at all"),
        ({"outbound_message_row_verified": False}, "row never verified"),
        ({"delivery_ids": ["foreign-delivery"]}, "foreign delivery id"),
        ({"captured_payload_digest": "sha256:not-a-digest"}, "digest of nothing"),
        ({"captured_payload_digest_verified": False}, "digest never recomputed"),
        ({"recorded_offer_delivery_ref": "some-other-delivery"}, "offer recorded against another delivery"),
        ({"recorded_offer_delivery_ref": ""}, "offer names no delivery"),
    ],
)
def test_a_foreign_or_unverified_artifact_is_rejected(overrides, label):
    assert CODE_ADDRESS_EVIDENCE_UNBOUND in _blockers(
        _address_turn(**overrides)
    ), label


def test_final_provenance_must_name_a_source():
    """"Non-empty mapping" established nothing about the final text."""
    assert CODE_ADDRESS_FINAL_SOURCE_UNESTABLISHED in _blockers(
        _address_turn(outbound_provenance={"address_turn_ref": "probe.city.0"}),
    )
    # A source outside the doctrine's closed set is not a source either.
    assert CODE_ADDRESS_FINAL_SOURCE_UNESTABLISHED in _blockers(
        _address_turn(outbound_provenance={"compose_source": "template"}),
    )
    # And a fallback has to say why it fell back.
    assert CODE_ADDRESS_FINAL_SOURCE_UNESTABLISHED in _blockers(
        _address_turn(outbound_provenance={"compose_source": "fallback_deterministic"}),
    )


def test_a_call_without_candidate_or_fallback_detail_is_rejected():
    stripped = {
        k: v for k, v in _model_call().items() if k != "candidate_present"
    }
    assert CODE_MODEL_CALL_EVIDENCE_INCOMPLETE in _blockers(
        _address_turn(model_bound_calls=[stripped]),
    )
    # Claimed a candidate but named no source.
    assert CODE_MODEL_CALL_EVIDENCE_INCOMPLETE in _blockers(
        _address_turn(
            model_bound_calls=[_model_call(candidate_present=True, compose_source="")],
        ),
    )
    # No candidate and no reason why.
    assert CODE_MODEL_CALL_EVIDENCE_INCOMPLETE in _blockers(
        _address_turn(
            model_bound_calls=[
                _model_call(candidate_present=False, fallback_reason="")
            ],
        ),
    )


def test_uncorrelated_or_silently_absent_timing_is_rejected():
    """Latency is acceptable correlated, or declared unavailable — not absent."""
    assert CODE_ADDRESS_TIMING_UNCORRELATED in _blockers(
        _address_turn(turn_timing={}, turn_timing_unavailable=False),
    )
    assert CODE_ADDRESS_TIMING_UNCORRELATED in _blockers(
        _address_turn(turn_timing={"total_turn_ms": 12}),
    )
    # Explicitly unavailable is a truthful outcome.
    assert CODE_ADDRESS_TIMING_UNCORRELATED not in _blockers(
        _address_turn(turn_timing={}, turn_timing_unavailable=True),
    )


def test_expectations_must_state_field_and_goal_and_missing_field():
    """``any`` accepted a manifest that asserted almost nothing."""
    for partial in (
        {"collection_field": "city"},
        {"response_goal": "collect_delivery_city"},
        {"collection_field": "city", "response_goal": "collect_delivery_city"},
    ):
        assert CODE_ADDRESS_EXPECTATIONS_MISSING in _blockers(
            _address_turn(), expectations=partial,
        ), partial


# ── C3: the acceptance cutoff is immutable ────────────────────────────────


def test_a_late_outcome_cannot_rewrite_a_sealed_record():
    """Sealing froze nothing: ``update`` never checked it.

    A call that started before the cutoff and finished after it
    overwrote its sealed record, and the late counter stayed at zero — so
    a timed-out call could be reported as answered.
    """
    from core.acceptance_compose_observer import _Recorder, ModelBoundCall  # noqa: PLC0415

    recorder = _Recorder()
    index = recorder.append(
        ModelBoundCall(
            call_index=0,
            stage=STAGE_ORDINARY,
            collection_field="city",
            turn_ref="t1",
            observed_at="orchestrator_adapter",
            response_goal="collect_delivery_city",
            missing_field="city",
            delivery_address_status="accepted",
            has_accepted_maps_reference=True,
            address_bound=True,
            stage_declared=True,
        )
    )
    recorder.seal()
    recorder.update(index, outcome_recorded=True, candidate_present=True)

    record = recorder.snapshot()[0]
    assert record.outcome_recorded is False, "a sealed record must not change"
    # And the call that never returned says so, rather than going silent.
    assert record.outcome_pending is True
    assert recorder.late_arrivals == 1
    assert recorder.late_breakdown() == {"late_appends": 0, "late_outcomes": 1}
    # A late APPEND is refused and counted separately.
    assert recorder.append(record) == -1
    assert recorder.late_breakdown()["late_appends"] == 1


def test_a_call_that_completes_after_the_cutoff_is_counted_not_absorbed():
    """Event-controlled, through a real thread — not an immediate raise."""
    import threading  # noqa: PLC0415

    from core.acceptance_compose_observer import (  # noqa: PLC0415
        late_model_bound_breakdown,
        seal_model_bound_observation,
    )

    released = threading.Event()
    started = threading.Event()

    def _worker() -> None:
        index = observe_model_bound_call(
            context_metadata=_metadata("collect_delivery_city", {"missing_field": "city"})
        )
        started.set()
        released.wait(5)
        record_model_bound_outcome(index, candidate_present=True, compose_source="late")

    async def _main():
        with _context(), model_bound_observation():
            with compose_stage(STAGE_ORDINARY, collection_field="city", turn_ref="t1"):
                task = asyncio.create_task(asyncio.to_thread(_worker))
                # Yield to the loop so the worker thread actually starts;
                # blocking here would keep it from ever running.
                for _ in range(200):
                    if started.is_set():
                        break
                    await asyncio.sleep(0.01)
                assert started.is_set(), "the worker must have reached the boundary"
            # The turn's acceptance cutoff closes here, while the call is
            # still outstanding — exactly the timeout case.
            seal_model_bound_observation()
            accepted = [c.to_dict() for c in recorded_model_bound_calls()]
            released.set()
            await task
            after = [c.to_dict() for c in recorded_model_bound_calls()]
            return accepted, after, dict(late_model_bound_breakdown())

    accepted, after, late = asyncio.run(_main())
    assert len(accepted) == 1
    assert accepted[0]["outcome_pending"] is True
    assert accepted[0]["outcome_recorded"] is False
    # The late completion changed nothing and was counted.
    assert after == accepted
    assert late["late_outcomes"] == 1


def test_a_pending_call_is_acceptable_evidence():
    """A genuine timeout is a truthful outcome, not missing evidence."""
    pending = _model_call(outcome_recorded=False, outcome_pending=True)
    assert CODE_MODEL_CALL_EVIDENCE_INCOMPLETE not in _blockers(
        _address_turn(model_bound_calls=[pending]),
    )
    silent = _model_call(outcome_recorded=False, outcome_pending=False)
    assert CODE_MODEL_CALL_EVIDENCE_INCOMPLETE in _blockers(
        _address_turn(model_bound_calls=[silent]),
    )


def test_a_subsequent_turn_starts_from_a_clean_cutoff():
    with _context():
        with model_bound_observation():
            with compose_stage(STAGE_ORDINARY, turn_ref="t1"):
                observe_model_bound_call(context_metadata=_metadata("a", {}))
            assert len(recorded_model_bound_calls()) == 1
        with model_bound_observation():
            assert recorded_model_bound_calls() == ()
            with compose_stage(STAGE_RECOVERY, turn_ref="t2"):
                observe_model_bound_call(context_metadata=_metadata("b", {}))
            calls = recorded_model_bound_calls()
            assert len(calls) == 1
            assert calls[0].turn_ref == "t2"


# ── C4: the runner drives the real chain ──────────────────────────────────
#
# Everything above drives pieces. This drives ``run_sandbox_of2_turn`` with
# a handler that executes the REAL OrderFlowV2 send block — the real owner
# result, the real composer, the real guards and recovery, the real
# serializer and the real ``_post_wa`` — substituting only the provider
# below the adapter, so the observation hook inside it actually runs.
#
# What this does NOT do is enter ``_handle_merchant_message`` itself; see
# the note in the harness runbook. The gap is stated rather than papered
# over.


def _reserve_tenant_one(db: Any) -> None:
    from models import Tenant  # noqa: PLC0415

    db.add(Tenant(name="platform-placeholder", is_active=False))
    db.flush()


def _address_suite():
    import importlib  # noqa: PLC0415

    tests_dir = str(REPO_ROOT / "tests")
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)
    return importlib.import_module("test_salla_customer_address_candidates")


def _real_chain_handler(*, addr, db, tenant, convo, result, captured_provider):
    """A handler that runs the real send block with the real transport."""
    from types import SimpleNamespace  # noqa: PLC0415

    import routers.whatsapp_webhook as webhook  # noqa: PLC0415

    from core.conversation_engine import StateManager  # noqa: PLC0415
    from modules.ai.brain.persona_ownership import (  # noqa: PLC0415
        PersonaBypassReason,
        PersonaOwnershipRecord,
    )

    namespace = addr._webhook_send_block()
    namespace.update(
        {
            "_trace": SimpleNamespace(outbound_lock_acquired=lambda: True),
            "persist_order_flow_v2_result": lambda *a, **k: None,
            "db": db,
            "tenant_id": tenant.id,
            "to": addr.CUSTOMER_PHONE,
            "phone_id": "internal-direct-code-probe",
            "wa_msg_id": "probe.city.0",
            "convo": convo,
            "text": "الرياض",
            # The REAL transport boundary, not a stand-in.
            "_post_wa": webhook._post_wa,
            "_persona_ownership": PersonaOwnershipRecord(),
            "_POReason": PersonaBypassReason,
            "StateManager": StateManager,
            "_sync_persona_observability": lambda: None,
            "_of2_result": result,
        }
    )

    async def _handler(*args: Any, **kwargs: Any) -> None:
        await namespace["_exercise"]()

    return _handler


def _fake_pipeline(reply_text: str = "وين نوصل طلبك؟"):
    """Substitute BELOW the adapter, so the observation hook still runs.

    Patching ``generate_ai_reply`` itself replaces the very function that
    records the call, which is why the first cut of this test saw zero
    model-bound calls through a chain that plainly made one.
    """
    from types import SimpleNamespace  # noqa: PLC0415

    def _run(request: Any):
        return SimpleNamespace(
            reply_text=reply_text, provider_used="fake-provider", metadata={},
        )

    return _run


def test_the_runner_drives_the_real_chain_and_captures_it():
    """Runner → real owner result → composer → guards → serializer → capture."""
    from unittest.mock import patch  # noqa: PLC0415

    import modules.ai.orchestrator.adapter as adapter  # noqa: PLC0415
    import routers.whatsapp_webhook as webhook  # noqa: PLC0415

    from services.internal_conversational_e2e_harness import (  # noqa: PLC0415
        run_sandbox_of2_turn,
    )

    addr = _address_suite()
    db, _engine = addr._make_db()
    # Tenant 1 is the platform tenant and is hard-denied by the identity
    # gate, so the probe tenant must not be the first row.
    _reserve_tenant_one(db)
    tenant, customer = addr._seed(db)
    addr._two_candidates(db, tenant, customer)
    convo = addr._conversation(db, tenant, customer)
    result = addr._address_turn(db, tenant, convo, field="city")

    provider_calls: list = []
    handler = _real_chain_handler(
        addr=addr, db=db, tenant=tenant, convo=convo, result=result,
        captured_provider=provider_calls,
    )

    async def _main():
        with patch.object(
            webhook, "provider_send_message", _recording_provider(provider_calls)
        ), patch.object(adapter._pipeline, "run", _fake_pipeline()):
            return await run_sandbox_of2_turn(
                db=db,
                request=_of2_request(
                    tenant_id=tenant.id,
                    allowed_tenants=frozenset({tenant.id}),
                    conversation=convo,
                    customer_phone=addr.CUSTOMER_PHONE,
                    turn_ref="probe.city.0",
                    expected_operational_result="address_reply",
                ),
                handler=handler,
            )

    outcome = asyncio.run(_main())
    evidence = outcome.evidence
    address_turn = evidence["address_turn"]

    assert provider_calls == [], "no provider call may be attempted"
    assert address_turn["transport"] == "captured"
    assert address_turn["captured_payload_digest_verified"] is True
    assert address_turn["outbound_message_row_verified"] is True
    assert address_turn["outbound_metadata_turn_ref"] == "probe.city.0"

    # The city turn's facts, read at the adapter boundary through the real
    # composer — the observation that was silently lost before.
    calls = address_turn["model_bound_calls"]
    assert len(calls) >= 1
    assert calls[0]["stage"] == STAGE_ORDINARY
    assert calls[0]["response_goal"] == "collect_delivery_city"
    assert calls[0]["missing_field"] == "city"
    assert calls[0]["delivery_address_status"] == "accepted"
    assert calls[0]["has_accepted_maps_reference"] is True
    assert calls[0]["observed_at"] == "orchestrator_adapter"

    # Receipt and recorded showing agree, tied to a delivery this turn
    # actually captured.
    assert address_turn["receipt_address_ids"] == address_turn["recorded_action_ids"]
    assert address_turn["recorded_offer_delivery_ref"] in address_turn["delivery_ids"]
    assert evidence["status"] == "evaluated"


def test_the_runner_drives_the_real_recovery_chain():
    """The guard fails for real; the recovery leg is observed as its own stage."""
    from unittest.mock import patch  # noqa: PLC0415

    import modules.ai.orchestrator.adapter as adapter  # noqa: PLC0415
    import routers.whatsapp_webhook as webhook  # noqa: PLC0415

    from services.internal_conversational_e2e_harness import (  # noqa: PLC0415
        run_sandbox_of2_turn,
    )

    addr = _address_suite()
    db, _engine = addr._make_db()
    # Tenant 1 is the platform tenant and is hard-denied by the identity
    # gate, so the probe tenant must not be the first row.
    _reserve_tenant_one(db)
    tenant, customer = addr._seed(db)
    addr._two_candidates(db, tenant, customer)
    convo = addr._conversation(db, tenant, customer)
    result = addr._address_turn(db, tenant, convo, field="city")

    provider_calls: list = []
    handler = _real_chain_handler(
        addr=addr, db=db, tenant=tenant, convo=convo, result=result,
        captured_provider=provider_calls,
    )

    async def _main():
        with patch.object(
            webhook, "provider_send_message", _recording_provider(provider_calls)
        ), patch.object(
            adapter._pipeline, "run", _fake_pipeline("تم حفظ عنوانك. نكمل الطلب؟")
        ), patch(
            "modules.ai.order_flow_v2.outbound_guards"
            ".apply_order_flow_v2_outbound_guards",
            side_effect=RuntimeError("isolated wrapper failure"),
        ):
            return await run_sandbox_of2_turn(
                db=db,
                request=_of2_request(
                    tenant_id=tenant.id,
                    allowed_tenants=frozenset({tenant.id}),
                    conversation=convo,
                    customer_phone=addr.CUSTOMER_PHONE,
                    turn_ref="probe.city.0",
                    failure_injection="none",
                    expected_operational_result="address_reply",
                ),
                handler=handler,
            )

    outcome = asyncio.run(_main())
    address_turn = outcome.evidence["address_turn"]

    assert provider_calls == []
    assert address_turn["execution_path"] == PATH_RECOVERY
    # Text-only: the refused turn drops the structured surface.
    assert address_turn["delivered_surface"] == SURFACE_TEXT
    stages = [c["stage"] for c in address_turn["model_bound_calls"]]
    assert STAGE_ORDINARY in stages and STAGE_RECOVERY in stages, stages
    # Every call on a city turn keeps the city goal, recovery included.
    for call in address_turn["model_bound_calls"]:
        assert call["response_goal"] == "collect_delivery_city", call
        assert call["missing_field"] == "city", call


# ── C4: the operator's scenario gate, actually exercised ──────────────────


def _load_scenarios(path: Any):
    import importlib.util as util  # noqa: PLC0415

    spec = util.spec_from_file_location(
        "of2_operator",
        REPO_ROOT / "scripts" / "operators" / "internal_conversational_e2e_session.py",
    )
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, module._load_scenarios(path)


def test_the_shipped_manifest_parses_through_the_operator():
    """The runbook's manifest is executable, not illustrative."""
    module, scenarios = _load_scenarios(
        REPO_ROOT / "docs" / "engineering" / "of2-address-scenarios.json"
    )
    assert len(scenarios) >= 7
    ids = {s["scenario_id"] for s in scenarios}
    assert {
        "ordinary_no_saved_choices",
        "ordinary_several_saved_choices",
        "city_with_accepted_address",
        "provider_failure_retains_choices",
        "provider_timeout_retains_choices",
        "guard_boundary_recovery_text_only",
        "continuation_after_captured_choice",
    } <= ids
    # Every OrderFlowV2 scenario names the customer state it assumes and
    # carries a STRUCTURAL inbound; free text can never reach this path.
    for scenario in scenarios:
        assert scenario["fixture_state"], scenario["scenario_id"]
        for turn in scenario["turns"]:
            if turn["mode"] != "of2":
                continue
            inbound = turn["inbound_metadata"]
            assert inbound, (scenario["scenario_id"], turn["text"])
            assert str(inbound.get("inbound_normalized_type") or "") == "interactive"
    for scenario in scenarios:
        for turn in scenario["turns"]:
            assert turn["mode"] == "of2"
            assert turn["expected_operational_result"]


def test_the_operator_refuses_an_of2_turn_that_declares_nothing(tmp_path):
    """Each omission is refused at load, before any turn runs."""
    import json  # noqa: PLC0415

    def _manifest(turn: Dict[str, Any]) -> Any:
        path = tmp_path / f"m{uuid.uuid4().hex[:8]}.json"
        path.write_text(
            json.dumps(
                {
                    "scenario_schema_version": "internal_conversational_e2e_scenarios_v2",
                    "scenarios": [
                        {
                            "scenario_id": "s1",
                            "fixture_state": "several_saved_addresses",
                            "turns": [turn],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return path

    base = {
        "text": "الرياض",
        "mode": "of2",
        "expected_status": "evaluated",
        "expects_address_turn": True,
        "expected_operational_result": "address_reply",
        "expectations": {
            "collection_field": "city",
            "response_goal": "collect_delivery_city",
            "missing_field": "city",
        },
    }
    module, scenarios = _load_scenarios(_manifest(dict(base)))
    assert scenarios[0]["turns"][0]["expectations"]["missing_field"] == "city"

    for drop, expected in (
        ("expects_address_turn", "expects_address_turn_required"),
        ("expected_operational_result", "expected_operational_result_invalid"),
        ("expectations", "expectations_required"),
    ):
        turn = {k: v for k, v in base.items() if k != drop}
        with pytest.raises(ValueError) as err:
            _load_scenarios(_manifest(turn))
        assert str(err.value) == expected, drop

    # A PARTIAL expectation is refused too. The validator requires every
    # field, so a loader accepting "any one of them" would bless a
    # manifest the run could never pass.
    for partial in (
        {"collection_field": "city"},
        {"collection_field": "city", "response_goal": "collect_delivery_city"},
        {"response_goal": "collect_delivery_city", "missing_field": "city"},
    ):
        turn = {**base, "expectations": partial}
        with pytest.raises(ValueError) as err:
            _load_scenarios(_manifest(turn))
        assert str(err.value) == "expectations_required", partial

    # An OrderFlowV2 scenario that does not say which customer state it
    # assumes cannot be prepared, and an unprepared one proves nothing.
    path = tmp_path / "nofixture.json"
    path.write_text(
        json.dumps(
            {
                "scenario_schema_version": "internal_conversational_e2e_scenarios_v2",
                "scenarios": [{"scenario_id": "s1", "turns": [dict(base)]}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as err:
        _load_scenarios(path)
    assert str(err.value) == "fixture_state_required"


def test_the_operator_refuses_an_unresolvable_captured_action():
    """An unresolved placeholder answers no showing, so it must not run."""
    module, _ = _load_scenarios(
        REPO_ROOT / "docs" / "engineering" / "of2-address-scenarios.json"
    )
    turn = {
        "inbound_metadata": {"button_id": "__captured_action_id__"},
        "captured_action_index": 0,
    }
    with pytest.raises(ValueError) as err:
        module._continuation_metadata(turn, [])
    assert str(err.value) == "captured_action_reference_unresolved"

    with pytest.raises(ValueError) as err:
        module._continuation_metadata(
            {**turn, "captured_action_index": 5}, ["nahla_addr_select:o:1"],
        )
    assert str(err.value) == "captured_action_reference_out_of_range"

    resolved = module._continuation_metadata(turn, ["nahla_addr_select:o:1"])
    assert resolved == {"button_id": "nahla_addr_select:o:1"}


# ── C4: the real entrypoint, prepared ─────────────────────────────────────
#
# Everything below drives ``_handle_merchant_message`` itself — the actual
# webhook entrypoint — through the real OrderFlowV2 owner, the real
# compose, the real guards, the real outbound serializer and the real
# ``_post_wa``, with capture standing in for the provider. Nothing in the
# owner is patched and no owner result is prepared by hand.


@pytest.fixture()
def order_flow_v2_live(monkeypatch):
    """OrderFlowV2 live for this process.

    Not a bypass: the gate is read exactly as the runtime reads it, and
    without this the preflight correctly reports ``shadow_only`` — a
    shadow evaluation observes and never sends, so no address reply can
    be delivered and no capture can occur. It is an environment
    prerequisite of the run, recorded in the runbook.
    """
    import core.config as config  # noqa: PLC0415

    # The flag is resolved into a module constant at import time, so the
    # environment variable alone would not reach the gate in an
    # already-imported process.
    monkeypatch.setenv("ORDER_FLOW_V2_ENABLED", "true")
    monkeypatch.setattr(config, "ORDER_FLOW_V2_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "ORDER_FLOW_V2_ENFORCE_TENANTS", "", raising=False)
    monkeypatch.setattr(config, "ORDER_FLOW_V2_DISABLED_TENANTS", "", raising=False)
    yield


def _sandbox_phone() -> str:
    """A recipient of this test's own.

    The outbound burst throttle is keyed on (tenant, recipient) and lives
    in the process, so tests sharing a number share its budget: a suite
    that ran a few address turns earlier silently throttled the ones
    after it. The throttle is a real production gate and is not
    disabled — each test simply talks to a different customer.
    """
    return f"96650{uuid.uuid4().int % 10_000_000:07d}"


def _live_sandbox(phone: str = ""):
    phone = phone or _sandbox_phone()
    """A sandbox tenant that can genuinely reach the address branch.

    Every precondition here is a REAL one the runtime checks: a trial
    window that grants billing access, a connected channel, commerce
    permissions that authorise a durable write, and a catalog product.
    None of them is a bypass — the gates stay in force and are satisfied.
    """
    from datetime import datetime, timedelta, timezone  # noqa: PLC0415

    from sqlalchemy import JSON, create_engine  # noqa: PLC0415
    from sqlalchemy.dialects.postgresql import JSONB  # noqa: PLC0415
    from sqlalchemy.orm import sessionmaker  # noqa: PLC0415

    from models import (  # noqa: PLC0415
        Base,
        CommercePermissions,
        Product,
        Tenant,
        WhatsAppConnection,
    )

    engine = create_engine("sqlite:///:memory:")
    saved = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, orig in saved:
        col.type = orig
    db = sessionmaker(bind=engine)()

    now = datetime.now(timezone.utc)
    db.add(Tenant(name="platform-placeholder", is_active=False))
    db.flush()
    tenant = Tenant(
        name="متجر تجريبي عام",
        is_active=True,
        trial_started_at=now - timedelta(days=1),
        trial_ends_at=now + timedelta(days=13),
    )
    db.add(tenant)
    db.flush()
    db.add(
        WhatsAppConnection(
            tenant_id=tenant.id,
            status="connected",
            phone_number_id="SANDBOX_PHONE_ID",
            phone_number="966510000000",
            provider="meta",
            sending_enabled=True,
            webhook_verified=True,
            connected_at=now,
            access_token="sandbox-token",
        )
    )
    db.add(
        CommercePermissions(
            tenant_id=tenant.id,
            can_create_orders=True,
            can_create_checkout_links=True,
            can_send_payment_links=True,
            can_apply_coupons=True,
            can_auto_generate_coupons=False,
            can_cancel_orders=False,
        )
    )
    db.add(
        Product(
            tenant_id=tenant.id,
            external_id="SANDBOX-SKU-1",
            sku="SANDBOX-SKU-1",
            title="حذاء رياضي أبيض",
            price="149.00",
            in_stock=True,
            stock_quantity=5,
            source="manual",
        )
    )
    db.commit()
    return db, tenant, phone


def _interactive(button_id: str) -> Dict[str, Any]:
    return {
        "source_type": "interactive",
        "inbound_normalized_type": "interactive",
        "type": "interactive",
        "button_id": button_id,
    }


def _entrypoint_request(*, tenant, convo, phone, text, meta, label, **over):
    from services.internal_conversational_e2e_harness import (  # noqa: PLC0415
        SandboxOf2TurnRequest,
    )

    base = dict(
        session_id=str(uuid.uuid4()),
        scenario_id=label,
        turn_index=0,
        tenant_id=tenant.id,
        customer_phone=phone,
        phone_id="SANDBOX_PHONE_ID",
        text=text,
        conversation=convo,
        allowed_tenants=frozenset({tenant.id}),
        evidence_hmac_key="k" * 32,
        runtime_revision="probe",
        database_identity_fingerprint="sha256:" + "0" * 64,
        network_attestation_id="att-1",
        llm_allowed_hosts=("api.anthropic.com",),
        turn_ref=f"probe.{label}.0",
        allow_llm_inference=True,
        expects_address_turn=True,
        # The three fields the validator requires. The accepted-address
        # extras are scenario-specific and are asserted where the fixture
        # actually establishes an accepted address.
        expectations={
            "collection_field": "city",
            "response_goal": "collect_delivery_city",
            "missing_field": "city",
        },
        expected_operational_result="address_reply",
        inbound_metadata=meta,
    )
    base.update(over)
    return SandboxOf2TurnRequest(**base)


def test_the_real_entrypoint_produces_a_prepared_address_turn(order_flow_v2_live):
    """``_handle_merchant_message`` itself, with nothing in the owner patched.

    This is the claim the whole harness rests on, and it was the one thing
    no test made: that the real entrypoint, given a real fixture, reaches
    the address branch and delivers a reply the evidence can bind.
    """
    from services.internal_conversational_e2e_harness import (  # noqa: PLC0415
        run_sandbox_of2_turn,
    )
    from services.internal_conversational_e2e_of2_fixtures import (  # noqa: PLC0415
        STATE_NO_SAVED,
        of2_fixture_preflight,
        prepare_of2_scenario_fixture,
    )

    db, tenant, phone = _live_sandbox()
    fixture = prepare_of2_scenario_fixture(
        db,
        tenant_id=tenant.id,
        customer_phone=phone,
        scenario_id="entry",
        session_id=str(uuid.uuid4()),
        state=STATE_NO_SAVED,
    )
    meta = _interactive("of2_continue_checkout")
    assert (
        of2_fixture_preflight(
            db,
            tenant_id=tenant.id,
            customer_phone=phone,
            conversation=fixture.conversation,
            message="أكمل الطلب",
            inbound_metadata=meta,
            inbound_normalized_type="interactive",
        )
        == []
    )

    outcome = asyncio.run(
        run_sandbox_of2_turn(
            db=db,
            request=_entrypoint_request(
                tenant=tenant,
                convo=fixture.conversation,
                phone=phone,
                text="أكمل الطلب",
                meta=meta,
                label="entry",
            ),
        )
    )
    evidence = outcome.evidence
    record = evidence["address_turn"]
    assert evidence["blockers"] == [], evidence["blockers"]
    assert evidence["verdict"] == "pass"
    assert record["transport"] == "captured"
    assert record["execution_path"] == PATH_ORDINARY
    assert record["collection_field"] == "city"
    assert record["outbound_message_row_verified"] is True
    # The model was actually called, and given the address context.
    address_calls = [c for c in record["model_bound_calls"] if c["address_bound"]]
    assert address_calls, record["model_bound_calls"]
    assert all(c["response_goal"] == "collect_delivery_city" for c in address_calls)
    # And the final customer text is the model's, not a template's.
    assert record["outbound_provenance"]["final_customer_text_source"] == "llm"


def test_saved_choices_reach_the_customer_through_the_real_entrypoint(order_flow_v2_live):
    """Several saved addresses, so the same turn carries tappable choices."""
    from services.internal_conversational_e2e_harness import (  # noqa: PLC0415
        run_sandbox_of2_turn,
    )
    from services.internal_conversational_e2e_of2_fixtures import (  # noqa: PLC0415
        STATE_SEVERAL_SAVED,
        prepare_of2_scenario_fixture,
    )

    db, tenant, phone = _live_sandbox()
    fixture = prepare_of2_scenario_fixture(
        db,
        tenant_id=tenant.id,
        customer_phone=phone,
        scenario_id="choices",
        session_id=str(uuid.uuid4()),
        state=STATE_SEVERAL_SAVED,
    )
    assert len(fixture.address_ids) == 3

    outcome = asyncio.run(
        run_sandbox_of2_turn(
            db=db,
            request=_entrypoint_request(
                tenant=tenant,
                convo=fixture.conversation,
                phone=phone,
                text="متابعة الشراء",
                meta=_interactive("of2_resume_checkout"),
                label="choices",
            ),
        )
    )
    record = outcome.evidence["address_turn"]
    assert outcome.evidence["blockers"] == [], outcome.evidence["blockers"]
    assert record["delivered_surface"] in (SURFACE_BUTTONS, SURFACE_LIST)
    # The ids that LEFT resolve to the addresses the platform recorded as
    # shown, from one showing.
    assert record["receipt_action_ids"]
    assert record["receipt_address_ids"] == record["recorded_action_ids"]
    assert record["recorded_offer_delivery_ref"] in record["delivery_ids"]


def test_the_preflight_refuses_free_text_before_running_it(order_flow_v2_live):
    """Free text can never reach this path, and the run must say so.

    Not a fixture gap: OrderFlowV2 owns a turn pre-Brain only for a
    structurally explicit inbound. A scenario written in prose would run,
    return quietly, and prove nothing.
    """
    from services.internal_conversational_e2e_of2_fixtures import (  # noqa: PLC0415
        LAYER_INBOUND,
        STATE_NO_SAVED,
        of2_fixture_preflight,
        prepare_of2_scenario_fixture,
    )

    db, tenant, phone = _live_sandbox()
    fixture = prepare_of2_scenario_fixture(
        db,
        tenant_id=tenant.id,
        customer_phone=phone,
        scenario_id="prose",
        session_id=str(uuid.uuid4()),
        state=STATE_NO_SAVED,
    )
    divergences = of2_fixture_preflight(
        db,
        tenant_id=tenant.id,
        customer_phone=phone,
        conversation=fixture.conversation,
        message="ابغى اكمل الطلب",
        inbound_metadata={"type": "text"},
        inbound_normalized_type="text",
    )
    assert [d.layer for d in divergences] == [LAYER_INBOUND]
    assert divergences[0].reason == "unstructured_requires_brain_semantic_ownership"


def test_the_preflight_stops_at_the_first_failing_layer(order_flow_v2_live):
    """A lower layer's opinion is meaningless while an upper one is broken."""
    from services.internal_conversational_e2e_of2_fixtures import (  # noqa: PLC0415
        LAYER_BILLING,
        LAYER_CHANNEL,
        LAYER_CHECKOUT,
        STATE_NO_SAVED,
        of2_fixture_preflight,
        prepare_of2_scenario_fixture,
    )
    from models import WhatsAppConnection  # noqa: PLC0415

    db, tenant, phone = _live_sandbox()
    fixture = prepare_of2_scenario_fixture(
        db,
        tenant_id=tenant.id,
        customer_phone=phone,
        scenario_id="layers",
        session_id=str(uuid.uuid4()),
        state=STATE_NO_SAVED,
    )
    meta = _interactive("of2_continue_checkout")

    def _probe():
        return of2_fixture_preflight(
            db,
            tenant_id=tenant.id,
            customer_phone=phone,
            conversation=fixture.conversation,
            message="أكمل الطلب",
            inbound_metadata=meta,
            inbound_normalized_type="interactive",
        )

    assert _probe() == []

    # An empty checkout is diagnosed at the checkout layer.
    meta_before = dict(fixture.conversation.extra_metadata or {})
    fixture.conversation.extra_metadata = {
        **meta_before,
        "brain_state": {"order_prep": {}},
    }
    db.commit()
    assert [d.layer for d in _probe()] == [LAYER_CHECKOUT]
    fixture.conversation.extra_metadata = meta_before
    db.commit()

    # A disconnected channel is diagnosed ABOVE the checkout, and the
    # checkout's own state is not consulted at all.
    db.query(WhatsAppConnection).delete()
    db.commit()
    assert [d.layer for d in _probe()] == [LAYER_CHANNEL]

    # Billing outranks the channel.
    tenant.trial_started_at = None
    tenant.trial_ends_at = None
    db.commit()
    assert [d.layer for d in _probe()] == [LAYER_BILLING]


# ── C1: selection is consumption, refusal has an author ───────────────────


def _selection(**over):
    """A complete, genuinely consumed selection.

    Every link the producer now establishes: the showing existed, the
    writer published an operation for THIS turn naming this address at
    the shown revision, and the durable row carries that operation's own
    reference.
    """
    base = {
        "consumed_action_id": "nahla_addr_select:offer:7",
        "action_offer_id": "offer",
        "action_address_id": "7",
        "showing_exists": True,
        "offer_identity": "offer",
        "shown_fingerprint": "fp-7",
        "operation": "adopt_selection",
        "operation_ref": "offer:",
        "operation_address_id": "7",
        "operation_fingerprint": "fp-7",
        "operation_recorded": True,
        "operation_observed_in_turn": True,
        "selected_address_id": "7",
        "selection_state": "selected",
        "selected_fingerprint": "fp-7",
        "selection_source": "customer_confirmed",
        "selection_operation_ref": "offer:",
        "selection_matches_action": True,
        "selection_scope_verified": True,
        "selection_operation_ref_before": "",
        "selection_state_before": "candidate",
        "action_already_consumed": False,
    }
    base.update(over)
    return base


def _result_blockers(**kw):
    from services.internal_conversational_e2e_harness import (  # noqa: PLC0415
        _operational_result_blockers,
    )

    base = dict(
        expected="structured_selection",
        status="evaluated",
        captured=False,
        address_turn={},
        state_delta={"conversation_metadata_fingerprint": {"before": "a", "after": "b"}},
        selection=_selection(),
        refusal={"gate": "", "reason": "", "observed": False},
    )
    base.update(kw)
    return _operational_result_blockers(**base)


def test_a_genuine_selection_passes():
    assert _result_blockers() == []


def test_an_unrelated_metadata_change_is_not_a_selection():
    """The reviewer's PASS, reproduced and then refused.

    A handler that consumed nothing, selected nothing and merely touched
    an unrelated counter moved the conversation fingerprint — and that
    was accepted as a structured selection.
    """
    blockers = _result_blockers(
        state_delta={"diagnostic_unrelated_counter": {"before": 0, "after": 1}},
        selection=_selection(
            consumed_action_id="",
            showing_exists=False,
            shown_fingerprint="",
            operation_recorded=False,
            operation_observed_in_turn=False,
            selection_state="",
            selected_fingerprint="",
            selection_matches_action=False,
            selection_scope_verified=False,
        ),
    )
    assert "structured_selection_action_not_consumed" in blockers
    assert "structured_selection_not_durably_recorded" in blockers
    assert "structured_selection_address_mismatch" in blockers


def test_an_unconsumed_action_is_refused():
    """The action was supplied but the runtime never resolved it."""
    blockers = _result_blockers(
        selection=_selection(consumed_action_id="", selection_matches_action=False)
    )
    assert "structured_selection_action_not_consumed" in blockers


def test_selecting_a_different_address_than_the_action_named_is_refused():
    blockers = _result_blockers(
        selection=_selection(
            # A different address was recorded as selected.
            selected_address_id="9",
            selected_fingerprint="fp-9",
            selection_matches_action=False,
        )
    )
    assert "structured_selection_address_mismatch" in blockers
    assert "structured_selection_revision_mismatch" in blockers


def test_a_selection_outside_this_customers_scope_is_refused():
    blockers = _result_blockers(
        selection=_selection(
            selection_matches_action=False, selection_scope_verified=False
        )
    )
    assert "structured_selection_scope_unverified" in blockers


def test_a_candidate_that_was_never_selected_is_refused():
    blockers = _result_blockers(
        selection=_selection(
            selection_state="candidate",
            selected_fingerprint="",
            selection_source="",
            selection_matches_action=False,
        )
    )
    assert "structured_selection_not_durably_recorded" in blockers


def test_a_no_op_is_not_a_refusal():
    """Absence of transport proved nothing: a quiet turn looked identical."""
    blockers = _result_blockers(expected="refusal", captured=False)
    assert blockers == ["refusal_gate_unestablished"]


def test_a_refusal_must_name_its_reason():
    blockers = _result_blockers(
        expected="refusal",
        captured=False,
        refusal={"gate": "egress:whatsapp_provider", "reason": "", "observed": True},
    )
    assert blockers == ["refusal_reason_unestablished"]


def test_a_gated_refusal_passes():
    assert (
        _result_blockers(
            expected="refusal",
            captured=False,
            refusal={
                "gate": "egress:whatsapp_provider",
                "reason": "internal_e2e_egress_denied",
                "observed": True,
            },
        )
        == []
    )


def test_a_refusal_that_sent_anyway_is_refused():
    blockers = _result_blockers(
        expected="refusal",
        captured=True,
        refusal={"gate": "g", "reason": "r", "observed": True},
    )
    assert blockers == ["expected_refusal_not_observed"]


def test_refusal_evidence_names_the_gate_it_observed():
    from services.internal_conversational_e2e_harness import (  # noqa: PLC0415
        _refusal_evidence,
    )

    denied = _refusal_evidence(
        denial_audits=[
            {"egress_kind": "whatsapp_provider", "reason": "internal_e2e_egress_denied"}
        ],
        blockers=[],
        status="evaluated",
        outbound_meta={},
    )
    assert denied == {
        "gate": "egress:whatsapp_provider",
        "reason": "internal_e2e_egress_denied",
        "observed": True,
    }

    suppressed = _refusal_evidence(
        denial_audits=[],
        blockers=[],
        status="evaluated",
        outbound_meta={"address_save_claim_suppress_reason": "unverifiable_save_claim"},
    )
    assert suppressed["gate"] == "provenance:address_save_claim_suppress_reason"
    assert suppressed["reason"] == "unverifiable_save_claim"

    raised = _refusal_evidence(
        denial_audits=[],
        blockers=["handler_exception"],
        status="handler_exception",
        outbound_meta={},
    )
    assert raised["gate"] == "handler_exception"

    quiet = _refusal_evidence(
        denial_audits=[], blockers=[], status="evaluated", outbound_meta={},
    )
    assert quiet["observed"] is False


# ── C2: content binding and timing correlation ────────────────────────────


def test_a_corrupted_persisted_body_is_refused_though_the_turn_ref_is_intact():
    """The producer-level corruption the reviewer demonstrated.

    Replacing the persisted body with unrelated text while leaving
    ``address_turn_ref`` untouched used to leave BOTH verification flags
    true, because the digest was only ever compared with itself.
    """
    record = _address_turn(
        persisted_body_digest="sha256:" + "d" * 64,
        captured_persisted_content_match=False,
        content_binding_reason="captured_persisted_content_differs",
    )
    assert CODE_ADDRESS_CONTENT_UNBOUND in _blockers(record)


def test_a_content_match_flag_alone_does_not_bind():
    """A true flag with digests that differ is still unbound."""
    record = _address_turn(
        persisted_body_digest="sha256:" + "e" * 64,
        captured_persisted_content_match=True,
    )
    assert CODE_ADDRESS_CONTENT_UNBOUND in _blockers(record)


def test_an_absent_persisted_body_is_refused():
    record = _address_turn(
        persisted_body_digest="",
        captured_persisted_content_match=False,
        content_binding_reason="persisted_row_absent",
    )
    assert CODE_ADDRESS_CONTENT_UNBOUND in _blockers(record)


def test_an_unsupported_representation_is_refused():
    """A transformation nobody declared is a finding, not a difference."""
    record = _address_turn(captured_persisted_representation="unsupported")
    assert CODE_ADDRESS_CONTENT_UNBOUND in _blockers(record)


def test_foreign_timing_is_refused_though_it_carries_a_turn_id():
    """The measurement must be THIS turn's, not merely some measurement."""
    record = _address_turn(
        turn_timing={
            "turn_id": "someone-elses-turn",
            "message_id": "probe.city.0",
            "conversation_id": 7,
            "tenant_id": 3,
            "total_turn_ms": 12,
            "llm_call_count": 1,
        }
    )
    assert CODE_ADDRESS_TIMING_UNCORRELATED in _blockers(record)


def test_timing_naming_a_foreign_message_is_refused():
    record = _address_turn(
        turn_timing={
            "turn_id": "turn-1",
            "message_id": "foreign-message-id",
            "conversation_id": 7,
            "tenant_id": 3,
            "total_turn_ms": 12,
            "llm_call_count": 1,
        }
    )
    assert CODE_ADDRESS_TIMING_UNCORRELATED in _blockers(record)


def test_timing_from_another_conversation_or_tenant_is_refused():
    for field, value in (("conversation_id", 99), ("tenant_id", 99)):
        record = _address_turn(
            turn_timing={
                "turn_id": "turn-1",
                "message_id": "probe.city.0",
                "conversation_id": 7,
                "tenant_id": 3,
                "total_turn_ms": 12,
                "llm_call_count": 1,
                field: value,
            }
        )
        assert CODE_ADDRESS_TIMING_UNCORRELATED in _blockers(record), field


def test_content_binding_compares_two_sources_not_one():
    """Only whitespace is normalised; no wording is preferred."""
    from services.internal_conversational_e2e_harness import (  # noqa: PLC0415
        _captured_customer_text,
        _content_binding,
    )

    text, representation = _captured_customer_text(
        {"text": {"body": "  وش  المدينة؟ "}}
    )
    assert representation == "text_body"
    bound = _content_binding(
        captured_text=text,
        persisted_body="وش المدينة؟",
        representation=representation,
        captured=True,
        row_found=True,
    )
    assert bound["content_match"] is True

    differs = _content_binding(
        captured_text=text,
        persisted_body="نص غير ذي صلة",
        representation=representation,
        captured=True,
        row_found=True,
    )
    assert differs["content_match"] is False
    assert differs["reason"] == "captured_persisted_content_differs"

    interactive, rep2 = _captured_customer_text(
        {"interactive": {"body": {"text": "اختر عنوانك"}}}
    )
    assert rep2 == "interactive_body"
    assert interactive == "اختر عنوانك"

    _, rep3 = _captured_customer_text({"template": {"name": "x"}})
    assert rep3 == "unsupported"


# ── C4: the operator's own run_session, end to end ────────────────────────


def _live_engine_sandbox(tmp_path):
    """A sandbox on a file engine, so the operator can open its own session."""
    from datetime import datetime, timedelta, timezone  # noqa: PLC0415

    from sqlalchemy import JSON, create_engine  # noqa: PLC0415
    from sqlalchemy.dialects.postgresql import JSONB  # noqa: PLC0415
    from sqlalchemy.orm import sessionmaker  # noqa: PLC0415

    from models import (  # noqa: PLC0415
        Base,
        CommercePermissions,
        Product,
        Tenant,
        WhatsAppConnection,
    )

    url = f"sqlite:///{tmp_path / 'sandbox.db'}"
    engine = create_engine(url)
    saved = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, orig in saved:
        col.type = orig

    db = sessionmaker(bind=engine)()
    now = datetime.now(timezone.utc)
    db.add(Tenant(name="platform-placeholder", is_active=False))
    db.flush()
    tenant = Tenant(
        name="متجر تجريبي عام",
        is_active=True,
        trial_started_at=now - timedelta(days=1),
        trial_ends_at=now + timedelta(days=13),
    )
    db.add(tenant)
    db.flush()
    db.add(
        WhatsAppConnection(
            tenant_id=tenant.id,
            status="connected",
            phone_number_id="SANDBOX_PHONE_ID",
            phone_number="966510000000",
            provider="meta",
            sending_enabled=True,
            webhook_verified=True,
            connected_at=now,
            access_token="sandbox-token",
        )
    )
    db.add(
        CommercePermissions(
            tenant_id=tenant.id,
            can_create_orders=True,
            can_create_checkout_links=True,
            can_send_payment_links=True,
            can_apply_coupons=True,
            can_auto_generate_coupons=False,
            can_cancel_orders=False,
        )
    )
    db.add(
        Product(
            tenant_id=tenant.id,
            external_id="SANDBOX-SKU-1",
            sku="SANDBOX-SKU-1",
            title="حذاء رياضي أبيض",
            price="149.00",
            in_stock=True,
            stock_quantity=5,
            source="manual",
        )
    )
    db.commit()
    tenant_id = tenant.id
    db.close()
    return engine, tenant_id, url


def _session_env(*, url, tenant_id, tmp_path):
    return {
        "NAHLA_INTERNAL_E2E_ENABLED": "true",
        "NAHLA_INTERNAL_E2E_CONFIRM": "true",
        "NAHLA_INTERNAL_E2E_DATABASE_URL": url,
        "NAHLA_INTERNAL_E2E_TENANT_ALLOWLIST": str(tenant_id),
        "NAHLA_INTERNAL_E2E_TEST_PHONE": _sandbox_phone(),
        "NAHLA_INTERNAL_E2E_EVIDENCE_HMAC_KEY": "e" * 32,
        "NAHLA_INTERNAL_E2E_ATTESTATION_HMAC_KEY": "a" * 32,
        "NAHLA_INTERNAL_E2E_LLM_ENABLED": "true",
        "NAHLA_INTERNAL_E2E_SESSION_DIR": str(tmp_path / "sessions"),
    }


def test_run_session_refuses_a_default_off_environment(tmp_path):
    """The operator's own entry point, with nothing enabled."""
    module, _ = _load_scenarios(
        REPO_ROOT / "docs" / "engineering" / "of2-address-scenarios.json"
    )
    result = asyncio.run(
        module.run_session(
            tenant_id=2,
            scenario_path=REPO_ROOT
            / "docs"
            / "engineering"
            / "of2-address-scenarios.json",
            env={},
        )
    )
    assert result["ok"] is False
    assert "internal_e2e_not_enabled" in result["blockers"]
    assert "internal_e2e_execution_not_confirmed" in result["blockers"]


def test_run_session_prepares_each_scenario_and_drives_the_real_entrypoint(
    tmp_path, monkeypatch, order_flow_v2_live
):
    """``run_session`` itself: manifest → fixtures → runner → entrypoint.

    Only two things are substituted, and neither is on the path under
    test: the sandbox ATTESTATION preflight, which asserts facts about an
    operator's environment rather than about the address path, and the
    model provider, which must not be called from an offline test. The
    fixtures, the owner, the compose, the guards, the serializer, the
    capture and the evidence are all real.
    """
    module, scenarios = _load_scenarios(
        REPO_ROOT / "docs" / "engineering" / "of2-address-scenarios.json"
    )
    engine, tenant_id, url = _live_engine_sandbox(tmp_path)
    env = _session_env(url=url, tenant_id=tenant_id, tmp_path=tmp_path)

    monkeypatch.setattr(
        module,
        "execute_preflight",
        lambda **kw: {
            "ok": True,
            "runtime_revision": "sandbox",
            "database_identity_fingerprint": "sha256:" + "0" * 64,
            "attestation_id": "att-sandbox",
            "llm_allowed_hosts": ["api.anthropic.com"],
        },
    )

    result = asyncio.run(
        module.run_session(
            tenant_id=tenant_id,
            scenario_path=REPO_ROOT
            / "docs"
            / "engineering"
            / "of2-address-scenarios.json",
            env=env,
            engine=engine,
        )
    )

    session = json.loads(Path(result["session_path"]).read_text(encoding="utf-8"))

    # Every scenario was PREPARED, and each one says which customer state
    # it was prepared into.
    prepared = {r["scenario_id"]: r for r in session["scenario_fixtures"]}
    assert {s["scenario_id"] for s in scenarios} == set(prepared)
    for scenario in scenarios:
        report = prepared[scenario["scenario_id"]]
        assert report["fixture"]["state"] == scenario["fixture_state"]
        # No scenario silently skipped its owner.
        assert report["divergences"] == [], (
            scenario["scenario_id"],
            report["divergences"],
        )

    # And the turns actually ran through the real entrypoint: every
    # address turn captured a reply and observed a real model-bound call.
    address_turns = [
        r
        for r in session["turn_results"]
        if (r.get("address_turn") or {}).get("transport") == "captured"
    ]
    # Every OrderFlowV2 turn in the manifest reached the send boundary.
    # Nothing was lost to the burst throttle, because the run paced
    # itself around it.
    assert session["outbound_pacing"]["pauses"] >= 1
    assert len(address_turns) == 8, [
        (r["scenario_id"], r["status"], (r.get("address_turn") or {}).get("transport"))
        for r in session["turn_results"]
    ]
    for turn in address_turns:
        record = turn["address_turn"]
        assert record["outbound_message_row_verified"] is True
        assert record["captured_persisted_content_match"] is True
        assert [c for c in record["model_bound_calls"] if c["address_bound"]]

    # The session is signed, and the signature covers what it reports.
    # Not one turn failed, at any layer.
    assert result["blockers"] == [], result["blockers"]
    assert result["verdict"] == "pass"

    by_scenario = {
        (r["scenario_id"], r["turn_index"]): r for r in session["turn_results"]
    }

    # Both failure paths ran, and they are DISTINCT. The ordinary
    # provider-failure path keeps the saved-address choices; the outer
    # guard's recovery is a text fallback that does not.
    failure = by_scenario[("provider_failure_retains_choices", 0)]["address_turn"]
    assert failure["execution_path"] == PATH_ORDINARY
    assert failure["delivered_surface"] in (SURFACE_BUTTONS, SURFACE_LIST)
    assert failure["injection_state"]["fired"] >= 1
    assert all(
        c["fallback_reason"] == "provider_call_raised"
        for c in failure["model_bound_calls"]
    )

    recovery = by_scenario[("guard_boundary_recovery_text_only", 0)]["address_turn"]
    assert recovery["execution_path"] == PATH_RECOVERY
    assert recovery["delivered_surface"] == SURFACE_TEXT
    assert recovery["injection_state"]["fired"] >= 1

    # The continuation CONSUMED the action it replayed: the address that
    # id names is durably recorded as selected, in this customer's scope.
    chosen = by_scenario[("continuation_after_captured_choice", 1)][
        "selection_evidence"
    ]
    assert chosen["consumed_action_id"].startswith("nahla_addr_select:")
    assert chosen["selection_state"] == "selected"
    assert chosen["selection_scope_verified"] is True
    assert chosen["selection_matches_action"] is True
    assert chosen["selected_address_id"] == chosen["action_address_id"]
    # And the id it replayed came from the PREVIOUS turn's captured
    # payload, not from the manifest.
    offered = by_scenario[("continuation_after_captured_choice", 0)]["address_turn"]
    assert chosen["consumed_action_id"] in offered["receipt_action_ids"]

    from services.internal_conversational_e2e_contract import (  # noqa: PLC0415
        verify_session_evidence,
    )

    assert verify_session_evidence(session, key=env["NAHLA_INTERNAL_E2E_EVIDENCE_HMAC_KEY"])


def test_run_session_reports_a_fixture_divergence_instead_of_a_quiet_pass(
    tmp_path, monkeypatch, order_flow_v2_live
):
    """A scenario whose preconditions do not hold must SAY which layer.

    Without this it runs, returns, captures nothing, and contributes a
    turn that looks merely uneventful.
    """
    module, _ = _load_scenarios(
        REPO_ROOT / "docs" / "engineering" / "of2-address-scenarios.json"
    )
    engine, tenant_id, url = _live_engine_sandbox(tmp_path)
    env = _session_env(url=url, tenant_id=tenant_id, tmp_path=tmp_path)
    monkeypatch.setattr(
        module,
        "execute_preflight",
        lambda **kw: {
            "ok": True,
            "runtime_revision": "sandbox",
            "database_identity_fingerprint": "sha256:" + "0" * 64,
            "attestation_id": "att-sandbox",
            "llm_allowed_hosts": ["api.anthropic.com"],
        },
    )
    # Take the channel away: a real precondition, really absent.
    from models import WhatsAppConnection  # noqa: PLC0415
    from sqlalchemy.orm import sessionmaker  # noqa: PLC0415

    admin = sessionmaker(bind=engine)()
    admin.query(WhatsAppConnection).delete()
    admin.commit()
    admin.close()

    result = asyncio.run(
        module.run_session(
            tenant_id=tenant_id,
            scenario_path=REPO_ROOT
            / "docs"
            / "engineering"
            / "of2-address-scenarios.json",
            env=env,
            engine=engine,
        )
    )
    assert result["ok"] is False
    assert "scenario_fixture_divergence" in result["blockers"]
    session = json.loads(Path(result["session_path"]).read_text(encoding="utf-8"))
    layers = {
        d["layer"]
        for report in session["scenario_fixtures"]
        for d in report["divergences"]
    }
    assert layers == {"channel"}


# ── C1: the PRODUCER must refuse, not just the validator ──────────────────
#
# The negative tests above hand the validator their own flags, so they
# cannot detect a producer that sets those flags optimistically — which is
# exactly the defect the last review found. Everything below drives the
# REAL producer, ``_selection_evidence``, through ``run_sandbox_of2_turn``
# against real persisted state, and reads the verdict it generates.


def _selection_probe(
    *,
    db,
    tenant,
    phone,
    fixture,
    button_id,
    handler,
    label,
):
    """One structured-selection turn, judged by the real producer."""
    import hashlib  # noqa: PLC0415

    from services.internal_conversational_e2e_harness import (  # noqa: PLC0415
        run_sandbox_of2_turn,
    )

    def _probe(dbx, tenant_id, convo):
        dbx.expire_all()
        dbx.refresh(convo)
        metadata = dict(getattr(convo, "extra_metadata", None) or {})
        return {
            "conversation_metadata_fingerprint": "sha256:"
            + hashlib.sha256(
                json.dumps(metadata, sort_keys=True, default=str).encode()
            ).hexdigest()
        }

    return asyncio.run(
        run_sandbox_of2_turn(
            db=db,
            request=_entrypoint_request(
                tenant=tenant,
                convo=fixture.conversation,
                phone=phone,
                text="اختيار العنوان",
                meta=_interactive(button_id),
                label=label,
                expects_address_turn=False,
                expected_operational_result="structured_selection",
                expected_state_delta_keys=("conversation_metadata_fingerprint",),
                state_probe=_probe,
            ),
            handler=handler,
        )
    ).evidence


def _counter_bump_handler(db, fixture):
    """Runs no owner, consumes no showing, selects nothing, sends nothing."""

    async def _handler(*args, **kwargs):
        convo = fixture.conversation
        metadata = dict(convo.extra_metadata or {})
        metadata["diagnostic_unrelated_counter"] = (
            int(metadata.get("diagnostic_unrelated_counter") or 0) + 1
        )
        convo.extra_metadata = metadata
        db.add(convo)
        db.commit()

    return _handler


def test_an_action_naming_no_showing_is_refused_by_the_producer(order_flow_v2_live):
    """A previously selected address does not answer an invented showing.

    The reviewed producer parsed the identifier, then accepted any row
    already marked ``selected`` for that address. A customer whose
    address had been selected on an earlier turn therefore produced a
    clean PASS from a handler that ran no owner and sent nothing.
    """
    from services.internal_conversational_e2e_of2_fixtures import (  # noqa: PLC0415
        STATE_ACCEPTED_PENDING_CITY,
        prepare_of2_scenario_fixture,
    )

    db, tenant, phone = _live_sandbox()
    fixture = prepare_of2_scenario_fixture(
        db,
        tenant_id=tenant.id,
        customer_phone=phone,
        scenario_id="stale",
        session_id=str(uuid.uuid4()),
        state=STATE_ACCEPTED_PENDING_CITY,
    )
    # The fixture really did leave an address selected before the turn.
    assert fixture.accepted_address_id

    evidence = _selection_probe(
        db=db,
        tenant=tenant,
        phone=phone,
        fixture=fixture,
        button_id=f"nahla_addr_select:NEVER_OFFERED:{fixture.accepted_address_id}",
        handler=_counter_bump_handler(db, fixture),
        label="stale",
    )

    assert evidence["verdict"] == "fail"
    assert "structured_selection_showing_unknown" in evidence["blockers"]
    assert "structured_selection_operation_not_recorded" in evidence["blockers"]
    assert "structured_selection_address_mismatch" in evidence["blockers"]

    chosen = evidence["selection_evidence"]
    # The producer itself reports the absence — these are not flags the
    # test supplied.
    assert chosen["showing_exists"] is False
    assert chosen["operation_recorded"] is False
    assert chosen["selection_matches_action"] is False
    # And it still reports the pre-existing state honestly, rather than
    # hiding it: the row IS selected. It just does not answer this action.
    assert chosen["selection_state"] == "selected"


def test_a_superseded_showing_does_not_answer_a_later_action(order_flow_v2_live):
    """An offer id that was live once, but is not the live showing now."""
    from services.internal_conversational_e2e_of2_fixtures import (  # noqa: PLC0415
        STATE_SEVERAL_SAVED,
        prepare_of2_scenario_fixture,
    )

    db, tenant, phone = _live_sandbox()
    fixture = prepare_of2_scenario_fixture(
        db,
        tenant_id=tenant.id,
        customer_phone=phone,
        scenario_id="superseded",
        session_id=str(uuid.uuid4()),
        state=STATE_SEVERAL_SAVED,
    )
    address_id = fixture.address_ids[0]

    evidence = _selection_probe(
        db=db,
        tenant=tenant,
        phone=phone,
        fixture=fixture,
        button_id=f"nahla_addr_select:SUPERSEDED_OFFER:{address_id}",
        handler=_counter_bump_handler(db, fixture),
        label="superseded",
    )
    assert evidence["verdict"] == "fail"
    assert "structured_selection_showing_unknown" in evidence["blockers"]
    assert evidence["selection_evidence"]["operation_recorded"] is False


def test_the_real_continuation_produces_a_complete_consumption_chain(
    tmp_path, monkeypatch, order_flow_v2_live
):
    """The positive case, with every link the producer establishes.

    Drives the shipped manifest through ``run_session`` and reads the
    continuation's evidence — the showing that existed when the action
    was taken, the writer's own published operation for that turn, and
    the durable row carrying that operation's reference.
    """
    module, _ = _load_scenarios(
        REPO_ROOT / "docs" / "engineering" / "of2-address-scenarios.json"
    )
    engine, tenant_id, url = _live_engine_sandbox(tmp_path)
    env = _session_env(url=url, tenant_id=tenant_id, tmp_path=tmp_path)
    monkeypatch.setattr(
        module,
        "execute_preflight",
        lambda **kw: {
            "ok": True,
            "runtime_revision": "sandbox",
            "database_identity_fingerprint": "sha256:" + "0" * 64,
            "attestation_id": "att-sandbox",
            "llm_allowed_hosts": ["api.anthropic.com"],
        },
    )
    result = asyncio.run(
        module.run_session(
            tenant_id=tenant_id,
            scenario_path=REPO_ROOT
            / "docs"
            / "engineering"
            / "of2-address-scenarios.json",
            env=env,
            engine=engine,
        )
    )
    session = json.loads(Path(result["session_path"]).read_text(encoding="utf-8"))
    by_scenario = {
        (r["scenario_id"], r["turn_index"]): r for r in session["turn_results"]
    }
    chosen = by_scenario[("continuation_after_captured_choice", 1)][
        "selection_evidence"
    ]

    # The showing the action answered existed, and the writer published
    # an operation for this turn naming this address at the SHOWN
    # revision.
    assert chosen["showing_exists"] is True
    assert chosen["shown_fingerprint"]
    assert chosen["operation_recorded"] is True
    assert chosen["operation_observed_in_turn"] is True
    assert chosen["operation_address_id"] == chosen["action_address_id"]
    assert chosen["operation_fingerprint"] == chosen["shown_fingerprint"]
    # The writer composes its reference from the showing it consumed and
    # the turn it answered, and the durable row carries that reference.
    assert chosen["operation_ref"].startswith(f"{chosen['offer_identity']}:")
    assert chosen["selection_operation_ref"] == chosen["operation_ref"]
    assert chosen["selected_fingerprint"] == chosen["shown_fingerprint"]
    assert chosen["selection_matches_action"] is True
    assert by_scenario[("continuation_after_captured_choice", 1)]["blockers"] == []


def test_a_revision_that_changed_since_it_was_shown_is_refused():
    """The recorded revision must be the one that was SHOWN.

    Checking only that the selected fingerprint is non-empty let an
    address whose content had changed since the customer saw it pass as
    though they had approved the new content.
    """
    blockers = _result_blockers(
        selection=_selection(
            shown_fingerprint="fp-as-shown",
            operation_fingerprint="fp-as-shown",
            selected_fingerprint="fp-changed-since",
            selection_matches_action=False,
        )
    )
    assert "structured_selection_revision_mismatch" in blockers
    assert "structured_selection_address_mismatch" in blockers


def test_an_operation_left_by_an_earlier_turn_is_not_this_turns():
    """Production stamps no turn id into a button turn's inbound metadata.

    The platform's reader therefore scopes an operation by conversation
    and TTL, so without the publication time an operation performed on a
    previous turn would answer for a turn that ran no owner at all.
    """
    blockers = _result_blockers(
        selection=_selection(
            operation_observed_in_turn=False, selection_matches_action=False
        )
    )
    assert "structured_selection_operation_not_from_this_turn" in blockers
    assert "structured_selection_operation_not_recorded" not in blockers


def _structured_action(action_id: str):
    """The (offer, address) the platform itself reads out of an action id."""
    from modules.ai.order_flow_v2.checkout_context import (  # noqa: PLC0415
        structured_consent_action,
    )

    resolved = structured_consent_action({"button_id": action_id})
    assert resolved, action_id
    return resolved


def _show_choices_and_get_action(db, tenant, phone, fixture, label):
    """Run a real showing turn and return one action id it actually sent."""
    from services.internal_conversational_e2e_harness import (  # noqa: PLC0415
        run_sandbox_of2_turn,
    )

    outcome = asyncio.run(
        run_sandbox_of2_turn(
            db=db,
            request=_entrypoint_request(
                tenant=tenant,
                convo=fixture.conversation,
                phone=phone,
                text="متابعة الشراء",
                meta=_interactive("of2_resume_checkout"),
                label=label,
            ),
        )
    )
    record = outcome.evidence["address_turn"]
    assert outcome.evidence["blockers"] == [], outcome.evidence["blockers"]
    assert record["receipt_action_ids"], record
    return record["receipt_action_ids"][0]


def test_replaying_a_consumed_action_is_not_a_new_selection(order_flow_v2_live):
    """The last C1 gap: a tap already answered does not select again.

    The writer is idempotent, so on a replay it republishes the same
    operation and leaves the row exactly as a fresh selection would. The
    showing is still live and the row still says ``selected``, so every
    post-turn link held and the replay was counted as a second
    structured selection by a turn that decided nothing.

    Nothing here demands a second durable write. The replay is separated
    from the original only by what the row carried BEFORE the turn ran.
    """
    from services.internal_conversational_e2e_of2_fixtures import (  # noqa: PLC0415
        STATE_SEVERAL_SAVED,
        prepare_of2_scenario_fixture,
    )

    db, tenant, phone = _live_sandbox()
    fixture = prepare_of2_scenario_fixture(
        db,
        tenant_id=tenant.id,
        customer_phone=phone,
        scenario_id="replay",
        session_id=str(uuid.uuid4()),
        state=STATE_SEVERAL_SAVED,
    )
    action_id = _show_choices_and_get_action(db, tenant, phone, fixture, "replay")

    # 1. The genuine selection. The real owner runs; nothing is patched.
    first = _selection_probe(
        db=db,
        tenant=tenant,
        phone=phone,
        fixture=fixture,
        button_id=action_id,
        handler=None,
        label="replay",
    )
    assert first["blockers"] == [], first["blockers"]
    chosen = first["selection_evidence"]
    assert chosen["selection_matches_action"] is True
    assert chosen["action_already_consumed"] is False
    # Nothing had consumed this action before that turn.
    assert chosen["selection_operation_ref_before"] != chosen["operation_ref"]
    consumed_ref = chosen["operation_ref"]

    # 2. The SAME tap again. Every post-turn link still holds — which is
    #    exactly why the pre-turn row is what decides.
    second = _selection_probe(
        db=db,
        tenant=tenant,
        phone=phone,
        fixture=fixture,
        button_id=action_id,
        handler=None,
        label="replay",
    )
    replay = second["selection_evidence"]
    assert replay["action_already_consumed"] is True
    assert replay["selection_operation_ref_before"] == consumed_ref
    assert replay["selection_state_before"] == "selected"
    assert replay["selection_matches_action"] is False
    assert second["verdict"] == "fail"
    assert "structured_selection_action_already_consumed" in second["blockers"]

    # The original selection is not retracted by refusing the replay: the
    # durable row still holds it, at the same reference.
    from models import CustomerAddressProvenance  # noqa: PLC0415

    _offer, address_id = _structured_action(action_id)
    row = (
        db.query(CustomerAddressProvenance)
        .filter(
            CustomerAddressProvenance.tenant_id == tenant.id,
            CustomerAddressProvenance.customer_address_id == int(address_id),
        )
        .first()
    )
    assert row is not None
    assert row.selection_state == "selected"
    assert row.selection_operation_ref == consumed_ref


def test_choosing_the_same_address_again_through_a_new_showing_passes(
    order_flow_v2_live,
):
    """Re-selection is not required to change the address.

    Refusing a replay must not turn into refusing a customer who is
    shown their addresses again and picks the same one. That answers a
    NEW showing, whose identity composes a different reference, so it is
    a selection in its own right even though the address id is
    unchanged — the replay check keys on the consumed action, never on
    the address.

    The second showing is recorded through the platform's own offer
    writer, the same call the delivery boundary makes, so the offer this
    turn answers is a real one and not a fixture's invention.
    """
    from modules.ai.order_flow_v2.checkout_context import (  # noqa: PLC0415
        record_offered_address_set,
    )
    from services.internal_conversational_e2e_of2_fixtures import (  # noqa: PLC0415
        STATE_SEVERAL_SAVED,
        prepare_of2_scenario_fixture,
    )

    db, tenant, phone = _live_sandbox()
    fixture = prepare_of2_scenario_fixture(
        db,
        tenant_id=tenant.id,
        customer_phone=phone,
        scenario_id="reselect",
        session_id=str(uuid.uuid4()),
        state=STATE_SEVERAL_SAVED,
    )
    first_action = _show_choices_and_get_action(db, tenant, phone, fixture, "reselect")
    first = _selection_probe(
        db=db, tenant=tenant, phone=phone, fixture=fixture,
        button_id=first_action, handler=None, label="reselect",
    )
    assert first["blockers"] == [], first["blockers"]
    first_offer, address_id = _structured_action(first_action)
    shown_fingerprint = first["selection_evidence"]["shown_fingerprint"]
    assert shown_fingerprint

    # A genuinely new showing of the SAME address, at the revision it
    # still has, recorded by the platform's own writer.
    second_offer = uuid.uuid4().hex[:16]
    assert second_offer != first_offer
    db.refresh(fixture.conversation)
    record_offered_address_set(
        db,
        tenant_id=tenant.id,
        conversation=fixture.conversation,
        candidates=[
            {"address_id": int(address_id), "fingerprint": shown_fingerprint}
        ],
        delivery_ref="reselect-delivery",
        offer_id=second_offer,
    )
    db.commit()

    second = _selection_probe(
        db=db, tenant=tenant, phone=phone, fixture=fixture,
        button_id=f"nahla_addr_select:{second_offer}:{address_id}",
        handler=None, label="reselect",
    )
    again = second["selection_evidence"]
    # The same address, and that is not held against it.
    assert again["action_address_id"] == str(address_id)
    assert again["offer_identity"] == second_offer
    assert again["action_already_consumed"] is False
    # The previous consumption is still on the row, and is simply not
    # this action's reference.
    assert again["selection_operation_ref_before"].startswith(f"{first_offer}:")
    assert again["selection_state_before"] == "selected"
    assert "structured_selection_action_already_consumed" not in second["blockers"]
    assert again["selection_matches_action"] is True
    assert second["blockers"] == [], second["blockers"]


def test_a_replayed_action_is_named_as_such_not_merely_mismatched():
    """The blocker says what actually happened.

    Every post-turn link can hold on a replay, so without its own name
    the refusal would have to borrow one that misdescribes it — the
    address did match, the scope was right, and the row was durable.
    """
    blockers = _result_blockers(
        selection=_selection(
            action_already_consumed=True,
            selection_operation_ref_before="offer:",
            selection_state_before="selected",
            selection_matches_action=False,
        )
    )
    assert "structured_selection_action_already_consumed" in blockers
    # And it is not confused with the cases that mean something else.
    assert "structured_selection_showing_unknown" not in blockers
    assert "structured_selection_not_durably_recorded" not in blockers
    assert "structured_selection_scope_unverified" not in blockers


def test_a_first_selection_is_not_treated_as_a_replay():
    """A row that carries ANOTHER action's reference is not this action's."""
    blockers = _result_blockers(
        selection=_selection(
            selection_operation_ref_before="a-different-offer:",
            selection_state_before="selected",
        )
    )
    assert "structured_selection_action_already_consumed" not in blockers
    assert blockers == []
