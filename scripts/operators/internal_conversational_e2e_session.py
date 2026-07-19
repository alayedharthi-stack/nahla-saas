"""Fail-closed operator for disposable internal conversational E2E sessions.

This module never calls the WhatsApp webhook, ``_post_wa``, or provider
dispatch. Sandbox cleanup is intentionally outside the application: dispose of
the database/service identified by the signed session attestation.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

APP_ROOT = Path(__file__).resolve().parents[2]
for _entry in (str(APP_ROOT), str(APP_ROOT / "backend"), str(APP_ROOT / "database")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from models import Conversation  # noqa: E402
from modules.ai.brain.pipeline import get_brain  # noqa: E402
from scripts.operators.deployment_revision_attestation_contract import (  # noqa: E402
    evaluate_runtime_revision_attestation,
)
from services.internal_conversational_e2e_contract import (  # noqa: E402
    DATABASE_URL_ENV,
    EVIDENCE_CHANNEL,
    EVIDENCE_HMAC_KEY_ENV,
    EVIDENCE_SCHEMA_VERSION,
    EXPECTED_DENIAL_KINDS,
    LLM_ENABLE_ENV,
    SESSION_DIR_ENV,
    TENANT_ALLOWLIST_ENV,
    TEST_PHONE_ENV,
    evaluate_preflight,
    hmac_identifier,
    normalize_phone,
    parse_int_allowlist,
    preliminary_environment_blockers,
    sign_session_evidence,
)
from services.internal_conversational_e2e_harness import (  # noqa: E402
    SandboxTurnRequest,
    run_sandbox_turn,
)


SCENARIO_SCHEMA_VERSION = "internal_conversational_e2e_scenarios_v1"
SESSION_SCHEMA_VERSION = "internal_conversational_e2e_session_v1"
MAX_SCENARIOS = 30
MAX_TURNS_PER_SCENARIO = 12


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _database_identity(conn: Any) -> dict[str, str]:
    row = conn.execute(
        text(
            """
            SELECT current_database() AS database_name,
                   COALESCE(inet_server_addr()::text, 'local') AS server_address,
                   COALESCE(inet_server_port()::text, 'local') AS server_port
            """
        )
    ).mappings().one()
    return {
        "database_name": str(row["database_name"]),
        "server_address": str(row["server_address"]),
        "server_port": str(row["server_port"]),
    }


def _tenant_rows(conn: Any, tenant_id: int) -> list[dict[str, Any]]:
    tenant_rows = conn.execute(
        text(
            "SELECT id,is_platform_tenant FROM tenants WHERE id=:tenant_id"
        ),
        {"tenant_id": tenant_id},
    ).mappings().all()
    rows: list[dict[str, Any]] = []
    for tenant in tenant_rows:
        ai_settings = conn.execute(
            text("SELECT ai_settings FROM tenant_settings WHERE tenant_id=:tenant_id"),
            {"tenant_id": tenant_id},
        ).scalar_one_or_none()
        roles = conn.execute(
            text("SELECT role FROM users WHERE tenant_id=:tenant_id"),
            {"tenant_id": tenant_id},
        ).scalars().all()
        rows.append(
            {
                "id": tenant["id"],
                "is_platform_tenant": bool(tenant["is_platform_tenant"]),
                "ai_settings": ai_settings,
                "user_roles": list(roles),
            }
        )
    return rows


def _runtime_revision(env: Mapping[str, str]) -> str | None:
    pin = str(env.get("NAHLA_INTERNAL_E2E_PINNED_REVISION") or "")
    result = evaluate_runtime_revision_attestation(
        pinned_target_revision=pin,
        target_app_root=APP_ROOT,
    )
    return result.attested_revision if result.ok else None


def execute_preflight(
    *,
    tenant_id: int,
    env: Mapping[str, str] | None = None,
    engine: Any | None = None,
) -> dict[str, Any]:
    env_map = dict(env or os.environ)
    preliminary_blockers = preliminary_environment_blockers(env_map)
    if preliminary_blockers:
        return {
            "ok": False,
            "blockers": sorted(set(preliminary_blockers)),
            "evidence_channel": EVIDENCE_CHANNEL,
            "tenant_id": tenant_id,
        }
    database_url = str(env_map.get(DATABASE_URL_ENV) or "").strip()
    if not database_url:
        return {
            "ok": False,
            "blockers": ["sandbox_database_url_missing"],
            "evidence_channel": EVIDENCE_CHANNEL,
            "tenant_id": tenant_id,
        }
    owned_engine = engine is None
    db_engine = engine or create_engine(database_url, pool_pre_ping=True)
    try:
        with db_engine.connect() as conn:
            identity = _database_identity(conn)
            tenant_rows = _tenant_rows(conn, tenant_id)
        return evaluate_preflight(
            env=env_map,
            tenant_id=tenant_id,
            identity=identity,
            tenant_rows=tenant_rows,
            attested_revision=_runtime_revision(env_map),
        )
    finally:
        if owned_engine:
            db_engine.dispose()


def _load_scenarios(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("scenario_schema_version") != SCENARIO_SCHEMA_VERSION:
        raise ValueError("scenario_manifest_invalid")
    scenarios = payload.get("scenarios")
    if not isinstance(scenarios, list) or not 0 < len(scenarios) <= MAX_SCENARIOS:
        raise ValueError("scenario_manifest_invalid")
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for scenario in scenarios:
        if not isinstance(scenario, Mapping):
            raise ValueError("scenario_manifest_invalid")
        scenario_id = str(scenario.get("scenario_id") or "")
        turns = scenario.get("turns")
        if (
            not scenario_id
            or scenario_id in seen
            or not isinstance(turns, list)
            or not 0 < len(turns) <= MAX_TURNS_PER_SCENARIO
        ):
            raise ValueError("scenario_manifest_invalid")
        checked_turns: list[dict[str, Any]] = []
        for turn in turns:
            if not isinstance(turn, Mapping) or not str(turn.get("text") or "").strip():
                raise ValueError("scenario_manifest_invalid")
            if "expected_text" in turn or "expected_reply" in turn:
                raise ValueError("exact_prose_assertion_forbidden")
            expected_denials = turn.get("expected_denial_kinds", [])
            if (
                not isinstance(expected_denials, list)
                or not all(isinstance(kind, str) for kind in expected_denials)
                or len(set(expected_denials)) != len(expected_denials)
                or not set(expected_denials).issubset(EXPECTED_DENIAL_KINDS)
            ):
                raise ValueError("expected_denial_kinds_invalid")
            checked_turns.append(
                {
                    "text": str(turn["text"]),
                    "expected_status": str(turn.get("expected_status") or "evaluated"),
                    "expected_state_delta_keys": sorted(
                        str(v) for v in (turn.get("expected_state_delta_keys") or [])
                    ),
                    "expected_denial_kinds": tuple(sorted(expected_denials)),
                }
            )
        seen.add(scenario_id)
        normalized.append({"scenario_id": scenario_id, "turns": checked_turns})
    return normalized


def _conversation(db: Any, *, tenant_id: int, phone: str, session_id: str) -> tuple[Any, bool]:
    rows = (
        db.query(Conversation)
        .filter(
            Conversation.tenant_id == tenant_id,
            Conversation.external_id == phone,
        )
        .all()
    )
    if len(rows) > 1:
        raise ValueError("conversation_identity_ambiguous")
    if rows:
        return rows[0], False
    convo = Conversation(
        tenant_id=tenant_id,
        external_id=phone,
        status="active",
        extra_metadata={
            "internal_e2e_session_id": session_id,
            "evidence_channel": EVIDENCE_CHANNEL,
            "synthetic": True,
        },
    )
    db.add(convo)
    db.commit()
    db.refresh(convo)
    return convo, True


def _state_probe(db: Any, tenant_id: int, convo: Any) -> dict[str, Any]:
    db.expire_all()
    db.refresh(convo)
    counts: dict[str, int] = {}
    for label, table_name in (
        ("message_events", "message_events"),
        ("orders", "orders"),
        ("handoff_sessions", "handoff_sessions"),
        ("automation_events", "automation_events"),
        ("llm_calls", "ai_usage_events"),
        ("tool_calls", "conversation_traces"),
    ):
        counts[label] = int(
            db.execute(
                text(f"SELECT COUNT(*) FROM {table_name} WHERE tenant_id=:tenant_id"),
                {"tenant_id": tenant_id},
            ).scalar_one()
            or 0
        )
    metadata = dict(getattr(convo, "extra_metadata", None) or {})
    counts.update(
        {
            "conversation_status": str(getattr(convo, "status", "") or ""),
            "conversation_handoff": bool(getattr(convo, "handoff_active", False)),
            "conversation_metadata_fingerprint": (
                f"sha256:{hashlib.sha256(_canonical(metadata).encode()).hexdigest()}"
            ),
        }
    )
    return counts


def _session_path(session_id: str, env: Mapping[str, str]) -> Path:
    configured = str(env.get(SESSION_DIR_ENV) or "").strip()
    base = Path(configured).expanduser().resolve() if configured else (
        APP_ROOT / ".nahla-internal-e2e-sessions"
    )
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{session_id}.json"


def _write_session(payload: Mapping[str, Any], env: Mapping[str, str]) -> Path:
    path = _session_path(str(payload["session_id"]), env)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(_canonical(payload) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


async def run_session(
    *,
    tenant_id: int,
    scenario_path: Path,
    env: Mapping[str, str] | None = None,
    engine: Any | None = None,
) -> dict[str, Any]:
    env_map = dict(env or os.environ)
    started_at_utc = datetime.now(timezone.utc).isoformat()
    preliminary_blockers = preliminary_environment_blockers(env_map)
    if preliminary_blockers:
        return {
            "ok": False,
            "blockers": sorted(set(preliminary_blockers)),
            "evidence_channel": EVIDENCE_CHANNEL,
            "tenant_id": tenant_id,
        }
    try:
        scenarios = _load_scenarios(scenario_path)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return {
            "ok": False,
            "blockers": ["scenario_manifest_invalid"],
            "evidence_channel": EVIDENCE_CHANNEL,
            "tenant_id": tenant_id,
            "exception_class": type(exc).__name__,
        }
    database_url = str(env_map.get(DATABASE_URL_ENV) or "").strip()
    if not database_url:
        return {"ok": False, "blockers": ["sandbox_database_url_missing"]}
    owned_engine = engine is None
    db_engine = engine or create_engine(database_url, pool_pre_ping=True)
    preflight = execute_preflight(tenant_id=tenant_id, env=env_map, engine=db_engine)
    if not preflight.get("ok"):
        if owned_engine:
            db_engine.dispose()
        return preflight

    session_id = str(uuid.uuid4())
    phone = normalize_phone(env_map.get(TEST_PHONE_ENV))
    evidence_key = str(env_map[EVIDENCE_HMAC_KEY_ENV])
    allowed_tenants = parse_int_allowlist(env_map.get(TENANT_ALLOWLIST_ENV))
    llm_allowed = str(env_map.get(LLM_ENABLE_ENV) or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    SessionLocal = sessionmaker(bind=db_engine, autoflush=False, autocommit=False)
    db = SessionLocal()
    results: list[dict[str, Any]] = []
    runner_mutations: list[str] = []
    try:
        convo, created = _conversation(
            db,
            tenant_id=tenant_id,
            phone=phone,
            session_id=session_id,
        )
        if created:
            runner_mutations.append("sandbox_conversation_created")
        for scenario in scenarios:
            for turn_index, turn in enumerate(scenario["turns"]):
                outcome = await run_sandbox_turn(
                    db=db,
                    request=SandboxTurnRequest(
                        session_id=session_id,
                        scenario_id=scenario["scenario_id"],
                        turn_index=turn_index,
                        tenant_id=tenant_id,
                        customer_phone=phone,
                        text=turn["text"],
                        conversation=convo,
                        allowed_tenants=allowed_tenants,
                        evidence_hmac_key=evidence_key,
                        runtime_revision=str(preflight["runtime_revision"]),
                        database_identity_fingerprint=str(
                            preflight["database_identity_fingerprint"]
                        ),
                        network_attestation_id=str(preflight["attestation_id"]),
                        llm_allowed_hosts=tuple(preflight["llm_allowed_hosts"]),
                        expected_denial_kinds=turn["expected_denial_kinds"],
                        allow_llm_inference=llm_allowed,
                    ),
                    brain_factory=get_brain,
                    state_probe=_state_probe,
                )
                evidence = dict(outcome.evidence)
                assertion_blockers: list[str] = []
                if evidence["status"] != turn["expected_status"]:
                    assertion_blockers.append("unexpected_turn_status")
                missing_delta = sorted(
                    set(turn["expected_state_delta_keys"]) - set(evidence["state_delta"])
                )
                if missing_delta:
                    assertion_blockers.append("expected_state_delta_missing")
                if assertion_blockers:
                    evidence["blockers"] = sorted(
                        set(evidence["blockers"]) | set(assertion_blockers)
                    )
                    evidence["verdict"] = "fail"
                results.append(evidence)
    except Exception as exc:
        db.rollback()
        results.append(
            {
                "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
                "evidence_channel": EVIDENCE_CHANNEL,
                "session_id": session_id,
                "tenant_id": tenant_id,
                "verdict": "fail",
                "blockers": ["runner_exception"],
                "exception_class": type(exc).__name__,
                "provider_observation": {
                    "source": "application_internal_e2e_context",
                    "network_dispatch_success_observed": False,
                    "is_actual_provider_telemetry": False,
                },
                "actual_provider_acceptance_satisfied": False,
            }
        )
    finally:
        db.close()
        if owned_engine:
            db_engine.dispose()

    completed_at_utc = datetime.now(timezone.utc).isoformat()
    session = {
        "session_schema_version": SESSION_SCHEMA_VERSION,
        "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
        "session_id": session_id,
        "tenant_id": tenant_id,
        "runtime_revision": preflight["runtime_revision"],
        "database_identity_fingerprint": preflight["database_identity_fingerprint"],
        "network_attestation_id": preflight["attestation_id"],
        "evidence_channel": EVIDENCE_CHANNEL,
        "test_phone_hmac": hmac_identifier(phone, key=evidence_key),
        "llm_inference_enabled": llm_allowed,
        "llm_allowed_hosts": preflight["llm_allowed_hosts"],
        "started_at_utc": started_at_utc,
        "completed_at_utc": completed_at_utc,
        "runner_mutations": runner_mutations,
        "turn_results": results,
        "verdict": "pass" if results and all(r.get("verdict") == "pass" for r in results) else "fail",
        "blockers": sorted(
            {
                blocker
                for result in results
                for blocker in (result.get("blockers") or [])
            }
        ),
        "actual_provider_acceptance_satisfied": False,
        "provider_observation": {
            "source": "application_internal_e2e_context",
            "network_dispatch_success_observed": False,
            "is_actual_provider_telemetry": False,
        },
        "cleanup_contract": "dispose_attested_sandbox_database_externally",
    }
    signed_session = sign_session_evidence(session, key=evidence_key)
    path = _write_session(signed_session, env_map)
    return {
        "ok": signed_session["verdict"] == "pass",
        "session_id": session_id,
        "tenant_id": tenant_id,
        "evidence_channel": EVIDENCE_CHANNEL,
        "verdict": signed_session["verdict"],
        "blockers": signed_session["blockers"],
        "session_path": str(path),
        "provider_observation": signed_session["provider_observation"],
        "integrity": {
            key: value
            for key, value in signed_session["integrity"].items()
            if key != "signature"
        },
        "actual_provider_acceptance_satisfied": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    preflight_parser = subparsers.add_parser("preflight")
    preflight_parser.add_argument("--tenant-id", type=int, required=True)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--tenant-id", type=int, required=True)
    run_parser.add_argument("--scenarios", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "preflight":
        result = execute_preflight(tenant_id=args.tenant_id)
    else:
        result = asyncio.run(
            run_session(tenant_id=args.tenant_id, scenario_path=args.scenarios)
        )
    print(_canonical(result))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
