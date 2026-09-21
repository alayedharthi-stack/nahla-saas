"""The trial evidence report: what the rows prove, and what they refuse to.

Offline. Every case here is the report's judgement over structures shaped like
the runtime's own rows, so the rules are exercised without a database and
without a merchant: the job reads identifiers and outcomes, never catalogue or
prose, so it is the same job for every store on the platform.
"""
from __future__ import annotations

import datetime as dt
import os
import sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
for _path in (os.path.join(_ROOT, "backend"), _ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from scripts.operators import commerce_runtime_trial_evidence as job  # noqa: E402

ADMITTED = dt.datetime(2026, 9, 21, 7, 0, tzinfo=dt.timezone.utc)


def turn(turn_id: int = 1, *, terminal=None, sequences=(), message_id: str = "wamid.TEST0001") -> dict:
    return {"turn_id": turn_id, "provider_message_id": message_id,
            "conversation_ref": f"whatsapp:v1:conv:{turn_id}", "admitted_at": ADMITTED,
            "terminal": terminal, "sequences": list(sequences)}


def terminal(processing="completed", transport="accepted", reach="unknown") -> dict:
    return {"processing_outcome": processing, "transport_outcome": transport,
            "customer_reach": reach}


def sequence(outcome="accepted", *, receipts=("accepted",), sequence_id=10) -> dict:
    return {"sequence_id": sequence_id, "outcome": outcome,
            "attempts": [{"attempt_id": sequence_id * 10, "receipts": [
                {"kind": kind, "provider_message_id": f"wamid.OUT{sequence_id}"}
                for kind in receipts]}]}


def answered_turn(turn_id: int = 1) -> dict:
    """One inbound, answered once, accepted by the provider, reach unknown."""
    return turn(turn_id, terminal=terminal(), sequences=[sequence(sequence_id=turn_id)])


# ── Masking ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    ("+966501234567", "+9665*****67"),
    ("966501234567", "+9665*****67"),
    ("12345", "***45"),
    ("", "***"),
    (None, "***"),
])
def test_a_recipient_is_rendered_without_being_the_number(value, expected):
    assert job.mask_phone(value) == expected


def test_a_reference_keeps_enough_to_tell_two_apart_and_no_more():
    full = "wamid.HBgMOTY2NTAxMjM0NTY3"
    assert job.mask_reference(full) == "wamid.…NTY3"
    assert job.mask_reference(full) != full
    assert job.mask_reference("wamid.HBgMOTY2NTAxMjM0OTk5") != job.mask_reference(full)
    assert job.mask_reference("short") == "sh***"
    assert job.mask_reference("") == "***"


def test_the_masked_identifiers_are_what_the_report_carries():
    report = job.turn_report(answered_turn())
    assert "wamid.TEST0001" not in report["inbound_message_id"]
    assert report["inbound_message_id"] == job.mask_reference("wamid.TEST0001")
    assert all("wamid.OUT" not in v for v in report["accepted_message_ids"])


# ── One turn, read from its own rows ─────────────────────────────────────────

def test_a_turn_with_no_terminal_is_reported_as_having_none_not_as_failed():
    report = job.turn_report(turn())
    assert report["terminal"] is None
    assert report["reply_intents"] == 0 and report["accepted_sends"] == 0


def test_the_receipts_behind_a_reply_are_counted_by_kind():
    report = job.turn_report(turn(1, terminal=terminal(),
                                  sequences=[sequence(receipts=("accepted", "delivered"))]))
    assert report["accepted_sends"] == 1
    assert report["reach_receipts"] == 1
    assert report["receipt_kinds"] == ["accepted", "delivered"]


# ── An empty window proves nothing ───────────────────────────────────────────

def test_a_window_with_no_turns_proves_nothing_at_all():
    found = job.verdicts([], effects_reserved=0, deferred=[])
    assert set(found) == set(job.CLAIMS)
    proven = {n for n in job.CLAIMS if found[n]["verdict"] == job.PROVEN}
    # A claim *about absence* is established by a read that found nothing: the
    # effect ledger was read and held no row, so no commerce write was
    # reserved. Every other claim here is a property *of rows*, and with no
    # rows there is nothing to judge — which is not the same as passing.
    assert proven == {"no_commerce_write_was_reserved"}
    assert all(found[n]["verdict"] == job.NOT_OBSERVED
               for n in set(job.CLAIMS) - proven)
    assert not job.refused_claims(found)


