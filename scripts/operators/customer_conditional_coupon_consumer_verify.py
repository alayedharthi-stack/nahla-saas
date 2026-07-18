"""Staging consumer sign-off verifier for conditional-coupon compose (closed JSON, fail-closed).

Recovered from the successful staging E2E ``staging_conditional_coupon_consumer_verify.py``
and hardened for repeatable operator launch sign-off. Does not enable persistent runtime
flags, perform real outbound provider calls, or mutate coupon state.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator, Mapping
from unittest.mock import AsyncMock, MagicMock, patch

from scripts.operators import customer_conditional_coupon_shadow_observation as shadow_probe
from scripts.operators.customer_conditional_coupon_consumer_verify_contract import (
    CODE_COMMAND_INVALID,
    CODE_DB_GATE_SKIPPED,
    CODE_PROBE_FAILED,
    COMPOSE_FLAG_ENV,
    FIXTURE_CUSTOMER_PHONE,
    FIXTURE_TENANT_ID,
    GENERIC_STORE_NAME,
    GENERIC_STORE_URL,
    MAX_ORDER_COUNT_QUERIES_PER_SHADOW_TURN,
    MAX_USAGE_EVIDENCE_QUERIES_PER_SHADOW_TURN,
    MESSAGE_ELIGIBLE,
    PHASE_A1_CAPABILITY,
    PHASE_ARTIFACT_PREFLIGHT,
    PHASE_COMPOSE_GENERAL_LLM_SAFE,
    PHASE_COMPOSE_GENERAL_LLM_UNSAFE_GUARD,
    PHASE_COMPOSE_PERSONA_SUCCESS,
    PHASE_DEFAULT_OFF,
    PHASE_PROJECTION_INELIGIBLE,
    PHASE_RUNTIME_REVISION_ATTESTATION,
    PHASE_SHADOW_OBSERVATION,
    PHASE_SUMMARY,
    PHASE_TEARDOWN_FLAGS,
    PHASE_WEBHOOK_DEDUP,
    PINNED_TARGET_RUNTIME_REVISION,
    PINNED_TARGET_RUNTIME_REVISION_SHORT,
    PROBE_DEDUP_AFTER_STUB,
    PROBE_DEDUP_BEFORE_STUB,
    PROBE_DEDUP_SNAPSHOT_ID,
    PROBE_PERSONA_COMPOSE_STUB,
    PROBE_SAFE_GENERAL_LLM_STUB,
    PROBE_UNSAFE_GENERAL_LLM_STUB,
    REPORT_SCHEMA_VERSION,
    SHADOW_FLAG_ENV,
    env_flag_enabled,
    normalize_pinned_revision,
)
from scripts.operators.deployment_revision_attestation_contract import (
    evaluate_runtime_revision_attestation,
)
from scripts.operators.customer_conditional_coupon_shadow_observation import (
    with_app_container_paths,
)


def _report(phase: str, **payload: Any) -> dict[str, Any]:
    return {
        "phase": phase,
        "report_schema_version": REPORT_SCHEMA_VERSION,
        **payload,
    }


def emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def gate_runtime_revision_attestation(
    *,
    target_app_root: Path | None = None,
    pinned_target_revision: str | None = None,
) -> dict[str, Any]:
    try:
        pin = normalize_pinned_revision(pinned_target_revision)
    except ValueError as exc:
        return _report(
            PHASE_RUNTIME_REVISION_ATTESTATION,
            ok=False,
            code=str(exc),
        )

    attestation = evaluate_runtime_revision_attestation(
        pinned_target_revision=pin,
        target_app_root=target_app_root,
    )
    payload = attestation.to_dict()
    for key in ("code", "ok"):
        payload.pop(key, None)
    return _report(
        PHASE_RUNTIME_REVISION_ATTESTATION,
        ok=attestation.ok,
        code=attestation.code,
        **payload,
    )


def gate_artifact_preflight(
    app_root: Path,
    *,
    pinned_source_revision: str | None = None,
) -> dict[str, Any]:
    from services.customer_conditional_coupon_shadow_deployment_artifact_contract import (
        evaluate_observation_window_preflight,
        evaluate_shadow_deployment_artifact,
    )

    try:
        pin = normalize_pinned_revision(pinned_source_revision)
    except ValueError as exc:
        return _report(
            PHASE_ARTIFACT_PREFLIGHT,
            ok=False,
            code=str(exc),
        )

    inv = evaluate_shadow_deployment_artifact(app_root)
    pf = evaluate_observation_window_preflight(
        pinned_source_revision=PINNED_TARGET_RUNTIME_REVISION_SHORT,
        inventory=inv,
        observation_flag_change_requested=True,
    )
    ok = bool(inv.ok and pf.ok and pin)
    return _report(
        PHASE_ARTIFACT_PREFLIGHT,
        ok=ok,
        inventory_ok=inv.ok,
        preflight_ok=pf.ok,
        contract=inv.contract_version,
        pinned_source_revision=pin,
        import_failures=list(inv.inventory.import_failures),
        blockers=list(pf.blockers),
    )


def gate_default_off(app_root: Path) -> dict[str, Any]:
    result = shadow_probe.execute_default_off_probe(app_root=app_root)
    ok = bool(result.get("ok") and result.get("zero_io_contract"))
    return _report(
        PHASE_DEFAULT_OFF,
        ok=ok,
        shadow_enabled=result.get("shadow_enabled"),
        zero_io_contract=result.get("zero_io_contract"),
        facts_count=result.get("facts_count"),
        telemetry=result.get("telemetry"),
    )


def gate_a1_capability(db: Any) -> dict[str, Any]:
    from sqlalchemy import text

    revs = db.execute(text("SELECT version_num FROM alembic_version ORDER BY 1")).fetchall()
    cap = db.execute(
        text(
            """
            SELECT state, validation_revision
            FROM order_customer_identity_capability_state
            LIMIT 1
            """
        )
    ).fetchone()
    dual = {row[0] for row in revs} == {"0088", "0089"}
    validated = cap is not None and cap[0] == "validated" and cap[1] == "0088"
    return _report(
        PHASE_A1_CAPABILITY,
        ok=dual and validated,
        alembic_revisions=[row[0] for row in revs],
        capability_state=cap[0] if cap else None,
        validation_revision=cap[1] if cap else None,
    )


def gate_shadow_observation(db: Any, app_root: Path) -> dict[str, Any]:
    os.environ[SHADOW_FLAG_ENV] = "true"
    try:
        result = shadow_probe.execute_shadow_observation_probe(
            db,
            tenant_id=FIXTURE_TENANT_ID,
            message=MESSAGE_ELIGIBLE,
            app_root=app_root,
        )
    finally:
        os.environ.pop(SHADOW_FLAG_ENV, None)

    telemetry = result.get("telemetry") or {}
    order_queries = int(telemetry.get("order_count_query_count") or 0)
    usage_queries = int(telemetry.get("usage_evidence_query_count") or 0)
    budgets_ok = (
        order_queries <= MAX_ORDER_COUNT_QUERIES_PER_SHADOW_TURN
        and usage_queries <= MAX_USAGE_EVIDENCE_QUERIES_PER_SHADOW_TURN
    )
    ok = bool(
        result.get("ok")
        and result.get("facts_count", 0) >= 1
        and result.get("subject_bridge_outcome") == "resolved"
        and not result.get("guards", {}).get("materialise_for_customer_called")
        and budgets_ok
    )
    return _report(
        PHASE_SHADOW_OBSERVATION,
        ok=ok,
        facts_count=result.get("facts_count"),
        subject_bridge_outcome=result.get("subject_bridge_outcome"),
        guards=result.get("guards"),
        telemetry=telemetry,
        query_budgets_ok=budgets_ok,
    )


def gate_projection_ineligible() -> dict[str, Any]:
    from modules.ai.brain.truth_surface.contract import (
        TrustedContextSnapshot,
        TrustedDomain,
        TrustedFact,
        TruthSource,
    )
    from modules.ai.brain.truth_surface.customer_conditional_coupon_contract import (
        COMPLETENESS_VERIFIED,
        EVALUATION_CONDITION_SHORTFALL,
        IDENTITY_STATUS_RESOLVED,
        MIN_ORDERS_STATE_SHORTFALL,
        build_sanitized_fact_record,
    )
    from modules.ai.brain.truth_surface.customer_conditional_coupon_consumption_gate import (
        maybe_customer_conditional_coupon_compose_facts,
    )

    record = build_sanitized_fact_record(
        identity_status=IDENTITY_STATUS_RESOLVED,
        customer_scope="nahla_internal_customer",
        order_history_completeness=COMPLETENESS_VERIFIED,
        order_history_completeness_source="order_customer_fk_a1_authoritative",
        completed_orders_count=1,
        min_orders_for_eligibility=3,
        orders_shortfall=2,
        min_orders_condition_state=MIN_ORDERS_STATE_SHORTFALL,
        prior_redemption_evidence_state="not_applicable",
        per_customer_usage_policy_state="verified",
        conditional_coupon_evaluation_state=EVALUATION_CONDITION_SHORTFALL,
        closed_reason_code="orders_shortfall",
        allow_min_orders_condition_claim=False,
    )
    snap = TrustedContextSnapshot(
        tenant_id=FIXTURE_TENANT_ID,
        facts=[
            TrustedFact(
                domain=TrustedDomain.CUSTOMER_CONDITIONAL_COUPON,
                key="customer_conditional_coupon:eligibility",
                value=record,
                source=TruthSource.PROMOTION_TABLE,
                path="customer_conditional_coupon_loader.layer0",
            )
        ],
    )
    snap.ensure_snapshot_id()
    os.environ[COMPOSE_FLAG_ENV] = "true"
    try:
        gated = maybe_customer_conditional_coupon_compose_facts(
            message=MESSAGE_ELIGIBLE,
            snapshot=snap,
            tenant_id=FIXTURE_TENANT_ID,
        )
    finally:
        os.environ.pop(COMPOSE_FLAG_ENV, None)

    ok = (
        gated is not None
        and gated.get("allow_min_orders_condition_claim") is False
        and gated.get("conditional_coupon_evaluation_state") == EVALUATION_CONDITION_SHORTFALL
        and bool(gated.get("facts_snapshot_id"))
    )
    return _report(
        PHASE_PROJECTION_INELIGIBLE,
        ok=ok,
        allow_min_orders_condition_claim=(gated or {}).get("allow_min_orders_condition_claim"),
        evaluation=(gated or {}).get("conditional_coupon_evaluation_state"),
        facts_snapshot_id=(gated or {}).get("facts_snapshot_id"),
    )


@contextmanager
def _outbound_suppression_guard() -> Iterator[list[str]]:
    calls: list[str] = []

    def _track(name: str, *_args: Any, **_kwargs: Any) -> MagicMock:
        calls.append(name)
        return MagicMock()

    with patch(
        "services.promotion_engine.materialise_for_customer",
        side_effect=lambda *_a, **_k: _track("materialise_for_customer"),
    ):
        yield calls


def _get_fixture_conversation(db: Any) -> Any:
    from sqlalchemy import text

    from services.customer_conditional_coupon_shadow_fixture_contract import (
        FIXTURE_MARKER_FIELD,
        FIXTURE_NAMESPACE,
    )

    conversation_id = db.execute(
        text(
            """
            SELECT id FROM conversations
            WHERE tenant_id = :tenant_id
              AND metadata ->> :marker_key = :marker_value
            LIMIT 1
            """
        ),
        {
            "tenant_id": FIXTURE_TENANT_ID,
            "marker_key": FIXTURE_MARKER_FIELD,
            "marker_value": FIXTURE_NAMESPACE,
        },
    ).scalar()
    if not conversation_id:
        return None
    from models import Conversation

    return db.get(Conversation, int(conversation_id))


def _run_brain_compose_probe(
    *,
    persona_stub: str | None,
    llm_stub: str | None,
) -> tuple[dict[str, Any], list[str]]:
    from database.session import SessionLocal
    from modules.ai.brain.decision.actions import ACTION_LLM_REPLY
    from modules.ai.brain.pipeline import get_brain
    from modules.ai.brain.truth_surface.trusted_context import clear_trusted_context
    from modules.ai.brain.types import (
        INTENT_GENERAL,
        CommerceFacts,
        Decision,
        Intent,
        MerchantConversationState,
    )

    clear_trusted_context()
    brain = get_brain()
    db = SessionLocal()
    conversation = _get_fixture_conversation(db)
    if conversation is None:
        db.close()
        raise RuntimeError(CODE_DB_GATE_SKIPPED)

    intent = Intent(name=INTENT_GENERAL, confidence=0.92, slots={})
    decision = Decision(action=ACTION_LLM_REPLY, args={})
    state = MerchantConversationState(
        stage="browsing",
        greeted=True,
        customer_goal="general_help",
    )
    facts = CommerceFacts(
        store_name=GENERIC_STORE_NAME,
        store_url=GENERIC_STORE_URL,
        store_url_resolved=True,
        store_url_source="settings",
        has_products=True,
        product_count=3,
        in_stock_count=2,
        orderable=True,
        has_coupons=True,
    )

    stack = ExitStack()
    stack.enter_context(patch("core.billing.has_billing_access", return_value=True))
    stack.enter_context(
        patch(
            "core.wa_usage.check_limit",
            return_value=SimpleNamespace(allowed=True, used_total=0, limit=1000, reason=""),
        )
    )
    stack.enter_context(
        patch(
            "core.ai_disabled_gate.is_ai_disabled_for_conversation",
            return_value=SimpleNamespace(disabled=False, reason=None),
        )
    )
    stack.enter_context(
        patch(
            "modules.ai.brain.postprocess.customer_conditional_coupon_general_llm_evidence_guard."
            "is_customer_conditional_coupon_layer0_enabled",
            return_value=True,
        )
    )
    stack.enter_context(
        patch(
            "modules.ai.brain.truth_surface.coupon_offer_consumption_gate."
            "is_trusted_context_coupon_offer_compose_enabled",
            return_value=False,
        )
    )
    stack.enter_context(patch.object(brain._classifier, "classify", return_value=intent))
    stack.enter_context(patch.object(brain._decision_engine, "decide", return_value=decision))
    stack.enter_context(patch.object(brain._policy_gate, "gate", side_effect=lambda d, _ctx: d))
    stack.enter_context(patch.object(brain._state_store, "load", return_value=state))
    stack.enter_context(patch.object(brain._state_store, "save"))
    stack.enter_context(patch.object(brain._facts_loader, "load", return_value=facts))
    stack.enter_context(patch.object(brain._memory_updater, "update"))

    if persona_stub is not None:
        from modules.ai.brain.persona.customer_conditional_coupon_answer import (
            build_customer_conditional_coupon_answer_event_metadata,
        )
        from modules.ai.brain.persona.facts_bundle import PersonaComposeResult

        async def _persona_compose(**kwargs: Any) -> tuple[str, PersonaComposeResult, dict[str, Any]]:
            compose_facts = dict(kwargs.get("customer_conditional_coupon_facts") or {})
            result = PersonaComposeResult(
                text=persona_stub,
                source="persona_llm",
                surface="customer_conditional_coupon_answer",
                facts_hash="consumer-verify-probe",
                guard_passed=True,
                language="ar",
            )
            event_meta = build_customer_conditional_coupon_answer_event_metadata(
                result,
                tenant_id=int(kwargs.get("tenant_id") or FIXTURE_TENANT_ID),
                compose_facts=compose_facts,
            )
            return persona_stub, result, event_meta

        stack.enter_context(
            patch(
                "modules.ai.brain.persona.customer_conditional_coupon_answer."
                "try_compose_customer_conditional_coupon_answer",
                side_effect=_persona_compose,
            )
        )
    elif llm_stub is not None:
        stack.enter_context(
            patch(
                "modules.ai.brain.persona.customer_conditional_coupon_answer."
                "try_compose_customer_conditional_coupon_answer",
                new_callable=AsyncMock,
                return_value=(None, None, None),
            )
        )
        stack.enter_context(
            patch(
                "modules.ai.brain.compose.responder.DefaultComposer._llm_compose",
                new_callable=AsyncMock,
                return_value=llm_stub,
            )
        )

    os.environ[COMPOSE_FLAG_ENV] = "true"
    outbound_calls: list[str] = []
    try:
        from modules.ai.brain.truth_surface.trusted_context import run_trusted_context_shadow

        with _outbound_suppression_guard() as tracked:
            run_trusted_context_shadow(
                db=db,
                tenant_id=FIXTURE_TENANT_ID,
                customer_phone=FIXTURE_CUSTOMER_PHONE,
                message=MESSAGE_ELIGIBLE,
                conversation=conversation,
                conversation_id=int(conversation.id),
            )
            with stack:
                result = asyncio.run(
                    brain.process(
                        db=db,
                        tenant_id=FIXTURE_TENANT_ID,
                        customer_phone=FIXTURE_CUSTOMER_PHONE,
                        message=MESSAGE_ELIGIBLE,
                        history=[],
                        profile={"preferred_language": "ar"},
                        conversation_id=int(conversation.id),
                        customer_id=int(conversation.customer_id or 0) or None,
                    )
                )
            outbound_calls = list(tracked)
        return result, outbound_calls
    finally:
        os.environ.pop(COMPOSE_FLAG_ENV, None)
        clear_trusted_context()
        db.close()


def _metadata_slice(result: Mapping[str, Any], *keys: str) -> dict[str, Any]:
    return {key: result.get(key) for key in keys}


def gate_compose_persona_success() -> dict[str, Any]:
    result, outbound_calls = _run_brain_compose_probe(
        persona_stub=PROBE_PERSONA_COMPOSE_STUB,
        llm_stub=None,
    )
    metadata = _metadata_slice(
        result,
        "compose_source",
        "chosen_path",
        "response_mode",
        "customer_conditional_coupon_compose_active",
        "facts_snapshot_id",
        "final_customer_text_source",
        "final_text_transformed",
        "llm_candidate_present",
    )
    ok = (
        not outbound_calls
        and result.get("chosen_path") == "customer_conditional_coupon_compose"
        and result.get("customer_conditional_coupon_compose_active") is True
        and bool(result.get("facts_snapshot_id"))
        and result.get("compose_source") == "persona_llm"
    )
    return _report(
        PHASE_COMPOSE_PERSONA_SUCCESS,
        ok=ok,
        metadata=metadata,
        outbound_calls=outbound_calls,
        telemetry=result.get("conditional_coupon_telemetry"),
    )


def gate_compose_general_llm_safe() -> dict[str, Any]:
    result, outbound_calls = _run_brain_compose_probe(
        persona_stub=None,
        llm_stub=PROBE_SAFE_GENERAL_LLM_STUB,
    )
    metadata = _metadata_slice(
        result,
        "compose_source",
        "chosen_path",
        "response_mode",
        "customer_conditional_coupon_general_llm_fallthrough",
        "facts_snapshot_id",
        "final_customer_text_source",
        "final_text_transformed",
        "final_transform_reasons",
        "llm_candidate_present",
    )
    ok = (
        not outbound_calls
        and result.get("chosen_path") == "customer_conditional_coupon_general_llm_fallthrough"
        and bool(result.get("facts_snapshot_id"))
        and result.get("compose_source") == "llm"
    )
    return _report(
        PHASE_COMPOSE_GENERAL_LLM_SAFE,
        ok=ok,
        metadata=metadata,
        outbound_calls=outbound_calls,
    )


def gate_compose_general_llm_unsafe_guard() -> dict[str, Any]:
    result, outbound_calls = _run_brain_compose_probe(
        persona_stub=None,
        llm_stub=PROBE_UNSAFE_GENERAL_LLM_STUB,
    )
    metadata = _metadata_slice(
        result,
        "compose_source",
        "chosen_path",
        "customer_conditional_coupon_general_llm_fallthrough",
        "customer_conditional_coupon_general_llm_guard_rejected",
        "conditional_coupon_guard_failed_reason",
        "final_customer_text_source",
        "final_text_transformed",
        "final_transform_reasons",
        "facts_snapshot_id",
    )
    transform_reasons = list(result.get("final_transform_reasons") or [])
    ok = (
        not outbound_calls
        and result.get("final_customer_text_source") == "guard_rewrite"
        and result.get("final_text_transformed") is True
        and "customer_conditional_coupon_general_llm_evidence_guard" in transform_reasons
        and bool(result.get("conditional_coupon_guard_failed_reason"))
    )
    return _report(
        PHASE_COMPOSE_GENERAL_LLM_UNSAFE_GUARD,
        ok=ok,
        metadata=metadata,
        outbound_calls=outbound_calls,
    )


def gate_webhook_dedup_snapshot_persistence() -> dict[str, Any]:
    from modules.ai.brain.persona.customer_conditional_coupon_provenance import (
        extract_constitutional_metadata,
        note_customer_conditional_coupon_dedup_substitution,
    )

    wire_metadata = {
        "chosen_path": "customer_conditional_coupon_general_llm_fallthrough",
        "customer_conditional_coupon_general_llm_fallthrough": True,
        "compose_source": "llm",
        "response_mode": "customer_conditional_coupon_general_llm",
        "llm_candidate_present": True,
        "final_text_transformed": False,
        "final_transform_reasons": [],
        "final_customer_text_source": "llm",
        "facts_snapshot_id": PROBE_DEDUP_SNAPSHOT_ID,
    }
    note_customer_conditional_coupon_dedup_substitution(
        wire_metadata,
        before=PROBE_DEDUP_BEFORE_STUB,
        after=PROBE_DEDUP_AFTER_STUB,
    )
    saved = extract_constitutional_metadata(wire_metadata)
    transform_reasons = list(wire_metadata.get("final_transform_reasons") or [])
    ok = (
        saved.get("facts_snapshot_id") == PROBE_DEDUP_SNAPSHOT_ID
        and wire_metadata.get("final_customer_text_source") == "dedup_substitution"
        and "chat_dedup_substitution" in transform_reasons
    )
    return _report(
        PHASE_WEBHOOK_DEDUP,
        ok=ok,
        saved_metadata=saved,
    )


def gate_teardown_flags() -> dict[str, Any]:
    flags = {
        "shadow": os.getenv(SHADOW_FLAG_ENV),
        "compose": os.getenv(COMPOSE_FLAG_ENV),
    }
    ok = not any(env_flag_enabled(value) for value in flags.values())
    return _report(PHASE_TEARDOWN_FLAGS, ok=ok, flags=flags)


def teardown_process_flags() -> None:
    os.environ.pop(SHADOW_FLAG_ENV, None)
    os.environ.pop(COMPOSE_FLAG_ENV, None)
    try:
        from modules.ai.brain.truth_surface.trusted_context import clear_trusted_context

        clear_trusted_context()
    except ImportError:  # noqa: silent-ok — teardown must remain best-effort if brain slice unavailable
        pass


def execute_consumer_verify(
    *,
    app_root: Path | None = None,
    target_app_root: Path | None = None,
    db: Any | None = None,
    pinned_source_revision: str | None = None,
    require_db_gates: bool = True,
    require_runtime_attestation: bool = True,
) -> dict[str, Any]:
    """Run all consumer sign-off gates and return a closed JSON summary."""
    try:
        pin = normalize_pinned_revision(pinned_source_revision)
    except ValueError as exc:
        revision_report = _report(
            PHASE_RUNTIME_REVISION_ATTESTATION,
            ok=False,
            code=str(exc),
        )
        return _report(
            PHASE_SUMMARY,
            ok=False,
            results={"runtime_revision_attestation": False},
            gate_reports=[revision_report],
            code=str(exc),
        )

    attestation = evaluate_runtime_revision_attestation(
        pinned_target_revision=pin,
        target_app_root=target_app_root or app_root,
    )
    if require_runtime_attestation and not attestation.ok:
        revision_report = gate_runtime_revision_attestation(
            target_app_root=target_app_root or app_root,
            pinned_target_revision=pin,
        )
        return _report(
            PHASE_SUMMARY,
            ok=False,
            results={"runtime_revision_attestation": False},
            gate_reports=[revision_report],
            code=revision_report.get("code"),
        )

    if attestation.target_app_root:
        root = shadow_probe.resolve_app_root(Path(attestation.target_app_root))
    else:
        root = shadow_probe.resolve_app_root(target_app_root or app_root)
    results: dict[str, bool] = {}
    reports: list[dict[str, Any]] = []

    def _run_gate(name: str, report: dict[str, Any]) -> None:
        reports.append(report)
        results[name] = bool(report.get("ok"))

    try:
        with with_app_container_paths(root):
            if require_runtime_attestation:
                _run_gate(
                    "runtime_revision_attestation",
                    gate_runtime_revision_attestation(
                        target_app_root=root,
                        pinned_target_revision=pin,
                    ),
                )
            _run_gate("artifact", gate_artifact_preflight(root, pinned_source_revision=pin))
            _run_gate("default_off", gate_default_off(root))
            _run_gate("projection_ineligible", gate_projection_ineligible())
            _run_gate("webhook_dedup_snapshot", gate_webhook_dedup_snapshot_persistence())

            if db is not None:
                _run_gate("a1_capability", gate_a1_capability(db))
                _run_gate("shadow", gate_shadow_observation(db, root))
                _run_gate("compose_persona_success", gate_compose_persona_success())
                _run_gate("compose_general_llm_safe", gate_compose_general_llm_safe())
                _run_gate("compose_general_llm_unsafe", gate_compose_general_llm_unsafe_guard())
            elif require_db_gates:
                skip = _report(PHASE_A1_CAPABILITY, ok=False, code=CODE_DB_GATE_SKIPPED)
                reports.append(skip)
                results["a1_capability"] = False
                results["shadow"] = False
                results["compose_persona_success"] = False
                results["compose_general_llm_safe"] = False
                results["compose_general_llm_unsafe"] = False

        teardown_process_flags()
        teardown = gate_teardown_flags()
        reports.append(teardown)
        results["teardown_flags"] = bool(teardown.get("ok"))
    except BaseException:
        teardown_process_flags()
        raise

    summary = _report(
        PHASE_SUMMARY,
        ok=all(results.values()),
        results=results,
        gate_reports=reports,
    )
    return summary


def _parse_cli_args(argv: list[str]) -> tuple[list[str], Path | None]:
    """Return ``(command_tokens, target_app_root)``."""
    tokens = list(argv)
    target_root: Path | None = None
    idx = 0
    while idx < len(tokens):
        if tokens[idx] == "--target-app-root":
            if idx + 1 >= len(tokens):
                raise ValueError("target_app_root_missing")
            target_root = Path(tokens[idx + 1])
            del tokens[idx : idx + 2]
            continue
        idx += 1
    return tokens, target_root


def main(argv: list[str] | None = None) -> int:
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    raw_arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        arguments, target_app_root = _parse_cli_args(raw_arguments)
    except ValueError:
        emit(_report(PHASE_SUMMARY, ok=False, code=CODE_COMMAND_INVALID))
        return 2
    if arguments not in ([], ["verify"]):
        emit(_report(PHASE_SUMMARY, ok=False, code=CODE_COMMAND_INVALID))
        return 2

    preflight = evaluate_runtime_revision_attestation(
        pinned_target_revision=PINNED_TARGET_RUNTIME_REVISION,
        target_app_root=target_app_root,
    )
    if not preflight.ok:
        revision_report = gate_runtime_revision_attestation(
            target_app_root=target_app_root,
            pinned_target_revision=PINNED_TARGET_RUNTIME_REVISION,
        )
        emit(revision_report)
        emit(
            _report(
                PHASE_SUMMARY,
                ok=False,
                results={"runtime_revision_attestation": False},
                code=revision_report.get("code"),
            )
        )
        return 2

    from database.session import SessionLocal

    db = SessionLocal()
    try:
        summary = execute_consumer_verify(db=db, target_app_root=target_app_root)
    except BaseException:
        emit(_report(PHASE_SUMMARY, ok=False, code=CODE_PROBE_FAILED))
        return 2
    finally:
        teardown_process_flags()
        db.close()

    for report in summary.get("gate_reports", []):
        emit(report)
    emit(summary)
    return 0 if summary.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
