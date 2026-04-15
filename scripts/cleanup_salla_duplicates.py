#!/usr/bin/env python
"""
scripts/cleanup_salla_duplicates.py
─────────────────────────────────────
Step 1 (pre-migration): identify and clean up duplicate Salla integrations.

IMPORTANT: This script must NOT use the SQLAlchemy Integration ORM for reads.
The `external_store_id` column may not exist yet in the database; ORM queries
would generate SELECT ... external_store_id and crash.  We use raw SQL only.

A "duplicate" means two or more `integrations` rows share the same logical
Salla store id (from config->>'store_id' or legacy external_store_id if present).

Winner-selection strategy (per group):
  1. Prefer the integration that has both api_key + refresh_token (full OAuth).
  2. Next, prefer one that has at least api_key (easy mode).
  3. Tiebreak: higher `id` (most recently inserted).

Loser rows:
  • tokens (api_key, access_token, refresh_token) are wiped.
  • config["store_id"] is removed so migration 0017 does not assign the same
    external_store_id as the winner (UNIQUE constraint).
  • enabled is set to False.
  • config["revoked_reason"] is set for audit trail.

Usage
─────
  python scripts/cleanup_salla_duplicates.py              # dry-run (no writes)
  python scripts/cleanup_salla_duplicates.py --execute    # apply to database
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    s = str(_p)
    if s not in sys.path:
        sys.path.insert(0, s)

from sqlalchemy import text  # noqa: E402
from sqlalchemy.exc import OperationalError, ProgrammingError  # noqa: E402
from database.session import SessionLocal  # noqa: E402


# ── Row type (plain dicts from raw SQL) ───────────────────────────────────────

def _store_id_from_row(r: dict[str, Any]) -> str:
    """Logical Salla store id: prefer DB column if present, else config."""
    # When external_store_id column is missing, r won't have the key
    ext = r.get("external_store_id")
    if ext:
        return str(ext)
    cfg = r.get("config") or {}
    if isinstance(cfg, dict):
        sid = cfg.get("store_id")
        return str(sid) if sid else ""
    return ""


def _score(r: dict[str, Any]) -> int:
    """Higher score = better candidate to keep in a duplicate group."""
    cfg: dict[str, Any] = r.get("config") or {}
    if not isinstance(cfg, dict):
        cfg = {}
    has_access = bool(cfg.get("api_key") or cfg.get("access_token"))
    has_refresh = bool(cfg.get("refresh_token"))
    rid = int(r["id"])
    return (has_access * 2 + has_refresh) * 1_000_000 + rid


def _fetch_salla_rows(db) -> list[dict[str, Any]]:
    """
    Load Salla integrations.  Prefer `external_store_id` when the column exists;
    if the DB predates migration 0017, fall back to a narrower SELECT (no ORM).
    """
    sql_with = """
        SELECT id, tenant_id, provider, config, enabled, external_store_id
        FROM integrations
        WHERE provider = 'salla'
        ORDER BY id
    """
    sql_without = """
        SELECT id, tenant_id, provider, config, enabled
        FROM integrations
        WHERE provider = 'salla'
        ORDER BY id
    """
    try:
        rows = db.execute(text(sql_with)).mappings().all()
    except (OperationalError, ProgrammingError):
        rows = db.execute(text(sql_without)).mappings().all()
    return [dict(row) for row in rows]


def find_duplicate_groups(db) -> dict[str, list[dict[str, Any]]]:
    """Return {store_id: [row dict, ...]} for groups with > 1 member."""
    rows = _fetch_salla_rows(db)
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        sid = _store_id_from_row(row)
        if not sid:
            continue
        groups.setdefault(sid, []).append(row)
    return {k: v for k, v in groups.items() if len(v) > 1}


def _tenant_has_other_data(db, tenant_id: int, skip_integration_id: int) -> bool:
    """Raw SQL only — no Integration ORM."""
    u = db.execute(
        text("SELECT 1 FROM users WHERE tenant_id = :tid LIMIT 1"),
        {"tid": tenant_id},
    ).scalar()
    if u:
        return True
    o = db.execute(
        text(
            """
            SELECT 1 FROM integrations
            WHERE tenant_id = :tid AND id != :skip
            LIMIT 1
            """
        ),
        {"tid": tenant_id, "skip": skip_integration_id},
    ).scalar()
    return bool(o)


def _apply_loser_update(db, loser_id: int, new_cfg: dict[str, Any]) -> None:
    db.execute(
        text(
            """
            UPDATE integrations
            SET config = CAST(:cfg AS jsonb), enabled = false
            WHERE id = :id
            """
        ),
        {"cfg": json.dumps(new_cfg), "id": loser_id},
    )


# ── Main cleanup ──────────────────────────────────────────────────────────────

def cleanup(dry_run: bool = True) -> int:
    """
    Scan for and (optionally) clean up duplicate Salla integrations.

    Returns the number of duplicate groups found.
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
            ranked = sorted(rows, key=_score, reverse=True)
            winner = ranked[0]
            losers = ranked[1:]
            win_cfg = winner.get("config") or {}
            if not isinstance(win_cfg, dict):
                win_cfg = {}

            print(f"  store_id: {store_id}  ({len(rows)} rows)")
            print(
                f"    ✅ KEEP   → id={winner['id']:>5}  tenant={winner['tenant_id']:<6}  "
                f"enabled={str(winner.get('enabled')):<5}  "
                f"api_key={'✓' if win_cfg.get('api_key') else '✗'}  "
                f"refresh={'✓' if win_cfg.get('refresh_token') else '✗'}"
            )
            for loser in losers:
                lc = loser.get("config") or {}
                if not isinstance(lc, dict):
                    lc = {}
                print(
                    f"    ⛔ REVOKE → id={loser['id']:>5}  tenant={loser['tenant_id']:<6}  "
                    f"enabled={str(loser.get('enabled')):<5}  "
                    f"api_key={'✓' if lc.get('api_key') else '✗'}  "
                    f"refresh={'✓' if lc.get('refresh_token') else '✗'}"
                )
                if not dry_run:
                    has_other = _tenant_has_other_data(db, loser["tenant_id"], loser["id"])
                    if not has_other:
                        print(
                            f"          (tenant {loser['tenant_id']} has no other data — "
                            "safe to delete manually after this script)"
                        )

            if not dry_run:
                for loser in losers:
                    new_cfg = dict(loser.get("config") or {})
                    if not isinstance(new_cfg, dict):
                        new_cfg = {}
                    new_cfg.pop("api_key", None)
                    new_cfg.pop("access_token", None)
                    new_cfg.pop("refresh_token", None)
                    new_cfg.pop("store_id", None)
                    new_cfg["revoked_reason"] = (
                        f"duplicate-cleanup: kept tenant {winner['tenant_id']} "
                        f"integration {winner['id']} for store {store_id}"
                    )
                    _apply_loser_update(db, loser["id"], new_cfg)

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
    sys.exit(0 if (found == 0 or args.execute) else 1)
