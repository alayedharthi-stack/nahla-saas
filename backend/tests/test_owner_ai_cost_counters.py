"""Offline accounting proofs: no real model requests or production data."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from database.models import AIActionLog, AIUsageEvent, ConversationTrace, Tenant
from modules.ai.orchestrator import ai_usage_ledger as ledger
from modules.ai.orchestrator.ai_usage_pricing import compute_usage_cost_usd


@compiles(JSONB, "sqlite")
def _jsonb_for_test(_type, _compiler, **_kwargs):
    return "JSON"


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Tenant.__table__.create(engine)
    AIUsageEvent.__table__.create(engine)
    AIActionLog.__table__.create(engine)
    ConversationTrace.__table__.create(engine)
    with sessionmaker(bind=engine)() as session:
        session.add_all([Tenant(id=1, name="Generic apparel"), Tenant(id=33, name="Generic gifts")])
        session.commit()
        yield session
    engine.dispose()


def _response(request_id, *, usage=True):
    return SimpleNamespace(
        id=request_id,
        usage=SimpleNamespace(input_tokens=1000, output_tokens=100,
                              cache_read_input_tokens=0, cache_creation_input_tokens=0)
        if usage else None,
    )


def _record(db, tenant, request_id, *, usage=True):
    ledger.record_ai_usage_from_anthropic(
        db=db, audit_extra={"tenant_id": tenant, "reason": "offline.accounting"},
        model="claude-haiku-4-5-20251001", response=_response(request_id, usage=usage),
        total_prompt_chars=4000, reply_text="x" * 400,
    )
    db.commit()


def test_two_tenants_new_turn_and_duplicate_do_not_cross_charge(db):
    as_of = datetime.now(timezone.utc) + timedelta(minutes=1)
    _record(db, 1, "offline-one")
    _record(db, 33, "offline-thirty-three")
    before_1 = ledger.aggregate_tenant_ledger(db, 1, period="all", as_of=as_of)
    before_33 = ledger.aggregate_tenant_ledger(db, 33, period="all", as_of=as_of)
    _record(db, 1, "offline-next")
    after_1 = ledger.aggregate_tenant_ledger(db, 1, period="all", as_of=as_of)
    assert after_1["calls_total"] == before_1["calls_total"] + 1
    assert after_1["actual_total_cost_usd"] > before_1["actual_total_cost_usd"]
    assert ledger.aggregate_tenant_ledger(db, 33, period="all", as_of=as_of) == before_33
    _record(db, 1, "offline-next")
    assert ledger.aggregate_tenant_ledger(db, 1, period="all", as_of=as_of) == after_1
    assert db.query(AIUsageEvent).count() == 3


def test_recorded_tokens_are_separate_from_estimated_tokens_and_provider_bill(db):
    _record(db, 1, "offline-actual")
    _record(db, 1, "offline-estimated", usage=False)
    payload = ledger.aggregate_tenant_ledger(db, 1, period="all")
    assert payload["actual_total_cost_usd"] == 0.0015
    assert payload["estimated_total_cost_usd"] == 0.0015
    assert payload["provider_reported_total_cost_usd"] is None
    assert payload["cost_basis"] == "tokens_x_versioned_rates"


def test_official_active_model_rates_and_cache():
    costs = compute_usage_cost_usd(provider="anthropic", model="claude-haiku-4-5-20251001",
                                  input_tokens=1000, output_tokens=100,
                                  cache_read_tokens=200, cache_write_tokens=300)
    assert costs["total_cost_usd"] == Decimal("0.001895")
    opus = compute_usage_cost_usd(provider="anthropic", model="claude-opus-4-6",
                                 input_tokens=1_000_000, output_tokens=1_000_000)
    assert opus["total_cost_usd"] == Decimal("30")


def test_period_bounds_exclude_future_rows_and_keep_tenants_independent(db):
    _record(db, 1, "offline-current")
    _record(db, 33, "offline-old")
    db.query(AIUsageEvent).filter_by(tenant_id=33).one().created_at = (
        datetime.now(timezone.utc) - timedelta(days=8)
    )
    _record(db, 1, "offline-future")
    db.query(AIUsageEvent).filter_by(request_id="offline-future").one().created_at = (
        datetime.now(timezone.utc) + timedelta(days=1)
    )
    db.commit()
    assert ledger.aggregate_tenant_ledger(db, 1, period="7d")["calls_total"] == 1
    assert ledger.aggregate_tenant_ledger(db, 33, period="7d")["calls_total"] == 0


def test_ledger_read_failure_is_not_a_zero_cost():
    class BrokenDB:
        def query(self, *_args):
            raise RuntimeError("offline read failure")
    with pytest.raises(RuntimeError):
        ledger.aggregate_tenant_ledger(BrokenDB(), 1)


def test_provider_single_step_persists_measured_usage_without_changing_answer(db, monkeypatch):
    from unittest.mock import MagicMock
    from database import session as sessions
    from modules.ai.orchestrator.providers import anthropic_provider

    fake = _response("offline-provider-response")
    fake.content = [SimpleNamespace(type="text", text="offline answer")]
    fake.stop_reason = "end_turn"
    sdk = MagicMock()
    sdk.Anthropic.return_value.messages.create.return_value = fake
    monkeypatch.setattr(anthropic_provider, "_API_KEY", "offline-test-key")
    monkeypatch.setattr(anthropic_provider, "_SDK_AVAILABLE", True)
    monkeypatch.setattr(anthropic_provider, "_anthropic_sdk", sdk)
    monkeypatch.setattr(sessions, "SessionLocal", sessionmaker(bind=db.get_bind()))
    provider = anthropic_provider.AnthropicProvider()
    answer = provider.call_single_step(messages=[], system="offline test", audit_context={
        "tenant_id": 1, "turn_id": 123, "model": "claude-haiku-4-5-20251001",
        "reason": "commerce_runtime_pilot",
    })
    assert answer["status"] == "ok"
    assert answer["blocks"] == [{"type": "text", "text": "offline answer"}]
    assert answer["usage"]["input_tokens"] == 1000
    row = db.query(AIUsageEvent).one()
    assert row.tenant_id == 1 and row.turn_id == 123
    assert row.total_cost_usd == Decimal("0.00150000")
    assert sdk.Anthropic.call_args.kwargs["max_retries"] == 0
    assert sdk.Anthropic.return_value.messages.create.call_count == 1


def test_admin_api_costs_and_activity_share_the_same_period(db):
    import asyncio
    from routers import admin

    now = datetime.now(timezone.utc)
    for tenant, age in [(1, 0), (1, 8), (33, 0)]:
        created = now - timedelta(days=age)
        db.add(ConversationTrace(tenant_id=tenant, customer_phone="offline", created_at=created,
                                 orchestrator_used=True, latency_ms=10))
        db.add(AIActionLog(tenant_id=tenant, action_type="offline", policy_result="approved",
                           created_at=created))
    _record(db, 1, "offline-api-one")
    _record(db, 33, "offline-api-thirty-three")
    first = asyncio.run(admin.admin_ai_usage_tenant(1, db=db, _admin={}, period="7d"))
    assert first["turns_total"] == first["ai_actions_logged"] == first["calls_total"] == 1
    assert first["provider_reported_total_cost_usd"] is None
    _record(db, 1, "offline-api-next")
    after = asyncio.run(admin.admin_ai_usage_tenant(1, db=db, _admin={}, period="7d"))
    other = asyncio.run(admin.admin_ai_usage_tenant(33, db=db, _admin={}, period="7d"))
    costs = asyncio.run(admin.admin_ai_costs(db=db, _admin={}, period="7d"))
    assert first["actual_total_cost_usd"] == 0.0015
    assert after["actual_total_cost_usd"] == 0.003
    assert other["actual_total_cost_usd"] == 0.0015
    assert costs["actual_total_cost_usd"] == 0.0045
    assert len({item["tenant_id"] for item in costs["tenants"]}) == 2
    assert costs["pricing_versions"] == {"2026-09-30-v2": 3}


def test_duplicate_rejection_preserves_callers_unrelated_transaction(db):
    _record(db, 1, "offline-duplicate-savepoint")
    db.query(Tenant).filter_by(id=33).one().name = "Generic gifts renamed"
    _record(db, 1, "offline-duplicate-savepoint")
    assert db.query(Tenant).filter_by(id=33).one().name == "Generic gifts renamed"
    assert db.query(AIUsageEvent).count() == 1


def test_unknown_tenant_is_separate_and_missing_request_id_does_not_collapse_calls(db):
    _record(db, 1, None)
    _record(db, 1, None)
    _record(db, None, "offline-unattributed")
    assert ledger.aggregate_tenant_ledger(db, 1, period="all")["calls_total"] == 2
    assert ledger.aggregate_platform_ledger(db, period="all")["unattributed_total_cost_usd"] == 0.0015


def test_month_to_date_boundary_is_explicit_utc():
    now = datetime(2026, 9, 30, 17, 32, tzinfo=timezone.utc)
    assert ledger.ledger_period_start("mtd", now=now) == datetime(2026, 9, 1, tzinfo=timezone.utc)


def test_missing_stored_cost_is_identified_as_incomplete(db):
    _record(db, 1, "offline-no-cost")
    db.query(AIUsageEvent).one().total_cost_usd = None
    db.commit()
    assert ledger.aggregate_tenant_ledger(db, 1, period="all")["unpriced_calls"] == 1


def _migration():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[2] / "database/migrations/versions/0114_ai_usage_request_dedup.py"
    spec = importlib.util.spec_from_file_location("owner_cost_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_blocks_historical_duplicates_without_deleting_rows(db):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    index = next(i for i in AIUsageEvent.__table__.indexes if i.name == "uq_ai_usage_provider_request")
    index.drop(db.get_bind())
    _record(db, 1, "offline-historical-duplicate")
    _record(db, 1, "offline-historical-duplicate")
    assert db.query(AIUsageEvent).count() == 2
    with Operations.context(MigrationContext.configure(db.connection())):
        with pytest.raises(RuntimeError, match="review reconciliation"):
            _migration().upgrade()
    assert db.query(AIUsageEvent).count() == 2


def test_migration_installs_dedup_index_after_clean_preflight(db):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    index = next(i for i in AIUsageEvent.__table__.indexes if i.name == "uq_ai_usage_provider_request")
    index.drop(db.get_bind())
    with Operations.context(MigrationContext.configure(db.connection())):
        _migration().upgrade()
    db.commit()
    _record(db, 1, "offline-post-migration")
    _record(db, 1, "offline-post-migration")
    assert db.query(AIUsageEvent).count() == 1
