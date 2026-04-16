#!/usr/bin/env python
"""
scripts/inspect_pipeline.py
───────────────────────────
Operational snapshot of the Salla → Nahla pipeline. Dumps, in a single pass:

  • webhook_events counts by status and recent failures / dead-letters
  • recent orders (last 24h) with their tenant + customer linkage
  • orders with NULL customer_id (loss-of-signal indicator)
  • customer segment distribution per tenant
  • customers whose stored metrics disagree with derived metrics
  • recent coupons, grouped by pool status, and any coupons missing the
    new NH*** short-code format

Run as:
    python scripts/inspect_pipeline.py
    python scripts/inspect_pipeline.py --tenant 12 --hours 72 --json

Exit code is always 0 — this is a read-only diagnostic tool.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    s = str(_p)
    if s not in sys.path:
        sys.path.insert(0, s)

from sqlalchemy import func  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from database.session import SessionLocal  # noqa: E402
from models import (  # noqa: E402
    Coupon,
    Customer,
    CustomerProfile,
    Order,
    Tenant,
    WebhookEvent,
)


NH_SHORT_RE = re.compile(r"^NH[A-Z0-9]{3}$")
LEGACY_SHORT_RE = re.compile(r"^NHL\d{3}$")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _fmt_dt(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


# ── Inspectors ───────────────────────────────────────────────────────────────


def inspect_webhook_events(db: Session, hours: int) -> Dict[str, Any]:
    cutoff = _utcnow() - timedelta(hours=hours)

    by_status: Dict[str, int] = dict(
        db.query(WebhookEvent.status, func.count(WebhookEvent.id))
        .group_by(WebhookEvent.status)
        .all()
    )

    recent_failed = (
        db.query(WebhookEvent)
        .filter(
            WebhookEvent.status.in_(("failed", "dead_letter")),
            WebhookEvent.received_at >= cutoff,
        )
        .order_by(WebhookEvent.received_at.desc())
        .limit(20)
        .all()
    )

    return {
        "by_status": by_status,
        "recent_failures": [
            {
                "id": e.id,
                "provider": e.provider,
                "event_type": e.event_type,
                "tenant_id": e.tenant_id,
                "attempts": e.attempts,
                "status": e.status,
                "received_at": _fmt_dt(e.received_at),
                "last_error": (e.last_error or "")[:200],
            }
            for e in recent_failed
        ],
    }


def inspect_orders(db: Session, hours: int, tenant_id: Optional[int]) -> Dict[str, Any]:
    """
    The Order table has no `created_at` column — it was designed as a mirror of
    Salla's `orders` resource where the authoritative timestamp is stored in
    `extra_metadata["created_at"]`. We order by `id` descending as a proxy for
    insertion order and filter in Python on the JSON timestamp when available.
    """
    cutoff = _utcnow() - timedelta(hours=hours)
    q = db.query(Order)
    if tenant_id is not None:
        q = q.filter(Order.tenant_id == tenant_id)
    orders = q.order_by(Order.id.desc()).limit(200).all()

    def _extract_created(o) -> Optional[datetime]:
        raw = (o.extra_metadata or {}).get("created_at") if o.extra_metadata else None
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    recent = [o for o in orders if (_extract_created(o) or _utcnow()) >= cutoff][:50]

    # "Unlinked" here means customer_info has no usable identity (phone/email/name).
    def _is_unlinked(o) -> bool:
        info = o.customer_info or {}
        return not any(info.get(k) for k in ("mobile", "phone", "email", "name"))

    unlinked = sum(1 for o in orders if _is_unlinked(o))
    missing_external = sum(1 for o in orders if not o.external_id)

    return {
        "recent_window_hours": hours,
        "sampled_count": len(orders),
        "recent_count": len(recent),
        "unlinked_customer_count": unlinked,
        "missing_external_id_count": missing_external,
        "recent": [
            {
                "id": o.id,
                "tenant_id": o.tenant_id,
                "external_id": o.external_id,
                "total": o.total,
                "status": o.status,
                "customer_phone": (o.customer_info or {}).get("mobile")
                or (o.customer_info or {}).get("phone"),
                "created_at": _fmt_dt(_extract_created(o)),
            }
            for o in recent
        ],
    }


def inspect_customers(db: Session, tenant_id: Optional[int]) -> Dict[str, Any]:
    """
    Classification source of truth is `customer_profiles.customer_status` (see
    CustomerIntelligenceService.compute_customer_status). Customer rows hold
    identity only — they have no `status` column. A "suspicious" row is a
    profile whose stored `total_orders` disagrees with the number of Order
    rows the customer's phone matches in the raw orders table.
    """
    # Classification histogram per tenant.
    q = (
        db.query(
            CustomerProfile.tenant_id,
            CustomerProfile.customer_status,
            func.count(CustomerProfile.id),
        )
        .group_by(CustomerProfile.tenant_id, CustomerProfile.customer_status)
    )
    if tenant_id is not None:
        q = q.filter(CustomerProfile.tenant_id == tenant_id)

    dist: Dict[int, Counter] = defaultdict(Counter)
    for tid, status, n in q.all():
        dist[tid][status or "unknown"] = int(n)

    # Build phone → real-order-count index for the tenant scope.
    order_q = db.query(Order)
    if tenant_id is not None:
        order_q = order_q.filter(Order.tenant_id == tenant_id)
    phone_counts: Counter = Counter()
    for o in order_q.all():
        info = o.customer_info or {}
        phone = info.get("mobile") or info.get("phone")
        if phone:
            phone_counts[(o.tenant_id, str(phone))] += 1

    # Compare stored CustomerProfile.total_orders vs real phone-matched count.
    suspicious: List[Dict[str, Any]] = []
    prof_q = db.query(CustomerProfile, Customer).join(
        Customer, Customer.id == CustomerProfile.customer_id
    )
    if tenant_id is not None:
        prof_q = prof_q.filter(CustomerProfile.tenant_id == tenant_id)
    for prof, cust in prof_q.limit(500).all():
        real = phone_counts.get((prof.tenant_id, str(cust.phone or "")), 0)
        stored = int(prof.total_orders or 0)
        if cust.phone and stored != real:
            suspicious.append({
                "customer_id": cust.id,
                "tenant_id": prof.tenant_id,
                "phone": cust.phone,
                "stored_total_orders": stored,
                "real_orders_for_phone": real,
                "customer_status": prof.customer_status,
                "last_recomputed_reason": prof.last_recomputed_reason,
                "metrics_computed_at": _fmt_dt(prof.metrics_computed_at),
            })

    return {
        "status_distribution": {
            str(tid): dict(c) for tid, c in dist.items()
        },
        "suspicious_classifications": suspicious[:50],
        "suspicious_total": len(suspicious),
    }


def inspect_coupons(db: Session, tenant_id: Optional[int]) -> Dict[str, Any]:
    q = db.query(Coupon)
    if tenant_id is not None:
        q = q.filter(Coupon.tenant_id == tenant_id)

    coupons = q.order_by(Coupon.id.desc()).limit(500).all()

    # Coupon has no first-class `status` or `segment` columns — those live in
    # `extra_metadata`. We also classify codes into the new (NH***), legacy
    # (NHL###) or marketing (free-form) buckets.
    nh_ok = 0
    legacy = 0
    marketing = 0
    malformed_pool: List[Dict[str, Any]] = []
    bucket_status: Counter = Counter()
    bucket_segment: Counter = Counter()
    for c in coupons:
        code = (c.code or "").upper()
        meta = c.extra_metadata or {}
        seg = meta.get("target_segment") or "unknown"
        src = meta.get("source") or "manual"
        used = str(meta.get("used", "false")).lower() == "true"
        if NH_SHORT_RE.match(code):
            nh_ok += 1
        elif LEGACY_SHORT_RE.match(code):
            legacy += 1
        else:
            marketing += 1
            if src == "auto":
                malformed_pool.append({
                    "id": c.id,
                    "code": c.code,
                    "segment": seg,
                    "source": src,
                    "tenant_id": c.tenant_id,
                })

        status_label = "used" if used else ("active" if meta.get("active", True) else "inactive")
        bucket_status[status_label] += 1
        if src == "auto":
            bucket_segment[seg] += 1

    return {
        "sampled": len(coupons),
        "by_status": dict(bucket_status),
        "by_segment": dict(bucket_segment),
        "nh_short_count": nh_ok,
        "nhl_legacy_count": legacy,
        "marketing_count": marketing,
        "malformed_pool_coupons": malformed_pool[:20],
        "malformed_total": len(malformed_pool),
    }


# ── CLI ───────────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description="Inspect Nahla pipeline health")
    ap.add_argument("--tenant", type=int, default=None, help="Limit to one tenant_id")
    ap.add_argument("--hours", type=int, default=24, help="Window for orders/webhooks")
    ap.add_argument("--json", action="store_true", help="Dump JSON instead of pretty text")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        report: Dict[str, Any] = {
            "generated_at": _utcnow().isoformat(),
            "tenant_filter": args.tenant,
            "window_hours": args.hours,
            "webhook_events": inspect_webhook_events(db, args.hours),
            "orders": inspect_orders(db, args.hours, args.tenant),
            "customers": inspect_customers(db, args.tenant),
            "coupons": inspect_coupons(db, args.tenant),
        }

        if args.tenant is None:
            report["tenants"] = [
                {"id": t.id, "name": getattr(t, "name", None)}
                for t in db.query(Tenant).order_by(Tenant.id).limit(50).all()
            ]
    finally:
        db.close()

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _print_report(report)
    return 0


def _print_report(report: Dict[str, Any]) -> None:
    print(f"=== Nahla pipeline snapshot @ {report['generated_at']} ===")
    print(f"Tenant filter:  {report['tenant_filter']}")
    print(f"Window hours:   {report['window_hours']}")

    we = report["webhook_events"]
    print("\n--- Webhook events ---")
    for status, n in (we.get("by_status") or {}).items():
        print(f"  {status:<12} {n}")
    if we["recent_failures"]:
        print("  Recent failures:")
        for f in we["recent_failures"]:
            print(
                f"   #{f['id']} {f['provider']}/{f['event_type']} "
                f"attempts={f['attempts']} status={f['status']} "
                f"err={f['last_error']}"
            )

    o = report["orders"]
    print("\n--- Orders ---")
    print(
        f"  sampled={o['sampled_count']}  in_window={o['recent_count']}  "
        f"unlinked_customer={o['unlinked_customer_count']}  "
        f"missing_external_id={o['missing_external_id_count']}"
    )

    c = report["customers"]
    print("\n--- Customers ---")
    for tid, dist in (c.get("status_distribution") or {}).items():
        print(f"  tenant={tid}: {dist}")
    if c["suspicious_total"]:
        print(f"  suspicious classifications: {c['suspicious_total']}")
        for s in c["suspicious_classifications"][:5]:
            print(
                f"    cust#{s['customer_id']} t={s['tenant_id']} phone={s['phone']} "
                f"stored={s['stored_total_orders']} real={s['real_orders_for_phone']} "
                f"status={s['customer_status']}"
            )

    cp = report["coupons"]
    print("\n--- Coupons ---")
    print(
        f"  sampled={cp['sampled']}  nh_short={cp['nh_short_count']}  "
        f"nhl_legacy={cp['nhl_legacy_count']}  marketing={cp['marketing_count']}  "
        f"malformed_pool={cp['malformed_total']}"
    )
    print(f"  by_status={cp['by_status']}  by_segment={cp['by_segment']}")


if __name__ == "__main__":
    raise SystemExit(main())
