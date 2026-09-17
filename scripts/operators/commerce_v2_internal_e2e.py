#!/usr/bin/env python3
"""Operate and score Commerce V2 through the zero-egress INTERNAL_E2E channel."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from evals.commerce_agent_v2_whatsapp.scorer import score_batch  # noqa: E402
from services.commerce_v2_internal_e2e import (  # noqa: E402
    InternalE2EContractError,
    InternalE2ETurnRequest,
    provision_internal_e2e_fixtures,
    reset_internal_e2e_customer,
    submit_internal_customer_turn,
)
from services.commerce_v2_whatsapp_e2e_contract import (  # noqa: E402
    load_corpus,
    render_controlled_test_data,
)
from services.commerce_v2_phase_2_7a_acceptance import (  # noqa: E402
    apply_assertion_review,
    load_acceptance_matrix,
    run_acceptance_matrix,
)


CORPUS_PATH = BACKEND / "evals" / "commerce_agent_v2_whatsapp" / "corpus_v1.json"


def _json_lines(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _write_json_lines(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )


def _database() -> tuple[Any, Any]:
    database_url = str(os.environ.get("DATABASE_URL") or "").strip()
    if not database_url:
        raise InternalE2EContractError("database_url_missing")
    engine = create_engine(database_url, pool_pre_ping=True)
    return engine, sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _corpus(seed: int, *, order_number: str) -> list[dict[str, Any]]:
    turns = load_corpus(CORPUS_PATH, seed=seed)
    turns = render_controlled_test_data(turns, test_order_number=order_number)
    return [
        {
            **turn.to_mapping(),
            "execution_mode": "INTERNAL_E2E",
        }
        for turn in turns
    ]


def command_validate(args: argparse.Namespace) -> int:
    rows = _corpus(args.seed, order_number=args.order_number)
    counts = {alias: sum(row["account_alias"] == alias for row in rows) for alias in "ABC"}
    tiers = {
        tier: sum(row["requested_service_tier"] == tier for row in rows)
        for tier in ("auto", "fast")
    }
    print(
        json.dumps(
            {
                "ok": True,
                "execution_mode": "INTERNAL_E2E",
                "turns": len(rows),
                "accounts": counts,
                "service_tiers": tiers,
            },
            sort_keys=True,
        )
    )
    return 0


def command_schedule(args: argparse.Namespace) -> int:
    rows = _corpus(args.seed, order_number=args.order_number)
    _write_json_lines(args.output, rows)
    print(json.dumps({"ok": True, "turns": len(rows), "output": str(args.output)}))
    return 0


def command_provision(args: argparse.Namespace) -> int:
    engine, SessionLocal = _database()
    db = SessionLocal()
    try:
        fixtures = provision_internal_e2e_fixtures(db, tenant_id=args.tenant_id)
        print(
            json.dumps(
                {
                    "ok": True,
                    "tenant_id": args.tenant_id,
                    "fixtures": {
                        alias: {
                            "alias": fixture.alias,
                            "customer_id": fixture.customer_id,
                            "conversation_id": fixture.conversation_id,
                            "identity": fixture.identity,
                            "order_id": fixture.order_id,
                            "order_number": fixture.order_number,
                        }
                        for alias, fixture in fixtures.items()
                    },
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    finally:
        db.close()
        engine.dispose()


async def _execute_row(
    SessionLocal: Any,
    tenant_id: int,
    row: dict[str, Any],
) -> dict[str, Any]:
    db = SessionLocal()
    try:
        expected = {
            "expected_tools": list(row.get("expected_tools") or []),
            "expected_outcome": row.get("expected_outcome") or "grounded_reply",
            "common_turn": bool(row.get("common_turn", True)),
        }
        return await submit_internal_customer_turn(
            db,
            InternalE2ETurnRequest(
                tenant_id=tenant_id,
                synthetic_customer_alias=row["account_alias"],
                text=row["inbound_text"],
                case_id=row["case_id"],
                service_tier=row["requested_service_tier"],
                expected=expected,
            ),
        )
    finally:
        db.close()


async def _run_rows(
    SessionLocal: Any,
    *,
    tenant_id: int,
    rows: list[dict[str, Any]],
    concurrency_waves: bool,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    if not concurrency_waves:
        for row in rows:
            result = await _execute_row(SessionLocal, tenant_id, row)
            results.append(result)
            if int(result.get("external_egress_count") or 0):
                break
        return results
    waves: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        waves.setdefault(int(row["wave_id"]), []).append(row)
    for wave_id in sorted(waves):
        wave_results = await asyncio.gather(
            *[_execute_row(SessionLocal, tenant_id, row) for row in waves[wave_id]]
        )
        results.extend(wave_results)
        if any(int(result.get("external_egress_count") or 0) for result in wave_results):
            break
    return results


def command_run(args: argparse.Namespace) -> int:
    rows = _json_lines(args.schedule)
    if not rows or any(row.get("execution_mode") != "INTERNAL_E2E" for row in rows):
        raise InternalE2EContractError("internal_e2e_schedule_invalid")
    engine, SessionLocal = _database()
    try:
        results = asyncio.run(
            _run_rows(
                SessionLocal,
                tenant_id=args.tenant_id,
                rows=rows,
                concurrency_waves=args.concurrency_waves,
            )
        )
        _write_json_lines(args.output, results)
        egress = sum(int(row.get("external_egress_count") or 0) for row in results)
        print(
            json.dumps(
                {
                    "ok": len(results) == len(rows) and egress == 0,
                    "turns_requested": len(rows),
                    "turns_completed": len(results),
                    "external_egress_count": egress,
                    "output": str(args.output),
                },
                sort_keys=True,
            )
        )
        return 0 if len(results) == len(rows) and egress == 0 else 1
    finally:
        engine.dispose()


def command_submit(args: argparse.Namespace) -> int:
    engine, SessionLocal = _database()
    db = SessionLocal()
    try:
        artifact = asyncio.run(
            submit_internal_customer_turn(
                db,
                InternalE2ETurnRequest(
                    tenant_id=args.tenant_id,
                    synthetic_customer_alias=args.alias,
                    text=args.text,
                    case_id=args.case_id,
                    service_tier=args.service_tier,
                ),
            )
        )
        print(json.dumps(artifact, ensure_ascii=False, sort_keys=True))
        return 0 if int(artifact.get("external_egress_count") or 0) == 0 else 1
    finally:
        db.close()
        engine.dispose()


def command_reset(args: argparse.Namespace) -> int:
    engine, SessionLocal = _database()
    db = SessionLocal()
    try:
        result = reset_internal_e2e_customer(
            db,
            tenant_id=args.tenant_id,
            synthetic_customer_alias=args.alias,
        )
        print(json.dumps({"ok": True, "alias": args.alias, "deleted": result}, sort_keys=True))
        return 0
    finally:
        db.close()
        engine.dispose()


def command_score(args: argparse.Namespace) -> int:
    corpus = _corpus(args.seed, order_number=args.order_number)
    evidence = _json_lines(args.evidence)
    report = score_batch(corpus, evidence)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "ok": report["hard_gates_passed"],
                "external_egress_total": report["external_egress_total"],
                "output": str(args.output),
            },
            sort_keys=True,
        )
    )
    return 0 if report["hard_gates_passed"] else 1


def command_acceptance(args: argparse.Namespace) -> int:
    """Run the deterministic Phase 2.7A A1–C4 matrix (no seed, no variants)."""
    matrix = load_acceptance_matrix()
    if args.print_matrix:
        print(json.dumps(matrix.to_mapping(), ensure_ascii=False, sort_keys=True))
        return 0
    engine, SessionLocal = _database()
    db = SessionLocal()
    try:
        report = asyncio.run(
            run_acceptance_matrix(
                db,
                matrix=matrix,
                halt_on_first_failure=args.halt_on_first_failure,
            )
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "machine_passed": bool(report["machine_passed"]),
                    "machine_summary": report["machine_summary"],
                    "review_status": report["review_status"],
                    "acceptance_passed": bool(report["acceptance_passed"]),
                    "classification": report["classification"],
                    "turns_executed": report["turns_executed"],
                    "halted_at": report["halted_at"],
                    "external_egress_total": report["external_egress_total"],
                    "run_id": report["run_id"],
                    "output": str(args.output),
                },
                sort_keys=True,
            )
        )
        return 0 if report["machine_passed"] else 1
    finally:
        db.close()
        engine.dispose()


def command_acceptance_review(args: argparse.Namespace) -> int:
    """Record a reviewer verdict on one assertion of an offline acceptance report."""
    report = json.loads(args.report.read_text(encoding="utf-8"))
    report = apply_assertion_review(
        report,
        turn_id=args.turn,
        assertion_index=args.assertion,
        verdict=args.verdict,
        reviewer=args.reviewer,
        evidence=args.evidence,
    )
    output = args.output or args.report
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "review_status": report["review_status"],
                "acceptance_passed": bool(report["acceptance_passed"]),
                "classification": report["classification"],
                "pending": report["review"]["pending"],
                "output": str(output),
            },
            sort_keys=True,
        )
    )
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    sub = root.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate-corpus")
    validate.add_argument("--seed", type=int, default=260914)
    validate.add_argument("--order-number", default="IE2E-C-001")
    validate.set_defaults(func=command_validate)
    schedule = sub.add_parser("schedule")
    schedule.add_argument("--seed", type=int, default=260914)
    schedule.add_argument("--order-number", default="IE2E-C-001")
    schedule.add_argument("--output", type=Path, required=True)
    schedule.set_defaults(func=command_schedule)
    provision = sub.add_parser("provision")
    provision.add_argument("--tenant-id", type=int, default=1)
    provision.set_defaults(func=command_provision)
    submit = sub.add_parser("submit")
    submit.add_argument("--tenant-id", type=int, default=1)
    submit.add_argument("--alias", choices=list("ABC"), required=True)
    submit.add_argument("--text", required=True)
    submit.add_argument("--case-id", default="")
    submit.add_argument("--service-tier", choices=("auto", "fast"), default="auto")
    submit.set_defaults(func=command_submit)
    run = sub.add_parser("run")
    run.add_argument("--tenant-id", type=int, default=1)
    run.add_argument("--schedule", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--concurrency-waves", action="store_true")
    run.set_defaults(func=command_run)
    reset = sub.add_parser("reset")
    reset.add_argument("--tenant-id", type=int, default=1)
    reset.add_argument("--alias", choices=list("ABC"), required=True)
    reset.set_defaults(func=command_reset)
    acceptance = sub.add_parser("acceptance")
    acceptance.add_argument("--output", type=Path, default=Path("/tmp/phase-2-7a-acceptance.json"))
    acceptance.add_argument("--halt-on-first-failure", action="store_true")
    acceptance.add_argument("--print-matrix", action="store_true")
    acceptance.set_defaults(func=command_acceptance)
    review = sub.add_parser("acceptance-review")
    review.add_argument("--report", type=Path, required=True)
    review.add_argument("--turn", required=True)
    review.add_argument("--assertion", type=int, required=True)
    review.add_argument("--verdict", choices=("approved", "rejected"), required=True)
    review.add_argument("--reviewer", required=True)
    review.add_argument("--evidence", required=True)
    review.add_argument("--output", type=Path)
    review.set_defaults(func=command_acceptance_review)
    score = sub.add_parser("score")
    score.add_argument("--seed", type=int, default=260914)
    score.add_argument("--order-number", default="IE2E-C-001")
    score.add_argument("--evidence", type=Path, required=True)
    score.add_argument("--output", type=Path, required=True)
    score.set_defaults(func=command_score)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        return int(args.func(args))
    except (InternalE2EContractError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(
            json.dumps(
                {"ok": False, "blocker": str(exc), "error_class": type(exc).__name__},
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
