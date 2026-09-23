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


def _r(*copies, status="sent", attempts=1, code=None, ledger=(), failed_at=False,
       has_log_row=True):
    """A recipient as the database reader builds it. ``ledger`` is a list of
    ``(attempt_no, state, wamid, code)``."""
    r = rec.Recipient("+966500000001", has_log_row=has_log_row, log_status=status,
                      log_attempts=attempts, log_error_code=code, log_failed_at=failed_at)
    for i, c in enumerate(copies):
        r.copies[f"w{i}"] = rec.Copy(wamid=f"w{i}", **c)
    for no, state, wamid, err in ledger:
        r.ledger[no] = rec.LedgerAttempt(no, state, wamid, err)
    return r


def test_categories():
    c = lambda r: rec.classify(r, delivery_evidence=True)  # noqa: E731
    assert c(_r({"read": True})) == "delivered_once"
    assert c(_r({"read": True}, {"failed": True}, attempts=2)) == "delivered_once"
    assert c(_r({"delivered": True}, {"read": True}, attempts=2)) == "delivered_multiple"
    assert c(_r({"delivered": True}, {}, attempts=2)) == "accepted_multiple_unproven"
    assert c(_r({"failed": True}, {"failed": True}, attempts=2)) == "all_failed"
    assert c(_r({})) == "uncertain"                       # no receipt ≠ not delivered
    assert c(_r(status="queued", attempts=0)) == "not_started"
    # A legacy failed row proves nothing without a positive rejection code.
    assert c(_r(status="failed")) == "uncertain"
    assert c(_r(status="failed", code="not_on_whatsapp")) == "all_failed"
    assert c(_r(status="sending")) == "uncertain"
    assert c(_r(status="skipped_duplicate", attempts=0)) == "excluded"
    assert c(_r(status="sending", attempts=0)) == "uncertain"


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


# ── R1: every counted attempt accounted for exactly once ────────────────


def test_legacy_ambiguous_failures_stay_uncertain():
    for code in ("watchdog_timeout", "exception", "no_message_id", "send_outcome_unknown",
                 "unknown", "", "retry_exhausted", "service_unavailable"):
        r = _r(status="failed", code=code)
        assert rec.classify(r, delivery_evidence=True) == "uncertain", code
        assert rec.resend_proposal(r, "uncertain") == "excluded_unresolved"


def test_unrecognised_code_is_not_rejection_evidence():
    """Absence from the ambiguous list is not proof: only known rejections are."""
    for code in ("131099_new_meta_code", "Rate limit hit", "rejected", "rate-limit"):
        r = _r(status="failed", code=code)
        assert rec.classify(r, delivery_evidence=True) == "uncertain", code


def test_summary_row_and_ledger_row_for_the_same_attempt_count_once():
    """The ledger copies its code onto the summary row: both describe
    attempt 2. Attempt 1 (legacy, overwritten) is unrecovered."""
    r = _r(status="failed", attempts=2, code="rate_limit",
           ledger=[(1, "rejected", None, "rate_limit")])
    h = rec.attempt_history(r)
    assert not h.complete and h.unknown_attempts == 1 and h.unsent_codes == ["rate_limit"]
    assert rec.classify(r, delivery_evidence=True) == "uncertain"
    assert rec.resend_proposal(r, "uncertain") == "excluded_unresolved"


def test_abandoned_claim_is_not_a_counted_attempt():
    # The counter was decremented when the claim was released.
    ok = _r(status="failed", attempts=1, code="rate_limit",
            ledger=[(1, "abandoned", None, "budget_exhausted"), (2, "rejected", None, "rate_limit")])
    assert rec.classify(ok, delivery_evidence=True) == "all_failed"
    # With a legacy attempt before it, that attempt is still unknown.
    short = _r(status="failed", attempts=2, code="rate_limit",
               ledger=[(1, "abandoned", None, "budget_exhausted"), (2, "rejected", None, "rate_limit")])
    assert rec.classify(short, delivery_evidence=True) == "uncertain"


