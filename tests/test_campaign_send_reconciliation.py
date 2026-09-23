"""Read-only campaign reconciliation: category rules and log parsing."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "campaign_send_reconciliation", ROOT / "scripts/operators/campaign_send_reconciliation.py",
)
rec = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = rec  # dataclasses resolve their module
_spec.loader.exec_module(rec)


def _r(*copies, status="sent", attempts=1, **kw):
    r = rec.Recipient("+966500000001", log_status=status, log_attempts=attempts, **kw)
    for i, c in enumerate(copies):
        r.copies[f"w{i}"] = rec.Copy(wamid=f"w{i}", **c)
    return r


def test_categories():
    c = rec.classify
    assert c(_r({"read": True}), delivery_evidence=True) == "delivered_once"
    assert c(_r({"read": True}, {"failed": True}), delivery_evidence=True) == "delivered_once"
    assert c(_r({"delivered": True}, {"read": True}), delivery_evidence=True) == "delivered_multiple"
    assert c(_r({"delivered": True}, {}), delivery_evidence=True) == "accepted_multiple_unproven"
    assert c(_r({"failed": True}, {"failed": True}), delivery_evidence=True) == "all_failed"
    assert c(_r({}), delivery_evidence=True) == "uncertain"          # no receipt ≠ not delivered
    assert c(_r(status="queued", attempts=0), delivery_evidence=True) == "not_started"
    # A legacy failed row proves nothing without an explicit rejection code.
    assert c(_r(status="failed"), delivery_evidence=True) == "uncertain"
    assert c(_r(status="failed", pre_accept_codes=["not_on_whatsapp"]),
             delivery_evidence=True) == "all_failed"
    assert c(_r(status="sending"), delivery_evidence=True) == "uncertain"
    assert c(_r(status="skipped_duplicate", attempts=0), delivery_evidence=True) == "excluded"


def test_log_parsing_pairs_each_failure_with_its_own_copy(tmp_path):
    lines = [
        "2026-09-23 10:56:26,291 INFO [campaign_dispatcher] campaign=9 sent OK to +966500007070 wamid=wamid.A",
        "2026-09-23 10:56:26,411 INFO [campaign_dispatcher] campaign=9 sent OK to +966500007070 wamid=wamid.B",
        "2026-09-23 10:56:27,000 INFO [campaign_dispatcher] campaign=8 sent OK to +966500000009 wamid=wamid.OTHER",
        "2026-09-23 10:56:34,762 INFO [PAYMENT_MEDIA_DIAG] status_failed wamid=wamid.A status=failed "
        "recipient_id=966500007070 timestamp=1 tenant_id=5 message_event=matched campaign_send_log=orphan "
        "errors=[{'code': 'REDACTED', 'title': 'Spam Rate limit hit', 'message': 'x'}]",
    ]
    f = tmp_path / "logs.json"
    f.write_text(json.dumps({"deploy": [{"timestamp": str(i), "message": m} for i, m in enumerate(lines)]}))
    recips = {}
    meta = rec.apply_logs(recips, campaign_id=9, tenant_id=5, log_paths=[str(f)], failed_tsv=[])
    assert meta["accepted_lines"] == 2 and meta["failed_events"] == 1
    r = recips["+966500007070"]
    assert r.copies["wamid.A"].failed and not r.copies["wamid.B"].failed
    report = rec.build_report(recips, delivery_evidence=False, sources={}, emit_recipients=True)
    assert report["recipients_by_accepted_copies"] == {2: 1}
    assert report["recipients_by_category"]["accepted_multiple_unproven"] == 1
    assert "7070" in report["recipients"][0]["recipient"] and "+9665" not in report["recipients"][0]["recipient"]


def test_phone_is_recovered_from_cloud_api_wamid():
    assert rec._phone_from_wamid(
        "wamid.HBgMOTY2NTAyNjIwMDI4FQIAERgUQ0VGMzc2M0MxQzgzODdDNzM0QkMA") == "+966502620028"


def test_missing_accept_line_is_inferred_from_its_failure_webhook(tmp_path):
    """A failure webhook proves Meta accepted that wamid. With the accept line
    missing from the export, the copy is counted only with the explicit flag,
    only when the webhook ties it to this campaign, and reported separately."""
    fail = ("2026-09-23 11:12:40,{ms} INFO [PAYMENT_MEDIA_DIAG] status_failed wamid={w} "
            "status=failed recipient_id=966500001111 timestamp=1 tenant_id=5 "
            "message_event=matched campaign_send_log={flag} "
            "errors=[{{'code': 'REDACTED', 'title': 'Spam Rate limit hit', 'message': 'x'}}]")
    lines = [fail.format(ms=100, w="wamid.X", flag="matched"),
             fail.format(ms=200, w="wamid.Y", flag="orphan"),
             fail.replace("966500001111", "966500002222").format(ms=300, w="wamid.Z", flag="orphan")]
    f = tmp_path / "logs.json"
    f.write_text(json.dumps({"deploy": [{"timestamp": str(i), "message": m} for i, m in enumerate(lines)]}))

    plain = {}
    meta = rec.apply_logs(plain, campaign_id=9, tenant_id=5, log_paths=[str(f)], failed_tsv=[])
    assert plain == {} and meta["failed_events_unmatched_wamid"] == 3

    inferred = {}
    meta = rec.apply_logs(inferred, campaign_id=9, tenant_id=5, log_paths=[str(f)],
                          failed_tsv=[], infer_from_failures=True)
    assert set(inferred) == {"+966500001111"}          # the orphan-only phone is not claimed
    assert len(inferred["+966500001111"].copies) == 2
    assert meta["accepted_inferred_from_failure"] == 2
    assert meta["failed_events_unmatched_wamid"] == 1


# ── R1: legacy ambiguous failures are never "proven failed" ─────────────


def test_legacy_ambiguous_failures_stay_uncertain():
    c = rec.classify
    for code in ("watchdog_timeout", "exception", "no_message_id", "send_outcome_unknown",
                 "unknown", ""):
        r = rec.Recipient("+966500000001", log_status="failed", log_attempts=1,
                          pre_accept_failures=1, pre_accept_codes=[code])
        assert c(r, delivery_evidence=True) == "uncertain", code
        assert rec.resend_proposal(r, "uncertain") == "excluded_unresolved"


def test_incomplete_attempt_history_is_not_proven_failed():
    """The legacy row keeps only its last error: with 3 attempts counted and
    one explicit code seen, the other two outcomes are unknown."""
    r = rec.Recipient("+966500000001", log_status="failed", log_attempts=3,
                      pre_accept_failures=1, pre_accept_codes=["rate_limit"])
    assert rec.classify(r, delivery_evidence=True) == "uncertain"
    # The attempt ledger proving every attempt unsent resolves it.
    r.ledger_unsent_attempts = 3
    assert rec.classify(r, delivery_evidence=True) == "all_failed"


def test_proven_unsent_controls():
    single = rec.Recipient("+966500000001", log_status="failed", log_attempts=1,
                           pre_accept_failures=1, pre_accept_codes=["rate_limit"])
    assert rec.classify(single, delivery_evidence=True) == "all_failed"
    assert rec.resend_proposal(single, "all_failed") == "retry_review_candidate"
    restricted = rec.Recipient("+966500000002", log_status="failed", log_attempts=1,
                               pre_accept_failures=1, pre_accept_codes=["not_on_whatsapp"])
    assert rec.resend_proposal(restricted, rec.classify(restricted, delivery_evidence=True)) \
        == "excluded_meta_restriction"


# ── R3: any proven delivery excludes the recipient from a resend ────────


def test_proven_delivery_excludes_regardless_of_other_copies():
    r = _r({"delivered": True}, {})                       # one delivered, one no receipt
    assert rec.classify(r, delivery_evidence=True) == "accepted_multiple_unproven"
    assert rec.resend_proposal(r, "accepted_multiple_unproven") == "excluded_proven_delivery"
    r2 = _r({"read": True}, {"failed": True, "failed_reason": "Spam Rate limit hit"})
    assert rec.resend_proposal(r2, rec.classify(r2, delivery_evidence=True)) \
        == "excluded_proven_delivery"
    report = rec.build_report({"a": r, "b": r2}, delivery_evidence=True, sources={},
                              emit_recipients=True)
    assert report["recipients_with_proven_delivery"] == 2
    assert all(x["has_proven_delivery"] and x["delivered_copies"] == 1
               for x in report["recipients"])
    assert report["resend_proposal"]["excluded_proven_delivery"] == 2


def test_post_accept_failure_is_a_restriction_not_a_retry():
    r = _r({"failed": True, "failed_reason": "Spam Rate limit hit"})
    assert rec.classify(r, delivery_evidence=True) == "all_failed"
    assert rec.resend_proposal(r, "all_failed") == "excluded_meta_restriction"


def test_log_only_reports_are_never_decision_eligible(tmp_path, capsys):
    f = tmp_path / "l.json"
    f.write_text(json.dumps({"deploy": []}))
    out = tmp_path / "r.json"
    assert rec.main(["--tenant-id", "5", "--campaign-id", "9", "--no-database",
                     "--log-json", str(f), "--out", str(out)]) == 0
    report = json.loads(out.read_text())
    assert report["decision_eligible"] is False
    assert report["delivery_evidence"] == "unavailable"


# ── R2: database mode — every source complete, or the run fails loudly ──

import os  # noqa: E402

import pytest  # noqa: E402

PG_DSN = os.environ.get("NAHLA_CAMPAIGN_LEDGER_PG_DSN")
pg = pytest.mark.skipif(not PG_DSN, reason="set NAHLA_CAMPAIGN_LEDGER_PG_DSN to run on PostgreSQL")


@pytest.fixture()
def pgdb():
    """Disposable PostgreSQL schema with the full model; yields (url, engine)."""
    import uuid
    from urllib.parse import quote
    from sqlalchemy import create_engine, text
    sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "database")]
    from models import Base
    schema = f"recon_{uuid.uuid4().hex[:8]}"
    admin = create_engine(PG_DSN)
    with admin.begin() as c:
        c.execute(text(f'CREATE SCHEMA "{schema}"'))
    opts = quote(f"-csearch_path={schema}")
    url = PG_DSN + ("&" if "?" in PG_DSN else "?") + f"options={opts}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    yield url, engine, schema
    engine.dispose()
    with admin.begin() as c:
        c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        c.execute(text("DROP ROLE IF EXISTS recon_ro_test"))
    admin.dispose()


def _seed_db(engine):
    """Generic merchant, one campaign, recipients covering every case:
    delivered once · duplicate with one delivered · both copies failed ·
    legacy watchdog failure · proven single rejection · not started ·
    ledger attempt uncertain."""
    from sqlalchemy import text
    now = "now() at time zone 'utc'"
    with engine.begin() as c:
        c.execute(text("INSERT INTO tenants (id, name, is_active) VALUES (5, 'متجر تجريبي عام', true)"))
        c.execute(text("INSERT INTO campaigns (id, tenant_id, name, campaign_type, status, "
                       "send_strategy) VALUES (9, 5, 'حملة عامة', 'broadcast', 'failed', 'immediate')"))
        rows = [
            # phone, status, attempts, wamid, error, delivered, read, failed
            ("+966500000001", "sent", 1, "w.1", None, True, False, False),
            ("+966500000002", "sent", 1, "w.2b", "131048", False, False, True),
            ("+966500000003", "sent", 1, "w.3b", "131048", False, False, True),
            ("+966500000004", "failed", 1, None, "watchdog_timeout", False, False, False),
            ("+966500000005", "failed", 1, None, "rate_limit", False, False, False),
            ("+966500000006", "queued", 0, None, None, False, False, False),
            ("+966500000007", "uncertain", 1, None, "send_outcome_unknown", False, False, False),
        ]
        for i, (ph, st, att, w, err, dl, rd, fl) in enumerate(rows, start=1):
            c.execute(text(
                "INSERT INTO campaign_send_logs (id, tenant_id, campaign_id, customer_phone_e164, "
                "status, attempt_count, provider_message_id, error_code, delivered_at, read_at, "
                f"failed_at, created_at, updated_at) VALUES (:i, 5, 9, :ph, :st, :att, :w, :err, "
                f"CASE WHEN :dl THEN {now} END, CASE WHEN :rd THEN {now} END, "
                f"CASE WHEN :fl THEN {now} END, {now}, {now})"),
                {"i": i, "ph": ph, "st": st, "att": att, "w": w, "err": err,
                 "dl": dl, "rd": rd, "fl": fl})
        # One message_events row per accepted copy (orphans included).
        for w, flags in (("w.1", {"_status_delivered": True}),
                         ("w.2a", {"_status_read": True}), ("w.2b", {"_status_failed": True}),
                         ("w.3a", {"_status_failed": True}), ("w.3b", {"_status_failed": True})):
            md = {"campaign_id": 9, "wa_message_id": w, **flags}
            c.execute(text("INSERT INTO message_events (tenant_id, direction, event_type, metadata) "
                           "VALUES (5, 'outbound', 'campaign', CAST(:m AS jsonb))"),
                      {"m": json.dumps(md)})
        # Ledger: one uncertain attempt for recipient 7.
        c.execute(text(
            "INSERT INTO campaign_send_attempts (tenant_id, campaign_id, send_log_id, "
            "customer_phone_e164, attempt_no, state, claimed_at, created_at, updated_at) "
            f"VALUES (5, 9, 7, '+966500000007', 1, 'uncertain', {now}, {now}, {now})"))
    # message_events carry no phone; map the synthetic wamids explicitly.
    return {"w.2a": "+966500000002", "w.3a": "+966500000003"}


def _run(url, tmp_path, *extra):
    out = tmp_path / "r.json"
    rc = rec.main(["--tenant-id", "5", "--campaign-id", "9", "--database-url", url,
                   "--emit-recipients", "--out", str(out), *extra])
    return rc, json.loads(out.read_text())


@pg
def test_database_mode_complete_run(pgdb, tmp_path, monkeypatch):
    url, engine, _ = pgdb
    phones = _seed_db(engine)
    monkeypatch.setattr(rec, "_phone_from_wamid", lambda w: phones.get(w))
    rc, rep = _run(url, tmp_path, "--check-schema")
    assert rc == 0
    assert rep["decision_eligible"] is True, rep["ineligible_reasons"]
    src = rep["sources"]["database"]["sources"]
    assert {v["status"] for v in src.values()} == {"complete"}
    assert rep["sources"]["database"]["preflight"]["schema_ok"] is True
    cats = rep["recipients_by_category"]
    assert cats["delivered_once"] == 2          # 1, and 2 (read copy + failed copy)
    assert cats["all_failed"] == 2              # 3 (both copies failed) and 5 (proven rejection)
    assert cats["uncertain"] == 2               # 4 (legacy watchdog) and 7 (ledger uncertain)
    assert cats["not_started"] == 1
    prop = rep["resend_proposal"]
    assert prop["excluded_proven_delivery"] == 2
    assert prop["excluded_meta_restriction"] == 1       # recipient 3
    assert prop["retry_review_candidate"] == 1          # recipient 5
    assert prop["excluded_unresolved"] == 2
    assert rep["recipients_with_proven_delivery"] == 2
    assert rep["messages"]["accepted"] == 5


@pg
def test_missing_ledger_table_fails_loudly_unless_explicitly_allowed(pgdb, tmp_path):
    from sqlalchemy import text
    url, engine, _ = pgdb
    _seed_db(engine)
    with engine.begin() as c:
        c.execute(text("DROP TABLE campaign_status_event_inbox"))
    rc, rep = _run(url, tmp_path)
    assert rc == 2 and rep["decision_eligible"] is False
    assert rep["sources"]["database"]["sources"]["campaign_status_event_inbox"]["status"] == "absent"
    rc, rep = _run(url, tmp_path, "--allow-missing-ledger", "--check-schema")
    assert rc == 0 and rep["decision_eligible"] is False     # usable for analysis only
    assert rep["sources"]["database"]["sources"]["campaign_status_event_inbox"] == {
        "status": "absent", "allowed": True}


@pg
def test_permission_failure_is_fatal_not_absent(pgdb, tmp_path):
    from sqlalchemy import text
    url, engine, schema = pgdb
    _seed_db(engine)
    with engine.begin() as c:
        c.execute(text("CREATE ROLE recon_ro_test LOGIN"))
        c.execute(text(f'GRANT USAGE ON SCHEMA "{schema}" TO recon_ro_test'))
        c.execute(text(f'GRANT SELECT ON ALL TABLES IN SCHEMA "{schema}" TO recon_ro_test'))
        c.execute(text("REVOKE SELECT ON message_delivery_events FROM recon_ro_test"))
    ro_url = url.replace("postgres@", "recon_ro_test@", 1)
    rc, rep = _run(ro_url, tmp_path)
    assert rc == 2 and rep["decision_eligible"] is False
    st = rep["sources"]["database"]["sources"]["message_delivery_events"]
    assert st["status"] == "error" and "InsufficientPrivilege" in st["error"]


@pg
def test_schema_drift_query_failure_is_fatal(pgdb, tmp_path):
    from sqlalchemy import text
    url, engine, _ = pgdb
    _seed_db(engine)
    with engine.begin() as c:
        c.execute(text("ALTER TABLE campaign_send_attempts RENAME COLUMN post_accept_error_code TO x"))
    rc, rep = _run(url, tmp_path)
    assert rc == 2
    assert rep["sources"]["database"]["sources"]["campaign_send_attempts"]["status"] == "error"


@pg
def test_failure_part_way_through_the_reads_is_fatal(pgdb, tmp_path, monkeypatch):
    url, engine, _ = pgdb
    _seed_db(engine)
    real = rec._read
    calls = {"n": 0}

    def flaky(conn, sql, params):
        calls["n"] += 1
        if calls["n"] == 3:             # after two sources were read successfully
            raise ConnectionError("server closed the connection unexpectedly")
        return real(conn, sql, params)

    monkeypatch.setattr(rec, "_read", flaky)
    rc, rep = _run(url, tmp_path)
    assert rc == 2 and rep["decision_eligible"] is False
    assert "recipients" not in rep and "recipients_by_category" not in rep
