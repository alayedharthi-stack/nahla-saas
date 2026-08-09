from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest

from modules.ai.brain.memory.updater import (
    DefaultMemoryUpdater,
    SUMMARISE_EVERY_N,
    build_summarise_payload,
    build_summary_prompt,
    run_deferred_memory_summarise,
    schedule_deferred_memory_summarise,
    should_apply_summary_source_turn,
)
from modules.ai.brain.types import (
    ActionResult,
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
)


def _ctx(*, turn: int = 1, customer_id: int = 42) -> BrainContext:
    return BrainContext(
        tenant_id=7,
        customer_phone="966500000001",
        message="مرحبا",
        intent=Intent(name="greeting", confidence=0.9),
        state=MerchantConversationState(turn=turn),
        facts=CommerceFacts(),
        history=[
            {"direction": "in", "body": "أبي حذاء رياضي"},
            {"direction": "out", "body": "أكيد، أي مقاس؟"},
        ],
        customer_id=customer_id,
    )


def _noop_updater_side_effects(updater: DefaultMemoryUpdater) -> None:
    updater._write_trace = MagicMock()  # type: ignore[method-assign]
    updater._bump_affinity = MagicMock()  # type: ignore[method-assign]
    updater._nudge_price_sensitivity = MagicMock()  # type: ignore[method-assign]
    updater._emit_sales_events = MagicMock()  # type: ignore[method-assign]
    updater._emit_anonymous_signal = MagicMock()  # type: ignore[method-assign]


def test_normal_turn_no_summary_payload() -> None:
    updater = DefaultMemoryUpdater()
    _noop_updater_side_effects(updater)
    result = ActionResult(success=True, data={})
    ctx = _ctx(turn=4)

    with patch.object(updater, "_summarise") as mock_sum:
        updater.update(MagicMock(), ctx, Decision(action="greet"), result, "أهلا", "discovery", 10)

    assert "memory_summarise_deferred" not in result.data
    mock_sum.assert_not_called()


def test_fifth_turn_defers_and_does_not_call_sync_summarise() -> None:
    updater = DefaultMemoryUpdater()
    _noop_updater_side_effects(updater)
    result = ActionResult(success=True, data={})
    ctx = _ctx(turn=SUMMARISE_EVERY_N)

    with patch.object(updater, "_summarise") as mock_sum:
        updater.update(MagicMock(), ctx, Decision(action="greet"), result, "أهلا", "discovery", 10)

    mock_sum.assert_not_called()
    payload = result.data.get("memory_summarise_deferred")
    assert isinstance(payload, dict)
    assert payload["tenant_id"] == 7
    assert payload["customer_id"] == 42
    assert payload["turn"] == SUMMARISE_EVERY_N


def test_fifth_turn_marks_deferred_telemetry() -> None:
    from core.turn_latency import (
        bind_turn_latency,
        new_turn_latency,
        reset_turn_latency,
        safe_set_memory_update_mode,
    )

    updater = DefaultMemoryUpdater()
    _noop_updater_side_effects(updater)
    timing = new_turn_latency(tenant_id=7)
    token = bind_turn_latency(timing)
    try:
        result = ActionResult(success=True, data={})
        ctx = _ctx(turn=SUMMARISE_EVERY_N)
        updater.update(MagicMock(), ctx, Decision(action="greet"), result, "أهلا", "discovery", 10)
        snap = timing.snapshot(finalize_total=False, cache=False)
    finally:
        reset_turn_latency(token)

    assert snap["memory_update_mode"] == "summarise_deferred"
    assert snap.get("memory_summarise_deferred_scheduled") is True
    assert snap.get("memory_summary_llm_ms") is None
    safe_set_memory_update_mode("summarise_deferred")
    timing.record_ms("memory_update", 12)
    snap2 = timing.snapshot(finalize_total=False, cache=False)
    assert snap2["accounted_ms"] == 12


