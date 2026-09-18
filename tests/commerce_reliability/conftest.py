"""Commerce Conversation Reliability Gate — pytest wiring.

Reconciliation hooks and the ``baseline`` / ``unimplemented`` fixtures come
from :mod:`tests.commerce_reliability.reliability_plugin`; this file adds the
application-facing fixtures (in-memory catalog database, disposable
PostgreSQL database) and nothing else.
"""
from __future__ import annotations

import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _entry in reversed([str(_REPO_ROOT), str(_REPO_ROOT / "backend"), str(_REPO_ROOT / "database")]):
    if _entry in sys.path:
        sys.path.remove(_entry)
    sys.path.insert(0, _entry)

from tests.commerce_reliability import reliability_evaluator as ev  # noqa: E402
from tests.commerce_reliability.reliability_plugin import (  # noqa: E402,F401
    BaselineDefect,
    UnimplementedContract,
    baseline,
    pytest_configure,
    pytest_runtest_makereport,
    reliability_manifest,
    unimplemented,
)
from tests.commerce_reliability import runtime_support as rs  # noqa: E402

__all__ = [
    "BaselineDefect", "UnimplementedContract", "baseline", "catalog_sqlite", "disposable_pg",
    "pg_admin_dsn", "pytest_configure", "pytest_runtest_makereport", "reliability_manifest",
    "unimplemented",
]


# ── In-memory catalog database (sqlite, JSONB swapped for JSON) ─────────────


@pytest.fixture
def catalog_sqlite(request: pytest.FixtureRequest) -> Iterator[rs.CatalogFixture]:
    fixture = rs.build_sqlite_catalog()
    try:
        yield fixture
    finally:
        fixture.close()


# ── Disposable PostgreSQL ────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def pg_admin_dsn() -> str:
    """Admin DSN for creating the disposable database.

    Fails (never skips) when ``NAHLA_RELIABILITY_REQUIRE_PG=1`` is set without
    ``NAHLA_RELIABILITY_PG_ADMIN_DSN``; skips only when the PostgreSQL tier
    was not requested at all (developer default run).
    """
    cfg = ev.resolve_postgres_config(os.environ)
    if cfg["error"]:
        pytest.fail(cfg["error"], pytrace=False)
    if not cfg["dsn"]:
        pytest.skip(
            "PostgreSQL tier not requested: set NAHLA_RELIABILITY_REQUIRE_PG=1 and "
            "NAHLA_RELIABILITY_PG_ADMIN_DSN to run it"
        )
    return str(cfg["dsn"])


@dataclass
class DisposablePostgres:
    name: str
    dsn: str
    engine: Any
    session_factory: Callable[[], Any]

    def session(self) -> Any:
        return self.session_factory()


@pytest.fixture(scope="session")
def disposable_pg(pg_admin_dsn: str) -> Iterator[DisposablePostgres]:
    """One disposable UTF-8 database per session; dropped WITH (FORCE) at exit."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    import models as M  # noqa: PLC0415 — the application's own model registry

    name = "nahla_reliability_" + uuid.uuid4().hex[:10]
    admin = create_engine(pg_admin_dsn, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        conn.execute(text(f"CREATE DATABASE \"{name}\" ENCODING 'UTF8' TEMPLATE template0"))
    dsn = pg_admin_dsn.rsplit("/", 1)[0] + "/" + name
    engine = create_engine(dsn, connect_args={"options": "-c client_encoding=utf8"})
    M.Base.metadata.create_all(engine)
    handle = DisposablePostgres(
        name=name, dsn=dsn, engine=engine, session_factory=sessionmaker(bind=engine),
    )
    try:
        yield handle
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()
