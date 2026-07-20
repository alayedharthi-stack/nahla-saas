"""Regressions for confined internal E2E SQL error audit instrumentation."""
from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text
from sqlalchemy.exc import CompileError, DBAPIError, ProgrammingError

from core.acceptance_execution_context import internal_conversational_e2e_context
from services.internal_conversational_e2e_sql_error_audit import (
    MAX_SQL_ERRORS_PER_SESSION,
    MAX_SQL_ERRORS_PER_TURN,
    current_internal_e2e_sql_error_turn,
    internal_e2e_sql_error_turn,
    install_internal_e2e_sql_error_listener,
    record_sql_error_from_context,
    recorded_sql_error_audits,
    recorded_session_sql_error_audits,
    reset_session_sql_error_audit,
    summarize_session_sql_error_audit,
    summarize_turn_sql_error_audit,
)


TENANT_ID = 48
SESSION_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SCENARIO_ID = "generic_catalog_probe"
SECRET_SQL = "SELECT secret_token FROM users WHERE email = 'pii@example.test'"
SECRET_MESSAGE = "FATAL: password authentication failed for user postgres"


def _exception_context(
    exc: BaseException,
    *,
    statement: str | None = None,
    active_transaction: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        sqlalchemy_exception=exc,
        original_exception=exc,
        statement=statement,
        connection=SimpleNamespace(
            in_transaction=lambda: active_transaction,
        ),
    )


def _programming_error(
    *,
    pgcode: str = "42P01",
    class_name: str = "UndefinedTable",
    statement: str | None = None,
) -> ProgrammingError:
    exc_type = type(class_name, (Exception,), {})
    orig = exc_type(SECRET_MESSAGE)
    orig.pgcode = pgcode  # type: ignore[attr-defined]
    return ProgrammingError(
        statement or SECRET_SQL,
        {},
        orig,
        connection_invalidated=False,
    )


@contextmanager
def _active_e2e_turn():
    reset_session_sql_error_audit()
    with internal_conversational_e2e_context(
        session_id=SESSION_ID,
        tenant_id=TENANT_ID,
        allow_llm_inference=True,
    ), internal_e2e_sql_error_turn(
        scenario_id=SCENARIO_ID,
        turn_index=0,
    ) as scope:
        yield scope


def test_no_capture_without_internal_e2e_context() -> None:
    with internal_e2e_sql_error_turn(scenario_id=SCENARIO_ID, turn_index=0):
        record_sql_error_from_context(_exception_context(_programming_error()))
    assert recorded_sql_error_audits() == ()


def test_no_capture_without_turn_binding() -> None:
    with internal_conversational_e2e_context(
        session_id=SESSION_ID,
        tenant_id=TENANT_ID,
        allow_llm_inference=True,
    ):
        record_sql_error_from_context(_exception_context(_programming_error()))
    assert recorded_sql_error_audits() == ()


def test_primary_then_secondary_classification_and_primary_missing() -> None:
    with _active_e2e_turn():
        record_sql_error_from_context(
            _exception_context(
                _programming_error(pgcode="42P01", class_name="UndefinedTable"),
                statement="SELECT 1 FROM products",
                active_transaction=True,
            )
        )
        record_sql_error_from_context(
            _exception_context(
                _programming_error(pgcode="25P02", class_name="InFailedSqlTransaction"),
                statement="SELECT 1",
                active_transaction=True,
            )
        )
        audits = recorded_sql_error_audits()

    assert len(audits) == 2
    assert audits[0].classification == "primary"
    assert audits[0].pgcode == "42P01"
    assert audits[0].operation_category == "SELECT"
    assert audits[0].table_name == "products"
    assert audits[0].transaction_invalidated is True
    assert audits[1].classification == "secondary"
    assert audits[1].pgcode == "25P02"
    assert audits[1].transaction_invalidated is True
    summary = summarize_turn_sql_error_audit(audits)
    assert summary["primary_missing"] is False


def test_only_secondary_preserves_primary_missing() -> None:
    with _active_e2e_turn():
        record_sql_error_from_context(
            _exception_context(
                _programming_error(pgcode="25P02", class_name="InFailedSqlTransaction"),
                statement="SELECT 1",
            )
        )
        audits = recorded_sql_error_audits()

    assert len(audits) == 1
    assert audits[0].classification == "secondary"
    summary = summarize_turn_sql_error_audit(audits)
    assert summary["primary_missing"] is True


