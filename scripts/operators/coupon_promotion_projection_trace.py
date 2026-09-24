#!/usr/bin/env python3
"""Why `[PROMOTION_PROJECTION]` withheld what it withheld, read through the
resolver the runtime actually uses.

Read-only. Nothing is created, assigned, generated, reserved or redeemed, and
no setting is written. It opens one session, runs the platform's own
`resolve_shareable_promotions` with the same arguments the tool passes it, and
reports each candidate against the merchant's own `min_remaining_hours`.

Why it exists rather than a hand-written query: the candidate set is **not**
"the coupons nearest expiry". The resolver takes

    ORDER BY Coupon.id DESC  LIMIT max(limit * 3, limit)

and then filters in Python by current validity, customer binding and a
non-empty code, stopping at `limit`. So the rows a `ORDER BY expires_at LIMIT
20` query returns are a different set, and reasoning about the withheld count
from them answers a different question. This runs the real thing.

It also reports the database's clock and this process's clock separately,
because the tool builds its cutoff from **process** time
(`promotions._now()`) while the expiry it compares against was written by the
database. A skew between them is one of the three causes this can distinguish.

    python -m scripts.operators.coupon_promotion_projection_trace \
        --tenant 1 --customer 66 [--limit 8] [--json]

`DATABASE_URL` is read from the environment and never printed. Coupon codes are
masked; no customer name, phone or address is read or shown.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _entry in (_REPO_ROOT, os.path.join(_REPO_ROOT, "backend"),
               os.path.join(_REPO_ROOT, "database")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)


def _mask(code: Any) -> str:
    """Enough to tell two codes apart in a report, not enough to redeem one."""
    text = str(code or "").strip()
    if not text:
        return "(empty)"
    if len(text) <= 3:
        return f"{text[0]}**"
    return f"{text[:2]}***{text[-1]}"


def _parse(raw: Any) -> Optional[datetime]:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def _session() -> Any:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        raise SystemExit("DATABASE_URL is not set; refusing to guess a target")
    return sessionmaker(bind=create_engine(dsn))()


def _ladder_rung(levels: Any, level_id: str) -> Dict[str, Any]:
    from services.coupon_level_contract import level_entry

    entry = level_entry(levels, level_id)
    return dict(entry) if isinstance(entry, dict) else {}


def trace(db: Any, *, tenant_id: int, customer_id: Optional[int], limit: int) -> Dict[str, Any]:
    from sqlalchemy import text as _sql
    from modules.ai.brain.commerce.promotion_truth import resolve_shareable_promotions
    from modules.ai.commerce_agent_v2.tools.promotions import (
        _expires_before, _min_remaining_hours,
    )
    from services.coupon_generator import _get_ai_policy, _get_levels_config

    process_now = datetime.now(timezone.utc)
    try:
        db_now = _parse(str(db.execute(_sql("SELECT now()")).scalar()))
    except Exception as exc:  # noqa: BLE001 - an unreadable clock is reported, not guessed
        db_now = None
        clock_error = type(exc).__name__
    else:
        clock_error = ""

    policy = _get_ai_policy(db, int(tenant_id)) or {}
    min_hours = _min_remaining_hours(policy)
    cutoff = process_now + timedelta(hours=min_hours) if min_hours > 0 else None

    levels = _get_levels_config(db, int(tenant_id)) or {}
    rungs = {rung: _ladder_rung(levels, rung)
             for rung in ("bronze", "silver", "gold", "vip")}

    truth = resolve_shareable_promotions(db, int(tenant_id), limit=int(limit),
                                         customer_id=customer_id)

    rows: List[Dict[str, Any]] = []
    for fact in list(getattr(truth, "shareable", None) or ()):
        if not isinstance(fact, dict):
            continue
        expires = _parse(fact.get("expires_at"))
        life_left_hours = (round((expires - process_now).total_seconds() / 3600.0, 2)
                           if expires is not None else None)
        withheld = bool(cutoff is not None
                        and fact.get("record_kind") == "coupon"
                        and _expires_before(fact, cutoff))
        rows.append({
            "coupon_id": fact.get("id"),
            "code_masked": _mask(fact.get("code")),
            "record_kind": fact.get("record_kind"),
            "coupon_level": fact.get("coupon_level"),
            "expires_at": fact.get("expires_at"),
            "expiry_readable": expires is not None,
            "life_left_hours": life_left_hours,
            "withheld_expiring_before_store_minimum": withheld,
        })

    withheld_count = sum(1 for r in rows if r["withheld_expiring_before_store_minimum"])
    readable = [r for r in rows if r["expiry_readable"]]
    silver_validity = rungs.get("silver", {}).get("validity_hours")

    # Three causes this can tell apart, and it says so rather than picking one
    # it cannot support.
    causes: List[str] = []
    try:
        structural = (silver_validity is not None and min_hours > 0
                      and int(silver_validity) <= min_hours)
    except (TypeError, ValueError):
        structural = False
    if structural:
        causes.append(
            f"structural: the rung is issued with validity_hours={silver_validity}, "
            f"which is not more than min_remaining_hours={min_hours}, so every code "
            "of this rung is born already too short to be handed out")
    if db_now is not None:
        skew_seconds = round((process_now - db_now).total_seconds(), 1)
        if abs(skew_seconds) > 60:
            causes.append(f"clock skew: process_now - db_now = {skew_seconds}s; the "
                          "cutoff is built from process time and compared against "
                          "database-written expiries")
    if readable and withheld_count == len(readable) and not structural:
        oldest = min(r["life_left_hours"] for r in readable)
        newest = max(r["life_left_hours"] for r in readable)
        causes.append(f"pool age: every readable candidate has between {oldest}h and "
                      f"{newest}h left against a {min_hours}h minimum — the pool was "
                      "filled long enough ago that the whole visible window has aged out")

    return {
        "tenant_id": int(tenant_id),
        "customer_id": customer_id,
        "limit_requested": int(limit),
        "process_now": process_now.isoformat(),
        "db_now": db_now.isoformat() if db_now is not None else None,
        "db_clock_error": clock_error,
        "effective_min_remaining_hours": min_hours,
        "cutoff": cutoff.isoformat() if cutoff is not None else None,
        "ai_policy_allowed_levels": policy.get("allowed_levels"),
        "ladder": {rung: {"enabled": cfg.get("enabled"),
                          "validity_hours": cfg.get("validity_hours"),
                          "allowed_channels": cfg.get("allowed_channels"),
                          "max_uses": cfg.get("max_uses")}
                   for rung, cfg in rungs.items()},
        "resolver_query_run": bool(getattr(truth, "query_run", False)),
        "resolver_candidate_count": getattr(truth, "candidate_count", None),
        "considered": len(rows),
        "withheld_expiring_before_store_minimum": withheld_count,
        "candidates": rows,
        "likely_causes": causes or ["not determined by this trace"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", type=int, required=True)
    parser.add_argument("--customer", type=int, default=None,
                        help="the Customer row id; omit for a store-wide read")
    parser.add_argument("--limit", type=int, default=8,
                        help="what the tool asked the resolver for (default 8)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    db = _session()
    try:
        report = trace(db, tenant_id=args.tenant, customer_id=args.customer,
                       limit=args.limit)
    finally:
        try:
            db.rollback()          # read-only: nothing to commit, ever
            db.close()
        except Exception as exc:  # noqa: BLE001
            # Said out loud rather than swallowed: the report above is already
            # printed and still valid, but an operator should know the session
            # did not close cleanly.
            print(f"[PROJECTION_TRACE] session close failed: {type(exc).__name__}",
                  file=sys.stderr)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print(f"[PROJECTION_TRACE] tenant={report['tenant_id']} "
          f"customer={report['customer_id']} limit={report['limit_requested']}")
    print(f"  process_now = {report['process_now']}")
    print(f"  db_now      = {report['db_now'] or '(unreadable: ' + report['db_clock_error'] + ')'}")
    print(f"  min_remaining_hours = {report['effective_min_remaining_hours']}  "
          f"cutoff = {report['cutoff']}")
    print(f"  allowed_levels = {report['ai_policy_allowed_levels']}")
    for rung, cfg in report["ladder"].items():
        print(f"    {rung:<7} enabled={cfg['enabled']} validity_hours={cfg['validity_hours']} "
              f"allowed_channels={cfg['allowed_channels']}")
    print(f"  considered={report['considered']} "
          f"withheld_expiring_before_store_minimum="
          f"{report['withheld_expiring_before_store_minimum']}")
    for row in report["candidates"]:
        print(f"    id={row['coupon_id']} code={row['code_masked']} "
              f"level={row['coupon_level']} expires_at={row['expires_at']} "
              f"life_left_h={row['life_left_hours']} "
              f"withheld={row['withheld_expiring_before_store_minimum']}")
    print("  likely causes:")
    for cause in report["likely_causes"]:
        print(f"    - {cause}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
