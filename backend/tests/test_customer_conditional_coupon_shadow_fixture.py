"""Tests for staging-only conditional-coupon shadow observation fixture harness."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from modules.ai.brain.truth_surface.customer_conditional_coupon_contract import (
    COMPLETENESS_VERIFIED,
    EVALUATION_CONDITION_SATISFIED,
    MIN_ORDERS_STATE_SATISFIED,
    assert_fact_record_sanitized,
)
from modules.ai.brain.truth_surface.customer_conditional_coupon_loader import (
    clear_customer_conditional_coupon_turn_cache,
    load_customer_conditional_coupon_facts,
)
from services.customer_conditional_coupon_shadow_fixture import (
    execute_customer_conditional_coupon_shadow_fixture_cleanup,
    execute_customer_conditional_coupon_shadow_fixture_seed,
    validate_shadow_fixture_capability_and_revision_gates,
)
from services.customer_conditional_coupon_shadow_fixture_contract import (
    CONFIRMATION_ENV_CLEANUP,
    CONFIRMATION_ENV_WRITE,
    CONFIRMATION_TOKEN_CLEANUP,
    CONFIRMATION_TOKEN_WRITE,
    FIXTURE_EXTERNAL_ID_PREFIX,
    FIXTURE_MARKER_FIELD,
    FIXTURE_NAMESPACE,
    FIXTURE_SCHEMA_VERSION,
    MIN_COUNTABLE_ORDERS,
)
from services.order_customer_identity_contract import (
    CAPABILITY_STATE_EXPAND,
    CAPABILITY_STATE_VALIDATED,
    EVIDENCE_AUTHORITATIVE,
    ORDER_SOURCE_NAHL_INTERNAL,
)
from tests.order_customer_identity_postgres_fixtures import (
    TEST_TENANT_A,
    TEST_TENANT_B,
    pg_session,
    postgres_engine,
    seed_capability_state,
    seed_tenant,
)

pytestmark = pytest.mark.usefixtures("postgres_engine")

_GENERIC_TENANT_NAME = "متجر تجريبي عام"
_NON_FIXTURE_ORDER_EXT = "PROD-ORDER-KEEP-01"
_CONDITIONAL_INTENT_MESSAGE = "conditional coupon after min orders for loyalty offer"

_PII_PATTERNS = (
    re.compile(r"@"),
    re.compile(r"\b\d{10,}\b"),
    re.compile(r'"customer_id"\s*:\s*\d'),
    re.compile(r'"conversation_id"\s*:\s*\d'),
    re.compile(r'"order_id"\s*:\s*\d'),
    re.compile(r'"phone"\s*:'),
    re.compile(r"postgresql://"),
    re.compile(r"Traceback"),
    re.compile(r"أحمد"),
    re.compile(r"حذاء"),
)

_REPO = Path(__file__).resolve().parents[2]
_CLI = "backend/scripts/seed_customer_conditional_coupon_shadow_fixture.py"


def _set_alembic_revisions(pg_session, *revisions: str) -> None:
    pg_session.execute(text("DELETE FROM alembic_version"))
    for revision in revisions:
        pg_session.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
            {"revision": revision},
        )
    pg_session.flush()


def _staging_env(**overrides: str) -> dict[str, str]:
    base = {
        "RAILWAY_PROJECT_NAME": "desirable-growth",
        "RAILWAY_ENVIRONMENT_NAME": "staging",
        CONFIRMATION_ENV_WRITE: CONFIRMATION_TOKEN_WRITE,
        CONFIRMATION_ENV_CLEANUP: CONFIRMATION_TOKEN_CLEANUP,
        "DATABASE_URL": (
            "postgresql://nahla:nahla_password@"
            "postgres-staging.railway.internal:5432/nahla_saas"
        ),
    }
    base.update(overrides)
    return base


def _fixture_dict_without_timestamp(payload: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(payload)
    out.pop("fixture_generated_at_utc", None)
    return out


def _assert_no_pii_in_fixture(payload: Dict[str, Any], *, known_safe: tuple[str, ...] = ()) -> None:
    blob = json.dumps(payload, ensure_ascii=False)
    for token in known_safe:
        blob = blob.replace(token, "")
    for pattern in _PII_PATTERNS:
        assert not pattern.search(blob), f"PII pattern {pattern.pattern!r} matched fixture JSON"


def _seed_gates(pg_session, *, tenant_id: int = TEST_TENANT_A) -> None:
    seed_tenant(
        pg_session,
        tenant_id=tenant_id,
        name=f"{_GENERIC_TENANT_NAME} ({tenant_id})",
    )
    _set_alembic_revisions(pg_session, "0088", "0089")
    seed_capability_state(
        pg_session,
        state=CAPABILITY_STATE_VALIDATED,
        validation_revision="0088",
    )


def _count_fixture_orders(pg_session, *, tenant_id: int) -> int:
    prefix = f"{FIXTURE_EXTERNAL_ID_PREFIX}-{int(tenant_id)}-%"
    return int(
        pg_session.execute(
            text(
                """
                SELECT count(*)::int
                FROM orders
                WHERE tenant_id = :tenant_id
                  AND external_id LIKE :prefix
                  AND metadata ->> :marker_key = :marker_value
                """
            ),
            {
                "tenant_id": int(tenant_id),
                "prefix": prefix,
                "marker_key": FIXTURE_MARKER_FIELD,
                "marker_value": FIXTURE_NAMESPACE,
            },
        ).scalar_one()
    )


@pytest.fixture(autouse=True)
def _clear_turn_cache() -> None:
    clear_customer_conditional_coupon_turn_cache()


@pytest.fixture
def shadow_coupon_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_SHADOW_ENABLED",
        "true",
    )


def test_gate_rejects_single_head_0088_only(pg_session) -> None:
    _seed_gates(pg_session)
    pg_session.execute(text("DELETE FROM alembic_version WHERE version_num = '0089'"))
    pg_session.flush()

    failure = validate_shadow_fixture_capability_and_revision_gates(pg_session)
    assert failure is not None
    assert failure.stage == "revision_0089_missing"


def test_gate_rejects_expand_capability(pg_session) -> None:
    seed_tenant(pg_session, tenant_id=TEST_TENANT_A, name=_GENERIC_TENANT_NAME)
    _set_alembic_revisions(pg_session, "0088", "0089")
    seed_capability_state(pg_session, state=CAPABILITY_STATE_EXPAND)

    failure = validate_shadow_fixture_capability_and_revision_gates(pg_session)
    assert failure is not None
    assert failure.stage == "capability_state_not_validated"


def test_dry_run_seed_reports_would_create_without_mutations(pg_session) -> None:
    _seed_gates(pg_session)
    before = _count_fixture_orders(pg_session, tenant_id=TEST_TENANT_A)

    result = execute_customer_conditional_coupon_shadow_fixture_seed(
        pg_session,
        TEST_TENANT_A,
        dry_run=True,
    )

    assert result.dry_run is True
    assert result.outcome == "success"
    assert result.committed is False
    assert sum(result.would_create.values()) > 0
    assert _count_fixture_orders(pg_session, tenant_id=TEST_TENANT_A) == before
    _assert_no_pii_in_fixture(result.to_dict(), known_safe=(str(TEST_TENANT_A),))


def test_write_creates_bridge_binding_and_conditional_promotion(pg_session) -> None:
    _seed_gates(pg_session)

    result = execute_customer_conditional_coupon_shadow_fixture_seed(
        pg_session,
        TEST_TENANT_A,
        dry_run=False,
    )

    assert result.outcome == "success", (
        result.gate_stage,
        result.gate_error_class,
        result.access_status,
    )
    assert result.committed is True
    assert result.created["countable_orders"] == MIN_COUNTABLE_ORDERS
    assert result.created["conditional_promotions"] == 1
    assert result.created["subject_bindings"] == 1
    assert result.bridge_resolved is True
    assert result.authoritative_internal_orders >= MIN_COUNTABLE_ORDERS
    assert result.active_conditional_targets >= 1

    binding_count = int(
        pg_session.execute(
            text(
                """
                SELECT count(*)::int
                FROM conversation_a1_subject_bindings casb
                JOIN conversations c ON c.id = casb.conversation_id
                  AND c.tenant_id = casb.tenant_id
                WHERE casb.tenant_id = :tenant_id
                  AND c.metadata ->> :marker_key = :marker_value
                  AND casb.binding_state = 'active'
                """
            ),
            {
                "tenant_id": TEST_TENANT_A,
                "marker_key": FIXTURE_MARKER_FIELD,
                "marker_value": FIXTURE_NAMESPACE,
            },
        ).scalar_one()
    )
    assert binding_count == 1


def test_seed_is_idempotent(pg_session) -> None:
    _seed_gates(pg_session)

    first = execute_customer_conditional_coupon_shadow_fixture_seed(
        pg_session,
        TEST_TENANT_A,
        dry_run=False,
    )
    assert first.outcome == "success"

    second = execute_customer_conditional_coupon_shadow_fixture_seed(
        pg_session,
        TEST_TENANT_A,
        dry_run=False,
    )
    assert second.outcome == "success"
    assert sum(second.created.values()) == 0
    assert second.bridge_resolved is True


def test_pg_loader_sees_resolved_bridge_and_sanitized_fact(
    pg_session,
    shadow_coupon_enabled: None,
) -> None:
    _seed_gates(pg_session)
    seed_result = execute_customer_conditional_coupon_shadow_fixture_seed(
        pg_session,
        TEST_TENANT_A,
        dry_run=False,
    )
    assert seed_result.outcome == "success"

    conversation_id = pg_session.execute(
        text(
            """
            SELECT id
            FROM conversations
            WHERE tenant_id = :tenant_id
              AND metadata ->> :marker_key = :marker_value
            LIMIT 1
            """
        ),
        {
            "tenant_id": TEST_TENANT_A,
            "marker_key": FIXTURE_MARKER_FIELD,
            "marker_value": FIXTURE_NAMESPACE,
        },
    ).scalar_one()
    from models import Conversation  # noqa: PLC0415

    conversation = pg_session.get(Conversation, int(conversation_id))
    assert conversation is not None

    with (
        patch(
            "services.promotion_engine.materialise_for_customer",
            new_callable=MagicMock,
        ) as materialise,
    ):
        facts, obs = load_customer_conditional_coupon_facts(
            db=pg_session,
            tenant_id=TEST_TENANT_A,
            message=_CONDITIONAL_INTENT_MESSAGE,
            conversation=conversation,
        )
        materialise.assert_not_called()

    assert len(facts) == 1
    record = facts[0].value
    assert_fact_record_sanitized(record)
    assert record["identity_status"] == "resolved"
    assert record["customer_scope"] == "nahla_internal_customer"
    assert record["order_history_completeness"] == COMPLETENESS_VERIFIED
    assert record["completed_orders_count"] == MIN_COUNTABLE_ORDERS
    assert record["min_orders_condition_state"] == MIN_ORDERS_STATE_SATISFIED
    assert record["conditional_coupon_evaluation_state"] == EVALUATION_CONDITION_SATISFIED
    assert obs["gate_skipped_reason"] is None
    assert obs["order_count_query_count"] == 1
    assert obs["usage_evidence_query_count"] == 1


def test_cleanup_removes_only_fixture_namespace_rows(pg_session) -> None:
    _seed_gates(pg_session)
    execute_customer_conditional_coupon_shadow_fixture_seed(
        pg_session,
        TEST_TENANT_A,
        dry_run=False,
    )

    from models import Customer, Order  # noqa: PLC0415

    keep_customer = Customer(tenant_id=TEST_TENANT_A, name="production-customer")
    pg_session.add(keep_customer)
    pg_session.flush()
    keep_order = Order(
        tenant_id=TEST_TENANT_A,
        external_id=_NON_FIXTURE_ORDER_EXT,
        status="pending",
        total="10.00",
        source="whatsapp",
        customer_id=keep_customer.id,
    )
    pg_session.add(keep_order)
    pg_session.flush()

    cleanup = execute_customer_conditional_coupon_shadow_fixture_cleanup(
        pg_session,
        TEST_TENANT_A,
        dry_run=False,
    )
    assert cleanup.outcome == "success"
    assert cleanup.committed is True
    assert _count_fixture_orders(pg_session, tenant_id=TEST_TENANT_A) == 0

    kept = (
        pg_session.query(Order.id)
        .filter(
            Order.tenant_id == TEST_TENANT_A,
            Order.external_id == _NON_FIXTURE_ORDER_EXT,
        )
        .count()
    )
    assert kept == 1


def test_cleanup_is_idempotent(pg_session) -> None:
    _seed_gates(pg_session)
    execute_customer_conditional_coupon_shadow_fixture_seed(
        pg_session,
        TEST_TENANT_A,
        dry_run=False,
    )

    first = execute_customer_conditional_coupon_shadow_fixture_cleanup(
        pg_session,
        TEST_TENANT_A,
        dry_run=False,
    )
    second = execute_customer_conditional_coupon_shadow_fixture_cleanup(
        pg_session,
        TEST_TENANT_A,
        dry_run=False,
    )
    assert first.outcome == "success"
    assert second.outcome == "success"
    assert sum(second.cleanup_deleted.values()) == 0


def test_cleanup_rejects_wrong_revision(pg_session) -> None:
    _seed_gates(pg_session)
    execute_customer_conditional_coupon_shadow_fixture_seed(
        pg_session,
        TEST_TENANT_A,
        dry_run=False,
    )
    _set_alembic_revisions(pg_session, "0088")

    result = execute_customer_conditional_coupon_shadow_fixture_cleanup(
        pg_session,
        TEST_TENANT_A,
        dry_run=True,
    )
    assert result.outcome == "failed"
    assert result.gate_stage == "revision_0089_missing"


def test_tenant_isolation(pg_session) -> None:
    _seed_gates(pg_session, tenant_id=TEST_TENANT_A)
    seed_tenant(pg_session, tenant_id=TEST_TENANT_B, name=f"{_GENERIC_TENANT_NAME} B")

    result_a = execute_customer_conditional_coupon_shadow_fixture_seed(
        pg_session,
        TEST_TENANT_A,
        dry_run=False,
    )
    assert result_a.outcome == "success"
    assert _count_fixture_orders(pg_session, tenant_id=TEST_TENANT_B) == 0


def test_cli_write_requires_staging_confirmation(pg_session) -> None:
    _seed_gates(pg_session)
    env = _staging_env()
    env.pop(CONFIRMATION_ENV_WRITE)

    proc = subprocess.run(
        [
            sys.executable,
            str(_REPO / _CLI),
            "--tenant-id",
            str(TEST_TENANT_A),
            "--write",
        ],
        cwd=str(_REPO),
        env={**os.environ, **env, "DATABASE_URL": os.environ.get("DATABASE_URL", env["DATABASE_URL"])},
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    payload = json.loads(proc.stdout)
    assert payload["access_status"] == "gate_rejected"


def test_fixture_json_schema_version(pg_session) -> None:
    _seed_gates(pg_session)
    result = execute_customer_conditional_coupon_shadow_fixture_seed(
        pg_session,
        TEST_TENANT_A,
        dry_run=True,
    )
    payload = result.to_dict()
    assert payload["fixture_schema_version"] == FIXTURE_SCHEMA_VERSION
    assert payload["fixture_namespace"] == FIXTURE_NAMESPACE
    assert payload["capability"]["alembic_revision_is_dual_0088_0089"] is True
    _assert_no_pii_in_fixture(payload, known_safe=(str(TEST_TENANT_A),))


def test_authoritative_orders_are_internal_completed(pg_session) -> None:
    _seed_gates(pg_session)
    execute_customer_conditional_coupon_shadow_fixture_seed(
        pg_session,
        TEST_TENANT_A,
        dry_run=False,
    )
    rows = pg_session.execute(
        text(
            """
            SELECT order_source_kind, customer_link_evidence_class, status
            FROM orders
            WHERE tenant_id = :tenant_id
              AND metadata ->> :marker_key = :marker_value
            """
        ),
        {
            "tenant_id": TEST_TENANT_A,
            "marker_key": FIXTURE_MARKER_FIELD,
            "marker_value": FIXTURE_NAMESPACE,
        },
    ).mappings().all()
    assert rows
    for row in rows:
        assert row["order_source_kind"] == ORDER_SOURCE_NAHL_INTERNAL
        assert row["customer_link_evidence_class"] == EVIDENCE_AUTHORITATIVE
        assert str(row["status"]).lower() == "completed"
