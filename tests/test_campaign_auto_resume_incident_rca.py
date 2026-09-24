"""The read-only incident RCA isolates the attempts of one window (claimed,
started, accepted, delivered/read, failed-after-accept kept apart),
intersects them with the unapproved rows listed before it, and reports the
strongest evidence each recipient had before the window — from the ledger,
the campaign's rows, ``message_events`` and ``message_delivery_events``.
A recipient is never ``clean`` while a required source is unread or a
pre-window copy cannot be placed. PostgreSQL only; generic merchant data;
no phone number in the output."""
from __future__ import annotations

import importlib.util
import json

import pytest
import sys
from pathlib import Path

import test_campaign_send_reconciliation as recon_tests
from test_campaign_send_reconciliation import (
    _base, _campaign_event, _customer_conversation, _log_row, _sql, pg,
)

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "campaign_auto_resume_incident_rca",
    ROOT / "scripts/operators/campaign_auto_resume_incident_rca.py",
)
rca = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = rca
_spec.loader.exec_module(rca)

pgdb = recon_tests.pgdb
SINCE, UNTIL = "2026-09-24T13:47:43Z", "2026-09-24T13:53:05Z"
IN_WINDOW, BEFORE = "2026-09-24 13:50:00", "2026-09-23 10:30:00"
A, B, C, D, E, F, G, H = (f"+96650000040{i}" for i in range(1, 9))


def _attempt(c, *, log_id, phone, n, state, at, campaign=9, wamid=None, started=True,
             accepted=False, delivered=False, read=False, failed=False, post_code=None):
    _sql(c, "INSERT INTO campaign_send_attempts (tenant_id, campaign_id, send_log_id, "
            "customer_phone_e164, attempt_no, state, messaging_scope_key, provider_message_id, "
            "claimed_at, request_started_at, accepted_at, delivered_at, read_at, failed_at, "
            "post_accept_error_code, created_at, updated_at) VALUES (5, :cp, :l, :p, :n, :st, "
            "'bm:BM-GEN', :w, :at, CASE WHEN :sd THEN CAST(:at AS timestamp) END, "
            "CASE WHEN :acc THEN CAST(:at AS timestamp) END, "
            "CASE WHEN :dl THEN CAST(:at AS timestamp) END, CASE WHEN :rd THEN CAST(:at AS timestamp) END, "
            "CASE WHEN :fl THEN CAST(:at AS timestamp) END, :pc, :at, :at)",
         cp=campaign, l=log_id, p=phone, n=n, st=state, w=wamid, at=at, sd=started,
         acc=accepted, dl=delivered, rd=read, fl=failed, pc=post_code)


def _event_at(c, when):
    """Date the most recent message_events row."""
    _sql(c, "UPDATE message_events SET created_at = :w WHERE id = (SELECT max(id) FROM message_events)",
         w=when)


