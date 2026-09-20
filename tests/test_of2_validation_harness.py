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


def _address_turn(**overrides: Any) -> Dict[str, Any]:
    record = {
        "turn_ref": "probe.city.0",
        "outbound_metadata_turn_ref": "probe.city.0",
        "transport": "captured",
        "execution_path": PATH_ORDINARY,
        "delivered_surface": SURFACE_LIST,
        "failure_injection": "none",
        "collection_field": "city",
        "receipt_action_ids": ["nahla_addr_select:offer:1"],
        "recorded_action_ids": ["1"],
        "model_bound_calls": [
            {
                "call_index": 0,
                "stage": STAGE_ORDINARY,
                "collection_field": "city",
                "turn_ref": "probe.city.0",
                "observed_at": "orchestrator_adapter",
                "response_goal": "collect_delivery_city",
                "missing_field": "city",
                "delivery_address_status": "accepted",
                "has_accepted_maps_reference": True,
            }
        ],
    }
    record.update(overrides)
    return record


def test_a_complete_address_record_is_accepted():
    assert address_turn_evidence_blockers(
        _address_turn(), expects_address_turn=True
    ) == []


def test_an_optional_field_does_not_permit_silent_absence():
    """``address_turn`` is optional in the schema, never optional in fact.

    A v3 artifact for a Brain turn legitimately has no address record. A
    scenario that declared it expects one and produced nothing is a failed
    run, not a thinner artifact.
    """
    assert address_turn_evidence_blockers(None, expects_address_turn=False) == []
    assert address_turn_evidence_blockers(None, expects_address_turn=True) == [
        CODE_ADDRESS_EVIDENCE_MISSING
    ]
    assert address_turn_evidence_blockers({}, expects_address_turn=True) == [
        CODE_ADDRESS_EVIDENCE_MISSING
    ]


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({"execution_path": "made_up"}, CODE_ADDRESS_EVIDENCE_INCOMPLETE),
        ({"delivered_surface": "carrier_pigeon"}, CODE_ADDRESS_EVIDENCE_INCOMPLETE),
        ({"failure_injection": "whatever"}, CODE_ADDRESS_EVIDENCE_INCOMPLETE),
        ({"transport": "dispatched"}, CODE_ADDRESS_EVIDENCE_INCOMPLETE),
        ({"model_bound_calls": []}, CODE_MODEL_CALL_EVIDENCE_INCOMPLETE),
    ],
)
def test_an_incomplete_address_record_is_refused(overrides, expected):
    blockers = address_turn_evidence_blockers(
        _address_turn(**overrides), expects_address_turn=True
    )
    assert expected in blockers


def test_a_reconstructed_observation_is_not_an_observation():
    """Only the boundary counts. A record from elsewhere is refused."""
    record = _address_turn()
    record["model_bound_calls"][0]["observed_at"] = "final_state_reconstruction"
    assert CODE_MODEL_CALL_EVIDENCE_INCOMPLETE in address_turn_evidence_blockers(
        record, expects_address_turn=True
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"outbound_metadata_turn_ref": ""},
        {"outbound_metadata_turn_ref": "probe.other.9"},
        {"turn_ref": ""},
    ],
)
def test_three_artifacts_that_cannot_be_tied_together_prove_nothing(overrides):
    """The captured payload, the stored metadata and the receipt are one turn."""
    assert CODE_ADDRESS_EVIDENCE_UNBOUND in address_turn_evidence_blockers(
        _address_turn(**overrides), expects_address_turn=True
    )


def test_a_model_call_from_another_turn_breaks_the_binding():
    record = _address_turn()
    record["model_bound_calls"][0]["turn_ref"] = "probe.other.9"
    assert CODE_ADDRESS_EVIDENCE_UNBOUND in address_turn_evidence_blockers(
        record, expects_address_turn=True
    )


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
