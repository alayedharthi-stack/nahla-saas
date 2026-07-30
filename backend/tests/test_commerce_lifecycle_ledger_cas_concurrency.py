"""
CAS concurrency tests for commerce lifecycle ledger send gates.

SQLite/threaded unit proofs plus PostgreSQL integration proofs for A1 CI.
"""
from __future__ import annotations

import asyncio
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Tuple
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import JSON, create_engine, inspect, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from core.commerce_lifecycle.dispatch import (  # noqa: E402
    commerce_lifecycle_send_audit_schema_ready,
    dispatch_external_lifecycle_notification,
)
from core.commerce_lifecycle.intents import BusinessIntent  # noqa: E402
from core.commerce_lifecycle.ledger import (  # noqa: E402
    SendLedgerOutcome,
    ShadowLedgerOutcome,
    finalize_send_outcome,
    mark_send_sending,
    reserve_send_decision,
    reserve_shadow_decision,
    mark_shadow_outcome,
    try_conditional_promote_shadow_send_row,
)
from models import CommerceLifecycleNotificationLedger, Tenant, TenantSettings  # noqa: E402

_GENERIC_TENANT_ID = 10
_GENERIC_ORDER_ID = 8801
_GENERIC_EVENT = "evt-cas-generic"
_GENERIC_VERSION = "v1"
_GENERIC_SERVICE_KEY = "order_confirmation"


def _install_ledger_table(engine) -> None:
    saved: list = []
    table = CommerceLifecycleNotificationLedger.__table__
    for col in table.columns:
        if isinstance(col.type, JSONB):
            saved.append((col, col.type))
            col.type = JSON()
    table.create(engine)
    for col, orig in saved:
        col.type = orig


def _make_sqlite_db() -> Tuple[Any, Any]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    _install_ledger_table(engine)
    Session = sessionmaker(bind=engine)
    return Session(), engine


def _make_threadsafe_sqlite_engine(db_path: Path):
    """File-backed SQLite — each session gets its own connection (thread-safe)."""
    engine = create_engine(
        f"sqlite:///{db_path.as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    _install_ledger_table(engine)
    with engine.connect() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL"))
        conn.commit()
    return engine


def _reserve_kwargs(**overrides):
    base = dict(
        tenant_id=_GENERIC_TENANT_ID,
        order_id=_GENERIC_ORDER_ID,
        business_intent=BusinessIntent.ORDER_CONFIRMED,
        channel="whatsapp",
        source_event_id=_GENERIC_EVENT,
        transition_version=_GENERIC_VERSION,
    )
    base.update(overrides)
    return base


def _send_reserve_kwargs(**overrides):
    return _reserve_kwargs(template_service_key=_GENERIC_SERVICE_KEY, **overrides)


def _seed_shadow_row(db) -> int:
    shadow = reserve_shadow_decision(
        db,
        **_reserve_kwargs(),
        dispatch_decision={"handoff_kind": "lifecycle_notification"},
        capabilities_snapshot={"has_external_tracking": True},
        evidence_present=["order_number"],
        commit=True,
    )
    mark_shadow_outcome(
        db,
        ledger_id=shadow.ledger_id,
        tenant_id=_GENERIC_TENANT_ID,
        outcome=ShadowLedgerOutcome.SHADOW_ELIGIBLE,
        commit=True,
    )
    return int(shadow.ledger_id)


class TestShadowPromotionCasSqlite:
    def test_second_shadow_promotion_call_is_loser(self):
        db, _ = _make_sqlite_db()
        ledger_id = _seed_shadow_row(db)
        first = try_conditional_promote_shadow_send_row(
            db,
            tenant_id=_GENERIC_TENANT_ID,
            ledger_id=ledger_id,
            service_key_audit=_GENERIC_SERVICE_KEY,
        )
        db.commit()
        second = try_conditional_promote_shadow_send_row(
            db,
            tenant_id=_GENERIC_TENANT_ID,
            ledger_id=ledger_id,
            service_key_audit=_GENERIC_SERVICE_KEY,
        )
        db.commit()

        row = db.query(CommerceLifecycleNotificationLedger).one()
        assert first is True
        assert second is False
        assert row.send_state == "reserved"
        assert row.outcome == SendLedgerOutcome.SEND_RESERVED.value

    def test_concurrent_shadow_promotion_has_single_winner(self, tmp_path: Path):
        engine = _make_threadsafe_sqlite_engine(tmp_path / "shadow_promotion_cas.sqlite")
        Session = sessionmaker(bind=engine)
        with Session() as setup:
            ledger_id = _seed_shadow_row(setup)

        barrier = threading.Barrier(2)
        winners: list[bool] = []
        winners_lock = threading.Lock()

        def _promote() -> None:
            with Session() as session:
                barrier.wait(timeout=10)
                won = try_conditional_promote_shadow_send_row(
                    session,
                    tenant_id=_GENERIC_TENANT_ID,
                    ledger_id=ledger_id,
                    service_key_audit=_GENERIC_SERVICE_KEY,
                )
                session.commit()
                with winners_lock:
                    winners.append(won)

        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(lambda _: _promote(), range(2)))

        with Session() as verify:
            row = verify.query(CommerceLifecycleNotificationLedger).one()

        assert sorted(winners) == [False, True]
        assert row.send_state == "reserved"
        assert row.outcome == SendLedgerOutcome.SEND_RESERVED.value


