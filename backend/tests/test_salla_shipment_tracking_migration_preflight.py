"""Unit contract for 0112's fail-closed pre-migration inspection."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


_REPO = Path(__file__).resolve().parents[2]
_MIGRATION_PATH = _REPO / "database" / "migrations" / "versions" / "0112_salla_shipment_tracking.py"
_SPEC = importlib.util.spec_from_file_location("migration_0112_preflight", _MIGRATION_PATH)
assert _SPEC and _SPEC.loader
_MIGRATION = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MIGRATION)


class _Inspector:
    def __init__(self, *, table_present: bool = True) -> None:
        self.table_present = table_present
        self.columns = {
            name: {"name": name, "nullable": nullable, "type": _type_for(expected_type)}
            for name, (nullable, expected_type) in _MIGRATION._FOUNDATION_COLUMNS.items()
        }
        self.unique_constraints = [{
            "name": _MIGRATION._FOUNDATION_UNIQUE,
            "column_names": ["order_id"],
        }]

    def get_table_names(self):
        return [_MIGRATION._TABLE] if self.table_present else []

    def get_columns(self, _table):
        return list(self.columns.values())

    def get_pk_constraint(self, _table):
        return {"constrained_columns": ["id"]}

    def get_unique_constraints(self, _table):
        return self.unique_constraints

    def get_foreign_keys(self, _table):
        return [
            {
                "constrained_columns": ["tenant_id"],
                "referred_table": "tenants",
                "referred_columns": ["id"],
            },
            {
                "constrained_columns": ["order_id"],
                "referred_table": "orders",
                "referred_columns": ["id"],
            },
        ]


def _type_for(expected: str):
    return {
        "integer": sa.Integer(),
        "string": sa.String(),
        "text": sa.Text(),
        "jsonb": JSONB(),
        "datetime": sa.DateTime(),
        "timezone_datetime": sa.DateTime(timezone=True),
    }[expected]


def _assert_preflight(inspector: _Inspector, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_MIGRATION.sa, "inspect", lambda _bind: inspector)
    _MIGRATION._assert_existing_table_matches_foundation(object())


def test_0112_preflight_accepts_the_required_0080_foundation(monkeypatch: pytest.MonkeyPatch):
    _assert_preflight(_Inspector(), monkeypatch)


@pytest.mark.parametrize("mutate", [
    lambda inspector: setattr(inspector, "table_present", False),
    lambda inspector: inspector.columns.pop("metadata"),
    lambda inspector: inspector.unique_constraints.__setitem__(0, {
        "name": _MIGRATION._FOUNDATION_UNIQUE,
        "column_names": ["tenant_id", "order_id"],
    }),
    lambda inspector: inspector.columns.__setitem__("tracking_data_source", {
        "name": "tracking_data_source", "nullable": True, "type": sa.Integer(),
    }),
])
def test_0112_preflight_refuses_missing_or_drifted_foundation(
    monkeypatch: pytest.MonkeyPatch,
    mutate,
):
    inspector = _Inspector()
    mutate(inspector)
    with pytest.raises(RuntimeError, match="required pre-0112 shipment foundation"):
        _assert_preflight(inspector, monkeypatch)
