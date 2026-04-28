"""
extend_trial.py
───────────────
One-shot script to extend a tenant's free trial by N days WITHOUT
activating any paid subscription. Safe to run multiple times.

Usage (Railway shell):
    python backend/scripts/extend_trial.py --tenant 33 --days 14
    python backend/scripts/extend_trial.py --tenant 33 --days 14 \
        --reason "payment_provider_pending_review"

Touches only `tenants.trial_ends_at` and writes audit metadata into
`tenant_settings.metadata.billing.trial_extension_*`. Never modifies
created_at, billing_status, subscription_status, or any subscription row.

Mirrors the logic of POST /admin/tenants/{tenant_id}/extend-trial so that
the result is identical regardless of which entry point you use.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

# ── Path bootstrap ────────────────────────────────────────────────────────────
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
for p in (
    os.path.join(ROOT, "backend"),
    os.path.join(ROOT, "database"),
):
    if p not in sys.path:
        sys.path.insert(0, p)

from sqlalchemy.orm.attributes import flag_modified  # noqa: E402

from core.database import SessionLocal               # noqa: E402
from core.tenant import get_or_create_settings       # noqa: E402
from models import Tenant                            # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("extend_trial")


def extend(tenant_id: int, days: int, reason: str) -> int:
    db = SessionLocal()
    try:
        tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
        if not tenant:
            log.error("Tenant %s not found", tenant_id)
            return 1

        now = datetime.now(timezone.utc)
        current = tenant.trial_ends_at
        if current is not None and current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)

        if current is not None and current > now:
            new_end = current + timedelta(days=days)
            mode = "extended_from_existing"
        else:
            new_end = now + timedelta(days=days)
            mode = "reset_to_now_plus_days"

        previous_iso = current.isoformat() if current else None
        tenant.trial_ends_at = new_end.replace(tzinfo=None)

        settings = get_or_create_settings(db, tenant_id)
        meta     = dict(settings.extra_metadata or {})
        billing  = dict(meta.get("billing") or {})
        history  = list(billing.get("trial_extension_history") or [])
        history.append({
            "extended_at":  now.isoformat(),
            "previous_end": previous_iso,
            "new_end":      new_end.isoformat(),
            "days_added":   days,
            "reason":       reason,
            "mode":         mode,
            "admin":        "cli:extend_trial.py",
        })
        billing["trial_extended_by_admin"] = True
        billing["trial_extension_reason"]  = reason
        billing["trial_extension_history"] = history[-20:]
        meta["billing"] = billing
        settings.extra_metadata = meta
        flag_modified(settings, "extra_metadata")

        db.commit()

        log.info("✓ tenant=%s mode=%s days=%s", tenant_id, mode, days)
        log.info("  previous_end : %s", previous_iso or "—")
        log.info("  new_end      : %s", new_end.isoformat())
        log.info("  reason       : %s", reason)
        return 0
    finally:
        db.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="Extend a tenant's free trial.")
    ap.add_argument("--tenant", type=int, required=True, help="Tenant ID")
    ap.add_argument("--days",   type=int, default=14,    help="Days to add (default: 14)")
    ap.add_argument(
        "--reason", type=str,
        default="payment_provider_pending_review",
        help="Audit reason (default: payment_provider_pending_review)",
    )
    args = ap.parse_args()

    if args.days < 1 or args.days > 365:
        log.error("days must be between 1 and 365")
        return 2

    return extend(args.tenant, args.days, args.reason)


if __name__ == "__main__":
    sys.exit(main())
