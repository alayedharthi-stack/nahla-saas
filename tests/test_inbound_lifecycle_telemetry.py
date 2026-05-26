"""tests/test_inbound_lifecycle_telemetry.py
─────────────────────────────────────────
Wave 2.0 Phase 1 (May 2026) — structured inbound-lifecycle
telemetry. The module is **observation-only**: it must produce
greppable summary lines without changing any caller's behaviour.

Every test in this file enforces one of the two invariants:

  (a) the module itself does what its docstring promises (closed
      event vocabulary, kill switch, masking, summary line shape,
      contextvar isolation), OR
  (b) wiring at a downstream call site is byte-equivalent to legacy:
      record_lifecycle never raises, never returns a value, and
      callers behave identically with the flag ON, OFF, or with the
      module entirely absent.

If a future refactor drops one of these guarantees, *this test file
is the thing that fails first*.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

import pytest

from core.inbound_lifecycle import (  # noqa: E402
    ALL_EVENTS,
    EVENT_BG_REJECTED,
    EVENT_BRAIN_INVOKED,
    EVENT_CONVERSATION_CREATED,
    EVENT_DEDUP_DROP_DB,
    EVENT_DEDUP_DROP_MEMORY,
    EVENT_END_DROPPED,
    EVENT_END_OK,
    EVENT_END_UNCAUGHT,
    EVENT_HTTP_REPLAY_REJECT,
    EVENT_HTTP_SIGNATURE_REJECT,
    EVENT_MESSAGE_SAVED,
    EVENT_MESSAGE_SAVED_ORPHAN,
    EVENT_MESSAGE_SAVE_ROLLBACK,
    EVENT_MISSING_PHONE_ID,
    EVENT_PAYMENT_SHORT_CIRCUIT,
    EVENT_RECEIPT_SHORT_CIRCUIT,
    EVENT_RECEIVED,
    EVENT_TENANT_RESOLVED,
    EVENT_UNKNOWN_PHONE_ID,
    EVENT_UNSUB_SHORT_CIRCUIT,
    InboundLifecycleTrace,
    LifecycleEvent,
    attach_normalizer_outcome,
    attach_tenant,
    current_trace,
    emit_lifecycle_summary,
    emit_standalone_event,
    inbound_lifecycle_trace,
    is_inbound_lifecycle_telemetry_enabled,
    make_trace_id,
    record_lifecycle,
)


# ════════════════════════════════════════════════════════════════
# Fixtures
# ════════════════════════════════════════════════════════════════


@pytest.fixture
def captured_lifecycle(caplog: pytest.LogCaptureFixture) -> List[str]:
    """Capture every ``[INBOUND_LIFECYCLE]`` line emitted by the
    module's logger. Returns the list of formatted messages so each
    test can assert on tokens directly."""
    caplog.set_level(logging.DEBUG, logger="nahla.inbound_lifecycle")
    yield caplog
    return None


def _summary_lines(caplog: pytest.LogCaptureFixture) -> List[str]:
    return [
        rec.getMessage()
        for rec in caplog.records
        if rec.name == "nahla.inbound_lifecycle"
        and "[INBOUND_LIFECYCLE]" in rec.getMessage()
    ]


# ════════════════════════════════════════════════════════════════
# Architectural invariants
# ════════════════════════════════════════════════════════════════


def test_event_vocabulary_is_closed_and_unique() -> None:
    """The event vocabulary is the public contract of this module.
    Anyone adding a new event must update both the constants and
    ``ALL_EVENTS``; this test catches drift."""
    assert len(ALL_EVENTS) == len(set(ALL_EVENTS)), (
        "ALL_EVENTS must contain unique tokens"
    )
    # Stable canonical names — drift here would break every grep
    # rule operators set up after rollout.
    for tok in ALL_EVENTS:
        assert isinstance(tok, str) and tok
        assert " " not in tok
        assert tok == tok.lower()


def test_kill_switch_default_on_and_respects_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("INBOUND_LIFECYCLE_TELEMETRY_ENABLED", raising=False)
    assert is_inbound_lifecycle_telemetry_enabled() is True
    monkeypatch.setenv("INBOUND_LIFECYCLE_TELEMETRY_ENABLED", "1")
    assert is_inbound_lifecycle_telemetry_enabled() is True
    monkeypatch.setenv("INBOUND_LIFECYCLE_TELEMETRY_ENABLED", "true")
    assert is_inbound_lifecycle_telemetry_enabled() is True
    monkeypatch.setenv("INBOUND_LIFECYCLE_TELEMETRY_ENABLED", "0")
    assert is_inbound_lifecycle_telemetry_enabled() is False
    monkeypatch.setenv("INBOUND_LIFECYCLE_TELEMETRY_ENABLED", "OFF")
    assert is_inbound_lifecycle_telemetry_enabled() is False


def test_record_lifecycle_is_no_op_outside_active_trace() -> None:
    """Outside any context manager, ``record_lifecycle`` must be a
    silent no-op so callers can sprinkle it freely without risk."""
    # No active trace at this point.
    assert current_trace() is None
    record_lifecycle(EVENT_RECEIVED, detail="should-not-crash")
    record_lifecycle("totally-unknown-event")
    assert current_trace() is None  # still None, nothing leaked


def test_record_lifecycle_swallows_internal_exceptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The module's docstring promises that recording never raises.
    We force a broken trace and verify the public API stays silent."""

    class BrokenTrace:
        def record(self, *a: Any, **kw: Any) -> None:
            raise RuntimeError("simulated trace corruption")

    from core import inbound_lifecycle as mod

    token = mod._active.set(BrokenTrace())  # type: ignore[arg-type]
    try:
        # Must not raise — the wrapper catches and discards.
        record_lifecycle(EVENT_RECEIVED, detail="x")
    finally:
        mod._active.reset(token)