class TestMarkSendSendingCasSqlite:
    def test_second_reserved_to_sending_call_is_loser(self):
        db, _ = _make_sqlite_db()
        reserve = reserve_send_decision(db, commit=True, **_send_reserve_kwargs())
        first = mark_send_sending(
            db,
            ledger_id=reserve.ledger_id,
            tenant_id=_GENERIC_TENANT_ID,
            template_name="order_confirmed_generic_ar",
            template_service_key=_GENERIC_SERVICE_KEY,
            commit=True,
        )
        second = mark_send_sending(
            db,
            ledger_id=reserve.ledger_id,
            tenant_id=_GENERIC_TENANT_ID,
            template_name="order_confirmed_generic_ar",
            template_service_key=_GENERIC_SERVICE_KEY,
            commit=True,
        )

        row = db.query(CommerceLifecycleNotificationLedger).one()
        assert first.transitioned is True
        assert second.transitioned is False
        assert row.send_state == "sending"
        assert row.send_attempt_count == 1


class TestFailedRetrySqlite:
    def test_failed_retry_then_sent(self):
        db, _ = _make_sqlite_db()
        first = reserve_send_decision(db, commit=True, **_send_reserve_kwargs())
        sending = mark_send_sending(
            db,
            ledger_id=first.ledger_id,
            tenant_id=_GENERIC_TENANT_ID,
            commit=True,
        )
        assert sending.transitioned is True
        finalize_send_outcome(
            db,
            ledger_id=first.ledger_id,
            tenant_id=_GENERIC_TENANT_ID,
            outcome=SendLedgerOutcome.FAILED,
            send_error_code="provider_timeout",
            commit=True,
        )

        retry = reserve_send_decision(db, commit=True, **_send_reserve_kwargs())
        assert retry.duplicate is False
        assert retry.recovered is True

        retry_send = mark_send_sending(
            db,
            ledger_id=retry.ledger_id,
            tenant_id=_GENERIC_TENANT_ID,
            commit=True,
        )
        assert retry_send.transitioned is True
        finalize_send_outcome(
            db,
            ledger_id=retry.ledger_id,
            tenant_id=_GENERIC_TENANT_ID,
            outcome=SendLedgerOutcome.SENT,
            provider_message_id="wamid.retry.generic",
            commit=True,
        )

        row = db.query(CommerceLifecycleNotificationLedger).one()
        assert row.send_state == "sent"
        assert row.send_attempt_count == 2

    def test_retry_exhausted_blocks_third_reservation(self):
        db, _ = _make_sqlite_db()
        for attempt in range(2):
            reserve = reserve_send_decision(db, commit=True, **_send_reserve_kwargs())
            mark_send_sending(
                db,
                ledger_id=reserve.ledger_id,
                tenant_id=_GENERIC_TENANT_ID,
                commit=True,
            )
            finalize_send_outcome(
                db,
                ledger_id=reserve.ledger_id,
                tenant_id=_GENERIC_TENANT_ID,
                outcome=SendLedgerOutcome.FAILED,
                send_error_code="provider_error",
                commit=True,
            )

        blocked = reserve_send_decision(db, commit=True, **_send_reserve_kwargs())
        assert blocked.duplicate is True
        row = db.query(CommerceLifecycleNotificationLedger).one()
        assert row.send_state == "failed"
        assert row.send_attempt_count == 2


