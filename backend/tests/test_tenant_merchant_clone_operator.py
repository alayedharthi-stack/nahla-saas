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
    PROVIDER_OWNERSHIP_KEYS,
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
    scrub_row_json_columns,
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
    table_counts: dict[str, int] | None = None,
    source_checksums: dict[str, str] | None = None,
    dependency_order: list[str] | None = None,
    target_denied_domain_counts: dict[str, int] | None = None,
) -> dict[str, object]:
    return clone_op.build_dry_run_digest_binding_payload(
        profile=profile,
        identity_mode_value=PRESERVE_TENANT_IDENTITY_MODE,
        source_database_identity_digest="sha256:source",
        target_database_identity_digest="sha256:target",
        source_tenant_id=48,
        target_tenant_id=48,
        target_shell_state="bootstrap_required",
        table_counts=table_counts or {},
        source_checksums=source_checksums or {},
        dependency_order=dependency_order or [],
        target_denied_domain_counts=target_denied_domain_counts or {},
        source_alembic_heads=source_heads,
        target_alembic_heads=target_heads,
    )


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
        "tenant_settings",
    ):
        assert required in names
    assert "store_knowledge_snapshots" not in names
    assert "coupons" not in names
    assert "whatsapp_templates" not in names
    assert "smart_automations" not in names


def test_full_merchant_profile_includes_store_knowledge_snapshots() -> None:
    full_names = allowed_table_names_for_profile(CLONE_PROFILE_FULL_MERCHANT)
    minimal_names = allowed_table_names_for_profile(CLONE_PROFILE_SALLA_MINIMAL)
    assert "store_knowledge_snapshots" in full_names
    assert "store_knowledge_snapshots" not in minimal_names
    full_spec = next(
        spec
        for spec in table_specs_for_profile(CLONE_PROFILE_FULL_MERCHANT)
        if spec.name == "store_knowledge_snapshots"
    )
    assert full_spec.upsert_on_tenant is True
    assert "store_profile" in full_spec.json_columns
    assert "coupon_summary" in full_spec.json_columns


def test_minimal_profile_excludes_derived_store_knowledge_snapshots() -> None:
    minimal_names = allowed_table_names_for_profile(CLONE_PROFILE_SALLA_MINIMAL)
    assert "store_knowledge_snapshots" not in minimal_names
    assert "store_knowledge_snapshots" in EXCLUDED_OPERATIONAL_TABLES
    simulated_plan = {
        "table_counts": {name: 0 for name in minimal_names},
        "excluded_operational_source_counts": {
            "store_knowledge_snapshots": 1,
            "coupons": 4413,
        },
    }
    assert simulated_plan["table_counts"].get("store_knowledge_snapshots", 0) == 0
    assert simulated_plan["excluded_operational_source_counts"]["store_knowledge_snapshots"] == 1


def test_minimal_profile_excludes_snapshot_pii_and_mixed_domain_cache() -> None:
    """Derived snapshot cache (PII + coupon/customer/order summaries) stays out of minimal scope."""
    minimal_names = allowed_table_names_for_profile(CLONE_PROFILE_SALLA_MINIMAL)
    source_snapshot_row = {
        "tenant_id": 48,
        "store_profile": {
            "contact_email": "owner@merchant.example",
            "contact_phone": "+966501234567",
        },
        "coupon_summary": {"active_coupons": 12},
        "catalog_summary": {"product_count": 99},
    }
    simulated_plan = {
        "profile": CLONE_PROFILE_SALLA_MINIMAL,
        "table_counts": {name: 0 for name in minimal_names},
        "excluded_operational_source_counts": {
            "store_knowledge_snapshots": 1,
        },
    }
    assert "store_knowledge_snapshots" not in simulated_plan["table_counts"]
    assert simulated_plan["excluded_operational_source_counts"]["store_knowledge_snapshots"] == 1
    assert source_snapshot_row["store_profile"]["contact_email"]
    assert source_snapshot_row["coupon_summary"]["active_coupons"] > 0
    for denied_table in ("customers", "orders", "coupons"):
        assert denied_table not in minimal_names
        assert denied_table in DENIED_TABLES or denied_table in EXCLUDED_OPERATIONAL_TABLES


