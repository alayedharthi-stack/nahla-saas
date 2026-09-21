"""The seam the webhook calls: one decision, one owner, nothing claimed falsely.

This is the only place the WhatsApp handler touches the commerce runtime, so
these cases hold it to the two promises the handler relies on: while the pilot
is off nothing at all happens, and once the route is taken it is never handed
back — not even by an exception. No database, no model, no network.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import pytest

from core.commerce_runtime import pilot_guard as pg
from core.commerce_runtime import runtime_entry as entry
from services import commerce_runtime_pilot as seam

TENANT = 4242
PHONE_ID = "1555000111"
OWNER_PHONE = "+966500000001"
MODEL = "model-configured-for-this-pilot"


class _Connection:
    id = 17


class _Barrier:
    """The tenant's handover barrier row, in its real shape."""

    def __init__(self, state: str, generation: int = 3) -> None:
        self.tenant_id = TENANT
        self.namespace = "live"
        self.state = state
        self.generation = generation
        self.opened_at = None
        self.settled_at = None
        self.evidence: Dict[str, Any] = {}


# The barrier row this tenant has. ``None`` — no row — is an open barrier, which
# is the state every case but the handover ones runs in.
BARRIER: List[Any] = [None]


class _Query:
    def __init__(self, row: Any) -> None:
        self._row = row

    def filter(self, *_a: Any) -> "_Query":
        return self

    def order_by(self, *_a: Any) -> "_Query":
        return self

    def first(self) -> Any:
        return self._row

    def all(self) -> List[Any]:
        return [] if self._row is None else [self._row]


class _Db:
    """The rows this seam actually reads: the connection and the barrier."""

    def query(self, model: Any) -> _Query:
        name = str(getattr(model, "__name__", ""))
        if name == "HandoverBarrier":
            return _Query(BARRIER[0])
        if name in {"HandoverWorker", "DeferredInbound"}:
            return _Query(None)
        return _Query(_Connection())


class _Convo:
    id = 501
    customer_id = 9
    language = "ar"


class _Trace:
    def __init__(self) -> None:
        self.marked: List[Dict[str, Any]] = []

    def mark_outbound_sent(self, *, source: str, length: int = 0) -> None:
        self.marked.append({"source": source, "length": length})


def report(**overrides: Any) -> entry.TurnReport:
    fields: Dict[str, Any] = {
        "reason": entry.HANDLED, "tenant_id": TENANT, "conversation_id": _Convo.id,
        "turn_id": 7, "dispatch_status": "accepted", "provider_message_id": "wamid.OK",
        "delivery_sequence_id": 3, "reply_text": "النص المرسل",
        "evidence_refs": ("catalog:product:1",),
    }
    fields.update(overrides)
    return entry.TurnReport(**fields)


@pytest.fixture()
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(pg.ENV_ENABLED, "true")
    monkeypatch.setenv(pg.ENV_TENANT_ALLOWLIST, str(TENANT))
    monkeypatch.setenv(pg.ENV_RECIPIENT_ALLOWLIST, OWNER_PHONE)
    monkeypatch.setenv(pg.ENV_MODEL, MODEL)


HISTORY = [
    ("inbound", "سؤال سابق", None),
    ("outbound", "جواب سابق", None),
    ("inbound", "عندكم حذاء؟", None),
]


@pytest.fixture()
def saved(monkeypatch: pytest.MonkeyPatch) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []

    class _StateManager:
        @staticmethod
        def save_message(db, phone, body, direction, **kwargs: Any) -> int:
            rows.append({"phone": phone, "body": body, "direction": direction, **kwargs})
            return 1

    import core.conversation_engine as engine_module

    monkeypatch.setattr(engine_module, "StateManager", _StateManager, raising=True)
    monkeypatch.setattr(seam, "_history_rows",
                        lambda db, **kwargs: (read.append(kwargs) or list(HISTORY)),
                        raising=True)
    return rows


read: List[Dict[str, Any]] = []


@pytest.fixture(autouse=True)
def _clear_reads() -> None:
    read.clear()
    BARRIER[0] = None


def draining_barrier() -> Any:
    """The row a drained tenant carries, in its real shape."""
    from core.commerce_runtime import handover

    return _Barrier(handover.STATE_DRAINING)


