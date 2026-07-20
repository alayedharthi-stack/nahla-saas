"""PostgreSQL migration/integration tests for cross_merchant model_path widen."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

_REPO = Path(__file__).resolve().parents[2]
_DATABASE = _REPO / "database"
_BACKEND = _REPO / "backend"
for entry in (str(_REPO), str(_BACKEND), str(_DATABASE)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from database.models import CrossMerchantSignal, Tenant  # noqa: E402
from modules.ai.security import (  # noqa: E402
    CrossMerchantLearningStore,
    LearningTier,
    MODEL_PATH_MAX_LENGTH,
    OutcomeKind,
    TraceEvent,
    UIMode,
    anonymize_tenant,
)
from modules.ai.security.trace_schema import normalize_model_path  # noqa: E402
from tests.order_customer_identity_postgres_fixtures import _connect_engine  # noqa: E402

PATH_40 = "generic_catalog_product_availability_chk"
PATH_47 = "generic_perfume_catalog_availability_compose_v2"


def _alembic_config(engine: Engine) -> Config:
    cfg = Config(str(_DATABASE / "alembic.ini"))
    url = str(engine.url.render_as_string(hide_password=False))
    cfg.set_main_option("script_location", str(_DATABASE / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    os.environ["DATABASE_URL"] = url
    return cfg


def _upgrade(engine: Engine, revision: str) -> None:
    command.upgrade(_alembic_config(engine), revision)


def _downgrade(engine: Engine, revision: str) -> None:
    command.downgrade(_alembic_config(engine), revision)


@pytest.fixture(scope="module")
def pg_engine() -> Iterator[Engine]:
    engine = _connect_engine()
    _upgrade(engine, "0091")
    yield engine
    engine.dispose()


def _event(*, model_path: str) -> TraceEvent:
    return TraceEvent(
        tenant_hash=anonymize_tenant(991_042, salt="generic-commerce-pg"),
        industry="cosmetics",
        intent="ask_product",
        action="search_products",
        ui_mode=UIMode.LIST,
        outcome=OutcomeKind.PRODUCT_PRESENTED,
        model_path=model_path,
        tier=LearningTier.GLOBAL,
    )


def test_migration_0091_widens_model_path_column(pg_engine: Engine) -> None:
    with pg_engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT character_maximum_length
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'cross_merchant_signals'
                  AND column_name = 'model_path'
                """
            )
        ).first()
    assert row is not None
    assert int(row[0]) == MODEL_PATH_MAX_LENGTH


def test_migration_0090_sibling_branch_widens_model_path(pg_engine: Engine) -> None:
    _upgrade(pg_engine, "0090")
    insp = inspect(pg_engine)
    cols = {c["name"]: c for c in insp.get_columns("cross_merchant_signals")}
    assert cols["model_path"].get("type").length == MODEL_PATH_MAX_LENGTH


def test_downgrading_one_sibling_keeps_shared_widen_and_data(pg_engine: Engine) -> None:
    long_path = PATH_47
    with pg_engine.begin() as conn:
        row_id = conn.execute(
            text(
                """
                INSERT INTO cross_merchant_signals (
                    tenant_hash, industry, intent, action, ui_mode, outcome,
                    value_bucket, turn_index, model_path, latency_ms, tier
                ) VALUES (
                    'forwardonlytest', 'apparel', 'ask_product',
                    'search_products', 'list', 'product_presented', 'unknown',
                    0, :model_path, 0, 'global'
                )
                RETURNING id
                """
            ),
            {"model_path": long_path},
        ).scalar_one()

    # Resolve -1 relative to the 0090 head only. 0091 remains applied.
    _downgrade(pg_engine, "0090-1")

    with pg_engine.connect() as conn:
        revisions = {
            row[0] for row in conn.execute(text("SELECT version_num FROM alembic_version"))
        }
        column_length = conn.execute(
            text(
                """
                SELECT character_maximum_length
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'cross_merchant_signals'
                  AND column_name = 'model_path'
                """
            )
        ).scalar_one()
        persisted_path = conn.execute(
            text("SELECT model_path FROM cross_merchant_signals WHERE id = :id"),
            {"id": row_id},
        ).scalar_one()

    assert revisions == {"0088", "0091"}
    assert int(column_length) == MODEL_PATH_MAX_LENGTH
    assert persisted_path == long_path


def test_legitimate_paths_persist_on_postgres(pg_engine: Engine) -> None:
    Session = sessionmaker(bind=pg_engine)
    operational = Session()
    try:
        for path in (PATH_40, PATH_47):
            assert len(path) in (40, 47)
            store = CrossMerchantLearningStore(operational)
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(
                    CrossMerchantLearningStore,
                    "is_enabled",
                    staticmethod(lambda: True),
                )
                row_id = store.record(_event(model_path=path))
            assert row_id is not None
            saved = (
                operational.query(CrossMerchantSignal)
                .filter(CrossMerchantSignal.id == row_id)
                .one()
            )
            assert saved.model_path == normalize_model_path(path)
    finally:
        operational.close()


def test_telemetry_failure_does_not_poison_operational_session(pg_engine: Engine) -> None:
    Session = sessionmaker(bind=pg_engine)
    operational = Session()
    try:
        operational.merge(
            Tenant(id=991_043, name="متجر تجريبي عام", is_active=True)
        )
        operational.flush()

        failing_session = MagicMockTelemetrySession()

        store = CrossMerchantLearningStore(
            operational,
            session_factory=lambda: failing_session,
        )
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                CrossMerchantLearningStore,
                "is_enabled",
                staticmethod(lambda: True),
            )
            assert store.record(_event(model_path=PATH_47)) is None

        operational.execute(
            text("UPDATE tenants SET name = :name WHERE id = :id"),
            {"name": "متجر تجريبي عام محدث", "id": 991_043},
        )
        operational.flush()
        name = operational.execute(
            text("SELECT name FROM tenants WHERE id = :id"),
            {"id": 991_043},
        ).scalar_one()
        assert name == "متجر تجريبي عام محدث"
    finally:
        operational.rollback()
        operational.close()


class MagicMockTelemetrySession:
    def add(self, _row: object) -> None:
        raise RuntimeError("simulated_telemetry_insert_failure")

    def commit(self) -> None:
        raise RuntimeError("simulated_telemetry_insert_failure")

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        return None

    def flush(self) -> None:
        raise RuntimeError("simulated_telemetry_insert_failure")