def test_minimal_profile_retains_generic_catalog_settings_and_integrations() -> None:
    minimal_names = allowed_table_names_for_profile(CLONE_PROFILE_SALLA_MINIMAL)
    generic_product_title = "حذاء رياضي أبيض"
    simulated_plan = {
        "table_counts": {
            "products": 1,
            "tenant_settings": 1,
            "integrations": 1,
            "merchant_knowledge_sections": 2,
            "delivery_zones": 1,
            "shipping_fees": 1,
        },
        "source_product_titles": [generic_product_title],
    }
    for table in (
        "products",
        "tenant_settings",
        "integrations",
        "merchant_knowledge_sections",
        "delivery_zones",
        "shipping_fees",
    ):
        assert table in minimal_names
        assert simulated_plan["table_counts"][table] > 0
    assert generic_product_title in simulated_plan["source_product_titles"]


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
    assert transformed["config"]["store_id"] == ""
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
    assert result["store_ai_enabled"] is True


def _tenant_settings_json_columns() -> tuple[str, ...]:
    spec = next(
        spec
        for spec in table_specs_for_profile(CLONE_PROFILE_SALLA_MINIMAL)
        if spec.name == "tenant_settings"
    )
    return spec.json_columns


def test_tenant_settings_null_ai_settings_materializes_safe_test_posture() -> None:
    row = {"tenant_id": 48, "ai_settings": None}
    transformed, transforms = scrub_row_json_columns(
        row,
        _tenant_settings_json_columns(),
        table="tenant_settings",
    )
    assert transformed["ai_settings"]["store_ai_mode"] == "test"
    assert transformed["ai_settings"]["ai_test_allowed_numbers"] == []
    assert transformed["ai_settings"]["store_ai_enabled"] is True
    assert any("tenant_settings.ai_settings_safe_test_mode" in t for t in transforms)


def test_tenant_settings_missing_ai_settings_materializes_safe_test_posture() -> None:
    row = {"tenant_id": 48}
    transformed, transforms = scrub_row_json_columns(
        row,
        _tenant_settings_json_columns(),
        table="tenant_settings",
    )
    assert transformed["ai_settings"]["store_ai_mode"] == "test"
    assert transformed["ai_settings"]["ai_test_allowed_numbers"] == []
    assert transformed["ai_settings"]["store_ai_enabled"] is True
    assert any("tenant_settings.ai_settings_safe_test_mode" in t for t in transforms)


def test_tenant_settings_null_whatsapp_settings_materializes_empty_object() -> None:
    row = {"tenant_id": 48, "whatsapp_settings": None}
    transformed, transforms = scrub_row_json_columns(
        row,
        _tenant_settings_json_columns(),
        table="tenant_settings",
    )
    assert transformed["whatsapp_settings"] == {}
    assert any("tenant_settings.whatsapp_settings_stripped" in t for t in transforms)


def test_tenant_settings_missing_whatsapp_settings_materializes_empty_object() -> None:
    row = {"tenant_id": 48}
    transformed, transforms = scrub_row_json_columns(
        row,
        _tenant_settings_json_columns(),
        table="tenant_settings",
    )
    assert transformed["whatsapp_settings"] == {}
    assert any("tenant_settings.whatsapp_settings_stripped" in t for t in transforms)


def test_tenant_settings_non_null_ai_forced_test_mode_and_empty_allowlist() -> None:
    row = {
        "tenant_id": 48,
        "ai_settings": {
            "store_ai_mode": "on",
            "ai_test_allowed_numbers": ["966500000000", "966511111111"],
            "store_ai_enabled": False,
            "persona_tone": "friendly",
        },
    }
    transformed, transforms = scrub_row_json_columns(
        row,
        _tenant_settings_json_columns(),
        table="tenant_settings",
    )
    assert transformed["ai_settings"]["store_ai_mode"] == "test"
    assert transformed["ai_settings"]["ai_test_allowed_numbers"] == []
    assert transformed["ai_settings"]["store_ai_enabled"] is True
    assert transformed["ai_settings"]["persona_tone"] == "friendly"
    assert any("tenant_settings.ai_settings_safe_test_mode" in t for t in transforms)


