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
        "stage": STAGE_ORDINARY,
        "collection_field": "city",
        "turn_ref": "probe.city.0",
        "observed_at": "orchestrator_adapter",
        "response_goal": "collect_delivery_city",
        "missing_field": "city",
        "delivery_address_status": "accepted",
        "has_accepted_maps_reference": True,
        "outcome_recorded": True,
        "candidate_present": True,
    }
    call.update(overrides)
    return call


def _address_turn(**overrides: Any) -> Dict[str, Any]:
    record = {
        "turn_ref": "probe.city.0",
        "outbound_metadata_turn_ref": "probe.city.0",
        "outbound_message_id": "4242",
        "transport": "captured",
        "delivery_ids": ["captured." + "a" * 32],
        "captured_payload_digest": "sha256:" + "b" * 64,
        "execution_path": PATH_ORDINARY,
        "delivered_surface": SURFACE_LIST,
        "failure_injection": "none",
        "injection_state": {"kind": "none", "site": "", "fired": 0, "armed": False},
        "compose_entered": True,
        "collection_field": "city",
        "receipt_action_ids": ["nahla_addr_select:offer:1"],
        "receipt_address_ids": ["1"],
        "recorded_action_ids": ["1"],
        "outbound_provenance": {"compose_source": "llm", "address_reply_composed": True},
        "model_bound_calls": [_model_call()],
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
    assert CODE_ADDRESS_EVIDENCE_INCOMPLETE in _blockers(
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
        compose_entered=False, model_bound_calls=[], outbound_provenance={"compose_source": "llm"},
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
