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
from typing import Any, Mapping, Optional

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
    ADDRESS_EXPECTATION_FIELDS,
    FAILURE_INJECTIONS,
    SCENARIO_SCHEMA_VERSION_V1,
    SCENARIO_SCHEMA_VERSION_V2,
    SCENARIO_SCHEMA_VERSIONS_SUPPORTED,
    TURN_MODE_BRAIN,
    TURN_MODE_OF2,
    TURN_MODES,
)
from services.internal_conversational_e2e_contract import (  # noqa: E402
    DATABASE_URL_ENV,
    EGRESS_DENIAL_KINDS,
    EVIDENCE_CHANNEL,
    EVIDENCE_HMAC_KEY_ENV,
    EVIDENCE_SCHEMA_VERSION,
    LIVE_TURN_STATUSES,
    LLM_ENABLE_ENV,
    SESSION_DIR_ENV,
    SAFE_AUDIT_VALUE_RE,
    SAFE_SCENARIO_ID_RE,
    TENANT_ALLOWLIST_ENV,
    TEST_PHONE_ENV,
    USER_ROLE_UNVERIFIABLE,
    evaluate_preflight,
    hmac_identifier,
    normalize_phone,
    parse_int_allowlist,
    preliminary_environment_blockers,
    sign_session_evidence,
)
from services.internal_conversational_e2e_harness import (  # noqa: E402
    OPERATIONAL_RESULTS,
    SandboxOf2TurnRequest,
    SandboxTurnRequest,
    run_sandbox_of2_turn,
    run_sandbox_turn,
)
from services.internal_conversational_e2e_sql_error_audit import (  # noqa: E402
    clear_last_turn_sql_error_audit,
    install_internal_e2e_sql_error_listener,
    last_turn_sql_error_audit,
    recorded_session_sql_error_audits,
    reset_session_sql_error_audit,
    summarize_session_sql_error_audit,
)


