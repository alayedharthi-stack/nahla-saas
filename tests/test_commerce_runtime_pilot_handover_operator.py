"""The handover check: it says "safe to stop" only against evidence.

Switching the pilot off is a rollback only when the runtime has nothing left in
flight. These cases hold the operator job to that: it reads the pilot's own
allowlist and nothing wider, it counts the three ways work can be abandoned, it
refuses to answer at all when it cannot read the database, and it exits 0 only
when every counted tenant has nothing outstanding. No database and no network.
"""
from __future__ import annotations

from typing import Any, Dict, List

import pytest

from core.commerce_runtime import pilot_guard as pg
from core.commerce_runtime import recovery
from scripts.operators import commerce_runtime_pilot_handover as job


def state(tenant_id: int, *, open_turns: int = 0, reserved: int = 0,
          unresolved: int = 0) -> recovery.HandoverState:
    return recovery.HandoverState(tenant_id=tenant_id, open_turns=open_turns,
                                  reserved_undispatched=reserved,
                                  unresolved_attempts=unresolved)


@pytest.fixture()
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(pg.ENV_ENABLED, "true")
    monkeypatch.setenv(pg.ENV_DRAINING, "true")
    monkeypatch.setenv(pg.ENV_TENANT_ALLOWLIST, "4242")


def observing(monkeypatch: pytest.MonkeyPatch, states: Any) -> List[List[int]]:
    asked: List[List[int]] = []

    def _observe(tenants: Any) -> Any:
        asked.append(list(tenants))
        if isinstance(states, BaseException):
            raise states
        return tuple(states)

    monkeypatch.setattr(job, "observe", _observe)
    return asked


# ── What the configuration says ──────────────────────────────────────────────


@pytest.mark.parametrize("env, expected", [
    ({}, "off"),
    ({pg.ENV_ENABLED: "false"}, "off"),
    ({pg.ENV_ENABLED: "true"}, "on"),
    ({pg.ENV_ENABLED: "true", pg.ENV_DRAINING: "true"}, "draining"),
    ({pg.ENV_DRAINING: "true"}, "off"),          # draining alone is not enabled
])
def test_the_three_modes_are_read_from_the_two_flags(env: Dict[str, str], expected: str):
    assert job.mode(env) == expected


def test_the_tenants_checked_are_the_pilot_s_own_allowlist():
    assert job.configured_tenants({pg.ENV_TENANT_ALLOWLIST: "7, 4242, abc, -1, 0, 7"}) == [7, 4242]


def test_without_an_allowlist_the_job_refuses_rather_than_reporting_nothing_to_do(monkeypatch,
                                                                                  capsys):
    monkeypatch.delenv(pg.ENV_TENANT_ALLOWLIST, raising=False)
    assert job.main([]) == job.EXIT_USAGE
    assert f"RESULT={job.RESULT_FAILED_PRECONDITION}" in capsys.readouterr().out


# ── The verdict ──────────────────────────────────────────────────────────────


def test_a_tenant_with_nothing_in_flight_is_safe_to_stop(configured, monkeypatch, capsys):
    observing(monkeypatch, [state(4242)])
    assert job.main([]) == job.EXIT_SETTLED
    out = capsys.readouterr().out
    assert f"RESULT={job.RESULT_SETTLED}" in out
    assert "COMMERCE_RUNTIME_PILOT_ENABLED=false" in out


@pytest.mark.parametrize("counts", [
    {"open_turns": 1},
    {"reserved": 1},
    {"unresolved": 1},
])
def test_any_one_kind_of_work_in_flight_is_not_safe_to_stop(configured, monkeypatch, capsys,
                                                            counts):
    observing(monkeypatch, [state(4242, **counts)])
    assert job.main([]) == job.EXIT_IN_FLIGHT
    assert f"RESULT={job.RESULT_IN_FLIGHT}" in capsys.readouterr().out


def test_one_tenant_still_holding_work_keeps_the_whole_answer_no(configured, monkeypatch):
    monkeypatch.setenv(pg.ENV_TENANT_ALLOWLIST, "7,4242")
    observing(monkeypatch, [state(7), state(4242, open_turns=2)])
    assert job.main([]) == job.EXIT_IN_FLIGHT


def test_a_database_that_cannot_be_read_is_never_reported_as_settled(configured, monkeypatch,
                                                                     capsys):
    observing(monkeypatch, RuntimeError("database unavailable"))
    assert job.main([]) == job.EXIT_FAILED
    assert f"RESULT={job.RESULT_FAILED}" in capsys.readouterr().out


def test_the_counts_reported_are_the_tenant_s_own(configured, monkeypatch, capsys):
    observing(monkeypatch, [state(4242, open_turns=3, reserved=2, unresolved=1)])
    job.main([])
    out = capsys.readouterr().out
    assert "open_turns=3" in out and "reserved_undispatched=2" in out
    assert "unresolved_attempts=1" in out and "settled=False" in out


def test_only_the_allowlisted_tenants_are_ever_inspected(configured, monkeypatch):
    asked = observing(monkeypatch, [state(4242)])
    job.main([])
    assert asked == [[4242]]


def test_a_pilot_still_taking_new_turns_is_told_to_drain_first(monkeypatch, capsys):
    monkeypatch.setenv(pg.ENV_ENABLED, "true")
    monkeypatch.delenv(pg.ENV_DRAINING, raising=False)
    monkeypatch.setenv(pg.ENV_TENANT_ALLOWLIST, "4242")
    observing(monkeypatch, [state(4242)])
    job.main([])
    out = capsys.readouterr().out
    assert "mode=on" in out and "COMMERCE_RUNTIME_PILOT_DRAINING=true" in out


def test_every_line_the_job_prints_is_grep_able_under_one_prefix(configured, monkeypatch,
                                                                 capsys):
    observing(monkeypatch, [state(4242, open_turns=1)])
    job.main([])
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert lines and all(line.startswith(job.LOG_PREFIX) for line in lines)


# ── The state object itself ──────────────────────────────────────────────────


def test_settled_means_all_three_counts_are_zero():
    assert state(1).settled is True
    assert state(1, open_turns=1).settled is False
    assert state(1, reserved=1).settled is False
    assert state(1, unresolved=1).settled is False


def test_an_empty_set_of_tenants_is_never_settled():
    """Nothing observed is not the same as nothing outstanding."""
    assert recovery.handover_settled(()) is False
    assert recovery.handover_settled((state(1),)) is True
