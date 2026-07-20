"""Bounded SQL error audit for confined internal conversational E2E.

Records structured DBAPI/SQLAlchemy errors at the engine ``handle_error``
boundary while ``internal_conversational_e2e_context`` is active. Never
captures SQL text, parameters, exception messages, credentials, or tenant PII.
"""
from __future__ import annotations

import re
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Optional

from core.acceptance_execution_context import current_acceptance_context


MAX_SQL_ERRORS_PER_TURN = 8
MAX_SQL_ERRORS_PER_SESSION = 32

_PGCODE_RE = re.compile(r"^[0-9A-Z]{5}$")
_SAFE_EXCEPTION_CLASS_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_SAFE_SCENARIO_ID_RE = re.compile(r"^[a-zA-Z0-9_.:-]{1,96}$")

_OPERATION_RE = re.compile(
    r"^\s*(SELECT|INSERT|UPDATE|DELETE)\b",
    re.IGNORECASE,
)
_TABLE_FROM_RE = re.compile(
    r"""
    (?:FROM|INTO|UPDATE)\s+
    (?:"(?P<quoted>[a-zA-Z_][a-zA-Z0-9_]*)"|(?P<plain>[a-zA-Z_][a-zA-Z0-9_]*))
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Closed allowlist — platform tables observable during confined E2E turns.
_AUDIT_TABLE_ALLOWLIST = frozenset(
    {
        "conversations",
        "messages",
        "products",
        "orders",
        "order_items",
        "customers",
        "customer_addresses",
        "tenants",
        "tenant_settings",
        "integrations",
        "coupons",
        "shipments",
        "payments",
        "catalog_items",
        "product_variants",
        "users",
    }
)

_INSTALLED_ENGINES: set[int] = set()


@dataclass(frozen=True)
class InternalE2ETurnBinding:
    scenario_id: str
    turn_index: int


@dataclass(frozen=True)
class SqlErrorAuditRecord:
    scenario_id: str
    turn_index: int
    sequence: int
    exception_class: str
    pgcode: str
    pg_category: str
    operation_category: str
    table_name: str
    transaction_invalidated: bool
    classification: Literal["primary", "secondary"]

    def to_audit_dict(self) -> dict[str, object]:
        return {
            "scenario_id": self.scenario_id,
            "turn_index": self.turn_index,
            "sequence": self.sequence,
            "exception_class": self.exception_class,
            "pgcode": self.pgcode,
            "pg_category": self.pg_category,
            "operation_category": self.operation_category,
            "table_name": self.table_name,
            "transaction_invalidated": self.transaction_invalidated,
            "classification": self.classification,
        }


_TURN_BINDING: ContextVar[Optional[InternalE2ETurnBinding]] = ContextVar(
    "nahla_internal_e2e_sql_turn_binding",
    default=None,
)
_SQL_ERROR_AUDIT: ContextVar[tuple[SqlErrorAuditRecord, ...]] = ContextVar(
    "nahla_internal_e2e_sql_error_audit",
    default=(),
)
_TURN_PRIMARY_SEEN: ContextVar[bool] = ContextVar(
    "nahla_internal_e2e_sql_primary_seen",
    default=False,
)
_SESSION_SQL_ERROR_AUDIT: ContextVar[tuple[SqlErrorAuditRecord, ...]] = ContextVar(
    "nahla_internal_e2e_session_sql_error_audit",
    default=(),
)


def bind_internal_e2e_turn(*, scenario_id: str, turn_index: int) -> None:
    """Bind the active confined-E2E turn for engine-level error attribution."""
    safe_scenario = str(scenario_id or "").strip()
    if not _SAFE_SCENARIO_ID_RE.fullmatch(safe_scenario):
        safe_scenario = "unknown"
    if type(turn_index) is not int or turn_index < 0:
        turn_index = -1
    _TURN_BINDING.set(InternalE2ETurnBinding(safe_scenario, turn_index))
    _SQL_ERROR_AUDIT.set(())
    _TURN_PRIMARY_SEEN.set(False)


def clear_internal_e2e_turn_binding() -> None:
    _TURN_BINDING.set(None)
    _SQL_ERROR_AUDIT.set(())
    _TURN_PRIMARY_SEEN.set(False)


def reset_session_sql_error_audit() -> None:
    _SESSION_SQL_ERROR_AUDIT.set(())


def recorded_sql_error_audits() -> tuple[SqlErrorAuditRecord, ...]:
    return _SQL_ERROR_AUDIT.get()


def recorded_session_sql_error_audits() -> tuple[SqlErrorAuditRecord, ...]:
    return _SESSION_SQL_ERROR_AUDIT.get()


def _safe_exception_class(exc: BaseException) -> str:
    name = exc.__class__.__name__
    if _SAFE_EXCEPTION_CLASS_RE.fullmatch(name):
        return name
    return "unknown"


def _sanitize_pgcode(exc: BaseException) -> str:
    for candidate in (getattr(exc, "orig", None), exc):
        if candidate is None:
            continue
        pgcode = getattr(candidate, "pgcode", None)
        if pgcode and _PGCODE_RE.fullmatch(str(pgcode)):
            return str(pgcode)
    return "unknown"


def _pg_category(pgcode: str) -> str:
    if pgcode == "unknown" or len(pgcode) < 2:
        return "unknown"
    return pgcode[:2]


def _is_failed_sql_transaction(exc: BaseException) -> bool:
    if exc.__class__.__name__ == "InFailedSqlTransaction":
        return True
    orig = getattr(exc, "orig", None)
    if orig is not None:
        if orig.__class__.__name__ == "InFailedSqlTransaction":
            return True
        if getattr(orig, "pgcode", None) == "25P02":
            return True
    return _sanitize_pgcode(exc) == "25P02"


def _operation_category(statement: object) -> str:
    if not isinstance(statement, str) or not statement.strip():
        return "other"
    match = _OPERATION_RE.match(statement)
    if not match:
        return "other"
    return match.group(1).upper()


def _table_name(statement: object, exc: BaseException) -> str:
    diag = getattr(getattr(exc, "orig", None), "diag", None)
    diag_table = getattr(diag, "table_name", None) if diag is not None else None
    if isinstance(diag_table, str) and diag_table in _AUDIT_TABLE_ALLOWLIST:
        return diag_table
    if isinstance(statement, str):
        match = _TABLE_FROM_RE.search(statement)
        if match:
            candidate = match.group("quoted") or match.group("plain")
            if candidate in _AUDIT_TABLE_ALLOWLIST:
                return candidate
    return "unknown"


def _classification(exc: BaseException) -> Literal["primary", "secondary"]:
    if _is_failed_sql_transaction(exc):
        return "secondary"
    if _TURN_PRIMARY_SEEN.get():
        return "secondary"
    _TURN_PRIMARY_SEEN.set(True)
    return "primary"


def _append_record(record: SqlErrorAuditRecord) -> None:
    turn_records = _SQL_ERROR_AUDIT.get()
    if len(turn_records) >= MAX_SQL_ERRORS_PER_TURN:
        return
    updated_turn = (*turn_records, record)
    _SQL_ERROR_AUDIT.set(updated_turn)

    session_records = _SESSION_SQL_ERROR_AUDIT.get()
    if len(session_records) >= MAX_SQL_ERRORS_PER_SESSION:
        return
    _SESSION_SQL_ERROR_AUDIT.set((*session_records, record))


def record_sql_error_from_context(exception_context: Any) -> None:
    """Capture one SQL error when confined E2E context and turn binding are active."""
    if current_acceptance_context() is None:
        return
    binding = _TURN_BINDING.get()
    if binding is None:
        return

    exc = getattr(exception_context, "sqlalchemy_exception", None)
    if exc is None:
        exc = getattr(exception_context, "original_exception", None)
    if not isinstance(exc, BaseException):
        return

    statement = getattr(exception_context, "statement", None)
    sequence = len(_SQL_ERROR_AUDIT.get()) + 1
    record = SqlErrorAuditRecord(
        scenario_id=binding.scenario_id,
        turn_index=binding.turn_index,
        sequence=sequence,
        exception_class=_safe_exception_class(exc),
        pgcode=_sanitize_pgcode(exc),
        pg_category=_pg_category(_sanitize_pgcode(exc)),
        operation_category=_operation_category(statement),
        table_name=_table_name(statement, exc),
        transaction_invalidated=True,
        classification=_classification(exc),
    )
    _append_record(record)


def summarize_turn_sql_error_audit(
    records: tuple[SqlErrorAuditRecord, ...] | tuple[() , ...],
) -> dict[str, object]:
    errors = [record.to_audit_dict() for record in records]
    has_primary = any(record.classification == "primary" for record in records)
    only_secondary = bool(records) and not has_primary
    return {
        "errors": errors,
        "error_count": len(records),
        "primary_missing": only_secondary,
        "truncated": len(records) >= MAX_SQL_ERRORS_PER_TURN,
    }


def summarize_session_sql_error_audit(
    records: tuple[SqlErrorAuditRecord, ...] | tuple[() , ...],
) -> dict[str, object]:
    errors = [record.to_audit_dict() for record in records]
    has_primary = any(record.classification == "primary" for record in records)
    only_secondary = bool(records) and not has_primary
    return {
        "errors": errors,
        "error_count": len(records),
        "primary_missing": only_secondary,
        "truncated": len(records) >= MAX_SQL_ERRORS_PER_SESSION,
    }


def install_internal_e2e_sql_error_listener(engine: Any) -> None:
    """Install a one-time ``handle_error`` listener on a disposable E2E engine."""
    engine_id = id(engine)
    if engine_id in _INSTALLED_ENGINES:
        return
    try:
        from sqlalchemy.engine import Engine
        from sqlalchemy import event
    except ImportError:
        return
    if not isinstance(engine, Engine):
        return

    @event.listens_for(engine, "handle_error")
    def _capture_sql_error(exception_context: Any) -> None:
        try:
            record_sql_error_from_context(exception_context)
        except Exception:  # noqa: silent-ok — audit capture must never disturb SQL error propagation
            return

    _INSTALLED_ENGINES.add(engine_id)


def evidence_completeness_fields() -> tuple[str, ...]:
    """Fields that must be present in signed turn/session evidence envelopes."""
    return ("runtime_error_audit",)
