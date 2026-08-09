"""Observability-only turn latency instrumentation tests."""
from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from core.turn_latency import (
    ACCOUNTABLE_STAGES,
    TurnLatency,
    bind_turn_latency,
    get_turn_latency,
    merge_turn_latency_into_metadata,
    new_turn_latency,
    refresh_turn_latency_on_outbound_message,
    reset_turn_latency,
    safe_record_accountable_once,
    safe_record_llm_call,
    safe_record_lock,
    safe_record_ms,
    safe_span,
)


def test_timing_fields_emitted_on_normal_turn() -> None:
    timing = new_turn_latency(tenant_id=1, conversation_id=9, message_id="wamid-a")
    token = bind_turn_latency(timing)
    try:
        with safe_span("inbound_persist"):
            time.sleep(0.001)
        with safe_span("state_load"):
            time.sleep(0.001)
        with safe_span("decision"):
            time.sleep(0.001)
        with safe_span("persona_compose"):
            time.sleep(0.001)
        safe_record_ms("provider_send", 12)
        snap = timing.snapshot()
    finally:
        reset_turn_latency(token)

    assert snap["tenant_id"] == 1
    assert snap["conversation_id"] == 9
    assert snap["turn_id"]
    assert snap["total_turn_ms"] >= 0
    assert "inbound_persist" in snap["spans_ms"]
    assert "state_load" in snap["spans_ms"]
    assert "decision" in snap["spans_ms"]
    assert "persona_compose" in snap["spans_ms"]
    assert snap["spans_ms"]["provider_send"] == 12
    assert "accounted_ms" in snap
    assert "unaccounted_ms" in snap
    assert "accounted_percent" in snap
    assert "prompt" not in snap
    assert "body" not in snap
    assert "system" not in snap


def test_catalog_search_detail_does_not_double_count_accounted() -> None:
    timing = new_turn_latency(tenant_id=7)
    timing.record_ms("tool_execution", 100)
    timing.record_ms("catalog_search", 80)  # detail
    timing.record_ms("persona_compose", 50)
    timing.record_ms("total_turn", 400)
    snap = timing.snapshot(finalize_total=False)
    assert snap["spans_ms"]["catalog_search"] == 80
    assert snap["accounted_ms"] == 150  # tool + persona only
    assert "catalog_search" not in ACCOUNTABLE_STAGES


def test_llm_timeout_fallback_fields_recordable() -> None:
    timing = new_turn_latency(tenant_id=1)
    token = bind_turn_latency(timing)
    try:
        safe_record_ms("persona_compose", 8000)
        safe_record_llm_call(
            purpose="persona_compose",
            model="gpt-5.6-luna",
            provider="openai_compatible",
            duration_ms=8000,
            timeout_seconds=8.0,
            fallback_reason="timeout",
            ttft_available=False,
            input_tokens=100,
            output_tokens=0,
        )
        snap = timing.snapshot()
    finally:
        reset_turn_latency(token)

    assert snap["spans_ms"]["persona_compose"] == 8000
    assert len(snap["llm_calls"]) == 1
    call = snap["llm_calls"][0]
    assert call["fallback_reason"] == "timeout"
    assert call["ttft_available"] is False
    assert call["first_token_ms"] is None
    assert call["timeout_seconds"] == 8.0
    assert call["input_tokens"] == 100


def test_lock_wait_vs_hold_separated() -> None:
    timing = new_turn_latency(tenant_id=1)
    token = bind_turn_latency(timing)
    try:
        safe_record_lock(wait_ms=250, hold_ms=4000, waiters_ahead=2)
        snap = timing.snapshot()
    finally:
        reset_turn_latency(token)

    assert snap["lock_wait_ms"] == 250
    assert snap["lock_hold_ms"] == 4000
    assert snap["waiters_ahead"] == 2
    assert snap["spans_ms"]["conversation_lock_wait"] == 250
    assert snap["spans_ms"]["conversation_lock_hold"] == 4000
    # Hold is detail-only; wait is accountable.
    assert snap["accounted_ms"] == 250


def test_provider_send_measured_independently() -> None:
    timing = new_turn_latency(tenant_id=1)
    timing.record_ms("persona_compose", 1000)
    timing.record_ms("provider_send", 551)
    snap = timing.snapshot()
    assert snap["spans_ms"]["provider_send"] == 551
    assert snap["spans_ms"]["persona_compose"] == 1000


