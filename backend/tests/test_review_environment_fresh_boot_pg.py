"""Known defect, recorded as a strict expected failure: the real server booted on
a FRESH, EMPTY review database did not bring it to the pinned normal bootstrap
target (0093) in any observed run.

Observed on PostgreSQL 16.15 and 18.6 with throw-away databases, and identically
on ``main`` without the review guard. First boot: the background ``create_all``
races Alembic's 0001, so the bootstrap upgrade fails — ``DuplicateTable`` in some
runs, ``UniqueViolation`` on ``pg_type_typname_nsp_index`` (concurrent CREATE
TABLE) in others — is logged and skipped, and ``alembic_version`` stays absent.
Second boot: Step B finds ``tenants`` without ``alembic_version``, stamps 0016,
and the upgrade then fails with ``DuplicateColumn`` on columns ``create_all``
already added. The worker keeps serving and logs "Bootstrap completed cleanly."
both times, but the database is not at 0093. This is the existing platform
bootstrap (``backend/main.py``), not the review guard; the runbook therefore
requires a separate, approved schema step before the first boot, proven by
``test_review_provisioning_premigration_then_boot_serves_with_the_complete_schema``.

The test asserts those observed markers first and raises
``BootNeverReached0093`` only at the final check, and the marker is
``xfail(strict=True, raises=BootNeverReached0093)``: any other failure (a server
that never starts, different markers) is a real failure, and if the first boot
ever wins the race or the bootstrap is fixed, the test XPASSes and the run fails
until the marker is removed. Not part of the required PostgreSQL proofs
inventory (an expected failure is not a proof) and not collected by any CI job
(the root run collects ``tests/`` and sets no PostgreSQL DSN), so it runs only
where a DSN is provided by hand. Skipped without a PostgreSQL admin DSN.
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


class BootNeverReached0093(AssertionError):
    """The documented outcome: two boots, still no ``alembic_version`` at 0093."""


@pytest.mark.xfail(strict=True, raises=BootNeverReached0093,
                   reason="fresh empty database: create_all races Alembic 0001; boot did not reach 0093 in any observed run")
def test_fresh_empty_marked_database_reaches_0093_by_booting(pg, boot_db, tmp_path):
    done = "[BOOT/db] Bootstrap completed cleanly."
    outputs = []
    for n in (1, 2):
        rc, out, port, proc = _run_server(pg, boot_db, tmp_path / f"boot{n}.log", until=done)
        try:
            assert rc is None and done in out, out[-3000:]
            assert _alive(port) == 200
        finally:
            _stop(proc)
        assert APP_PASSWORD not in out
        outputs.append(out)
    first, second = outputs
    if "Step C: alembic upgrade 0093 OK rc=0" not in first:
        assert "Step C FAILED" in first, first[-3000:]
        assert "DuplicateTable" in first or "UniqueViolation" in first, first[-3000:]
        assert "stamping to 0016" in second and "DuplicateColumn" in second, second[-3000:]
    versions = None
    if "alembic_version" in _public_tables(pg, boot_db):
        versions = _scalar(pg["app_url"](boot_db), "SELECT string_agg(version_num, ',') FROM alembic_version")
    if versions != "0093":
        raise BootNeverReached0093(f"alembic_version after two boots: {versions!r}")
