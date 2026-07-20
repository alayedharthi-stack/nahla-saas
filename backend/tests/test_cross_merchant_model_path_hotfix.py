"""Unit regressions for cross-merchant model_path schema/session hotfix."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for p in (str(REPO_ROOT), str(BACKEND_DIR), str(DATABASE_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from database.models import CrossMerchantSignal  # noqa: E402
from modules.ai.security import (  # noqa: E402
    CrossMerchantLearningStore,
    LearningTier,
    ModelPathTooLongError,
    OutcomeKind,
    MODEL_PATH_MAX_LENGTH,
    TraceEvent,
    UIMode,
    anonymize_tenant,
    validate_anonymized,
)

GENERIC_MERCHANT_INDUSTRY = "apparel"
PATH_40 = "generic_catalog_product_availability_chk"
PATH_47 = "generic_perfume_catalog_availability_compose_v2"


def _event(*, model_path: str) -> TraceEvent:
    return TraceEvent(
        tenant_hash=anonymize_tenant(42, salt="generic-commerce-fixture"),
        industry=GENERIC_MERCHANT_INDUSTRY,
        intent="ask_product",
        action="search_products",
        ui_mode=UIMode.LIST,
        outcome=OutcomeKind.PRODUCT_PRESENTED,
        model_path=model_path,
        tier=LearningTier.GLOBAL,
    )


class TestModelPathContract:
    def test_legitimate_40_char_path_validates(self) -> None:
        assert len(PATH_40) == 40
        clean = validate_anonymized(_event(model_path=PATH_40))
        assert clean.model_path == PATH_40

    def test_legitimate_47_char_path_validates(self) -> None:
        assert len(PATH_47) == 47
        clean = validate_anonymized(_event(model_path=PATH_47))
        assert clean.model_path == PATH_47

    def test_oversize_path_fails_closed_without_truncation(self) -> None:
        oversize = "x" * (MODEL_PATH_MAX_LENGTH + 1)
        with pytest.raises(ModelPathTooLongError) as exc_info:
            validate_anonymized(_event(model_path=oversize))
        assert exc_info.value.actual_length == MODEL_PATH_MAX_LENGTH + 1
        assert exc_info.value.max_length == MODEL_PATH_MAX_LENGTH

    def test_orm_model_path_length_matches_contract(self) -> None:
        column = CrossMerchantSignal.__table__.c.model_path
        assert column.type.length == MODEL_PATH_MAX_LENGTH

    @pytest.mark.parametrize(
        "migration_name",
        (
            "0090_cross_merchant_model_path_128.py",
            "0091_cross_merchant_model_path_128.py",
        ),
    )
    def test_sibling_downgrades_are_schema_no_ops(
        self,
        migration_name: str,
        monkeypatch,
    ) -> None:
        migration_path = (
            REPO_ROOT / "database" / "migrations" / "versions" / migration_name
        )
        spec = importlib.util.spec_from_file_location(
            f"test_{migration_name.removesuffix('.py')}",
            migration_path,
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        monkeypatch.setattr(
            module.op,
            "alter_column",
            lambda *args, **kwargs: pytest.fail("downgrade attempted schema narrowing"),
        )

        module.downgrade()


class TestCrossMerchantStoreIsolation:
    def test_record_uses_isolated_session_not_operational_db(self, monkeypatch) -> None:
        monkeypatch.setattr(
            CrossMerchantLearningStore, "is_enabled", staticmethod(lambda: True)
        )

        operational_db = MagicMock()
        telemetry_db = MagicMock()
        telemetry_db.commit.return_value = None
        row_holder: dict = {}

        class _Signal:
            def __init__(self, **kwargs):
                row_holder.update(kwargs)
                self.id = 77

        monkeypatch.setattr(
            "database.models.CrossMerchantSignal",
            _Signal,
            raising=False,
        )
        monkeypatch.setattr(
            "modules.ai.security.cross_merchant_store.TenantIsolationLayer.is_cross_tenant_safe",
            staticmethod(lambda _model: True),
        )

        store = CrossMerchantLearningStore(
            operational_db,
            session_factory=lambda: telemetry_db,
        )
        result = store.record(_event(model_path=PATH_47))

        assert result == 77
        operational_db.add.assert_not_called()
        telemetry_db.add.assert_called_once()
        telemetry_db.commit.assert_called_once()
        telemetry_db.close.assert_called_once()
        assert row_holder["model_path"] == PATH_47

    def test_oversize_path_rejected_without_touching_sessions(self, monkeypatch) -> None:
        monkeypatch.setattr(
            CrossMerchantLearningStore, "is_enabled", staticmethod(lambda: True)
        )
        operational_db = MagicMock()
        telemetry_db = MagicMock()

        store = CrossMerchantLearningStore(
            operational_db,
            session_factory=lambda: telemetry_db,
        )
        oversize = "y" * (MODEL_PATH_MAX_LENGTH + 5)
        result = store.record(_event(model_path=oversize))

        assert result is None
        operational_db.add.assert_not_called()
        telemetry_db.add.assert_not_called()

    def test_other_anonymization_failures_still_surface(self, monkeypatch) -> None:
        monkeypatch.setattr(
            CrossMerchantLearningStore, "is_enabled", staticmethod(lambda: True)
        )
        event = _event(model_path=PATH_40)
        event.extra = {"phone": "+966500000000"}

        with pytest.raises(ValueError, match="forbidden key"):
            CrossMerchantLearningStore(
                MagicMock(),
                session_factory=MagicMock(),
            ).record(event)

    def test_removed_commit_false_api_fails_explicitly(self, monkeypatch) -> None:
        monkeypatch.setattr(
            CrossMerchantLearningStore, "is_enabled", staticmethod(lambda: True)
        )
        store = CrossMerchantLearningStore(MagicMock())

        with pytest.raises(TypeError, match="commit"):
            store.record(_event(model_path=PATH_40), commit=False)  # type: ignore[call-arg]

    def test_telemetry_insert_failure_leaves_operational_session_usable(self) -> None:
        metadata = MetaData()
        tenants = Table(
            "tenants",
            metadata,
            Column("id", Integer, primary_key=True),
            Column("name", String(64), nullable=False),
        )
        engine = create_engine("sqlite:///:memory:")
        metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        operational = Session()
        operational.execute(tenants.insert().values(id=1, name="متجر تجريبي عام"))
        operational.flush()

        telemetry_db = MagicMock()
        telemetry_db.add.side_effect = RuntimeError("simulated_insert_failure")
        telemetry_db.rollback.return_value = None
        telemetry_db.close.return_value = None

        monkeypatch_enabled = pytest.MonkeyPatch()
        monkeypatch_enabled.setattr(
            CrossMerchantLearningStore, "is_enabled", staticmethod(lambda: True)
        )
        monkeypatch_enabled.setattr(
            "modules.ai.security.cross_merchant_store.TenantIsolationLayer.is_cross_tenant_safe",
            staticmethod(lambda _model: True),
        )
        try:
            store = CrossMerchantLearningStore(
                operational,
                session_factory=lambda: telemetry_db,
            )
            assert store.record(_event(model_path=PATH_40)) is None

            operational.execute(
                tenants.update().where(tenants.c.id == 1).values(name="متجر تجريبي عام v2")
            )
            operational.flush()
            row = operational.execute(
                tenants.select().where(tenants.c.id == 1)
            ).first()
            assert row is not None
            assert row.name == "متجر تجريبي عام v2"
        finally:
            monkeypatch_enabled.undo()
            operational.close()
