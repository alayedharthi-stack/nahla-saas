"""Create a fresh disposable database only on an explicitly known loopback PG.

The administrator URL must name the built-in postgres database. CI uses its
pinned PostgreSQL 18 service on 127.0.0.1:5433; local development uses the
separately initialized test cluster on 127.0.0.1:55491. No arbitrary host,
port, or database name is accepted.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
import uuid

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


def admin_url():
    raw = os.environ.get("SALLA_COMM_TEST_PG_URL", "")
    if not raw:
        raise RuntimeError("isolated_postgres_url_missing")
    url = make_url(raw)
    if url.drivername not in {"postgresql", "postgresql+psycopg2"}:
        raise RuntimeError("isolated_postgres_driver_required")
    if url.host != "127.0.0.1" or url.port not in {5433, 55491} or url.database != "postgres":
        raise RuntimeError("isolated_postgres_target_required")
    if url.port == 5433 and not (
        os.environ.get("GITHUB_ACTIONS") == "true"
        and os.environ.get("CI") == "true"
        and os.environ.get("NAHLA_RELIABILITY_REQUIRE_PG") == "1"
        and raw == os.environ.get("NAHLA_RELIABILITY_PG_ADMIN_DSN")
    ):
        raise RuntimeError("ci_postgres_service_context_required")
    return url


@contextmanager
def fresh_database():
    """Yield a unique empty DB, then remove exactly that DB in all outcomes."""
    admin = admin_url()
    name = f"salla_comm_{uuid.uuid4().hex[:16]}"
    engine = create_engine(admin, isolation_level="AUTOCOMMIT", connect_args={"connect_timeout": 3})
    created = False
    try:
        with engine.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
            created = True
        yield admin.set(database=name), name
    finally:
        if created:
            with engine.connect() as conn:
                conn.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        engine.dispose()
