"""
Layer 3 Human Dialogue Review — runner script.

Usage:
  cd backend && python -m tests.salla_acceptance.run_layer3_dialogue

Requires OPENAI_API_KEY for live Luna compose. Stops with Critical blocker
when the key is absent — no stub or mock scoring.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent.parent
for _p in (_BACKEND, _BACKEND / "tests", _BACKEND.parent / "database"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from core.inbound_dedup import reset_cache  # noqa: E402
from models import Conversation, HandoffSession  # noqa: E402
from tests.commerce_scenario_fixtures import make_scenario_db  # noqa: E402
from tests.salla_acceptance.fixtures import (  # noqa: E402
    PHONE_CUST_A,
    PHONE_CUST_B,
    PHONE_CUST_C,
    PHONE_CUST_D,
    seed_dual_tenant_world,
)
from tests.salla_acceptance.layer2_harness import scenario_world_from_bundle  # noqa: E402
from tests.salla_acceptance.layer3_harness import (  # noqa: E402
    COMPOSE_SPY,
    Layer3BrainRunner,
)
from tests.salla_acceptance.layer3_provider import (  # noqa: E402
    apply_layer3_process_env,
    layer3_blocker_reason,
    openai_key_present,
    resolve_layer3_llm_config,
)
from tests.salla_acceptance.layer3_scoring import (  # noqa: E402
    aggregate_suite_scores,
    rank_sessions,
    recommend_fix_packages,
    score_session,
)
from tests.salla_acceptance.layer3_sessions import (  # noqa: E402
    all_layer3_sessions,
    session_customer_message_total,
)

RESULTS_PATH = _HERE / "LAYER3_ACCEPTANCE_RESULTS.json"
SESSIONS_DIR = _HERE / "LAYER3_SESSIONS"

_CUSTOMER_PHONES = {
    "A": PHONE_CUST_A,
    "B": PHONE_CUST_B,
    "C": PHONE_CUST_C,
    "D": PHONE_CUST_D,
}


def create_fresh_layer3_world():
    """Fresh in-memory scenario DB + dual-tenant fixture for one Layer3 session."""
    db, engine = make_scenario_db()
    world = seed_dual_tenant_world(db)
    return world, db, engine


def dispose_layer3_world(db, engine) -> None:
    try:
        db.close()
    finally:
        engine.dispose()


def reset_layer3_session_isolation(
    world,
    *,
    tenant_key: str,
    customer_key: str,
) -> None:
    """
    Defensive cleanup of handoff/ownership flags within a session world.

    Primary isolation is ``create_fresh_layer3_world()`` — one fresh DB per
    Layer 3 scenario. This helper clears residual handoff state when useful.
    """
    phone = _CUSTOMER_PHONES.get(customer_key)
    if not phone:
        return
    bundle = world.tenant_a if tenant_key == "A" else world.tenant_b
    tenant_id = bundle.tenant_id
    conversation = bundle.conversations.get(customer_key)
    if conversation is None:
        return

    world.db.query(HandoffSession).filter(
        HandoffSession.tenant_id == tenant_id,
        HandoffSession.customer_phone == phone,
    ).delete(synchronize_session=False)

    convo = world.db.query(Conversation).filter_by(id=conversation.id).one()
    convo.is_human_handoff = False
    convo.handoff_active = False
    convo.needs_human = False
    if str(convo.status or "").lower() in {"human", "handoff"}:
        convo.status = "active"
    world.db.add(convo)
    world.db.commit()


def _git_head_sha() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(_BACKEND.parent),
            text=True,
        )
        return out.strip()[:12]
    except Exception:  # noqa: BLE001
        return "unknown"


def _write_blocker_report(reason: str) -> Dict[str, Any]:
    sessions = all_layer3_sessions()
    report: Dict[str, Any] = {
        "base_sha": _git_head_sha(),
        "test_environment": "synthetic_sqlite_layer3_live_llm_blocked",
        "llm_provider_and_model": "openai_compatible/gpt-5.6-luna (required, not executed)",
        "openai_key_present": False,
        "anthropic_key_present": bool(
            os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_API_KEY")
        ),
        "blocker": reason,
        "sessions_total": len(sessions),
        "sessions_executed": 0,
        "customer_messages_total": session_customer_message_total(),
        "customer_messages_executed": 0,
        "tenants_tested": [],
        "critical_failures": ["LAYER3_BLOCKED_NO_OPENAI_KEY"],
        "major_failures": [],
        "minor_failures": [],
        "critical_count": 1,
        "major_count": 0,
        "minor_count": 0,
        "tracking_delivery_accuracy_pct": None,
        "handoff_accuracy_pct": None,
        "dedup_behavior_assessment": "not_executed",
        "conversation_quality_score": None,
        "average_response_latency_ms": None,
        "tools_observed": [],
        "compose_usage_rate": 0.0,
        "telemetry_gaps": ["live_compose_never_ran"],
        "blocking_defects": [reason],
        "recommended_fix_packages": [
            "P0: Provide OPENAI_API_KEY in process env for Layer3 live Luna compose",
        ],
        "ready_for_internal_live_test": False,
        "ready_for_tenant1_pilot": False,
        "recommended_next_action": reason,
        "best_5_sessions": [],
        "worst_5_sessions": [],
        "live_compose_proven": False,
    }
    RESULTS_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def prove_one_live_compose_turn(world) -> Dict[str, Any]:
    """Single-turn smoke: real Luna compose via webhook path."""
    sw = scenario_world_from_bundle(world.db, world.tenant_a, "A")
    runner = Layer3BrainRunner(sw)
    before = COMPOSE_SPY.call_count
    turn = runner.run_turn("مرحبا")
    after = COMPOSE_SPY.call_count
    reply = turn.outbound_reply or turn.raw_composed_reply
    return {
        "provider": turn.compose_provider or "openai_compatible",
        "model": turn.compose_model or "gpt-5.6-luna",
        "reply_len": len(reply or ""),
        "compose_calls": after - before,
        "compose_source": turn.compose_source,
        "brain_called": turn.brain_called,
        "latency_ms": turn.latency_ms,
        # Live proof: non-empty outbound + compose/brain activity.
        # Do not require reply_len > 5 — short Arabic greetings (len==5) are valid.
        "ok": bool(
            (reply or "").strip()
            and ((after > before) or turn.brain_called)
        ),
    }


def _reset_customer_handoff(db, bundle, customer_key: str) -> None:
    """Clear G7 handoff residue so dedup sessions do not inherit human ownership."""
    phone_map = {
        "A": PHONE_CUST_A,
        "B": PHONE_CUST_B,
        "C": PHONE_CUST_C,
        "D": PHONE_CUST_D,
    }
    phone = phone_map.get(customer_key, "")
    convo = bundle.conversations.get(customer_key)
    if convo is not None:
        row = db.query(Conversation).filter_by(id=convo.id).one()
        row.is_human_handoff = False
        row.handoff_active = False
        row.needs_human = False
        if str(row.status or "").lower() == "human":
            row.status = "active"
        db.add(row)
    if phone:
        db.query(HandoffSession).filter_by(
            tenant_id=bundle.tenant_id,
            customer_phone=phone,
            status="active",
        ).delete(synchronize_session=False)
    db.commit()


def _run_dedup_session(world, script) -> List[Any]:
    _reset_customer_handoff(world.db, world.tenant_a, script.customer_key)
    sw = scenario_world_from_bundle(world.db, world.tenant_a, script.customer_key)
    reset_cache()
    runner = Layer3BrainRunner(sw)
    text = script.messages[0]
    first = runner.run_turn(text, provider_msg_id="wamid.l3.dedup.fixed")
    second = runner.run_turn(text, provider_msg_id="wamid.l3.dedup.fixed")
    return [first, second]


def _run_handoff_session(world, script) -> List[Any]:
    sw = scenario_world_from_bundle(world.db, world.tenant_a, script.customer_key)
    customer_phone = _CUSTOMER_PHONES[script.customer_key]
    runner = Layer3BrainRunner(sw)
    turns = []
    for idx, msg in enumerate(script.messages):
        turns.append(runner.run_turn(msg, label=f"turn_{idx+1}"))
        if idx == 0:
            convo = world.db.query(Conversation).filter_by(id=sw.conversation.id).one()
            if not (convo.is_human_handoff or convo.handoff_active):
                hs = HandoffSession(
                    tenant_id=world.tenant_a.tenant_id,
                    customer_phone=customer_phone,
                    status="active",
                    handoff_reason="layer3_test",
                    last_message=msg,
                )
                world.db.add(hs)
                convo.is_human_handoff = True
                convo.handoff_active = True
                convo.needs_human = True
                convo.status = "human"
                world.db.add(convo)
                world.db.commit()
            runner = Layer3BrainRunner(
                sw,
                ownership_state="human_active",
                skip_ai=True,
            )
    return turns


def run_all_sessions(
    *,
    sessions_dir: Optional[Path] = None,
) -> Tuple[List[Any], List[Any]]:
    session_results: List[Dict[str, Any]] = []
    session_scores = []
    out_dir = sessions_dir or SESSIONS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    for script in all_layer3_sessions():
        print(f"  [{script.session_id}] group={script.group} tenant={script.tenant} ...", flush=True)
        world, db, engine = create_fresh_layer3_world()
        try:
            reset_cache()
            reset_layer3_session_isolation(
                world,
                tenant_key=script.tenant,
                customer_key=script.customer_key,
            )
            bundle = world.tenant_a if script.tenant == "A" else world.tenant_b
            if script.expected_checks.get("dedup_steps"):
                turns = _run_dedup_session(world, script)
            elif script.expected_checks.get("handoff_then_no_commerce"):
                turns = _run_handoff_session(world, script)
            else:
                sw = scenario_world_from_bundle(world.db, bundle, script.customer_key)
                runner = Layer3BrainRunner(sw)
                turns = runner.run_thread(script.messages)

            scored = score_session(script, turns, compose_real=True)
            session_scores.append(scored)
            evidence = {
                "session_id": script.session_id,
                "group": script.group,
                "tenant": script.tenant,
                "customer_key": script.customer_key,
                "tester_role": script.tester_role,
                "description": script.description,
                "messages": script.messages,
                "expected_checks": script.expected_checks,
                "score": scored.to_dict(),
                "turns": [t.to_dict() for t in turns],
            }
            session_results.append(evidence)
            (out_dir / f"{script.session_id}.json").write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            if scored.critical_defects:
                print(f"    CRITICAL: {scored.critical_defects}", flush=True)
            elif scored.major_defects:
                print(f"    MAJOR: {scored.major_defects}", flush=True)
            else:
                print(f"    OK ({scored.session_pct}%)", flush=True)
            time.sleep(0.3)
        finally:
            dispose_layer3_world(db, engine)

    return session_results, session_scores


def build_final_report(
    session_results: List[Dict[str, Any]],
    session_scores: List[Any],
    llm_config: Any,
    *,
    live_proof: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    agg = aggregate_suite_scores(session_scores)
    best, worst = rank_sessions(session_scores)
    latencies = [
        t["latency_ms"]
        for sr in session_results
        for t in sr.get("turns", [])
        if t.get("latency_ms")
    ]
    avg_lat = round(sum(latencies) / len(latencies), 1) if latencies else None
    compose_turns = sum(
        1 for sr in session_results for t in sr.get("turns", []) if t.get("compose_invoked")
    )
    total_turns = sum(len(sr.get("turns", [])) for sr in session_results)
    compose_rate = round(compose_turns / total_turns, 3) if total_turns else 0.0
    tools = list(
        dict.fromkeys(
            tool
            for sr in session_results
            for t in sr.get("turns", [])
            for tool in t.get("tools_observed") or []
        )
    )

    crit = agg.get("critical_count", 0)
    gates = {
        "zero_critical": crit == 0,
        "isolation_100": agg.get("isolation_accuracy_pct", 0) >= 100,
        "privacy_100": agg.get("privacy_accuracy_pct", 0) >= 100,
        "product_95": agg.get("product_accuracy_pct", 0) >= 95,
        "context_90": agg.get("context_accuracy_pct", 0) >= 90,
        "knowledge_95": agg.get("knowledge_accuracy_pct", 0) >= 95,
        "quality_85": agg.get("conversation_quality_score", 0) >= 85,
        "tracking_when_available": agg.get("tracking_delivery_accuracy_pct", 0) >= 95,
    }
    ready_internal = all(gates.values())

    return {
        "base_sha": _git_head_sha(),
        "test_environment": "synthetic_sqlite_layer3_live_llm_webhook",
        "llm_provider_and_model": llm_config.to_report_dict()["llm_provider_and_model"],
        "openai_key_present": True,
        "live_compose_proven": bool(live_proof and live_proof.get("ok")),
        "live_compose_proof": live_proof or {},
        "sessions_total": len(session_results),
        "sessions_executed": len(session_results),
        "customer_messages_total": session_customer_message_total(),
        "customer_messages_executed": sum(len(sr.get("messages", [])) for sr in session_results),
        "tenants_tested": ["A", "B"],
        "critical_failures": agg.get("critical_defects", []),
        "major_failures": agg.get("major_defects", []),
        "minor_failures": agg.get("minor_defects", []),
        "critical_count": agg.get("critical_count", 0),
        "major_count": agg.get("major_count", 0),
        "minor_count": agg.get("minor_count", 0),
        "accuracy_metrics": {
            "isolation_accuracy_pct": agg.get("isolation_accuracy_pct"),
            "privacy_accuracy_pct": agg.get("privacy_accuracy_pct"),
            "product_accuracy_pct": agg.get("product_accuracy_pct"),
            "context_accuracy_pct": agg.get("context_accuracy_pct"),
            "knowledge_accuracy_pct": agg.get("knowledge_accuracy_pct"),
            "conversation_quality_score": agg.get("conversation_quality_score"),
            "tracking_delivery_accuracy_pct": agg.get("tracking_delivery_accuracy_pct"),
            "average_session_pct": agg.get("average_session_pct"),
        },
        "gates": gates,
        "tracking_delivery_accuracy_pct": agg.get("tracking_delivery_accuracy_pct"),
        "handoff_accuracy_pct": round(
            agg.get("axis_averages", {}).get("handoff_truth", 0) / 5 * 100, 1
        ),
        "dedup_behavior_assessment": "executed_in_L3-G8-01",
        "average_response_latency_ms": avg_lat,
        "tools_observed": tools,
        "compose_usage_rate": compose_rate,
        "telemetry_gaps": [],
        "blocking_defects": agg.get("critical_defects", []) + agg.get("major_defects", [])[:3],
        "recommended_fix_packages": recommend_fix_packages(session_scores),
        "ready_for_internal_live_test": ready_internal,
        "ready_for_tenant1_pilot": ready_internal and agg.get("major_count", 0) == 0,
        "recommended_next_action": (
            "Proceed to internal live WhatsApp test."
            if ready_internal
            else "Fix blocking defects from Layer3 evidence before live test."
        ),
        "best_5_sessions": best,
        "worst_5_sessions": worst,
        "session_summaries": [
            {"session_id": sr["session_id"], "score_pct": sr["score"]["session_pct"]}
            for sr in session_results
        ],
    }


def main() -> int:
    apply_layer3_process_env()
    print("=== Layer 3 Human Dialogue Review ===", flush=True)

    if not openai_key_present():
        reason = layer3_blocker_reason()
        print(f"CRITICAL BLOCKER: {reason}", flush=True)
        report = _write_blocker_report(reason)
        print(f"Blocker report: {RESULTS_PATH}", flush=True)
        return 2

    llm_config = resolve_layer3_llm_config()
    assert llm_config is not None

    proof_world, proof_db, proof_engine = create_fresh_layer3_world()
    try:
        print("Proving one live Luna compose turn...", flush=True)
        proof = prove_one_live_compose_turn(proof_world)
        print(
            f"  provider={proof['provider']} model={proof['model']} "
            f"reply_len={proof['reply_len']} compose_calls={proof['compose_calls']} "
            f"ok={proof['ok']}",
            flush=True,
        )
        if not proof["ok"]:
            reason = "Live compose proof failed — empty reply or no compose invocation"
            print(f"CRITICAL BLOCKER: {reason}", flush=True)
            _write_blocker_report(reason)
            return 2

        print(f"Running {len(all_layer3_sessions())} sessions...", flush=True)
        session_results, session_scores = run_all_sessions()
        report = build_final_report(session_results, session_scores, llm_config, live_proof=proof)
        RESULTS_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nDone. Report: {RESULTS_PATH}", flush=True)
        print(
            f"Critical={report['critical_count']} Major={report['major_count']} "
            f"ready_internal={report['ready_for_internal_live_test']}",
            flush=True,
        )
        return 0 if report["critical_count"] == 0 else 1
    finally:
        dispose_layer3_world(proof_db, proof_engine)


if __name__ == "__main__":
    raise SystemExit(main())