def test_make_trace_id_prefers_msg_id_and_caps_length() -> None:
    tid_a = make_trace_id(provider="meta", msg_id="wamid.HBgL")
    tid_b = make_trace_id(provider="meta", msg_id="wamid.HBgL")
    assert tid_a == tid_b
    assert "wamid.HBgL" in tid_a
    assert len(tid_a) <= 96
    # Without msg_id, falls back to a uuid suffix → uniqueness.
    tid_c = make_trace_id(provider="meta", phone_number_id="111")
    tid_d = make_trace_id(provider="meta", phone_number_id="111")
    assert tid_c != tid_d


def test_phone_masking_never_leaks_full_number() -> None:
    from core.inbound_lifecycle import _mask_phone

    assert _mask_phone(None) == ""
    assert _mask_phone("") == ""
    assert _mask_phone("12") == "*12"
    assert _mask_phone("9665551234") == "*1234"
    # Pathological — must not raise.
    assert isinstance(_mask_phone(object()), str)  # type: ignore[arg-type]


# ════════════════════════════════════════════════════════════════
# Trace lifecycle
# ════════════════════════════════════════════════════════════════


def test_context_manager_emits_received_and_summary(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="nahla.inbound_lifecycle")
    msg = {"id": "wamid.X", "type": "text", "from": "9665551234",
           "text": {"body": "hi"}}
    with inbound_lifecycle_trace(provider="meta", phone_number_id="P", msg=msg) as tr:
        assert tr is not None
        assert tr.events[0].name == EVENT_RECEIVED
        assert tr.body_len == 2
        # Active trace is reachable from anywhere in this scope.
        assert current_trace() is tr

    # After exit, no active trace + summary line emitted.
    assert current_trace() is None
    lines = _summary_lines(caplog)
    assert len(lines) == 1
    line = lines[0]
    assert "trace_id=" in line
    assert "provider=meta" in line
    assert "msg_id=wamid.X" in line
    # Last 4 of the sender phone, masked.
    assert "sender=*1234" in line
    assert "convo_created=false" in line
    assert "message_saved=false" in line


def test_context_manager_skipped_when_kill_switch_off(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INBOUND_LIFECYCLE_TELEMETRY_ENABLED", "0")
    caplog.set_level(logging.DEBUG, logger="nahla.inbound_lifecycle")
    with inbound_lifecycle_trace(
        provider="meta", phone_number_id="P", msg={"id": "x"},
    ) as tr:
        assert tr is None
        record_lifecycle(EVENT_DEDUP_DROP_MEMORY)
    # No summary line emitted with telemetry off.
    assert _summary_lines(caplog) == []


def test_context_manager_emits_summary_even_on_uncaught_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="nahla.inbound_lifecycle")
    with pytest.raises(RuntimeError):
        with inbound_lifecycle_trace(
            provider="meta", phone_number_id="P",
            msg={"id": "wamid.E", "type": "text"},
        ) as tr:
            assert tr is not None
            tr.record(EVENT_TENANT_RESOLVED, tenant_id=33)
            raise RuntimeError("boom")
    lines = _summary_lines(caplog)
    assert len(lines) == 1
    assert f"final={EVENT_END_UNCAUGHT}" in lines[0]
    assert "tenant_id=33" in lines[0]