def test_tenant_settings_non_null_whatsapp_strips_credentials_retains_safe_metadata() -> None:
    row = {
        "tenant_id": 48,
        "whatsapp_settings": {
            "access_token": "secret-token",
            "verify_token": "verify-me",
            "phone_number": "966500000000",
            "owner_whatsapp_number": "966511111111",
            "business_name": "متجر تجريبي عام",
            "catalog_enabled": True,
        },
    }
    transformed, transforms = scrub_row_json_columns(
        row,
        _tenant_settings_json_columns(),
        table="tenant_settings",
    )
    wa = transformed["whatsapp_settings"]
    assert wa["access_token"] == ""
    assert wa["verify_token"] == ""
    assert wa["phone_number"] == ""
    assert wa["owner_whatsapp_number"] == ""
    assert wa["business_name"] == "متجر تجريبي عام"
    assert wa["catalog_enabled"] is True
    assert scan_for_unhandled_forbidden_keys(wa) == []
    assert any("tenant_settings.whatsapp_settings_stripped" in t for t in transforms)


def test_tenant_settings_whatsapp_unknown_forbidden_key_rejected() -> None:
    row = {
        "tenant_id": 48,
        "whatsapp_settings": {"customer_id": "cust-123", "business_name": "متجر تجريبي عام"},
    }
    with pytest.raises(ValueError, match="whatsapp_settings_unhandled_forbidden_key"):
        scrub_row_json_columns(
            row,
            _tenant_settings_json_columns(),
            table="tenant_settings",
        )


def test_tenant_settings_transform_generic_merchant_fixture() -> None:
    tenant_settings_spec = next(
        spec
        for spec in table_specs_for_profile(CLONE_PROFILE_SALLA_MINIMAL)
        if spec.name == "tenant_settings"
    )
    row = {
        "tenant_id": 48,
        "show_nahla_branding": True,
        "branding_text": "Powered by Nahla",
        "ai_settings": None,
        "whatsapp_settings": None,
        "store_settings": {"currency": "SAR", "catalog_title": "حذاء رياضي أبيض"},
    }
    transformed, transforms = clone_op._transform_row(
        row,
        spec_name=tenant_settings_spec.name,
        spec_json_columns=tenant_settings_spec.json_columns,
        target_tenant_id=48,
        id_maps={},
        remap_fk_columns=tenant_settings_spec.remap_fk_columns,
        scrub_phone_columns=tenant_settings_spec.scrub_phone_columns,
        deferred_fk_columns=tenant_settings_spec.deferred_fk_columns,
    )
    assert transformed["ai_settings"]["store_ai_mode"] == "test"
    assert transformed["ai_settings"]["ai_test_allowed_numbers"] == []
    assert transformed["ai_settings"]["store_ai_enabled"] is True
    assert transformed["whatsapp_settings"] == {}
    assert transformed["store_settings"]["catalog_title"] == "حذاء رياضي أبيض"
    assert any("tenant_settings.ai_settings_safe_test_mode" in t for t in transforms)
    assert any("tenant_settings.whatsapp_settings_stripped" in t for t in transforms)


def test_stale_v8_dry_run_digest_rejected_on_apply() -> None:
    stale_payload = clone_op.build_dry_run_digest_binding_payload(
        profile=CLONE_PROFILE_SALLA_MINIMAL,
        identity_mode_value=PRESERVE_TENANT_IDENTITY_MODE,
        source_database_identity_digest="sha256:source",
        target_database_identity_digest="sha256:target",
        source_tenant_id=48,
        target_tenant_id=48,
        target_shell_state="bootstrap_required",
        table_counts={},
        source_checksums={},
        dependency_order=[],
        target_denied_domain_counts={},
        source_alembic_heads=sorted(EXPECTED_SOURCE_ALEMBIC_HEADS),
        target_alembic_heads=sorted(EXPECTED_TARGET_ALEMBIC_HEADS),
    )
    stale_payload["schema_version"] = "tenant_merchant_clone_dry_run_v8"
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
        clone_id="schema-v8-drift-test",
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


