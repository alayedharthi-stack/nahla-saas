"""Staging-safe product availability truth guard shadow observation operator.

Deterministic synthetic matrix — no LLM calls, no outbound providers, no customer text
in operator JSON. Safe for CI and recurring staging polling.
"""
from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from scripts.operators.deployment_revision_attestation_contract import (
    evaluate_runtime_revision_attestation,
    normalize_revision_token,
)
from scripts.operators.product_availability_truth_guard_shadow_observation_contract import (
    CODE_COMMAND_INVALID,
    CODE_ENFORCE_MODE_ENABLED,
    CODE_PROBE_FAILED,
    CODE_SHADOW_MODE_NOT_ENABLED,
    ENFORCE_MODE_VALUE,
    FIXTURE_TENANT_A,
    FIXTURE_TENANT_B,
    MAX_ACCEPTABLE_ADDITIONAL_LLM_CALLS,
    MAX_ACCEPTABLE_CUSTOMER_TEXT_CHANGES,
    MAX_ACCEPTABLE_DUPLICATE_INVOCATIONS,
    MAX_ACCEPTABLE_OUTBOUND_PROVIDER_CALLS,
    OBSERVATION_WINDOW_HOURS,
    PHASE_DEFAULT_OFF,
    PHASE_RUNTIME_REVISION_ATTESTATION,
    PHASE_SUMMARY,
    PHASE_SYNTHETIC_MATRIX,
    PHASE_TEARDOWN,
    REPORT_SCHEMA_VERSION,
    SHADOW_MODE_ENV,
    SHADOW_MODE_VALUE,
)


def resolve_app_root(artifact_root: Path | None = None) -> Path:
    root = (artifact_root or Path(__file__).resolve().parents[2]).resolve()
    if (root / "backend").is_dir():
        return root
    if root.name == "backend" and root.parent.is_dir():
        return root.parent
    raise ValueError("artifact_root_invalid")


def app_container_sys_path_entries(app_root: Path | None = None) -> list[str]:
    root = resolve_app_root(app_root)
    return [str(root), str(root / "backend"), str(root / "database")]


@contextmanager
def with_app_container_paths(app_root: Path | None = None) -> Iterator[Path]:
    root = resolve_app_root(app_root)
    entries = app_container_sys_path_entries(root)
    saved = list(sys.path)
    try:
        sys.path[:] = [entry for entry in sys.path if entry not in entries]
        sys.path[:0] = entries
        yield root
    finally:
        sys.path[:] = saved


def _guard_mode() -> str:
    return os.environ.get(SHADOW_MODE_ENV, "off").strip().lower()


def _emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _report(phase: str, **payload: Any) -> dict[str, Any]:
    return {
        "phase": phase,
        "report_schema_version": REPORT_SCHEMA_VERSION,
        **payload,
    }


def _sku(
    pid: int,
    title: str,
    *,
    checkout: bool,
    years: list[str] | None = None,
    family: str = "",
) -> dict[str, Any]:
    from core.product_entity_resolution import family_key_from_title  # noqa: PLC0415

    return {
        "id": pid,
        "title": title,
        "sku": f"SKU-{pid}",
        "external_id": f"ext-{pid}",
        "can_checkout": checkout,
        "in_stock": checkout,
        "years": years or [],
        "weights": [],
        "family_key": family or family_key_from_title(title),
    }


def _ctx(
    *,
    skus: list[dict[str, Any]],
    focus: dict[str, Any] | None = None,
    kb: list[dict[str, Any]] | None = None,
    links: list[dict[str, Any]] | None = None,
    connected: bool = True,
) -> dict[str, Any]:
    return {
        "platform_connected": connected,
        "focus_product": focus,
        "recommended_product_ids": [],
        "catalog_skus": skus,
        "kb_signals": kb or [],
        "product_links": links or [],
    }