def test_unknown_earlier_attempt_with_a_known_failed_copy():
    """Has-copy branch: one failed copy, two counted attempts, no evidence
    for the other attempt — it may be a delivered copy."""
    r = _r({"failed": True, "failed_reason": "Spam Rate limit hit"}, attempts=2)
    assert rec.attempt_history(r).unknown_attempts == 1
    assert rec.classify(r, delivery_evidence=True) == "uncertain"
    # A delivered copy stays excluded even though its history is incomplete.
    d = _r({"read": True}, attempts=2)
    assert rec.classify(d, delivery_evidence=True) == "uncertain"
    assert rec.resend_proposal(d, "uncertain") == "excluded_proven_delivery"


def test_more_outcomes_than_counted_attempts_is_not_proof():
    r = _r({"failed": True}, {"failed": True}, attempts=1)   # counter lost an update
    assert not rec.attempt_history(r).complete
    assert rec.classify(r, delivery_evidence=True) == "accepted_multiple_unproven"


def test_log_observations_add_no_coverage():
    r = _r(status="failed", attempts=2, code="rate_limit")
    r.observed_codes += ["rate_limit"]                    # same or earlier attempt — unknown
    assert rec.classify(r, delivery_evidence=True) == "uncertain"
    assert rec.classify(_r(has_log_row=False, status=None, attempts=0), delivery_evidence=False) \
        == "uncertain"


def test_complete_history_controls():
    c = lambda r: rec.classify(r, delivery_evidence=True)  # noqa: E731
    single = _r(status="failed", code="rate_limit")
    assert c(single) == "all_failed"
    assert rec.resend_proposal(single, "all_failed") == "retry_review_candidate"
    ledger_both = _r(status="failed", attempts=2, code="rate_limit",
                     ledger=[(1, "rejected", None, "rate_limit"), (2, "rejected", None, "rate_limit")])
    assert c(ledger_both) == "all_failed"
    assert rec.resend_proposal(ledger_both, "all_failed") == "retry_review_candidate"
    copy_then_rejection = _r({"failed": True}, status="failed", attempts=2, code="rate_limit")
    assert c(copy_then_rejection) == "all_failed"
    assert rec.resend_proposal(copy_then_rejection, "all_failed") == "excluded_meta_restriction"
    delivered_then_rejected = _r({"delivered": True}, status="failed", attempts=2,
                                 code="not_on_whatsapp")
    assert c(delivered_then_rejected) == "delivered_once"
    restricted = _r(status="failed", code="not_on_whatsapp")
    assert rec.resend_proposal(restricted, c(restricted)) == "excluded_meta_restriction"
    mixed = _r(status="failed", attempts=2, code="not_on_whatsapp",
               ledger=[(1, "not_sent", None, "transport_not_sent"),
                       (2, "rejected", None, "not_on_whatsapp")])
    assert c(mixed) == "all_failed"
    assert rec.resend_proposal(mixed, "all_failed") == "excluded_meta_restriction"


def test_unresolved_evidence_keeps_the_recipient_unresolved():
    r = _r(status="failed", code="rate_limit")
    r.unresolved_evidence.append("unapplied inbox receipt for an unknown wamid")
    assert rec.classify(r, delivery_evidence=True) == "uncertain"


# ── R3: any proven delivery excludes the recipient from a resend ────────


def test_proven_delivery_excludes_regardless_of_other_copies():
    r = _r({"delivered": True}, {}, attempts=2)           # one delivered, one no receipt
    assert rec.classify(r, delivery_evidence=True) == "accepted_multiple_unproven"
    assert rec.resend_proposal(r, "accepted_multiple_unproven") == "excluded_proven_delivery"
    r2 = _r({"read": True}, {"failed": True, "failed_reason": "Spam Rate limit hit"}, attempts=2)
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


NOW = "now() at time zone 'utc'"


def _sql(c, sql, **params):
    from sqlalchemy import text
    return c.execute(text(sql), params)


def _log_row(c, i, phone, status, attempts, wamid=None, err=None, dl=False, rd=False, fl=False):
    _sql(c, "INSERT INTO campaign_send_logs (id, tenant_id, campaign_id, customer_phone_e164, "
            "status, attempt_count, provider_message_id, error_code, delivered_at, read_at, "
            f"failed_at, created_at, updated_at) VALUES (:i, 5, 9, :ph, :st, :att, :w, :err, "
            f"CASE WHEN :dl THEN {NOW} END, CASE WHEN :rd THEN {NOW} END, "
            f"CASE WHEN :fl THEN {NOW} END, {NOW}, {NOW})",
         i=i, ph=phone, st=status, att=attempts, w=wamid, err=err, dl=dl, rd=rd, fl=fl)


