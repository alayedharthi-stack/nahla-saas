"""Tests for merchant-plane tenant clone operator."""
from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

_REPO = Path(__file__).resolve().parents[2]
for entry in (str(_REPO), str(_REPO / "backend"), str(_REPO / "database")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from scripts.operators import tenant_merchant_clone as clone_op  # noqa: E402
from scripts.operators.tenant_merchant_clone_contract import (  # noqa: E402
    ALLOWED_TABLE_NAMES,
    APPLY_CONFIRM_ENV,
    APPLY_CONFIRM_TOKEN,
    DENIED_TABLES,
    DRY_RUN_DIGEST_SCHEMA_VERSION,
    PRODUCTION_IDENTITY_CLASS,
    PRODUCTION_SOURCE_CONFIRM_ENV,
    PRODUCTION_SOURCE_CONFIRM_TOKEN,
    TARGET_TEST_SLUG_MARKERS,
)
from scripts.operators.tenant_merchant_clone_scrubber import (  # noqa: E402
    scrub_ai_settings,
    scrub_integration_config,
    scrub_json_value,
    scan_for_unhandled_forbidden_keys,
)

_STAGING_TARGET_URL = (
    "postgresql+psycopg2://operator:password@"
    "postgres-staging.railway.internal:5432/nahla"
)
_STAGING_SOURCE_URL = (
    "postgresql+psycopg2://readonly:password@"
    "postgres-staging.railway.internal:5432/nahla"
)

_BASE_ENV = {
    "RAILWAY_PROJECT_NAME": "desirable-growth",
    "RAILWAY_ENVIRONMENT_NAME": "staging",
    "NAHLA_CLONE_SOURCE_RAILWAY_PROJECT": "desirable-growth",
    "NAHLA_CLONE_SOURCE_RAILWAY_ENVIRONMENT": "staging",
    "NAHLA_CLONE_SOURCE_DATABASE_URL": _STAGING_SOURCE_URL,
    "DATABASE_URL": _STAGING_TARGET_URL,
    "NAHLA_TENANT_MERCHANT_CLONE_ENABLED": "1",
    APPLY_CONFIRM_ENV: APPLY_CONFIRM_TOKEN,
}


def _pg_url() -> str:
    return (os.environ.get("A1_PG_TEST_DATABASE_URL") or "").strip()


def test_contract_allow_deny_disjoint() -> None:
    overlap = ALLOWED_TABLE_NAMES & DENIED_TABLES
    assert not overlap


def test_scrub_ai_settings_forces_test_mode() -> None:
    result = scrub_ai_settings({"store_ai_mode": "on", "ai_test_allowed_numbers": ["966500000000"]})
    assert result["store_ai_mode"] == "test"
    assert result["ai_test_allowed_numbers"] == []


def test_scrub_integration_config_strips_tokens() -> None:
    result = scrub_integration_config({"access_token": "secret", "store_id": "123"})
    assert result["access_token"] == ""
    assert result["store_id"] == "123"


def test_forbidden_json_customer_id_fails_closed() -> None:
    violations = scan_for_unhandled_forbidden_keys({"customer_id": 42})
    assert violations == ["customer_id"]


def test_scrub_json_phone_literal() -> None:
    scrubbed, transforms = scrub_json_value({"contact": "+966501234567"})
    assert any("scrub_phone_literal" in t for t in transforms)


def test_source_equals_target_rejected() -> None:
    request = clone_op.build_request_from_env(
        mode="dry-run",
        source_tenant_id=33,
        target_tenant_id=33,
        clone_id=None,
        dry_run_digest=None,
        manifest_path=None,
        env=_BASE_ENV,
    )
    failure = clone_op.validate_source_target_distinct(request)
    assert failure is not None
    assert failure.stage == "source_equals_target_tenant"


def test_production_source_requires_extra_token() -> None:
    env = dict(_BASE_ENV)
    env["NAHLA_CLONE_SOURCE_RAILWAY_ENVIRONMENT"] = "production"
    failure = clone_op.validate_production_source_gate(env, PRODUCTION_IDENTITY_CLASS)
    assert failure is not None
    env[PRODUCTION_SOURCE_CONFIRM_ENV] = PRODUCTION_SOURCE_CONFIRM_TOKEN
    assert clone_op.validate_production_source_gate(env, PRODUCTION_IDENTITY_CLASS) is None


def test_target_non_staging_host_rejected() -> None:
    env = dict(_BASE_ENV)
    env["DATABASE_URL"] = "postgresql://operator:password@localhost/nahla"
    request = clone_op.build_request_from_env(
        mode="apply",
        source_tenant_id=33,
        target_tenant_id=99033,
        clone_id="c1",
        dry_run_digest="abc",
        manifest_path=None,
        env=env,
    )
    failure = clone_op.validate_target_database_host(env, request.target_database_url)
    assert failure is not None
    assert failure.stage == "target_database_host_not_experimental_staging"


def test_apply_requires_confirmation_token() -> None:
    env = dict(_BASE_ENV)
    del env[APPLY_CONFIRM_ENV]
    failure = clone_op.validate_apply_confirmation(env, mode="apply")
    assert failure is not None


def test_target_test_marker_validation() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE tenants (id INTEGER PRIMARY KEY, name TEXT, domain TEXT)"))
        conn.execute(
            text("INSERT INTO tenants(id,name,domain) VALUES (1,'store','shop.example.com')")
        )
        failure = clone_op.validate_target_test_markers(conn, 1)
    assert failure is not None
    assert failure.stage == "target_tenant_not_test_marked"


def test_target_test_marker_accepts_marker() -> None:
    marker = next(iter(TARGET_TEST_SLUG_MARKERS))
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE tenants (id INTEGER PRIMARY KEY, name TEXT, domain TEXT)"))
        conn.execute(
            text("INSERT INTO tenants(id,name,domain) VALUES (1,:name,'')"),
            {"name": f"merchant{marker}"},
        )
        assert clone_op.validate_target_test_markers(conn, 1) is None


def test_main_dry_run_gate_failure_emits_json(capsys: pytest.CaptureFixture[str]) -> None:
    env = dict(_BASE_ENV)
    env["RAILWAY_ENVIRONMENT_NAME"] = "production"
    with patch.dict(os.environ, env, clear=False):
        rc = clone_op.main(
            ["dry-run", "--source-tenant-id", "33", "--target-tenant-id", "99033"]
        )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["outcome"] == "failed"


@pytest.mark.skipif(not _pg_url(), reason="A1 PostgreSQL integration URL unavailable")
def test_pg_clone_generic_catalog_kb_coupon_without_category_assumptions(
    tmp_path: Path,
) -> None:
    """Prove shoe/perfume/clothing scenarios copy with scrubbed test AI settings."""
    engine = create_engine(_pg_url(), pool_pre_ping=True)
    source_name = f"source-{uuid.uuid4().hex}"
    target_name = f"target-acceptance-test-{uuid.uuid4().hex}"
    unrelated_name = f"unrelated-{uuid.uuid4().hex}"
    source_id = target_id = unrelated_id = None
    try:
        with engine.begin() as conn:
            source_id = conn.execute(
                text("INSERT INTO tenants(name,is_active) VALUES(:n,true) RETURNING id"),
                {"n": source_name},
            ).scalar_one()
            target_id = conn.execute(
                text("INSERT INTO tenants(name,is_active) VALUES(:n,true) RETURNING id"),
                {"n": target_name},
            ).scalar_one()
            unrelated_id = conn.execute(
                text("INSERT INTO tenants(name,is_active) VALUES(:n,true) RETURNING id"),
                {"n": unrelated_name},
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO tenant_settings(tenant_id,ai_settings,store_settings) "
                    "VALUES (:tid, CAST(:ai AS jsonb), CAST('{}' AS jsonb))"
                ),
                {
                    "tid": source_id,
                    "ai": json.dumps(
                        {"store_ai_mode": "on", "ai_test_allowed_numbers": ["966500000000"]}
                    ),
                },
            )
            conn.execute(
                text(
                    "INSERT INTO tenant_settings(tenant_id,ai_settings,store_settings) "
                    "VALUES (:tid, CAST('{}' AS jsonb), CAST('{}' AS jsonb))"
                ),
                {"tid": target_id},
            )
            for title in ("حذاء رياضي أبيض", "عطر ورد 100ml", "قميص قطني أزرق"):
                conn.execute(
                    text(
                        "INSERT INTO products(tenant_id,title,in_stock,has_variants) "
                        "VALUES (:tid,:title,true,false)"
                    ),
                    {"tid": source_id, "title": title},
                )
            conn.execute(
                text(
                    "INSERT INTO merchant_knowledge_sections(tenant_id,kind,title,body,is_active) "
                    "VALUES (:tid,'goal_based_recommendation','goal','generic goal',true)"
                ),
                {"tid": source_id},
            )
            conn.execute(
                text(
                    "INSERT INTO manual_coupons(tenant_id,code,title,is_active,priority) "
                    "VALUES (:tid,'SHOE10','حذاء',true,1)"
                ),
                {"tid": source_id},
            )
            conn.execute(
                text(
                    "INSERT INTO customers(tenant_id,name,phone,normalized_phone) "
                    "VALUES (:tid,'denied','0500000000','+966500000000')"
                ),
                {"tid": source_id},
            )
            conn.execute(
                text("INSERT INTO products(tenant_id,title,in_stock,has_variants) VALUES (:tid,'keep',true,false)"),
                {"tid": unrelated_id},
            )

        env = dict(_BASE_ENV)
        env["NAHLA_CLONE_SOURCE_DATABASE_URL"] = _pg_url()
        env["DATABASE_URL"] = _pg_url()
        env["NAHLA_CLONE_UNRELATED_TENANT_ID"] = str(unrelated_id)
        env[APPLY_CONFIRM_ENV] = APPLY_CONFIRM_TOKEN
        env["NAHLA_TENANT_MERCHANT_CLONE_ENABLED"] = "1"

        dry_request = clone_op.build_request_from_env(
            mode="dry-run",
            source_tenant_id=int(source_id),
            target_tenant_id=int(target_id),
            clone_id=None,
            dry_run_digest=None,
            manifest_path=None,
            env=env,
        )
        plan = clone_op.build_plan(dry_request)
        assert plan["schema_version"] == DRY_RUN_DIGEST_SCHEMA_VERSION
        assert plan["table_counts"]["products"] == 3
        assert plan["table_counts"]["manual_coupons"] == 1
        assert "customers" in plan["denied_domain_source_counts"]
        assert plan["denied_domain_source_counts"]["customers"] >= 1

        manifest_path = tmp_path / "manifest.json"
        apply_request = clone_op.build_request_from_env(
            mode="apply",
            source_tenant_id=int(source_id),
            target_tenant_id=int(target_id),
            clone_id=str(uuid.uuid4()),
            dry_run_digest=plan["dry_run_digest"],
            manifest_path=manifest_path,
            env=env,
        )
        result = clone_op.apply_clone(apply_request)
        assert result["outcome"] == "applied"
        assert manifest_path.is_file()

        with engine.connect() as conn:
            ai_settings = conn.execute(
                text("SELECT ai_settings FROM tenant_settings WHERE tenant_id=:tid"),
                {"tid": target_id},
            ).scalar_one()
            assert ai_settings["store_ai_mode"] == "test"
            assert ai_settings["ai_test_allowed_numbers"] == []
            product_count = conn.execute(
                text("SELECT COUNT(*) FROM products WHERE tenant_id=:tid"),
                {"tid": target_id},
            ).scalar_one()
            assert product_count == 3
            denied_customers = conn.execute(
                text("SELECT COUNT(*) FROM customers WHERE tenant_id=:tid"),
                {"tid": target_id},
            ).scalar_one()
            assert denied_customers == 0
            unrelated_count = conn.execute(
                text("SELECT COUNT(*) FROM products WHERE tenant_id=:tid"),
                {"tid": unrelated_id},
            ).scalar_one()
            assert unrelated_count == 1
            wa_conn = conn.execute(
                text("SELECT COUNT(*) FROM whatsapp_connections WHERE tenant_id=:tid"),
                {"tid": target_id},
            ).scalar_one()
            assert wa_conn == 0

        cleanup_request = clone_op.build_request_from_env(
            mode="cleanup",
            source_tenant_id=int(source_id),
            target_tenant_id=int(target_id),
            clone_id=result["clone_id"],
            dry_run_digest=None,
            manifest_path=manifest_path,
            env={**env, "NAHLA_TENANT_MERCHANT_CLONE_CLEANUP_CONFIRM": "CLEANUP_TENANT_33_MERCHANT_CLONE"},
        )
        cleaned = clone_op.cleanup_clone(cleanup_request)
        assert cleaned["outcome"] == "cleaned"
        with engine.connect() as conn:
            assert (
                conn.execute(
                    text("SELECT COUNT(*) FROM products WHERE tenant_id=:tid"),
                    {"tid": target_id},
                ).scalar_one()
                == 0
            )
    finally:
        for tid in (source_id, target_id, unrelated_id):
            if tid is None:
                continue
            with engine.begin() as conn:
                for table in (
                    "manual_coupons",
                    "merchant_knowledge_sections",
                    "products",
                    "customers",
                    "tenant_settings",
                    "tenants",
                ):
                    conn.execute(text(f"DELETE FROM {table} WHERE tenant_id=:tid"), {"tid": tid})


@pytest.mark.skipif(not _pg_url(), reason="A1 PostgreSQL integration URL unavailable")
def test_pg_schema_drift_rejected() -> None:
    engine = create_engine(_pg_url(), pool_pre_ping=True)
    with engine.connect() as conn:
        failure = clone_op.validate_alembic_heads(conn)
    if failure is None:
        pytest.skip("database at expected heads — drift case not reproducible")
    assert failure.stage == "alembic_heads_mismatch_or_multi_head_drift"
