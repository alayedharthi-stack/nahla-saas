"""Running Alembic in this process must not take the application's logging with it.

Alembic's ``env.py`` calls ``logging.config.fileConfig``. Its default,
``disable_existing_loggers=True``, sets ``disabled = True`` on every logger
object that already exists and is not named in ``alembic.ini``. Logger objects
are process-global singletons, so the silence outlives the migration, the test
that ran it and the module it lived in.

Production is not reached by this and the claim is bounded to what was checked:
``backend/main.py`` runs its bootstrap upgrade through ``subprocess.run``, a
separate process, so ``fileConfig`` there cannot touch this process's loggers.
That covers the boot path only. Any other in-process Alembic entry point would
need its own check.

The tests do run it in-process: ``legacy_migration_drift_postgres_fixtures``
calls ``command.upgrade`` directly, so every test afterwards inherits silenced
loggers. That is worse than a visible failure. A test written to prove a secret
never reaches the log reads an empty capture and passes — it stops testing
anything while still reporting success.

So this pins the half that makes the other half mean anything: the record must
**arrive**. It deliberately claims no more. Nothing here injects a secret into a
logging or redaction path, so this file is not evidence that redaction works —
that belongs to the privacy suites that exercise that path, and their negative
assertions only mean something while capture is alive, which is what this
guards.
"""
from __future__ import annotations

import logging
import os
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

_REPO = Path(__file__).resolve().parents[2]
for _p in (_REPO, _REPO / "backend", _REPO / "database", _REPO / "backend" / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from legacy_migration_drift_postgres_fixtures import (  # noqa: E402
    connect_engine,
    create_ephemeral_database,
    drop_ephemeral_database,
    run_alembic,
)

# The migration only has to *run*; the damage is done when ``env.py`` is
# imported, not by any particular revision.
_UPGRADE_TARGET = "0001"


def _admin_or_skip():
    try:
        return connect_engine()
    except pytest.skip.Exception:
        if os.getenv("LEGACY_MIG_PG_TEST_DATABASE_URL", "").strip():
            pytest.fail("PostgreSQL configured but unavailable for the logging isolation proof")
        pytest.skip("PostgreSQL DSN is not configured")


@contextmanager
def _ephemeral_database():
    admin = _admin_or_skip()
    db_name, _ = create_ephemeral_database(admin)
    engine = create_engine(
        str(admin.url.set(database=db_name).render_as_string(hide_password=False)),
        poolclass=NullPool,
    )
    try:
        yield engine
    finally:
        engine.dispose()
        drop_ephemeral_database(admin, db_name)
        admin.dispose()


def test_an_application_record_survives_an_in_process_migration(caplog) -> None:
    """The real application logger, after a real in-process upgrade.

    ``nahla.brain.trusted_context`` is the object
    ``backend/modules/ai/brain/truth_surface/trusted_context.py`` emits its
    ``[TRUSTED_CONTEXT_SHADOW] build_failed … stage=coupon_promotion_loader``
    warning through — the very line whose disappearance surfaced this. It is
    imported from the application module rather than rebuilt here, so the test
    holds the object the application actually uses.
    """
    from modules.ai.brain.truth_surface.trusted_context import (  # noqa: PLC0415
        logger as application_logger,
    )

    with _ephemeral_database() as engine:
        run_alembic(engine, _UPGRADE_TARGET)

        with caplog.at_level(logging.WARNING, logger=application_logger.name):
            application_logger.warning(
                "[TRUSTED_CONTEXT_SHADOW] build_failed tenant=%s stage=%s error_class=%s",
                4242, "coupon_promotion_loader", "RuntimeError",
            )

    # It arrived. On the unfixed behaviour this is where it stops: the logger
    # object was disabled by the migration and emitted nothing at all.
    assert not application_logger.disabled, (
        "the in-process migration disabled the application logger; every log "
        "assertion after it is now vacuous"
    )
    captured = caplog.text
    assert "stage=coupon_promotion_loader" in captured
    assert "RuntimeError" in captured


def test_the_guard_reports_capture_death_rather_than_a_clean_log(caplog) -> None:
    """The failure mode this guard exists to distinguish.

    Disabling the logger by hand reproduces exactly what the migration did.
    Every ``not in`` assertion a privacy test could make still holds — that is
    the point, and it is why absence is worthless on its own — while the
    arrival check fails. Without an arrival check, a suite reports success over
    a log nobody was writing to.
    """
    from modules.ai.brain.truth_surface.trusted_context import (  # noqa: PLC0415
        logger as application_logger,
    )

    previously = application_logger.disabled
    try:
        application_logger.disabled = True
        with caplog.at_level(logging.WARNING, logger=application_logger.name):
            application_logger.warning("[TRUSTED_CONTEXT_SHADOW] build_failed stage=%s", "x")
        captured = caplog.text
        # Both hold, and neither means anything: nothing was captured at all.
        assert "secret-value" not in captured
        assert "stage=" not in captured
    finally:
        application_logger.disabled = previously