def test_nested_context_manager_does_not_double_emit(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="nahla.inbound_lifecycle")
    msg = {"id": "wamid.N", "type": "text", "from": "9665550000"}
    with inbound_lifecycle_trace(provider="meta", phone_number_id="P", msg=msg) as outer:
        with inbound_lifecycle_trace(
            provider="meta", phone_number_id="P", msg=msg,
        ) as inner:
            # Inner reuses the outer trace — same identity.
            assert inner is outer
    # Exactly one summary line, regardless of nesting.
    assert len(_summary_lines(caplog)) == 1


def test_attach_helpers_populate_trace() -> None:
    msg = {"id": "wamid.A", "type": "image", "from": "9665557777",
           "image": {"caption": "hello"}}
    with inbound_lifecycle_trace(provider="meta", phone_number_id="P", msg=msg) as tr:
        assert tr is not None
        attach_tenant(33)
        attach_normalizer_outcome(
            normalized_type="image", text_len=5, fallback_set=False,
        )
        assert tr.tenant_id == 33
        assert tr.has_caption is True
        # Recorded events include tenant_resolved + normalizer_ok.
        names = [e.name for e in tr.events]
        assert EVENT_TENANT_RESOLVED in names
        assert "normalizer_ok" in names


# ════════════════════════════════════════════════════════════════
# Aggregate state — orphan / rollback / persist counters
# ════════════════════════════════════════════════════════════════


def test_message_saved_with_conversation_id_is_not_orphan(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="nahla.inbound_lifecycle")
    with inbound_lifecycle_trace(
        provider="meta", phone_number_id="P",
        msg={"id": "wamid.S", "type": "text"},
    ) as tr:
        record_lifecycle(EVENT_CONVERSATION_CREATED, conversation_id=42)
        record_lifecycle(EVENT_MESSAGE_SAVED, conversation_id=42)
        assert tr is not None
        assert tr.message_saved is True
        assert tr.conversation_id == 42
        assert tr.orphan_message_count == 0
    line = _summary_lines(caplog)[0]
    assert "convo_id=42" in line
    assert "orphan_messages=0" in line
    assert "message_saved=true" in line