def test_scrub_integration_config_strips_tokens() -> None:
    result = scrub_integration_config({"access_token": "secret", "store_id": "123"})
    assert result["access_token"] == ""
    assert result["store_id"] == ""


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
    assert result["store_id"] == ""
    assert result["nested"]["contact_email"] == ""
    assert result["contacts"][0]["ownerEmail"] == ""


def test_scrub_integration_config_rejects_unknown_forbidden_keys() -> None:
    with pytest.raises(ValueError, match="integration_config_unhandled_forbidden_key"):
        scrub_integration_config({"customer_id": "cust-123", "store_id": "123"})


def test_provider_ownership_keys_registry_is_closed() -> None:
    assert "store_id" in PROVIDER_OWNERSHIP_KEYS
    assert "merchant_id" in PROVIDER_OWNERSHIP_KEYS
    assert "external_store_id" in PROVIDER_OWNERSHIP_KEYS
    assert "authorization_id" in PROVIDER_OWNERSHIP_KEYS
    assert "phone_number_id" in PROVIDER_OWNERSHIP_KEYS
    assert "waba_id" in PROVIDER_OWNERSHIP_KEYS
    assert "whatsapp_business_account_id" in PROVIDER_OWNERSHIP_KEYS
    assert "shop_id" in PROVIDER_OWNERSHIP_KEYS
    assert "seller_id" in PROVIDER_OWNERSHIP_KEYS
    assert "vendor_id" in PROVIDER_OWNERSHIP_KEYS
    assert "meta_business_id" in PROVIDER_OWNERSHIP_KEYS
    assert "meta_catalog_id" in PROVIDER_OWNERSHIP_KEYS


def test_scrub_integration_config_strips_ownership_keys_nested_and_camel() -> None:
    from scripts.operators.tenant_merchant_clone_scrubber import _scrub_integration_value

    config = {
        "store_id": "store-snake-99",
        "routing": {"merchantId": "merchant-camel-88"},
        "providers": [
            {
                "externalStoreId": "ext-nested-77",
                "phoneNumberId": "phone-id-66",
                "waba_id": "waba-55",
            }
        ],
        "meta": {"whatsappBusinessAccountId": "waba-camel-44"},
    }
    result, transforms = _scrub_integration_value(config)
    assert result["store_id"] == ""
    assert result["routing"]["merchantId"] == ""
    assert result["providers"][0]["externalStoreId"] == ""
    assert result["providers"][0]["phoneNumberId"] == ""
    assert result["providers"][0]["waba_id"] == ""
    assert result["meta"]["whatsappBusinessAccountId"] == ""
    assert any("scrub_integration_ownership_key:store_id" in t for t in transforms)
    assert any("scrub_integration_ownership_key:routing.merchantId" in t for t in transforms)
    assert any(
        "scrub_integration_ownership_key:providers[0].externalStoreId" in t
        for t in transforms
    )


def test_scrub_integration_ownership_markers_path_only_no_raw_values() -> None:
    from scripts.operators.tenant_merchant_clone_scrubber import _scrub_integration_value

    sensitive_store_id = "store-sensitive-abc-12345"
    _, transforms = _scrub_integration_value(
        {"store_id": sensitive_store_id, "merchant_id": "merchant-sensitive-xyz"}
    )
    serialized = json.dumps(transforms)
    assert "store-sensitive-abc-12345" not in serialized
    assert "merchant-sensitive-xyz" not in serialized
    assert all(t.startswith("scrub_integration_ownership_key:") for t in transforms)


def test_scrub_integration_config_retains_safe_metadata() -> None:
    result = scrub_integration_config(
        {
            "provider": "generic_commerce",
            "app_type": "catalog",
            "store_label": "متجر تجريبي عام",
            "catalog_enabled": True,
            "store_id": "must-strip",
        }
    )
    assert result["provider"] == "generic_commerce"
    assert result["app_type"] == "catalog"
    assert result["store_label"] == "متجر تجريبي عام"
    assert result["catalog_enabled"] is True
    assert result["store_id"] == ""