def _seed(engine):
    with engine.begin() as c:
        _base(c)
        _log_row(c, 1, A, "sent", 1, wamid="w.1")             # clean recipient
        _log_row(c, 2, B, "sent", 1, wamid="w.2")             # unapproved; B had row 3 delivered
        _log_row(c, 3, B.lstrip("+"), "delivered", 1, wamid="w.3", dl=True)  # same recipient, other format
        _sql(c, "UPDATE campaign_send_logs SET sent_at = :s, delivered_at = :s WHERE id = 3", s=BEFORE)
        _log_row(c, 4, C, "sent", 2, wamid="w.4")             # an earlier attempt was uncertain
        _log_row(c, 5, D, "queued", 0)                        # unapproved, not sent
        _log_row(c, 6, E, "failed", 1, wamid="w.6", fl=True)  # Meta 131049 after accept
        _log_row(c, 7, F, "sent", 1, wamid="w.7")             # read in another campaign before
        _log_row(c, 9, G, "sent", 1, wamid="w.9")             # only message_events knows its first copy
        _log_row(c, 10, H, "queued", 0)                       # claimed, never started
        for i in (1, 2, 4, 6, 7, 9):
            _sql(c, "UPDATE campaign_send_logs SET sent_at = :s WHERE id = :i", s=IN_WINDOW, i=i)
        _attempt(c, log_id=4, phone=C, n=1, state="uncertain", at=BEFORE)
        _sql(c, "INSERT INTO campaign_send_logs (id, tenant_id, campaign_id, customer_phone_e164, "
                "status, attempt_count, created_at, updated_at) VALUES (8, 5, 10, :p, 'read', 1, "
                ":s, :s)", p=F, s=BEFORE)
        _attempt(c, log_id=8, phone=F, n=1, state="accepted", at=BEFORE, campaign=10,
                 accepted=True, read=True, wamid="w.old")
        for i, ph in ((1, A), (2, B), (4, C), (7, F), (9, G)):
            _attempt(c, log_id=i, phone=ph, n=2 if i == 4 else 1, state="accepted",
                     at=IN_WINDOW, wamid=f"w.{i}", accepted=True)
        _attempt(c, log_id=6, phone=E, n=1, state="accepted", at=IN_WINDOW, wamid="w.6",
                 accepted=True, failed=True, post_code="marketing_blocked")
        _attempt(c, log_id=10, phone=H, n=1, state="not_sent", at=IN_WINDOW, started=False)
        # G's first copy (09-23) was read; its wamid was overwritten on the row
        # and it predates the ledger -- message_events is the only trace.
        conv_g = _customer_conversation(c, 5, G)
        _campaign_event(c, "w.g-first", conv=conv_g, _status_delivered=True, _status_read=True)
        _event_at(c, BEFORE)
        # The window's own copy for A, read already: never pre-window evidence.
        conv_a = _customer_conversation(c, 5, A)
        _campaign_event(c, "w.1", conv=conv_a, _status_delivered=True, _status_read=True)
        _event_at(c, IN_WINDOW)
        _sql(c, "INSERT INTO campaign_dispatch_leases (campaign_id, tenant_id, pause_reason, "
                "paused_at, updated_at) VALUES (9, 5, 'provider_throttling', "
                "'2026-09-24 13:53:05', now())")


def _run(url, capsys, *extra):
    rc = rca.main(["--tenant-id", "5", "--campaign-id", "9", "--since", SINCE, "--until", UNTIL,
                   "--database-url", url, *extra])
    raw = capsys.readouterr().out
    return rc, raw, json.loads(raw)


@pg
def test_incident_rca_isolates_the_window_and_its_history(pgdb, capsys):
    url, engine, _ = pgdb
    _seed(engine)
    rc, raw, out = _run(url, capsys, "--unapproved-send-log-ids", "2,5")
    assert rc == 0
    for ph in (A, B, C, D, E, F, G, H):
        assert ph not in raw and ph.lstrip("+") not in raw

    w = out["window"]
    assert w["attempts_claimed_in_window"] == 7
    assert w["requests_started_in_window"] == 6
    assert w["accepted_in_window"] == 6
    assert w["failed_after_accept"] == 1
    assert w["post_accept_error_keys"] == {"marketing_blocked": 1}
    assert w["recipients_claimed"] == 7 and w["recipients_accepted"] == 6
    assert w["send_log_ids_claimed"] == [1, 2, 4, 6, 7, 9, 10]

    inter = out["unapproved_intersection"]
    assert inter["unapproved_ids_hit_by_row"] == [2]
    assert inter["unapproved_ids_hit_by_recipient"] == [2]     # row 5 (D) was not claimed
    assert inter["unapproved_rows_whose_recipient_was_accepted"] == 1

    hist = out["history_before_window"]
    assert hist["by_strongest_evidence"] == {
        "clean": 3, "delivered": 1, "uncertain": 1, "read": 2}
    assert hist["send_log_ids_by_strongest_evidence"] == {
        "read": [7, 9], "delivered": [2], "uncertain": [4], "clean": [1, 6, 10]}
    assert hist["evidence_hits_by_source"]["message_events"]["read"] == 1
    assert hist["pre_window_copies_not_placeable"] == {}
    assert set(hist["sources_read"]) >= {
        "campaign_send_attempts", "campaign_send_logs", "message_events", "message_delivery_events"}
    assert out["campaign_now"]["lease"]["pause_reason"] == "provider_throttling"
    # The owner's table, over the recipients Meta accepted in the window
    # (A B C E F G): B's other row, F's other campaign and G's first copy
    # were accepted before; C's earlier attempt is uncertain.
    assert hist["table_accepted_in_window"] == {
        "new_recipients": 6, "with_prior_accepted": 3, "with_prior_delivered": 3,
        "with_prior_read": 2, "with_prior_uncertain": 1, "with_prior_request_started": 0,
        "unresolved_evidence": 0, "clean": 2, "duplicate_incident": 4}


