"""
grant_manual_billing.py
───────────────────────
Grant or revoke a tenant-scoped manual gift billing grant (metadata only).

Usage (Railway shell):
    python backend/scripts/grant_manual_billing.py --tenant 42 --days 30 \\
        --plan starter --reason "gift for merchant community member" \\
        --granted-by "ops@nahla"

    python backend/scripts/grant_manual_billing.py --tenant 42 --revoke \\
        --granted-by "ops@nahla"

    python backend/scripts/grant_manual_billing.py --tenant 42 --days 30 \\
        --reason "test" --granted-by "ops@nahla" --dry-run

Never creates BillingSubscription, BillingPayment, or BillingInvoice rows.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
for p in (
    os.path.join(ROOT, "backend"),
    os.path.join(ROOT, "database"),
):
    if p not in sys.path:
        sys.path.insert(0, p)

from core.database import SessionLocal  # noqa: E402
from core.manual_billing_grant import (  # noqa: E402
    ManualGiftGrantError,
    apply_manual_gift_grant,
    revoke_manual_gift_grant,
)

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("grant_manual_billing")


def main() -> int:
    ap = argparse.ArgumentParser(description="Grant or revoke a manual gift billing grant.")
    ap.add_argument("--tenant", type=int, required=True, help="Tenant ID")
    ap.add_argument("--days", type=int, default=30, help="Grant duration in days (default: 30)")
    ap.add_argument("--plan", type=str, default="starter", help="Plan slug (v1: starter only)")
    ap.add_argument("--reason", type=str, default="", help="Audit reason (required for grant)")
    ap.add_argument("--granted-by", type=str, required=True, help="Admin identity for audit trail")
    ap.add_argument("--revoke", action="store_true", help="Revoke the current gift grant")
    ap.add_argument("--force", action="store_true", help="Replace an existing active gift grant")
    ap.add_argument("--dry-run", action="store_true", help="Validate only; do not write metadata")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        if args.revoke:
            result = revoke_manual_gift_grant(
                db,
                args.tenant,
                granted_by=args.granted_by,
                dry_run=args.dry_run,
            )
            log.info("✓ revoke tenant=%s dry_run=%s", args.tenant, args.dry_run)
            log.info("  revoked_by : %s", result.get("revoked_by"))
            log.info("  revoked_at : %s", result.get("revoked_at"))
            return 0

        if not (args.reason or "").strip():
            log.error("--reason is required when granting")
            return 2

        result = apply_manual_gift_grant(
            db,
            args.tenant,
            days=args.days,
            plan_slug=args.plan,
            reason=args.reason,
            granted_by=args.granted_by,
            force=args.force,
            dry_run=args.dry_run,
        )
        log.info("✓ grant tenant=%s dry_run=%s plan=%s", args.tenant, args.dry_run, result["plan_slug"])
        log.info("  starts_at  : %s", result.get("starts_at"))
        log.info("  ends_at    : %s", result.get("ends_at"))
        log.info("  reason     : %s", result.get("reason"))
        log.info("  granted_by : %s", result.get("granted_by"))
        return 0
    except ManualGiftGrantError as exc:
        log.error("✗ %s (%s)", exc, exc.code)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