def test_tables_that_were_not_read_are_never_reported_as_clean():
    found = job.verdicts([], effects_reserved=None, deferred=None)
    assert found["no_commerce_write_was_reserved"]["verdict"] == job.NOT_OBSERVED
    assert found["every_deferred_inbound_is_accounted_for"]["verdict"] == job.NOT_OBSERVED


# ── The claims, each refused by the rows that contradict it ──────────────────

def test_an_admitted_turn_with_no_terminal_refuses_the_first_claim():
    found = job.verdicts([answered_turn(1), turn(2)], effects_reserved=0, deferred=[])
    entry = found["every_admitted_turn_reached_a_terminal"]
    assert entry["verdict"] == job.REFUSED and entry["turns"] == [2]
    assert job.refused_claims(found) == ["every_admitted_turn_reached_a_terminal"]


def test_a_second_reply_intent_for_one_inbound_is_refused():
    doubled = turn(3, terminal=terminal(),
                   sequences=[sequence(sequence_id=1), sequence(sequence_id=2)])
    found = job.verdicts([doubled], effects_reserved=0, deferred=[])
    entry = found["at_most_one_reply_intent_per_turn"]
    assert entry["verdict"] == job.REFUSED and entry["turns"] == [3]


def test_more_accepted_sends_than_intents_is_one_intent_sent_twice():
    twice = turn(4, terminal=terminal(),
                 sequences=[sequence(receipts=("accepted", "accepted"))])
    found = job.verdicts([twice], effects_reserved=0, deferred=[])
    entry = found["at_most_one_accepted_send_per_reply_intent"]
    assert entry["verdict"] == job.REFUSED and entry["turns"] == [4]


def test_one_accepted_send_per_intent_is_proven_not_assumed():
    found = job.verdicts([answered_turn(1), answered_turn(2)], effects_reserved=0, deferred=[])
    assert found["at_most_one_accepted_send_per_reply_intent"]["verdict"] == job.PROVEN


def test_an_unknown_send_reported_as_completed_is_refused():
    lying = turn(5, terminal=terminal(processing="completed", transport="unknown"),
                 sequences=[sequence(outcome="unknown", receipts=("unknown",))])
    found = job.verdicts([lying], effects_reserved=0, deferred=[])
    entry = found["no_unknown_send_was_reported_completed"]
    assert entry["verdict"] == job.REFUSED and entry["turns"] == [5]


def test_an_unknown_send_recorded_as_failed_keeps_the_claim():
    honest = turn(6, terminal=terminal(processing="failed", transport="unknown"),
                  sequences=[sequence(outcome="unknown", receipts=("unknown",))])
    found = job.verdicts([honest], effects_reserved=0, deferred=[])
    assert found["no_unknown_send_was_reported_completed"]["verdict"] == job.PROVEN


def test_reach_claimed_without_a_receipt_behind_it_is_refused():
    claimed = turn(7, terminal=terminal(reach="reached"), sequences=[sequence()])
    found = job.verdicts([claimed], effects_reserved=0, deferred=[])
    entry = found["customer_reach_is_never_claimed_without_a_receipt"]
    assert entry["verdict"] == job.REFUSED and entry["turns"] == [7]


def test_reach_with_a_delivery_receipt_behind_it_is_proven():
    evidenced = turn(8, terminal=terminal(reach="reached"),
                     sequences=[sequence(receipts=("accepted", "delivered"))])
    found = job.verdicts([evidenced], effects_reserved=0, deferred=[])
    assert found["customer_reach_is_never_claimed_without_a_receipt"]["verdict"] == job.PROVEN


def test_acceptance_alone_never_becomes_a_reach_claim():
    found = job.verdicts([answered_turn()], effects_reserved=0, deferred=[])
    entry = found["customer_reach_is_never_claimed_without_a_receipt"]
    assert entry["verdict"] == job.NOT_OBSERVED
    assert "acceptance" in entry["why"]


