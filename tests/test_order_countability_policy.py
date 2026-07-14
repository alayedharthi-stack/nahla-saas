"""Parity tests for the deterministic order-countability policy."""
from __future__ import annotations

import inspect
from itertools import chain
import re

import pytest
from sqlalchemy import Boolean, Column, MetaData, String, Table, create_engine, event, select
from sqlalchemy.dialects import postgresql

from models import Order
from services import customer_intelligence
from services.order_countability_policy import (
    COUNTABLE_ORDER_STATUSES,
    EXCLUDED_ORDER_STATUSES,
    countable_order_sql_predicate,
    is_countable_order,
    order_status_key,
    order_status_key_sql,
)

LEGACY_STATUS_CASES = (
    (
        "{'slug': 'paid', 'name': 'cancelled', 'code': 'refunded'}",
        "paid",
        True,
    ),
    ("{'name': 'canceled', 'code': 'paid'}", "canceled", False),
    ("{'code': 'refunded'}", "refunded", False),
    ("{'slug': 'cancelled'", "{'slug': 'cancelled'", True),
)


def _sqlite_postgresql_substring(value: str | None, pattern: str) -> str | None:
    """SQLite test equivalent for PostgreSQL ``substring(... FROM regex)``."""
    if value is None:
        return None
    match = re.search(pattern, value)
    return match.group(1) if match else None


def _sqlite_engine_with_postgresql_substring() -> object:
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _register_postgresql_substring(dbapi_connection, _connection_record) -> None:
        dbapi_connection.create_function("substring", 2, _sqlite_postgresql_substring)

    return engine


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        *[(status, True) for status in sorted(COUNTABLE_ORDER_STATUSES)],
        *[(status, False) for status in sorted(EXCLUDED_ORDER_STATUSES)],
        ("under_review", True),
        ("merchant_custom_stage", True),
        ("", False),
        ("   ", False),
        (None, False),
        (" CANCELED ", False),
    ],
)
def test_python_status_matrix_preserves_current_policy(status: str | None, expected: bool) -> None:
    assert is_countable_order(status) is expected


@pytest.mark.parametrize(
    ("status", "is_abandoned", "expected"),
    [
        ("paid", False, True),
        ("delivered", False, True),
        ("under_review", False, True),
        ("merchant_custom_stage", False, True),
        ("cancelled", False, False),
        ("canceled", False, False),
        ("refunded", False, False),
        ("abandoned", False, False),
        ("paid", True, False),
        ("under_review", True, False),
        ("cancelled", True, False),
        ("paid", None, True),
    ],
)
def test_object_inputs_apply_abandoned_state(
    status: str,
    is_abandoned: bool | None,
    expected: bool,
) -> None:
    # A generic commerce order has no customer or tenant coupling in this
    # classifier; only operational status and abandonment state are read.
    order = Order(
        tenant_id=1,
        external_id="generic-order-1",
        status=status,
        is_abandoned=is_abandoned,
    )
    assert is_countable_order(order) is expected


def test_raw_statuses_do_not_have_object_abandonment_semantics() -> None:
    assert is_countable_order("paid") is True
    assert is_countable_order(Order(tenant_id=1, status="paid", is_abandoned=True)) is False


def test_legacy_customer_intelligence_exports_are_canonical_policy_exports() -> None:
    assert customer_intelligence.is_countable_order is is_countable_order
    assert customer_intelligence.order_status_key is order_status_key
    assert customer_intelligence.COUNTABLE_ORDER_STATUSES is COUNTABLE_ORDER_STATUSES
    assert customer_intelligence.EXCLUDED_ORDER_STATUSES is EXCLUDED_ORDER_STATUSES


def test_legacy_status_repr_recovery_is_preserved_in_python_policy() -> None:
    legacy_status = "{'id': 1, 'name': 'Processing', 'slug': 'processing'}"
    assert order_status_key(legacy_status) == "processing"
    assert is_countable_order(legacy_status) is True


@pytest.mark.parametrize(("status", "expected_key", "expected_countable"), LEGACY_STATUS_CASES)
def test_legacy_mapping_recovery_precedence_and_malformed_fallback(
    status: str,
    expected_key: str,
    expected_countable: bool,
) -> None:
    assert order_status_key(status) == expected_key
    assert is_countable_order(status) is expected_countable


def test_sql_predicate_api_has_no_database_or_identity_coupling() -> None:
    assert tuple(inspect.signature(countable_order_sql_predicate).parameters) == (
        "status",
        "is_abandoned",
    )
    assert countable_order_sql_predicate(Order.status, Order.is_abandoned) is not None


def test_sql_predicate_matches_python_matrix() -> None:
    metadata = MetaData()
    cases = Table(
        "countability_cases",
        metadata,
        Column("row_id", String, primary_key=True),
        Column("status", String, nullable=True),
        Column("is_abandoned", Boolean, nullable=True),
    )
    rows = [
        {
            "row_id": f"{index}-{str(status)}-{is_abandoned}",
            "status": status,
            "is_abandoned": is_abandoned,
        }
        for index, (status, is_abandoned) in enumerate(
            chain.from_iterable(
                (
                    ((status, abandoned) for abandoned in (False, True, None))
                    for status in (
                        *sorted(COUNTABLE_ORDER_STATUSES),
                        *sorted(EXCLUDED_ORDER_STATUSES),
                        "under_review",
                        "merchant_custom_stage",
                        "",
                        "   ",
                        None,
                        " CANCELED ",
                        *(status for status, _key, _countable in LEGACY_STATUS_CASES),
                    )
                )
            )
        )
    ]
    expected_ids = {
        row["row_id"]
        for row in rows
        if is_countable_order(
            Order(
                tenant_id=1,
                external_id=row["row_id"],
                status=row["status"] or "",
                is_abandoned=row["is_abandoned"],
            )
        )
    }

    predicate = countable_order_sql_predicate(cases.c.status, cases.c.is_abandoned)
    engine = _sqlite_engine_with_postgresql_substring()
    try:
        metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(cases.insert(), rows)
            actual_ids = set(
                connection.execute(select(cases.c.row_id).where(predicate)).scalars()
            )
    finally:
        engine.dispose()

    assert actual_ids == expected_ids


def test_sql_predicate_is_postgresql_composable_and_parameterized() -> None:
    predicate = countable_order_sql_predicate(Order.status, Order.is_abandoned)
    statement = select(Order.id, order_status_key_sql(Order.status)).where(predicate)
    compiled = statement.compile(dialect=postgresql.dialect())

    assert "orders.status" in str(compiled)
    assert "orders.is_abandoned" in str(compiled)
    # One projection plus the predicate's nonblank and exclusion checks each
    # compose the three-field recovery expression.
    assert str(compiled).count("SUBSTRING") == 9
    assert "tenant_id" not in str(compiled)
    assert "customer_id" not in str(compiled)
    assert "cancelled" not in str(compiled)
    assert any("slug" in str(value) for value in compiled.params.values())
    assert any("name" in str(value) for value in compiled.params.values())
    assert any("code" in str(value) for value in compiled.params.values())
    assert set(EXCLUDED_ORDER_STATUSES).issubset(
        set(chain.from_iterable(
            value if isinstance(value, (list, tuple, set)) else (value,)
            for value in compiled.params.values()
        ))
    )