SCENARIO_SCHEMA_VERSION = SCENARIO_SCHEMA_VERSION_V1
CAPTURED_ACTION_PLACEHOLDER = "__captured_action_id__"
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
        # Canonical Alembic schemas may lack users.role; any user must remain explicit.
        roles = conn.execute(
            text(
                """
                SELECT COALESCE(
                    to_jsonb(u)->>'role',
                    :unverifiable_role
                ) AS role
                FROM users AS u
                WHERE u.tenant_id=:tenant_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "unverifiable_role": USER_ROLE_UNVERIFIABLE,
            },
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
    manifest_version = str(payload.get("scenario_schema_version") or "")
    if manifest_version not in SCENARIO_SCHEMA_VERSIONS_SUPPORTED:
        raise ValueError("scenario_manifest_invalid")
    of2_allowed = manifest_version == SCENARIO_SCHEMA_VERSION_V2
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
            not SAFE_SCENARIO_ID_RE.fullmatch(scenario_id)
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
            expected_status = str(turn.get("expected_status") or "evaluated")
            if expected_status not in LIVE_TURN_STATUSES:
                raise ValueError("expected_status_invalid")
            expected_denials = turn.get("expected_denials", [])
            if (
                not isinstance(expected_denials, list)
            ):
                raise ValueError("expected_denials_invalid")
            normalized_denials: list[tuple[str, str]] = []
            for denial in expected_denials:
                if (
                    not isinstance(denial, Mapping)
                    or set(denial) != {"egress_kind", "operation"}
                    or denial.get("egress_kind") not in EGRESS_DENIAL_KINDS
                    or not isinstance(denial.get("operation"), str)
                    or not SAFE_AUDIT_VALUE_RE.fullmatch(denial["operation"])
                ):
                    raise ValueError("expected_denials_invalid")
                normalized_denials.append(
                    (str(denial["egress_kind"]), denial["operation"])
                )
            if len(set(normalized_denials)) != len(normalized_denials):
                raise ValueError("expected_denials_invalid")
            # OrderFlowV2 turns are a v2-manifest capability. A v1
            # manifest that names one is rejected rather than silently
            # demoted to a Brain turn, which would quietly measure a
            # different path than the scenario asked for.
            mode = str(turn.get("mode") or TURN_MODE_BRAIN)
            if mode not in TURN_MODES or (mode == TURN_MODE_OF2 and not of2_allowed):
                raise ValueError("turn_mode_invalid")
            failure_injection = str(turn.get("failure_injection") or "none")
            if failure_injection not in FAILURE_INJECTIONS:
                raise ValueError("failure_injection_invalid")
            # Never defaulted for an OrderFlowV2 turn. A default of false
            # let a no-op handler with no capture and no model call
            # report a PASS, which is exactly the claim the flag exists
            # to prevent.
            if mode == TURN_MODE_OF2 and "expects_address_turn" not in turn:
                raise ValueError("expects_address_turn_required")
            expects_address_turn = bool(turn.get("expects_address_turn") or False)
            if expects_address_turn and mode != TURN_MODE_OF2:
                # Only the OrderFlowV2 path can produce address evidence,
                # so a Brain turn claiming it would declare a requirement
                # nothing in the run could ever satisfy.
                raise ValueError("expects_address_turn_invalid")
            operational_result = str(turn.get("expected_operational_result") or "")
            if mode == TURN_MODE_OF2 and operational_result not in OPERATIONAL_RESULTS:
                # Every OrderFlowV2 turn says what it must DO. Address
                # evidence is legitimately optional on a continuation
                # turn; proving the turn happened is not.
                raise ValueError("expected_operational_result_invalid")
            expectations = turn.get("expectations") or {}
            if not isinstance(expectations, Mapping):
                raise ValueError("expectations_invalid")
            if expects_address_turn and not any(
                str(expectations.get(field) or "").strip()
                for field in ADDRESS_EXPECTATION_FIELDS
            ):
                # "Some string is present" is not an assertion. A turn
                # that expects address evidence must say which field and
                # which goal it expects, or the evidence cannot contradict
                # anything.
                raise ValueError("expectations_required")
            checked_turns.append(
                {
                    "text": str(turn["text"]),
                    "expected_status": expected_status,
                    "expected_state_delta_keys": sorted(
                        str(v) for v in (turn.get("expected_state_delta_keys") or [])
                    ),
                    "expected_denials": tuple(sorted(normalized_denials)),
                    "mode": mode,
                    "failure_injection": failure_injection,
                    "expects_address_turn": expects_address_turn,
                    "expectations": dict(expectations),
                    "expected_operational_result": operational_result,
                    "inbound_metadata": dict(turn.get("inbound_metadata") or {}),
                    "captured_action_index": (
                        int(turn["captured_action_index"])
                        if isinstance(turn.get("captured_action_index"), int)
                        else None
                    ),
                }
            )
        seen.add(scenario_id)
        normalized.append({"scenario_id": scenario_id, "turns": checked_turns})
    return normalized


def _of2_path_report(turn_results: list[dict[str, Any]]) -> dict[str, Any]:
    """Natural outcomes and injected mechanism checks, never mixed.

    Two corrections live here. An injected failure confirms a MECHANISM;
    counting it beside naturally observed outcomes would invent a failure
    rate the run never measured, so the two carry separate denominators.
    And the denominator counts turns the OWNER established as address
    collection turns — the previous count of "every record the runner
    created" filed a customer-name turn as an address attempt.

    Outcomes are distinguished rather than bucketed: healthy ordinary
    composition and an ordinary provider-failure fallback are different
    results and must not share a number.
    """
    natural: dict[tuple[str, str], list[int]] = {}
    injected: dict[tuple[str, str], list[int]] = {}
    surfaces: dict[str, int] = {}
    attempted = 0
    not_address = 0
    for row in turn_results:
        address_turn = row.get("address_turn")
        if not isinstance(address_turn, Mapping) or not address_turn:
            continue
        # The owner's own state patch decides this, not the existence of
        # a record the runner writes for every OF2 invocation.
        if str(address_turn.get("collection_field") or "") not in (
            "delivery_address",
            "city",
        ):
            not_address += 1
            continue
        attempted += 1
        path = str(address_turn.get("execution_path") or "unresolved")
        outcome = _of2_outcome(address_turn)
        surface = str(address_turn.get("delivered_surface") or "none")
        surfaces[surface] = surfaces.get(surface, 0) + 1
        timing = address_turn.get("turn_timing")
        latency = int(row.get("latency_ms") or 0)
        if isinstance(timing, Mapping) and timing.get("total_turn_ms"):
            latency = int(timing.get("total_turn_ms") or latency)
        bucket = (
            natural
            if str(address_turn.get("failure_injection") or "none") == "none"
            else injected
        )
        bucket.setdefault((path, outcome), []).append(latency)

    def _summary(bucket: dict[tuple[str, str], list[int]]) -> dict[str, Any]:
        total = sum(len(v) for v in bucket.values())
        return {
            "denominator": total,
            "by_path_and_outcome": {
                f"{path}:{outcome}": {
                    "samples": len(samples),
                    "latency_ms_p50": _percentile(samples, 50),
                    "latency_ms_p95": _percentile(samples, 95),
                }
                for (path, outcome), samples in sorted(bucket.items())
            },
        }

    return {
        "address_turns_attempted": attempted,
        "non_address_turns_excluded": not_address,
        "natural": _summary(natural),
        "injected_mechanism_checks": _summary(injected),
        # A separate dimension on purpose: an ordinary turn for a customer
        # with no saved addresses carries no choices and is still ordinary.
        "delivered_surface_counts": dict(sorted(surfaces.items())),
        "representative_of_production": False,
    }


def _of2_outcome(address_turn: Mapping[str, Any]) -> str:
    """Success, fallback, refusal or unresolved — from the provenance."""
    provenance = address_turn.get("outbound_provenance")
    provenance = dict(provenance) if isinstance(provenance, Mapping) else {}
    reason = str(provenance.get("fallback_reason") or "").strip()
    if str(provenance.get("compose_source") or "") == "llm" and not reason:
        return "composed"
    if reason in (
        "address_reply_unsupported_after_compose",
        "address_reply_unverifiable_after_compose",
    ):
        return "refused_claim"
    if reason:
        return "fallback"
    if address_turn.get("compose_entered"):
        return "composed"
    return "unresolved"


def _percentile(samples: list[int], pct: int) -> Optional[int]:
    if not samples:
        return None
    ordered = sorted(samples)
    index = max(0, min(len(ordered) - 1, (pct * len(ordered)) // 100))
    return int(ordered[index])


def _continuation_metadata(
    turn: Mapping[str, Any], captured_action_ids: list[str],
) -> dict[str, Any]:
    """Replay a structured action taken from the previous captured payload.

    Substituting a real delivered id — rather than one the manifest
    invented — is the only way a continuation turn tests the offer the
    customer was actually shown. An index with no captured id leaves the
    placeholder in place, so the turn fails rather than silently
    answering a showing that never happened.
    """
    metadata = dict(turn.get("inbound_metadata") or {})
    placeholder_present = any(
        value == CAPTURED_ACTION_PLACEHOLDER for value in metadata.values()
    )
    index = turn.get("captured_action_index")
    if not placeholder_present:
        if index is not None:
            raise ValueError("captured_action_reference_unused")
        return metadata
    # A placeholder that cannot be resolved must REFUSE, not travel into
    # the turn unresolved: an unresolved marker answers no showing, and a
    # turn that silently replays it proves nothing about continuation.
    if index is None:
        raise ValueError("captured_action_reference_unresolved")
    if not captured_action_ids:
        raise ValueError("captured_action_reference_unresolved")
    if not 0 <= int(index) < len(captured_action_ids):
        raise ValueError("captured_action_reference_out_of_range")
    action_id = str(captured_action_ids[int(index)])
    return {
        key: (action_id if value == CAPTURED_ACTION_PLACEHOLDER else value)
        for key, value in metadata.items()
    }


def _conversation(db: Any, *, tenant_id: int, phone: str, session_id: str) -> tuple[Any, bool]:
    rows = (
        db.query(Conversation)
        .filter(
            Conversation.tenant_id == tenant_id,
            Conversation.external_id == phone,
        )
        .all()
    )
    if rows:
        raise ValueError("sandbox_test_phone_not_pristine")
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
    install_internal_e2e_sql_error_listener(db_engine)
    reset_session_sql_error_audit()
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
        last_captured_action_ids: list[str] = []
        for scenario in scenarios:
            for turn_index, turn in enumerate(scenario["turns"]):
                clear_last_turn_sql_error_audit()
                if turn["mode"] == TURN_MODE_OF2:
                    outcome = await run_sandbox_of2_turn(
                        db=db,
                        request=SandboxOf2TurnRequest(
                            session_id=session_id,
                            scenario_id=scenario["scenario_id"],
                            turn_index=turn_index,
                            tenant_id=tenant_id,
                            customer_phone=phone,
                            phone_id="internal-direct-code-probe",
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
                            turn_ref=(
                                f"probe.{scenario['scenario_id']}.{turn_index}"
                            ),
                            expected_denials=turn["expected_denials"],
                            allow_llm_inference=llm_allowed,
                            failure_injection=turn["failure_injection"],
                            expects_address_turn=turn["expects_address_turn"],
                            expectations=turn["expectations"],
                            expected_state_delta_keys=tuple(
                                turn["expected_state_delta_keys"]
                            ),
                            expected_operational_result=turn[
                                "expected_operational_result"
                            ],
                            inbound_metadata=_continuation_metadata(
                                turn, last_captured_action_ids,
                            ),
                            state_probe=_state_probe,
                        ),
                    )
                    clear_last_turn_sql_error_audit()
                    of2_evidence = dict(outcome.evidence)
                    # The same assertions the Brain path applies. Appending
                    # and continuing skipped them entirely, so a turn that
                    # returned early at a gate still reported the status it
                    # was initialised with.
                    of2_blockers: list[str] = []
                    if of2_evidence["status"] != turn["expected_status"]:
                        of2_blockers.append("unexpected_turn_status")
                    if of2_blockers:
                        of2_evidence["blockers"] = sorted(
                            set(of2_evidence.get("blockers") or []) | set(of2_blockers)
                        )
                        of2_evidence["verdict"] = "fail"
                    # A structured action the customer can only have taken
                    # from what was actually delivered: the next turn
                    # replays an id read back off THIS turn's captured
                    # payload, never one invented by the scenario.
                    last_captured_action_ids = list(
                        (of2_evidence.get("address_turn") or {}).get(
                            "receipt_action_ids"
                        )
                        or []
                    )
                    results.append(of2_evidence)
                    continue
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
                        expected_denials=turn["expected_denials"],
                        allow_llm_inference=llm_allowed,
                    ),
                    brain_factory=get_brain,
                    state_probe=_state_probe,
                )
                clear_last_turn_sql_error_audit()
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
        stable_blocker = (
            str(exc)
            if isinstance(exc, ValueError)
            and str(exc) == "sandbox_test_phone_not_pristine"
            else "runner_exception"
        )
        results.append(
            {
                "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
                "evidence_channel": EVIDENCE_CHANNEL,
                "session_id": session_id,
                "tenant_id": tenant_id,
                "verdict": "fail",
                "blockers": [stable_blocker],
                "exception_class": type(exc).__name__,
                "runtime_error_audit": last_turn_sql_error_audit()
                or {
                    "errors": [],
                    "error_count": 0,
                    "primary_missing": False,
                    "truncated": False,
                },
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
        "of2_path_report": _of2_path_report(results),
        "observation_window_utc": {
            "started_at_utc": started_at_utc,
            "completed_at_utc": completed_at_utc,
        },
        "runtime_error_audit": summarize_session_sql_error_audit(
            recorded_session_sql_error_audits()
        ),
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