def _synthetic_scenarios() -> list[dict[str, Any]]:
    fam = "sport|shoe"
    return [
        {
            "case_id": "catalog_available_positive_claim",
            "tenant_id": FIXTURE_TENANT_A,
            "reply": "متوفر",
            "inbound_text": "حذاء رياضي أبيض",
            "context": _ctx(
                skus=[_sku(1, "حذاء رياضي أبيض", checkout=True)],
                focus={"id": 1, "title": "حذاء رياضي أبيض"},
            ),
            "expect_would_rewrite": False,
        },
        {
            "case_id": "catalog_unavailable_negative_claim",
            "tenant_id": FIXTURE_TENANT_A,
            "reply": "غير متوفر",
            "inbound_text": "عطر ورد 100ml",
            "context": _ctx(
                skus=[_sku(2, "عطر ورد 100ml", checkout=False)],
                focus={"id": 2, "title": "عطر ورد 100ml"},
            ),
            "expect_would_rewrite": False,
        },
        {
            "case_id": "kb_catalog_conflict",
            "tenant_id": FIXTURE_TENANT_A,
            "reply": "غير متوفر",
            "inbound_text": "قميص قطني أزرق",
            "context": _ctx(
                skus=[_sku(3, "قميص قطني أزرق", checkout=False, years=["2025"])],
                focus={"id": 3, "title": "قميص قطني أزرق"},
                kb=[{
                    "section_id": 10,
                    "kind": "quick_update",
                    "avail_polarity": "positive",
                    "primary_year": "2025",
                    "linked_product_ids": [3],
                }],
                links=[{"section_id": 10, "product_id": 3, "source": "manual", "confidence": None}],
            ),
            "expect_would_rewrite": True,
        },
        {
            "case_id": "unknown_entity_positive_claim",
            "tenant_id": FIXTURE_TENANT_A,
            "reply": "متوفر",
            "inbound_text": "",
            "context": _ctx(
                skus=[_sku(4, "حقيبة يد جلدية", checkout=True)],
            ),
            "expect_would_rewrite": True,
        },
        {
            "case_id": "variant_specific_conflict",
            "tenant_id": FIXTURE_TENANT_A,
            "reply": "متوفر",
            "inbound_text": "حذاء رياضي أبيض مقاس 42",
            "context": _ctx(
                skus=[
                    _sku(10, "حذاء رياضي أبيض مقاس 41", checkout=False, family=fam),
                    _sku(11, "حذاء رياضي أبيض مقاس 43", checkout=True, family=fam),
                ],
                focus=None,
            ),
            "expect_would_rewrite": False,
        },
        {
            "case_id": "irrelevant_turn_no_claim",
            "tenant_id": FIXTURE_TENANT_A,
            "reply": "أهلاً، كيف أقدر أساعدك؟",
            "inbound_text": "مرحبا",
            "context": _ctx(
                skus=[_sku(20, "ساعة يد فضية", checkout=True)],
            ),
            "expect_would_rewrite": False,
        },
        {
            "case_id": "tenant_b_isolation",
            "tenant_id": FIXTURE_TENANT_B,
            "reply": "متوفر",
            "inbound_text": "نظارة شمسية سوداء",
            "context": _ctx(
                skus=[_sku(30, "نظارة شمسية سوداء", checkout=True)],
                focus={"id": 30, "title": "نظارة شمسية سوداء"},
            ),
            "expect_would_rewrite": False,
        },
    ]


def execute_default_off_probe(*, app_root: Path | None = None) -> dict[str, Any]:
    with with_app_container_paths(app_root):
        from modules.ai.brain.postprocess.product_availability_truth_guard import (  # noqa: PLC0415
            apply_product_availability_truth_guard,
            product_availability_guard_mode,
        )

        mode = product_availability_guard_mode()
        result = apply_product_availability_truth_guard(
            reply="متوفر",
            availability_context=_ctx(skus=[_sku(99, "حذاء رياضي أبيض", checkout=True)]),
            tenant_id=FIXTURE_TENANT_A,
            invocation_site="probe",
        )
        return _report(
            PHASE_DEFAULT_OFF,
            ok=mode == "off",
            guard_mode=mode,
            action=result.action,
            customer_text_changed=result.reply != "متوفر",
        )


def execute_synthetic_matrix_probe(*, app_root: Path | None = None) -> dict[str, Any]:
    with with_app_container_paths(app_root):
        from modules.ai.brain.postprocess.product_availability_shadow_telemetry import (  # noqa: PLC0415
            aggregate_shadow_observations,
            reset_turn_invocation_scope,
        )
        from modules.ai.brain.postprocess.product_availability_truth_guard import (  # noqa: PLC0415
            apply_product_availability_truth_guard,
        )

        if _guard_mode() != SHADOW_MODE_VALUE:
            return _report(PHASE_SYNTHETIC_MATRIX, ok=False, code=CODE_SHADOW_MODE_NOT_ENABLED)
        if _guard_mode() == ENFORCE_MODE_VALUE:
            return _report(PHASE_SYNTHETIC_MATRIX, ok=False, code=CODE_ENFORCE_MODE_ENABLED)

        observations: list[dict[str, Any]] = []
        case_results: list[dict[str, Any]] = []

        for scenario in _synthetic_scenarios():
            reset_turn_invocation_scope()
            original = scenario["reply"]
            result = apply_product_availability_truth_guard(
                reply=original,
                availability_context=scenario["context"],
                inbound_text=scenario["inbound_text"],
                tenant_id=scenario["tenant_id"],
                conversation_id=scenario["tenant_id"] * 100,
                invocation_site="probe",
                turn_token=str(scenario["case_id"]),
            )
            byte_identical = result.reply == original
            case_ok = (
                byte_identical
                and not result.replaced
                and result.would_rewrite is scenario["expect_would_rewrite"]
            )
            case_results.append({
                "byte_identical": byte_identical,
                "case_id": scenario["case_id"],
                "customer_text_changed": not byte_identical,
                "expect_would_rewrite": scenario["expect_would_rewrite"],
                "guard_action": result.action,
                "ok": case_ok,
                "replaced": result.replaced,
                "tenant_id": scenario["tenant_id"],
                "would_rewrite": result.would_rewrite,
            })
            if result.evidence is not None:
                observations.append({
                    "tenant_id": scenario["tenant_id"],
                    "evidence_state": result.evidence.evidence_state,
                    "guard_action": result.action,
                    "would_rewrite": result.would_rewrite,
                    "reason_code": result.reason or "-",
                    "customer_text_changed": not byte_identical,
                    "additional_llm_calls": 0,
                    "guard_duration_ms": 0,
                    "duplicate_invocation": False,
                })

        metrics = aggregate_shadow_observations(observations)
        matrix_ok = all(row["ok"] for row in case_results)
        safety_ok = (
            metrics["customer_text_changed_count"] <= MAX_ACCEPTABLE_CUSTOMER_TEXT_CHANGES
            and metrics["duplicate_invocation_count"] <= MAX_ACCEPTABLE_DUPLICATE_INVOCATIONS
            and metrics["additional_llm_calls"] <= MAX_ACCEPTABLE_ADDITIONAL_LLM_CALLS
        )
        return _report(
            PHASE_SYNTHETIC_MATRIX,
            ok=matrix_ok and safety_ok,
            case_results=case_results,
            metrics=metrics,
            guards={
                "additional_llm_calls": 0,
                "customer_text_changed_count": metrics["customer_text_changed_count"],
                "duplicate_invocation_count": metrics["duplicate_invocation_count"],
                "outbound_provider_calls": 0,
            },
        )


