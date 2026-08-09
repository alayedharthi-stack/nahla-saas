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