def test_bounded_fields_never_leak_sql_message_or_secrets() -> None:
    with _active_e2e_turn():
        record_sql_error_from_context(
            _exception_context(
                _programming_error(statement=SECRET_SQL),
                statement=SECRET_SQL,
            )
        )
        audits = recorded_sql_error_audits()

    encoded = json.dumps([audit.to_audit_dict() for audit in audits], ensure_ascii=False)
    assert SECRET_SQL not in encoded
    assert SECRET_MESSAGE not in encoded
    assert "password" not in encoded.lower()
    assert "pii@example.test" not in encoded
    assert set(audits[0].to_audit_dict()) == {
        "scenario_id",
        "turn_index",
        "sequence",
        "exception_class",
        "pgcode",
        "pg_category",
        "operation_category",
        "table_name",
        "transaction_invalidated",
        "classification",
    }


def test_unknown_table_outside_allowlist_is_redacted() -> None:
    with _active_e2e_turn():
        record_sql_error_from_context(
            _exception_context(
                _programming_error(statement="SELECT 1 FROM secret_probe_table"),
                statement="SELECT 1 FROM secret_probe_table",
            )
        )
        audits = recorded_sql_error_audits()

    assert audits[0].table_name == "unknown"
    assert "secret_probe_table" not in json.dumps(audits[0].to_audit_dict())


def test_turn_error_cardinality_is_bounded() -> None:
    with _active_e2e_turn() as scope:
        for _ in range(MAX_SQL_ERRORS_PER_TURN + 3):
            record_sql_error_from_context(
                _exception_context(
                    _programming_error(pgcode="42P01"),
                    statement="UPDATE products SET id = 1",
                )
            )
        audits = recorded_sql_error_audits()

    assert len(audits) == MAX_SQL_ERRORS_PER_TURN
    assert scope.summary["truncated"] is True


def test_exact_turn_cap_is_not_truncated_until_one_more_error() -> None:
    with _active_e2e_turn():
        for _ in range(MAX_SQL_ERRORS_PER_TURN):
            record_sql_error_from_context(
                _exception_context(_programming_error(), active_transaction=True)
            )
        audits = recorded_sql_error_audits()
        assert summarize_turn_sql_error_audit(audits)["truncated"] is False

        record_sql_error_from_context(
            _exception_context(_programming_error(), active_transaction=True)
        )
        audits = recorded_sql_error_audits()
        assert summarize_turn_sql_error_audit(audits)["truncated"] is True


def test_transaction_invalidation_requires_db_error_and_active_transaction() -> None:
    with _active_e2e_turn():
        record_sql_error_from_context(
            _exception_context(_programming_error(), active_transaction=False)
        )
        record_sql_error_from_context(
            _exception_context(CompileError(SECRET_MESSAGE), active_transaction=True)
        )
        audits = recorded_sql_error_audits()

    assert len(audits) == 1
    assert audits[0].transaction_invalidated is False


def test_turn_scope_snapshots_and_resets_when_execution_raises() -> None:
    reset_session_sql_error_audit()
    scope = None
    with pytest.raises(RuntimeError, match="state probe failed"):
        with internal_conversational_e2e_context(
            session_id=SESSION_ID,
            tenant_id=TENANT_ID,
            allow_llm_inference=True,
        ), internal_e2e_sql_error_turn(
            scenario_id=SCENARIO_ID,
            turn_index=7,
        ) as scope:
            record_sql_error_from_context(
                _exception_context(_programming_error(), active_transaction=True)
            )
            raise RuntimeError("state probe failed")

    assert scope is not None
    assert scope.summary["error_count"] == 1
    assert current_internal_e2e_sql_error_turn() is None
    assert recorded_sql_error_audits() == ()


