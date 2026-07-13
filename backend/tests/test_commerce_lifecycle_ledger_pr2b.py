"""
PR 2B — commerce lifecycle shadow notification ledger tests.
"""
from __future__ import annotations

import ast
import importlib
import os
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Tuple

import pytest
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from core.commerce_lifecycle.intents import BusinessIntent  # noqa: E402
from core.commerce_lifecycle.ledger import (  # noqa: E402
    ShadowLedgerOutcome,
    build_lifecycle_idempotency_key,
    hash_destination_reference,
    is_shadow_ledger_enabled,
    mark_shadow_outcome,
    reserve_shadow_decision,
    sanitize_capabilities_snapshot,
    sanitize_dispatch_decision,
)
from models import CommerceLifecycleNotificationLedger  # noqa: E402


def _make_db() -> Tuple[Any, Any]:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    saved: list = []
    table = CommerceLifecycleNotificationLedger.__table__
    for col in table.columns:
        if isinstance(col.type, JSONB):
            saved.append((col, col.type))
            col.type = JSON()
    table.create(engine)
    for col, orig in saved:
        col.type = orig
    Session = sessionmaker(bind=engine)
    return Session(), engine


def _reserve(**kwargs):
    defaults = dict(
        tenant_id=1,
        order_id=100,
        business_intent=BusinessIntent.ORDER_CONFIRMED,
        channel="whatsapp",
        source_event_id="evt-1",
        transition_version="v1",
        destination_hash=hash_destination_reference("+966500000001"),
        capabilities_snapshot={"has_external_tracking": True},
        evidence_present=["order_number"],
        dispatch_decision={"handoff_kind": "lifecycle_notification"},
    )
    defaults.update(kwargs)
    db, _ = _make_db()
    result = reserve_shadow_decision(db, **defaults)
    db.commit()
    return db, result


class TestIdempotencyKey:
    def test_key_includes_transition_identity(self):
        key_a = build_lifecycle_idempotency_key(
            tenant_id=1,
            order_id=10,
            business_intent="order_confirmed",
            channel="whatsapp",
            source_event_id="evt-1",
            transition_version="v1",
        )
        key_b = build_lifecycle_idempotency_key(
            tenant_id=1,
            order_id=10,
            business_intent="order_confirmed",
            channel="whatsapp",
            source_event_id="evt-1",
            transition_version="v2",
        )
        assert key_a != key_b


class TestReserveShadowDecision:
    def test_first_reservation_succeeds(self):
        db, result = _reserve()
        assert result.duplicate is False
        assert result.outcome == ShadowLedgerOutcome.SHADOW_RESERVED.value
        row = db.query(CommerceLifecycleNotificationLedger).one()
        assert row.tenant_id == 1
        assert row.order_id == 100

    def test_same_idempotency_key_returns_duplicate(self):
        db, first = _reserve()
        second = reserve_shadow_decision(
            db,
            tenant_id=1,
            order_id=100,
            business_intent=BusinessIntent.ORDER_CONFIRMED,
            channel="whatsapp",
            source_event_id="evt-1",
            transition_version="v1",
        )
        assert first.ledger_id == second.ledger_id
        assert second.duplicate is True
        assert db.query(CommerceLifecycleNotificationLedger).count() == 1

    def test_different_intent_allowed(self):
        db, _ = _reserve()
        other = reserve_shadow_decision(
            db,
            tenant_id=1,
            order_id=100,
            business_intent=BusinessIntent.SHIPMENT_AVAILABLE,
            channel="whatsapp",
            source_event_id="evt-1",
            transition_version="v1",
        )
        db.commit()
        assert other.duplicate is False
        assert db.query(CommerceLifecycleNotificationLedger).count() == 2

    def test_different_transition_version_allowed(self):
        db, _ = _reserve()
        other = reserve_shadow_decision(
            db,
            tenant_id=1,
            order_id=100,
            business_intent=BusinessIntent.ORDER_CONFIRMED,
            channel="whatsapp",
            source_event_id="evt-1",
            transition_version="v2",
        )
        db.commit()
        assert other.duplicate is False

    def test_different_tenant_isolated(self):
        db, _ = _reserve(tenant_id=1)
        other = reserve_shadow_decision(
            db,
            tenant_id=2,
            order_id=100,
            business_intent=BusinessIntent.ORDER_CONFIRMED,
            channel="whatsapp",
            source_event_id="evt-1",
            transition_version="v1",
        )
        db.commit()
        assert other.duplicate is False
        assert db.query(CommerceLifecycleNotificationLedger).count() == 2

    def test_invalid_business_intent_rejected(self):
        db, _ = _make_db()
        with pytest.raises(ValueError, match="unsupported business_intent"):
            reserve_shadow_decision(
                db,
                tenant_id=1,
                order_id=1,
                business_intent="salla_shipped",
                channel="whatsapp",
            )

    def test_missing_tenant_rejected(self):
        db, _ = _make_db()
        with pytest.raises(ValueError, match="tenant_id"):
            reserve_shadow_decision(
                db,
                tenant_id=0,
                order_id=1,
                business_intent=BusinessIntent.ORDER_CONFIRMED,
                channel="whatsapp",
            )

    def test_missing_order_rejected(self):
        db, _ = _make_db()
        with pytest.raises(ValueError, match="order_id"):
            reserve_shadow_decision(
                db,
                tenant_id=1,
                order_id=0,
                business_intent=BusinessIntent.ORDER_CONFIRMED,
                channel="whatsapp",
            )


