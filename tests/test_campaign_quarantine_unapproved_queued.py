"""Hold only the queued rows the reconciliation does not approve for a new
send; everything else stays exactly as it was. PostgreSQL only (the tool
reads with PostgreSQL JSONB operators and row locks)."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

import test_campaign_send_reconciliation as recon_tests
from test_campaign_send_reconciliation import (
    _attempt, _base, _campaign_event, _customer_conversation, _inbox, _log_row, _sql, pg,
)

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "campaign_quarantine_unapproved_queued",
    ROOT / "scripts/operators/campaign_quarantine_unapproved_queued.py",
)
q = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = q
_spec.loader.exec_module(q)

pgdb = recon_tests.pgdb

APPROVED = "+966500000201"       # queued, never attempted          → stays queued
RECLAIMED = "+966500000202"      # queued, counter 1, claim reclaimed → hold
COPY = "+966500000203"           # queued, but an accepted copy exists → hold
PENDING = "+966500000204"        # queued, unapplied inbox receipt    → hold
DELIVERED = "+966500000205"      # sent + delivered                   → untouched
RESTRICTED = "+966500000206"     # failed not_on_whatsapp             → untouched


def _seed(engine):
    with engine.begin() as c:
        _base(c)
        _log_row(c, 1, APPROVED, "queued", 0)
        _log_row(c, 2, RECLAIMED, "queued", 1)
        _attempt(c, 2, RECLAIMED, 1, "abandoned", err="worker_lost_before_request")
        _log_row(c, 3, COPY, "queued", 0)
        _campaign_event(c, "w.203", _customer_conversation(c, 5, COPY))
        _log_row(c, 4, PENDING, "queued", 0)
        _inbox(c, "w.unknown204", "delivered", recipient=PENDING.lstrip("+"))
        _log_row(c, 5, DELIVERED, "sent", 1, wamid="w.205", dl=True)
        _log_row(c, 6, RESTRICTED, "failed", 1, err="not_on_whatsapp")


def _statuses(engine):
    with engine.connect() as c:
        return {ph: (st, code) for ph, st, code in _sql(c,
                "SELECT customer_phone_e164, status, error_code FROM campaign_send_logs "
                "WHERE campaign_id = 9").all()}


def _run(url, capsys, *extra):
    rc = q.main(["--tenant-id", "5", "--campaign-id", "9", "--database-url", url, *extra])
    return rc, json.loads(capsys.readouterr().out)


@pg
def test_dry_run_names_exactly_the_unapproved_queued_rows_and_writes_nothing(pgdb, capsys):
    url, engine, _ = pgdb
    _seed(engine)
    before = _statuses(engine)
    rc, out = _run(url, capsys)
    assert rc == 0, out["pre_write_gate"]
    assert out["unapproved_queued_count"] == 3
    got = {r["recipient"][3:7]: r["proposal"] for r in out["unapproved_queued"]}
    assert got == {"0202": "excluded_unresolved", "0203": "excluded_unresolved",
                   "0204": "excluded_unresolved"}
    assert "+9665" not in json.dumps(out)                    # masked
    assert _statuses(engine) == before


@pg
def test_apply_holds_only_those_rows_and_the_final_gate_passes(pgdb, capsys, monkeypatch):
    url, engine, _ = pgdb
    _seed(engine)
    before = _statuses(engine)
    monkeypatch.setenv("NAHLA_QUARANTINE_CONFIRM", "campaign-9")
    rc, out = _run(url, capsys, "--apply", "--expect", "3")
    assert rc == 0, out.get("final_gate") or out.get("result")
    assert out["held"] == 3
    after = _statuses(engine)
    for ph in (RECLAIMED, COPY, PENDING):
        assert after[ph] == ("uncertain", "send_outcome_unknown")
    for ph in (APPROVED, DELIVERED, RESTRICTED):
        assert after[ph] == before[ph]                         # untouched
    gate = out["after"]
    assert gate["send_log_status_counts"]["queued"] == 1
    assert gate["resend_proposal"]["not_started_new_send_decision"] == 1
    assert out["remaining_unapproved_queued"] == 0 and out["final_gate"]["passed"] is True
    # Running again finds nothing to hold.
    rc, out = _run(url, capsys, "--apply", "--expect", "0")
    assert rc == 0 and out["held"] == 0


@pg
@pytest.mark.parametrize("extra, env", [
    (["--apply"], "campaign-9"),                               # no --expect
    (["--apply", "--expect", "2"], "campaign-9"),              # reviewed count differs
    (["--apply", "--expect", "3"], None),                      # no confirmation
    (["--apply", "--expect", "3"], "campaign-8"),              # confirmation for another campaign
])
def test_apply_refuses_without_the_reviewed_count_and_confirmation(pgdb, capsys, monkeypatch,
                                                                   extra, env):
    url, engine, _ = pgdb
    _seed(engine)
    before = _statuses(engine)
    if env:
        monkeypatch.setenv("NAHLA_QUARANTINE_CONFIRM", env)
    else:
        monkeypatch.delenv("NAHLA_QUARANTINE_CONFIRM", raising=False)
    rc, out = _run(url, capsys, *extra)
    assert rc == 3 and out["result"].startswith("refused")
    assert _statuses(engine) == before


@pg
@pytest.mark.parametrize("state", ["live_lease", "in_flight", "active"])
def test_apply_refuses_while_the_campaign_can_send(pgdb, capsys, monkeypatch, state):
    url, engine, _ = pgdb
    _seed(engine)
    with engine.begin() as c:
        if state == "live_lease":
            _sql(c, "INSERT INTO campaign_dispatch_leases (campaign_id, tenant_id, owner, "
                    "acquired_at, heartbeat_at, expires_at, updated_at) VALUES (9, 5, 'worker', "
                    "now() at time zone 'utc', now() at time zone 'utc', "
                    "now() at time zone 'utc' + interval '5 minutes', now() at time zone 'utc')")
        elif state == "in_flight":
            _attempt(c, 1, APPROVED, 1, "request_started")
        else:
            _sql(c, "UPDATE campaigns SET status = 'active' WHERE id = 9")
    before = _statuses(engine)
    monkeypatch.setenv("NAHLA_QUARANTINE_CONFIRM", "campaign-9")
    rc, out = _run(url, capsys, "--apply", "--expect", "3")
    assert rc == 3
    assert out["pre_write_gate"]["passed"] is False
    assert _statuses(engine) == before


@pg
def test_rows_that_changed_after_the_snapshot_abort_the_write(pgdb, capsys, monkeypatch):
    """A reviewed row that is no longer queued when the write locks the rows
    aborts the whole transaction."""
    url, engine, _ = pgdb
    _seed(engine)
    real = q.apply_hold

    def racing(url_, **kw):
        with engine.begin() as c:
            _sql(c, "UPDATE campaign_send_logs SET status = 'sending' "
                    "WHERE customer_phone_e164 = :p", p=COPY)
        return real(url_, **kw)

    monkeypatch.setattr(q, "apply_hold", racing)
    monkeypatch.setenv("NAHLA_QUARANTINE_CONFIRM", "campaign-9")
    rc, out = _run(url, capsys, "--apply", "--expect", "3")
    assert rc == 3 and "do not match" in out["result"]
    after = _statuses(engine)
    assert after[RECLAIMED] == ("queued", None) and after[PENDING] == ("queued", None)


# ── Reversible, idempotent, never destructive ───────────────────────────


def _counts(engine):
    with engine.connect() as c:
        return (_sql(c, "SELECT count(*) FROM campaign_send_logs").scalar(),
                _sql(c, "SELECT count(*) FROM campaign_send_attempts").scalar())


def _row(engine, phone):
    with engine.connect() as c:
        return tuple(_sql(c, "SELECT status, error_code, error_message FROM campaign_send_logs "
                             "WHERE customer_phone_e164 = :p", p=phone).first())


@pg
def test_dry_run_reports_groups_and_exact_before_after(pgdb, capsys):
    url, engine, _ = pgdb
    _seed(engine)
    rc, out = _run(url, capsys)
    assert rc == 0
    changes = out["planned_changes"]
    assert [c["send_log_id"] for c in changes] == [2, 3, 4]
    assert all(c["before"]["status"] == "queued" and c["after"] == {
        "status": "uncertain", "error_code": "send_outcome_unknown"} for c in changes)
    by_id = {c["send_log_id"]: c for c in changes}
    assert by_id[2]["ledger_states"] == ["abandoned"] and by_id[2]["row_attempt_count"] == 1
    assert by_id[3]["accepted_copies"] == 1 and by_id[3]["delivered_copies"] == 0
    assert by_id[4]["reason"] == "unapplied inbox receipt for an unknown wamid"
    assert sum(g["rows"] for g in out["unapproved_queued_groups"]) == 3
    assert "+9665" not in json.dumps(out)


@pg
def test_hold_keeps_history_and_is_neither_failed_nor_resendable(pgdb, capsys, monkeypatch):
    url, engine, _ = pgdb
    _seed(engine)
    with engine.begin() as c:
        _sql(c, "UPDATE campaign_send_logs SET error_code = 'transport_not_sent', "
                "error_message = 'connect timeout' WHERE customer_phone_e164 = :p", p=RECLAIMED)
    counts = _counts(engine)
    monkeypatch.setenv("NAHLA_QUARANTINE_CONFIRM", "campaign-9")
    rc, out = _run(url, capsys, "--apply", "--expect", "3")
    assert rc == 0, out.get("final_gate")
    assert _counts(engine) == counts                           # nothing deleted
    st, code, msg = _row(engine, RECLAIMED)
    assert (st, code) == ("uncertain", "send_outcome_unknown")  # unknown, not failed
    held = q.parse_hold(msg)
    assert held["prev_error_code"] == "transport_not_sent"
    assert held["prev_error_message"] == "connect timeout"
    # Nothing re-queues it: the retry path only promotes failed rows, the
    # dispatcher only claims queued rows, the reconciliation keeps it out.
    sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "database")]
    from sqlalchemy.orm import Session
    from services import campaign_dispatcher as disp
    with Session(engine) as db:
        assert disp.reschedule_failed_for_retry(db, 9) == 0
        db.commit()
    assert _row(engine, RECLAIMED)[0] == "uncertain"
    rc, out = _run(url, capsys)
    assert out["unapproved_queued_count"] == 0
    # Idempotent: a second apply has nothing to hold.
    rc, out = _run(url, capsys, "--apply", "--expect", "0")
    assert rc == 0 and out["held"] == 0 and _counts(engine) == counts


@pg
def test_revert_restores_exactly_the_held_rows(pgdb, capsys, monkeypatch):
    url, engine, _ = pgdb
    _seed(engine)
    with engine.begin() as c:
        _sql(c, "UPDATE campaign_send_logs SET error_code = 'transport_not_sent', "
                "error_message = 'connect timeout' WHERE customer_phone_e164 = :p", p=RECLAIMED)
        # An unrelated uncertain row the tool never held must stay untouched.
        _log_row(c, 7, "+966500000207", "uncertain", 1, err="send_outcome_unknown")
    before = _statuses(engine)
    before_reclaimed = _row(engine, RECLAIMED)
    monkeypatch.setenv("NAHLA_QUARANTINE_CONFIRM", "campaign-9")
    _run(url, capsys, "--apply", "--expect", "3")
    rc, out = _run(url, capsys, "--revert", "--expect", "2")   # wrong count
    assert rc == 3 and out["result"].startswith("refused")
    rc, out = _run(url, capsys, "--revert", "--expect", "3")
    assert rc == 0 and out["reverted"] == 3
    assert _statuses(engine) == before
    assert _row(engine, RECLAIMED) == before_reclaimed
    rc, out = _run(url, capsys, "--revert", "--expect", "0")   # idempotent
    assert rc == 0 and out["reverted"] == 0
    monkeypatch.delenv("NAHLA_QUARANTINE_CONFIRM")
    rc, out = _run(url, capsys, "--revert", "--expect", "0")
    assert rc == 3                                             # confirmation required