def call(*, trace: Optional[_Trace] = None, text: str = "عندكم حذاء؟",
         legacy_answered: bool = False, gate_skipped: bool = False) -> seam.PilotResult:
    return asyncio.run(seam.maybe_handle_with_commerce_runtime(
        db=_Db(), tenant_id=TENANT, phone_id=PHONE_ID, to=OWNER_PHONE, text=text,
        convo=_Convo(), wa_msg_id="wamid.INBOUND", inbound_metadata={"k": "v"},
        trace=trace or _Trace(), legacy_already_answered=legacy_answered,
        ai_gate_skipped=gate_skipped, customer_name="نورة عبدالله",
    ))


def patch_runtime(monkeypatch: pytest.MonkeyPatch, outcome: Any) -> List[Dict[str, Any]]:
    calls: List[Dict[str, Any]] = []

    def run_turn(**kwargs: Any) -> Any:
        calls.append(kwargs)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(entry, "run_commerce_runtime_turn", run_turn, raising=True)
    return calls


# ── While the pilot is off ───────────────────────────────────────────────────


def test_while_the_pilot_is_off_the_legacy_path_keeps_the_turn(monkeypatch):
    monkeypatch.delenv(pg.ENV_ENABLED, raising=False)
    calls = patch_runtime(monkeypatch, report())
    result = call()
    assert result.handled is False and result.reason == pg.PILOT_DISABLED
    assert calls == []


@pytest.mark.parametrize("kwargs, reason", [
    ({"legacy_answered": True}, pg.LEGACY_ALREADY_ANSWERED),
    ({"gate_skipped": True}, pg.AI_GATE_SKIPPED),
    ({"text": "   "}, pg.EMPTY_INBOUND),
])
def test_every_refusal_leaves_the_turn_with_the_legacy_path(enabled, monkeypatch, kwargs, reason):
    calls = patch_runtime(monkeypatch, report())
    result = call(**kwargs)
    assert result.handled is False and result.reason == reason
    assert calls == []


def test_a_pilot_with_no_configured_model_never_reaches_the_runtime(enabled, monkeypatch):
    monkeypatch.delenv(pg.ENV_MODEL, raising=False)
    calls = patch_runtime(monkeypatch, report())
    result = call()
    assert result.handled is False and result.reason == pg.MODEL_NOT_CONFIGURED
    assert calls == []


# ── Once the route is taken ──────────────────────────────────────────────────


def test_a_permitted_turn_is_run_with_the_verified_scope_and_the_recorded_conversation(
        enabled, monkeypatch, saved):
    calls = patch_runtime(monkeypatch, report())
    result = call()
    assert result.handled is True and result.reason == entry.HANDLED
    assert len(calls) == 1
    passed = calls[0]
    assert passed["tenant_id"] == TENANT
    assert passed["conversation_id"] == _Convo.id
    assert passed["connection_ref"] == f"wa:{PHONE_ID}"
    assert passed["connection_id"] == "17"            # the verified row id, not the channel ref
    assert passed["customer_id"] == _Convo.customer_id
    assert passed["provider_message_id"] == "wamid.INBOUND"
    assert passed["inbound_text"] == "عندكم حذاء؟"
    assert passed["instructions"].strip()              # the existing instructions, not composed here
    assert passed["budget"].max_steps <= pg.MAX_STEPS_CEILING
    assert passed["model"] == MODEL                    # the configured one, not a resolved default
    assert passed["context_preamble"]["verified_customer_name"] == "نورة عبدالله"
    # The prior turns, with the message being answered not shown twice.
    assert passed["history"] == [{"role": "user", "text": "سؤال سابق"},
                                 {"role": "assistant", "text": "جواب سابق"}]


def test_an_inbound_message_the_store_has_not_persisted_is_not_trimmed_from_the_history(
        enabled, monkeypatch, saved):
    calls = patch_runtime(monkeypatch, report())
    call(text="سؤال جديد تمامًا")
    assert calls[0]["history"][-1] == {"role": "user", "text": "عندكم حذاء؟"}


def test_a_failure_after_the_route_was_taken_never_hands_the_customer_back(enabled, monkeypatch):
    patch_runtime(monkeypatch, RuntimeError("boom"))
    result = call()
    assert result.handled is True and result.reason == "internal_error"


