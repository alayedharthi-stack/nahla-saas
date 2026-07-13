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
from sqlalchemy import JSON, Column, Integer, MetaData, Table, create_engine, text
from sqlalchemy.dialects.postgresql import JSONB
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
    mark_shadow_outcome,
    reserve_shadow_decision,
    sanitize_capabilities_snapshot,
    sanitize_dispatch_decision,
    sanitize_evidence_present,
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


def _reserve(db=None, **kwargs):
    defaults = dict(
        tenant_id=1,
        order_id=100,
        business_intent=BusinessIntent.ORDER_CONFIRMED,
        channel="whatsapp",
        source_event_id="evt-1",
        transition_version="v1",
        capabilities_snapshot={"has_external_tracking": True},
        evidence_present=["order_number"],
        dispatch_decision={"handoff_kind": "lifecycle_notification"},
    )
    defaults.update(kwargs)
    if db is None:
        db, _ = _make_db()
    result = reserve_shadow_decision(db, **defaults)
    return db, result


class TestIdempotencyKey:
    def test_same_transition_produces_same_key(self):
        kwargs = dict(
            tenant_id=1,
            order_id=10,
            business_intent="order_confirmed",
            channel="whatsapp",
            source_event_id="evt-1",
            transition_version="v1",
        )
        assert build_lifecycle_idempotency_key(**kwargs) == build_lifecycle_idempotency_key(**kwargs)

    def test_different_transition_version_produces_different_key(self):
        base = dict(
            tenant_id=1,
            order_id=10,
            business_intent="order_confirmed",
            channel="whatsapp",
            source_event_id="evt-1",
        )
        assert build_lifecycle_idempotency_key(**base, transition_version="v1") != (
            build_lifecycle_idempotency_key(**base, transition_version="v2")
        )

    def test_delimiter_in_source_event_id_does_not_collide(self):
        key_a = build_lifecycle_idempotency_key(
            tenant_id=1,
            order_id=10,
            business_intent="order_confirmed",
            channel="whatsapp",
            source_event_id="evt:part:a",
            transition_version="v1",
        )
        key_b = build_lifecycle_idempotency_key(
            tenant_id=1,
            order_id=10,
            business_intent="order_confirmed",
            channel="whatsapp",
            source_event_id="evt",
            transition_version="part:a:v1",
        )
        assert key_a != key_b

    def test_none_and_empty_string_are_not_interchangeable(self):
        with_none = build_lifecycle_idempotency_key(
            tenant_id=1,
            order_id=10,
            business_intent="order_confirmed",
            channel="whatsapp",
            source_event_id=None,
            transition_version=None,
        )
        with pytest.raises(ValueError, match="must be None or non-empty"):
            build_lifecycle_idempotency_key(
                tenant_id=1,
                order_id=10,
                business_intent="order_confirmed",
                channel="whatsapp",
                source_event_id="",
                transition_version="v1",
            )
        assert with_none

    def test_whitespace_only_source_event_id_rejected(self):
        with pytest.raises(ValueError, match="source_event_id"):
            build_lifecycle_idempotency_key(
                tenant_id=1,
                order_id=10,
                business_intent="order_confirmed",
                channel="whatsapp",
                source_event_id="   ",
                transition_version="v1",
            )


class TestReserveShadowDecision:
    def test_first_reservation_succeeds(self):
        db, result = _reserve()
        db.commit()
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

    def test_duplicate_does_not_overwrite_original_outcome(self):
        db, first = _reserve()
        mark_shadow_outcome(
            db,
            ledger_id=first.ledger_id,
            tenant_id=1,
            outcome=ShadowLedgerOutcome.SHADOW_ELIGIBLE,
        )
        duplicate = reserve_shadow_decision(
            db,
            tenant_id=1,
            order_id=100,
            business_intent=BusinessIntent.ORDER_CONFIRMED,
            channel="whatsapp",
            source_event_id="evt-1",
            transition_version="v1",
        )
        row = db.query(CommerceLifecycleNotificationLedger).one()
        assert duplicate.duplicate is True
        assert duplicate.outcome == ShadowLedgerOutcome.SHADOW_ELIGIBLE.value
        assert row.outcome == ShadowLedgerOutcome.SHADOW_ELIGIBLE.value

    def test_outer_transaction_survives_duplicate_conflict(self):
        db, engine = _make_db()
        metadata = MetaData()
        scratch = Table(
            "scratch_rows",
            metadata,
            Column("id", Integer, primary_key=True),
            Column("value", Integer, nullable=False),
        )
        scratch.create(engine)
        db.execute(scratch.insert().values(value=7))
        db.flush()

        _reserve(db=db)
        duplicate = reserve_shadow_decision(
            db,
            tenant_id=1,
            order_id=100,
            business_intent=BusinessIntent.ORDER_CONFIRMED,
            channel="whatsapp",
            source_event_id="evt-1",
            transition_version="v1",
        )
        assert duplicate.duplicate is True
        assert db.execute(text("SELECT COUNT(*) FROM scratch_rows")).scalar() == 1
        db.execute(text("SELECT 1"))
        db.commit()

        verify = sessionmaker(bind=engine)()
        try:
            assert verify.query(CommerceLifecycleNotificationLedger).count() == 1
            assert verify.execute(text("SELECT COUNT(*) FROM scratch_rows")).scalar() == 1
        finally:
            verify.close()

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

    def test_invalid_business_intent_rejected(self):
        db, _ = _make_db()
        with pytest.raises(ValueError, match="unsupported business_intent"):
            reserve_shadow_decision(
                db,
                tenant_id=1,
                order_id=1,
                business_intent="salla_shipped",
                channel="whatsapp",
                source_event_id="evt-1",
                transition_version="v1",
            )

    def test_invalid_channel_rejected(self):
        db, _ = _make_db()
        with pytest.raises(ValueError, match="unsupported channel"):
            reserve_shadow_decision(
                db,
                tenant_id=1,
                order_id=1,
                business_intent=BusinessIntent.ORDER_CONFIRMED,
                channel="sms",
                source_event_id="evt-1",
                transition_version="v1",
            )


