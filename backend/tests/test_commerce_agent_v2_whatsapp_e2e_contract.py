"""Contract tests for the independent real WhatsApp acceptance harness."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from core.conversation_lock import conversation_lock
from evals.commerce_agent_v2_whatsapp.scorer import score_batch
from services.commerce_v2_whatsapp_e2e_contract import (
    READ_ONLY_TOOLS,
    load_corpus,
    render_controlled_test_data,
    validate_test_owned_accounts,
)


CORPUS_PATH = (
    Path(__file__).parents[1]
    / "evals"
    / "commerce_agent_v2_whatsapp"
    / "corpus_v1.json"
)


def _corpus(seed: int = 260914):
    return load_corpus(CORPUS_PATH, seed=seed)


def test_corpus_has_180_randomized_read_only_turns_with_a_b_c_split() -> None:
    corpus = _corpus()
    assert len(corpus) == 180
    assert {alias: sum(turn.account_alias == alias for turn in corpus) for alias in "ABC"} == {
        "A": 60,
        "B": 60,
        "C": 60,
    }
    assert {tier: sum(turn.requested_service_tier == tier for turn in corpus) for tier in ("auto", "fast")} == {
        "auto": 90,
        "fast": 90,
    }
    assert all(set(turn.expected_tools).issubset(READ_ONLY_TOOLS) for turn in corpus)
    assert [turn.inbound_text for turn in corpus] != [
        turn.inbound_text for turn in _corpus(seed=260915)
    ]


def test_multi_turn_sequences_remain_ordered_within_each_account() -> None:
    for alias in "ABC":
        positions: dict[str, list[int]] = {}
        for turn in _corpus():
            if turn.account_alias == alias:
                positions.setdefault(turn.sequence_id, []).append(turn.sequence_position)
        assert all(values == sorted(values) for values in positions.values())


def test_test_accounts_are_distinct_and_never_persisted_in_corpus() -> None:
    fingerprints = validate_test_owned_accounts(
        {"A": "+966500000001", "B": "+966500000002", "C": "+966500000003"}
    )
    assert fingerprints == {"A": "confirmed:12digits", "B": "confirmed:12digits", "C": "confirmed:12digits"}
    with pytest.raises(ValueError, match="distinct"):
        validate_test_owned_accounts(
            {"A": "+966500000001", "B": "+966500000001", "C": "+966500000003"}
        )


def test_controlled_order_placeholder_is_rendered_only_at_execution_time() -> None:
    raw = _corpus()
    assert any("{TEST_ORDER_NUMBER}" in turn.inbound_text for turn in raw)
    rendered = render_controlled_test_data(raw, test_order_number="TEST-123")
    assert all("{TEST_ORDER_NUMBER}" not in turn.inbound_text for turn in rendered)


@pytest.mark.asyncio
async def test_same_conversation_serializes_but_different_customers_can_overlap() -> None:
    timeline: list[tuple[str, str]] = []
    first_entered = asyncio.Event()

    async def same_customer(label: str) -> None:
        async with conversation_lock(1, "+966 500 000 001", msg_id=label):
            timeline.append((label, "start"))
            if label == "first":
                first_entered.set()
                await asyncio.sleep(0.02)
            timeline.append((label, "end"))

    first = asyncio.create_task(same_customer("first"))
    await first_entered.wait()
    second = asyncio.create_task(same_customer("second"))
    await asyncio.gather(first, second)
    assert timeline == [
        ("first", "start"),
        ("first", "end"),
        ("second", "start"),
        ("second", "end"),
    ]

    active = 0
    peak = 0
    both_started = asyncio.Event()

    async def different_customer(phone: str) -> None:
        nonlocal active, peak
        async with conversation_lock(1, phone):
            active += 1
            peak = max(peak, active)
            if active == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=0.2)
            active -= 1

    await asyncio.gather(
        different_customer("+966500000001"),
        different_customer("+966500000002"),
    )
    assert peak == 2


def test_scorer_passes_complete_evidence_and_fails_closed_on_leakage() -> None:
    corpus = [turn.to_mapping() for turn in _corpus()]
    evidence = []
    for turn in corpus:
        evidence.append(
            {
                "case_id": turn["case_id"],
                "tenant_id": 1,
                "owner": "commerce_agent_v2",
                "v1_bypassed": True,
                "status": "completed",
                "inbound_wamid": f"in-{turn['case_id']}",
                "outbound_wamids": [f"out-{turn['case_id']}"],
                "trace_id": f"trace-{turn['case_id']}",
                "guardrail_passed": True,
                "tool_calls": turn["expected_tools"],
                "fallback_type": (
                    "expected_safe_fallback"
                    if turn["expected_outcome"] == "safe_missing_fact"
                    else "none"
                ),
                "requested_service_tier": turn["requested_service_tier"],
                **{key: 0 for key in (
                    "unsupported_commercial_claims",
                    "cross_tenant_leakage",
                    "cross_customer_leakage",
                    "duplicate_replies",
                    "silent_v1_fallback",
                    "write_mutations",
                    "salla_mutations",
                )},
            }
        )
    report = score_batch(corpus, evidence)
    assert report["hard_gates_passed"] is True
    assert report["turns_passed"] == 180

    evidence[0]["cross_customer_leakage"] = 1
    failed = score_batch(corpus, evidence)
    assert failed["hard_gates_passed"] is False
    assert failed["material_failure"] is True

    evidence[0]["cross_customer_leakage"] = 0
    duplicate = [*evidence[:-1], evidence[0]]
    duplicate_report = score_batch(corpus, duplicate)
    assert duplicate_report["hard_gates_passed"] is False
    assert duplicate_report["duplicate_evidence_case_ids"]
