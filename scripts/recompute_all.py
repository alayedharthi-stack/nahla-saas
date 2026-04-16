#!/usr/bin/env python
"""
scripts/recompute_all.py
────────────────────────
Batch-recompute every customer profile (RFM + status) for one or all tenants
and, optionally, top up each segment's coupon pool using the new NH***
generator. Useful when:

  • Historical orders were back-filled and classifications need to catch up.
  • Coupon pools were created with the old NHL### generator and you want the
    new NH*** pool refreshed.
  • After deploying a classification rule change.

Usage
─────
    python scripts/recompute_all.py                 # all tenants, classify only
    python scripts/recompute_all.py --tenant 12     # one tenant
    python scripts/recompute_all.py --coupons       # also refill coupon pool
    python scripts/recompute_all.py --dry-run       # no commits

Exit code
─────────
  0 on success. Non-zero if ANY tenant failed — the script still processes
  the rest of the tenants and logs the failures.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import traceback
from pathlib import Path
from typing import List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    s = str(_p)
    if s not in sys.path:
        sys.path.insert(0, s)

from database.session import SessionLocal  # noqa: E402
from models import Tenant  # noqa: E402
from services.customer_intelligence import CustomerIntelligenceService  # noqa: E402
from services.coupon_generator import CouponGeneratorService  # noqa: E402


def _target_tenant_ids(db, only: Optional[int]) -> List[int]:
    if only is not None:
        return [only]
    rows = db.query(Tenant.id).order_by(Tenant.id).all()
    return [r[0] for r in rows]


async def _run_one(tenant_id: int, *, with_coupons: bool, commit: bool) -> dict:
    db = SessionLocal()
    result = {"tenant_id": tenant_id, "classified": 0, "coupons": None, "error": None}
    try:
        intel = CustomerIntelligenceService(db, tenant_id)
        n = intel.rebuild_profiles_for_tenant(
            reason="recompute_all_script",
            commit=commit,
        )
        result["classified"] = int(n or 0)

        if with_coupons:
            gen = CouponGeneratorService(db, tenant_id)
            try:
                result["coupons"] = await gen.ensure_coupon_pool()
            except Exception as exc:  # noqa: BLE001
                result["coupons"] = {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()
        if commit:
            try:
                db.rollback()
            except Exception:  # noqa: silent-ok — rollback best-effort; primary error already captured above
                pass
    finally:
        db.close()
    return result


async def _main_async(args: argparse.Namespace) -> int:
    db = SessionLocal()
    try:
        tenant_ids = _target_tenant_ids(db, args.tenant)
    finally:
        db.close()

    if not tenant_ids:
        print("No tenants found.")
        return 0

    print(f"Recomputing {len(tenant_ids)} tenant(s): {tenant_ids}")
    print(f"  coupons: {'yes' if args.coupons else 'no'}   dry-run: {args.dry_run}")

    failed = 0
    for tid in tenant_ids:
        res = await _run_one(
            tid, with_coupons=args.coupons, commit=not args.dry_run
        )
        if res["error"]:
            failed += 1
            print(f"  tenant {tid}: ERROR {res['error']}")
        else:
            extra = ""
            if res["coupons"] is not None:
                extra = f"  coupons={res['coupons']}"
            print(f"  tenant {tid}: classified={res['classified']}{extra}")

    print(f"Done. {failed} tenant(s) failed.")
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Recompute Nahla customer classifications + coupon pools")
    ap.add_argument("--tenant", type=int, default=None, help="Process only this tenant_id")
    ap.add_argument("--coupons", action="store_true", help="Also ensure_coupon_pool() per tenant")
    ap.add_argument("--dry-run", action="store_true", help="Do not commit classification changes")
    args = ap.parse_args()
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