def test_session_primary_missing_is_computed_per_turn() -> None:
    reset_session_sql_error_audit()
    with internal_conversational_e2e_context(
        session_id=SESSION_ID,
        tenant_id=TENANT_ID,
        allow_llm_inference=True,
    ):
        with internal_e2e_sql_error_turn(scenario_id=SCENARIO_ID, turn_index=0):
            record_sql_error_from_context(
                _exception_context(
                    _programming_error(pgcode="42P01"),
                    active_transaction=True,
                )
            )
        with internal_e2e_sql_error_turn(scenario_id=SCENARIO_ID, turn_index=1):
            record_sql_error_from_context(
                _exception_context(
                    _programming_error(
                        pgcode="25P02",
                        class_name="InFailedSqlTransaction",
                    ),
                    active_transaction=True,
                )
            )

    summary = summarize_session_sql_error_audit(recorded_session_sql_error_audits())
    assert summary["error_count"] == 2
    assert summary["primary_missing"] is True


def test_session_truncation_requires_an_actually_dropped_record() -> None:
    reset_session_sql_error_audit()
    with internal_conversational_e2e_context(
        session_id=SESSION_ID,
        tenant_id=TENANT_ID,
        allow_llm_inference=True,
    ):
        for turn_index in range(
            MAX_SQL_ERRORS_PER_SESSION // MAX_SQL_ERRORS_PER_TURN
        ):
            with internal_e2e_sql_error_turn(
                scenario_id=SCENARIO_ID,
                turn_index=turn_index,
            ):
                for _ in range(MAX_SQL_ERRORS_PER_TURN):
                    record_sql_error_from_context(
                        _exception_context(
                            _programming_error(),
                            active_transaction=True,
                        )
                    )

        records = recorded_session_sql_error_audits()
        assert len(records) == MAX_SQL_ERRORS_PER_SESSION
        assert summarize_session_sql_error_audit(records)["truncated"] is False

        with internal_e2e_sql_error_turn(
            scenario_id=SCENARIO_ID,
            turn_index=99,
        ):
            record_sql_error_from_context(
                _exception_context(
                    _programming_error(),
                    active_transaction=True,
                )
            )

    records = recorded_session_sql_error_audits()
    assert len(records) == MAX_SQL_ERRORS_PER_SESSION
    assert summarize_session_sql_error_audit(records)["truncated"] is True


def test_disposable_postgres_engine_captures_swallowed_primary_and_secondary() -> None:
    from tests.order_customer_identity_postgres_fixtures import _connect_engine

    engine = _connect_engine()
    install_internal_e2e_sql_error_listener(engine)

    with _active_e2e_turn():
        with engine.begin() as conn:
            try:
                conn.execute(
                    text("SELECT * FROM nahla_e2e_sql_error_probe_nonexistent")
                )
            except DBAPIError:
                pass
            try:
                conn.execute(text("SELECT 1"))
            except DBAPIError:
                pass
        audits = recorded_sql_error_audits()

    if not audits:
        pytest.skip("PostgreSQL did not surface expected SQL errors in this environment")

    encoded = json.dumps([audit.to_audit_dict() for audit in audits], ensure_ascii=False)
    assert "nahla_e2e_sql_error_probe_nonexistent" not in encoded
    assert len(audits) >= 2
    assert audits[0].classification == "primary"
    assert audits[0].pgcode != "25P02"
    assert audits[0].transaction_invalidated is True
    assert any(audit.classification == "secondary" for audit in audits[1:])
    assert any(
        audit.pgcode == "25P02" and audit.transaction_invalidated is True
        for audit in audits[1:]
    )
    assert summarize_turn_sql_error_audit(audits)["primary_missing"] is False


def test_listener_is_noop_without_active_e2e_context_on_disposable_engine() -> None:
    from tests.order_customer_identity_postgres_fixtures import _connect_engine

    engine = _connect_engine()
    install_internal_e2e_sql_error_listener(engine)
    reset_session_sql_error_audit()

    with pytest.raises(DBAPIError):
        with engine.begin() as conn:
            conn.execute(text("SELECT * FROM nahla_e2e_no_context_probe_nonexistent"))

    assert recorded_sql_error_audits() == ()


def test_listener_installation_is_idempotent_per_engine() -> None:
    from sqlalchemy import create_engine

    engine = create_engine("sqlite:///:memory:")
    install_internal_e2e_sql_error_listener(engine)
    install_internal_e2e_sql_error_listener(engine)

    with _active_e2e_turn():
        with pytest.raises(DBAPIError):
            with engine.begin() as conn:
                conn.execute(text("SELECT * FROM unavailable_table"))
        audits = recorded_sql_error_audits()

    assert len(audits) == 1
    engine.dispose()