def test_an_unavailable_history_does_not_stop_the_turn(enabled, monkeypatch):
    calls = patch_runtime(monkeypatch, report())

    def _broken(*args: Any, **kwargs: Any):
        raise RuntimeError("no history")

    class _StateManager:
        @staticmethod
        def save_message(*args: Any, **kwargs: Any) -> int:
            return 1

    import core.conversation_engine as engine_module

    monkeypatch.setattr(engine_module, "StateManager", _StateManager, raising=True)
    monkeypatch.setattr(seam, "_history_rows", _broken, raising=True)
    result = call()
    assert result.handled is True
    assert calls[0]["history"] == []


# ── What is recorded afterwards ──────────────────────────────────────────────


def test_an_identified_acceptance_is_traced_and_persisted_with_its_provenance(
        enabled, monkeypatch, saved):
    patch_runtime(monkeypatch, report())
    trace = _Trace()
    call(trace=trace)
    assert trace.marked == [{"source": seam.TRACE_SOURCE, "length": len("النص المرسل")}]
    assert len(saved) == 1
    row = saved[0]
    assert row["direction"] == "outbound" and row["body"] == "النص المرسل"
    meta = row["extra_metadata"]
    assert meta["compose_source"] == "llm"
    assert meta["chosen_path"] == "commerce_runtime_pilot"
    # The transport never ran here, so the transmitted text was not observed and
    # is recorded as unverified rather than certified unchanged.
    assert meta["final_text_transformed"] is True
    assert meta["final_transform_reasons"] == [seam.WIRE_UNOBSERVED]
    assert meta["commerce_runtime_wire_observed"] is False
    assert meta["provider_message_id"] == "wamid.OK"
    assert meta["commerce_runtime_turn_id"] == 7
    assert meta["evidence_refs"] == ["catalog:product:1"]


@pytest.mark.parametrize("overrides", [
    {"dispatch_status": "rejected", "provider_message_id": None},
    {"dispatch_status": "unknown", "provider_message_id": None},
    {"dispatch_status": "accepted", "provider_message_id": None},   # no id is not an acceptance
    {"dispatch_status": None, "delivery_sequence_id": None, "reply_text": ""},
])
def test_a_turn_that_was_not_accepted_traces_nothing_and_persists_nothing(
        enabled, monkeypatch, saved, overrides):
    patch_runtime(monkeypatch, report(**overrides))
    trace = _Trace()
    result = call(trace=trace)
    assert result.handled is True
    assert trace.marked == [] and saved == []


def test_an_accepted_send_whose_text_cannot_be_read_back_persists_no_row(
        enabled, monkeypatch, saved):
    patch_runtime(monkeypatch, report(reply_text=""))
    trace = _Trace()
    call(trace=trace)
    assert trace.marked == [{"source": seam.TRACE_SOURCE, "length": 0}]
    assert saved == []                                 # the send happened; no message is invented


# ── The history belongs to this conversation (F7) ────────────────────────────


def test_the_history_is_read_for_the_conversation_being_answered(enabled, monkeypatch, saved):
    """Not resolved from the phone number: one tenant can hold several."""
    patch_runtime(monkeypatch, report())
    call()
    assert len(read) == 1
    assert read[0]["conversation_id"] == _Convo.id
    assert read[0]["tenant_id"] == TENANT


# ── What was transmitted, not what was reserved (F11) ────────────────────────


def wire_send(monkeypatch: pytest.MonkeyPatch, *, transmitted: Optional[str] = None,
              duplicate: bool = False) -> List[Dict[str, Any]]:
    """Double the webhook's sender, observing the wire exactly as ``_post_wa`` does."""
    seen: List[Dict[str, Any]] = []

    async def _send(*, phone_id: str, to: str, text: str, _tenant_id: int, _db: Any,
                    _blocked_path: str, _result_sink: Dict[str, Any]) -> bool:
        from core.outbound_wire_audit import observe_wire_payload

        body = text if transmitted is None else transmitted
        payload = {"to": to, "type": "text", "text": {"body": body}}
        observe_wire_payload(_tenant_id, payload, "outbound_payload_sanitizer")
        seen.append({"to": to, "handed": text, "transmitted": body})
        _result_sink.update({"classification": "ok", "wamid": "wamid.WIRE", "http_status": 200,
                             "duplicate_suppressed": duplicate})
        return True

    import routers.whatsapp_webhook as webhook

    monkeypatch.setattr(webhook, "_send_whatsapp_message", _send, raising=True)
    return seen


