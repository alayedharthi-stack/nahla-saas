#!/usr/bin/env python3
"""Prepare, observe, and score Work-operated real WhatsApp Commerce V2 runs.

This operator never sends WhatsApp messages itself. Work owns the linked test
devices and sends each scheduled turn through the real customer channel.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from evals.commerce_agent_v2_whatsapp.scorer import score_batch  # noqa: E402
from services.commerce_v2_whatsapp_e2e_contract import (  # noqa: E402
    load_corpus,
    render_controlled_test_data,
    validate_test_owned_accounts,
)
CORPUS_PATH = BACKEND / "evals" / "commerce_agent_v2_whatsapp" / "corpus_v1.json"


def _json_lines(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _write_json_lines(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _corpus(seed: int, *, render: bool) -> list[dict[str, Any]]:
    turns = load_corpus(CORPUS_PATH, seed=seed)
    if render:
        raw_accounts = os.environ.get("COMMERCE_V2_E2E_TEST_ACCOUNTS_JSON", "")
        accounts = json.loads(raw_accounts or "{}")
        validate_test_owned_accounts(accounts)
        turns = render_controlled_test_data(
            turns,
            test_order_number=os.environ.get("COMMERCE_V2_E2E_TEST_ORDER_NUMBER", ""),
        )
    return [turn.to_mapping() for turn in turns]


def _activation_blockers(env: dict[str, str], *, live: bool) -> list[str]:
    blockers: list[str] = []
    outbound = {
        int(value)
        for value in env.get("COMMERCE_AGENT_V2_OUTBOUND_TENANT_IDS", "").split(",")
        if value.strip().isdigit()
    }
    if live:
        tenants = {
            int(value)
            for value in env.get("COMMERCE_AGENT_V2_TENANT_IDS", "").split(",")
            if value.strip().isdigit()
        }
        if env.get("COMMERCE_AGENT_V2_ENABLED", "").lower() != "true":
            blockers.append("commerce_v2_not_enabled")
        if tenants != {1}:
            blockers.append("tenant_allowlist_must_equal_1")
        if outbound != {1}:
            blockers.append("outbound_allowlist_must_equal_1")
        if env.get("COMMERCE_AGENT_V2_SHADOW_ONLY", "true").lower() != "false":
            blockers.append("shadow_only_must_be_false")
        if env.get("COMMERCE_AGENT_V2_KILL_SWITCH", "false").lower() != "false":
            blockers.append("kill_switch_must_be_false")
    elif outbound:
        blockers.append("outbound_must_remain_disabled_during_preparation")
    return blockers


def command_validate(args: argparse.Namespace) -> int:
    corpus = _corpus(args.seed, render=False)
    counts = {alias: sum(row["account_alias"] == alias for row in corpus) for alias in "ABC"}
    tiers = {tier: sum(row["requested_service_tier"] == tier for row in corpus) for tier in ("auto", "fast")}
    print(json.dumps({"valid": True, "turns": len(corpus), "accounts": counts, "tiers": tiers}, sort_keys=True))
    return 0


def command_schedule(args: argparse.Namespace) -> int:
    blockers = _activation_blockers(dict(os.environ), live=args.live)
    if blockers:
        print(json.dumps({"ok": False, "blockers": blockers}, sort_keys=True))
        return 2
    try:
        rows = _corpus(args.seed, render=True)
    except (ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "blocker": str(exc)}, sort_keys=True))
        return 2
    _write_json_lines(args.output, rows)
    print(json.dumps({"ok": True, "turns": len(rows), "output": str(args.output)}, sort_keys=True))
    return 0


def command_observe(args: argparse.Namespace) -> int:
    from services.commerce_v2_whatsapp_e2e_observer import observe_persisted_turn

    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print(json.dumps({"ok": False, "blocker": "database_url_missing"}))
        return 2
    engine = create_engine(database_url, pool_pre_ping=True)
    db = sessionmaker(bind=engine)()
    try:
        if engine.dialect.name == "postgresql":
            db.execute(text("SET TRANSACTION READ ONLY"))
        evidence = observe_persisted_turn(
            db,
            account_alias=args.account,
            case_id=args.case_id,
            inbound_wamid=args.inbound_wamid,
        )
        print(json.dumps(evidence, ensure_ascii=False, sort_keys=True))
        return 0
    finally:
        db.rollback()
        db.close()
        engine.dispose()


def command_snapshot(args: argparse.Namespace) -> int:
    from services.commerce_v2_whatsapp_e2e_observer import capture_controlled_state

    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print(json.dumps({"ok": False, "blocker": "database_url_missing"}))
        return 2
    conversation_ids = [
        int(value) for value in args.conversation_ids.split(",") if value.strip().isdigit()
    ]
    if not conversation_ids:
        print(json.dumps({"ok": False, "blocker": "conversation_ids_required"}))
        return 2
    engine = create_engine(database_url, pool_pre_ping=True)
    db = sessionmaker(bind=engine)()
    try:
        if engine.dialect.name == "postgresql":
            db.execute(text("SET TRANSACTION READ ONLY"))
        snapshot = capture_controlled_state(db, conversation_ids=conversation_ids)
        args.output.write_text(
            json.dumps(snapshot, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(json.dumps({"ok": True, "output": str(args.output)}, sort_keys=True))
        return 0
    finally:
        db.rollback()
        db.close()
        engine.dispose()


def command_compare_state(args: argparse.Namespace) -> int:
    from services.commerce_v2_whatsapp_e2e_observer import compare_controlled_state

    before = json.loads(args.before.read_text(encoding="utf-8"))
    after = json.loads(args.after.read_text(encoding="utf-8"))
    result = compare_controlled_state(before, after)
    print(json.dumps({"ok": not any(result.values()), **result}, sort_keys=True))
    return 0 if not any(result.values()) else 1


def command_score(args: argparse.Namespace) -> int:
    corpus = _corpus(args.seed, render=False)
    evidence = _json_lines(args.evidence)
    report = score_batch(corpus, evidence)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"ok": report["hard_gates_passed"], "output": str(args.output)}, sort_keys=True))
    return 0 if report["hard_gates_passed"] else 1


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    sub = root.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate-corpus")
    validate.add_argument("--seed", type=int, default=260914)
    validate.set_defaults(func=command_validate)
    schedule = sub.add_parser("schedule")
    schedule.add_argument("--seed", type=int, default=260914)
    schedule.add_argument("--output", type=Path, required=True)
    schedule.add_argument("--live", action="store_true")
    schedule.set_defaults(func=command_schedule)
    observe = sub.add_parser("observe")
    observe.add_argument("--account", choices=list("ABC"), required=True)
    observe.add_argument("--case-id", required=True)
    observe.add_argument("--inbound-wamid", required=True)
    observe.set_defaults(func=command_observe)
    snapshot = sub.add_parser("snapshot")
    snapshot.add_argument("--conversation-ids", required=True)
    snapshot.add_argument("--output", type=Path, required=True)
    snapshot.set_defaults(func=command_snapshot)
    compare_state = sub.add_parser("compare-state")
    compare_state.add_argument("--before", type=Path, required=True)
    compare_state.add_argument("--after", type=Path, required=True)
    compare_state.set_defaults(func=command_compare_state)
    score = sub.add_parser("score")
    score.add_argument("--seed", type=int, default=260914)
    score.add_argument("--evidence", type=Path, required=True)
    score.add_argument("--output", type=Path, required=True)
    score.set_defaults(func=command_score)
    return root


def main() -> int:
    args = parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