try:
    from tests.order_customer_identity_postgres_fixtures import (  # noqa: E402
        _connect_engine,
        _ensure_a1_schema,
    )

    _PG_FIXTURES_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PG_FIXTURES_AVAILABLE = False


def _upgrade_pg(engine, revision: str) -> None:
    import os

    from alembic import command
    from alembic.config import Config

    prev_cwd = os.getcwd()
    try:
        os.chdir(DATABASE_DIR)
        cfg = Config("alembic.ini")
        cfg.set_main_option(
            "sqlalchemy.url",
            str(engine.url.render_as_string(hide_password=False)),
        )
        os.environ["DATABASE_URL"] = str(engine.url.render_as_string(hide_password=False))
        command.upgrade(cfg, revision)
    finally:
        os.chdir(prev_cwd)


@pytest.fixture(scope="module")
def lifecycle_pg_engine_0093():
    if not _PG_FIXTURES_AVAILABLE:
        pytest.skip("postgres fixtures unavailable")
    engine = _connect_engine()
    _ensure_a1_schema(engine)
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def lifecycle_pg_engine(lifecycle_pg_engine_0093):
    _upgrade_pg(lifecycle_pg_engine_0093, "0094")
    yield lifecycle_pg_engine_0093


def _pg_tenant_id() -> int:
    return 995_000_000 + (uuid.uuid4().int % 900_000)


def _pg_tenant_name(tenant_id: int) -> str:
    # Tenant.name is globally unique on PostgreSQL.
    return f"متجر تجريبي عام-{tenant_id}"


def _pg_ensure_tenant(session, tenant_id: int) -> None:
    if session.get(Tenant, tenant_id) is None:
        session.add(Tenant(id=tenant_id, name=_pg_tenant_name(tenant_id)))
    # Pre-create settings so concurrent dispatch workers do not race on
    # get_or_create_settings → tenant_settings_tenant_id_key.
    existing_settings = (
        session.query(TenantSettings).filter_by(tenant_id=tenant_id).one_or_none()
    )
    if existing_settings is None:
        session.add(TenantSettings(tenant_id=tenant_id))


def _pg_seed_shadow(engine, *, tenant_id: int, order_id: int) -> int:
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    session = Session()
    try:
        _pg_ensure_tenant(session, tenant_id)
        shadow = reserve_shadow_decision(
            session,
            tenant_id=tenant_id,
            order_id=order_id,
            business_intent=BusinessIntent.ORDER_CONFIRMED,
            channel="whatsapp",
            source_event_id=f"evt-pg-{tenant_id}",
            transition_version="v1",
            dispatch_decision={"handoff_kind": "lifecycle_notification"},
            capabilities_snapshot={"has_external_tracking": True},
            evidence_present=["order_number"],
        )
        mark_shadow_outcome(
            session,
            ledger_id=shadow.ledger_id,
            tenant_id=tenant_id,
            outcome=ShadowLedgerOutcome.SHADOW_ELIGIBLE,
        )
        session.commit()
        return int(shadow.ledger_id)
    finally:
        session.close()


def _pg_reserve_send(engine, *, tenant_id: int, order_id: int) -> bool:
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    session = Session()
    try:
        result = reserve_send_decision(
            session,
            tenant_id=tenant_id,
            order_id=order_id,
            business_intent=BusinessIntent.ORDER_CONFIRMED,
            channel="whatsapp",
            source_event_id=f"evt-pg-{tenant_id}",
            transition_version="v1",
            template_service_key=_GENERIC_SERVICE_KEY,
            commit=True,
        )
        return result.duplicate
    finally:
        session.close()