def test_stale_v9_dry_run_digest_rejected_on_apply() -> None:
    stale_payload = clone_op.build_dry_run_digest_binding_payload(
        profile=CLONE_PROFILE_SALLA_MINIMAL,
        identity_mode_value=PRESERVE_TENANT_IDENTITY_MODE,
        source_database_identity_digest="sha256:source",
        target_database_identity_digest="sha256:target",
        source_tenant_id=48,
        target_tenant_id=48,
        target_shell_state="bootstrap_required",
        table_counts={},
        source_checksums={},
        dependency_order=[],
        target_denied_domain_counts={},
        source_alembic_heads=sorted(EXPECTED_SOURCE_ALEMBIC_HEADS),
        target_alembic_heads=sorted(EXPECTED_TARGET_ALEMBIC_HEADS),
    )
    stale_payload["schema_version"] = "tenant_merchant_clone_dry_run_v9"
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
        clone_id="schema-v9-drift-test",
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


def test_full_merchant_profile_integration_ownership_scrubbed_generic_merchant() -> None:
    integration_spec = next(
        spec
        for spec in table_specs_for_profile(CLONE_PROFILE_FULL_MERCHANT)
        if spec.name == "integrations"
    )
    row = {
        "id": 7,
        "tenant_id": 48,
        "provider": "generic_commerce",
        "app_type": "catalog",
        "external_store_id": "ext-generic-7",
        "enabled": True,
        "config": {
            "store_label": "متجر تجريبي عام",
            "shop_id": "shop-generic-1",
            "seller_id": "seller-generic-2",
            "vendor_id": "vendor-generic-3",
            "meta_business_id": "meta-biz-4",
            "meta_catalog_id": "meta-cat-5",
            "authorization_id": "auth-6",
        },
    }
    transformed, transforms = clone_op._transform_row(
        row,
        spec_name=integration_spec.name,
        spec_json_columns=integration_spec.json_columns,
        target_tenant_id=48,
        id_maps={},
        remap_fk_columns=integration_spec.remap_fk_columns,
        scrub_phone_columns=integration_spec.scrub_phone_columns,
        deferred_fk_columns=integration_spec.deferred_fk_columns,
    )
    assert transformed["enabled"] is False
    assert transformed["external_store_id"] is None
    assert transformed["config"]["store_label"] == "متجر تجريبي عام"
    for ownership_key in (
        "shop_id",
        "seller_id",
        "vendor_id",
        "meta_business_id",
        "meta_catalog_id",
        "authorization_id",
    ):
        assert transformed["config"][ownership_key] == ""
    assert any("integrations.config_stripped" in t for t in transforms)


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


def test_dry_run_digest_schema_version_is_v10() -> None:
    assert DRY_RUN_DIGEST_SCHEMA_VERSION == "tenant_merchant_clone_dry_run_v10"


def test_dry_run_digest_unchanged_when_only_volatile_source_telemetry_differs() -> None:
    binding = _topology_digest_payload(
        profile=CLONE_PROFILE_SALLA_MINIMAL,
        source_heads=sorted(EXPECTED_SOURCE_ALEMBIC_HEADS),
        target_heads=sorted(EXPECTED_TARGET_ALEMBIC_HEADS),
        table_counts={"products": 12},
    )
    assert "excluded_operational_source_counts" not in binding
    assert "denied_domain_source_counts" not in binding
    digest = clone_op.compute_dry_run_digest(binding)
    # v6 bound observational telemetry; identical copy state must keep the same v10 digest
    # even when operator-report counts drift between dry-run and apply.
    report_a = {
        "excluded_operational_source_counts": {
            "coupons": 4413,
            "whatsapp_templates": 88,
        },
        "denied_domain_source_counts": {
            "integrity_events": 120,
            "store_sync_jobs": 7,
            "system_events": 340,
        },
    }
    report_b = {
        "excluded_operational_source_counts": {
            "coupons": 9999,
            "whatsapp_templates": 0,
        },
        "denied_domain_source_counts": {
            "integrity_events": 999,
            "store_sync_jobs": 999,
            "system_events": 999,
        },
    }
    assert digest == clone_op.compute_dry_run_digest(binding)
    assert report_a != report_b
    # v6-style payloads would have diverged; v10 binding ignores report-only telemetry.
    v6_style_a = {**binding, **report_a}
    v6_style_b = {**binding, **report_b}
    assert clone_op.compute_dry_run_digest(v6_style_a) != clone_op.compute_dry_run_digest(
        v6_style_b
    )
    assert clone_op.compute_dry_run_digest(v6_style_a) != digest


