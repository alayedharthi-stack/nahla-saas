"""The read-only budget audit recomputes the guard's ``used_24h`` from the
rows that produced it, per source. PostgreSQL only."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import test_campaign_send_reconciliation as recon_tests
from test_campaign_send_reconciliation import _base, _log_row, _sql, pg

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "campaign_messaging_budget_audit",
    ROOT / "scripts/operators/campaign_messaging_budget_audit.py",
)
audit = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = audit
_spec.loader.exec_module(audit)

pgdb = recon_tests.pgdb
AT = "2026-09-24T09:53:49"


def _seed(engine):
    """A generic merchant: yesterday's legacy campaign sends (inside the
    window, most failed after acceptance), one older send (outside), and
    ledger attempts for today's campaign."""
    with engine.begin() as c:
        _base(c)
        _sql(c, "INSERT INTO whatsapp_connections (tenant_id, status, whatsapp_business_account_id, "
                "phone_number_id, meta_messaging_limit, meta_tier_updated_at, provider) VALUES "
                "(5, 'connected', 'WABA-GEN', 'PN-GEN', 'TIER_250', '2026-09-24 08:32:01', 'meta')")
        rows = [
            # id, phone, sent_at, failed, delivered
            (1, "+966500000301", "2026-09-23 10:20:00", True, False),
            (2, "+966500000302", "2026-09-23 10:40:00", True, False),
            (3, "+966500000303", "2026-09-23 11:10:00", False, True),
            (4, "+966500000304", "2026-09-23 11:15:00", False, False),
            (5, "+966500000305", "2026-09-23 08:00:00", False, True),   # older than 24h
        ]
        for i, ph, sent, fl, dl in rows:
            _log_row(c, i, ph, "sent", 1, wamid=f"w.{i}", fl=fl, dl=dl)
            _sql(c, "UPDATE campaign_send_logs SET sent_at = :s, created_at = :s WHERE id = :i",
                 s=sent, i=i)
        _log_row(c, 6, "+966500000306", "queued", 0)
        for n, ph in enumerate(("+966500000303", "+966500000307"), 1):
            _sql(c, "INSERT INTO campaign_send_attempts (tenant_id, campaign_id, send_log_id, "
                    "customer_phone_e164, attempt_no, state, messaging_scope_key, claimed_at, "
                    "request_started_at, created_at, updated_at) VALUES (5, 10, 6, :p, :n, "
                    "'accepted', 'waba:WABA-GEN', '2026-09-24 09:00:00', '2026-09-24 09:00:00', "
                    "'2026-09-24 09:00:00', '2026-09-24 09:00:00')", p=ph, n=n)


@pg
def test_audit_recomputes_used_24h_per_source(pgdb, capsys):
    url, engine, _ = pgdb
    _seed(engine)
    rc = audit.main(["--tenant-id", "5", "--at", AT, "--database-url", url, "--expect-used", "5"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0, out["expect_used"]
    legacy = out["legacy_rule"]
    assert out["since"] == "2026-09-23T09:53:49"
    assert out["connection"]["scope_key"] == "waba:WABA-GEN"
    assert out["connection"]["has_business_manager_id"] is False
    assert legacy["send_logs"]["unique_phones"] == 4          # the 08:00 send is outside
    assert legacy["send_logs"]["oldest"].startswith("2026-09-23T10:20")
    assert legacy["attempts"]["unique_phones"] == 2
    assert legacy["intersection"] == 1
    assert legacy["union_used_24h"] == 5
    assert legacy["union_failed_after_accept_only"] == 2
    assert legacy["union_with_delivery_evidence"] == 1
    assert "+9665" not in json.dumps(out)                    # no recipient printed


@pg
def test_audit_flags_a_guard_number_the_data_does_not_support(pgdb, capsys):
    url, engine, _ = pgdb
    _seed(engine)
    rc = audit.main(["--tenant-id", "5", "--at", AT, "--database-url", url, "--expect-used", "1483"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 5 and out["expect_used"]["matches"] is False