class TestPrivacySnapshots:
    def test_evidence_snapshot_keys_only(self):
        db, _ = _reserve(evidence_present=["order_number", "delivered_at"])
        db.commit()
        row = db.query(CommerceLifecycleNotificationLedger).one()
        assert row.evidence_present_json == ["order_number", "delivered_at"]

    def test_unknown_evidence_field_rejected(self):
        with pytest.raises(ValueError, match="unknown evidence field"):
            sanitize_evidence_present(["not_a_real_field"])

    def test_capabilities_snapshot_booleans_only(self):
        with pytest.raises(ValueError, match="boolean"):
            sanitize_capabilities_snapshot({"has_payment_link": "yes"})

    def test_unknown_capability_field_rejected(self):
        with pytest.raises(ValueError, match="unknown capability field"):
            sanitize_capabilities_snapshot({"not_a_capability": True})

    def test_message_text_cannot_be_stored(self):
        db, _ = _make_db()
        with pytest.raises(ValueError, match="message_text"):
            reserve_shadow_decision(
                db,
                tenant_id=1,
                order_id=1,
                business_intent=BusinessIntent.ORDER_CONFIRMED,
                channel="whatsapp",
                source_event_id="evt-1",
                transition_version="v1",
                dispatch_decision={"message_text": "hello"},
            )

    def test_nested_sensitive_key_rejected(self):
        with pytest.raises(ValueError, match="token"):
            sanitize_dispatch_decision({"meta": {"access_token": "secret"}})

    def test_unknown_dispatch_key_rejected(self):
        with pytest.raises(ValueError, match="unknown dispatch_decision key"):
            sanitize_dispatch_decision({"arbitrary_field": "value"})

    def test_valid_allowlisted_decision_persists(self):
        db, _ = _reserve(
            dispatch_decision={
                "handoff_kind": "lifecycle_notification",
                "intent": "order_confirmed",
            }
        )
        db.commit()
        row = db.query(CommerceLifecycleNotificationLedger).one()
        assert row.dispatch_decision_json == {
            "handoff_kind": "lifecycle_notification",
            "intent": "order_confirmed",
        }

    def test_shadow_dispatch_dimensions_allowlisted(self):
        decision = sanitize_dispatch_decision({
            "handoff_kind": "external_lifecycle_shadow",
            "intent": "shipment_available",
            "reason_code": "eligible",
            "business_evidence_valid": "true",
            "capabilities_valid": "true",
            "template_evidence_valid": "false",
            "template_missing_evidence": "tracking_url",
        })
        assert decision["template_missing_evidence"] == "tracking_url"

    def test_no_destination_persisted(self):
        db, _ = _reserve()
        row = db.query(CommerceLifecycleNotificationLedger).one()
        dumped = repr(row)
        assert "destination" not in dumped.lower()


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

    def test_same_outcome_update_is_idempotent(self):
        db, reserved = _reserve()
        mark_shadow_outcome(
            db,
            ledger_id=reserved.ledger_id,
            tenant_id=1,
            outcome=ShadowLedgerOutcome.SHADOW_ELIGIBLE,
            reason_code="missing_evidence",
        )
        again = mark_shadow_outcome(
            db,
            ledger_id=reserved.ledger_id,
            tenant_id=1,
            outcome=ShadowLedgerOutcome.SHADOW_ELIGIBLE,
            reason_code="missing_evidence",
        )
        assert again.outcome == ShadowLedgerOutcome.SHADOW_ELIGIBLE.value

    def test_sent_outcome_impossible(self):
        db, reserved = _reserve()
        with pytest.raises(ValueError, match="not permitted"):
            mark_shadow_outcome(
                db,
                ledger_id=reserved.ledger_id,
                tenant_id=1,
                outcome="sent",
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
        forbidden = (
            "modules.ai",
            "automation_engine",
            "delivery_policy",
            "service_template_resolver",
            "meta",
            "openai",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    for bad in forbidden:
                        assert bad not in alias.name
            elif isinstance(node, ast.ImportFrom) and node.module:
                for bad in forbidden:
                    assert bad not in node.module


class TestMigrationStructure:
    def test_migration_revision_chain(self):
        mod = importlib.import_module(
            "database.migrations.versions.0086_commerce_lifecycle_notification_ledger"
        )
        assert mod.revision == "0086"
        assert mod.down_revision == "0085"
        assert callable(mod.upgrade)
        assert callable(mod.downgrade)
