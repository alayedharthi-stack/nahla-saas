"""Run conditional-coupon consumer verify on staging with external-runner attestation."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(REPO / "backend"), str(REPO / "database"), "/app", "/app/backend", "/app/database"]

from database.session import SessionLocal  # noqa: E402
from scripts.operators import customer_conditional_coupon_consumer_verify as consumer_verify  # noqa: E402
from scripts.operators.customer_conditional_coupon_consumer_verify import (  # noqa: E402
    execute_consumer_verify,
)
from scripts.operators.customer_conditional_coupon_consumer_verify_contract import (  # noqa: E402
    COMPOSE_FLAG_ENV,
    PHASE_COMPOSE_GENERAL_LLM_SAFE,
    PHASE_COMPOSE_GENERAL_LLM_UNSAFE_GUARD,
    PHASE_COMPOSE_PERSONA_SUCCESS,
    PHASE_SUMMARY,
)


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    sys.stdout.flush()


def _run_preflight(db) -> int:
    summary = execute_consumer_verify(
        db=db,
        app_root=Path("/app"),
        require_runtime_attestation=False,
    )
    for report in summary.get("gate_reports", []):
        _emit(report)
    _emit(summary)
    return 0 if summary.get("ok") else 2


def _run_compose_window(db) -> int:
    os.environ[COMPOSE_FLAG_ENV] = "true"
    reports: list[dict] = []
    results: dict[str, bool] = {}
    try:
        for name, fn in (
            ("compose_persona_success", consumer_verify.gate_compose_persona_success),
            ("compose_general_llm_safe", consumer_verify.gate_compose_general_llm_safe),
            ("compose_general_llm_unsafe", consumer_verify.gate_compose_general_llm_unsafe_guard),
        ):
            report = fn()
            reports.append(report)
            results[name] = bool(report.get("ok"))
            _emit(report)
    finally:
        os.environ.pop(COMPOSE_FLAG_ENV, None)
    summary = {
        "phase": PHASE_SUMMARY,
        "report_schema_version": "coupon_consumer_verify_v1",
        "ok": all(results.values()),
        "results": results,
        "gate_reports": reports,
        "window": "compose_master_on",
    }
    _emit(summary)
    return 0 if summary["ok"] else 2


def main() -> int:
    mode = "preflight"
    if "--compose-window" in sys.argv:
        mode = "compose_window"
    db = SessionLocal()
    try:
        if mode == "compose_window":
            return _run_compose_window(db)
        return _run_preflight(db)
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
