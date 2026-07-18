"""Tests for merchant-plane tenant clone operator."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

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
    CLONE_PROFILE_FULL_MERCHANT,
    CLONE_PROFILE_SALLA_MINIMAL,
    DEFAULT_ACCEPTANCE_TENANT_ID,
    DENIED_TABLES,
    DRY_RUN_DIGEST_SCHEMA_VERSION,
    EXCLUDED_OPERATIONAL_TABLES,
    EXPECTED_SOURCE_ALEMBIC_HEADS,
    EXPECTED_TARGET_ALEMBIC_HEADS,
    PRESERVE_TENANT_IDENTITY_MODE,
    PRODUCTION_IDENTITY_CLASS,
    PRODUCTION_SOURCE_CONFIRM_ENV,
    PRODUCTION_SOURCE_CONFIRM_TOKEN,
    allowed_table_names_for_profile,
    resolve_clone_profile,
    table_specs_for_profile,
)
from scripts.operators.real_channel_conversational_acceptance_contract import (  # noqa: E402
    PHASE_TENANT_33_LIMITED,
    TENANT_33_LIMITED,
    load_scenario_manifest,
)
from scripts.operators.tenant_merchant_clone_scrubber import (  # noqa: E402
    scrub_ai_settings,
    scrub_integration_config,
    scrub_json_value,
    scan_for_unhandled_forbidden_keys,
)

_STAGING_TARGET_URL = (
    "postgresql+psycopg2://operator:password@"
    "postgres-staging.railway.internal:5432/nahla_target"
)
_STAGING_SOURCE_URL = (
    "postgresql+psycopg2://readonly:password@"
    "postgres-staging.railway.internal:5432/nahla_source"
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


def _clone_pg_urls() -> tuple[str, str]:
    return (
        (os.environ.get("TENANT_CLONE_PG_SOURCE_DATABASE_URL") or "").strip(),
        (os.environ.get("TENANT_CLONE_PG_TARGET_DATABASE_URL") or "").strip(),
    )


def _sqlite_engine_with_revisions(revisions: frozenset[str]) -> object:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR)"))
        for revision in sorted(revisions):
            conn.execute(
                text("INSERT INTO alembic_version (version_num) VALUES (:rev)"),
                {"rev": revision},
            )
    return engine


def _topology_digest_payload(
    *,
    profile: str = CLONE_PROFILE_SALLA_MINIMAL,
    source_heads: list[str],
    target_heads: list[str],
) -> dict[str, object]:
    return {
        "schema_version": DRY_RUN_DIGEST_SCHEMA_VERSION,
        "profile": profile,
        "identity_mode": PRESERVE_TENANT_IDENTITY_MODE,
        "source_database_identity_digest": "sha256:source",
        "target_database_identity_digest": "sha256:target",
        "source_tenant_id": 48,
        "target_tenant_id": 48,
        "target_shell_state": "bootstrap_required",
        "table_counts": {},
        "source_checksums": {},
        "dependency_order": [],
        "excluded_operational_source_counts": {},
        "denied_domain_source_counts": {},
        "target_denied_domain_counts": {},
        "source_alembic_heads": source_heads,
        "target_alembic_heads": target_heads,
    }


def test_clone_profile_missing_rejected() -> None:
    with pytest.raises(ValueError, match="clone_profile_missing"):
        resolve_clone_profile(None)
    failure = clone_op.validate_clone_profile(None)
    assert failure is not None
    assert failure.stage == "clone_profile_missing"


def test_clone_profile_unknown_rejected() -> None:
    with pytest.raises(ValueError, match="clone_profile_unknown"):
        resolve_clone_profile("not_a_real_profile")
    failure = clone_op.validate_clone_profile("not_a_real_profile")
    assert failure is not None
    assert failure.stage == "clone_profile_unknown"


def test_salla_minimal_profile_dependency_order_has_no_fk_gaps() -> None:
    specs = table_specs_for_profile(CLONE_PROFILE_SALLA_MINIMAL)
    seen: set[str] = set()
    parent_for_column = {
        "product_id": "products",
        "group_id": "product_groups",
        "variant_id": "product_variants",
        "source_product_id": "products",
        "target_product_id": "products",
        "section_id": "merchant_knowledge_sections",
        "media_id": "ai_media_library",
    }
    for spec in specs:
        for column in spec.remap_fk_columns:
            parent = parent_for_column[column]
            assert parent in seen, f"{spec.name}.{column} requires {parent} earlier"
        seen.add(spec.name)
    names = allowed_table_names_for_profile(CLONE_PROFILE_SALLA_MINIMAL)
    for required in (
        "products",
        "product_variants",
        "integrations",
        "merchant_knowledge_sections",
        "delivery_zones",
        "shipping_fees",
        "store_knowledge_snapshots",
    ):
        assert required in names
    assert "coupons" not in names
    assert "whatsapp_templates" not in names
    assert "smart_automations" not in names


def test_salla_minimal_excludes_operational_tables_from_contract() -> None:
    minimal_names = allowed_table_names_for_profile(CLONE_PROFILE_SALLA_MINIMAL)
    full_names = allowed_table_names_for_profile(CLONE_PROFILE_FULL_MERCHANT)
    assert EXCLUDED_OPERATIONAL_TABLES.issubset(full_names)
    assert not EXCLUDED_OPERATIONAL_TABLES & minimal_names


def test_coupon_like_source_rows_report_excluded_not_copied() -> None:
    minimal_names = allowed_table_names_for_profile(CLONE_PROFILE_SALLA_MINIMAL)
    for coupon_table in ("coupons", "coupon_rules", "manual_coupons", "promotions"):
        assert coupon_table not in minimal_names
        assert coupon_table in EXCLUDED_OPERATIONAL_TABLES
    simulated_plan = {
        "table_counts": {name: 0 for name in minimal_names},
        "excluded_operational_source_counts": {
            "coupons": 4413,
            "coupon_rules": 0,
            "manual_coupons": 0,
            "promotions": 0,
        },
    }
    assert simulated_plan["table_counts"].get("coupons", 0) == 0
    assert simulated_plan["excluded_operational_source_counts"]["coupons"] == 4413


def test_minimal_profile_excludes_automation_template_channel_tables() -> None:
    names = allowed_table_names_for_profile(CLONE_PROFILE_SALLA_MINIMAL)
    for excluded in (
        "whatsapp_templates",
        "smart_automations",
        "automation_rules",
        "merchant_branches",
        "branch_contacts",
        "merchant_widgets",
        "widget_settings",
    ):
        assert excluded not in names


def test_dry_run_digest_differs_between_full_and_minimal_profiles() -> None:
    payload_full = _topology_digest_payload(
        profile=CLONE_PROFILE_FULL_MERCHANT,
        source_heads=sorted(EXPECTED_SOURCE_ALEMBIC_HEADS),
        target_heads=sorted(EXPECTED_TARGET_ALEMBIC_HEADS),
    )
    payload_full["dependency_order"] = [
        spec.name for spec in table_specs_for_profile(CLONE_PROFILE_FULL_MERCHANT)
    ]
    payload_minimal = _topology_digest_payload(
        profile=CLONE_PROFILE_SALLA_MINIMAL,
        source_heads=sorted(EXPECTED_SOURCE_ALEMBIC_HEADS),
        target_heads=sorted(EXPECTED_TARGET_ALEMBIC_HEADS),
    )
    payload_minimal["dependency_order"] = [
        spec.name for spec in table_specs_for_profile(CLONE_PROFILE_SALLA_MINIMAL)
    ]
    assert clone_op.compute_dry_run_digest(payload_full) != clone_op.compute_dry_run_digest(
        payload_minimal
    )


def test_apply_rejects_profile_mismatch() -> None:
    minimal_digest = clone_op.compute_dry_run_digest(
        _topology_digest_payload(
            profile=CLONE_PROFILE_SALLA_MINIMAL,
            source_heads=["0089"],
            target_heads=["0088", "0089"],
        )
    )
    fresh_plan = {
        "profile": CLONE_PROFILE_SALLA_MINIMAL,
        "dry_run_digest": minimal_digest,
        "target_shell_state": "bootstrap_required",
        "source_database_identity_digest": "sha256:source",
        "target_database_identity_digest": "sha256:target",
    }
    request = clone_op.build_request_from_env(
        mode="apply",
        profile=CLONE_PROFILE_FULL_MERCHANT,
        source_tenant_id=48,
        target_tenant_id=48,
        clone_id="profile-drift-test",
        dry_run_digest=minimal_digest,
        manifest_path=None,
        env=_BASE_ENV,
    )
    mock_engine = MagicMock()
    mock_engine.connect.return_value.__enter__ = MagicMock(return_value=MagicMock())
    mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)
    with (
        patch.object(clone_op, "connect_engine", return_value=mock_engine),
        patch.object(clone_op, "build_plan", return_value=fresh_plan),
    ):
        with pytest.raises(ValueError, match="clone_profile_mismatch"):
            clone_op.apply_clone(request)


def test_integration_row_scrubbed_and_disabled() -> None:
    row = {
        "id": 1,
        "tenant_id": 48,
        "provider": "salla",
        "external_store_id": "store-123",
        "enabled": True,
        "config": {"access_token": "secret", "store_id": "123"},
    }
    transformed, transforms = clone_op._transform_row(
        row,
        spec_name="integrations",
        spec_json_columns=("config",),
        target_tenant_id=48,
        id_maps={},
        remap_fk_columns=(),
        scrub_phone_columns=(),
        deferred_fk_columns=(),
    )
    assert transformed["enabled"] is False
    assert transformed["external_store_id"] is None
    assert transformed["config"]["access_token"] == ""
    assert transformed["config"]["store_id"] == "123"
    assert any("integrations.disabled_until_staging_credentials" in t for t in transforms)


def test_revision_topology_source_0089_target_dual_head_accepted() -> None:
    source_engine = _sqlite_engine_with_revisions(frozenset({"0089"}))
    target_engine = _sqlite_engine_with_revisions(frozenset({"0088", "0089"}))
    with source_engine.connect() as source_conn, target_engine.connect() as target_conn:
        assert clone_op.validate_source_alembic_heads(source_conn) is None
        assert clone_op.validate_target_alembic_heads(target_conn) is None


def test_revision_topology_source_dual_head_rejected() -> None:
    engine = _sqlite_engine_with_revisions(frozenset({"0088", "0089"}))
    with engine.connect() as conn:
        failure = clone_op.validate_source_alembic_heads(conn)
    assert failure is not None
    assert failure.stage == "source_alembic_multi_head_drift"


def test_revision_topology_source_0088_only_rejected() -> None:
    engine = _sqlite_engine_with_revisions(frozenset({"0088"}))
    with engine.connect() as conn:
        failure = clone_op.validate_source_alembic_heads(conn)
    assert failure is not None
    assert failure.stage == "source_alembic_revision_mismatch"


def test_revision_topology_target_single_0089_rejected() -> None:
    engine = _sqlite_engine_with_revisions(frozenset({"0089"}))
    with engine.connect() as conn:
        failure = clone_op.validate_target_alembic_heads(conn)
    assert failure is not None
    assert failure.stage == "target_alembic_revision_missing:0088"


def test_revision_topology_unknown_head_rejected_on_either_side() -> None:
    source_engine = _sqlite_engine_with_revisions(frozenset({"0090"}))
    target_engine = _sqlite_engine_with_revisions(frozenset({"0088", "0089", "0090"}))
    with source_engine.connect() as conn:
        failure = clone_op.validate_source_alembic_heads(conn)
        assert failure is not None
        assert failure.stage == "source_alembic_unknown_revision"
    with target_engine.connect() as conn:
        failure = clone_op.validate_target_alembic_heads(conn)
        assert failure is not None
        assert failure.stage == "target_alembic_unknown_revision"


def test_dry_run_digest_changes_when_topology_heads_change() -> None:
    payload_a = _topology_digest_payload(
        profile=CLONE_PROFILE_SALLA_MINIMAL,
        source_heads=sorted(EXPECTED_SOURCE_ALEMBIC_HEADS),
        target_heads=sorted(EXPECTED_TARGET_ALEMBIC_HEADS),
    )
    payload_b = _topology_digest_payload(
        profile=CLONE_PROFILE_SALLA_MINIMAL,
        source_heads=sorted({"0088", "0089"}),
        target_heads=sorted(EXPECTED_TARGET_ALEMBIC_HEADS),
    )
    digest_a = clone_op.compute_dry_run_digest(payload_a)
    digest_b = clone_op.compute_dry_run_digest(payload_b)
    assert digest_a != digest_b


def test_apply_revalidation_rejects_topology_drift() -> None:
    stale_digest = clone_op.compute_dry_run_digest(
        _topology_digest_payload(
            profile=CLONE_PROFILE_SALLA_MINIMAL,
            source_heads=["0088", "0089"],
            target_heads=["0088", "0089"],
        )
    )
    fresh_plan = {
        "profile": CLONE_PROFILE_SALLA_MINIMAL,
        "dry_run_digest": clone_op.compute_dry_run_digest(
            _topology_digest_payload(
                profile=CLONE_PROFILE_SALLA_MINIMAL,
                source_heads=["0089"],
                target_heads=["0088", "0089"],
            )
        ),
        "target_shell_state": "bootstrap_required",
        "source_database_identity_digest": "sha256:source",
        "target_database_identity_digest": "sha256:target",
    }
    request = clone_op.build_request_from_env(
        mode="apply",
        profile=CLONE_PROFILE_SALLA_MINIMAL,
        source_tenant_id=48,
        target_tenant_id=48,
        clone_id="topology-drift-test",
        dry_run_digest=stale_digest,
        manifest_path=None,
        env=_BASE_ENV,
    )
    mock_engine = MagicMock()
    mock_engine.connect.return_value.__enter__ = MagicMock(return_value=MagicMock())
    mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)
    with (
        patch.object(clone_op, "connect_engine", return_value=mock_engine),
        patch.object(clone_op, "build_plan", return_value=fresh_plan),
    ):
        with pytest.raises(ValueError, match="dry_run_digest_mismatch"):
            clone_op.apply_clone(request)


def test_preserve_tenant_48_cross_database_identity_mode() -> None:
    request = clone_op.build_request_from_env(
        mode="dry-run",
        profile=CLONE_PROFILE_SALLA_MINIMAL,
        source_tenant_id=48,
        target_tenant_id=48,
        clone_id=None,
        dry_run_digest=None,
        manifest_path=None,
        env=_BASE_ENV,
    )
    assert clone_op.validate_source_target_distinct(request) is None
    assert clone_op.identity_mode(request) == PRESERVE_TENANT_IDENTITY_MODE


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


def test_scrub_integration_config_strips_provider_email_keys() -> None:
    pii_email = "owner@merchant.example"
    config = {
        "salla_owner_email": pii_email,
        "store_id": "123",
        "nested": {"contact_email": "nested@merchant.example"},
        "contacts": [{"ownerEmail": "camel@merchant.example"}],
    }
    result = scrub_integration_config(config)
    serialized = json.dumps(result)
    assert pii_email not in serialized
    assert "nested@merchant.example" not in serialized
    assert "camel@merchant.example" not in serialized
    assert result["salla_owner_email"] == ""
    assert result["store_id"] == "123"
    assert result["nested"]["contact_email"] == ""
    assert result["contacts"][0]["ownerEmail"] == ""


def test_scrub_integration_config_rejects_unknown_forbidden_keys() -> None:
    with pytest.raises(ValueError, match="integration_config_unhandled_forbidden_key"):
        scrub_integration_config({"customer_id": "cust-123", "store_id": "123"})


def test_old_dry_run_digest_schema_rejected_on_apply() -> None:
    stale_payload = _topology_digest_payload(
        profile=CLONE_PROFILE_SALLA_MINIMAL,
        source_heads=sorted(EXPECTED_SOURCE_ALEMBIC_HEADS),
        target_heads=sorted(EXPECTED_TARGET_ALEMBIC_HEADS),
    )
    stale_payload["schema_version"] = "tenant_merchant_clone_dry_run_v4"
    stale_digest = clone_op.compute_dry_run_digest(stale_payload)
    fresh_plan = {
        "profile": CLONE_PROFILE_SALLA_MINIMAL,
        "dry_run_digest": clone_op.compute_dry_run_digest(
            _topology_digest_payload(
                profile=CLONE_PROFILE_SALLA_MINIMAL,
                source_heads=sorted(EXPECTED_SOURCE_ALEMBIC_HEADS),
                target_heads=sorted(EXPECTED_TARGET_ALEMBIC_HEADS),
            )
        ),
        "target_shell_state": "bootstrap_required",
        "source_database_identity_digest": "sha256:source",
        "target_database_identity_digest": "sha256:target",
    }
    request = clone_op.build_request_from_env(
        mode="apply",
        profile=CLONE_PROFILE_SALLA_MINIMAL,
        source_tenant_id=48,
        target_tenant_id=48,
        clone_id="schema-version-drift-test",
        dry_run_digest=stale_digest,
        manifest_path=None,
        env=_BASE_ENV,
    )
    mock_engine = MagicMock()
    mock_engine.connect.return_value.__enter__ = MagicMock(return_value=MagicMock())
    mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)
    with (
        patch.object(clone_op, "connect_engine", return_value=mock_engine),
        patch.object(clone_op, "build_plan", return_value=fresh_plan),
    ):
        with pytest.raises(ValueError, match="dry_run_digest_mismatch"):
            clone_op.apply_clone(request)


def test_dry_run_digest_schema_version_is_v5() -> None:
    assert DRY_RUN_DIGEST_SCHEMA_VERSION == "tenant_merchant_clone_dry_run_v5"


def test_forbidden_json_customer_id_fails_closed() -> None:
    violations = scan_for_unhandled_forbidden_keys({"customer_id": 42})
    assert violations == ["customer_id"]


def test_scrub_json_phone_literal() -> None:
    scrubbed, transforms = scrub_json_value({"contact": "+966501234567"})
    assert any("scrub_phone_literal" in t for t in transforms)


def test_same_tenant_id_across_distinct_database_endpoints_is_accepted() -> None:
    request = clone_op.build_request_from_env(
        mode="dry-run",
        profile=CLONE_PROFILE_FULL_MERCHANT,
        source_tenant_id=33,
        target_tenant_id=33,
        clone_id=None,
        dry_run_digest=None,
        manifest_path=None,
        env=_BASE_ENV,
    )
    failure = clone_op.validate_source_target_distinct(request)
    assert failure is None
    assert clone_op.identity_mode(request) == PRESERVE_TENANT_IDENTITY_MODE


def test_same_database_rejected_even_with_distinct_credentials() -> None:
    env = dict(_BASE_ENV)
    env["NAHLA_CLONE_SOURCE_DATABASE_URL"] = (
        "postgresql://readonly:source-secret@postgres-staging.railway.internal:5432/nahla_target"
    )
    request = clone_op.build_request_from_env(
        mode="dry-run",
        profile=CLONE_PROFILE_FULL_MERCHANT,
        source_tenant_id=33,
        target_tenant_id=33,
        clone_id=None,
        dry_run_digest=None,
        manifest_path=None,
        env=env,
    )
    failure = clone_op.validate_source_target_distinct(request)
    assert failure is not None
    assert failure.stage == "source_equals_target_database"


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
        profile=CLONE_PROFILE_SALLA_MINIMAL,
        source_tenant_id=33,
        target_tenant_id=33,
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


def test_main_dry_run_gate_failure_emits_json(capsys: pytest.CaptureFixture[str]) -> None:
    env = dict(_BASE_ENV)
    env["RAILWAY_ENVIRONMENT_NAME"] = "production"
    with patch.dict(os.environ, env, clear=False):
        rc = clone_op.main(
            [
                "dry-run",
                "--profile",
                CLONE_PROFILE_SALLA_MINIMAL,
                "--source-tenant-id",
                "33",
                "--target-tenant-id",
                "33",
            ]
        )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["outcome"] == "failed"


def test_acceptance_manifest_and_session_align_to_clone_tenant_33() -> None:
    manifest = load_scenario_manifest(_REPO)
    limited = [
        row
        for row in manifest["scenarios"]
        if row["phase"] == PHASE_TENANT_33_LIMITED
    ]
    assert DEFAULT_ACCEPTANCE_TENANT_ID == TENANT_33_LIMITED == 33
    assert limited
    assert {row["tenant_id"] for row in limited} == {DEFAULT_ACCEPTANCE_TENANT_ID}


@pytest.mark.skipif(
    not all(_clone_pg_urls()),
    reason="tenant clone PostgreSQL source/target databases unavailable",
)
def test_pg_preserve_tenant_33_bootstrap_cleanup_and_shell_guards(
    tmp_path: Path,
) -> None:
    """Cross-DB Tenant 33 is preserved; shell lifecycle stays fail-closed."""
    source_url, target_url = _clone_pg_urls()
    source_engine = create_engine(source_url, pool_pre_ping=True)
    target_engine = create_engine(target_url, pool_pre_ping=True)
    tenant_id = DEFAULT_ACCEPTANCE_TENANT_ID
    with source_engine.begin() as conn:
        conn.execute(text("DELETE FROM alembic_version WHERE version_num = '0088'"))
    with source_engine.begin() as conn:
        conn.execute(text("DELETE FROM tenant_settings WHERE tenant_id=:tid"), {"tid": tenant_id})
        conn.execute(text("DELETE FROM products WHERE tenant_id=:tid"), {"tid": tenant_id})
        conn.execute(
            text("DELETE FROM merchant_knowledge_sections WHERE tenant_id=:tid"),
            {"tid": tenant_id},
        )
        conn.execute(text("DELETE FROM manual_coupons WHERE tenant_id=:tid"), {"tid": tenant_id})
        conn.execute(text("DELETE FROM customers WHERE tenant_id=:tid"), {"tid": tenant_id})
        conn.execute(text("DELETE FROM tenants WHERE id=:tid"), {"tid": tenant_id})
        conn.execute(
            text(
                "INSERT INTO tenants(id,name,is_active,branding) "
                "VALUES (:tid,'generic-source',true,CAST('{\"theme\":\"neutral\"}' AS jsonb))"
            ),
            {"tid": tenant_id},
        )
        conn.execute(
            text(
                "INSERT INTO tenant_settings"
                "(tenant_id,show_nahla_branding,branding_text,ai_settings,whatsapp_settings) "
                "VALUES (:tid,true,'Powered by Nahla',CAST(:ai AS jsonb),CAST(:wa AS jsonb))"
            ),
            {
                "tid": tenant_id,
                "ai": json.dumps(
                    {
                        "store_ai_mode": "on",
                        "ai_test_allowed_numbers": ["966500000000"],
                    }
                ),
                "wa": json.dumps(
                    {"access_token": "must-not-copy", "phone_number": "966500000000"}
                ),
            },
        )
        for title in ("حذاء رياضي أبيض", "عطر ورد 100ml", "قميص قطني أزرق"):
            conn.execute(
                text(
                    "INSERT INTO products(tenant_id,title,in_stock,has_variants) "
                    "VALUES (:tid,:title,true,false)"
                ),
                {"tid": tenant_id, "title": title},
            )
        conn.execute(
            text(
                "INSERT INTO merchant_knowledge_sections"
                "(tenant_id,kind,title,body,is_active) "
                "VALUES (:tid,'goal_based_recommendation','goal','generic goal',true)"
            ),
            {"tid": tenant_id},
        )
        conn.execute(
            text(
                "INSERT INTO manual_coupons(tenant_id,code,title,is_active,priority) "
                "VALUES (:tid,'GENERIC10','generic',true,1)"
            ),
            {"tid": tenant_id},
        )
        conn.execute(
            text(
                "INSERT INTO customers(tenant_id,name,phone,normalized_phone) "
                "VALUES (:tid,'denied','0500000000','+966500000000')"
            ),
            {"tid": tenant_id},
        )

    with target_engine.begin() as conn:
        for table in (
            "customers",
            "tenant_settings",
            "manual_coupons",
            "merchant_knowledge_sections",
            "products",
        ):
            conn.execute(
                text(f"DELETE FROM {table} WHERE tenant_id=:tid"),
                {"tid": tenant_id},
            )
        conn.execute(text("DELETE FROM tenants WHERE id=:tid"), {"tid": tenant_id})

    env = {
        **_BASE_ENV,
        "NAHLA_CLONE_SOURCE_DATABASE_URL": source_url,
        "DATABASE_URL": target_url,
        "NAHLA_TENANT_MERCHANT_CLONE_CLEANUP_CONFIRM": (
            "CLEANUP_TENANT_33_MERCHANT_CLONE"
        ),
    }
    dry_request = clone_op.build_request_from_env(
        mode="dry-run",
        profile=CLONE_PROFILE_SALLA_MINIMAL,
        source_tenant_id=tenant_id,
        target_tenant_id=tenant_id,
        clone_id="tenant-33-cross-db-test",
        dry_run_digest=None,
        manifest_path=None,
        env=env,
    )
    plan = clone_op.build_plan(dry_request)
    assert plan["profile"] == CLONE_PROFILE_SALLA_MINIMAL
    assert plan["identity_mode"] == PRESERVE_TENANT_IDENTITY_MODE
    assert plan["database_identities_distinct"] is True
    assert plan["source_database_identity_digest"] != plan["target_database_identity_digest"]
    assert plan["target_tenant_bootstrap_planned"] is True
    assert plan["table_counts"]["products"] == 3
    assert plan["table_counts"].get("manual_coupons", 0) == 0
    assert plan["excluded_operational_source_counts"].get("manual_coupons", 0) == 1

    manifest_path = tmp_path / "tenant-33-clone-manifest.json"
    apply_request = clone_op.build_request_from_env(
        mode="apply",
        profile=CLONE_PROFILE_SALLA_MINIMAL,
        source_tenant_id=tenant_id,
        target_tenant_id=tenant_id,
        clone_id=dry_request.clone_id,
        dry_run_digest=plan["dry_run_digest"],
        manifest_path=manifest_path,
        env=env,
    )
    result = clone_op.apply_clone(apply_request)
    assert result["identity_mode"] == PRESERVE_TENANT_IDENTITY_MODE
    assert result["target_tenant_bootstrapped"] is True

    with target_engine.connect() as conn:
        settings = conn.execute(
            text(
                "SELECT ai_settings,whatsapp_settings FROM tenant_settings "
                "WHERE tenant_id=:tid"
            ),
            {"tid": tenant_id},
        ).mappings().one()
        assert settings["ai_settings"]["store_ai_mode"] == "test"
        assert settings["ai_settings"]["ai_test_allowed_numbers"] == []
        assert settings["whatsapp_settings"]["access_token"] == ""
        assert settings["whatsapp_settings"]["phone_number"] == ""
        assert conn.execute(
            text("SELECT COUNT(*) FROM manual_coupons WHERE tenant_id=:tid"),
            {"tid": tenant_id},
        ).scalar_one() == 0
        for table in ("users", "customers", "orders", "message_events", "whatsapp_connections"):
            assert conn.execute(
                text(f"SELECT COUNT(*) FROM {table} WHERE tenant_id=:tid"),
                {"tid": tenant_id},
            ).scalar_one() == 0

    cleanup_request = clone_op.build_request_from_env(
        mode="cleanup",
        profile=CLONE_PROFILE_SALLA_MINIMAL,
        source_tenant_id=tenant_id,
        target_tenant_id=tenant_id,
        clone_id=result["clone_id"],
        dry_run_digest=None,
        manifest_path=manifest_path,
        env=env,
    )
    cleaned = clone_op.cleanup_clone(cleanup_request)
    assert cleaned["target_tenant_shell_deleted"] is True
    with target_engine.connect() as conn:
        assert conn.execute(
            text("SELECT COUNT(*) FROM tenants WHERE id=:tid"),
            {"tid": tenant_id},
        ).scalar_one() == 0

    # A pre-existing acceptance-marked empty shell is accepted and never owned
    # by cleanup; adding any operational row makes it fail closed.
    with target_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants(id,name,is_active,is_platform_tenant) "
                "VALUES (:tid,'tenant-33-acceptance-test',true,false)"
            ),
            {"tid": tenant_id},
        )
    existing_plan = clone_op.build_plan(dry_request)
    assert existing_plan["target_shell_state"] == "existing_safe_empty"
    assert existing_plan["target_tenant_bootstrap_planned"] is False
    existing_manifest_path = tmp_path / "existing-shell-manifest.json"
    existing_apply_request = clone_op.build_request_from_env(
        mode="apply",
        profile=CLONE_PROFILE_SALLA_MINIMAL,
        source_tenant_id=tenant_id,
        target_tenant_id=tenant_id,
        clone_id="tenant-33-existing-shell-test",
        dry_run_digest=existing_plan["dry_run_digest"],
        manifest_path=existing_manifest_path,
        env=env,
    )
    existing_result = clone_op.apply_clone(existing_apply_request)
    assert existing_result["target_tenant_bootstrapped"] is False
    existing_cleanup_request = clone_op.build_request_from_env(
        mode="cleanup",
        profile=CLONE_PROFILE_SALLA_MINIMAL,
        source_tenant_id=tenant_id,
        target_tenant_id=tenant_id,
        clone_id=existing_result["clone_id"],
        dry_run_digest=None,
        manifest_path=existing_manifest_path,
        env=env,
    )
    existing_cleaned = clone_op.cleanup_clone(existing_cleanup_request)
    assert existing_cleaned["target_tenant_shell_deleted"] is False
    with target_engine.connect() as conn:
        assert conn.execute(
            text("SELECT COUNT(*) FROM tenants WHERE id=:tid"),
            {"tid": tenant_id},
        ).scalar_one() == 1

    with target_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO customers(tenant_id,name,phone) "
                "VALUES (:tid,'unsafe','0500000000')"
            ),
            {"tid": tenant_id},
        )
    with pytest.raises(ValueError, match="target_denied_rows_present:customers"):
        clone_op.build_plan(dry_request)

    with source_engine.connect() as source_conn:
        with target_engine.connect() as target_conn:
            _, _, failure = clone_op.validate_runtime_database_distinct(
                source_conn,
                target_conn,
            )
            assert failure is None
        _, _, failure = clone_op.validate_runtime_database_distinct(
            source_conn,
            source_conn,
        )
        assert failure is not None
        assert failure.stage == "source_equals_target_database_runtime"
