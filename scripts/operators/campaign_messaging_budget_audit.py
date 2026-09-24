#!/usr/bin/env python3
"""Read-only audit of the shared Meta messaging budget at one instant.

Answers, from the database alone, why the campaign budget guard counted
``used_24h`` recipients at ``--at``:

* the connection's stored tier (``meta_messaging_limit``), when it was read,
  and the messaging scope key the guard used;
* the window (``since = at - 24h``);
* per source — ledger attempts vs. send-log rows — the unique phones, the
  oldest and newest timestamp that put them in the window, the
  intersection and the union, broken down by tenant and campaign;
* for the union: how many of those recipients have delivery evidence and
  how many only a post-accept failure (Meta says delivered-to users count;
  a failed-after-accept send reached no one);
* ``sent_at`` sanity: rows whose ``sent_at`` is later than the campaign's
  last attempt/claim or inside the audit hour (a rewrite would show here);
* the same numbers under the current rule (``messaging_usage``) for
  comparison.

``legacy_rule`` reproduces the formula that ran before this change
(attempts in BUDGET_STATES by scope ∪ send-logs of the scope's tenants
with ``sent_at >= since`` or a recent uncertain/sending row). With
``--expect-used N`` the exit code is non-zero when the legacy union does
not equal ``N`` — i.e. the guard's number is not what the data says.

One ``REPEATABLE READ, READ ONLY`` transaction; recipients never printed
(counts and timestamps only); no token or secret column is selected.

  DATABASE_URL=… python scripts/operators/campaign_messaging_budget_audit.py \\
      --tenant-id 33 --at 2026-09-24T09:53:49Z --expect-used 1483
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]
for _p in (ROOT / "backend", ROOT / "database"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _iso(v: Any) -> Optional[str]:
    return v.isoformat() if hasattr(v, "isoformat") else v


def _naive(v: datetime) -> datetime:
    return v.astimezone(timezone.utc).replace(tzinfo=None) if v.tzinfo else v


def _span(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    ts = [r["ts"] for r in rows if r.get("ts") is not None]
    return {"oldest": _iso(min(ts)) if ts else None, "newest": _iso(max(ts)) if ts else None}


def audit(url: str, *, tenant_id: int, at: datetime) -> Dict[str, Any]:
    from sqlalchemy import create_engine, text  # noqa: PLC0415
    from sqlalchemy.orm import Session  # noqa: PLC0415
    from services import campaign_send_ledger as ledger  # noqa: PLC0415

    at = _naive(at)
    since = at - ledger.MESSAGING_WINDOW
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            db = Session(bind=conn)
            c = conn.execute(text(
                "SELECT id, tenant_id, status, business_manager_id, meta_business_account_id, "
                "whatsapp_business_account_id, phone_number_id, meta_messaging_limit, "
                "meta_tier_updated_at FROM whatsapp_connections WHERE tenant_id = :t"),
                {"t": tenant_id}).mappings().first()
            if c is None:
                raise SystemExit(f"no WhatsApp connection for tenant {tenant_id}")
            wa = type("Conn", (), dict(c))()
            scope = ledger.messaging_scope_key(wa)
            conns = ledger._scope_connections(db, wa, scope)
            scope_tenants = sorted({int(x.tenant_id) for x in conns if getattr(x, "tenant_id", None)})

            # ── legacy rule (what the guard computed before this change) ──
            states = sorted(ledger.BUDGET_STATES)
            att_rows = [dict(r) for r in conn.execute(text(
                "SELECT customer_phone_e164 AS phone, campaign_id, tenant_id, state, "
                "COALESCE(request_started_at, claimed_at) AS ts, failed_at, delivered_at, read_at "
                "FROM campaign_send_attempts WHERE messaging_scope_key = :s AND state = ANY(:st) "
                "AND COALESCE(request_started_at, claimed_at) >= :since"),
                {"s": scope, "st": states, "since": since}).mappings()]
            log_rows = [dict(r) for r in conn.execute(text(
                "SELECT id, customer_phone_e164 AS phone, campaign_id, tenant_id, status, "
                "sent_at, updated_at, created_at, failed_at, delivered_at, read_at, "
                "CASE WHEN sent_at >= :since THEN sent_at ELSE updated_at END AS ts "
                "FROM campaign_send_logs WHERE tenant_id = ANY(:t) AND (sent_at >= :since OR "
                "(status IN ('uncertain', 'sending') AND updated_at >= :since))"),
                {"t": scope_tenants, "since": since}).mappings()]
            a_ph = {r["phone"] for r in att_rows}
            l_ph = {r["phone"] for r in log_rows}
            union = a_ph | l_ph

            per: Counter = Counter()
            first_row: Dict[str, Dict[str, Any]] = {}
            for r in att_rows + log_rows:
                first_row.setdefault(r["phone"], r)
            for ph, r in first_row.items():
                per[(r["tenant_id"], r["campaign_id"])] += 1

            # Delivery evidence of the union's log rows (send-log columns).
            delivered = {r["phone"] for r in log_rows if r["delivered_at"] or r["read_at"]} | \
                        {r["phone"] for r in att_rows if r["delivered_at"] or r["read_at"]}
            failed_only = {r["phone"] for r in log_rows
                           if r["failed_at"] and not (r["delivered_at"] or r["read_at"])} - delivered

            # sent_at sanity per campaign in the window.
            by_campaign: Dict[int, List[Dict[str, Any]]] = {}
            for r in log_rows:
                by_campaign.setdefault(int(r["campaign_id"]), []).append(r)
            sanity = {}
            for cid, rows in by_campaign.items():
                last_claim = conn.execute(text(
                    "SELECT max(claimed_at) FROM campaign_send_attempts WHERE campaign_id = :c"),
                    {"c": cid}).scalar()
                camp = conn.execute(text(
                    "SELECT status, launched_at, updated_at FROM campaigns WHERE id = :c"),
                    {"c": cid}).mappings().first() or {}
                sent = [r["sent_at"] for r in rows if r["sent_at"] is not None]
                sanity[str(cid)] = {
                    "rows": len(rows), "sent_at_oldest": _iso(min(sent)) if sent else None,
                    "sent_at_newest": _iso(max(sent)) if sent else None,
                    "sent_at_after_created_plus_24h": sum(
                        1 for r in rows if r["sent_at"] and r["created_at"]
                        and r["sent_at"] > r["created_at"] + timedelta(hours=24)),
                    "sent_at_in_last_hour_before_at": sum(
                        1 for r in rows if r["sent_at"] and r["sent_at"] >= at - timedelta(hours=1)),
                    "last_ledger_claim": _iso(last_claim),
                    "campaign_status": camp.get("status"),
                    "launched_at": _iso(camp.get("launched_at")),
                }

            # ── current rule ─────────────────────────────────────────────
            usage = ledger.messaging_usage(db, scope, conns, since=since)
            cur_src: Counter = Counter()
            for rows in usage.values():
                for s in {v["source"] for v in rows}:
                    cur_src[s] += 1
            db.close()

        limit = ledger.parse_messaging_tier(c["meta_messaging_limit"])
        return {
            "at": at.isoformat(), "since": since.isoformat(),
            "connection": {
                "tenant_id": c["tenant_id"], "status": c["status"],
                "scope_key": scope,
                "has_business_manager_id": bool(c["business_manager_id"]
                                                or c["meta_business_account_id"]),
                "stored_tier": c["meta_messaging_limit"], "parsed_limit": limit,
                "tier_updated_at": _iso(c["meta_tier_updated_at"]),
                "scope_connections": len(conns), "scope_tenants": scope_tenants,
                "campaign_budget_percent": ledger.CAMPAIGN_BUDGET_PERCENT,
            },
            "legacy_rule": {
                "attempts": {"unique_phones": len(a_ph), **_span(att_rows)},
                "send_logs": {"unique_phones": len(l_ph), **_span(log_rows)},
                "intersection": len(a_ph & l_ph),
                "union_used_24h": len(union),
                "by_tenant_campaign": [
                    {"tenant_id": t, "campaign_id": cid, "unique_phones": n}
                    for (t, cid), n in sorted(per.items(), key=lambda kv: -kv[1])],
                "union_with_delivery_evidence": len(delivered & union),
                "union_failed_after_accept_only": len(failed_only & union),
            },
            "sent_at_sanity": sanity,
            "current_rule": {
                "used_24h": len(usage),
                "phones_by_source": dict(cur_src),
            },
        }
    finally:
        engine.dispose()


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tenant-id", type=int, required=True)
    ap.add_argument("--at", required=True, help="ISO timestamp, e.g. 2026-09-24T09:53:49Z")
    ap.add_argument("--expect-used", type=int)
    ap.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    args = ap.parse_args(argv)
    if not args.database_url:
        ap.error("DATABASE_URL is required")
    at = datetime.fromisoformat(args.at.replace("Z", "+00:00"))
    out = audit(args.database_url, tenant_id=args.tenant_id, at=at)
    rc = 0
    if args.expect_used is not None:
        got = out["legacy_rule"]["union_used_24h"]
        out["expect_used"] = {"expected": args.expect_used, "recomputed": got,
                              "matches": got == args.expect_used}
        rc = 0 if got == args.expect_used else 5
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return rc


if __name__ == "__main__":
    sys.exit(main())
