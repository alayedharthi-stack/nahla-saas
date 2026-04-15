#!/usr/bin/env python
"""
scripts/cleanup_salla_duplicates.py
─────────────────────────────────────
Step 1 (pre-migration): identify and clean up duplicate Salla integrations.

A "duplicate" means two or more `integrations` rows share the same
  (provider='salla', external_store_id)
value.  Migration 0017 creates a UNIQUE constraint on that pair and will
raise a RuntimeError if any duplicates remain.

Winner-selection strategy (per group):
  1. Prefer the integration that has both api_key + refresh_token (full OAuth).
  2. Next, prefer one that has at least api_key (easy mode).
  3. Tiebreak: higher `id` (most recently inserted).

Loser rows:
  • tokens (api_key, access_token, refresh_token) are wiped.
  • enabled is set to False.
  • config["revoked_reason"] is set for audit trail.
  • external_store_id is backfilled if it was NULL (preparation for migration).

Usage
─────
  python scripts/cleanup_salla_duplicates.py              # dry-run (no writes)
  python scripts/cleanup_salla_duplicates.py --execute    # apply to database
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    s = str(_p)
    if s not in sys.path:
        sys.path.insert(0, s)

from database.models import Integration, Tenant  # noqa: E402
from database.session import SessionLocal  # noqa: E402


# ── Winner scoring ────────────────────────────────────────────────────────────

def _score(row: Integration) -> int:
    """Higher score = better candidate to keep in a duplicate group."""
    cfg: dict[str, Any] = row.config or {}
    has_access  = bool(cfg.get("api_key") or cfg.get("access_token"))
    has_refresh = bool(cfg.get("refresh_token"))
    return (has_access * 2 + has_refresh) * 1_000_000 + row.id


# ── Duplicate finder ──────────────────────────────────────────────────────────

def find_duplicate_groups(db) -> dict[str, list[Integration]]:
    """Return {store_id: [integration, ...]} for groups with > 1 member."""
    rows: list[Integration] = (
        db.query(Integration)
        .filter(Integration.provider == "salla")
        .order_by(Integration.id)
        .all()
    )

    groups: dict[str, list[Integration]] = {}
    for row in rows:
        # Use external_store_id first; fall back to config for pre-migration rows
        sid = row.external_store_id or (row.config or {}).get("store_id") or ""
        if not sid:
            continue
        groups.setdefault(sid, []).append(row)

    return {k: v for k, v in groups.items() if len(v) > 1}


# ── Tenant audit ──────────────────────────────────────────────────────────────

def _tenant_has_other_data(db, tenant_id: int, skip_integration_id: int) -> bool:
    """
    Returns True when the tenant owns data besides the duplicate integration.
    Used to decide whether to mention potential orphan cleanup.
    """
    from database.models import User, Integration as _Int  # noqa: PLC0415
    has_users = db.query(User).filter_by(tenant_id=tenant_id).first() is not None
    other_integrations = (
        db.query(_Int)
        .filter(
            _Int.tenant_id == tenant_id,
            _Int.id != skip_integration_id,
        )
        .first()
    ) is not None
    return has_users or other_integrations


# ── Main cleanup ──────────────────────────────────────────────────────────────

def cleanup(dry_run: bool = True) -> int:
    """
    Scan for and (optionally) clean up duplicate Salla integrations.

    Returns the number of duplicate groups found.
    Exits 0 when no groups found (migration can proceed safely).
    """
    db = SessionLocal()
    try:
        groups = find_duplicate_groups(db)

        if not groups:
            print("✅  No duplicate Salla integrations found — database is clean.")
            print("    You may safely apply migration 0017.")
            return 0

        total_losers = sum(len(v) - 1 for v in groups.values())
        print(
            f"⚠️   Found {len(groups)} duplicate Salla store group(s) "
            f"with {total_losers} integration(s) to revoke:\n"
        )

        for store_id, rows in sorted(groups.items()):
            ranked   = sorted(rows, key=_score, reverse=True)
            winner   = ranked[0]
            losers   = ranked[1:]
            win_cfg  = winner.config or {}

            print(f"  store_id: {store_id}  ({len(rows)} rows)")
            print(
                f"    ✅ KEEP   → id={winner.id:>5}  tenant={winner.tenant_id:<6}  "
                f"enabled={str(winner.enabled):<5}  "
                f"api_key={'✓' if win_cfg.get('api_key') else '✗'}  "
                f"refresh={'✓' if win_cfg.get('refresh_token') else '✗'}"
            )
            for loser in losers:
                lc = loser.config or {}
                print(
                    f"    ⛔ REVOKE → id={loser.id:>5}  tenant={loser.tenant_id:<6}  "
                    f"enabled={str(loser.enabled):<5}  "
                    f"api_key={'✓' if lc.get('api_key') else '✗'}  "
                    f"refresh={'✓' if lc.get('refresh_token') else '✗'}"
                )
                if not dry_run:
                    has_other = _tenant_has_other_data(db, loser.tenant_id, loser.id)
                    if not has_other:
                        print(
                            f"          (tenant {loser.tenant_id} has no other data — "
                            "safe to delete manually after this script)"
                        )

            if not dry_run:
                # Backfill winner's external_store_id if missing
                if not winner.external_store_id:
                    winner.external_store_id = store_id

                for loser in losers:
                    # Backfill external_store_id so constraint is satisfied
                    if not loser.external_store_id:
                        loser.external_store_id = store_id

                    new_cfg = dict(loser.config or {})
                    new_cfg.pop("api_key",       None)
                    new_cfg.pop("access_token",  None)
                    new_cfg.pop("refresh_token", None)
                    new_cfg["revoked_reason"] = (
                        f"duplicate-cleanup: kept tenant {winner.tenant_id} "
                        f"integration {winner.id} for store {store_id}"
                    )
                    loser.config  = new_cfg
                    loser.enabled = False

            print()

        if dry_run:
            print(
                f"[DRY RUN]  Would revoke {total_losers} integration(s) "
                f"across {len(groups)} group(s)."
            )
            print("           Re-run with --execute to apply changes.\n")
        else:
            db.commit()
            print(
                f"✅  Done.  Revoked {total_losers} integration(s) "
                f"across {len(groups)} group(s)."
            )
            print("   Next: run verify_salla_no_duplicates.py, then apply migration 0017.\n")

        return len(groups)

    except Exception as exc:
        db.rollback()
        print(f"❌  Error during cleanup: {exc}", file=sys.stderr)
        raise
    finally:
        db.close()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Clean up duplicate Salla integrations before applying migration 0017.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Steps:
  1. python scripts/cleanup_salla_duplicates.py              (review)
  2. python scripts/cleanup_salla_duplicates.py --execute    (apply)
  3. python scripts/verify_salla_no_duplicates.py            (confirm)
  4. alembic upgrade head                                     (migration)
""",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Write changes to the database (default is dry-run)",
    )
    args = parser.parse_args()

    found = cleanup(dry_run=not args.execute)
    # Exit 0 only when DB is clean OR changes were applied.
    # Exit 1 in dry-run mode when duplicates still exist — signals CI to stop.
    sys.exit(0 if (found == 0 or args.execute) else 1)