def test_telemetry_failure_does_not_fail_customer_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    timing = new_turn_latency(tenant_id=1)
    token = bind_turn_latency(timing)

    def _boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("telemetry boom")

    monkeypatch.setattr(timing, "record_ms", _boom)
    try:
        # Must not raise to caller.
        safe_record_ms("decision", 10)
        with safe_span("facts_load"):
            pass
        safe_record_lock(wait_ms=1, hold_ms=2, waiters_ahead=0)
        safe_record_llm_call(purpose="x", model="m", ttft_available=False)
        merge_turn_latency_into_metadata({}, timing)
        timing.emit_log()
    finally:
        reset_turn_latency(token)


def test_nested_spans_do_not_double_count_accounted() -> None:
    timing = new_turn_latency(tenant_id=1)
    timing.record_ms("tool_execution", 200)
    timing.record_ms("catalog_search", 150)
    timing.record_ms("conversation_lock_hold", 5000)
    timing.record_ms("facts_db", 40)
    timing.record_ms("state_load", 30)
    timing.record_ms("total_turn", 6000)
    snap = timing.snapshot(finalize_total=True, cache=False)
    # Only accountable stages: tool_execution + state_load
    assert snap["accounted_ms"] == 230
    assert snap["unaccounted_ms"] == 6000 - 230


def test_tenant_isolation_between_instances() -> None:
    a = new_turn_latency(tenant_id=1, conversation_id=9)
    b = new_turn_latency(tenant_id=2, conversation_id=99)
    a.record_ms("persona_compose", 111)
    b.record_ms("persona_compose", 222)
    assert a.snapshot()["tenant_id"] == 1
    assert b.snapshot()["tenant_id"] == 2
    assert a.spans_ms["persona_compose"] == 111
    assert b.spans_ms["persona_compose"] == 222


def test_snapshot_has_no_prompt_or_body_secrets() -> None:
    timing = new_turn_latency(tenant_id=1, message_id="wamid")
    timing.record_llm_call(
        purpose="persona_compose",
        model="gpt-5.6-luna",
        provider="openai_compatible",
        duration_ms=10,
        ttft_available=False,
        input_tokens=5,
        output_tokens=3,
    )
    snap = timing.snapshot()
    blob = str(snap)
    for forbidden in ("system_prompt", "user_content", "raw_prompt", "OPENAI_API_KEY"):
        assert forbidden not in blob


def test_negligible_instrumentation_overhead() -> None:
    timing = new_turn_latency(tenant_id=1)
    t0 = time.perf_counter()
    for i in range(20):
        timing.record_ms(f"state_load", 1)
        timing.record_ms("decision", 1)
    snap = timing.snapshot()
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    assert elapsed_ms < 50.0
    assert snap["total_turn_ms"] >= 0


def test_conversation_lock_records_into_contextvar() -> None:
    from core.conversation_lock import conversation_lock

    async def _run() -> None:
        timing = new_turn_latency(tenant_id=42, message_id="m1")
        token = bind_turn_latency(timing)
        try:
            async with conversation_lock(42, "966555906901", msg_id="m1", text_snippet="hi"):
                await asyncio.sleep(0.005)
            assert timing.lock_wait_ms is not None
            assert timing.lock_hold_ms is not None
            assert timing.lock_hold_ms >= 0
            assert timing.waiters_ahead == 0
        finally:
            reset_turn_latency(token)

    asyncio.run(_run())


def test_ttft_not_invented_when_unavailable() -> None:
    timing = new_turn_latency(tenant_id=1)
    timing.record_llm_call(
        purpose="persona_compose",
        model="gpt-5.6-luna",
        first_token_ms=123,  # would-be fake
        ttft_available=False,
        duration_ms=500,
    )
    call = timing.snapshot()["llm_calls"][0]
    assert call["ttft_available"] is False
    assert call["first_token_ms"] is None


def test_merge_metadata_uses_fresh_snapshot_not_stale_cache() -> None:
    timing = new_turn_latency(tenant_id=3, conversation_id=5)
    timing.record_ms("provider_send", 9)
    timing.snapshot(finalize_total=False, cache=True)
    timing.record_ms("outbound_persist", 21)
    meta: dict[str, Any] = {}
    merge_turn_latency_into_metadata(meta, timing)
    assert isinstance(meta["turn_timing"], dict)
    assert meta["turn_timing"]["tenant_id"] == 3
    assert meta["turn_timing"]["spans_ms"]["provider_send"] == 9
    assert meta["turn_timing"]["spans_ms"]["outbound_persist"] == 21
    assert meta["turn_timing"]["snapshot_finalized"] is False


