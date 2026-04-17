#!/usr/bin/env python
"""
scripts/backfill_whatsapp_phone_metadata.py
────────────────────────────────────────────
Repair `whatsapp_connections` rows that flipped to `status='connected'`
without populating `phone_number` / `business_display_name`.

Background
──────────
`commit_connection()` is now self-healing — every new connect/reconnect
calls `fetch_phone_metadata()` and persists the display fields before
the row is written. This script exists for two purposes:

  1. Repair pre-existing rows that were written before the fix shipped
     (the "half-bootstrapped" tenants surfaced by the RCA in
     `docs/runbooks/whatsapp-half-bootstrap-rca.md`).
  2. Provide a quick re-sync command when Meta updates a number's
     verified_name and we want to refresh our cached copy.

Usage
─────
    # Dry-run across all tenants (default — never writes)
    python scripts/backfill_whatsapp_phone_metadata.py

    # Apply for one tenant
    python scripts/backfill_whatsapp_phone_metadata.py --tenant 1 --commit

    # Apply for every half-bootstrapped row
    python scripts/backfill_whatsapp_phone_metadata.py --all --commit

    # Force re-sync even when fields are already populated
    python scripts/backfill_whatsapp_phone_metadata.py --all --commit --refresh

Flags
─────
    --tenant N   Only operate on tenant N.
    --all        Operate on every connected tenant (mutually exclusive with --tenant).
    --commit     Persist changes (default is dry-run).
    --refresh    Re-fetch metadata even when both fields are already populated.

Exit code
─────────
    0 — every row processed, regardless of how many were updated.
    1 — at least one row failed (per-row failures never abort the run; we
        still process the rest and the final exit code reflects the worst).

Safety
──────
    • Never alters connection status, sending_enabled, tokens, or webhook flags.
    • Touches only `phone_number`, `business_display_name`, and `updated_at`.
    • Runs Meta lookups serially with a 10s timeout (matches the helper).
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import List, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    s = str(_p)
    if s not in sys.path:
        sys.path.insert(0, s)

from datetime import datetime, timezone  # noqa: E402

from sqlalchemy import or_  # noqa: E402

from core.database import SessionLocal  # noqa: E402
from models import WhatsAppConnection  # noqa: E402
from services.whatsapp_connection_service import fetch_phone_metadata  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("backfill_wa_phone_metadata")


def _select_rows(
    db, *, tenant_id: int | None, refresh: bool
) -> List[WhatsAppConnection]:
    q = db.query(WhatsAppConnection).filter(WhatsAppConnection.status == "connected")
    if tenant_id is not None:
        q = q.filter(WhatsAppConnection.tenant_id == tenant_id)
    if not refresh:
        q = q.filter(
            or_(
                WhatsAppConnection.phone_number.is_(None),
                WhatsAppConnection.business_display_name.is_(None),
            )
        )
    # Need credentials to call Meta.
    q = q.filter(
        WhatsAppConnection.phone_number_id.isnot(None),
        WhatsAppConnection.access_token.isnot(None),
    )
    return q.order_by(WhatsAppConnection.tenant_id.asc(), WhatsAppConnection.id.asc()).all()


def _run(*, tenant_id: int | None, commit: bool, refresh: bool) -> Tuple[int, int, int]:
    """Returns (scanned, updated, failed)."""
    scanned = updated = failed = 0
    db = SessionLocal()
    try:
        rows = _select_rows(db, tenant_id=tenant_id, refresh=refresh)
        if not rows:
            log.info("nothing to do — no rows match the filter")
            return 0, 0, 0

        for conn in rows:
            scanned += 1
            try:
                meta = fetch_phone_metadata(
                    conn.phone_number_id, conn.access_token, conn.tenant_id
                )
                new_phone   = meta.get("display_phone_number")
                new_display = meta.get("verified_name")

                changed = False
                if new_phone and (refresh or not conn.phone_number) and new_phone != conn.phone_number:
                    log.info(
                        "tenant=%s id=%s phone_number: %r → %r",
                        conn.tenant_id, conn.id, conn.phone_number, new_phone,
                    )
                    if commit:
                        conn.phone_number = new_phone
                    changed = True
                if new_display and (refresh or not conn.business_display_name) and new_display != conn.business_display_name:
                    log.info(
                        "tenant=%s id=%s business_display_name: %r → %r",
                        conn.tenant_id, conn.id, conn.business_display_name, new_display,
                    )
                    if commit:
                        conn.business_display_name = new_display
                    changed = True

                if not changed:
                    log.info(
                        "tenant=%s id=%s phone_id=%s — Meta returned no usable fields "
                        "(token may lack scope or number deleted)",
                        conn.tenant_id, conn.id, conn.phone_number_id,
                    )
                    continue

                if commit:
                    conn.updated_at = datetime.now(timezone.utc)
                    db.commit()
                updated += 1
            except Exception as exc:  # noqa: BLE001 — per-row failure must not abort
                failed += 1
                log.exception(
                    "tenant=%s id=%s — backfill failed: %s",
                    conn.tenant_id, conn.id, exc,
                )
                db.rollback()
    finally:
        db.close()

    return scanned, updated, failed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", type=int, default=None, help="Only this tenant id")
    parser.add_argument("--all", action="store_true", help="Every connected tenant")
    parser.add_argument("--commit", action="store_true", help="Persist changes (default: dry-run)")
    parser.add_argument(
        "--refresh", action="store_true",
        help="Re-fetch metadata even for rows where both fields are already populated",
    )
    args = parser.parse_args()

    if args.tenant is None and not args.all:
        log.warning("no scope flag — defaulting to dry-run across all connected tenants")
    if args.tenant is not None and args.all:
        parser.error("--tenant and --all are mutually exclusive")

    mode = "COMMIT" if args.commit else "DRY-RUN"
    scope = f"tenant={args.tenant}" if args.tenant is not None else "ALL connected"
    log.info("[%s] starting backfill (scope=%s, refresh=%s)", mode, scope, args.refresh)

    scanned, updated, failed = _run(
        tenant_id=args.tenant, commit=args.commit, refresh=args.refresh
    )
    log.info(
        "[%s] done — scanned=%d updated=%d failed=%d", mode, scanned, updated, failed,
    )
    if not args.commit and (scanned or updated):
        log.info("dry-run only — re-run with --commit to persist")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
