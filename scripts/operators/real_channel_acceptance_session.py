"""Fail-closed session runner for actual-provider conversational acceptance.

This operator never sends WhatsApp messages and never mutates tenant data. A
human (or a separately authorized existing device integration) sends each test
input from the private allowlisted WhatsApp device. The runner observes
persisted evidence and cannot award actual-channel PASS from an injected HTTP
request, a direct code call, or database fixtures alone.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from sqlalchemy import create_engine, text

from scripts.operators.real_channel_conversational_acceptance import (
    execute_channel_health_preflight,
    gate_runtime_revision_attestation,
    gate_staging_identity,
)
from scripts.operators.real_channel_conversational_acceptance_contract import (
    ALLOWLIST_PHONES_ENV,
    ARCH001_SHADOW_SIGNOFF_ENV,
    CODE_ACCEPTANCE_NOT_ENABLED,
    CODE_ARCH001_SIGNOFF_MISSING,
    CODE_CHANNEL_HEALTH_BLOCKED,
    CODE_CONFIG_DRIFT,
    CODE_DEVICE_ATTESTATION_REQUIRED,
    CODE_EVENT_CURSOR_STALE,
    CODE_EXECUTION_NOT_CONFIRMED,
    CODE_HUMAN_ASSESSMENT_REQUIRED,
    CODE_INBOUND_ORIGIN_REJECTED,
    CODE_INBOUND_PROVIDER_ID_MISSING,
    CODE_INBOUND_PROVIDER_ID_REJECTED,
    CODE_OUTBOUND_PROVIDER_ID_MISSING,
    CODE_PHONE_NOT_ALLOWLISTED,
    CODE_PROVENANCE_INCOMPLETE,
    CODE_RATE_CAP_EXCEEDED,
    CODE_REAL_CHANNEL_REQUIRED,
    CODE_SESSION_NOT_FOUND,
    CODE_SESSION_STATE_INVALID,
    CODE_STORE_AI_MODE_INVALID,
    CODE_TENANT_1_PASS_ARTIFACT_INVALID,
    EVIDENCE_CHANNEL_ACTUAL_PROVIDER,
    EVIDENCE_CHANNEL_DIRECT_CODE_PROBE,
    EVIDENCE_CHANNEL_DIRECT_SIGNED_WEBHOOK,
    EVIDENCE_HMAC_KEY_ENV,
    EVIDENCE_SCHEMA_VERSION,
    EXECUTION_CONFIRM_ENV,
    HUMAN_RUBRIC_VALUES,
    MASTER_ENABLE_ENV,
    MAX_INBOUND_MESSAGES_PER_SESSION,
    MAX_LLM_CALLS_PER_SESSION,
    MAX_OUTBOUND_PROVIDER_CALLS_PER_SESSION,
    MAX_SCENARIOS_PER_SESSION,
    MAX_SESSION_COST_USD,
    PHASE_TENANT_1_INTENSIVE,
    PHASE_TENANT_33_LIMITED,
    PHASE_TENANT_48_SALLA_MINIMAL,
    PINNED_REVISION_ENV,
    PROVENANCE_FIELDS,
    REVIEWER_ID_ENV,
    SESSION_DEFAULT_DIR,
    SESSION_DIR_ENV,
    SESSION_SCHEMA_VERSION,
    SESSION_STATE_AWAITING_DEVICE_SEND,
    SESSION_STATE_COMPLETED,
    SESSION_STATE_HUMAN_ASSESSED,
    SESSION_STATE_OBSERVED,
    SESSION_STATE_SCENARIO_COMPLETED,
    SESSION_STATE_STARTED,
    SESSION_STATE_TORN_DOWN,
    TENANT_1_INTENSIVE,
    TENANT_1_PASS_ARTIFACT_ENV,
    TENANT_1_PHONE_ENV,
    TENANT_33_LIMITED,
    TENANT_33_PHONE_ENV,
    TENANT_48_PHONE_ENV,
    TENANT_48_SALLA_MINIMAL,
    env_flag_enabled,
    hmac_identifier,
    load_scenario_manifest,
    parse_allowlist_phones,
    resolve_acceptance_phase,
)

DEPLOYMENT_ID_ENV = "RAILWAY_DEPLOYMENT_ID"
DATABASE_URL_ENV = "DATABASE_URL"
_PROVIDER_WAMID_RE = re.compile(r"^wamid\.[A-Za-z0-9_+=:/.-]{12,255}$")
_REJECTED_MARKERS = (
    "synthetic",
    "fixture",
    "direct",
    "probe",
    "constitution-smoke",
    "not_real_channel",
)
_CORE_PROVENANCE_FIELDS = PROVENANCE_FIELDS[:6]


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _hmac_value(value: Any, *, key: str) -> str:
    digest = hmac.new(key.encode(), _canonical(value).encode(), hashlib.sha256).hexdigest()
    return f"hmac-sha256:{digest}"


def _session_dir(app_root: Path | None = None) -> Path:
    configured = (os.environ.get(SESSION_DIR_ENV) or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    root = (app_root or Path(__file__).resolve().parents[2]).resolve()
    return root / SESSION_DEFAULT_DIR


def _session_path(session_id: str, app_root: Path | None = None) -> Path:
    if not re.fullmatch(r"[a-f0-9-]{12,64}", session_id):
        raise ValueError(CODE_SESSION_NOT_FOUND)
    return _session_dir(app_root) / f"{session_id}.json"


def _write_session(session: Mapping[str, Any], app_root: Path | None = None) -> Path:
    path = _session_path(str(session["session_id"]), app_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(_canonical(session) + "\n", encoding="utf-8")
    tmp.replace(path)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def load_session(session_id: str, app_root: Path | None = None) -> dict[str, Any]:
    path = _session_path(session_id, app_root)
    if not path.exists():
        raise ValueError(CODE_SESSION_NOT_FOUND)
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("session_schema_version") != SESSION_SCHEMA_VERSION:
        raise ValueError(CODE_SESSION_STATE_INVALID)
    return data


def _engine():
    database_url = (os.environ.get(DATABASE_URL_ENV) or "").strip()
    if not database_url:
        raise ValueError("database_url_missing")
    return create_engine(database_url, pool_pre_ping=True)


_PHONE_ENV_BY_TENANT = {
    TENANT_1_INTENSIVE: TENANT_1_PHONE_ENV,
    TENANT_33_LIMITED: TENANT_33_PHONE_ENV,
    TENANT_48_SALLA_MINIMAL: TENANT_48_PHONE_ENV,
}


def _phone_env_for_tenant(tenant_id: int) -> str:
    try:
        return _PHONE_ENV_BY_TENANT[tenant_id]
    except KeyError as exc:
        raise ValueError("tenant_not_allowed") from exc


def _phase_for_tenant(tenant_id: int) -> str:
    return resolve_acceptance_phase(tenant_id)


def _redact_ai_settings(ai_settings: Mapping[str, Any], *, key: str) -> dict[str, Any]:
    allowlist = [
        hmac_identifier(str(phone), key=key)
        for phone in (ai_settings.get("ai_test_allowed_numbers") or [])
    ]
    return {
        "store_ai_mode": ai_settings.get("store_ai_mode"),
        "store_ai_enabled": ai_settings.get("store_ai_enabled"),
        "ai_test_allowed_numbers_hmac": allowlist,
        "setting_keys": sorted(str(name) for name in ai_settings),
        "all_other_values": "<redacted>",
    }


def _normalize_conversation_guard(row: Mapping[str, Any] | None) -> dict[str, bool]:
    row = row or {}
    return {
        "is_human_handoff": bool(row.get("is_human_handoff")),
        "paused_by_human": bool(row.get("paused_by_human")),
        "ai_paused": bool(row.get("ai_paused")),
        "handoff_active": bool(row.get("handoff_active")),
        "needs_human": bool(row.get("needs_human")),
    }


def _required_start_gates(tenant_id: int, app_root: Path | None) -> list[str]:
    blockers: list[str] = []
    if not env_flag_enabled(os.environ.get(MASTER_ENABLE_ENV)):
        blockers.append(CODE_ACCEPTANCE_NOT_ENABLED)
    if not env_flag_enabled(os.environ.get(EXECUTION_CONFIRM_ENV)):
        blockers.append(CODE_EXECUTION_NOT_CONFIRMED)
    if not env_flag_enabled(os.environ.get(ARCH001_SHADOW_SIGNOFF_ENV)):
        blockers.append(CODE_ARCH001_SIGNOFF_MISSING)
    identity = gate_staging_identity()
    if not identity.get("ok"):
        blockers.append(str(identity.get("code") or "staging_identity_rejected"))
    revision = gate_runtime_revision_attestation(
        pinned_target_revision=os.environ.get(PINNED_REVISION_ENV),
        target_app_root=app_root,
    )
    if not revision.get("ok"):
        blockers.append(str(revision.get("code") or "runtime_revision_mismatch"))
    channel = execute_channel_health_preflight(tenant_id=tenant_id)
    if not channel.get("ok"):
        blockers.append(str(channel.get("code") or CODE_CHANNEL_HEALTH_BLOCKED))
    if not (os.environ.get(DEPLOYMENT_ID_ENV) or "").strip():
        blockers.append("deployment_id_missing")
    if not (os.environ.get(EVIDENCE_HMAC_KEY_ENV) or "").strip():
        blockers.append("evidence_hmac_key_missing")
    if tenant_id == TENANT_33_LIMITED:
        try:
            verify_tenant_1_pass_artifact()
        except ValueError:
            blockers.append(CODE_TENANT_1_PASS_ARTIFACT_INVALID)
    return blockers


def verify_tenant_1_pass_artifact() -> dict[str, Any]:
    artifact_path = (os.environ.get(TENANT_1_PASS_ARTIFACT_ENV) or "").strip()
    key = (os.environ.get(EVIDENCE_HMAC_KEY_ENV) or "").strip()
    if not artifact_path or not key:
        raise ValueError(CODE_TENANT_1_PASS_ARTIFACT_INVALID)
    payload = json.loads(Path(artifact_path).read_text(encoding="utf-8"))
    signature = str(payload.pop("signature", ""))
    expected = _hmac_value(payload, key=key)
    if not hmac.compare_digest(signature, expected):
        raise ValueError(CODE_TENANT_1_PASS_ARTIFACT_INVALID)
    if payload.get("tenant_id") != TENANT_1_INTENSIVE or payload.get("verdict") != "pass":
        raise ValueError(CODE_TENANT_1_PASS_ARTIFACT_INVALID)
    if not payload.get("teardown_verified"):
        raise ValueError(CODE_TENANT_1_PASS_ARTIFACT_INVALID)
    return payload


def start_session(*, tenant_id: int, app_root: Path | None = None) -> dict[str, Any]:
    phase = _phase_for_tenant(tenant_id)
    root = (app_root or Path(__file__).resolve().parents[2]).resolve()
    blockers = _required_start_gates(tenant_id, root)
    if blockers:
        return {"ok": False, "state": "blocked", "blockers": sorted(set(blockers))}

    key = os.environ[EVIDENCE_HMAC_KEY_ENV]
    phone = re.sub(r"\D", "", os.environ.get(_phone_env_for_tenant(tenant_id), ""))
    allowlist = parse_allowlist_phones(os.environ.get(ALLOWLIST_PHONES_ENV))
    if not phone or phone not in allowlist:
        return {"ok": False, "state": "blocked", "blockers": [CODE_PHONE_NOT_ALLOWLISTED]}

    manifest = load_scenario_manifest(root)
    scenarios = [row["scenario_id"] for row in manifest["scenarios"] if row["phase"] == phase]
    if not scenarios or len(scenarios) > MAX_SCENARIOS_PER_SESSION:
        return {"ok": False, "state": "blocked", "blockers": [CODE_RATE_CAP_EXCEEDED]}

    engine = _engine()
    with engine.connect() as conn:
        ai_settings = conn.execute(
            text("SELECT ai_settings FROM tenant_settings WHERE tenant_id=:tenant_id"),
            {"tenant_id": tenant_id},
        ).scalar_one_or_none()
        if not isinstance(ai_settings, Mapping):
            return {"ok": False, "state": "blocked", "blockers": ["tenant_settings_missing"]}
        if str(ai_settings.get("store_ai_mode") or "") != "test":
            return {"ok": False, "state": "blocked", "blockers": [CODE_STORE_AI_MODE_INVALID]}
        if ai_settings.get("store_ai_enabled", True) is not True:
            return {"ok": False, "state": "blocked", "blockers": [CODE_STORE_AI_MODE_INVALID]}
        db_allowlist = {
            re.sub(r"\D", "", str(value))
            for value in (ai_settings.get("ai_test_allowed_numbers") or [])
        }
        if phone not in db_allowlist or not db_allowlist.issubset(set(allowlist)):
            return {"ok": False, "state": "blocked", "blockers": [CODE_PHONE_NOT_ALLOWLISTED]}
        cursor = conn.execute(
            text("SELECT COALESCE(MAX(id),0) FROM message_events WHERE tenant_id=:tenant_id"),
            {"tenant_id": tenant_id},
        ).scalar_one()
        usage_cursor = conn.execute(
            text("SELECT COALESCE(MAX(id),0) FROM ai_usage_events WHERE tenant_id=:tenant_id"),
            {"tenant_id": tenant_id},
        ).scalar_one()
        order_cursor = conn.execute(
            text("SELECT COALESCE(MAX(id),0) FROM orders WHERE tenant_id=:tenant_id"),
            {"tenant_id": tenant_id},
        ).scalar_one()
        tenant_guard_state = conn.execute(
            text(
                "SELECT subscription_status,ai_blocked_numbers FROM tenants "
                "WHERE id=:tenant_id"
            ),
            {"tenant_id": tenant_id},
        ).mappings().one()
        conversation_guard_state = conn.execute(
            text(
                "SELECT status,is_human_handoff,paused_by_human,ai_paused,"
                "handoff_active,needs_human FROM conversations "
                "WHERE tenant_id=:tenant_id AND external_id=:phone "
                "ORDER BY id DESC LIMIT 1"
            ),
            {"tenant_id": tenant_id, "phone": phone},
        ).mappings().first()

    session_id = str(uuid.uuid4())
    exact_config = {
        "ai_settings": ai_settings,
        "tenant_guard_state": dict(tenant_guard_state),
        "conversation_guard_state": _normalize_conversation_guard(conversation_guard_state),
    }
    snapshot_fingerprint = _hmac_value(exact_config, key=key)
    session = {
        "session_schema_version": SESSION_SCHEMA_VERSION,
        "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
        "session_id": session_id,
        "state": SESSION_STATE_STARTED,
        "tenant_id": tenant_id,
        "phase": phase,
        "started_at_utc": _utc_now(),
        "deployment": {
            "revision": os.environ.get(PINNED_REVISION_ENV),
            "deployment_id_hmac": hmac_identifier(os.environ[DEPLOYMENT_ID_ENV], key=key),
        },
        "test_phone_hmac": hmac_identifier(phone, key=key),
        "event_cursor": int(cursor or 0),
        "usage_cursor": int(usage_cursor or 0),
        "order_cursor": int(order_cursor or 0),
        "config_snapshot": {
            "fingerprint": snapshot_fingerprint,
            "sanitized": _redact_ai_settings(ai_settings, key=key),
            "guard_state": {
                "subscription_status": tenant_guard_state.get("subscription_status"),
                "blocked_number_count": len(tenant_guard_state.get("ai_blocked_numbers") or []),
                "conversation": (
                    _normalize_conversation_guard(conversation_guard_state)
                ),
            },
        },
        "scenario_ids": scenarios,
        "scenario_index": 0,
        "scenario_results": [],
        "active_scenario": None,
        "totals": {"inbound": 0, "outbound_provider": 0, "llm_calls": 0, "cost_usd": 0.0},
        "mutations_performed_by_runner": False,
    }
    path = _write_session(session, root)
    return {
        "ok": True,
        "session_id": session_id,
        "state": SESSION_STATE_STARTED,
        "tenant_id": tenant_id,
        "test_phone_hmac": session["test_phone_hmac"],
        "event_cursor": session["event_cursor"],
        "scenario_count": len(scenarios),
        "session_path": str(path),
        "messages_sent": 0,
        "tenant_mutations": 0,
    }


def next_scenario(session_id: str, *, app_root: Path | None = None) -> dict[str, Any]:
    session = load_session(session_id, app_root)
    if session["state"] not in {SESSION_STATE_STARTED, SESSION_STATE_SCENARIO_COMPLETED}:
        raise ValueError(CODE_SESSION_STATE_INVALID)
    index = int(session["scenario_index"])
    if index >= len(session["scenario_ids"]):
        session["state"] = SESSION_STATE_COMPLETED
        _write_session(session, app_root)
        return {"ok": True, "state": SESSION_STATE_COMPLETED, "remaining": 0}
    manifest = load_scenario_manifest(app_root)
    by_id = {row["scenario_id"]: row for row in manifest["scenarios"]}
    scenario = by_id[session["scenario_ids"][index]]
    session["active_scenario"] = {
        "scenario_id": scenario["scenario_id"],
        "opened_at_utc": _utc_now(),
        "cursor": session["event_cursor"],
        "machine_observation": None,
        "device_attestation": None,
        "human_assessment": None,
        "outbound_expected": bool(
            scenario["channel_evidence_required"]["outbound_expected"]
        ),
        "send_type": scenario["device_action"]["send_type"],
    }
    session["state"] = SESSION_STATE_AWAITING_DEVICE_SEND
    _write_session(session, app_root)
    return {
        "ok": True,
        "state": session["state"],
        "scenario_id": scenario["scenario_id"],
        "taxonomy": scenario["taxonomy"],
        "instructions": {
            "boundary": "manual_real_test_device_send_required",
            "send_from": "private_allowlisted_whatsapp_test_device",
            "send_type": scenario["device_action"]["send_type"],
            "test_input": scenario["inbound"],
            "outbound_expected": scenario["channel_evidence_required"]["outbound_expected"],
            "preconditions": scenario["preconditions"],
            "expected_deterministic_state": scenario["expected_state"],
            "prohibited_operational_claims": scenario["prohibited_claims"],
            "budgets": {
                "max_llm_calls": scenario["max_llm_calls"],
                "max_tool_calls": scenario["max_tool_calls"],
                "latency_budget_ms": scenario["latency_budget_ms"],
            },
            "cleanup_after_scenario": scenario["cleanup"],
            "do_not": [
                "do_not_post_directly_to_webhook",
                "do_not_call_internal_handler",
                "do_not_insert_database_rows",
            ],
            "after_send": f"observe --session-id {session_id}",
        },
        "archival": "raw test input is not copied into session evidence",
    }


def _metadata(row: Mapping[str, Any]) -> dict[str, Any]:
    value = row.get("metadata") or row.get("extra_metadata") or {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _provider_id_rejection(provider_id: str) -> str | None:
    if not provider_id:
        return CODE_INBOUND_PROVIDER_ID_MISSING
    lowered = provider_id.lower()
    if not _PROVIDER_WAMID_RE.fullmatch(provider_id):
        return CODE_INBOUND_PROVIDER_ID_REJECTED
    if any(marker in lowered for marker in _REJECTED_MARKERS):
        return CODE_INBOUND_PROVIDER_ID_REJECTED
    return None


def classify_inbound_candidate(
    row: Mapping[str, Any],
    *,
    event_cursor: int,
    started_at_utc: str,
    expected_phone_hmac: str,
    hmac_key: str,
) -> dict[str, Any]:
    """Pure classifier. It never returns actual_provider_channel by itself."""
    event_id = int(row.get("id") or 0)
    meta = _metadata(row)
    origin = str(meta.get("message_origin") or "")
    provider_id = str(meta.get("wa_message_id") or "")
    created = _parse_dt(row.get("created_at"))
    started = _parse_dt(started_at_utc)
    phone = str(meta.get("phone") or meta.get("customer_phone") or "")
    phone_hmac = hmac_identifier(re.sub(r"\D", "", phone), key=hmac_key) if phone else ""

    blockers: list[str] = []
    if event_id <= event_cursor or created is None or started is None or created < started:
        blockers.append(CODE_EVENT_CURSOR_STALE)
    if str(row.get("direction") or "").lower() != "inbound":
        blockers.append(CODE_INBOUND_ORIGIN_REJECTED)
    if origin != "live_webhook" or bool(meta.get("historical_import")):
        blockers.append(CODE_INBOUND_ORIGIN_REJECTED)
    fixture_label = str(
        meta.get("acceptance_fixture")
        or meta.get("fixture_label")
        or meta.get("evidence_label")
        or ""
    ).lower()
    if any(marker in fixture_label for marker in _REJECTED_MARKERS):
        blockers.append(CODE_INBOUND_ORIGIN_REJECTED)
    rejected_id = _provider_id_rejection(provider_id)
    if rejected_id:
        blockers.append(rejected_id)
    if phone_hmac != expected_phone_hmac:
        blockers.append(CODE_PHONE_NOT_ALLOWLISTED)

    explicit_probe = str(meta.get("acceptance_evidence_channel") or "").lower()
    if explicit_probe == EVIDENCE_CHANNEL_DIRECT_CODE_PROBE or any(
        marker in provider_id.lower() for marker in ("constitution-smoke", "direct-code")
    ):
        channel = EVIDENCE_CHANNEL_DIRECT_CODE_PROBE
    else:
        # A database row, even with live_webhook + a valid-looking wamid, cannot
        # prove provider delivery. Real-device attestation upgrades it later.
        channel = EVIDENCE_CHANNEL_DIRECT_SIGNED_WEBHOOK
    return {
        "eligible_provider_candidate": not blockers,
        "evidence_channel": channel,
        "blockers": sorted(set(blockers)),
        "event_id": event_id,
        "provider_message_id_hmac": (
            hmac_identifier(provider_id, key=hmac_key) if provider_id else None
        ),
        "normalized_type": (
            (meta.get("normalized_inbound") or {}).get("normalized_type")
            or (meta.get("normalized_inbound") or {}).get("type")
        ),
        "provider_media_id_hmac": _media_id_hmac(meta, hmac_key),
        "media_processing": _media_processing(meta, hmac_key),
    }


def _media_id_hmac(meta: Mapping[str, Any], key: str) -> str | None:
    normalized = meta.get("normalized_inbound") or {}
    if not isinstance(normalized, Mapping):
        return None
    media_id = normalized.get("media_id") or normalized.get("id")
    return hmac_identifier(str(media_id), key=key) if media_id else None


def _media_processing(meta: Mapping[str, Any], key: str) -> dict[str, Any] | None:
    normalized = meta.get("normalized_inbound") or {}
    if not isinstance(normalized, Mapping):
        return None
    return {
        "audio_download_status": normalized.get("audio_download_status"),
        "transcript_status": normalized.get("transcript_status"),
        "transcript_output_hmac": (
            hmac_identifier(str(normalized["transcript_text"]), key=key)
            if normalized.get("transcript_text")
            else None
        ),
        "vision_status": normalized.get("vision_status"),
        "ai_used_audio": bool(normalized.get("ai_used_audio")),
        "ai_used_image": bool(normalized.get("ai_used_image")),
    }


def _outbound_candidate(
    rows: Sequence[Mapping[str, Any]],
    *,
    inbound_id: int,
    hmac_key: str,
) -> dict[str, Any] | None:
    for row in rows:
        if int(row.get("id") or 0) <= inbound_id:
            continue
        if str(row.get("direction") or "").lower() != "outbound":
            continue
        meta = _metadata(row)
        provider_send = meta.get("provider_send") or {}
        if not isinstance(provider_send, Mapping):
            continue
        wamid = str(provider_send.get("wamid") or "")
        if provider_send.get("status") != "sent" or not wamid or _provider_id_rejection(wamid):
            continue
        provenance = {key: meta.get(key) for key in PROVENANCE_FIELDS}
        missing = [key for key in _CORE_PROVENANCE_FIELDS if key not in meta]
        provenance_chain = {
            "decision": {
                "chosen_path": meta.get("chosen_path"),
                "response_mode": meta.get("response_mode"),
                "decision_reason_present": bool(meta.get("decision_reason")),
            },
            "facts": {
                "trusted_fact_surface_present": any(
                    key in meta
                    for key in (
                        "trusted_context",
                        "truth_surface",
                        "operational_evidence",
                        "commerce_evidence",
                    )
                ),
                "metadata_keys": sorted(
                    key for key in meta
                    if "evidence" in str(key) or "truth" in str(key) or "fact" in str(key)
                ),
            },
            "compose": provenance,
            "guards": {
                "final_text_transformed": meta.get("final_text_transformed"),
                "final_transform_reasons": meta.get("final_transform_reasons"),
            },
            "sanitizer": {
                "body_sync_present": bool(meta.get("body_sync_history")),
                "outbound_text_policy_present": bool(meta.get("outbound_text_policy")),
            },
            "dedup": {
                "duplicate_suppressed": bool(meta.get("_nahla_duplicate_suppressed")),
                "operation": provider_send.get("operation"),
            },
            "wire": {
                "provider_status": "sent",
                "provider_message_id_hmac": hmac_identifier(wamid, key=hmac_key),
            },
        }
        return {
            "event_id": int(row["id"]),
            "provider_message_id_hmac": hmac_identifier(wamid, key=hmac_key),
            "provider_status": "sent",
            "provenance": provenance,
            "provenance_chain": provenance_chain,
            "provenance_missing": missing,
            "created_at": str(row.get("created_at") or ""),
        }
    return None


def _query_observation(session: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    engine = _engine()
    phone = re.sub(
        r"\D",
        "",
        os.environ.get(_phone_env_for_tenant(int(session["tenant_id"])), ""),
    )
    with engine.connect() as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                text(
                    "SELECT id,direction,event_type,created_at,metadata "
                    "FROM message_events WHERE tenant_id=:tenant_id AND id>:cursor "
                    "ORDER BY id ASC LIMIT :limit"
                ),
                {
                    "tenant_id": session["tenant_id"],
                    "cursor": session["active_scenario"]["cursor"],
                    "limit": MAX_INBOUND_MESSAGES_PER_SESSION + MAX_OUTBOUND_PROVIDER_CALLS_PER_SESSION,
                },
            ).mappings()
        ]
        usage = dict(
            conn.execute(
                text(
                    "SELECT COUNT(*) AS llm_calls,COALESCE(SUM(total_cost_usd),0) AS cost_usd,"
                    "COALESCE(MAX(id),:cursor) AS max_usage_id "
                    "FROM ai_usage_events WHERE tenant_id=:tenant_id AND id>:cursor"
                ),
                {"tenant_id": session["tenant_id"], "cursor": session["usage_cursor"]},
            ).mappings().one()
        )
        trace_rows = list(
            conn.execute(
                text(
                    "SELECT actions_triggered,latency_ms FROM conversation_traces "
                    "WHERE tenant_id=:tenant_id AND customer_phone=:phone "
                    "AND created_at>=:opened_at ORDER BY id ASC"
                ),
                {
                    "tenant_id": session["tenant_id"],
                    "phone": phone,
                    "opened_at": _parse_dt(session["active_scenario"]["opened_at_utc"]),
                },
            ).mappings()
        )
        tool_calls = 0
        trace_latencies: list[int] = []
        for trace in trace_rows:
            actions = trace.get("actions_triggered")
            if isinstance(actions, list):
                tool_calls += len(actions)
            elif isinstance(actions, Mapping):
                tool_calls += len(actions)
            if trace.get("latency_ms") is not None:
                trace_latencies.append(int(trace["latency_ms"]))
        usage["tool_calls"] = tool_calls
        usage["trace_latency_ms"] = max(trace_latencies) if trace_latencies else None
        conversation = conn.execute(
            text(
                "SELECT id,status,is_human_handoff,paused_by_human,ai_paused,"
                "handoff_active,needs_human FROM conversations "
                "WHERE tenant_id=:tenant_id AND external_id=:phone "
                "ORDER BY id DESC LIMIT 1"
            ),
            {"tenant_id": session["tenant_id"], "phone": phone},
        ).mappings().first()
        recent_orders = list(
            conn.execute(
                text(
                    "SELECT id,status,source,is_abandoned FROM orders "
                    "WHERE tenant_id=:tenant_id AND id>:cursor ORDER BY id ASC LIMIT 20"
                ),
                {
                    "tenant_id": session["tenant_id"],
                    "cursor": int(session.get("order_cursor") or 0),
                },
            ).mappings()
        )
        usage["state_evidence"] = {
            "conversation": dict(conversation) if conversation else None,
            "recent_orders": [dict(order) for order in recent_orders],
            "trace_action_count": tool_calls,
        }
    return rows, usage


def observe(session_id: str, *, app_root: Path | None = None) -> dict[str, Any]:
    session = load_session(session_id, app_root)
    if session["state"] != SESSION_STATE_AWAITING_DEVICE_SEND:
        raise ValueError(CODE_SESSION_STATE_INVALID)
    rows, usage = _query_observation(session)
    key = os.environ.get(EVIDENCE_HMAC_KEY_ENV, "")
    if not key:
        raise ValueError("evidence_hmac_key_missing")

    inbound_candidates = [
        row for row in rows if str(row.get("direction") or "").lower() == "inbound"
    ]
    if not inbound_candidates:
        return {"ok": False, "state": session["state"], "blockers": ["no_new_inbound_event"]}
    inbound_row = inbound_candidates[0]
    inbound = classify_inbound_candidate(
        inbound_row,
        event_cursor=int(session["active_scenario"]["cursor"]),
        started_at_utc=str(session["active_scenario"]["opened_at_utc"]),
        expected_phone_hmac=str(session["test_phone_hmac"]),
        hmac_key=key,
    )
    outbound = _outbound_candidate(rows, inbound_id=inbound["event_id"], hmac_key=key)
    blockers = list(inbound["blockers"])
    if outbound is None and session["active_scenario"].get("outbound_expected", True):
        blockers.append(CODE_OUTBOUND_PROVIDER_ID_MISSING)
    elif outbound["provenance_missing"]:
        blockers.append(CODE_PROVENANCE_INCOMPLETE)

    llm_calls = int(usage.get("llm_calls") or 0)
    cost_usd = float(usage.get("cost_usd") or 0)
    manifest = load_scenario_manifest(app_root)
    scenario = next(
        row for row in manifest["scenarios"]
        if row["scenario_id"] == session["active_scenario"]["scenario_id"]
    )
    if scenario["device_action"]["send_type"] in {"audio", "voice", "image", "video", "document"}:
        if not inbound.get("provider_media_id_hmac"):
            blockers.append("provider_media_id_missing")
    if llm_calls > int(scenario["max_llm_calls"]):
        blockers.append(CODE_RATE_CAP_EXCEEDED)
    if cost_usd > MAX_SESSION_COST_USD:
        blockers.append(CODE_RATE_CAP_EXCEEDED)

    opened = _parse_dt(session["active_scenario"]["opened_at_utc"])
    outbound_dt = _parse_dt((outbound or {}).get("created_at"))
    latency_ms = usage.get("trace_latency_ms")
    if latency_ms is None:
        latency_ms = int((outbound_dt - opened).total_seconds() * 1000) if opened and outbound_dt else None
    if latency_ms is None or latency_ms > int(scenario["latency_budget_ms"]):
        blockers.append("latency_budget_exceeded")

    observation = {
        "machine_verdict": "candidate_pass" if not blockers else "fail",
        "evidence_channel": inbound["evidence_channel"],
        "blockers": sorted(set(blockers)),
        "inbound": inbound,
        "outbound": outbound,
        "budgets": {
            "llm_calls": llm_calls,
            "max_llm_calls": scenario["max_llm_calls"],
            "cost_usd": cost_usd,
            "latency_ms": latency_ms,
            "latency_budget_ms": scenario["latency_budget_ms"],
            "tool_calls": int(usage.get("tool_calls") or 0),
            "max_tool_calls": scenario["max_tool_calls"],
        },
        "prohibited_claims": {
            "declared": scenario["prohibited_claims"],
            "machine_enforcement": "structured_evidence_and_provenance_only",
            "human_truthfulness_attestation_required": True,
        },
        "state_evidence": {
            "expected_contract": scenario["expected_state"],
            "observed": usage.get("state_evidence") or {},
            "machine_scope": (
                "persisted conversation/order/action state; semantic truthfulness "
                "requires closed human rubric"
            ),
        },
    }
    if observation["budgets"]["tool_calls"] > int(scenario["max_tool_calls"]):
        observation["blockers"].append(CODE_RATE_CAP_EXCEEDED)
        observation["machine_verdict"] = "fail"
    projected = {
        "inbound": int(session["totals"]["inbound"]) + 1,
        "outbound_provider": int(session["totals"]["outbound_provider"]) + (1 if outbound else 0),
        "llm_calls": int(session["totals"]["llm_calls"]) + llm_calls,
        "cost_usd": float(session["totals"]["cost_usd"]) + cost_usd,
    }
    if (
        projected["inbound"] > MAX_INBOUND_MESSAGES_PER_SESSION
        or projected["outbound_provider"] > MAX_OUTBOUND_PROVIDER_CALLS_PER_SESSION
        or projected["llm_calls"] > MAX_LLM_CALLS_PER_SESSION
        or projected["cost_usd"] > MAX_SESSION_COST_USD
    ):
        observation["blockers"].append(CODE_RATE_CAP_EXCEEDED)
        observation["machine_verdict"] = "fail"

    session["active_scenario"]["machine_observation"] = observation
    session["state"] = SESSION_STATE_OBSERVED
    session["event_cursor"] = max([int(row["id"]) for row in rows] or [session["event_cursor"]])
    session["usage_cursor"] = int(usage.get("max_usage_id") or session["usage_cursor"])
    session["totals"] = {
        "inbound": projected["inbound"],
        "outbound_provider": projected["outbound_provider"],
        "llm_calls": projected["llm_calls"],
        "cost_usd": round(projected["cost_usd"], 8),
    }
    _write_session(session, app_root)
    return {"ok": not observation["blockers"], "state": session["state"], **observation}


def record_device_attestation(
    session_id: str,
    *,
    provider: str,
    sent_from_private_device: bool,
    outbound_received_on_device: bool,
    media_fixture_sent: bool | None = None,
    app_root: Path | None = None,
) -> dict[str, Any]:
    session = load_session(session_id, app_root)
    if session["state"] != SESSION_STATE_OBSERVED:
        raise ValueError(CODE_SESSION_STATE_INVALID)
    reviewer = (os.environ.get(REVIEWER_ID_ENV) or "").strip()
    key = (os.environ.get(EVIDENCE_HMAC_KEY_ENV) or "").strip()
    if not reviewer or not key:
        raise ValueError("reviewer_attestation_env_missing")
    attestation = {
        "provider": provider,
        "sent_from_private_allowlisted_device": bool(sent_from_private_device),
        "outbound_received_on_same_device": bool(outbound_received_on_device),
        "media_fixture_sent_from_device": media_fixture_sent,
        "reviewer_hmac": hmac_identifier(reviewer, key=key),
        "attested_at_utc": _utc_now(),
    }
    observation = session["active_scenario"]["machine_observation"]
    outbound_requirement_met = (
        outbound_received_on_device
        if session["active_scenario"].get("outbound_expected", True)
        else not outbound_received_on_device
    )
    media_requirement_met = (
        bool(media_fixture_sent)
        if session["active_scenario"].get("send_type") in {"audio", "voice", "image", "video", "document"}
        else True
    )
    if (
        sent_from_private_device
        and outbound_requirement_met
        and media_requirement_met
        and observation["inbound"]["eligible_provider_candidate"]
        and observation["evidence_channel"] == EVIDENCE_CHANNEL_DIRECT_SIGNED_WEBHOOK
    ):
        observation["evidence_channel"] = EVIDENCE_CHANNEL_ACTUAL_PROVIDER
    session["active_scenario"]["device_attestation"] = attestation
    _write_session(session, app_root)
    return {
        "ok": observation["evidence_channel"] == EVIDENCE_CHANNEL_ACTUAL_PROVIDER,
        "evidence_channel": observation["evidence_channel"],
        "blockers": (
            [] if observation["evidence_channel"] == EVIDENCE_CHANNEL_ACTUAL_PROVIDER
            else [CODE_DEVICE_ATTESTATION_REQUIRED]
        ),
    }


def record_human_assessment(
    session_id: str,
    *,
    naturalness: str,
    context_continuity: str,
    audio_quality: str,
    operational_truthfulness: str,
    app_root: Path | None = None,
) -> dict[str, Any]:
    values = {naturalness, context_continuity, audio_quality, operational_truthfulness}
    if not values.issubset(HUMAN_RUBRIC_VALUES):
        raise ValueError("human_rubric_invalid")
    session = load_session(session_id, app_root)
    if session["state"] != SESSION_STATE_OBSERVED:
        raise ValueError(CODE_SESSION_STATE_INVALID)
    reviewer = (os.environ.get(REVIEWER_ID_ENV) or "").strip()
    key = (os.environ.get(EVIDENCE_HMAC_KEY_ENV) or "").strip()
    if not reviewer or not key:
        raise ValueError("reviewer_attestation_env_missing")
    assessment = {
        "rubric": {
            "naturalness": naturalness,
            "context_continuity": context_continuity,
            "audio_quality": audio_quality,
            "operational_truthfulness": operational_truthfulness,
        },
        "reviewer_hmac": hmac_identifier(reviewer, key=key),
        "attested_at_utc": _utc_now(),
        "no_raw_phone_or_customer_pii": True,
    }
    session["active_scenario"]["human_assessment"] = assessment
    session["state"] = SESSION_STATE_HUMAN_ASSESSED
    _write_session(session, app_root)
    rubric_ok = (
        naturalness == "pass"
        and operational_truthfulness == "pass"
        and context_continuity != "fail"
        and audio_quality != "fail"
    )
    return {"ok": rubric_ok, "state": session["state"], "rubric": assessment["rubric"]}


def complete_scenario(session_id: str, *, app_root: Path | None = None) -> dict[str, Any]:
    session = load_session(session_id, app_root)
    if session["state"] != SESSION_STATE_HUMAN_ASSESSED:
        raise ValueError(CODE_HUMAN_ASSESSMENT_REQUIRED)
    active = session["active_scenario"]
    observation = active["machine_observation"]
    assessment = active["human_assessment"]
    blockers = list(observation.get("blockers") or [])
    if observation.get("evidence_channel") != EVIDENCE_CHANNEL_ACTUAL_PROVIDER:
        blockers.append(CODE_REAL_CHANNEL_REQUIRED)
    if not active.get("device_attestation"):
        blockers.append(CODE_DEVICE_ATTESTATION_REQUIRED)
    rubric = assessment["rubric"]
    if (
        rubric.get("naturalness") != "pass"
        or rubric.get("operational_truthfulness") != "pass"
        or rubric.get("context_continuity") == "fail"
        or rubric.get("audio_quality") == "fail"
    ):
        blockers.append("human_assessment_failed")
    verdict = "pass" if not blockers else "fail"
    result = {
        "scenario_id": active["scenario_id"],
        "verdict": verdict,
        "evidence_channel": observation["evidence_channel"],
        "machine_verdict": observation["machine_verdict"],
        "blockers": sorted(set(blockers)),
        "completed_at_utc": _utc_now(),
        "evidence": {
            "inbound": observation["inbound"],
            "outbound": observation["outbound"],
            "budgets": observation["budgets"],
            "human_assessment": assessment,
            "device_attestation": active["device_attestation"],
        },
    }
    session["scenario_results"].append(result)
    session["scenario_index"] += 1
    session["active_scenario"] = None
    session["state"] = SESSION_STATE_SCENARIO_COMPLETED
    _write_session(session, app_root)
    return {"ok": verdict == "pass", **result, "state": session["state"]}


def session_status(session_id: str, *, app_root: Path | None = None) -> dict[str, Any]:
    session = load_session(session_id, app_root)
    return {
        "ok": True,
        "session_id": session_id,
        "state": session["state"],
        "tenant_id": session["tenant_id"],
        "test_phone_hmac": session["test_phone_hmac"],
        "scenario_index": session["scenario_index"],
        "scenario_count": len(session["scenario_ids"]),
        "pass_count": sum(r["verdict"] == "pass" for r in session["scenario_results"]),
        "fail_count": sum(r["verdict"] == "fail" for r in session["scenario_results"]),
        "totals": session["totals"],
        "mutations_performed_by_runner": False,
    }


def emit_defect_bundle(
    session_id: str,
    *,
    app_root: Path | None = None,
) -> dict[str, Any]:
    session = load_session(session_id, app_root)
    failures = [r for r in session["scenario_results"] if r["verdict"] == "fail"]
    if not failures and session.get("active_scenario"):
        active = session["active_scenario"]
        failures = [{
            "scenario_id": active["scenario_id"],
            "verdict": "fail",
            "blockers": (active.get("machine_observation") or {}).get("blockers", ["incomplete"]),
            "evidence_channel": (active.get("machine_observation") or {}).get(
                "evidence_channel", EVIDENCE_CHANNEL_DIRECT_SIGNED_WEBHOOK
            ),
        }]
    bundle = {
        "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
        "session_id": session_id,
        "tenant_id": session["tenant_id"],
        "test_phone_hmac": session["test_phone_hmac"],
        "created_at_utc": _utc_now(),
        "failures": failures,
        "classification": "eval_regression_engineering",
        "auto_merge_fixes": False,
        "constitution_review_required": True,
    }
    root = (app_root or Path(__file__).resolve().parents[2]).resolve()
    out = root / "docs/engineering/staging-evidence/defect-bundles" / f"{session_id}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(bundle, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"ok": True, "bundle_path": str(out.relative_to(root)), "failure_count": len(failures)}


def teardown(session_id: str, *, app_root: Path | None = None) -> dict[str, Any]:
    session = load_session(session_id, app_root)
    key = (os.environ.get(EVIDENCE_HMAC_KEY_ENV) or "").strip()
    if not key:
        raise ValueError("evidence_hmac_key_missing")
    engine = _engine()
    phone = re.sub(
        r"\D",
        "",
        os.environ.get(_phone_env_for_tenant(int(session["tenant_id"])), ""),
    )
    with engine.connect() as conn:
        current_ai = conn.execute(
            text("SELECT ai_settings FROM tenant_settings WHERE tenant_id=:tenant_id"),
            {"tenant_id": session["tenant_id"]},
        ).scalar_one_or_none()
        current_tenant = conn.execute(
            text(
                "SELECT subscription_status,ai_blocked_numbers FROM tenants "
                "WHERE id=:tenant_id"
            ),
            {"tenant_id": session["tenant_id"]},
        ).mappings().one()
        current_conversation = conn.execute(
            text(
                "SELECT status,is_human_handoff,paused_by_human,ai_paused,"
                "handoff_active,needs_human FROM conversations "
                "WHERE tenant_id=:tenant_id AND external_id=:phone "
                "ORDER BY id DESC LIMIT 1"
            ),
            {"tenant_id": session["tenant_id"], "phone": phone},
        ).mappings().first()
    current_exact = {
        "ai_settings": current_ai,
        "tenant_guard_state": dict(current_tenant),
        "conversation_guard_state": _normalize_conversation_guard(current_conversation),
    }
    current_fingerprint = _hmac_value(current_exact, key=key)
    unchanged = hmac.compare_digest(
        current_fingerprint, session["config_snapshot"]["fingerprint"]
    )
    flags_lingering = any(
        env_flag_enabled(os.environ.get(name))
        for name in (MASTER_ENABLE_ENV, EXECUTION_CONFIRM_ENV)
    )
    blockers = [] if unchanged else [CODE_CONFIG_DRIFT]
    if flags_lingering:
        blockers.append("acceptance_flags_lingering")
    all_passed = (
        len(session["scenario_results"]) == len(session["scenario_ids"])
        and all(result["verdict"] == "pass" for result in session["scenario_results"])
    )
    teardown_ok = unchanged and not flags_lingering
    session["state"] = SESSION_STATE_TORN_DOWN if teardown_ok else session["state"]
    session["teardown"] = {
        "verified_at_utc": _utc_now(),
        "config_exact_match": unchanged,
        "runner_flags_lingering": flags_lingering,
        "runner_mutations": False,
        "blockers": blockers,
    }
    _write_session(session, app_root)
    root = (app_root or Path(__file__).resolve().parents[2]).resolve()
    archive = (
        root / "docs/engineering/staging-evidence"
        / f"real-channel-acceptance-{session_id}.json"
    )
    archive.write_text(json.dumps(session, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    pass_artifact = None
    if teardown_ok and all_passed and session["tenant_id"] == TENANT_1_INTENSIVE:
        artifact_payload = {
            "schema_version": "tenant_1_actual_channel_pass_v1",
            "tenant_id": TENANT_1_INTENSIVE,
            "session_id": session_id,
            "verdict": "pass",
            "scenario_count": len(session["scenario_results"]),
            "deployment": session["deployment"],
            "teardown_verified": True,
            "issued_at_utc": _utc_now(),
        }
        artifact_payload["signature"] = _hmac_value(artifact_payload, key=key)
        artifact = root / "docs/engineering/staging-evidence" / f"tenant-1-pass-{session_id}.json"
        artifact.write_text(json.dumps(artifact_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        pass_artifact = str(artifact)
    return {
        "ok": teardown_ok,
        "state": session["state"],
        "config_exact_match": unchanged,
        "blockers": blockers,
        "tenant_1_pass_artifact": pass_artifact,
        "evidence_archive": str(archive),
    }


def _bool_arg(raw: str) -> bool:
    if raw == "yes":
        return True
    if raw == "no":
        return False
    raise argparse.ArgumentTypeError("expected yes|no")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start-session")
    start.add_argument("--tenant", type=int, choices=[1, 33, 48], required=True)
    for command in (
        "next-scenario",
        "observe",
        "poll",
        "complete-scenario",
        "session-status",
        "emit-defect-bundle",
        "teardown",
    ):
        child = sub.add_parser(command)
        child.add_argument("--session-id", required=True)
    device = sub.add_parser("record-device-attestation")
    device.add_argument("--session-id", required=True)
    device.add_argument("--provider", choices=["meta", "360dialog"], required=True)
    device.add_argument("--sent-from-private-device", type=_bool_arg, choices=[True, False], required=True)
    device.add_argument("--outbound-received", type=_bool_arg, choices=[True, False], required=True)
    device.add_argument("--media-fixture-sent", type=_bool_arg, choices=[True, False])
    human = sub.add_parser("record-human-assessment")
    human.add_argument("--session-id", required=True)
    for name in ("naturalness", "context-continuity", "audio-quality", "operational-truthfulness"):
        human.add_argument(f"--{name}", choices=sorted(HUMAN_RUBRIC_VALUES), required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "start-session":
            result = start_session(tenant_id=args.tenant)
        elif args.command == "next-scenario":
            result = next_scenario(args.session_id)
        elif args.command in {"observe", "poll"}:
            result = observe(args.session_id)
        elif args.command == "record-device-attestation":
            result = record_device_attestation(
                args.session_id,
                provider=args.provider,
                sent_from_private_device=args.sent_from_private_device,
                outbound_received_on_device=args.outbound_received,
                media_fixture_sent=args.media_fixture_sent,
            )
        elif args.command == "record-human-assessment":
            result = record_human_assessment(
                args.session_id,
                naturalness=args.naturalness,
                context_continuity=args.context_continuity,
                audio_quality=args.audio_quality,
                operational_truthfulness=args.operational_truthfulness,
            )
        elif args.command == "complete-scenario":
            result = complete_scenario(args.session_id)
        elif args.command == "session-status":
            result = session_status(args.session_id)
        elif args.command == "emit-defect-bundle":
            result = emit_defect_bundle(args.session_id)
        elif args.command == "teardown":
            result = teardown(args.session_id)
        else:
            raise ValueError("command_invalid")
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
        return 0 if result.get("ok") else 2
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "code": str(exc)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
