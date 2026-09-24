"""The read-only incident RCA isolates the sends of one window, intersects
them with the unapproved rows listed before it, and reports the strongest
evidence each recipient had before the window. PostgreSQL only; generic
merchant data; no phone number in the output."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import test_campaign_send_reconciliation as recon_tests
from test_campaign_send_reconciliation import _base, _log_row, _sql, pg

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
A, B, C, D, E, F = (f"+96650000040{i}" for i in range(1, 7))


def _attempt(c, *, log_id, phone, n, state, at, campaign=9, wamid=None, accepted=False,
             delivered=False, read=False, failed=False, post_code=None):
    _sql(c, "INSERT INTO campaign_send_attempts (tenant_id, campaign_id, send_log_id, "
            "customer_phone_e164, attempt_no, state, messaging_scope_key, provider_message_id, "
            "claimed_at, request_started_at, accepted_at, delivered_at, read_at, failed_at, "
            "post_accept_error_code, created_at, updated_at) VALUES (5, :cp, :l, :p, :n, :st, "
            "'bm:BM-GEN', :w, :at, :at, CASE WHEN :acc THEN CAST(:at AS timestamp) END, "
            "CASE WHEN :dl THEN CAST(:at AS timestamp) END, CASE WHEN :rd THEN CAST(:at AS timestamp) END, "
            "CASE WHEN :fl THEN CAST(:at AS timestamp) END, :pc, :at, :at)",
         cp=campaign, l=log_id, p=phone, n=n, st=state, w=wamid, at=at, acc=accepted,
         dl=delivered, rd=read, fl=failed, pc=post_code)


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
        for i in (1, 2, 4, 6, 7):
            _sql(c, "UPDATE campaign_send_logs SET sent_at = :s WHERE id = :i", s=IN_WINDOW, i=i)
        _attempt(c, log_id=4, phone=C, n=1, state="uncertain", at=BEFORE)
        _sql(c, "INSERT INTO campaign_send_logs (id, tenant_id, campaign_id, customer_phone_e164, "
                "status, attempt_count, created_at, updated_at) VALUES (8, 5, 10, :p, 'read', 1, "
                ":s, :s)", p=F, s=BEFORE)
        _attempt(c, log_id=8, phone=F, n=1, state="accepted", at=BEFORE, campaign=10,
                 accepted=True, read=True, wamid="w.old")
        for i, ph in ((1, A), (2, B), (4, C), (7, F)):
            _attempt(c, log_id=i, phone=ph, n=2 if i == 4 else 1, state="accepted",
                     at=IN_WINDOW, wamid=f"w.{i}", accepted=True)
        _attempt(c, log_id=6, phone=E, n=1, state="accepted", at=IN_WINDOW, wamid="w.6",
                 accepted=True, failed=True, post_code="marketing_blocked")
        _sql(c, "INSERT INTO campaign_dispatch_leases (campaign_id, tenant_id, pause_reason, "
                "paused_at, updated_at) VALUES (9, 5, 'provider_throttling', "
                "'2026-09-24 13:53:05', now())")


@pg
def test_incident_rca_isolates_the_window_and_its_history(pgdb, capsys):
    url, engine, _ = pgdb
    _seed(engine)
    rc = rca.main(["--tenant-id", "5", "--campaign-id", "9", "--since", SINCE, "--until", UNTIL,
                   "--unapproved-send-log-ids", "2,5", "--database-url", url])
    raw = capsys.readouterr().out
    out = json.loads(raw)
    assert rc == 0
    for ph in (A, B, C, D, E, F):
        assert ph not in raw and ph.lstrip("+") not in raw

    new = out["new_sends"]
    assert new["attempts"] == 5 and new["recipients"] == 5
    assert new["send_log_ids"] == [1, 2, 4, 6, 7]
    assert new["accepted"] == 5 and new["failed_after_accept"] == 1
    assert new["post_accept_error_keys"] == {"marketing_blocked": 1}

    inter = out["unapproved_intersection"]
    assert inter["sent_rows_that_are_unapproved"] == 1
    assert inter["unapproved_ids_hit_by_row"] == [2]
    assert inter["unapproved_ids_hit_by_recipient"] == [2]     # row 5 (D) was not sent

    hist = out["history_before_window"]
    assert hist["by_strongest_evidence"] == {"clean": 2, "delivered": 1, "uncertain": 1, "read": 1}
    assert hist["send_log_ids_by_evidence"] == {"read": [7], "delivered": [2], "uncertain": [4]}
    assert out["campaign_now"]["lease"]["pause_reason"] == "provider_throttling"


@pg
def test_incident_rca_is_read_only(pgdb, capsys):
    url, engine, _ = pgdb
    _seed(engine)
    from sqlalchemy import text

    def snap():
        with engine.connect() as c:
            return tuple(c.execute(text(
                "SELECT (SELECT md5(string_agg(t::text, ',' ORDER BY id)) FROM campaign_send_logs t), "
                "(SELECT md5(string_agg(t::text, ',' ORDER BY id)) FROM campaign_send_attempts t)")).one())
    before = snap()
    rca.main(["--tenant-id", "5", "--campaign-id", "9", "--since", SINCE, "--until", UNTIL,
              "--database-url", url])
    capsys.readouterr()
    assert snap() == before