def test_a_reserved_commerce_effect_is_refused_because_the_pilot_has_no_write_tool():
    found = job.verdicts([answered_turn()], effects_reserved=1, deferred=[])
    assert found["no_commerce_write_was_reserved"]["verdict"] == job.REFUSED


def test_a_deferred_inbound_with_no_disposition_is_refused_and_named():
    found = job.verdicts([], effects_reserved=0,
                         deferred=[{"id": 41, "state": "pending", "disposition": None},
                                   {"id": 42, "state": "disposed", "disposition": "replayed"}])
    entry = found["every_deferred_inbound_is_accounted_for"]
    assert entry["verdict"] == job.REFUSED and entry["turns"] == [41]


def test_deferred_inbounds_all_disposed_are_proven():
    found = job.verdicts([], effects_reserved=0,
                         deferred=[{"id": 42, "state": "disposed", "disposition": "replayed"}])
    assert found["every_deferred_inbound_is_accounted_for"]["verdict"] == job.PROVEN


def test_normal_runtime_resolution_does_not_require_an_operator_disposition():
    # resolve_inbound writes state=resolved and deliberately leaves disposition
    # NULL. The schema actually forbids a disposition on a resolved row.
    found = job.verdicts([], deferred=[
        {"id": 43, "state": "resolved", "disposition": None},
        {"id": 44, "state": "disposed", "disposition": "unanswered"},
    ])
    assert found["every_deferred_inbound_is_accounted_for"]["verdict"] == job.PROVEN


def test_a_label_cannot_make_a_pending_or_invalid_row_accounted_for():
    found = job.verdicts([], deferred=[
        {"id": 45, "state": "pending", "disposition": "replayed"},
        {"id": 46, "state": "disposed", "disposition": "arbitrary note"},
    ])
    entry = found["every_deferred_inbound_is_accounted_for"]
    assert entry["verdict"] == job.REFUSED
    assert entry["turns"] == [45, 46]


def test_an_unsent_intent_cannot_hide_two_acceptances_on_another_intent():
    data = turn(47, terminal=terminal(), sequences=[
        sequence(sequence_id=1, receipts=("accepted", "accepted")),
        sequence(sequence_id=2, outcome="reserved", receipts=()),
    ])
    entry = job.verdicts([data])["at_most_one_accepted_send_per_reply_intent"]
    assert entry["verdict"] == job.REFUSED
    assert entry["turns"] == [47]


def test_failed_read_only_setup_refuses_before_reading_any_window(monkeypatch):
    from unittest.mock import Mock

    db = Mock()
    read = Mock(side_effect=AssertionError("must not read without read-only setup"))
    monkeypatch.setattr(job, "configured_tenants", lambda: [1])
    monkeypatch.setattr(job, "session", lambda: db)
    monkeypatch.setattr(job, "_read_only", lambda _db: False)
    monkeypatch.setattr(job, "read_window", read)
    assert job.main([]) == job.EXIT_FAILED
    read.assert_not_called()
    db.rollback.assert_called_once()
    db.close.assert_called_once()


def test_a_clean_trial_refuses_nothing_and_claims_only_what_it_saw():
    turns = [answered_turn(1), answered_turn(2), answered_turn(3)]
    found = job.verdicts(turns, effects_reserved=0, deferred=[])
    assert not job.refused_claims(found)
    assert found["every_admitted_turn_reached_a_terminal"]["verdict"] == job.PROVEN
    assert found["at_most_one_reply_intent_per_turn"]["verdict"] == job.PROVEN


def test_every_contradicted_claim_is_named_in_the_closed_sets_order():
    broken = turn(9, terminal=terminal(processing="completed", transport="unknown",
                                       reach="reached"),
                  sequences=[sequence(sequence_id=1), sequence(sequence_id=2)])
    found = job.verdicts([broken], effects_reserved=2, deferred=[{"id": 7, "disposition": ""}])
    assert job.refused_claims(found) == [
        "at_most_one_reply_intent_per_turn",
        "no_unknown_send_was_reported_completed",
        "customer_reach_is_never_claimed_without_a_receipt",
        "no_commerce_write_was_reserved",
        "every_deferred_inbound_is_accounted_for",
    ]


