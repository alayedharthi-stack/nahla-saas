"""Authenticated operator operations for the fixed Tenant 1 INTERNAL_E2E corpus."""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy.orm.attributes import flag_modified

from evals.commerce_agent_v2_whatsapp.scorer import score_batch
from services.commerce_v2_internal_e2e import (
    INTERNAL_E2E_ENABLED_ENV,
    INTERNAL_E2E_TENANT_ALLOWLIST_ENV,
    InternalE2EContractError,
    InternalE2ETurnRequest,
    assert_internal_e2e_operator_scope,
    provision_internal_e2e_fixtures,
    submit_internal_customer_turn,
)
from services.commerce_v2_whatsapp_e2e_contract import load_corpus, render_controlled_test_data


OPERATOR_TENANT_ID = 1
APPROVED_CORPUS_ID = "commerce_v2_phase_2_6_180_v1"
_CORPUS_PATH = (
    Path(__file__).resolve().parents[1]
    / "evals"
    / "commerce_agent_v2_whatsapp"
    / "corpus_v1.json"
)
_CONTROL_DIRECTION = "internal_e2e_control"
_CONTROL_EVENT_TYPE = "internal_e2e_batch"
logger = logging.getLogger("nahla.commerce_v2.internal_e2e_operator")


def _validated_batch_id(value: object) -> str:
    try:
        parsed = uuid.UUID(str(value or ""))
    except (ValueError, TypeError, AttributeError) as exc:
        raise InternalE2EContractError("internal_e2e_batch_id_invalid") from exc
    if str(parsed) != str(value):
        raise InternalE2EContractError("internal_e2e_batch_id_invalid")
    return str(parsed)


def approved_corpus(*, seed: int, order_number: str) -> list[dict[str, Any]]:
    if not 0 <= int(seed) <= 2_147_483_647:
        raise InternalE2EContractError("internal_e2e_seed_invalid")
    turns = render_controlled_test_data(
        load_corpus(_CORPUS_PATH, seed=int(seed)), test_order_number=order_number
    )
    rows = [{**turn.to_mapping(), "execution_mode": "INTERNAL_E2E"} for turn in turns]
    if len(rows) != 180:
        raise InternalE2EContractError("approved_internal_e2e_corpus_size_invalid")
    return rows


