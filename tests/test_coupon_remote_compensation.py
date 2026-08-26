"""Unit tests for durable Salla coupon remote compensation records."""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import JSON, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from database.models import Base, Tenant, TenantSettings
from services.coupon_remote_compensation import (
    list_pending_compensations,
    record_unresolved_compensation,
    retry_pending_compensations,
)


@event.listens_for(Base.metadata, "before_create")
def _remap_jsonb(target, connection, **kw):
    for table in target.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()


def _make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    tenant = Tenant(name="Comp Tenant", is_active=True)
    session.add(tenant)
    session.commit()
    return session, tenant.id, engine


def test_record_unresolved_compensation_is_idempotent():
    db, tenant_id, engine = _make_db()
    try:
        first = record_unresolved_compensation(
            db,
            tenant_id,
            salla_coupon_id="remote-1",
            code_hash="hash-abc",
            error_class="delete_by_id_false",
            idempotency_key="idem-1",
        )
        second = record_unresolved_compensation(
            db,
            tenant_id,
            salla_coupon_id="remote-1",
            code_hash="hash-abc",
            error_class="delete_by_id_false",
            idempotency_key="idem-1",
        )
        db.commit()
        assert first is True
        assert second is False
        pending = list_pending_compensations(db, tenant_id)
        assert len(pending) == 1
    finally:
        db.close()
        engine.dispose()


def test_delete_false_keeps_pending():
    db, tenant_id, engine = _make_db()
    try:
        record_unresolved_compensation(
            db,
            tenant_id,
            salla_coupon_id="remote-2",
            code_hash="hash-def",
            error_class="delete_by_id_false",
            idempotency_key="idem-2",
        )
        db.commit()
        adapter = SimpleNamespace(delete_coupon_by_id=AsyncMock(return_value=False))
        result = asyncio.run(retry_pending_compensations(db, tenant_id, adapter))
        db.commit()
        assert result == {"attempted": 1, "resolved": 0, "failed": 1}
        assert len(list_pending_compensations(db, tenant_id)) == 1
    finally:
        db.close()
        engine.dispose()


def test_delete_exception_keeps_pending(caplog):
    db, tenant_id, engine = _make_db()
    try:
        record_unresolved_compensation(
            db,
            tenant_id,
            salla_coupon_id="remote-3",
            code_hash="hash-ghi",
            error_class="delete_by_id_false",
            idempotency_key="idem-3",
        )
        db.commit()

        async def _boom(_coupon_id: str):
            raise RuntimeError("provider down")

        adapter = SimpleNamespace(delete_coupon_by_id=_boom)
        with caplog.at_level(logging.WARNING):
            result = asyncio.run(retry_pending_compensations(db, tenant_id, adapter))
        db.commit()
        assert result == {"attempted": 1, "resolved": 0, "failed": 1}
        assert len(list_pending_compensations(db, tenant_id)) == 1
        assert "SECRET-CODE-XYZ" not in caplog.text
    finally:
        db.close()
        engine.dispose()


def test_retry_resolves_pending():
    db, tenant_id, engine = _make_db()
    try:
        record_unresolved_compensation(
            db,
            tenant_id,
            salla_coupon_id="remote-4",
            code_hash="hash-jkl",
            error_class="delete_by_id_false",
            idempotency_key="idem-4",
        )
        db.commit()
        adapter = SimpleNamespace(delete_coupon_by_id=AsyncMock(return_value=True))
        result = asyncio.run(retry_pending_compensations(db, tenant_id, adapter))
        db.commit()
        assert result == {"attempted": 1, "resolved": 1, "failed": 0}
        assert list_pending_compensations(db, tenant_id) == []
    finally:
        db.close()
        engine.dispose()


def test_record_logs_no_raw_code(caplog):
    db, tenant_id, engine = _make_db()
    try:
        raw_code = "NHSECRET"
        with caplog.at_level(logging.WARNING):
            record_unresolved_compensation(
                db,
                tenant_id,
                salla_coupon_id="remote-5",
                code_hash="hash-mno",
                error_class="delete_by_id_false",
                idempotency_key="idem-5",
            )
        assert raw_code not in caplog.text
    finally:
        db.close()
        engine.dispose()
