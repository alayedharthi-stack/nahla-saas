"""NHL external_order_number allocation — never reuse human references."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from models import Base, Order  # noqa: E402
from services.nahla_order_bridge import (  # noqa: E402
    _allocate_nhl_number,
    _max_nhl_sequence_for_tenant,
    _nhl_number_taken,
)
from sqlalchemy import JSON, create_engine  # noqa: E402
from sqlalchemy.dialects.postgresql import JSONB  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402


def _make_db() -> Tuple[Any, Any]:
    engine = create_engine("sqlite:///:memory:")
    saved: list = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, orig in saved:
        col.type = orig
    Session = sessionmaker(bind=engine)
    return Session(), engine


def _order(
    *,
    tenant_id: int,
    external_id: str,
    external_order_number: str,
    status: str = "pending_customer_info",
    source: str = "whatsapp",
    lifecycle: str = "whatsapp_draft",
) -> Order:
    return Order(
        tenant_id=tenant_id,
        external_id=external_id,
        external_order_number=external_order_number,
        status=status,
        source=source,
        total="126.00 ر.س",
        customer_name="Customer",
        customer_info={"phone": "+966500000000"},
        line_items=[],
        extra_metadata={"lifecycle": lifecycle, "source_kind": "nahla_order"},
    )


def test_nhl_allocator_does_not_reuse_cancelled_order_number() -> None:
    db, _ = _make_db()
    db.add(
        _order(
            tenant_id=33,
            external_id="nahla-wa-archived-33-2868-o89",
            external_order_number="NHL-33-000045",
            status="cancelled",
            lifecycle="archived_canary",
        )
    )
    db.commit()

    assert _allocate_nhl_number(db, 33) == "NHL-33-000046"


def test_nhl_allocator_does_not_depend_on_external_id_prefix() -> None:
    db, _ = _make_db()
    db.add_all(
        [
            _order(
                tenant_id=33,
                external_id="nahla-wa-33-100",
                external_order_number="NHL-33-000044",
            ),
            Order(
                tenant_id=33,
                external_id="salla-import-999",
                external_order_number="NHL-33-000045",
                status="paid",
                source="salla",
                total="50.00 ر.س",
                customer_name="Salla",
                customer_info={},
                line_items=[],
                extra_metadata={"lifecycle": "paid"},
            ),
        ]
    )
    db.commit()

    # Only one nahla-wa row — old count-based allocator would return 000002.
    assert _allocate_nhl_number(db, 33) == "NHL-33-000046"


def test_nhl_allocator_uses_max_existing_sequence_not_count() -> None:
    db, _ = _make_db()
    db.add_all(
        [
            _order(
                tenant_id=33,
                external_id="nahla-wa-33-1",
                external_order_number="NHL-33-000001",
            ),
            _order(
                tenant_id=33,
                external_id="nahla-wa-33-2",
                external_order_number="NHL-33-000010",
            ),
            _order(
                tenant_id=33,
                external_id="nahla-wa-33-3",
                external_order_number="NHL-33-000005",
            ),
        ]
    )
    db.commit()

    assert _max_nhl_sequence_for_tenant(db, 33) == 10
    assert _allocate_nhl_number(db, 33) == "NHL-33-000011"


def test_nhl_allocator_skips_archived_lifecycle_orders_but_counts_their_numbers() -> None:
    db, _ = _make_db()
    db.add(
        _order(
            tenant_id=33,
            external_id="nahla-wa-archived-33-2868-o89",
            external_order_number="NHL-33-000045",
            status="cancelled",
            lifecycle="archived_canary",
        )
    )
    db.commit()

    assert _max_nhl_sequence_for_tenant(db, 33) == 45
    assert _allocate_nhl_number(db, 33) == "NHL-33-000046"


def test_nhl_allocator_retries_on_collision(monkeypatch: pytest.MonkeyPatch) -> None:
    db, _ = _make_db()
    db.add(
        _order(
            tenant_id=33,
            external_id="nahla-wa-33-2868",
            external_order_number="NHL-33-000045",
        )
    )
    db.commit()

    taken = {"NHL-33-000046"}
    real_taken = _nhl_number_taken

    def _fake_taken(session: Any, tenant_id: int, number: str) -> bool:
        if number in taken:
            return True
        return real_taken(session, tenant_id, number)

    monkeypatch.setattr(
        "services.nahla_order_bridge._nhl_number_taken",
        _fake_taken,
    )

    assert _allocate_nhl_number(db, 33) == "NHL-33-000047"


def test_nhl_allocator_is_tenant_scoped() -> None:
    db, _ = _make_db()
    db.add_all(
        [
            _order(
                tenant_id=33,
                external_id="nahla-wa-33-2868",
                external_order_number="NHL-33-000045",
            ),
            _order(
                tenant_id=44,
                external_id="nahla-wa-44-100",
                external_order_number="NHL-44-000003",
            ),
        ]
    )
    db.commit()

    assert _allocate_nhl_number(db, 33) == "NHL-33-000046"
    assert _allocate_nhl_number(db, 44) == "NHL-44-000004"


def test_nhl_allocator_archived_order_89_then_new_order_gets_000046() -> None:
    """Regression for canary clean-rerun collision (orders 89 + 96)."""
    db, _ = _make_db()
    db.add(
        Order(
            tenant_id=33,
            external_id="nahla-wa-archived-33-2868-o89",
            external_order_number="NHL-33-000045",
            status="cancelled",
            source="whatsapp",
            total="126.00 ر.س",
            customer_name="سعدية الحارثي",
            customer_info={"phone": "+966507283619"},
            line_items=[],
            extra_metadata={
                "lifecycle": "archived_canary",
                "source_kind": "nahla_order",
            },
        )
    )
    db.commit()

    assert _allocate_nhl_number(db, 33) == "NHL-33-000046"
    # Historical NHL-33-000045 remains on archived order 89 (evidence preserved).
    assert _nhl_number_taken(db, 33, "NHL-33-000045")