def operator_status(db: Any, *, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    env_map = env or os.environ
    enabled = str(env_map.get(INTERNAL_E2E_ENABLED_ENV) or "").lower() in {
        "1", "true", "yes", "on"
    }
    configured = str(env_map.get(INTERNAL_E2E_TENANT_ALLOWLIST_ENV) or "")
    status: dict[str, Any] = {
        "enabled": enabled,
        "operator_tenant_id": OPERATOR_TENANT_ID,
        "approved_corpus_id": APPROVED_CORPUS_ID,
        "approved_aliases": ["A", "B", "C"],
        "configured_allowlist": configured,
        "external_egress_allowed": False,
    }
    if not enabled:
        status["fixtures"] = {}
        return status
    assert_internal_e2e_operator_scope(OPERATOR_TENANT_ID, env=env_map)
    from models import Conversation
    fixtures: dict[str, Any] = {}
    for alias in "ABC":
        identity = f"internal_e2e:t1:customer:{alias.lower()}"
        row = db.query(Conversation).filter(
            Conversation.tenant_id == 1, Conversation.external_id == identity
        ).one_or_none()
        fixtures[alias] = {
            "provisioned": row is not None,
            "conversation_id": int(row.id) if row is not None else None,
        }
    status["fixtures"] = fixtures
    return status


def _batch_control(db: Any, batch_id: str) -> Any:
    from models import MessageEvent
    batch_id = _validated_batch_id(batch_id)
    rows = db.query(MessageEvent).filter(
        MessageEvent.tenant_id == OPERATOR_TENANT_ID,
        MessageEvent.direction == _CONTROL_DIRECTION,
        MessageEvent.event_type == _CONTROL_EVENT_TYPE,
    ).all()
    matches = [row for row in rows if dict(row.extra_metadata or {}).get("batch_id") == batch_id]
    if len(matches) != 1:
        raise InternalE2EContractError("internal_e2e_batch_not_found")
    return matches[0]


def create_batch(
    db: Any, *, seed: int, concurrency_waves: bool, env: Mapping[str, str] | None = None
) -> dict[str, Any]:
    from models import MessageEvent
    assert_internal_e2e_operator_scope(OPERATOR_TENANT_ID, env=env)
    fixtures = provision_internal_e2e_fixtures(db, tenant_id=OPERATOR_TENANT_ID, env=env)
    rows = approved_corpus(seed=seed, order_number=fixtures["C"].order_number)
    batch_id = str(uuid.uuid4())
    control = MessageEvent(
        tenant_id=OPERATOR_TENANT_ID,
        conversation_id=fixtures["A"].conversation_id,
        direction=_CONTROL_DIRECTION,
        body="",
        event_type=_CONTROL_EVENT_TYPE,
        extra_metadata={
            "channel": "internal_e2e",
            "synthetic": True,
            "test_only": True,
            "batch_id": batch_id,
            "corpus_id": APPROVED_CORPUS_ID,
            "seed": int(seed),
            "concurrency_waves": bool(concurrency_waves),
            "status": "queued",
            "turns_expected": len(rows),
            "turns_completed": 0,
            "external_egress_count": 0,
        },
    )
    db.add(control)
    db.commit()
    return dict(control.extra_metadata or {})


def batch_status(db: Any, batch_id: str) -> dict[str, Any]:
    assert_internal_e2e_operator_scope(OPERATOR_TENANT_ID)
    return dict(_batch_control(db, batch_id).extra_metadata or {})


def _batch_artifacts(db: Any, batch_id: str) -> list[dict[str, Any]]:
    from models import MessageEvent
    rows = db.query(MessageEvent).filter(
        MessageEvent.tenant_id == OPERATOR_TENANT_ID,
        MessageEvent.direction == "internal_e2e_outbound",
    ).order_by(MessageEvent.id.asc()).all()
    artifacts = []
    for row in rows:
        meta = dict(row.extra_metadata or {})
        artifact = meta.get("artifact")
        if isinstance(artifact, dict) and artifact.get("batch_id") == batch_id:
            artifacts.append(dict(artifact))
    return artifacts


async def execute_batch(batch_id: str) -> None:
    """Starlette background task; each turn owns its normal DB session."""
    from core.database import SessionLocal
    control_db = SessionLocal()
    try:
        control = _batch_control(control_db, batch_id)
        meta = dict(control.extra_metadata or {})
        assert_internal_e2e_operator_scope(OPERATOR_TENANT_ID)
        fixtures = provision_internal_e2e_fixtures(control_db, tenant_id=OPERATOR_TENANT_ID)
        rows = approved_corpus(seed=int(meta["seed"]), order_number=fixtures["C"].order_number)
        meta["status"] = "running"
        control.extra_metadata = meta
        flag_modified(control, "extra_metadata")
        control_db.commit()

        async def run_row(row: dict[str, Any]) -> dict[str, Any]:
            db = SessionLocal()
            try:
                return await submit_internal_customer_turn(
                    db,
                    InternalE2ETurnRequest(
                        tenant_id=OPERATOR_TENANT_ID,
                        synthetic_customer_alias=row["account_alias"],
                        text=row["inbound_text"],
                        case_id=row["case_id"],
                        service_tier=row["requested_service_tier"],
                        expected={
                            "expected_tools": list(row.get("expected_tools") or []),
                            "expected_outcome": row.get("expected_outcome") or "grounded_reply",
                            "common_turn": bool(row.get("common_turn", True)),
                        },
                        batch_id=batch_id,
                    ),
                )
            finally:
                db.close()

        results: list[dict[str, Any]] = []
        if bool(meta.get("concurrency_waves")):
            waves: dict[int, list[dict[str, Any]]] = {}
            for row in rows:
                waves.setdefault(int(row["wave_id"]), []).append(row)
            for wave_id in sorted(waves):
                results.extend(await asyncio.gather(*(run_row(row) for row in waves[wave_id])))
                if any(int(row.get("external_egress_count") or 0) for row in results):
                    break
        else:
            for row in rows:
                result = await run_row(row)
                results.append(result)
                if int(result.get("external_egress_count") or 0):
                    break
        control = _batch_control(control_db, batch_id)
        meta = dict(control.extra_metadata or {})
        meta.update(
            {
                "status": "completed" if len(results) == len(rows) else "failed",
                "turns_completed": len(results),
                "external_egress_count": sum(
                    int(row.get("external_egress_count") or 0) for row in results
                ),
            }
        )
        control.extra_metadata = meta
        flag_modified(control, "extra_metadata")
        control_db.commit()
    except Exception as exc:
        control_db.rollback()
        logger.exception(
            "INTERNAL_E2E approved batch failed batch_id=%s error_class=%s",
            batch_id,
            type(exc).__name__,
        )
        try:
            control = _batch_control(control_db, batch_id)
            meta = dict(control.extra_metadata or {})
            meta.update({"status": "failed", "failure_reason": type(exc).__name__})
            control.extra_metadata = meta
            flag_modified(control, "extra_metadata")
            control_db.commit()
        except Exception:  # noqa: silent-ok — original failure is logged above
            control_db.rollback()
    finally:
        control_db.close()


def score_completed_batch(db: Any, batch_id: str) -> dict[str, Any]:
    meta = batch_status(db, batch_id)
    if meta.get("status") != "completed":
        raise InternalE2EContractError("internal_e2e_batch_not_completed")
    corpus = approved_corpus(seed=int(meta["seed"]), order_number="IE2E-C-001")
    return score_batch(corpus, _batch_artifacts(db, batch_id))


def inspect_result(
    db: Any, *, internal_message_id: str | None = None, trace_id: str | None = None
) -> dict[str, Any]:
    assert_internal_e2e_operator_scope(OPERATOR_TENANT_ID)
    if bool(internal_message_id) == bool(trace_id):
        raise InternalE2EContractError("exactly_one_result_identifier_required")
    from models import MessageEvent
    rows = db.query(MessageEvent).filter(
        MessageEvent.tenant_id == OPERATOR_TENANT_ID,
        MessageEvent.direction == "internal_e2e_outbound",
    ).order_by(MessageEvent.id.desc()).all()
    for row in rows:
        artifact = dict(row.extra_metadata or {}).get("artifact")
        if not isinstance(artifact, dict):
            continue
        if internal_message_id in {
            artifact.get("internal_inbound_message_id"),
            artifact.get("internal_outbound_message_id"),
        } or (trace_id and artifact.get("trace_id") == trace_id):
            return dict(artifact)
    raise InternalE2EContractError("internal_e2e_result_not_found")


__all__ = [
    "APPROVED_CORPUS_ID", "OPERATOR_TENANT_ID", "approved_corpus", "batch_status",
    "create_batch", "execute_batch", "inspect_result", "operator_status",
    "score_completed_batch",
]