def _customer_conversation(c, tenant, phone):
    cid = _sql(c, "INSERT INTO customers (tenant_id, name, phone, normalized_phone) "
                  "VALUES (:t, 'نورة عبدالله', :p, :p) RETURNING id", t=tenant, p=phone).scalar()
    return _sql(c, "INSERT INTO conversations (tenant_id, customer_id, status) "
                   "VALUES (:t, :cu, 'active') RETURNING id", t=tenant, cu=cid).scalar()


def _campaign_event(c, wamid, conv=None, tenant=5, campaign=9, **flags):
    md = {"campaign_id": campaign, "wa_message_id": wamid, **flags}
    _sql(c, "INSERT INTO message_events (tenant_id, conversation_id, direction, event_type, "
            "metadata) VALUES (:t, :cv, 'outbound', 'campaign', CAST(:m AS jsonb))",
         t=tenant, cv=conv, m=json.dumps(md))


def _mde(c, wamid, status, log_id=None, tenant=5):
    _sql(c, "INSERT INTO message_delivery_events (tenant_id, wamid, status, campaign_send_log_id, "
            f"suppress_on_repeat, occurred_at, source) VALUES (:t, :w, :s, :l, false, {NOW}, 'meta')",
         t=tenant, w=wamid, s=status, l=log_id)


def _inbox(c, wamid, status, recipient=None, attempt_id=None, applied=False):
    _sql(c, "INSERT INTO campaign_status_event_inbox (provider_message_id, status, recipient_id, "
            f"attempt_id, received_at, applied_at) VALUES (:w, :s, :r, :a, {NOW}, "
            f"CASE WHEN :ap THEN {NOW} END)", w=wamid, s=status, r=recipient, a=attempt_id, ap=applied)


def _attempt(c, log_id, phone, no, state, wamid=None, err=None):
    return _sql(c, "INSERT INTO campaign_send_attempts (tenant_id, campaign_id, send_log_id, "
                   "customer_phone_e164, attempt_no, state, provider_message_id, error_code, "
                   f"claimed_at, created_at, updated_at) VALUES (5, 9, :l, :p, :n, :s, :w, :e, "
                   f"{NOW}, {NOW}, {NOW}) RETURNING id",
                l=log_id, p=phone, n=no, s=state, w=wamid, e=err).scalar()


def _base(c):
    _sql(c, "INSERT INTO tenants (id, name, is_active) VALUES (5, 'متجر تجريبي عام', true), "
            "(6, 'متجر آخر', true)")
    _sql(c, "INSERT INTO campaigns (id, tenant_id, name, campaign_type, status, send_strategy) "
            "VALUES (9, 5, 'حملة عامة', 'broadcast', 'failed', 'immediate'), "
            "(10, 5, 'حملة أخرى', 'broadcast', 'failed', 'immediate')")


def _seed_db(engine):
    """Generic merchant, one campaign, recipients covering every case:
    delivered once · duplicate with one read copy · both copies failed ·
    legacy watchdog failure · proven single rejection · not started ·
    ledger attempt uncertain. The overwritten first copies (w.2a, w.3a) are
    only in message_events, placed through their conversation's customer."""
    with engine.begin() as c:
        _base(c)
        rows = [
            ("+966500000001", "sent", 1, "w.1", None, True, False, False),
            ("+966500000002", "sent", 2, "w.2b", "131048", False, False, True),
            ("+966500000003", "sent", 2, "w.3b", "131048", False, False, True),
            ("+966500000004", "failed", 1, None, "watchdog_timeout", False, False, False),
            ("+966500000005", "failed", 1, None, "rate_limit", False, False, False),
            ("+966500000006", "queued", 0, None, None, False, False, False),
            ("+966500000007", "uncertain", 1, None, "send_outcome_unknown", False, False, False),
        ]
        conv = {}
        for i, (ph, st, att, w, err, dl, rd, fl) in enumerate(rows, start=1):
            _log_row(c, i, ph, st, att, w, err, dl, rd, fl)
            conv[ph] = _customer_conversation(c, 5, ph)
        _campaign_event(c, "w.1", conv["+966500000001"], _status_delivered=True)
        _campaign_event(c, "w.2a", conv["+966500000002"], _status_read=True)
        _campaign_event(c, "w.2b", conv["+966500000002"], _status_failed=True)
        _campaign_event(c, "w.3a", conv["+966500000003"], _status_failed=True)
        _campaign_event(c, "w.3b", conv["+966500000003"], _status_failed=True)
        _attempt(c, 7, "+966500000007", 1, "uncertain")
        # Unrelated tenant / campaign evidence must stay out of the report.
        _customer_conversation(c, 6, "+966500000001")
        _campaign_event(c, "w.other-tenant", None, tenant=6, _status_delivered=True)
        _campaign_event(c, "w.other-campaign", None, campaign=10, _status_delivered=True)