def test_slow_llm_deferred_runner_does_not_block_update_path() -> None:
    updater = DefaultMemoryUpdater()
    _noop_updater_side_effects(updater)
    result = ActionResult(success=True, data={})
    ctx = _ctx(turn=SUMMARISE_EVERY_N)

    t0 = time.monotonic()
    updater.update(MagicMock(), ctx, Decision(action="greet"), result, "أهلا", "discovery", 10)
    elapsed = time.monotonic() - t0
    assert elapsed < 0.5

    payload = result.data["memory_summarise_deferred"]

    def _slow_llm(_payload):
        time.sleep(0.2)
        return {"summary": "ملخص"}, 200

    async def _run() -> None:
        with patch(
            "modules.ai.brain.memory.updater._call_summary_llm",
            side_effect=_slow_llm,
        ), patch(
            "modules.ai.brain.memory.updater._write_summary_atomic",
            return_value=5,
        ):
            await run_deferred_memory_summarise(payload)

    asyncio.run(_run())


def test_deferred_summary_failure_is_fail_open() -> None:
    payload = build_summarise_payload(_ctx(turn=5))
    assert payload is not None

    async def _run() -> None:
        with patch(
            "modules.ai.brain.memory.updater._call_summary_llm",
            side_effect=RuntimeError("llm down"),
        ):
            await run_deferred_memory_summarise(payload)

    asyncio.run(_run())


def test_deferred_runner_does_not_touch_conversation_lock() -> None:
    import inspect

    from modules.ai.brain import memory

    source = inspect.getsource(memory.updater.run_deferred_memory_summarise)
    assert "from core.conversation_lock" not in source
    assert "conversation_lock." not in source
    assert "with conversation_lock" not in source


def test_should_apply_summary_source_turn_newer_wins() -> None:
    assert should_apply_summary_source_turn(None, 5) is True
    assert should_apply_summary_source_turn(4, 5) is True
    assert should_apply_summary_source_turn(5, 5) is False
    assert should_apply_summary_source_turn(6, 5) is False


def test_write_summary_sql_includes_tenant_isolation() -> None:
    import inspect

    from modules.ai.brain.memory import updater

    source = inspect.getsource(updater._write_summary_atomic)
    assert "tenant_id = EXCLUDED.tenant_id" in source
    assert "summary_source_turn" in source
    assert "conversation_history_summaries.tenant_id = EXCLUDED.tenant_id" in source


def _simulate_summary_upsert(
    store: dict[int, dict],
    *,
    tenant_id: int,
    customer_id: int,
    turn: int,
    summary_text: str,
) -> bool:
    """Mirror _write_summary_atomic newer-wins + tenant WHERE semantics."""
    existing = store.get(customer_id)
    if existing is None:
        store[customer_id] = {
            "tenant_id": tenant_id,
            "summary_source_turn": turn,
            "summary_text": summary_text,
        }
        return True
    if int(existing["tenant_id"]) != int(tenant_id):
        return False
    if not should_apply_summary_source_turn(existing.get("summary_source_turn"), turn):
        return False
    store[customer_id] = {
        "tenant_id": tenant_id,
        "summary_source_turn": turn,
        "summary_text": summary_text,
    }
    return True


