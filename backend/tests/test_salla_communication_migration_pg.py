"""0125 on the repository's real 0124 synthetic baseline, not a stub table."""
from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, inspect, text

from backend.tests._salla_comm_pg_harness import admin_url, fresh_database


@pytest.mark.parametrize("bad_url", [
    "postgresql://tester@db.example/postgres",
    "postgresql://tester@127.0.0.1:5432/postgres",
    "postgresql://tester@localhost:5433/postgres",
    "postgresql://tester@127.0.0.1:55491/customer_db",
])
def test_pg_target_guard_rejects_nonisolated_urls(monkeypatch, bad_url):
    monkeypatch.setenv("SALLA_COMM_TEST_PG_URL", bad_url)
    with pytest.raises(RuntimeError, match="isolated_postgres_target_required"):
        admin_url()


def test_ci_port_requires_known_workflow_context(monkeypatch):
    ci_url = "postgresql://tester@127.0.0.1:5433/postgres"
    monkeypatch.setenv("SALLA_COMM_TEST_PG_URL", ci_url)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with pytest.raises(RuntimeError, match="ci_postgres_service_context_required"):
        admin_url()
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("NAHLA_RELIABILITY_REQUIRE_PG", "1")
    monkeypatch.setenv("NAHLA_RELIABILITY_PG_ADMIN_DSN", ci_url)
    assert admin_url().port == 5433
    monkeypatch.setenv("NAHLA_RELIABILITY_PG_ADMIN_DSN", "postgresql://tester@127.0.0.1:5433/other")
    with pytest.raises(RuntimeError, match="ci_postgres_service_context_required"):
        admin_url()


@pytest.mark.skipif(not os.getenv("SALLA_COMM_TEST_PG_URL"), reason="isolated local PG URL required")
def test_migration_0125_on_full_0124_baseline_preserves_synthetic_data():
    from backend.tests import test_shopify_release_gate_pg as h
    from backend.tests import _alembic_topology_probe as topo

    assert h.ADMIN_URL == os.environ["SALLA_COMM_TEST_PG_URL"]
    with fresh_database() as (url, db_name):
        h._build(db_name, "0124")
        target = url.render_as_string(hide_password=False)
        baseline = topo.schema_snapshot(target, exclude_prefixes=())
        sentinels = topo.plant_sentinels(target)
        engine = create_engine(url)
        try:
            with engine.begin() as conn:
                row_id = conn.execute(text(
                    "INSERT INTO webhook_events(provider, event_type, external_event_id, "
                    "store_id, raw_body, status) VALUES "
                    "('salla', 'synthetic.event', 'synthetic-0125-row', "
                    "'synthetic-store', 'synthetic-only', 'processed') RETURNING id"
                )).scalar_one()
                original = conn.execute(text(
                    "SELECT provider, event_type, external_event_id, store_id, raw_body, status "
                    "FROM webhook_events WHERE id=:id"
                ), {"id": row_id}).one()

            upgraded = h._alembic(db_name, "upgrade", "0125")
            assert upgraded.returncode == 0, upgraded.stderr[-2000:]
            assert "0125" in topo.version_rows(target)
            columns = {column["name"] for column in inspect(engine).get_columns("webhook_events")}
            assert {
                "communication_send_state", "communication_provider_message_id",
                "communication_send_error_code", "communication_send_attempted_at",
            } <= columns
            assert inspect(engine).has_table("salla_communication_function_nonces")
            topo.assert_sentinels(target, sentinels, "after 0125")

            downgraded = h._alembic(db_name, "downgrade", "0124")
            assert downgraded.returncode == 0, downgraded.stderr[-2000:]
            assert "0124" in topo.version_rows(target)
            assert "0125" not in topo.version_rows(target)
            assert topo.schema_diff(
                topo.schema_snapshot(target, exclude_prefixes=()), baseline
            ) == []
            with engine.connect() as conn:
                after = conn.execute(text(
                    "SELECT provider, event_type, external_event_id, store_id, raw_body, status "
                    "FROM webhook_events WHERE id=:id"
                ), {"id": row_id}).one()
                assert after == original
            topo.assert_sentinels(target, sentinels, "after 0125 rollback")
        finally:
            engine.dispose()
