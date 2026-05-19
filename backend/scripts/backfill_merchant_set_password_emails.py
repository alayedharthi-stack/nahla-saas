"""
backfill_merchant_set_password_emails.py
────────────────────────────────────────
One-shot backfill script for existing Salla / Zid merchants who were
auto-created BEFORE the welcome-email flow landed. They have:

  * a ``User`` row with role="merchant" and a random bcrypt
    ``password_hash`` they cannot guess
  * an ``Integration`` row linking them to a Salla/Zid store
  * NEVER received a "set your password" email

This script:

  1. Enumerates ``Integration`` rows where ``provider in {salla, zid}``
     AND ``enabled = true``.
  2. For each, resolves the primary merchant ``User`` on the same tenant
     (matching by ``config['salla_owner_email']`` first, then any user
     on the tenant as a fallback).
  3. Skips rows where:
       * the email is a derived placeholder (``@salla-merchant.nahlah.ai``
         or ``@zid-merchant.nahlah.ai``) — no real inbox to deliver to
       * the user has already received a welcome token within the
         configured cooldown (default 14 days) — prevents accidental
         re-spamming on multiple runs
       * ``--tenant N`` was passed and this row is on a different tenant
  4. Issues a single-use ``PasswordSetupToken`` (purpose="welcome",
     issued_via="backfill_set_password") and dispatches the welcome
     email synchronously via the same ``send_email`` helper used by the
     OAuth handler.
  5. Records a per-row audit event so subsequent runs can see what's
     already been processed.

Run modes
─────────
Always starts in DRY-RUN (no DB writes, no emails sent) until ``--apply``
is passed. Use ``--tenant N`` to target a single tenant first, then
``--limit M`` for staged rollout, then ``--apply`` against everyone.

Usage
─────
    # 1. Dry-run all merchants
    python backend/scripts/backfill_merchant_set_password_emails.py

    # 2. Targeted dry-run
    python backend/scripts/backfill_merchant_set_password_emails.py --tenant 12

    # 3. Send to a single tenant (real)
    python backend/scripts/backfill_merchant_set_password_emails.py --tenant 12 --apply

    # 4. Bulk run capped at 50 rows
    python backend/scripts/backfill_merchant_set_password_emails.py --apply --limit 50

Exit codes
──────────
* 0 — success
* 1 — partial success (some rows failed to email; details in stdout)
* 2 — fatal error (DB unreachable, missing env vars)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Path bootstrap so this script runs from any cwd ─────────────────────────
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "backend"))

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("backfill_set_password")


# ── CLI ─────────────────────────────────────────────────────────────────────
def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--apply",  action="store_true",
                   help="Actually issue tokens + send emails. Without this flag the script is a dry run.")
    p.add_argument("--tenant", type=int, default=None,
                   help="Process only the given tenant_id.")
    p.add_argument("--provider", choices=["salla", "zid", "all"], default="all",
                   help="Filter by integration provider. Default: all.")
    p.add_argument("--limit",  type=int, default=0,
                   help="Process at most N rows. 0 = no limit.")
    p.add_argument("--cooldown-days", type=int, default=14,
                   help="Skip merchants who received a welcome token within this many days.")
    return p.parse_args()


# ── Main ─────────────────────────────────────────────────────────────────────
async def main() -> int:
    args = _parse_args()

    try:
        from core.database import SessionLocal
        from core.merchant_provisioning import _DASHBOARD_ORIGIN_FALLBACK  # noqa: F401  - sentinel
    except Exception:
        # _DASHBOARD_ORIGIN_FALLBACK isn't a real import — we just want to
        # ensure the module loads cleanly. Fall through to DB import only.
        from core.database import SessionLocal

    try:
        from core.config import DASHBOARD_URL
        from core.password_setup import issue_token as issue_set_password_token
        from core.notifications import email_set_password, send_email
        from models import Integration, PasswordSetupToken, User
    except Exception as exc:
        logger.error("Import failed (env not configured?): %s", exc)
        return 2

    dashboard_origin = (DASHBOARD_URL or "https://app.nahlah.ai").rstrip("/")
    cooldown_cutoff  = datetime.now(timezone.utc) - timedelta(days=args.cooldown_days)

    db = SessionLocal()
    try:
        q = db.query(Integration).filter(Integration.enabled == True)  # noqa: E712
        if args.provider == "all":
            q = q.filter(Integration.provider.in_(["salla", "zid"]))
        else:
            q = q.filter(Integration.provider == args.provider)
        if args.tenant is not None:
            q = q.filter(Integration.tenant_id == args.tenant)

        rows = q.order_by(Integration.id.asc()).all()
        if args.limit and args.limit > 0:
            rows = rows[: args.limit]

        logger.info(
            "Found %d candidate integration row(s) | provider=%s tenant=%s apply=%s",
            len(rows), args.provider, args.tenant, args.apply,
        )

        sent      = 0
        skipped   = 0
        failed    = 0
        processed_users: set[int] = set()

        for integ in rows:
            cfg     = dict(integ.config or {})
            stored  = (cfg.get("salla_owner_email") or "").strip().lower()
            tenant  = integ.tenant_id

            user = None
            if stored:
                user = (
                    db.query(User)
                    .filter(User.tenant_id == tenant, User.email == stored)
                    .first()
                )
            if user is None:
                user = (
                    db.query(User)
                    .filter(User.tenant_id == tenant, User.role == "merchant")
                    .order_by(User.id.asc())
                    .first()
                )

            if user is None:
                logger.warning(
                    "[skip] no merchant user on tenant=%s integration=%s — orphan integration",
                    tenant, integ.id,
                )
                skipped += 1
                continue

            if user.id in processed_users:
                # Same user already processed via a different integration row.
                skipped += 1
                continue
            processed_users.add(user.id)

            email = (user.email or "").strip().lower()
            if not email or email.endswith("@salla-merchant.nahlah.ai") or email.endswith("@zid-merchant.nahlah.ai"):
                logger.info(
                    "[skip] derived placeholder email | user=%s email=%s",
                    user.id, email,
                )
                skipped += 1
                continue

            recent = (
                db.query(PasswordSetupToken)
                .filter(
                    PasswordSetupToken.user_id == user.id,
                    PasswordSetupToken.purpose == "welcome",
                    PasswordSetupToken.created_at >= cooldown_cutoff,
                )
                .first()
            )
            if recent is not None:
                logger.info(
                    "[skip] cooldown active | user=%s last_token_at=%s",
                    user.id, recent.created_at,
                )
                skipped += 1
                continue

            store_name = (cfg.get("store_name") or "متجرك")
            source     = "سلة" if integ.provider == "salla" else "زد"

            if not args.apply:
                logger.info(
                    "[dry] would email user=%s tenant=%s provider=%s store=%r",
                    user.id, tenant, integ.provider, store_name,
                )
                sent += 1
                continue

            try:
                raw_token = issue_set_password_token(
                    db, user,
                    purpose="welcome",
                    issued_via="backfill_set_password",
                )
            except Exception as exc:
                logger.exception("[fail] issue_token failed | user=%s exc=%s", user.id, exc)
                failed += 1
                continue

            url = f"{dashboard_origin}/set-password?token={raw_token}"
            try:
                ok = await send_email(
                    to      = email,
                    subject = "أهلاً بك في نحلة — اضبط كلمة مرورك",
                    html    = email_set_password(
                        store_name       = store_name,
                        email            = email,
                        set_password_url = url,
                        dashboard_url    = dashboard_origin,
                        source_label     = source,
                    ),
                )
            except Exception as exc:
                logger.exception("[fail] send_email crashed | user=%s exc=%s", user.id, exc)
                failed += 1
                continue

            if ok:
                logger.info("[sent] user=%s email=%s tenant=%s", user.id, email, tenant)
                sent += 1
            else:
                logger.warning("[fail] provider returned failure | user=%s email=%s", user.id, email)
                failed += 1

        logger.info("DONE | sent=%d skipped=%d failed=%d apply=%s", sent, skipped, failed, args.apply)
        if failed > 0 and args.apply:
            return 1
        return 0
    finally:
        try:
            db.close()
        except Exception:  # noqa: silent-ok — db.close() best-effort during script teardown
            pass


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
