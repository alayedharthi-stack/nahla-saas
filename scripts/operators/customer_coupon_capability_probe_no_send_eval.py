"""No-send coupon capability probe eval.

Calls only run_coupon_capability_probe(). Never issues coupons, never sends
WhatsApp, never writes coupon rows. Test utterances live in the test module.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database", REPO_ROOT / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

# Fail closed: this harness must never touch issuance or tenant canary enablement.
os.environ.pop("NAHLA_CUSTOMER_COUPON_CANARY_TENANTS", None)
os.environ["CUSTOMER_COUPON_LIVE_ROUTING"] = "false"
os.environ["CUSTOMER_COUPON_LIVE_ISSUANCE"] = "false"


def _eval_rounds() -> int:
    raw = str(os.environ.get("NAHLA_CUSTOMER_COUPON_PROBE_EVAL_ROUNDS", "1") or "1").strip()
    try:
        value = int(raw)
    except ValueError:
        return 1
    return max(1, value)


def _main() -> int:
    if not str(os.environ.get("OPENAI_API_KEY", "") or "").strip():
        print("PROBE_GATE=OPEN")
        print("OPENAI_API_KEY_PRESENT=no")
        print("PROBE_SAMPLE_SIZE=not measured")
        return 2

    from modules.ai.brain.intent.coupon_capability_probe import run_coupon_capability_probe
    from modules.ai.orchestrator.customer_chat_models import resolve_tiny_customer_chat_model
    from test_customer_coupon_phase2c_canary import (
        NEGATIVE_CLASSIFICATION_EXAMPLES,
        POSITIVE_CLASSIFICATION_EXAMPLES,
    )

    assert "issue_customer_coupon" not in Path(
        sys.modules["modules.ai.brain.intent.coupon_capability_probe"].__file__
    ).read_text(encoding="utf-8")

    positives = list(POSITIVE_CLASSIFICATION_EXAMPLES)
    negatives = list(NEGATIVE_CLASSIFICATION_EXAMPLES)
    rounds = _eval_rounds()
    model = resolve_tiny_customer_chat_model()
    latencies: list[int] = []
    parse_failures = 0
    false_positives: list[str] = []
    false_negatives: list[str] = []
    positive_pass = 0
    negative_pass = 0

    async def _run_one(text: str) -> dict:
        return await run_coupon_capability_probe(text)

    for round_idx in range(1, rounds + 1):
        for text in positives:
            result = asyncio.run(_run_one(text))
            latencies.append(int(result.get("coupon_capability_probe_ms") or 0))
            if not result.get("coupon_capability_parse_ok"):
                parse_failures += 1
            cap = str(result.get("coupon_capability") or "none")
            if cap == "customer_coupon_request" and result.get("coupon_capability_parse_ok"):
                positive_pass += 1
            else:
                false_negatives.append(f"round={round_idx} text={text}")
        for text in negatives:
            result = asyncio.run(_run_one(text))
            latencies.append(int(result.get("coupon_capability_probe_ms") or 0))
            if not result.get("coupon_capability_parse_ok"):
                parse_failures += 1
            cap = str(result.get("coupon_capability") or "none")
            if cap == "none":
                negative_pass += 1
            else:
                false_positives.append(f"round={round_idx} cap={cap} text={text}")

    unique_cases = len(positives) + len(negatives)
    sample = len(latencies)
    ordered = sorted(latencies)
    avg = sum(latencies) / sample if sample else 0
    p95 = ordered[max(0, int(sample * 0.95) - 1)] if sample else 0
    mx = ordered[-1] if ordered else 0
    parse_rate = (parse_failures / sample) if sample else 1.0
    print(f"PROBE_MODEL={model}")
    print(f"EVAL_ROUNDS={rounds}")
    print(f"UNIQUE_CASES={unique_cases}")
    print(f"TOTAL_CLASSIFICATIONS={sample}")
    print(f"POSITIVE_CLASSIFICATIONS={len(positives) * rounds}")
    print(f"NEGATIVE_CLASSIFICATIONS={len(negatives) * rounds}")
    print(f"PROBE_SAMPLE_SIZE={sample}")
    print(f"POSITIVE_COUNT={len(positives)}")
    print(f"NEGATIVE_COUNT={len(negatives)}")
    print(f"POSITIVE_PASS={positive_pass}")
    print(f"NEGATIVE_PASS={negative_pass}")
    print(f"FALSE_POSITIVES={len(false_positives)}")
    print(f"FALSE_NEGATIVES={len(false_negatives)}")
    print(f"PARSE_FAILURES={parse_failures}")
    print(f"PARSE_FAILURE_RATE={parse_rate:.4f}")
    print(f"PROBE_AVG_LATENCY_MS={avg:.1f}")
    print(f"PROBE_P95_LATENCY_MS={p95}")
    print(f"PROBE_MAX_LATENCY_MS={mx}")
    print("CONTEXTUAL_FOLLOWUP_NOT_PROVEN=yes")
    print("FALSE_POSITIVE_EVIDENCE:")
    if false_positives:
        for row in false_positives:
            print(f"  FP={row}")
    else:
        print("  (none)")
    print("FALSE_NEGATIVE_EVIDENCE:")
    if false_negatives:
        for row in false_negatives:
            print(f"  FN={row}")
    else:
        print("  (none)")
    if false_positives:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