def test_refresh_outbound_message_includes_provider_send() -> None:
    timing = new_turn_latency(tenant_id=11, conversation_id=3)
    timing.record_ms("persona_compose", 400)
    timing.record_ms("outbound_persist", 30)
    timing.record_ms("provider_send", 120)
    row = MagicMock()
    row.extra_metadata = {"phone": "966500000001"}
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = row

    ok = refresh_turn_latency_on_outbound_message(db, timing, message_event_id=99)

    assert ok is True
    snap = row.extra_metadata["turn_timing"]
    assert snap["spans_ms"]["provider_send"] == 120
    assert snap["snapshot_finalized"] is True
    assert snap["end_to_end_total_ms"] == snap["total_turn_ms"]


def test_refresh_outbound_failure_still_finalizes_snapshot() -> None:
    timing = new_turn_latency(tenant_id=2)
    timing.record_ms("outbound_persist", 18)
    timing.record_ms("provider_send", 250)
    row = MagicMock()
    row.extra_metadata = {
        "provider_send": {
            "status": "failed",
            "classification": "provider_error_field",
            "duration_ms": 250,
        },
    }
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = row

    ok = refresh_turn_latency_on_outbound_message(db, timing, message_event_id=7)

    assert ok is True
    snap = row.extra_metadata["turn_timing"]
    assert snap["spans_ms"]["provider_send"] == 250
    assert snap["snapshot_finalized"] is True
    assert row.extra_metadata["provider_send"]["status"] == "failed"


def test_brain_mid_snapshot_distinguishable_from_final() -> None:
    timing = new_turn_latency(tenant_id=1)
    timing.record_ms("persona_compose", 500)
    time.sleep(0.005)
    timing.mark_brain_boundary()
    mid = timing.snapshot(finalize_total=False, cache=False)
    timing.record_ms("post_brain_dispatch", 300)
    timing.record_ms("outbound_persist", 40)
    timing.record_ms("provider_send", 60)
    final = timing.snapshot(finalize_total=True, cache=False)

    assert mid["brain_total_ms"] >= 5
    assert mid["snapshot_finalized"] is False
    assert final["end_to_end_total_ms"] >= mid["brain_total_ms"]
    assert final["spans_ms"]["post_brain_dispatch"] == 300


def test_end_to_end_total_includes_post_brain_dispatch() -> None:
    timing = new_turn_latency(tenant_id=4)
    timing.mark_brain_boundary()
    timing.record_ms("post_brain_dispatch", 800)
    timing.record_ms("outbound_persist", 50)
    timing.record_ms("provider_send", 150)
    snap = timing.snapshot(finalize_total=True, cache=False)
    assert snap["spans_ms"]["post_brain_dispatch"] == 800
    assert snap["accounted_ms"] >= 1000


def test_guards_not_double_counted_in_accounted_ms() -> None:
    timing = new_turn_latency(tenant_id=1)
    timing.record_ms("guards", 200)
    timing.record_accountable_once("guards", 150)
    snap = timing.snapshot(finalize_total=False, cache=False)
    assert snap["spans_ms"]["guards"] == 200
    assert snap["accounted_ms"] == 200


def test_safe_record_accountable_once_skips_second_pipeline_guard() -> None:
    timing = new_turn_latency(tenant_id=1)
    token = bind_turn_latency(timing)
    try:
        safe_record_ms("guards", 180)
        safe_record_accountable_once("guards", 90)
        snap = timing.snapshot(finalize_total=False, cache=False)
    finally:
        reset_turn_latency(token)
    assert snap["spans_ms"]["guards"] == 180


def test_detail_spans_excluded_from_accounted_ms() -> None:
    timing = new_turn_latency(tenant_id=1)
    timing.record_ms("facts_load", 30)
    timing.record_ms("facts_db", 40)
    timing.record_ms("catalog_search", 80)
    timing.record_ms("conversation_lock_hold", 5000)
    snap = timing.snapshot(finalize_total=False, cache=False)
    assert snap["accounted_ms"] == 30
    assert "facts_db" not in ACCOUNTABLE_STAGES
    assert "catalog_search" not in ACCOUNTABLE_STAGES


def test_refresh_fail_open_does_not_raise() -> None:
    timing = new_turn_latency(tenant_id=1)
    timing.record_ms("provider_send", 10)
    refresh_turn_latency_on_outbound_message(None, timing, message_event_id=1)
    refresh_turn_latency_on_outbound_message(MagicMock(), None, message_event_id=1)


def test_snapshot_refresh_overhead_smoke() -> None:
    timing = new_turn_latency(tenant_id=1)
    for _ in range(10):
        timing.record_ms("state_load", 1)
        timing.record_ms("provider_send", 2)
    t0 = time.perf_counter()
    for _ in range(20):
        timing.snapshot(finalize_total=True, cache=False)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    assert elapsed_ms < 100.0