@pg
def test_request_started_is_evidence_and_a_claim_never_started_is_not(pgdb, capsys):
    url, engine, _ = pgdb
    _seed(engine)
    with engine.begin() as c:
        _attempt(c, log_id=1, phone=A, n=0, state="claimed", at=BEFORE, started=False)
        _attempt(c, log_id=6, phone=E, n=0, state="request_started", at=BEFORE)
    _, _, out = _run(url, capsys)
    hist = out["history_before_window"]
    by = hist["send_log_ids_by_strongest_evidence"]
    assert by["request_started"] == [6]
    assert 1 in by["clean"]                                   # the request never left
    assert hist["recipients_with_prior_claim_never_started"] == 1
    assert hist["table_accepted_in_window"]["with_prior_request_started"] == 1


@pg
def test_message_events_alone_keeps_a_recipient_from_clean(pgdb, capsys):
    """G has no earlier attempt and no other row: only its first copy in
    message_events says it was read. Delete that copy and G is clean --
    the classification rests on message_events."""
    from sqlalchemy import text
    url, engine, _ = pgdb
    _seed(engine)
    _, _, out = _run(url, capsys)
    assert 9 in out["history_before_window"]["send_log_ids_by_strongest_evidence"]["read"]
    with engine.begin() as c:
        c.execute(text("DELETE FROM message_events WHERE metadata->>'wa_message_id' = 'w.g-first'"))
    _, _, out = _run(url, capsys)
    by = out["history_before_window"]["send_log_ids_by_strongest_evidence"]
    assert 9 not in by.get("read", []) and 9 in by["clean"]


@pg
def test_a_copy_that_cannot_be_placed_leaves_nobody_clean(pgdb, capsys):
    url, engine, _ = pgdb
    _seed(engine)
    with engine.begin() as c:
        _campaign_event(c, "w.orphan", conv=None, _status_delivered=True)   # no conversation, unknown wamid
        _event_at(c, BEFORE)
    _, _, out = _run(url, capsys)
    hist = out["history_before_window"]
    assert "clean" not in hist["by_strongest_evidence"]
    assert hist["by_strongest_evidence"]["unresolved_evidence"] == 3
    assert hist["send_log_ids_by_strongest_evidence"]["unresolved_evidence"] == [1, 6, 10]
    assert hist["pre_window_copies_not_placeable"] == {"no_recipient_link": 1}


@pg
def test_an_unreadable_source_classifies_nobody(pgdb, capsys):
    from sqlalchemy import text
    url, engine, schema = pgdb
    _seed(engine)
    with engine.begin() as c:
        c.execute(text("CREATE ROLE recon_ro_test LOGIN"))
        c.execute(text(f'GRANT USAGE ON SCHEMA "{schema}" TO recon_ro_test'))
        c.execute(text(f'GRANT SELECT ON ALL TABLES IN SCHEMA "{schema}" TO recon_ro_test'))
        c.execute(text("REVOKE SELECT ON message_events FROM recon_ro_test"))
    rc, _, out = _run(url.replace("postgres@", "recon_ro_test@", 1), capsys)
    assert rc == 3
    assert "history_before_window" not in out
    assert out["source"].startswith("message_events")