def _run(url, tmp_path, *extra):
    out = tmp_path / "r.json"
    rc = rec.main(["--tenant-id", "5", "--campaign-id", "9", "--database-url", url,
                   "--emit-recipients", "--out", str(out), *extra])
    return rc, json.loads(out.read_text())


def _by_suffix(rep, suffix):
    return next(x for x in rep["recipients"] if x["recipient"].startswith(f"•••{suffix}#"))


@pg
def test_database_mode_complete_run(pgdb, tmp_path):
    url, engine, _ = pgdb
    _seed_db(engine)
    rc, rep = _run(url, tmp_path, "--check-schema")
    assert rc == 0
    assert rep["decision_eligible"] is True, rep["ineligible_reasons"]
    db = rep["sources"]["database"]
    assert {v["status"] for v in db["sources"].values()} == {"complete"}
    assert db["attribution"]["complete"] is True, db["attribution"]
    assert db["preflight"]["schema_ok"] is True
    assert rep["recipients_total"] == 7                  # other tenant/campaign excluded
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


# ── R1 through the database reader ──────────────────────────────────────


@pg
def test_db_summary_and_ledger_rows_of_one_attempt_are_not_two_attempts(pgdb, tmp_path):
    """Recipient 1: two counted attempts, the ledger holds only the second
    (its code copied onto the summary row) — attempt 1 is unrecovered.
    Recipient 2 (control): the ledger holds every counted attempt."""
    url, engine, _ = pgdb
    with engine.begin() as c:
        _base(c)
        _log_row(c, 1, "+966500000011", "failed", 2, err="rate_limit")
        _attempt(c, 1, "+966500000011", 1, "rejected", err="rate_limit")
        _log_row(c, 2, "+966500000012", "failed", 1, err="rate_limit")
        _attempt(c, 2, "+966500000012", 1, "rejected", err="rate_limit")
    rc, rep = _run(url, tmp_path, "--check-schema")
    assert rc == 0 and rep["decision_eligible"] is True, rep["ineligible_reasons"]
    dup = _by_suffix(rep, "0011")
    assert dup["category"] == "uncertain" and dup["resend_proposal"] == "excluded_unresolved"
    assert dup["history_complete"] is False and dup["unknown_attempts"] == 1
    ctl = _by_suffix(rep, "0012")
    assert ctl["category"] == "all_failed" and ctl["resend_proposal"] == "retry_review_candidate"


@pg
def test_db_known_failed_copy_with_an_unrecovered_attempt(pgdb, tmp_path):
    url, engine, _ = pgdb
    with engine.begin() as c:
        _base(c)
        _log_row(c, 1, "+966500000021", "sent", 2, wamid="w.21", err="131048", fl=True)
        _log_row(c, 2, "+966500000022", "failed", 1, err="131099_new_meta_code")
        _log_row(c, 3, "+966500000023", "sent", 1, wamid="w.23", err="131048", fl=True)
    rc, rep = _run(url, tmp_path, "--check-schema")
    assert rc == 0
    assert _by_suffix(rep, "0021")["category"] == "uncertain"
    assert _by_suffix(rep, "0022")["category"] == "uncertain"       # unknown code: no proof
    assert _by_suffix(rep, "0023")["category"] == "all_failed"      # control: complete history


# ── R2: query completion is not attribution ─────────────────────────────


@pg
def test_unmatched_delivered_campaign_event_blocks_eligibility(pgdb, tmp_path):
    url, engine, _ = pgdb
    _seed_db(engine)
    with engine.begin() as c:
        # A campaign-scoped copy with a delivery receipt and no association
        # to any recipient of the campaign.
        _campaign_event(c, "w.lost", None, _status_delivered=True)
    rc, rep = _run(url, tmp_path, "--check-schema")
    assert rc == 0
    assert rep["decision_eligible"] is False
    assert any("not attributed" in x for x in rep["ineligible_reasons"])
    attr = rep["sources"]["database"]["attribution"]
    assert attr["campaign_events_unattributed"] == 1
    assert attr["unattributed_with_delivery_evidence"] == 1
    assert attr["complete"] is False


