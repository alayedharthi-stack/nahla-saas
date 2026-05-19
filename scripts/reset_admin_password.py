#!/usr/bin/env python3
"""
scripts/reset_admin_password.py
───────────────────────────────
Offline replacement for the removed ``GET/POST /admin/debug/reset-admin``
HTTP endpoint. Writes a bcrypt hash directly into the ``users`` table via
``DATABASE_URL`` and never opens an HTTP surface.

Usage (Railway shell)
─────────────────────
    railway run python scripts/reset_admin_password.py \\
        --email admin@nahlah.ai \\
        --password "$(openssl rand -base64 24)"

The script will:

* Connect to ``DATABASE_URL`` (from Railway env).
* Create or update ``users(email=...)`` with ``role='admin'`` +
  ``is_active=True`` + bcrypt-hashed password.
* Ensure ``tenants(id=1)`` exists (admin convention) and link the row.
* Print a short JSON summary so the operator can confirm the change.

Safety
──────
* Refuses to run when ``--password`` is shorter than 12 characters.
* Refuses to run when the password matches a known forbidden default
  (``12345678``, ``nahla-admin-2026``, etc.).
* Always commits in a single transaction; rolls back on any error.

This script is intentionally NOT importable as a router — calling it
from anywhere other than a privileged shell breaks the security model
that replaced the HTTP endpoint.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone


_FORBIDDEN_PASSWORDS = {
    "",
    "12345678",
    "nahla-admin-2026",
    "admin",
    "password",
    "change-me",
}


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Create or reset the platform admin user.")
    p.add_argument("--email",     required=True,  help="Admin email (e.g. admin@nahlah.ai)")
    p.add_argument("--password",  required=True,  help="New password (min 12 chars, must not be a placeholder)")
    p.add_argument("--tenant-id", default=1, type=int, help="Tenant id for the admin (default 1)")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    email = (args.email or "").strip().lower()
    pw    = args.password or ""

    if not email or "@" not in email:
        print(f"[reset-admin] FAIL — invalid email: {email!r}", file=sys.stderr)
        return 2
    if len(pw) < 12:
        print("[reset-admin] FAIL — password must be at least 12 characters.", file=sys.stderr)
        return 2
    if pw.strip().lower() in _FORBIDDEN_PASSWORDS:
        print("[reset-admin] FAIL — password matches a forbidden placeholder.", file=sys.stderr)
        return 2

    db_url = (os.environ.get("DATABASE_URL", "") or "").strip()
    if not db_url:
        print("[reset-admin] FAIL — DATABASE_URL is not set.", file=sys.stderr)
        return 2

    # Heavy imports below the early validation so the script fails fast
    # on bad input before paying the SQLAlchemy import cost.
    try:
        import bcrypt  # noqa: PLC0415
        from sqlalchemy import create_engine, text  # noqa: PLC0415
    except ImportError as exc:
        print(f"[reset-admin] FAIL — missing dependency: {exc}", file=sys.stderr)
        return 3

    pw_hash = bcrypt.hashpw(pw[:72].encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    now_iso = datetime.now(timezone.utc).isoformat()

    engine = create_engine(db_url, pool_pre_ping=True, future=True)
    summary = {"email": email, "tenant_id": args.tenant_id, "action": None}
    try:
        with engine.begin() as conn:
            # Ensure the tenant row exists. ``ON CONFLICT DO NOTHING``
            # keeps a real merchant tenant intact if it happens to use
            # this id (the admin convention is tenant_id=1).
            conn.execute(text(
                "INSERT INTO tenants (id, name, is_active, created_at) "
                "VALUES (:tid, :name, TRUE, :now) "
                "ON CONFLICT (id) DO NOTHING"
            ), {"tid": args.tenant_id, "name": "Nahla Admin", "now": now_iso})

            # Upsert the admin user.
            existing = conn.execute(
                text("SELECT id FROM users WHERE email = :e"),
                {"e": email},
            ).first()
            if existing:
                conn.execute(text(
                    "UPDATE users "
                    "SET password_hash = :pw, role = 'admin', is_active = TRUE, "
                    "    tenant_id = COALESCE(tenant_id, :tid) "
                    "WHERE email = :e"
                ), {"pw": pw_hash, "e": email, "tid": args.tenant_id})
                summary["action"]  = "updated"
                summary["user_id"] = existing[0]
            else:
                row = conn.execute(text(
                    "INSERT INTO users "
                    "(username, email, password_hash, role, is_active, created_at, tenant_id) "
                    "VALUES (:u, :e, :pw, 'admin', TRUE, :now, :tid) "
                    "RETURNING id"
                ), {
                    "u": email, "e": email, "pw": pw_hash,
                    "now": now_iso, "tid": args.tenant_id,
                }).first()
                summary["action"]  = "created"
                summary["user_id"] = row[0] if row else None
    except Exception as exc:  # noqa: BLE001
        print(f"[reset-admin] FAIL — DB error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 4

    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