class TestPrivacySnapshots:
    def test_evidence_snapshot_keys_only(self):
        db, _ = _reserve(evidence_present=["order_number", "delivered_at"])
        row = db.query(CommerceLifecycleNotificationLedger).one()
        assert row.evidence_present_json == ["order_number", "delivered_at"]

    def test_capabilities_snapshot_booleans_only(self):
        with pytest.raises(ValueError, match="boolean"):
            sanitize_capabilities_snapshot({"has_payment_link": "yes"})

    def test_message_text_cannot_be_stored(self):
        db, _ = _make_db()
        with pytest.raises(ValueError, match="message_text"):
            reserve_shadow_decision(
                db,
                tenant_id=1,
                order_id=1,
                business_intent=BusinessIntent.ORDER_CONFIRMED,
                channel="whatsapp",
                dispatch_decision={"message_text": "hello"},
            )

    def test_prompt_and_token_rejected(self):
        with pytest.raises(ValueError, match="prompt"):
            sanitize_dispatch_decision({"prompt": "system"})
        with pytest.raises(ValueError, match="token"):
            sanitize_dispatch_decision({"access_token": "secret"})

    def test_payment_url_key_rejected_in_dispatch_decision(self):
        with pytest.raises(ValueError, match="payment_url"):
            sanitize_dispatch_decision({"payment_url": "https://pay.example/x"})


class TestMarkShadowOutcome:
    def test_outcome_transition_controlled(self):
        db, reserved = _reserve()
        marked = mark_shadow_outcome(
            db,
            ledger_id=reserved.ledger_id,
            tenant_id=1,
            outcome=ShadowLedgerOutcome.SHADOW_ELIGIBLE,
            reason_code="missing_evidence",
        )
        db.commit()
        assert marked.outcome == ShadowLedgerOutcome.SHADOW_ELIGIBLE.value
        with pytest.raises(ValueError, match="transition not allowed"):
            mark_shadow_outcome(
                db,
                ledger_id=reserved.ledger_id,
                tenant_id=1,
                outcome=ShadowLedgerOutcome.SHADOW_BLOCKED,
            )

    def test_invalid_outcome_rejected(self):
        db, reserved = _reserve()
        with pytest.raises(ValueError, match="invalid shadow ledger outcome"):
            mark_shadow_outcome(
                db,
                ledger_id=reserved.ledger_id,
                tenant_id=1,
                outcome="sent",
            )

    def test_reserved_outcome_not_markable(self):
        db, reserved = _reserve()
        with pytest.raises(ValueError, match="not markable"):
            mark_shadow_outcome(
                db,
                ledger_id=reserved.ledger_id,
                tenant_id=1,
                outcome=ShadowLedgerOutcome.SHADOW_RESERVED,
            )


class TestConcurrency:
    def test_concurrent_duplicate_attempts_produce_one_row(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            engine = create_engine(
                f"sqlite:///{path}",
                connect_args={"check_same_thread": False},
            )
            table = CommerceLifecycleNotificationLedger.__table__
            saved: list = []
            for col in table.columns:
                if isinstance(col.type, JSONB):
                    saved.append((col, col.type))
                    col.type = JSON()
            table.create(engine)
            for col, orig in saved:
                col.type = orig
            Session = sessionmaker(bind=engine)
            barrier = threading.Barrier(2)
            results = []

            def _worker():
                session = Session()
                try:
                    barrier.wait(timeout=5)
                    results.append(
                        reserve_shadow_decision(
                            session,
                            tenant_id=9,
                            order_id=42,
                            business_intent=BusinessIntent.ORDER_DELIVERED,
                            channel="whatsapp",
                            source_event_id="evt-concurrent",
                            transition_version="v1",
                        )
                    )
                    session.commit()
                finally:
                    session.close()

            t1 = threading.Thread(target=_worker)
            t2 = threading.Thread(target=_worker)
            t1.start()
            t2.start()
            t1.join()
            t2.join()

            verify = Session()
            try:
                assert verify.query(CommerceLifecycleNotificationLedger).count() == 1
                assert sum(1 for r in results if r.duplicate) == 1
                assert sum(1 for r in results if not r.duplicate) == 1
            finally:
                verify.close()
        finally:
            try:
                os.remove(path)
            except OSError:
                pass


class TestArchitectureIsolation:
    def test_shadow_service_imports_no_send_ai_automation(self):
        path = BACKEND_DIR / "core" / "commerce_lifecycle" / "ledger.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        forbidden = ("modules.ai", "automation_engine", "meta", "openai")
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    for bad in forbidden:
                        assert bad not in alias.name
            elif isinstance(node, ast.ImportFrom) and node.module:
                for bad in forbidden:
                    assert bad not in node.module

    def test_feature_flag_defaults_false(self, monkeypatch):
        monkeypatch.delenv("COMMERCE_LIFECYCLE_SHADOW_LEDGER_ENABLED", raising=False)
        assert is_shadow_ledger_enabled() is False


class TestMigrationStructure:
    def test_migration_revision_chain(self):
        mod = importlib.import_module(
            "database.migrations.versions.0086_commerce_lifecycle_notification_ledger"
        )
        assert mod.revision == "0086"
        assert mod.down_revision == "0085"
        assert callable(mod.upgrade)
        assert callable(mod.downgrade)