@pg
def test_incident_rca_is_read_only(pgdb, capsys):
    from sqlalchemy import text
    url, engine, _ = pgdb
    _seed(engine)

    def snap():
        with engine.connect() as c:
            return tuple(c.execute(text(
                "SELECT (SELECT md5(string_agg(t::text, ',' ORDER BY id)) FROM campaign_send_logs t), "
                "(SELECT md5(string_agg(t::text, ',' ORDER BY id)) FROM campaign_send_attempts t), "
                "(SELECT md5(string_agg(t::text, ',' ORDER BY id)) FROM message_events t)")).one())
    before = snap()
    _run(url, capsys)
    assert snap() == before


# ── Review findings: false clean, raw-field classification, identity ─────


@pg
def test_a_never_started_claim_plus_an_unplaceable_copy_is_never_clean(pgdb, capsys):
    """Reproduces the review's case: the recipient's only history is a claim
    that never started (not evidence) and some pre-window copy cannot be
    placed. The orphan rule must still mark it unresolved -- the earlier
    head skipped it because its evidence set was not empty, then dropped
    ``claimed_never_started`` and reported clean."""
    url, engine, _ = pgdb
    _seed(engine)
    with engine.begin() as c:
        _attempt(c, log_id=1, phone=A, n=0, state="claimed", at=BEFORE, started=False)
        _campaign_event(c, "w.orphan", conv=None, _status_delivered=True)
        _event_at(c, BEFORE)
    _, _, out = _run(url, capsys)
    by = out["history_before_window"]["send_log_ids_by_strongest_evidence"]
    assert 1 in by["unresolved_evidence"]
    assert 1 not in by.get("clean", [])


@pg
@pytest.mark.parametrize("state, wamid, started, expect", [
    ("claimed", None, True, "request_started"),        # label says claimed, request left
    ("rejected", "w.contradiction", True, "accepted"),  # label says rejected, Meta gave a wamid
    ("abandoned", None, True, "request_started"),      # abandoned after the request started
    ("mystery", None, False, "unknown"),               # a label nobody defined
])
def test_prior_attempts_are_classified_from_their_raw_fields(pgdb, capsys, state, wamid,
                                                             started, expect):
    url, engine, _ = pgdb
    _seed(engine)
    with engine.begin() as c:
        _attempt(c, log_id=1, phone=A, n=0, state=state, at=BEFORE, started=started, wamid=wamid)
    _, _, out = _run(url, capsys)
    by = out["history_before_window"]["send_log_ids_by_strongest_evidence"]
    assert 1 in by[expect] and 1 not in by.get("clean", [])


@pg
@pytest.mark.parametrize("spelling", ["00" + A.lstrip("+"), "0" + A[4:], "+966 50 000 0401"])
def test_history_is_matched_by_identity_not_by_digits(pgdb, capsys, spelling):
    """A's earlier accepted attempt stored as ``00966…`` / ``05…`` / spaced
    is still A's (digit stripping alone missed ``00966…`` and ``05…``)."""
    url, engine, _ = pgdb
    _seed(engine)
    with engine.begin() as c:
        _attempt(c, log_id=8, phone=spelling, n=5, state="accepted", at=BEFORE, campaign=10,
                 accepted=True, wamid="w.other-spelling")
    _, _, out = _run(url, capsys)
    by = out["history_before_window"]["send_log_ids_by_strongest_evidence"]
    assert 1 in by["accepted"]


@pg
def test_a_window_number_without_identity_is_unresolved(pgdb, capsys):
    url, engine, _ = pgdb
    _seed(engine)
    with engine.begin() as c:
        _log_row(c, 11, "+96650000040", "sent", 1, wamid="w.11")      # one digit short
        _attempt(c, log_id=11, phone="+96650000040", n=1, state="accepted", at=IN_WINDOW,
                 accepted=True, wamid="w.11")
    _, _, out = _run(url, capsys)
    assert out["window"]["recipients_without_validated_identity"] == 1
    assert 11 in out["history_before_window"]["send_log_ids_by_strongest_evidence"][
        "unresolved_evidence"]
