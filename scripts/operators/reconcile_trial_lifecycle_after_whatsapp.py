"""Discover / apply historical trial reconciliation after WhatsApp connect.

Read-only by default. Never takes a tenant_id special-case.

  python -m scripts.operators.reconcile_trial_lifecycle_after_whatsapp discover
  python -m scripts.operators.reconcile_trial_lifecycle_after_whatsapp apply
  python -m scripts.operators.reconcile_trial_lifecycle_after_whatsapp apply --execute
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _entry in (str(_REPO_ROOT), str(_REPO_ROOT / "backend"), str(_REPO_ROOT / "database")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool


def _emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


def _database_url() -> str:
    for key in ("DATABASE_PUBLIC_URL", "DATABASE_URL"):
        url = (os.environ.get(key) or "").strip()
        if url:
            return url
    raise SystemExit("DATABASE_URL is required")


def _session():
    engine = create_engine(_database_url(), poolclass=NullPool)
    return sessionmaker(bind=engine)()


def cmd_discover() -> int:
    from core.trial_lifecycle import (  # noqa: PLC0415
        RECONCILE_DECISION_AMBIGUOUS,
        RECONCILE_DECISION_APPLY,
        RECONCILE_DECISION_SKIP,
        discover_missing_trial_after_whatsapp,
    )

    db = _session()
    try:
        db.rollback()
        rows = discover_missing_trial_after_whatsapp(db)
        eligible = [r for r in rows if r.get("decision") == RECONCILE_DECISION_APPLY]
        ambiguous = [r for r in rows if r.get("decision") == RECONCILE_DECISION_AMBIGUOUS]
        skipped = [r for r in rows if r.get("decision") == RECONCILE_DECISION_SKIP]
        skip_counts: dict[str, int] = {}
        for row in skipped:
            reason = str(row.get("reason") or "unknown")
            skip_counts[reason] = skip_counts.get(reason, 0) + 1
        ambiguous_counts: dict[str, int] = {}
        for row in ambiguous:
            reason = str(row.get("reason") or "unknown")
            ambiguous_counts[reason] = ambiguous_counts.get(reason, 0) + 1
        _emit({
            "ok": True,
            "readonly": True,
            "queried_at": datetime.now(timezone.utc).isoformat(),
            "scanned": len(rows),
            "eligible": len(eligible),
            "skipped": len(skipped),
            "ambiguous": len(ambiguous),
            "skip_counts": skip_counts,
            "ambiguous_counts": ambiguous_counts,
            "candidates": eligible,
            "ambiguous_rows": ambiguous,
        })
        return 0
    finally:
        db.close()


def cmd_apply(*, execute: bool) -> int:
    from core.trial_lifecycle import reconcile_missing_trials_after_whatsapp_connect  # noqa: PLC0415

    db = _session()
    try:
        report = reconcile_missing_trials_after_whatsapp_connect(db, dry_run=not execute)
        report["ok"] = True
        report["queried_at"] = datetime.now(timezone.utc).isoformat()
        _emit(report)
        return 0
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("discover", help="Read-only classification of every merchant")
    apply_p = sub.add_parser("apply", help="Reconcile eligible tenants (dry-run unless --execute)")
    apply_p.add_argument(
        "--execute",
        action="store_true",
        help="Persist changes. Without this flag the command is dry-run.",
    )
    args = parser.parse_args(argv)
    if args.cmd == "discover":
        return cmd_discover()
    return cmd_apply(execute=bool(getattr(args, "execute", False)))


if __name__ == "__main__":
    raise SystemExit(main())