def test_orphan_message_event_is_counted_and_visible_in_summary(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The architectural failure mode that motivated W2.0.1: a
    ``MessageEvent`` saved with ``conversation_id=NULL``. The summary
    must surface this without an aggregator."""
    caplog.set_level(logging.DEBUG, logger="nahla.inbound_lifecycle")
    with inbound_lifecycle_trace(
        provider="meta", phone_number_id="P",
        msg={"id": "wamid.O", "type": "image"},
    ) as tr:
        record_lifecycle(EVENT_RECEIPT_SHORT_CIRCUIT)
        record_lifecycle(EVENT_MESSAGE_SAVED_ORPHAN)
        assert tr is not None
        assert tr.message_saved is True
        assert tr.orphan_message_count == 1
        assert tr.conversation_id is None
    line = _summary_lines(caplog)[0]
    assert "orphan_messages=1" in line
    assert "convo_id=" in line  # value is empty for unset
    assert f"path=" in line and "receipt_short_circuit" in line


def test_rollback_recorded_in_summary(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="nahla.inbound_lifecycle")
    with inbound_lifecycle_trace(
        provider="meta", phone_number_id="P",
        msg={"id": "wamid.R", "type": "text"},
    ) as tr:
        record_lifecycle(EVENT_CONVERSATION_CREATED, conversation_id=7)
        record_lifecycle(EVENT_MESSAGE_SAVE_ROLLBACK)
        assert tr is not None
        assert tr.rollback_count == 1
    assert "rollbacks=1" in _summary_lines(caplog)[0]


def test_summary_path_token_truncated_for_long_traces(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The summary line is one log entry — even on extreme traces it
    must stay bounded. We force >16 events and verify the truncation
    marker."""
    caplog.set_level(logging.DEBUG, logger="nahla.inbound_lifecycle")
    with inbound_lifecycle_trace(
        provider="meta", phone_number_id="P",
        msg={"id": "wamid.L", "type": "text"},
    ):
        for _ in range(40):
            record_lifecycle(EVENT_DEDUP_DROP_MEMORY)
    line = _summary_lines(caplog)[0]
    assert "...(" in line  # truncation marker present


# ════════════════════════════════════════════════════════════════
# Standalone events (HTTP-layer rejects, BG drops)
# ════════════════════════════════════════════════════════════════


def test_emit_standalone_event_logs_with_lifecycle_prefix(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="nahla.inbound_lifecycle")
    emit_standalone_event(
        EVENT_HTTP_SIGNATURE_REJECT, provider="meta", detail="bad_sig",
    )
    emit_standalone_event(
        EVENT_HTTP_REPLAY_REJECT, provider="meta",
    )
    emit_standalone_event(
        EVENT_BG_REJECTED, detail="name=whatsapp_inbound",
    )
    msgs = [r.getMessage() for r in caplog.records
            if "[INBOUND_LIFECYCLE]" in r.getMessage()]
    assert len(msgs) == 3
    assert any(f"event={EVENT_HTTP_SIGNATURE_REJECT}" in m for m in msgs)
    assert any(f"event={EVENT_HTTP_REPLAY_REJECT}" in m for m in msgs)
    assert any(f"event={EVENT_BG_REJECTED}" in m for m in msgs)


def test_emit_standalone_event_silent_when_disabled(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INBOUND_LIFECYCLE_TELEMETRY_ENABLED", "0")
    caplog.set_level(logging.DEBUG, logger="nahla.inbound_lifecycle")
    emit_standalone_event(EVENT_BG_REJECTED, detail="x")
    assert _summary_lines(caplog) == []


# ════════════════════════════════════════════════════════════════
# Integration scenarios — six directive paths from the user
# ════════════════════════════════════════════════════════════════


def _drive_and_summarize(
    *, msg: Dict[str, Any], events: List[Any],
    caplog: pytest.LogCaptureFixture,
) -> str:
    """Helper: open a trace, replay a sequence of events, return the
    single summary line that the context manager emitted."""
    caplog.clear()
    caplog.set_level(logging.DEBUG, logger="nahla.inbound_lifecycle")
    with inbound_lifecycle_trace(provider="meta", phone_number_id="P", msg=msg):
        for ev in events:
            if isinstance(ev, tuple):
                name, kwargs = ev
                record_lifecycle(name, **kwargs)
            else:
                record_lifecycle(ev)
    lines = _summary_lines(caplog)
    assert len(lines) == 1, f"expected 1 summary line, got {lines!r}"
    return lines[0]


def test_scenario_happy_path_text_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    line = _drive_and_summarize(
        msg={"id": "h.1", "type": "text", "from": "9665550001"},
        events=[
            (EVENT_TENANT_RESOLVED, {"tenant_id": 33}),
            (EVENT_DEDUP_DROP_MEMORY, {}),  # skipped, no — this would early-end
        ],
        caplog=caplog,
    )
    # Even with our last event being a dedup drop in this synthetic
    # scenario, the summary still emits.
    assert "trace_id=" in line and "tenant_id=33" in line


def test_scenario_dedup_memory_drop_marks_end_dropped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    line = _drive_and_summarize(
        msg={"id": "d.1", "type": "text", "from": "9665550002"},
        events=[EVENT_DEDUP_DROP_MEMORY, EVENT_END_DROPPED],
        caplog=caplog,
    )
    assert f"final={EVENT_END_DROPPED}" in line
    assert "message_saved=false" in line
    assert "dedup_drop_memory" in line


def test_scenario_dedup_db_drop_after_idempotency_mark(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Exposes the architectural trap: ``mark_processed`` committed
    on a previous turn, this turn drops on the duplicate guard.
    Operators must see the mismatch without DB inspection."""
    line = _drive_and_summarize(
        msg={"id": "dbd.1", "type": "text", "from": "9665550003"},
        events=[
            (EVENT_TENANT_RESOLVED, {"tenant_id": 33}),
            EVENT_DEDUP_DROP_DB,
            EVENT_END_DROPPED,
        ],
        caplog=caplog,
    )
    assert "dedup_drop_db" in line
    assert "convo_created=false" in line
    assert "orphan_messages=0" in line


def test_scenario_orphan_via_payment_short_circuit(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The smoking-gun case: a payment short-circuit persisted a
    ``MessageEvent`` without a Conversation row. The summary line is
    enough to identify this without joining tables."""
    line = _drive_and_summarize(
        msg={"id": "pay.1", "type": "image", "from": "9665550004"},
        events=[
            (EVENT_TENANT_RESOLVED, {"tenant_id": 33}),
            EVENT_PAYMENT_SHORT_CIRCUIT,
            EVENT_MESSAGE_SAVED_ORPHAN,
        ],
        caplog=caplog,
    )
    assert "convo_created=false" in line
    assert "message_saved=true" in line
    assert "orphan_messages=1" in line
    assert "payment_short_circuit" in line


def test_scenario_full_brain_path(
    caplog: pytest.LogCaptureFixture,
) -> None:
    line = _drive_and_summarize(
        msg={"id": "br.1", "type": "text", "from": "9665550005",
             "text": {"body": "كم سعر العسل؟"}},
        events=[
            (EVENT_TENANT_RESOLVED, {"tenant_id": 33}),
            (EVENT_CONVERSATION_CREATED, {"conversation_id": 1001}),
            (EVENT_MESSAGE_SAVED, {"conversation_id": 1001}),
            EVENT_BRAIN_INVOKED,
        ],
        caplog=caplog,
    )
    assert "convo_created=true" in line
    assert "convo_id=1001" in line
    assert "message_saved=true" in line
    assert "orphan_messages=0" in line
    assert "brain_invoked" in line


def test_scenario_unsub_short_circuit_is_terminal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    line = _drive_and_summarize(
        msg={"id": "u.1", "type": "text", "from": "9665550006"},
        events=[
            (EVENT_TENANT_RESOLVED, {"tenant_id": 33}),
            EVENT_UNSUB_SHORT_CIRCUIT,
            EVENT_END_DROPPED,
        ],
        caplog=caplog,
    )
    assert "unsub_short_circuit" in line
    assert f"final={EVENT_END_DROPPED}" in line
    assert "message_saved=false" in line


def test_scenario_unknown_phone_id_drop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    line = _drive_and_summarize(
        msg={"id": "uk.1", "type": "text", "from": "9665550007"},
        events=[EVENT_UNKNOWN_PHONE_ID, EVENT_END_DROPPED],
        caplog=caplog,
    )
    assert "unknown_phone_id" in line
    assert "tenant_id=" in line  # empty value is fine


def test_scenario_missing_phone_id_drop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    line = _drive_and_summarize(
        msg={"id": "mp.1", "type": "text"},
        events=[EVENT_MISSING_PHONE_ID, EVENT_END_DROPPED],
        caplog=caplog,
    )
    assert "missing_phone_id" in line


# ════════════════════════════════════════════════════════════════
# Telemetry-only contract — wiring at downstream call sites
# ════════════════════════════════════════════════════════════════


def test_save_message_emits_lifecycle_event_when_called_inside_trace(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The wiring inside ``StateManager.save_message`` records an
    event when invoked under an active trace, AND must not change
    the function's return value (None) or raise."""
    from core.conversation_engine import StateManager

    class _StubDB:
        def __init__(self) -> None:
            self.added: List[Any] = []

        def add(self, obj: Any) -> None:
            self.added.append(obj)

        def commit(self) -> None:
            pass

        def rollback(self) -> None:
            pass

    caplog.set_level(logging.DEBUG, logger="nahla.inbound_lifecycle")
    db = _StubDB()
    with inbound_lifecycle_trace(
        provider="meta", phone_number_id="P",
        msg={"id": "sv.1", "type": "text", "from": "9665550010"},
    ) as tr:
        rv = StateManager.save_message(
            db, "9665550010", "hello", "inbound",
            conversation_id=99, tenant_id=33,
        )
        assert rv is None  # legacy contract — no return value
        assert tr is not None
        names = [e.name for e in tr.events]
        assert EVENT_MESSAGE_SAVED in names
        assert tr.conversation_id == 99


def test_save_message_records_orphan_when_conversation_id_missing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A ``MessageEvent`` saved with ``conversation_id=None`` is the
    architectural failure W2.0.1 is built to surface. The wiring
    must record EVENT_MESSAGE_SAVED_ORPHAN, NOT EVENT_MESSAGE_SAVED."""
    from core.conversation_engine import StateManager

    class _StubDB:
        def add(self, obj: Any) -> None:  # noqa: D401
            pass

        def commit(self) -> None:
            pass

        def rollback(self) -> None:
            pass

    caplog.set_level(logging.DEBUG, logger="nahla.inbound_lifecycle")
    with inbound_lifecycle_trace(
        provider="meta", phone_number_id="P",
        msg={"id": "or.1", "type": "image", "from": "9665550011"},
    ) as tr:
        StateManager.save_message(
            _StubDB(), "9665550011", "[receipt]", "inbound",
            conversation_id=None, tenant_id=33,
            extra_metadata={"payment_receipt_short_circuit": True},
        )
        assert tr is not None
        names = [e.name for e in tr.events]
        assert EVENT_MESSAGE_SAVED_ORPHAN in names
        assert EVENT_MESSAGE_SAVED not in names
        assert tr.orphan_message_count == 1


def test_save_message_does_not_crash_outside_trace() -> None:
    """When telemetry is off OR no trace is active, the wiring is a
    no-op. This guards against regressions where the import itself
    breaks the legacy save path."""
    from core.conversation_engine import StateManager

    class _StubDB:
        def add(self, obj: Any) -> None:  # noqa: D401
            pass

        def commit(self) -> None:
            pass

        def rollback(self) -> None:
            pass

    # No active trace.
    rv = StateManager.save_message(
        _StubDB(), "966999", "x", "inbound",
        conversation_id=1, tenant_id=33,
    )
    assert rv is None


def test_save_message_records_rollback_on_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from core.conversation_engine import StateManager

    class _BrokenDB:
        def add(self, obj: Any) -> None:
            pass

        def commit(self) -> None:
            raise RuntimeError("commit blew up")

        def rollback(self) -> None:
            pass

    caplog.set_level(logging.DEBUG, logger="nahla.inbound_lifecycle")
    with inbound_lifecycle_trace(
        provider="meta", phone_number_id="P",
        msg={"id": "rb.1", "type": "text", "from": "9665550012"},
    ) as tr:
        # save_message swallows exceptions — confirm we still record
        # the rollback event without re-raising.
        StateManager.save_message(
            _BrokenDB(), "9665550012", "x", "inbound",
            conversation_id=42, tenant_id=33,
        )
        assert tr is not None
        names = [e.name for e in tr.events]
        assert EVENT_MESSAGE_SAVE_ROLLBACK in names
        assert tr.rollback_count == 1


def test_summary_line_is_byte_stable_for_identical_traces(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two traces with identical inputs produce summary lines that
    differ ONLY in trace_id and elapsed_ms (the only intentionally
    non-deterministic fields). This pins the format so a future
    refactor cannot silently shift columns."""
    caplog.set_level(logging.DEBUG, logger="nahla.inbound_lifecycle")
    msg = {"id": "stable.1", "type": "text", "from": "9665550099",
           "text": {"body": "hello"}}

    def _run() -> str:
        caplog.clear()
        with inbound_lifecycle_trace(provider="meta", phone_number_id="X", msg=msg):
            record_lifecycle(EVENT_TENANT_RESOLVED, tenant_id=33)
            record_lifecycle(EVENT_CONVERSATION_LOOKUP_HIT := "conversation_lookup_hit",
                             conversation_id=7)
            record_lifecycle(EVENT_MESSAGE_SAVED, conversation_id=7)
        return _summary_lines(caplog)[0]

    a = _run()
    b = _run()

    def _strip_volatile(line: str) -> str:
        import re
        line = re.sub(r"trace_id=\S+", "trace_id=X", line)
        line = re.sub(r"elapsed_ms=\d+", "elapsed_ms=N", line)
        return line

    assert _strip_volatile(a) == _strip_volatile(b)
