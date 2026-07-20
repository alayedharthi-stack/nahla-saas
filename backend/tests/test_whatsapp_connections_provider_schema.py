"""Unit regressions for whatsapp_connections.provider schema/session hotfix."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for p in (str(REPO_ROOT), str(BACKEND_DIR), str(DATABASE_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from core.acceptance_execution_context import internal_conversational_e2e_context
from core.merchant_capabilities import resolve_merchant_capabilities
from core.native_catalog_capability import load_whatsapp_connection
from database.models import WhatsAppConnection
from services.internal_conversational_e2e_sql_error_audit import (
    install_internal_e2e_sql_error_listener,
    internal_e2e_sql_error_turn,
    recorded_sql_error_audits,
    reset_session_sql_error_audit,
    summarize_turn_sql_error_audit,
)
from services.whatsapp_platform.provider_utils import WHATSAPP_PROVIDER_META
from whatsapp_connections_provider_helpers import (
    ProviderSchemaContractError,
    _normalized_server_default,
    ensure_whatsapp_connections_provider_column,
)

GENERIC_TENANT_ID = 991_501
GENERIC_MERCHANT_NAME = "متجر تجريبي عام"


class TestProviderOrmContract:
    def test_provider_column_matches_bounded_string_contract(self) -> None:
        column = WhatsAppConnection.__table__.c.provider
        assert column.nullable is False
        assert column.default is not None
        assert column.default.arg == WHATSAPP_PROVIDER_META

    @pytest.mark.parametrize(
        "migration_name",
        (
            "0092_whatsapp_connections_provider.py",
            "0093_whatsapp_connections_provider.py",
        ),
    )
    def test_sibling_downgrades_are_schema_no_ops(self, migration_name: str) -> None:
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

        module.downgrade()

    @pytest.mark.parametrize(
        ("raw", "expected"),
        (
            (None, None),
            ("'meta'::character varying", "meta"),
            ("('meta'::text)", "meta"),
            ("'dialog360'::character varying", "dialog360"),
        ),
    )
    def test_postgres_default_normalization(self, raw, expected) -> None:
        assert _normalized_server_default(raw) == expected

    def test_missing_base_table_fails_closed(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "whatsapp_connections_provider_helpers.op.get_bind",
            lambda: MagicMock(),
        )
        monkeypatch.setattr(
            "whatsapp_connections_provider_helpers.has_table",
            lambda *_args: False,
        )

        with pytest.raises(
            ProviderSchemaContractError,
            match="required_table_missing:whatsapp_connections",
        ):
            ensure_whatsapp_connections_provider_column()


class TestLoadWhatsappConnectionIsolation:
    def test_lookup_uses_isolated_session_not_operational_db(self, monkeypatch) -> None:
        operational_db = MagicMock()
        isolated_db = MagicMock()
        isolated_db.query.return_value.filter.return_value.first.return_value = None

        monkeypatch.setattr(
            "core.native_catalog_capability._open_isolated_read_session",
            lambda _db: isolated_db,
        )

        result = load_whatsapp_connection(operational_db, GENERIC_TENANT_ID)

        assert result is None
        operational_db.query.assert_not_called()
        isolated_db.query.assert_called_once()
        isolated_db.close.assert_called_once()

    def test_lookup_failure_rolls_back_only_isolated_session(self, monkeypatch) -> None:
        operational_db = MagicMock()
        isolated_db = MagicMock()
        isolated_db.query.side_effect = ProgrammingError(
            "SELECT provider",
            {},
            Exception("column provider does not exist"),
        )

        monkeypatch.setattr(
            "core.native_catalog_capability._open_isolated_read_session",
            lambda _db: isolated_db,
        )

        assert load_whatsapp_connection(operational_db, GENERIC_TENANT_ID) is None
        operational_db.rollback.assert_not_called()
        isolated_db.rollback.assert_called_once()
        isolated_db.close.assert_called_once()

    def test_simulated_missing_column_leaves_operational_session_usable(self) -> None:
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
        operational.execute(tenants.insert().values(id=1, name=GENERIC_MERCHANT_NAME))
        operational.flush()

        isolated_db = MagicMock()
        isolated_db.query.side_effect = ProgrammingError(
            "SELECT provider",
            {},
            Exception("column provider does not exist"),
        )
        isolated_db.rollback.return_value = None
        isolated_db.close.return_value = None

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(
            "core.native_catalog_capability._open_isolated_read_session",
            lambda _db: isolated_db,
        )
        try:
            assert load_whatsapp_connection(operational, GENERIC_TENANT_ID) is None

            operational.execute(
                tenants.update().where(tenants.c.id == 1).values(name="متجر محدث")
            )
            operational.flush()
            row = operational.execute(
                tenants.select().where(tenants.c.id == 1)
            ).first()
            assert row is not None
            assert row.name == "متجر محدث"
        finally:
            monkeypatch.undo()
            operational.close()


class TestCatalogNavigationSqlAudit:
    def test_generic_catalog_capability_probe_does_not_emit_sql_audit_errors(
        self,
        monkeypatch,
    ) -> None:
        metadata = MetaData()
        tenants = Table(
            "tenants",
            metadata,
            Column("id", Integer, primary_key=True),
            Column("name", String(64), nullable=False),
        )
        engine = create_engine("sqlite:///:memory:")
        metadata.create_all(engine)
        install_internal_e2e_sql_error_listener(engine)
        Session = sessionmaker(bind=engine)
        db = Session()
        db.execute(tenants.insert().values(id=GENERIC_TENANT_ID, name=GENERIC_MERCHANT_NAME))
        db.flush()

        monkeypatch.setattr(
            "modules.ai.brain.commerce.sales_channel_capabilities.resolve_merchant_sales_channels",
            lambda *_args, **_kwargs: SimpleNamespace(
                online_store=SimpleNamespace(enabled=False, available=False),
                whatsapp_quick_order=SimpleNamespace(enabled=True),
            ),
        )
        monkeypatch.setattr(
            "core.merchant_payment_methods.load_merchant_payment_methods",
            lambda *_args, **_kwargs: SimpleNamespace(
                bank_transfer_enabled=False,
                cash_on_delivery_enabled=False,
                moyasar_checkout_ready=False,
            ),
        )

        reset_session_sql_error_audit()
        with internal_conversational_e2e_context(
            session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            tenant_id=GENERIC_TENANT_ID,
            allow_llm_inference=True,
        ), internal_e2e_sql_error_turn(
            scenario_id="generic_catalog_probe",
            turn_index=0,
        ):
            caps = resolve_merchant_capabilities(db, GENERIC_TENANT_ID)
            assert caps.has_whatsapp_catalog is False

            db.execute(
                text("UPDATE tenants SET name = :name WHERE id = :id"),
                {"name": "متجر بعد التصفح", "id": GENERIC_TENANT_ID},
            )
            db.flush()

        audits = recorded_sql_error_audits()
        assert summarize_turn_sql_error_audit(audits)["primary_missing"] is False
        assert all(audit.pgcode != "25P02" for audit in audits)
        db.close()
