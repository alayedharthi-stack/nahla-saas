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
    assert c(_r(status="failed"), delivery_evidence=True) == "all_failed"
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