@pytest.mark.skipif(not _PG_FIXTURES_AVAILABLE, reason="postgres fixtures unavailable")
class TestShadowPromotionCasPostgres:
    def test_concurrent_shadow_promotion_single_winner_pg(self, lifecycle_pg_engine) -> None:
        tenant_id = _pg_tenant_id()
        order_id = 8801
        _pg_seed_shadow(lifecycle_pg_engine, tenant_id=tenant_id, order_id=order_id)

        with ThreadPoolExecutor(max_workers=2) as executor:
            duplicates = list(
                executor.map(
                    lambda _: _pg_reserve_send(
                        lifecycle_pg_engine,
                        tenant_id=tenant_id,
                        order_id=order_id,
                    ),
                    range(2),
                )
            )

        Session = sessionmaker(bind=lifecycle_pg_engine)
        with Session() as session:
            row = (
                session.query(CommerceLifecycleNotificationLedger)
                .filter_by(tenant_id=tenant_id, order_id=order_id)
                .one()
            )
            assert sorted(duplicates) == [False, True]
            assert row.send_state == "reserved"


def _pg_mark_sending(engine, *, tenant_id: int, ledger_id: int) -> bool:
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    session = Session()
    try:
        result = mark_send_sending(
            session,
            ledger_id=ledger_id,
            tenant_id=tenant_id,
            template_name="order_confirmed_generic_ar",
            template_service_key=_GENERIC_SERVICE_KEY,
            commit=True,
        )
        return result.transitioned
    finally:
        session.close()


@pytest.mark.skipif(not _PG_FIXTURES_AVAILABLE, reason="postgres fixtures unavailable")
class TestMarkSendSendingCasPostgres:
    def test_concurrent_reserved_to_sending_single_winner_pg(self, lifecycle_pg_engine) -> None:
        tenant_id = _pg_tenant_id()
        order_id = 8802
        Session = sessionmaker(bind=lifecycle_pg_engine, expire_on_commit=False)
        session = Session()
        try:
            _pg_ensure_tenant(session, tenant_id)
            reserve = reserve_send_decision(
                session,
                tenant_id=tenant_id,
                order_id=order_id,
                business_intent=BusinessIntent.ORDER_CONFIRMED,
                channel="whatsapp",
                source_event_id=f"evt-send-{tenant_id}",
                transition_version="v1",
                template_service_key=_GENERIC_SERVICE_KEY,
            )
            ledger_id = int(reserve.ledger_id)
            session.commit()
        finally:
            session.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            transitions = list(
                executor.map(
                    lambda _: _pg_mark_sending(
                        lifecycle_pg_engine,
                        tenant_id=tenant_id,
                        ledger_id=ledger_id,
                    ),
                    range(2),
                )
            )

        with Session() as session:
            row = session.get(CommerceLifecycleNotificationLedger, ledger_id)
            assert sorted(transitions) == [False, True]
            assert row is not None
            assert row.send_state == "sending"
            assert row.send_attempt_count == 1


def _generic_order():
    return SimpleNamespace(
        id=501,
        external_id="generic-ord-8801",
        external_order_number="ORD-GEN-8801",
        status="under_review",
        checkout_url="https://shop.generic.example/checkout/8801",
        customer_name="أحمد سالم",
        customer_info={"phone": "+966500111222"},
        extra_metadata={"payment_method": "cod"},
    )


def _merchant_caps():
    return SimpleNamespace(
        to_dict=lambda: {
            "has_external_store": True,
            "supports_external_checkout": True,
            "supports_external_coupons": False,
            "supports_whatsapp_orders": True,
            "supports_nahla_orders": False,
            "supports_bank_transfer": False,
            "supports_cod": True,
            "has_whatsapp_catalog": False,
            "has_external_tracking": True,
            "has_nahla_tracking": False,
            "has_payment_link": True,
        }
    )


def _approved_template():
    return SimpleNamespace(
        id=11,
        name="order_confirmed_generic_ar",
        language="ar",
        components=[{"type": "BODY", "text": "مرحبا {{1}} طلب {{2}}"}],
    )


def _dispatch_kwargs(db, *, tenant_id: int):
    return dict(
        db=db,
        tenant_id=tenant_id,
        order=_generic_order(),
        provider="salla",
        raw_previous_status=None,
        raw_current_status="under_review",
        normalized_order={
            "external_id": "generic-ord-8801",
            "status": "under_review",
            "external_order_number": "ORD-GEN-8801",
        },
        raw_payload={"event_id": "evt-dispatch-pg", "updated_at": "2026-07-30T10:00:00Z"},
    )