def test_dry_run_digest_changes_when_copy_affecting_table_counts_change() -> None:
    payload_a = _topology_digest_payload(
        profile=CLONE_PROFILE_SALLA_MINIMAL,
        source_heads=sorted(EXPECTED_SOURCE_ALEMBIC_HEADS),
        target_heads=sorted(EXPECTED_TARGET_ALEMBIC_HEADS),
        table_counts={"products": 10, "integrations": 1},
    )
    payload_b = _topology_digest_payload(
        profile=CLONE_PROFILE_SALLA_MINIMAL,
        source_heads=sorted(EXPECTED_SOURCE_ALEMBIC_HEADS),
        target_heads=sorted(EXPECTED_TARGET_ALEMBIC_HEADS),
        table_counts={"products": 11, "integrations": 1},
    )
    assert clone_op.compute_dry_run_digest(payload_a) != clone_op.compute_dry_run_digest(
        payload_b
    )


def test_stale_v7_dry_run_digest_rejected_on_apply() -> None:
    stale_payload = clone_op.build_dry_run_digest_binding_payload(
        profile=CLONE_PROFILE_SALLA_MINIMAL,
        identity_mode_value=PRESERVE_TENANT_IDENTITY_MODE,
        source_database_identity_digest="sha256:source",
        target_database_identity_digest="sha256:target",
        source_tenant_id=48,
        target_tenant_id=48,
        target_shell_state="bootstrap_required",
        table_counts={},
        source_checksums={},
        dependency_order=[],
        target_denied_domain_counts={},
        source_alembic_heads=sorted(EXPECTED_SOURCE_ALEMBIC_HEADS),
        target_alembic_heads=sorted(EXPECTED_TARGET_ALEMBIC_HEADS),
    )
    stale_payload["schema_version"] = "tenant_merchant_clone_dry_run_v7"
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
        clone_id="schema-v7-drift-test",
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


def test_stale_v6_dry_run_digest_rejected_on_apply() -> None:
    stale_payload = clone_op.build_dry_run_digest_binding_payload(
        profile=CLONE_PROFILE_SALLA_MINIMAL,
        identity_mode_value=PRESERVE_TENANT_IDENTITY_MODE,
        source_database_identity_digest="sha256:source",
        target_database_identity_digest="sha256:target",
        source_tenant_id=48,
        target_tenant_id=48,
        target_shell_state="bootstrap_required",
        table_counts={},
        source_checksums={},
        dependency_order=[],
        target_denied_domain_counts={},
        source_alembic_heads=sorted(EXPECTED_SOURCE_ALEMBIC_HEADS),
        target_alembic_heads=sorted(EXPECTED_TARGET_ALEMBIC_HEADS),
    )
    stale_payload["schema_version"] = "tenant_merchant_clone_dry_run_v6"
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
        clone_id="schema-v6-drift-test",
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


