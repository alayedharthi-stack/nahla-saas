"""The synthetic probe end to end on real PostgreSQL with the scripted provider.

What is proved: the probe creates and drops its own database on the admin
server, migrates it with the repository's chain, seeds a generic merchant,
runs every case through the pilot's own entry point with the real read tools
and the platform's wire sanitiser, replays each inbound without a second
send, reserves no commerce effect, meets every expectation, and prints
nothing that came from a secret-bearing environment variable.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in [str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")]:
    if p not in sys.path:
        sys.path.insert(0, p)

from scripts.operators import commerce_runtime_synthetic_probe as probe  # noqa: E402

pytestmark = pytest.mark.usefixtures("pg_admin_dsn")


def _databases(admin_dsn: str) -> set:
    admin = create_engine(admin_dsn, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            return {row[0] for row in conn.execute(
                text("SELECT datname FROM pg_database WHERE datname LIKE 'nahla_synthetic_probe_%'"))}
    finally:
        admin.dispose()


def test_the_scripted_probe_runs_every_case_and_leaves_nothing_behind(pg_admin_dsn: str, monkeypatch,
                                                                       capsys) -> None:
    monkeypatch.setenv(probe.ADMIN_DSN_ENV, pg_admin_dsn)
    monkeypatch.setenv("SOME_FAKE_API_KEY", "fake-secret-value-for-the-scrubber")
    before = _databases(pg_admin_dsn)

    exit_code = probe.main(["--provider", "scripted"])

    out = capsys.readouterr().out
    assert exit_code == probe.EXIT_OK, out
    results = [json.loads(line[len(probe.RESULT_PREFIX):]) for line in out.splitlines()
               if line.startswith(probe.RESULT_PREFIX)]
    summaries = [json.loads(line[len(probe.SUMMARY_PREFIX):]) for line in out.splitlines()
                 if line.startswith(probe.SUMMARY_PREFIX)]
    assert [r["case"] for r in results] == [c.name for c in probe.CASES]
    assert len(summaries) == 1
    summary = summaries[0]
    assert summary["duplicate_sends"] == 0 and summary["commerce_effects"] == 0
    assert summary["unmet_expectations"] == {}
    assert summary["real_whatsapp_transport"] is False and summary["production_customer_rows_used"] is False
    assert summary["real_runtime_and_tools"] is True and summary["real_postgresql"] is True

    listing = next(r for r in results if r["case"] == "listing_with_links")
    assert listing["wire_equals_reserved_intent"] is True and listing["wire_sanitised"] is False
    assert listing["wire_hosts"] == ["demo-probe.example-store.sa"]
    assert listing["sanitizer_audit_lines"] == 1 and listing["sanitizer_blocked_lines"] == 0
    bundle = next(r for r in results if r["case"] == "details_bundle")
    assert bundle["tools_called"].count("get_product_details") == 4
    coupon = next(r for r in results if r["case"] == "coupon")
    assert coupon["evidence_refs"].startswith("promotion:coupon:")
    assert all(r["replay_transport_calls"] == 0 and r["replay_reason"] == "turn_already_terminal"
               for r in results)

    # Output hygiene: no configured secret and no DSN password in any line.
    assert "fake-secret-value-for-the-scrubber" not in out
    password = pg_admin_dsn.split("://", 1)[1].split("@", 1)[0].split(":", 1)[-1]
    if password:
        assert password not in out
    # The disposable database is gone, and nothing else of the probe's is left.
    assert "SYNTHETIC_DATABASE_REMOVED=" in out
    assert _databases(pg_admin_dsn) == before


def test_the_probe_drops_its_database_even_when_a_case_fails(pg_admin_dsn: str, monkeypatch, capsys) -> None:
    monkeypatch.setenv(probe.ADMIN_DSN_ENV, pg_admin_dsn)
    before = _databases(pg_admin_dsn)

    def broken(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        raise RuntimeError("seed exploded")

    monkeypatch.setattr(probe, "seed_merchant", broken)
    exit_code = probe.main(["--provider", "scripted", "--cases", "greeting"])
    captured = capsys.readouterr()
    assert exit_code == probe.EXIT_FAILED
    assert "seed exploded" in captured.err
    assert "SYNTHETIC_DATABASE_REMOVED=" in captured.out
    assert _databases(pg_admin_dsn) == before
    assert os.environ.get("DATABASE_URL") in (None, "") or "nahla_synthetic_probe_" not in str(
        os.environ.get("DATABASE_URL"))
