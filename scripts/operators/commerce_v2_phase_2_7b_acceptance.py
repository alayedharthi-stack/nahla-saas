#!/usr/bin/env python3
"""Phase 2.7B knowledge acceptance operator.

One deterministic lifecycle: provision an isolated synthetic world, create the
run record, execute K01→K16 against it, read status, record human verdicts, and
clean up with an explicit report.

Every command is scoped to a temporary acceptance database that must prove it
holds no real data first, and no command can reach WhatsApp, a customer or a
Salla mutation: turns run through the internal channel, which has no provider
dispatcher.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve()
_ROOT = _HERE.parents[2]
for _path in (_ROOT, _ROOT / "backend", _ROOT / "database"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from services.commerce_v2_internal_e2e import (  # noqa: E402
    InternalE2ETurnRequest,
    submit_internal_customer_turn,
)
from services.commerce_v2_phase_2_7b_environment import (  # noqa: E402
    cleanup_knowledge_acceptance_environment,
    describe_knowledge_acceptance_environment,
    provision_knowledge_acceptance_environment,
    reset_acceptance_thread,
    verify_acceptance_fixtures,
    verify_case_bindings,
)
from services.commerce_v2_phase_2_7b_faults import knowledge_fault  # noqa: E402
from services.commerce_v2_phase_2_7b_knowledge_acceptance import (  # noqa: E402
    KNOWLEDGE_CONTRACT_VERSION_V2,
    KNOWLEDGE_CONTRACT_VERSIONS,
    KNOWLEDGE_CONTRACT_VERSIONS_WITH_BINDINGS,
    create_knowledge_acceptance_run,
    execute_knowledge_acceptance_run,
    knowledge_run_status,
    load_knowledge_acceptance_matrix,
    record_knowledge_review,
)


def _session() -> tuple[Any, Any]:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    url = os.environ.get("P27B_DATABASE_URL") or os.environ["DATABASE_URL"]
    engine = create_engine(url, pool_pre_ping=True)
    return engine, sessionmaker(bind=engine)()


def _emit(tag: str, payload: Any) -> None:
    print(f"[{tag}] " + json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))


def command_provision(_args: argparse.Namespace) -> int:
    engine, db = _session()
    try:
        environment = provision_knowledge_acceptance_environment(db)
        _emit("P27B_PROVISION", environment)
        _emit(
            "P27B_FIXTURES",
            verify_acceptance_fixtures(db, int(environment["tenant_id"])),
        )
        return 0
    finally:
        db.close()
        engine.dispose()


def command_describe(_args: argparse.Namespace) -> int:
    engine, db = _session()
    try:
        _emit("P27B_ENVIRONMENT", describe_knowledge_acceptance_environment(db))
        return 0
    finally:
        db.close()
        engine.dispose()


def command_create(args: argparse.Namespace) -> int:
    engine, db = _session()
    try:
        environment = describe_knowledge_acceptance_environment(db)
        if not environment.get("provisioned"):
            _emit("P27B_CREATE", {"error": "environment_not_provisioned"})
            return 3
        conversation_id = sorted(environment["conversations"].values())[0]
        meta = create_knowledge_acceptance_run(
            db,
            commit=args.commit,
            conversation_id=int(conversation_id),
            tenant_id=int(environment["tenant_id"]),
            contract_version=args.contract,
        )
        _emit("P27B_CREATE", {key: meta[key] for key in ("run_id", "status", "contract_version", "matrix_sha256", "commit")})
        return 0
    finally:
        db.close()
        engine.dispose()


def command_run(args: argparse.Namespace) -> int:
    engine, db = _session()
    try:
        environment = describe_knowledge_acceptance_environment(db)
        tenant_id = int(environment["tenant_id"])
        conversations = sorted(environment["conversations"].values())
        matrix = load_knowledge_acceptance_matrix(contract_version=args.contract)
        # Prove every fixture the matrix needs resolves through the real
        # lookup before a single case is spent.
        _emit("P27B_FIXTURES", verify_acceptance_fixtures(db, tenant_id))
        if matrix.contract_version in KNOWLEDGE_CONTRACT_VERSIONS_WITH_BINDINGS:
            _emit("P27B_BINDINGS", verify_case_bindings(db, tenant_id, matrix))
        alias = matrix.required_aliases[0]
        conversation_id = int(sorted(environment["conversations"].values())[0])

        async def _turn(session: Any, case: Any, text: str) -> dict[str, Any]:
            request = InternalE2ETurnRequest(
                tenant_id=tenant_id,
                synthetic_customer_alias=alias,
                text=text,
                case_id=f"P27B:{case.case_id}",
                expected={
                    "case_id": case.case_id,
                    "contract_version": matrix.contract_version,
                    "expected_tools": list(case.expected.get("expected_tools") or []),
                    # The internal channel classifies a complete fallback as
                    # expected only when the case itself says so; v3 scores
                    # any other fallback as an unexpected runtime failure.
                    "expected_outcome": str(case.expected.get("expected_outcome") or ""),
                },
                batch_id=f"p27b:{args.run_id}"[:64],
            )
            return await submit_internal_customer_turn(session, request)

        async def submit_case(session: Any, case: Any) -> dict[str, Any]:
            # Reset first: the model reads history from stored MessageEvent
            # rows, so a previous case's turns reach this prompt until they are
            # deleted.  Then replay this case's own anchor turns, so the case
            # stands on the product its binding declares and not on whatever an
            # earlier case happened to surface.
            if getattr(case, "reset_thread_before", False):
                _emit(
                    "P27B_RESET",
                    {
                        "case_id": case.case_id,
                        **reset_acceptance_thread(
                            session,
                            tenant_id=tenant_id,
                            conversation_id=conversation_id,
                        ),
                    },
                )
            for anchor in getattr(case, "anchor_turns", ()) or ():
                await _turn(session, case, anchor)
            fault = str(getattr(case, "knowledge_fault_mode", "") or "")
            if fault:
                with knowledge_fault(fault):
                    return await _turn(session, case, case.input)
            return await _turn(session, case, case.input)

        meta = asyncio.run(
            execute_knowledge_acceptance_run(
                db,
                args.run_id,
                submit_case=submit_case,
                tenant_id=tenant_id,
                matrix=matrix,
            )
        )
        _emit(
            "P27B_RUN",
            {
                key: meta.get(key)
                for key in (
                    "run_id", "status", "machine_passed", "machine_summary",
                    "cases_executed", "cases_machine_failed", "external_egress_total",
                    "review_status", "acceptance_passed", "classification", "machine_digest",
                )
            },
        )
        _emit("P27B_CONVERSATIONS", conversations)
        return 0 if meta.get("machine_passed") else 1
    finally:
        db.close()
        engine.dispose()


def command_status(args: argparse.Namespace) -> int:
    engine, db = _session()
    try:
        meta = knowledge_run_status(db, args.run_id)
        _emit("P27B_STATUS", {key: value for key, value in meta.items() if key != "report"})
        if args.full:
            _emit("P27B_REPORT", meta.get("report"))
        return 0
    finally:
        db.close()
        engine.dispose()


def command_review(args: argparse.Namespace) -> int:
    engine, db = _session()
    try:
        meta = record_knowledge_review(
            db,
            args.run_id,
            case_id=args.case,
            assertion_index=int(args.assertion),
            verdict=args.verdict,
            reviewer=args.reviewer,
            evidence=args.evidence,
        )
        _emit(
            "P27B_REVIEW",
            {
                key: meta.get(key)
                for key in ("run_id", "review", "review_status", "acceptance_passed", "classification")
            },
        )
        return 0
    finally:
        db.close()
        engine.dispose()


def command_cleanup(_args: argparse.Namespace) -> int:
    engine, db = _session()
    try:
        _emit("P27B_CLEANUP", cleanup_knowledge_acceptance_environment(db))
        return 0
    finally:
        db.close()
        engine.dispose()


def build_parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Phase 2.7B knowledge acceptance operator")
    sub = root.add_subparsers(dest="command", required=True)

    sub.add_parser("provision").set_defaults(func=command_provision)
    sub.add_parser("describe").set_defaults(func=command_describe)

    create = sub.add_parser("create")
    create.add_argument("--commit", required=True)
    create.add_argument(
        "--contract", default=KNOWLEDGE_CONTRACT_VERSION_V2, choices=KNOWLEDGE_CONTRACT_VERSIONS
    )
    create.set_defaults(func=command_create)

    run = sub.add_parser("run")
    run.add_argument("--run-id", dest="run_id", required=True)
    run.add_argument(
        "--contract", default=KNOWLEDGE_CONTRACT_VERSION_V2, choices=KNOWLEDGE_CONTRACT_VERSIONS
    )
    run.set_defaults(func=command_run)

    status = sub.add_parser("status")
    status.add_argument("--run-id", dest="run_id", required=True)
    status.add_argument("--full", action="store_true")
    status.set_defaults(func=command_status)

    review = sub.add_parser("review")
    review.add_argument("--run-id", dest="run_id", required=True)
    review.add_argument("--case", required=True)
    review.add_argument("--assertion", required=True)
    review.add_argument("--verdict", choices=("approved", "rejected"), required=True)
    review.add_argument("--reviewer", required=True)
    review.add_argument("--evidence", required=True)
    review.set_defaults(func=command_review)

    sub.add_parser("cleanup").set_defaults(func=command_cleanup)
    return root


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