def test_integration_transform_scrubs_salla_owner_email() -> None:
    pii_email = "owner@merchant.example"
    row = {
        "id": 1,
        "tenant_id": 48,
        "provider": "salla",
        "external_store_id": "store-123",
        "enabled": True,
        "config": {"salla_owner_email": pii_email, "store_id": "123"},
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
    assert transformed["config"]["salla_owner_email"] == ""
    assert transformed["config"]["store_id"] == ""
    assert pii_email not in json.dumps(transforms)


def test_integration_transform_scrubs_nested_email_variants() -> None:
    row = {
        "id": 1,
        "tenant_id": 48,
        "provider": "salla",
        "enabled": True,
        "config": {
            "nested": {"contact_email": "nested@merchant.example"},
            "contacts": [{"ownerEmail": "camel@merchant.example"}],
            "store_id": "123",
        },
    }
    transformed, _ = clone_op._transform_row(
        row,
        spec_name="integrations",
        spec_json_columns=("config",),
        target_tenant_id=48,
        id_maps={},
        remap_fk_columns=(),
        scrub_phone_columns=(),
        deferred_fk_columns=(),
    )
    assert transformed["config"]["nested"]["contact_email"] == ""
    assert transformed["config"]["contacts"][0]["ownerEmail"] == ""


def test_integration_transform_rejects_unknown_forbidden_customer_id() -> None:
    row = {
        "id": 1,
        "tenant_id": 48,
        "provider": "salla",
        "enabled": True,
        "config": {"customer_id": "cust-123", "store_id": "123"},
    }
    with pytest.raises(ValueError, match="integration_config_unhandled_forbidden_key"):
        clone_op._transform_row(
            row,
            spec_name="integrations",
            spec_json_columns=("config",),
            target_tenant_id=48,
            id_maps={},
            remap_fk_columns=(),
            scrub_phone_columns=(),
            deferred_fk_columns=(),
        )


def test_integration_transform_scrubs_known_secrets_and_passes_post_scrub_scan() -> None:
    row = {
        "id": 1,
        "tenant_id": 48,
        "provider": "salla",
        "enabled": True,
        "config": {
            "access_token": "secret-token",
            "refresh_token": "refresh",
            "phone_e164": "+966501234567",
            "store_id": "123",
        },
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
    assert transformed["config"]["access_token"] == ""
    assert transformed["config"]["refresh_token"] == ""
    assert transformed["config"]["phone_e164"] == "+00000000000"
    assert transformed["config"]["store_id"] == ""
    assert scan_for_unhandled_forbidden_keys(transformed["config"]) == []
    assert any("integrations.config_stripped" in t for t in transforms)


def test_non_integration_json_still_validates_raw_source_fail_closed() -> None:
    row = {
        "id": 1,
        "tenant_id": 48,
        "metadata": {"customer_id": "cust-123", "title": "generic"},
    }
    with pytest.raises(ValueError, match="unhandled_forbidden_json:products.metadata"):
        clone_op._transform_row(
            row,
            spec_name="products",
            spec_json_columns=("metadata",),
            target_tenant_id=48,
            id_maps={},
            remap_fk_columns=(),
            scrub_phone_columns=(),
            deferred_fk_columns=(),
        )


def test_stale_v5_dry_run_digest_rejected_on_apply() -> None:
    stale_payload = _topology_digest_payload(
        profile=CLONE_PROFILE_SALLA_MINIMAL,
        source_heads=sorted(EXPECTED_SOURCE_ALEMBIC_HEADS),
        target_heads=sorted(EXPECTED_TARGET_ALEMBIC_HEADS),
    )
    stale_payload["schema_version"] = "tenant_merchant_clone_dry_run_v5"
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
        clone_id="schema-v5-drift-test",
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


def test_minimal_profile_integration_row_transform_path_succeeds() -> None:
    integration_spec = next(
        spec
        for spec in table_specs_for_profile(CLONE_PROFILE_SALLA_MINIMAL)
        if spec.name == "integrations"
    )
    row = {
        "id": 99,
        "tenant_id": 48,
        "provider": "salla",
        "external_store_id": "ext-99",
        "enabled": True,
        "config": {
            "salla_owner_email": "owner@merchant.example",
            "access_token": "tok",
            "store_id": "123",
        },
    }
    transformed, transforms = clone_op._transform_row(
        row,
        spec_name=integration_spec.name,
        spec_json_columns=integration_spec.json_columns,
        target_tenant_id=48,
        id_maps={},
        remap_fk_columns=integration_spec.remap_fk_columns,
        scrub_phone_columns=integration_spec.scrub_phone_columns,
        deferred_fk_columns=integration_spec.deferred_fk_columns,
    )
    assert transformed["enabled"] is False
    assert transformed["external_store_id"] is None
    assert transformed["config"]["salla_owner_email"] == ""
    assert transformed["config"]["access_token"] == ""
    assert transformed["config"]["store_id"] == ""
    assert transformed["tenant_id"] == 48
    assert "id" not in transformed
    assert any("integrations.config_stripped" in t for t in transforms)


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
