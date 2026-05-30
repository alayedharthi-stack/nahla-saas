#!/usr/bin/env python3
"""
monitor_active_order_context.py
───────────────────────────────
Read-only Phase A telemetry rollup for observable commerce memory.

Usage (log file from Railway / local capture):
  python scripts/monitor_active_order_context.py --log prod_logs.txt

Usage (DB health — requires DATABASE_URL, optional tenant filter):
  python scripts/monitor_active_order_context.py --db [--tenant-id 33]

Usage (both):
  python scripts/monitor_active_order_context.py --log prod_logs.txt --db

Parses:
  [ACTIVE_ORDER_CONTEXT] persisted write_source=...
  [ACTIVE_ORDER_CONTEXT] telemetry ... active_order_context_source=...
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
DATABASE = ROOT / "database"
for p in (str(ROOT), str(BACKEND), str(DATABASE)):
    if p not in sys.path:
        sys.path.insert(0, p)

_PERSIST_RE = re.compile(
    r"\[ACTIVE_ORDER_CONTEXT\]\s+persisted\s+write_source=(?P<ws>\S+)"
    r".*?order_id=(?P<oid>\S+)"
    r".*?order_status=(?P<os>\S+)"
)
_TELEMETRY_RE = re.compile(
    r"\[ACTIVE_ORDER_CONTEXT\]\s+telemetry\s+tenant=(?P<t>\d+)"
    r".*?active_order_context_source=(?P<src>\S+)"
    r".*?tracking_resolution_mode=(?P<mode>\S+)"
    r".*?order_id=(?P<oid>\S+)"
    r".*?shipping_status=(?P<ss>\S+)"
    r".*?tracking_available=(?P<ta>\S+)"
)


def _parse_log(path: Optional[str]) -> Dict[str, Any]:
    lines: List[str] = []
    if path and path != "-":
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    elif path == "-":
        lines = sys.stdin.read().splitlines()

    persist_ws = Counter()
    persist_status = Counter()
    ctx_source = Counter()
    resolution_mode = Counter()
    tracking_avail = Counter()
    ambiguous: List[str] = []

    for line in lines:
        m = _PERSIST_RE.search(line)
        if m:
            persist_ws[m.group("ws")] += 1
            persist_status[m.group("os")] += 1
            continue
        t = _TELEMETRY_RE.search(line)
        if not t:
            continue
        src = t.group("src")
        mode = t.group("mode")
        ctx_source[src] += 1
        resolution_mode[mode] += 1
        tracking_avail[t.group("ta").lower()] += 1
        # Ambiguous: structured bundle expected but resolution fell back to history.
        if src == "inferred" and mode == "inferred_history":
            ambiguous.append(line.strip()[:240])

    tele_total = sum(ctx_source.values())
    structured = ctx_source.get("structured", 0)
    inferred = ctx_source.get("inferred", 0)
    structured_pct = round(100.0 * structured / tele_total, 1) if tele_total else 0.0

    return {
        "log_lines":           len(lines),
        "persist_writes":      dict(persist_ws),
        "persist_order_status": dict(persist_status),
        "telemetry_total":     tele_total,
        "context_source":      dict(ctx_source),
        "structured_pct":      structured_pct,
        "inferred_pct":        round(100.0 - structured_pct, 1) if tele_total else 0.0,
        "resolution_mode":     dict(resolution_mode),
        "tracking_available":  dict(tracking_avail),
        "ambiguous_samples":   ambiguous[:15],
        "ambiguous_count":     len(ambiguous),
    }


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None


def _db_health(tenant_id: Optional[int], stale_days: int) -> Dict[str, Any]:
    url = os.environ.get("DATABASE_URL", "").replace("postgres://", "postgresql://", 1)
    if not url:
        return {"error": "DATABASE_URL not set"}

    from sqlalchemy import create_engine, text  # noqa: PLC0415

    engine = create_engine(url)
    now = datetime.now(timezone.utc)
    stale_cutoff_days = stale_days

    q = """
        SELECT
            c.id,
            c.tenant_id,
            c.extra_metadata->>'active_order_id' AS active_order_id,
            c.extra_metadata->'active_order_context' AS ctx,
            c.extra_metadata->'recent_order_ids' AS recent_ids,
            c.updated_at
        FROM conversations c
        WHERE c.extra_metadata ? 'active_order_context'
    """
    params: Dict[str, Any] = {}
    if tenant_id is not None:
        q += " AND c.tenant_id = :tid"
        params["tid"] = tenant_id

    rows = engine.connect().execute(text(q), params).fetchall()

    total = len(rows)
    mismatches = 0
    stale = 0
    recent_anomalies = 0
    not_shipped = 0
    samples_stale: List[Dict[str, Any]] = []
    samples_mismatch: List[Dict[str, Any]] = []

    for row in rows:
        conv_id, tid, active_id, ctx, recent, updated_at = row
        ctx = ctx or {}
        recent_list = recent or []
        if not isinstance(recent_list, list):
            recent_list = []

        oid_ctx = str((ctx or {}).get("order_id") or "").strip()
        active_id = str(active_id or "").strip()
        if active_id and oid_ctx and active_id != oid_ctx:
            mismatches += 1
            if len(samples_mismatch) < 10:
                samples_mismatch.append({
                    "conv_id": conv_id, "tenant_id": tid,
                    "active_order_id": active_id, "ctx_order_id": oid_ctx,
                })

        if active_id and recent_list:
            if recent_list[0] != active_id:
                recent_anomalies += 1
            if len(set(recent_list)) != len(recent_list):
                recent_anomalies += 1

        ship = str((ctx or {}).get("shipping_status") or "")
        if ship == "not_shipped":
            not_shipped += 1

        confirmed = _parse_iso((ctx or {}).get("confirmed_at"))
        if confirmed and confirmed.tzinfo is None:
            confirmed = confirmed.replace(tzinfo=timezone.utc)
        age_days = (now - confirmed).days if confirmed else None
        if (
            age_days is not None
            and age_days >= stale_cutoff_days
            and ship == "not_shipped"
            and str((ctx or {}).get("order_status") or "") in (
                "pending_review", "confirmed", "preparing",
            )
        ):
            stale += 1
            if len(samples_stale) < 10:
                samples_stale.append({
                    "conv_id": conv_id,
                    "tenant_id": tid,
                    "order_id": active_id or oid_ctx,
                    "order_status": (ctx or {}).get("order_status"),
                    "age_days": age_days,
                    "product_summary": (ctx or {}).get("product_summary"),
                })

    return {
        "conversations_with_structured_context": total,
        "active_id_ctx_mismatch": mismatches,
        "recent_order_ids_anomalies": recent_anomalies,
        "not_shipped_count": not_shipped,
        "possibly_stale_context": stale,
        "stale_threshold_days": stale_cutoff_days,
        "samples_stale": samples_stale,
        "samples_mismatch": samples_mismatch,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Phase A active_order_context telemetry rollup")
    ap.add_argument("--log", help="Log file path (use '-' for stdin)")
    ap.add_argument("--db", action="store_true", help="Run DB health checks (readonly)")
    ap.add_argument("--tenant-id", type=int, default=None)
    ap.add_argument("--stale-days", type=int, default=7,
                    help="Flag contexts not_shipped older than N days as possibly stale")
    ap.add_argument("--json", action="store_true", help="Emit JSON only")
    args = ap.parse_args()

    out: Dict[str, Any] = {"generated_at": datetime.now(timezone.utc).isoformat()}

    if args.log:
        out["logs"] = _parse_log(args.log)
    if args.db:
        out["db"] = _db_health(args.tenant_id, args.stale_days)

    if not args.log and not args.db:
        ap.error("Provide --log and/or --db")

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    print("=== Active Order Context Monitor (Phase A) ===")
    print(f"Generated: {out['generated_at']}\n")

    if "logs" in out:
        lg = out["logs"]
        print("--- Log telemetry ---")
        print(f"  Lines scanned:        {lg['log_lines']}")
        print(f"  Persist writes:       {lg['persist_writes']}")
        print(f"  Telemetry events:       {lg['telemetry_total']}")
        print(f"  structured:             {lg['context_source'].get('structured', 0)} "
              f"({lg['structured_pct']}%)")
        print(f"  inferred (fallback):    {lg['context_source'].get('inferred', 0)} "
              f"({lg['inferred_pct']}%)")
        print(f"  resolution_mode:        {lg['resolution_mode']}")
        print(f"  tracking_available:     {lg['tracking_available']}")
        print(f"  ambiguous (inferred+history mode): {lg['ambiguous_count']}")
        if lg["ambiguous_samples"]:
            print("  ambiguous samples (first 5):")
            for s in lg["ambiguous_samples"][:5]:
                print(f"    - {s}")
        print()

    if "db" in out:
        db = out["db"]
        if "error" in db:
            print(f"--- DB health --- ERROR: {db['error']}\n")
        else:
            print("--- DB health (readonly) ---")
            print(f"  Conversations w/ context: {db['conversations_with_structured_context']}")
            print(f"  active_id vs ctx mismatch: {db['active_id_ctx_mismatch']}")
            print(f"  recent_order_ids anomalies: {db['recent_order_ids_anomalies']}")
            print(f"  not_shipped (expected pre-A.1): {db['not_shipped_count']}")
            print(f"  possibly stale (>{db['stale_threshold_days']}d, not_shipped): "
                  f"{db['possibly_stale_context']}")
            if db["samples_stale"]:
                print("  stale samples:")
                for s in db["samples_stale"][:5]:
                    print(f"    - {s}")
            if db["samples_mismatch"]:
                print("  mismatch samples:")
                for s in db["samples_mismatch"][:5]:
                    print(f"    - {s}")
            print()


if __name__ == "__main__":
    main()