def gate_runtime_revision_attestation(
    *,
    pinned_target_revision: str,
    target_app_root: Path | None = None,
) -> dict[str, Any]:
    try:
        pin = normalize_revision_token(pinned_target_revision)
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


def build_observation_window(
    *,
    start_utc: datetime | None = None,
) -> dict[str, str]:
    start = start_utc or datetime.now(timezone.utc)
    end = start + timedelta(hours=OBSERVATION_WINDOW_HOURS)
    return {
        "duration_hours": str(OBSERVATION_WINDOW_HOURS),
        "end_utc": end.replace(microsecond=0).isoformat(),
        "start_utc": start.replace(microsecond=0).isoformat(),
    }


def teardown_command() -> str:
    return (
        "railway variables --environment staging "
        f"--set \"{SHADOW_MODE_ENV}=off\" --service nahla-saas"
    )


def execute_full_probe(
    *,
    app_root: Path | None = None,
    pinned_target_revision: str | None = None,
    include_revision_gate: bool = True,
) -> dict[str, Any]:
    phases: list[dict[str, Any]] = []
    default_off = execute_default_off_probe(app_root=app_root)
    phases.append(default_off)

    if include_revision_gate and pinned_target_revision:
        phases.append(
            gate_runtime_revision_attestation(
                pinned_target_revision=pinned_target_revision,
                target_app_root=app_root or resolve_app_root(None),
            )
        )

    os.environ[SHADOW_MODE_ENV] = SHADOW_MODE_VALUE
    try:
        matrix = execute_synthetic_matrix_probe(app_root=app_root)
    finally:
        os.environ.pop(SHADOW_MODE_ENV, None)
    phases.append(matrix)

    ok = bool(matrix.get("ok"))
    if default_off.get("guard_mode") == "off":
        ok = ok and bool(default_off.get("ok"))
    for phase in phases:
        if phase.get("phase") == PHASE_RUNTIME_REVISION_ATTESTATION:
            ok = ok and bool(phase.get("ok"))
    return _report(
        PHASE_SUMMARY,
        ok=ok,
        observation_window=build_observation_window(),
        phases=phases,
        teardown_command=teardown_command(),
        evidence_accumulation_path="docs/engineering/staging-evidence/",
    )


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if arguments == ["default-off"]:
            _emit(execute_default_off_probe())
            return 0
        if arguments == ["matrix"]:
            os.environ[SHADOW_MODE_ENV] = SHADOW_MODE_VALUE
            try:
                _emit(execute_synthetic_matrix_probe())
            finally:
                os.environ.pop(SHADOW_MODE_ENV, None)
            return 0
        if arguments[:1] == ["full-probe"]:
            pin = arguments[1] if len(arguments) > 1 else None
            _emit(
                execute_full_probe(
                    pinned_target_revision=pin,
                    include_revision_gate=bool(pin),
                )
            )
            return 0
        if arguments == ["teardown"]:
            _emit(_report(PHASE_TEARDOWN, ok=True, command=teardown_command()))
            return 0
        raise ValueError(CODE_COMMAND_INVALID)
    except ValueError:
        _emit(_report(PHASE_SUMMARY, ok=False, code=CODE_COMMAND_INVALID))
        return 2
    except BaseException:
        _emit(_report(PHASE_SUMMARY, ok=False, code=CODE_PROBE_FAILED))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