@pytest.mark.skipif(not _PG_FIXTURES_AVAILABLE, reason="postgres fixtures unavailable")
class TestDispatchProviderCasPostgres:
    @patch("core.automation_engine.send_lifecycle_whatsapp_template", new_callable=AsyncMock)
    @patch("core.service_template_resolver.resolve_template_for_send")
    @patch("core.merchant_capabilities.resolve_merchant_capabilities")
    def test_concurrent_dispatch_single_provider_call_pg(
        self,
        mock_caps,
        mock_resolve_tpl,
        mock_send,
        lifecycle_pg_engine,
        monkeypatch,
    ) -> None:
        monkeypatch.setenv("COMMERCE_LIFECYCLE_DISPATCH_ENABLED", "true")
        monkeypatch.setenv("COMMERCE_LIFECYCLE_SEND_STALE_SECONDS", "300")
        monkeypatch.setenv("COMMERCE_LIFECYCLE_DISPATCH_RECIPIENT_ALLOWLIST", "+966500111222")
        mock_caps.return_value = _merchant_caps()
        mock_resolve_tpl.return_value = _approved_template()
        mock_send.return_value = ("sent", {"wa_message_id": "wamid.pg.cas"})

        tenant_id = _pg_tenant_id()
        monkeypatch.setenv(
            "COMMERCE_LIFECYCLE_DISPATCH_TENANT_ALLOWLIST",
            str(tenant_id),
        )
        Session = sessionmaker(bind=lifecycle_pg_engine)

        def _run_dispatch() -> bool:
            session = Session()
            try:
                result = asyncio.run(
                    dispatch_external_lifecycle_notification(
                        **_dispatch_kwargs(session, tenant_id=tenant_id)
                    )
                )
                session.commit()
                return bool(result.dispatched)
            finally:
                session.close()

        with Session.begin() as session:
            _pg_ensure_tenant(session, tenant_id)

        with ThreadPoolExecutor(max_workers=2) as executor:
            dispatched_flags = list(executor.map(lambda _: _run_dispatch(), range(2)))

        assert dispatched_flags.count(True) == 1
        assert mock_send.await_count == 1

        with Session() as session:
            rows = (
                session.query(CommerceLifecycleNotificationLedger)
                .filter_by(tenant_id=tenant_id)
                .all()
            )
            assert len(rows) == 1
            assert rows[0].send_state == "sent"


@pytest.mark.skipif(not _PG_FIXTURES_AVAILABLE, reason="postgres fixtures unavailable")
class TestMigration0094GuardPostgres:
    def test_pre_0094_schema_blocks_dispatch_without_provider_call(
        self,
        lifecycle_pg_engine_0093,
        monkeypatch,
    ) -> None:
        monkeypatch.setenv("COMMERCE_LIFECYCLE_DISPATCH_ENABLED", "true")
        Session = sessionmaker(bind=lifecycle_pg_engine_0093)
        with Session() as session:
            assert commerce_lifecycle_send_audit_schema_ready(session) is False
            insp = inspect(lifecycle_pg_engine_0093)
            columns = {
                col["name"]
                for col in insp.get_columns("commerce_lifecycle_notification_ledger")
            }
            assert "send_state" not in columns

        with patch(
            "core.automation_engine.send_lifecycle_whatsapp_template",
            new_callable=AsyncMock,
        ) as mock_send, patch(
            "core.service_template_resolver.resolve_template_for_send",
            return_value=_approved_template(),
        ), patch(
            "core.merchant_capabilities.resolve_merchant_capabilities",
            return_value=_merchant_caps(),
        ):
            with Session.begin() as session:
                tenant_id = _pg_tenant_id()
                _pg_ensure_tenant(session, tenant_id)
            # Allowlists must pass so the 0093 schema gate is the observed reason.
            monkeypatch.setenv(
                "COMMERCE_LIFECYCLE_DISPATCH_TENANT_ALLOWLIST",
                str(tenant_id),
            )
            monkeypatch.setenv(
                "COMMERCE_LIFECYCLE_DISPATCH_RECIPIENT_ALLOWLIST",
                "+966500111222",
            )
            with Session() as session:
                result = asyncio.run(
                    dispatch_external_lifecycle_notification(
                        **_dispatch_kwargs(session, tenant_id=tenant_id)
                    )
                )
            assert result.reason_code == "migration_0094_required"
            assert result.dispatched is False
            assert result.ledger_id is None
            assert mock_send.await_count == 0
