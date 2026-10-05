"""Known defect, recorded as a strict expected failure: the real server booted on
a FRESH, EMPTY review database never brings it to the pinned normal bootstrap
target (0093).

Observed on PostgreSQL 16.15 and 18.6 with throw-away databases. First boot: the
background ``create_all`` creates ``tenants`` before Alembic's 0001 runs, so the
bootstrap upgrade fails ("relation ... already exists"), is logged and skipped,
and ``alembic_version`` stays absent. Second boot: Step B finds ``tenants``
without ``alembic_version``, stamps 0016, and the upgrade then fails on columns
``create_all`` already added. The worker keeps serving and logs "Bootstrap
completed cleanly." both times, but the database is not at 0093. This is the
existing platform bootstrap (``backend/main.py``), not the review guard; the
runbook therefore requires a separate, approved schema step before the first
boot, proven by ``test_review_provisioning_premigration_then_boot_serves_with_the_complete_schema``.

``strict=True``: if the bootstrap is ever fixed this test starts passing and the
run fails until the marker is removed. Not part of the required PostgreSQL
proofs inventory (an expected failure is not a proof). Skipped without a
PostgreSQL admin DSN, like the module it reuses.
"""
from __future__ import annotations

import pytest

from test_review_environment_guard_pg import (  # noqa: F401 — fixtures are used by name
    APP_PASSWORD,
    _alive,
    _public_tables,
    _run_server,
    _scalar,
    _stop,
    boot_db,
    pg,
    pytestmark,
)


@pytest.mark.xfail(strict=True, reason="fresh empty database: create_all races Alembic 0001; boot never reaches 0093")
def test_fresh_empty_marked_database_reaches_0093_by_booting(pg, boot_db, tmp_path):
    done = "[BOOT/db] Bootstrap completed cleanly."
    for n in (1, 2):
        rc, out, port, proc = _run_server(pg, boot_db, tmp_path / f"boot{n}.log", until=done)
        try:
            assert rc is None and done in out, out[-3000:]
            assert _alive(port) == 200
        finally:
            _stop(proc)
        assert APP_PASSWORD not in out
    assert "alembic_version" in _public_tables(pg, boot_db)
    assert _scalar(pg["app_url"](boot_db), "SELECT version_num FROM alembic_version") == "0093"
