"""
scripts/diagnose_salla_token.py
─────────────────────────────────
Comprehensive Salla-token diagnostic for a single integration.

Prints the full DB state (selected + sibling rows for the same store), then
optionally drives one full refresh cycle through the production code path
(`POST https://accounts.salla.sa/oauth2/token`) and prints DB state before
and after, plus the raw Salla HTTP response.

Use this when an alert email shows a confusing
`refresh_attempts=0 / last_error=invalid_grant` combination — it proves
whether the alert points at an orphan record that has been superseded by a
newer reinstall, and verifies that `attempts`, `first_failure_at`,
`last_error` and `needs_reauth` are stamped correctly after the fix.

Examples
────────
  # Dump state for tenant 1 / integration 3 (no Salla call):
  python scripts/diagnose_salla_token.py --tenant 1 --integration 3

  # Drive one refresh cycle and print before/after:
  python scripts/diagnose_salla_token.py --tenant 1 --integration 3 --refresh

  # Probe Salla without writing to DB:
  python scripts/diagnose_salla_token.py --tenant 1 --integration 3 --refresh --dry-run

Required environment
────────────────────
  DATABASE_URL          PostgreSQL DSN
  SALLA_CLIENT_ID       (only for --refresh)
  SALLA_CLIENT_SECRET   (only for --refresh)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    sp = str(_p)
    if sp not in sys.path:
        sys.path.insert(0, sp)


def _row_summary(intg, *, label: str = "") -> dict:
    cfg = intg.config or {}
    return {
        "label":                   label,
        "integration_id":          intg.id,
        "tenant_id":               intg.tenant_id,
        "enabled":                 intg.enabled,
        "external_store_id":       intg.external_store_id,
        "store_id":                cfg.get("store_id"),
        "store_name":              cfg.get("store_name"),
        "app_type":                cfg.get("app_type"),
        "token_source":            cfg.get("token_source") or cfg.get("api_key_source"),
        "has_access_token":        bool(cfg.get("api_key")),
        "has_refresh_token":       bool(cfg.get("refresh_token")),
        "expires_at":              cfg.get("expires_at") or cfg.get("token_expires_at"),
        "last_token_refresh_at":   cfg.get("last_token_refresh_at") or cfg.get("last_token_refresh"),
        "token_refresh_status":    cfg.get("token_refresh_status"),
        "token_refresh_error":     cfg.get("token_refresh_error"),
        "token_refresh_failed_at": cfg.get("token_refresh_failed_at"),
        "first_failure_at":        cfg.get("token_refresh_first_failed_at"),
        "refresh_attempts":        cfg.get("token_refresh_attempts", 0),
        "needs_reauth":            bool(cfg.get("needs_reauth")),
        "needs_reauth_reason":     cfg.get("needs_reauth_reason"),
        "needs_reauth_at":         cfg.get("needs_reauth_at"),
        "alert_sent_at":           cfg.get("token_reauth_alert_sent_at"),
        "superseded":              bool(cfg.get("superseded")),
        "superseded_by":           cfg.get("superseded_by_integration_id"),
        "alert_suppressed":        bool(cfg.get("alert_suppressed")),
        "no_auto_refresh":         bool(cfg.get("no_auto_refresh")),
        "no_auto_refresh_reason":  cfg.get("no_auto_refresh_reason"),
    }


def _print_section(title: str, payload) -> None:
    bar = "═" * (len(title) + 4)
    print(f"\n{bar}\n  {title}\n{bar}")
    if isinstance(payload, (dict, list)):
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(payload)


async def _refresh_via_admin_endpoint(integration_id: int, *, dry_run: bool) -> dict:
    """Drive the same code path the admin endpoint uses, but in-process."""
    from core.database import SessionLocal  # type: ignore
    from models import Integration  # type: ignore
    from routers import admin_salla_token

    # Force the gate flag for in-process invocation
    os.environ.setdefault("ENABLE_ADMIN_DEBUG", "true")

    # The endpoint is async and depends on `db: Session = Depends(get_db)`.
    # We invoke it directly for an in-process diagnostic.
    db = SessionLocal()
    try:
        # Bypass the FastAPI Path validator by calling the function directly.
        result = await admin_salla_token.salla_force_refresh_integration(
            integration_id=integration_id,
            secret=None,
            dry_run=dry_run,
            db=db,
        )
    finally:
        db.close()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant",      type=int, required=True, help="tenant_id")
    parser.add_argument("--integration", type=int, required=True, help="integration_id (Salla)")
    parser.add_argument("--refresh",     action="store_true", help="drive a full refresh cycle")
    parser.add_argument("--dry-run",     action="store_true", help="probe Salla without DB writes")
    args = parser.parse_args()

    if not os.environ.get("DATABASE_URL"):
        print("Set DATABASE_URL before running this script.")
        return 2

    from core.database import SessionLocal  # type: ignore
    from models import Integration  # type: ignore

    db = SessionLocal()
    try:
        intg = db.query(Integration).filter(Integration.id == args.integration).first()
        if not intg:
            print(f"integration #{args.integration} not found")
            return 3
        if intg.tenant_id != args.tenant:
            print(f"WARNING: integration #{args.integration} belongs to tenant {intg.tenant_id}, not {args.tenant}")
        if intg.provider != "salla":
            print(f"integration #{args.integration} provider={intg.provider} (expected salla)")
            return 4

        cfg = intg.config or {}
        store_id = cfg.get("store_id") or intg.external_store_id

        # 1) Selected row
        _print_section(f"Selected (integration #{args.integration})", _row_summary(intg, label="selected"))

        # 2) Sibling rows for the same store
        siblings = (
            db.query(Integration)
            .filter(
                Integration.provider == "salla",
                Integration.tenant_id == intg.tenant_id,
                Integration.id != intg.id,
            )
            .order_by(Integration.id.asc())
            .all()
        )
        sibling_summaries = []
        for s in siblings:
            scfg = s.config or {}
            ssid = scfg.get("store_id") or s.external_store_id
            same_store = str(ssid or "") == str(store_id or "")
            sibling_summaries.append({
                **_row_summary(s, label="sibling"),
                "same_store": same_store,
            })
        _print_section(f"Siblings for tenant {intg.tenant_id}", sibling_summaries or "(none)")

        # 3) Superseded check
        try:
            from core.salla_token_alerts import find_superseding_integration
            superseder = find_superseding_integration(db, intg)
        except Exception as exc:
            superseder = None
            print(f"\n(warn) superseded check failed: {exc}")
        if superseder:
            _print_section(
                "Superseded check",
                {
                    "result": "superseded",
                    "by_integration_id": superseder.id,
                    "note": "Owner alerts for the selected row should be auto-suppressed.",
                },
            )
        else:
            _print_section("Superseded check", {"result": "not_superseded"})

        # 4) Anomaly callout
        anomaly = bool(cfg.get("token_refresh_error")) and (cfg.get("token_refresh_attempts", 0) or 0) == 0
        if anomaly:
            _print_section(
                "ANOMALY",
                {
                    "issue": "refresh_attempts=0 with last_error set",
                    "diagnosis": (
                        "Legacy invalid_grant path stamped the error WITHOUT bumping the counter. "
                        "Run with --refresh to restamp via the patched code path."
                    ),
                },
            )

        # 5) Optional refresh
        if args.refresh:
            print("\n--- Driving refresh cycle ---")
            result = asyncio.run(
                _refresh_via_admin_endpoint(args.integration, dry_run=args.dry_run),
            )
            _print_section("Refresh result", result)
            print(
                "\nReload the row with: "
                f"python scripts/diagnose_salla_token.py --tenant {args.tenant} --integration {args.integration}",
            )

    finally:
        db.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