def run_through_transport(monkeypatch: pytest.MonkeyPatch, intent: str) -> None:
    """Patch the runtime so it dispatches the reserved intent through the real seam."""
    def run_turn(**kwargs: Any) -> Any:
        response = kwargs["transport"]({"text": intent})
        assert response.http_status == 200
        return report(reply_text=intent, provider_message_id="wamid.WIRE")

    monkeypatch.setattr(entry, "run_commerce_runtime_turn", run_turn, raising=True)


def test_an_unchanged_wire_is_recorded_as_untransformed(enabled, monkeypatch, saved):
    wire_send(monkeypatch)
    run_through_transport(monkeypatch, "النص المرسل")
    call()
    meta = saved[0]["extra_metadata"]
    assert saved[0]["body"] == "النص المرسل"
    assert meta["final_text_transformed"] is False and meta["final_transform_reasons"] == []
    assert meta["commerce_runtime_wire_observed"] is True
    assert meta["final_customer_text_source"] == "llm"


def test_a_send_path_that_rewrote_the_body_stores_what_was_transmitted(
        enabled, monkeypatch, saved):
    """The customer saw the sanitiser's text; the conversation must say so."""
    sent = wire_send(monkeypatch, transmitted="النص بعد التنقية")
    run_through_transport(monkeypatch, "النص المرسل")
    trace = _Trace()
    call(trace=trace)
    assert sent[0]["handed"] == "النص المرسل"
    row = saved[0]
    assert row["body"] == "النص بعد التنقية"
    meta = row["extra_metadata"]
    assert meta["final_text_transformed"] is True
    assert "outbound_payload_sanitizer" in meta["final_transform_reasons"]
    assert meta["final_customer_text_source"] == "llm_postprocess"
    assert trace.marked == [{"source": seam.TRACE_SOURCE, "length": len("النص بعد التنقية")}]


def test_the_reserved_intent_stays_identifiable_beside_the_transmitted_text(
        enabled, monkeypatch, saved):
    import hashlib

    wire_send(monkeypatch, transmitted="النص بعد التنقية")
    run_through_transport(monkeypatch, "النص المرسل")
    call()
    meta = saved[0]["extra_metadata"]
    assert meta["commerce_runtime_intent_sha256"] == hashlib.sha256(
        "النص المرسل".encode("utf-8")).hexdigest()
    assert meta["commerce_runtime_delivery_sequence_id"] == 3   # the ledger keeps the intent


def test_a_duplicate_suppressed_send_says_so_rather_than_claiming_this_transmission(
        enabled, monkeypatch, saved):
    wire_send(monkeypatch, duplicate=True)
    run_through_transport(monkeypatch, "النص المرسل")
    call()
    assert saved[0]["extra_metadata"]["commerce_runtime_wire_duplicate_suppressed"] is True


def test_an_observation_never_writes_to_any_row_by_itself(enabled, monkeypatch, saved):
    """The audit is unbound: it reads the wire, it does not persist attempts."""
    wire_send(monkeypatch)
    run_through_transport(monkeypatch, "النص المرسل")
    call()
    assert len(saved) == 1                     # exactly the one row this module writes
    assert "wire_attempts" not in saved[0]["extra_metadata"]


def test_a_persistence_failure_does_not_undo_a_send_that_already_happened(enabled, monkeypatch):
    patch_runtime(monkeypatch, report())

    class _Failing:
        @staticmethod
        def save_message(*args: Any, **kwargs: Any):
            raise RuntimeError("store unavailable")

    import core.conversation_engine as engine_module

    monkeypatch.setattr(engine_module, "StateManager", _Failing, raising=True)
    monkeypatch.setattr(seam, "_history_rows", lambda db, **kwargs: [], raising=True)
    result = call()
    assert result.handled is True and result.reason == entry.HANDLED
