"""Unit tests for read-only Knowledge Base source provenance operator."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

_REPO = Path(__file__).resolve().parents[2]
for entry in (str(_REPO), str(_REPO / "backend"), str(_REPO / "database")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from scripts.operators import knowledge_source_provenance as probe_op  # noqa: E402
from scripts.operators.knowledge_source_provenance_contract import (  # noqa: E402
    CODE_COMMAND_INVALID,
    CODE_DATABASE_URL_MISSING,
    CODE_TENANT_NOT_FOUND,
    DIAGNOSTIC_GAP_NOTE,
    REPORT_SCHEMA_VERSION,
    REQUIRED_TENANT_REPORT_KEYS,
    compute_divergence,
    snapshot_has_nonempty_policy_or_shipping,
    store_settings_has_policy_or_faq,
    text_length,
)


def test_contract_text_length_and_divergence() -> None:
    assert text_length("  hello  ") == 5
    assert text_length(None) == 0

    assert store_settings_has_policy_or_faq(
        store_settings={"shipping_policy": "x"},
        manual_knowledge_base_length=0,
    )
    assert not store_settings_has_policy_or_faq(
        store_settings={},
        manual_knowledge_base_length=0,
    )

    assert snapshot_has_nonempty_policy_or_shipping(
        {"shipping_policy": "ships"},
        {},
    )
    assert not snapshot_has_nonempty_policy_or_shipping({}, {})

    divergence = compute_divergence(
        knowledge_hub_active_sections=0,
        intelligence_store_settings_has_policy_or_faq=True,
        snapshot_has_nonempty_policy_or_shipping_text=False,
        structured_facts_nonempty=False,
        manual_knowledge_base_length=0,
    )
    assert divergence["sources_diverge"] is True

    divergence = compute_divergence(
        knowledge_hub_active_sections=2,
        intelligence_store_settings_has_policy_or_faq=False,
        snapshot_has_nonempty_policy_or_shipping_text=False,
        structured_facts_nonempty=False,
        manual_knowledge_base_length=0,
    )
    assert divergence["knowledge_hub_has_sections"] is True
    assert divergence["sources_diverge"] is True


def test_cli_help_exits_zero() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "scripts.operators.knowledge_source_provenance", "--help"],
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0
    assert "tenant" in completed.stdout
    assert "inventory" in completed.stdout


def test_main_rejects_unknown_command() -> None:
    with pytest.raises(SystemExit) as exc:
        probe_op.main(["unknown"])
    assert exc.value.code != 0


def test_run_tenant_requires_database_url() -> None:
    with patch.dict("os.environ", {}, clear=True):
        rc = probe_op.run_tenant(1)
    assert rc != 0


def test_run_tenant_emits_missing_database_url_code(capsys: pytest.CaptureFixture[str]) -> None:
    with patch.dict("os.environ", {}, clear=True):
        rc = probe_op.run_tenant(1)
    captured = capsys.readouterr().out.strip()
    payload = json.loads(captured)
    assert rc == 1
    assert payload["code"] == CODE_DATABASE_URL_MISSING


def test_resolve_database_url_prefers_public_over_private() -> None:
    url = probe_op.resolve_database_url(
        {
            "DATABASE_URL": "postgresql://private.railway.internal/db",
            "DATABASE_PUBLIC_URL": "postgresql://public.example/db",
        }
    )
    assert url.startswith("postgresql://public.example/")


def test_build_tenant_report_shape_with_sqlite_fixtures() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE tenants (id INTEGER PRIMARY KEY, name TEXT)"))
        conn.execute(text("INSERT INTO tenants (id, name) VALUES (1, 'متجر تجريبي عام')"))
        conn.execute(
            text(
                """
                CREATE TABLE tenant_settings (
                    tenant_id INTEGER PRIMARY KEY,
                    ai_settings TEXT,
                    store_settings TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO tenant_settings (tenant_id, ai_settings, store_settings)
                VALUES (1, '{"manual_knowledge_base": ""}', '{"shipping_policy": "short"}')
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE merchant_knowledge_sections (
                    id INTEGER PRIMARY KEY,
                    tenant_id INTEGER,
                    kind TEXT,
                    title TEXT,
                    body TEXT,
                    is_active INTEGER,
                    deleted_at TEXT,
                    updated_at TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO merchant_knowledge_sections
                (id, tenant_id, kind, title, body, is_active, deleted_at, updated_at)
                VALUES (10, 1, 'faq', 'FAQ title', 'abcd', 1, NULL, '2026-01-01')
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE merchant_knowledge_drafts (
                    id INTEGER PRIMARY KEY,
                    tenant_id INTEGER
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE merchant_branches (
                    id INTEGER PRIMARY KEY,
                    tenant_id INTEGER
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE store_knowledge_snapshots (
                    tenant_id INTEGER PRIMARY KEY,
                    policy_summary TEXT,
                    shipping_summary TEXT,
                    last_full_sync_at TEXT
                )
                """
            )
        )

    SessionLocal = sessionmaker(bind=engine)
    with engine.connect() as conn, SessionLocal() as db:
        with (
            patch.object(probe_op, "probe_structured_facts", return_value={"importable": True, "empty": False, "length": 12}),
            patch.object(
                probe_op,
                "probe_merchant_context",
                return_value={
                    "importable": True,
                    "policy_presence": {
                        "shipping_policy": True,
                        "payment_policy": False,
                        "return_policy": False,
                        "warranty_policy": False,
                        "delivery_areas": False,
                        "working_hours": False,
                    },
                },
            ),
        ):
            report = probe_op.build_tenant_report(conn, db, 1)

    assert set(report) >= REQUIRED_TENANT_REPORT_KEYS
    assert report["report_schema_version"] == REPORT_SCHEMA_VERSION
    assert report["tenant_id"] == 1
    assert report["tenant_name"] == "متجر تجريبي عام"
    assert report["counts"]["merchant_knowledge_sections"]["active"] == 1
    assert report["counts"]["tenant_settings"]["store_settings_field_lengths"]["shipping_policy"] == 5
    assert report["sample_section_ids"][0]["body_len"] == 4
    assert report["sample_section_ids"][0]["kind"] == "faq"
    assert report["diagnostic_gap_note"] == DIAGNOSTIC_GAP_NOTE
    assert "api_surface_map" in report


def test_build_tenant_report_missing_tenant_raises() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE tenants (id INTEGER PRIMARY KEY, name TEXT)"))

    SessionLocal = sessionmaker(bind=engine)
    with engine.connect() as conn, SessionLocal() as db:
        with pytest.raises(LookupError, match=CODE_TENANT_NOT_FOUND):
            probe_op.build_tenant_report(conn, db, 99)


def test_main_tenant_command_routes() -> None:
    with patch.object(probe_op, "run_tenant", return_value=0) as run_tenant:
        rc = probe_op.main(["tenant", "1"])
    assert rc == 0
    run_tenant.assert_called_once_with(1)


def test_main_inventory_command_routes() -> None:
    with patch.object(probe_op, "run_inventory", return_value=0) as run_inventory:
        rc = probe_op.main(["inventory"])
    assert rc == 0
    run_inventory.assert_called_once()


def test_emit_error_includes_code(capsys: pytest.CaptureFixture[str]) -> None:
    rc = probe_op.emit_error(CODE_COMMAND_INVALID)
    payload = json.loads(capsys.readouterr().out.strip())
    assert rc == 1
    assert payload["code"] == CODE_COMMAND_INVALID