# ── Rendering ────────────────────────────────────────────────────────────────

def test_the_report_names_every_claim_and_every_turn():
    turns = [answered_turn(1), turn(2)]
    found = job.verdicts(turns, effects_reserved=0, deferred=[])
    lines = job.render(1, [job.turn_report(t) for t in turns], found)
    assert lines[0] == "tenant=1 turns=2"
    assert any("turn=1" in line and "completed/accepted/reach=unknown" in line for line in lines)
    assert any("turn=2" in line and "no_terminal" in line for line in lines)
    for name in job.CLAIMS:
        assert any(f"claim {name}=" in line for line in lines)


# ── Scope ────────────────────────────────────────────────────────────────────

def test_only_the_pilots_own_allowlist_is_ever_read():
    from core.commerce_runtime import pilot_guard as pg

    assert job.configured_tenants({pg.ENV_TENANT_ALLOWLIST: "1, 33 ,1"}) == [1, 33]
    assert job.configured_tenants({pg.ENV_TENANT_ALLOWLIST: ""}) == []
    assert job.configured_tenants({pg.ENV_TENANT_ALLOWLIST: "nope,-2,0"}) == []


def test_a_window_is_a_stated_window():
    default = dt.datetime(2026, 9, 21, 8, 0, tzinfo=dt.timezone.utc)
    assert job._moment("", default=default) == default
    assert job._moment("2026-09-21T06:00:00Z", default=default) == dt.datetime(
        2026, 9, 21, 6, 0, tzinfo=dt.timezone.utc)
    # A moment without a zone is read as UTC rather than as local time.
    assert job._moment("2026-09-21T06:00:00", default=default).tzinfo is dt.timezone.utc
    with pytest.raises(ValueError):
        job._moment("yesterday", default=default)


def test_an_empty_or_backwards_window_is_refused_before_anything_is_read(monkeypatch):
    from core.commerce_runtime import pilot_guard as pg

    monkeypatch.setenv(pg.ENV_TENANT_ALLOWLIST, "1")

    def never(*_args, **_kwargs):  # pragma: no cover - the point is it is not called
        raise AssertionError("the database was opened for an empty window")

    monkeypatch.setattr(job, "session", never)
    assert job.main(["--since", "2026-09-21T08:00:00Z",
                     "--until", "2026-09-21T06:00:00Z"]) == job.EXIT_USAGE
    assert job.main(["--since", "yesterday"]) == job.EXIT_USAGE


def test_without_an_allowlist_nothing_is_read(monkeypatch):
    from core.commerce_runtime import pilot_guard as pg

    monkeypatch.delenv(pg.ENV_TENANT_ALLOWLIST, raising=False)

    def never(*_args, **_kwargs):  # pragma: no cover - the point is it is not called
        raise AssertionError("the database was opened with no allowlist")

    monkeypatch.setattr(job, "session", never)
    assert job.main([]) == job.EXIT_USAGE


def test_a_contradicted_claim_leaves_the_job_refusing(monkeypatch, capsys):
    from core.commerce_runtime import pilot_guard as pg

    monkeypatch.setenv(pg.ENV_TENANT_ALLOWLIST, "1")

    class _Session:
        def rollback(self): pass
        def close(self): pass

    monkeypatch.setattr(job, "session", lambda: _Session())
    monkeypatch.setattr(job, "_read_only", lambda _db: True)
    monkeypatch.setattr(job, "read_window",
                        lambda _db, **_kw: ([turn(2)], 0, []))
    assert job.main([]) == job.EXIT_REFUSED
    printed = capsys.readouterr().out
    assert "RESULT=REFUSED" in printed
    assert "every_admitted_turn_reached_a_terminal" in printed


def test_an_unreadable_database_is_never_a_clean_report(monkeypatch, capsys):
    from core.commerce_runtime import pilot_guard as pg

    monkeypatch.setenv(pg.ENV_TENANT_ALLOWLIST, "1")

    def boom():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(job, "session", boom)
    assert job.main([]) == job.EXIT_FAILED
    assert "RESULT=FAILED" in capsys.readouterr().out
