"""One-shot, scoped, read-only diagnostic; never starts the application."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from sqlalchemy.engine import make_url

SNAPSHOT = "1f1bbe5381bd129595e27229853934ff39ce956e"
CONFIRMATION = "READ_ONLY_TENANT_1_CONVERSATION_9_1F1BBE53"
ROOT = Path(__file__).resolve().parents[2]


def emit(value):
    print("SCOPED_COUPON_TRACE=" + json.dumps(value, ensure_ascii=False), flush=True)


def select(value, keys):
    return {key: value[key] for key in keys if key in value} if isinstance(value, dict) else value


def main():
    if os.environ.get("NAHLA_COUPON_TRACE_CONFIRM") != CONFIRMATION:
        emit({"status": "idle", "diagnostic_snapshot": SNAPSHOT})
        return 0
    try:
        raw = os.environ.get("DATABASE_URL", "")
        parsed = make_url(raw.replace("postgres://", "postgresql://", 1))
        if (parsed.host, parsed.port or 5432, parsed.database) != (
            "postgres-ancu.railway.internal", 5432, "railway"
        ):
            emit({"status": "target_refused"})
            return 1
        env = dict(os.environ)
        env.update({
            "NAHLA_TRACE_TENANT_ID": "1",
            "NAHLA_TRACE_CONVERSATION_ID": "9",
            "PGOPTIONS": "-c default_transaction_read_only=on -c statement_timeout=15000",
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        env.pop("NAHLA_TRACE_CUSTOMER_ID", None)
        env.pop("NAHLA_TRACE_PHONE", None)
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts/operators/coupon_eligibility_trace.py")],
            env=env, cwd=ROOT, capture_output=True, text=True, timeout=180,
        )
        # Raw stderr/SQL errors are never logged: they may contain query values.
        if proc.returncode:
            emit({"status": "trace_failed", "exit_code": proc.returncode})
            return 1
        lines = [line for line in proc.stdout.splitlines() if line.startswith("COUPON_TRACE=")]
        out = json.loads(lines[-1].split("=", 1)[1])
        if out.get("read_only") != "on" or out.get("tenant_id") != 1:
            emit({"status": "scope_refused"})
            return 1
        out["diagnostic_snapshot"] = SNAPSHOT
        out["projection_is"] = "candidate_code_on_current_production_data_not_historical_turn_replay"
        out["target"] = "postgres-ancu.railway.internal:5432/railway"
        # The source trace is read-only; further narrow the material put in logs.
        out.get("projection", {}).pop("detail", None)
        out.get("projection", {}).pop("kept_codes_masked", None)
        for row in out.get("candidate_coupons", []):
            row.pop("code_masked", None)
            row.pop("code_len", None)
        policy = out.get("merchant_policy", {})
        policy["ai_policy"] = select(policy.get("ai_policy"), (
            "enabled", "allowed_levels", "min_remaining_hours", "pool_mode", "unreadable",
        ))
        policy["first_purchase_rule"] = select(policy.get("first_purchase_rule"), (
            "enabled", "discount_type", "discount_value", "validity_days", "max_uses", "min_order_amount",
        ))
        emit(out)
        return 0
    except Exception as exc:
        emit({"status": "operator_failed", "error_type": type(exc).__name__})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