def test_multi_tenant_summary_isolation_and_newer_wins() -> None:
    """Tenant A/B stay isolated; older deferred write cannot overwrite newer."""
    store: dict[int, dict] = {}
    # Same-shaped customer identifiers across tenants (globally distinct PKs).
    customer_a = 9001
    customer_b = 9001 + 1  # similar identifier, different tenant row

    assert _simulate_summary_upsert(
        store, tenant_id=101, customer_id=customer_a, turn=10, summary_text="A10"
    )
    assert _simulate_summary_upsert(
        store, tenant_id=202, customer_id=customer_b, turn=11, summary_text="B11"
    )
    assert store[customer_a]["summary_text"] == "A10"
    assert store[customer_b]["summary_text"] == "B11"

    # Older A must not overwrite newer A.
    assert _simulate_summary_upsert(
        store, tenant_id=101, customer_id=customer_a, turn=15, summary_text="A15"
    )
    assert not _simulate_summary_upsert(
        store, tenant_id=101, customer_id=customer_a, turn=10, summary_text="A10-stale"
    )
    assert store[customer_a]["summary_text"] == "A15"
    assert store[customer_a]["summary_source_turn"] == 15

    # A must never overwrite B (tenant mismatch on same customer_id key).
    assert not _simulate_summary_upsert(
        store, tenant_id=101, customer_id=customer_b, turn=99, summary_text="A-hijack-B"
    )
    assert store[customer_b]["summary_text"] == "B11"
    assert store[customer_b]["tenant_id"] == 202

    # B must never overwrite A.
    assert not _simulate_summary_upsert(
        store, tenant_id=202, customer_id=customer_a, turn=99, summary_text="B-hijack-A"
    )
    assert store[customer_a]["summary_text"] == "A15"
    assert store[customer_a]["tenant_id"] == 101

    # Payload contract always carries tenant_id (immutable capture).
    payload_a = build_summarise_payload(_ctx(turn=10, customer_id=customer_a))
    assert payload_a is not None
    assert payload_a["tenant_id"] == 7
    assert payload_a["customer_id"] == customer_a
    assert "history_lines" in payload_a
    assert set(payload_a.keys()) >= {
        "tenant_id",
        "customer_id",
        "turn",
        "stage",
        "history_lines",
        "message_id",
    }


def test_write_summary_atomic_binds_tenant_scoped_params() -> None:
    captured: dict = {}

    class _DB:
        def execute(self, _stmt, params):
            captured.update(params)

        def commit(self):
            return None

    from modules.ai.brain.memory.updater import _write_summary_atomic

    _write_summary_atomic(
        _DB(),
        {
            "tenant_id": 303,
            "customer_id": 44001,
            "turn": 20,
            "stage": "checkout",
        },
        {"summary": "ملخص عام", "last_intent": "order", "sentiment": "neutral"},
    )
    assert captured["tenant_id"] == 303
    assert captured["customer_id"] == 44001
    assert captured["summary_source_turn"] == 20


def test_schedule_uses_spawn_background() -> None:
    payload = build_summarise_payload(_ctx(turn=5))
    assert payload is not None
    with patch("core.runtime_perf.spawn_background") as mock_spawn:
        ok = schedule_deferred_memory_summarise(payload, request_id="rid-1")
    assert ok is True
    mock_spawn.assert_called_once()
    assert mock_spawn.call_args.kwargs["name"] == "memory_summarise"
    assert mock_spawn.call_args.kwargs["request_id"] == "rid-1"


def test_summary_prompt_fragment_unchanged() -> None:
    prompt = build_summary_prompt(["عميل: أبي عطر ورد"])
    assert "لخّص هذه المحادثة بين عميل ومساعد متجر إلكتروني" in prompt
    assert '"last_intent": "browse|order|complaint|inquiry"' in prompt
    assert '"sentiment": "positive|neutral|negative|frustrated"' in prompt


def test_memory_update_span_excludes_llm_on_deferred_turn() -> None:
    from core.turn_latency import bind_turn_latency, new_turn_latency, reset_turn_latency

    updater = DefaultMemoryUpdater()
    _noop_updater_side_effects(updater)
    timing = new_turn_latency(tenant_id=7)
    token = bind_turn_latency(timing)
    try:
        result = ActionResult(success=True, data={})
        ctx = _ctx(turn=SUMMARISE_EVERY_N)
        with patch(
            "modules.ai.brain.memory.updater._call_summary_llm",
            side_effect=lambda _p: ({"summary": "x"}, 999),
        ):
            updater.update(MagicMock(), ctx, Decision(action="greet"), result, "أهلا", "discovery", 10)
        timing.record_ms("memory_update", 18)
        snap = timing.snapshot(finalize_total=False, cache=False)
    finally:
        reset_turn_latency(token)

    assert snap["memory_update_mode"] == "summarise_deferred"
    assert snap.get("memory_summary_llm_ms") is None
    assert snap["spans_ms"].get("memory_summary_llm", 0) == 0
    assert snap["accounted_ms"] == 18
