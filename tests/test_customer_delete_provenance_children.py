"""``routers.customers._delete_customer_children`` must remove the durable
``customer_name_provenance`` row (migration 0107, NOT NULL FK, no cascade)
before the parent customer row, and must only nullify optional FK tables
that exist in the bound schema.

The PostgreSQL proof (FK violation before the fix) lives in
``backend/tests/test_customer_delete_provenance_pg.py``; this unit test
runs in the default suite and asserts the helper's statements directly.
"""
from __future__ import annotations

import os
import sys
from typing import Any, List
from unittest.mock import MagicMock

from sqlalchemy import create_engine

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (REPO, os.path.join(REPO, "backend"), os.path.join(REPO, "database")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from models import (  # noqa: E402
    CustomerNameAuditLog,
    CustomerNameProvenance,
    CustomerProfile,
)
from routers.customers import _delete_customer_children  # noqa: E402


# The optional (nullable FK) tables the helper nullifies, minus the phantom
# ``delivery_quality_events`` which nothing in this repository defines.
_PRESENT_OPTIONAL_TABLES = (
    "notification_logs",
    "automation_events",
    "automation_executions",
    "ai_action_logs",
    "campaign_send_logs",
)


class _RecordingSession:
    """Records ORM bulk deletes and raw statements; the inspector sees a
    real SQLite schema holding only the tables above."""

    def __init__(self) -> None:
        self._engine = create_engine("sqlite://")
        with self._engine.begin() as conn:
            for table in _PRESENT_OPTIONAL_TABLES:
                conn.exec_driver_sql(
                    f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, "
                    "tenant_id INTEGER, customer_id INTEGER)"
                )
        self.deleted_models: List[Any] = []
        self.executed_sql: List[str] = []

    def get_bind(self) -> Any:
        return self._engine

    def query(self, model: Any) -> Any:
        query = MagicMock(name=f"query({model.__name__})")

        def _delete(**_kwargs: Any) -> int:
            self.deleted_models.append(model)
            return 0

        query.filter.return_value.delete.side_effect = _delete
        return query

    def execute(self, statement: Any, params: Any = None) -> None:
        self.executed_sql.append(str(statement))


def test_provenance_rows_are_deleted_with_the_other_children() -> None:
    db = _RecordingSession()
    _delete_customer_children(db, [7, 8], tenant_id=3)

    assert CustomerNameProvenance in db.deleted_models
    # Ordering: provenance goes with the other NOT NULL FK children, before
    # any parent delete the caller issues afterwards.
    assert db.deleted_models.index(CustomerNameProvenance) > db.deleted_models.index(
        CustomerNameAuditLog
    )
    assert CustomerProfile in db.deleted_models


def test_only_present_optional_fk_tables_are_nullified() -> None:
    db = _RecordingSession()
    _delete_customer_children(db, [7], tenant_id=3)

    touched = "\n".join(db.executed_sql)
    assert "UPDATE notification_logs SET customer_id = NULL" in touched
    assert "UPDATE campaign_send_logs SET customer_id = NULL" in touched
    assert "delivery_quality_events" not in touched, (
        "no model or migration defines delivery_quality_events; updating a "
        "missing relation aborts the delete transaction on PostgreSQL"
    )
