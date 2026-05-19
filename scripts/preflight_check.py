#!/usr/bin/env python3
"""
scripts/preflight_check.py
──────────────────────────
Boot-time guard that refuses to start the worker when a production deploy
is missing or misconfiguring a critical secret.

Usage
─────
Invoked from ``start.sh`` BEFORE ``uvicorn`` binds to ``$PORT``::

    python scripts/preflight_check.py || exit 1

The script:

* Exits 0 silently on any non-production environment (``ENVIRONMENT`` !=
  ``production``) — local dev / staging deploys boot without ceremony.
* Exits 0 with a green summary in production when every required secret
  is present AND not equal to a known placeholder.
* Exits 1 with a red, line-by-line diagnosis when a check fails. Each
  line is prefixed with ``[FAIL]`` and tells the operator exactly which
  variable to set. Railway logs show this directly.

Why a separate script (not just ``core.config``)?
──────────────────────────────────────────────────
``core/config.py`` raises in production when secrets are missing, but it
runs *inside* uvicorn's worker initialisation. The traceback appears in
the worker log AFTER uvicorn already bound the port, so the platform's
healthcheck briefly sees the port open and reports "deploy succeeded"
before the worker dies. Running this script first keeps the platform's
view of "deploy success" honest: a misconfigured prod deploy never
exposes a port at all.
"""
from __future__ import annotations

import os
import sys
from typing import List, Tuple


_FORBIDDEN_JWT_SECRETS = {
    "",
    "change-me",
    "change-me-to-a-long-random-string",
    "secret",
    "dev",
    "dev-secret",
    "nahla-dev",
}

_FORBIDDEN_ADMIN_PASSWORDS = {
    "",
    "change-me",
    "nahla-admin-2026",
    "12345678",
    "admin",
    "password",
}

_FORBIDDEN_WA_VERIFY_TOKENS = {
    "",
    "nahla2025",
    "verify-me",
    "test",
}


def _check(name: str, *, forbidden: set, min_length: int = 0) -> Tuple[bool, str]:
    """Return ``(ok, message)`` for a single env var."""
    raw = (os.environ.get(name) or "").strip()
    low = raw.lower()
    if low in forbidden:
        return False, (
            f"[FAIL] {name} is empty or set to a known placeholder "
            f"(value-prefix={raw[:4]!r}). Generate a strong random value and "
            f"set it in Railway -> Variables."
        )
    if min_length and len(raw) < min_length:
        return False, (
            f"[FAIL] {name} is shorter than the {min_length}-char minimum. "
            f"Generate a longer random value."
        )
    return True, f"[ ok ] {name}"


def _bool_env(name: str, default: str = "false") -> bool:
    return (os.environ.get(name, default) or default).strip().lower() in (
        "1", "true", "yes", "on",
    )


def main() -> int:
    env = (os.environ.get("ENVIRONMENT", "") or "").strip().lower()
    if env != "production":
        # Non-prod boots without ceremony — only production deploys are
        # blocked. Print one line so the operator can see preflight ran.
        print(f"[preflight] ENVIRONMENT={env or '(unset)'} - skipping production checks.")
        return 0

    results: List[Tuple[bool, str]] = [
        _check("JWT_SECRET",            forbidden=_FORBIDDEN_JWT_SECRETS,        min_length=32),
        _check("ADMIN_PASSWORD",        forbidden=_FORBIDDEN_ADMIN_PASSWORDS,    min_length=12),
        _check("WHATSAPP_VERIFY_TOKEN", forbidden=_FORBIDDEN_WA_VERIFY_TOKENS,   min_length=16),
    ]

    # ADMIN_EMAIL doesn't have a forbidden list — but it must be present and
    # contain ``@`` so the env-fallback admin path is wired correctly.
    admin_email = (os.environ.get("ADMIN_EMAIL", "") or "").strip()
    if not admin_email or "@" not in admin_email:
        results.append((False, "[FAIL] ADMIN_EMAIL is missing or malformed."))
    else:
        results.append((True, "[ ok ] ADMIN_EMAIL"))

    # DATABASE_URL is required for every meaningful workload.
    if not (os.environ.get("DATABASE_URL", "") or "").strip():
        results.append((False, "[FAIL] DATABASE_URL is missing."))
    else:
        results.append((True, "[ ok ] DATABASE_URL"))

    # Phase 1B: Zid webhook secret is OPTIONAL by default (audit-only)
    # but PROMOTED to required at boot when ZID_WEBHOOK_REQUIRED_AT_BOOT=true.
    # This lets ops dial up enforcement after the audit window closes.
    if _bool_env("ZID_WEBHOOK_REQUIRED_AT_BOOT", "false"):
        if not (os.environ.get("ZID_WEBHOOK_SECRET", "") or "").strip():
            results.append((False, (
                "[FAIL] ZID_WEBHOOK_SECRET is required when "
                "ZID_WEBHOOK_REQUIRED_AT_BOOT=true. Generate a strong "
                "random value, set it in Railway and in the Zid Partner "
                "Portal webhook configuration."
            )))
        else:
            results.append((True, "[ ok ] ZID_WEBHOOK_SECRET (required-at-boot)"))
    else:
        # Soft warning — not a fail. Still useful in audit mode so ops sees
        # the gap before promoting the flag.
        if not (os.environ.get("ZID_WEBHOOK_SECRET", "") or "").strip():
            results.append((True, (
                "[warn] ZID_WEBHOOK_SECRET is empty - audit-mode only; "
                "set it before flipping ZID_WEBHOOK_ENFORCE_SIGNATURE."
            )))

    failed = [msg for ok, msg in results if not ok]
    for ok, msg in results:
        print(msg)

    if failed:
        print()
        print(f"[preflight] {len(failed)} CRITICAL check(s) failed - refusing to boot.")
        print("[preflight] Fix the variables in Railway -> Variables, redeploy, and try again.")
        return 1

    print("[preflight] all production checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
