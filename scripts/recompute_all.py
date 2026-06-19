#!/usr/bin/env python
"""
scripts/recompute_all.py
────────────────────────
Batch-recompute every customer profile (RFM + CRM status) for one or all
tenants via ``CustomerIntelligenceService.recompute_profile_for_customer``.

Use after deploying a classification rule change — e.g. the recent-contact
gate that stops WhatsApp-active customers appearing as ``inactive`` — so
existing ``CustomerProfile`` rows refresh without waiting for a new inbound
message.

Also useful when:

  • Historical orders were back-filled and classifications need to catch up.
  • Coupon pools should be refreshed (``--coupons``).

Usage
─────
    python scripts/recompute_all.py                      # all tenants
    python scripts/recompute_all.py --tenant-id 33       # one tenant
    python scripts/recompute_all.py --tenant 33          # alias
    python scripts/recompute_all.py --dry-run            # no commits
    python scripts/recompute_all.py --coupons            # also refill pools

Production (Railway)
────────────────────
    railway run --service nahla-saas python scripts/recompute_all.py --tenant-id 33

Admin API alternative (platform JWT)
────────────────────────────────────
    POST /admin/customers/recompute-profiles?tenant_id=33

Exit code
─────────
  0 when every tenant completes with zero per-customer failures.
  Non-zero if any tenant-level error or any customer recompute failed.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    s = str(_p)
    if s not in sys.path:
        sys.path.insert(0, s)

from database.session import SessionLocal  # noqa: E402
from models import Customer, Tenant  # noqa: E402
from services.customer_intelligence import CustomerIntelligenceService  # noqa: E402
from services.coupon_generator import CouponGeneratorService  # noqa: E402

_log = logging.getLogger("nahla.recompute_all")


def _target_tenant_ids(db, only: Optional[int]) -> List[int]:
    if only is not None:
        return [only]
    rows = db.query(Tenant.id).order_by(Tenant.id).all()
    return [r[0] for r in rows]


def recompute_tenant_profiles(
    db,
    tenant_id: int,
    *,
    reason: str = "recompute_all_script",
    commit: bool = True,
    emit_event: bool = False,
) -> Dict[str, Any]:
    """Recompute every customer in a tenant; continue on per-customer errors."""
    svc = CustomerIntelligenceService(db, tenant_id)
    customer_ids = [
        row[0]
        for row in (
            db.query(Customer.id)
            .filter(Customer.tenant_id == tenant_id)
            .order_by(Customer.id)
            .all()
        )
    ]

    success = 0
    failed = 0
    errors: List[Tuple[int, str]] = []

    for customer_id in customer_ids:
        try:
            profile = svc.recompute_profile_for_customer(
                customer_id,
                reason=reason,
                commit=commit,
                emit_event=emit_event,
            )
            if profile is None:
                failed += 1
                msg = "customer_not_found"
                errors.append((customer_id, msg))
                _log.warning(
                    "tenant=%s customer=%s skipped: %s",
                    tenant_id,
                    customer_id,
                    msg,
                )
                continue
            success += 1
        except Exception as exc:
            failed += 1
            msg = f"{type(exc).__name__}: {exc}"
            errors.append((customer_id, msg))
            _log.exception(
                "tenant=%s customer=%s recompute failed",
                tenant_id,
                customer_id,
            )
            if commit:
                try:
                    db.rollback()
                except Exception as rollback_exc:
                    _log.warning(
                        "tenant=%s customer=%s rollback failed: %s",
                        tenant_id,
                        customer_id,
                        rollback_exc,
                    )

    if not commit:
        try:
            db.rollback()
        except Exception as rollback_exc:
            _log.warning("tenant=%s dry-run rollback failed: %s", tenant_id, rollback_exc)

    return {
        "tenant_id": tenant_id,
        "total": len(customer_ids),
        "success": success,
        "failed": failed,
        "errors": errors,
    }


async def _run_one(
    tenant_id: int,
    *,
    with_coupons: bool,
    commit: bool,
    emit_event: bool,
) -> dict:
    db = SessionLocal()
    result: Dict[str, Any] = {
        "tenant_id": tenant_id,
        "total": 0,
        "success": 0,
        "failed": 0,
        "errors": [],
        "coupons": None,
        "error": None,
    }
    try:
        stats = recompute_tenant_profiles(
            db,
            tenant_id,
            reason="recompute_all_script",
            commit=commit,
            emit_event=emit_event,
        )
        result.update(stats)

        if with_coupons and commit and stats["failed"] == 0:
            gen = CouponGeneratorService(db, tenant_id)
            try:
                result["coupons"] = await gen.ensure_coupon_pool()
            except Exception as exc:
                result["coupons"] = {"error": str(exc)}
                _log.exception("tenant=%s coupon pool refresh failed", tenant_id)
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()
        if commit:
            try:
                db.rollback()
            except Exception as rollback_exc:
                _log.warning("tenant=%s rollback failed: %s", tenant_id, rollback_exc)
    finally:
        db.close()
    return result


async def _main_async(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    tenant_filter = args.tenant_id if args.tenant_id is not None else args.tenant

    db = SessionLocal()
    try:
        tenant_ids = _target_tenant_ids(db, tenant_filter)
    finally:
        db.close()

    if not tenant_ids:
        print("No tenants found.")
        return 0

    print(f"Recomputing {len(tenant_ids)} tenant(s): {tenant_ids}")
    print(
        f"  coupons: {'yes' if args.coupons else 'no'}"
        f"   dry-run: {args.dry_run}"
        f"   emit_events: {'yes' if args.emit_events else 'no'}"
    )

    tenants_failed = 0
    customers_failed = 0
    for tid in tenant_ids:
        res = await _run_one(
            tid,
            with_coupons=args.coupons,
            commit=not args.dry_run,
            emit_event=args.emit_events,
        )
        if res.get("error"):
            tenants_failed += 1
            print(f"  tenant {tid}: TENANT ERROR {res['error']}")
            continue

        customers_failed += int(res.get("failed") or 0)
        extra = ""
        if res.get("coupons") is not None:
            extra = f"  coupons={res['coupons']}"
        print(
            f"  tenant {tid}: total={res['total']} success={res['success']}"
            f" failed={res['failed']}{extra}"
        )
        for customer_id, err in (res.get("errors") or [])[:10]:
            print(f"    customer {customer_id}: {err}")
        remaining = len(res.get("errors") or []) - 10
        if remaining > 0:
            print(f"    ... and {remaining} more customer error(s)")

    print(
        f"Done. tenant_errors={tenants_failed}"
        f" customer_failures={customers_failed}"
    )
    return 1 if tenants_failed or customers_failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Recompute Nahla customer profiles (CRM status + RFM)",
    )
    ap.add_argument(
        "--tenant-id",
        type=int,
        default=None,
        dest="tenant_id",
        help="Process only this tenant_id",
    )
    ap.add_argument(
        "--tenant",
        type=int,
        default=None,
        help="Alias for --tenant-id",
    )
    ap.add_argument(
        "--coupons",
        action="store_true",
        help="Also ensure_coupon_pool() per tenant after successful recompute",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Run recompute without committing profile changes",
    )
    ap.add_argument(
        "--emit-events",
        action="store_true",
        help="Emit customer_status_changed automation events (default: off)",
    )
    args = ap.parse_args()
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