@pg
def test_copy_recovered_after_initial_reads_gets_its_receipts(pgdb, tmp_path):
    """Recipient 31: a second copy is linked only by a delivery event on its
    send-log row; its read receipt sits in the inbox. Recipient 32: a copy
    known only from an exported accept line; its delivered receipt sits in
    message_delivery_events without a send-log link."""
    url, engine, _ = pgdb
    with engine.begin() as c:
        _base(c)
        _log_row(c, 1, "+966500000031", "sent", 2, wamid="w.31b", err="131048", fl=True)
        _mde(c, "w.31a", "sent", log_id=1)
        _inbox(c, "w.31a", "read", applied=True)
        _log_row(c, 2, "+966500000032", "failed", 2, err="rate_limit")
        _mde(c, "w.32a", "delivered")
    logs = tmp_path / "logs.json"
    logs.write_text(json.dumps({"deploy": [{"timestamp": "1", "message":
        "2026-09-23 10:56:26,291 INFO [campaign_dispatcher] campaign=9 sent OK to "
        "+966500000032 wamid=w.32a"}]}))
    rc, rep = _run(url, tmp_path, "--check-schema", "--log-json", str(logs))
    assert rc == 0, rep
    r31 = _by_suffix(rep, "0031")
    assert r31["has_proven_delivery"] and r31["copies"] == 2
    assert r31["resend_proposal"] == "excluded_proven_delivery"
    r32 = _by_suffix(rep, "0032")
    assert r32["has_proven_delivery"] and r32["resend_proposal"] == "excluded_proven_delivery"
    assert r32["category"] == "delivered_once"          # copy + proven rejection = 2 attempts
    assert rep["sources"]["database"]["attribution"]["copies_added_after_reads"] == 0
    assert rep["decision_eligible"] is True, rep["ineligible_reasons"]


@pg
def test_empty_known_set_still_checks_read_access(pgdb, tmp_path):
    """Nothing was ever accepted: no wamid to look up. A denied evidence
    table must still fail the run, not be reported complete."""
    from sqlalchemy import text
    url, engine, schema = pgdb
    with engine.begin() as c:
        _base(c)
        _log_row(c, 1, "+966500000041", "queued", 0)
        c.execute(text("CREATE ROLE recon_ro_test LOGIN"))
        c.execute(text(f'GRANT USAGE ON SCHEMA "{schema}" TO recon_ro_test'))
        c.execute(text(f'GRANT SELECT ON ALL TABLES IN SCHEMA "{schema}" TO recon_ro_test'))
        c.execute(text("REVOKE SELECT ON campaign_status_event_inbox FROM recon_ro_test"))
    ro_url = url.replace("postgres@", "recon_ro_test@", 1)
    rc, rep = _run(ro_url, tmp_path)
    assert rc == 2 and rep["decision_eligible"] is False
    st = rep["sources"]["database"]["sources"]["campaign_status_event_inbox"]
    assert st["status"] == "error" and "InsufficientPrivilege" in st["error"]
    # With access, the empty lookups run and the report is complete.
    rc, rep = _run(url, tmp_path, "--check-schema")
    assert rc == 0 and rep["decision_eligible"] is True, rep["ineligible_reasons"]
    assert rep["recipients_by_category"]["not_started"] == 1


@pg
def test_unapplied_inbox_receipt_keeps_the_recipient_unresolved(pgdb, tmp_path):
    url, engine, _ = pgdb
    with engine.begin() as c:
        _base(c)
        _log_row(c, 1, "+966500000051", "failed", 1, err="rate_limit")
        _inbox(c, "w.unknown", "delivered", recipient="966500000051")
    rc, rep = _run(url, tmp_path, "--check-schema")
    assert rc == 0
    r = _by_suffix(rep, "0051")
    assert r["category"] == "uncertain" and r["resend_proposal"] == "excluded_unresolved"
    assert rep["sources"]["database"]["attribution"]["recipients_with_pending_inbox_candidates"] == 1


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
