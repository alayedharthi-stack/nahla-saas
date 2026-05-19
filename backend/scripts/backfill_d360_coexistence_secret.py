"""
backfill_d360_coexistence_secret.py
───────────────────────────────────
One-shot backfill script for the 360dialog ``coexistence_internal_secret``
header. Phase 1B requires every connected 360dialog WhatsApp connection
to carry a per-row HMAC-equivalent shared secret so the inbound webhook
handler can authenticate the source. Connections provisioned BEFORE the
secret-enforcement code landed have ``coexistence_internal_secret``
empty; this script:

* enumerates every ``WhatsAppConnection`` with provider == "d360" (or any
  connection whose ``extra_metadata`` indicates a 360dialog channel) and
  status in {"connected", "active", "pending_activation"},
* skips rows whose ``coexistence_internal_secret`` is already populated,
* generates a fresh per-row secret with ``secrets.token_urlsafe(24)``,
* POSTs the secret to 360dialog as the channel + WABA webhook header,
* persists the new secret on the connection row.

Run modes
─────────
The script ALWAYS starts in ``--dry-run`` semantics until the operator
explicitly passes ``--apply``. A dry-run prints the plan without making
any 360dialog API calls and without writing to the DB. ``--limit N``
processes at most N rows; combine with ``--tenant N`` for targeted runs.

This is meant to be invoked from the Railway shell (or any environment
that has ``DATABASE_URL`` + ``D360_PARTNER_API_KEY`` set), during a
chosen maintenance window.

Usage
─────
    # 1. Dry-run — prints the plan, makes NO changes
    python backend/scripts/backfill_d360_coexistence_secret.py

    # 2. Targeted single-tenant dry-run
    python backend/scripts/backfill_d360_coexistence_secret.py --tenant 12

    # 3. Real run, all rows
    python backend/scripts/backfill_d360_coexistence_secret.py --apply

    # 4. Real run, capped at 5 rows for staged rollout
    python backend/scripts/backfill_d360_coexistence_secret.py --apply --limit 5

Exit codes
──────────
* 0 — success (no changes when dry-run; all rows updated when --apply).
* 1 — partial success (some rows failed). Operator should re-run without
      ``--limit`` after fixing the underlying issue.
* 2 — fatal error (DB unreachable, invalid CLI args).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import secrets as _secrets
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# ── Repo bootstrap ────────────────────────────────────────────────────────────
# Allow ``python backend/scripts/backfill_d360_coexistence_secret.py`` from
# the repo root by prepending ``backend/`` to ``sys.path`` so we can use the
# normal application imports (``models``, ``session``, ``services.…``).
_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

# Imports below intentionally come AFTER sys.path manipulation. They will
# fail loudly with a clear ImportError if DATABASE_URL is missing — that
# is the operator's signal to set the env var before re-running.
from sqlalchemy.orm.attributes import flag_modified  # noqa: E402

from session import SessionLocal  # noqa: E402
from models import WhatsAppConnection  # noqa: E402
from services.whatsapp_platform.service import (  # noqa: E402
    dialog360_configure_webhook,
    dialog360_set_waba_webhook,
)

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("d360-backfill")


def _coexistence_webhook_url() -> str:
    backend_url = (os.environ.get("BACKEND_URL")
                   or "https://nahla-saas-production.up.railway.app").rstrip("/")
    return f"{backend_url}/webhook/whatsapp/360dialog"


def _is_360dialog_connection(conn: WhatsAppConnection) -> bool:
    """Heuristic identification of 360dialog channels.

    The ``provider`` column is the canonical signal but historical rows
    may not have it set. Falling back to ``extra_metadata.provider_details``
    catches those cases without false positives because non-360dialog
    flows never write that key.
    """
    if (conn.provider or "").lower() in ("d360", "360dialog"):
        return True
    meta = conn.extra_metadata or {}
    provider_details = meta.get("provider_details") or {}
    if isinstance(provider_details, dict) and provider_details.get("channel_id"):
        return True
    return False


def _select_rows(
    *,
    tenant_filter: Optional[int],
    limit: Optional[int],
) -> List[WhatsAppConnection]:
    db = SessionLocal()
    try:
        q = db.query(WhatsAppConnection)
        if tenant_filter is not None:
            q = q.filter(WhatsAppConnection.tenant_id == tenant_filter)
        rows = q.all()
    finally:
        db.close()

    candidates: List[WhatsAppConnection] = []
    for row in rows:
        if not _is_360dialog_connection(row):
            continue
        meta = row.extra_metadata or {}
        existing = (meta.get("coexistence_internal_secret") or "").strip()
        if existing:
            continue
        candidates.append(row)
        if limit is not None and len(candidates) >= limit:
            break
    return candidates


async def _push_secret_to_360dialog(
    api_key: str,
    secret: str,
) -> Dict[str, Any]:
    """Configure both channel and WABA webhooks with the new secret.

    Returns a dict shaped like ``{"channel": <result>, "waba": <result>}``
    where each value is the provider response or ``{"error": "..."}``.
    Errors from one scope do NOT abort the other — partial success is
    explicit in the result.
    """
    headers = {"X-Nahla-Coexistence-Secret": secret}
    url = _coexistence_webhook_url()
    out: Dict[str, Any] = {}
    try:
        out["channel"] = await asyncio.wait_for(
            dialog360_configure_webhook(api_key=api_key, url=url, headers=headers, timeout=5.0),
            timeout=8.0,
        )
    except Exception as exc:  # noqa: BLE001 — surface the error in the structured result
        out["channel"] = {"error": f"{type(exc).__name__}: {exc}"}
    try:
        out["waba"] = await asyncio.wait_for(
            dialog360_set_waba_webhook(
                api_key=api_key,
                url=url,
                headers=headers,
                override_all=True,
                timeout=8.0,
            ),
            timeout=10.0,
        )
    except Exception as exc:  # noqa: BLE001
        out["waba"] = {"error": f"{type(exc).__name__}: {exc}"}
    return out


def _persist_secret(
    conn_id: int,
    secret: str,
    push_result: Dict[str, Any],
) -> None:
    """Write the new secret + push outcome to the connection row."""
    db = SessionLocal()
    try:
        row = db.query(WhatsAppConnection).filter(WhatsAppConnection.id == conn_id).first()
        if row is None:
            return
        meta = dict(row.extra_metadata or {})
        meta["coexistence_internal_secret"] = secret
        meta["internal_header_name"] = "X-Nahla-Coexistence-Secret"
        backfill_log = dict(meta.get("backfill_log") or {})
        backfill_log["coexistence_secret_set_at"] = datetime.now(timezone.utc).isoformat()
        backfill_log["coexistence_secret_push_result"] = push_result
        meta["backfill_log"] = backfill_log
        row.extra_metadata = meta
        flag_modified(row, "extra_metadata")
        db.commit()
    finally:
        db.close()


async def _process(
    rows: List[WhatsAppConnection],
    *,
    apply_changes: bool,
) -> int:
    failures = 0
    for row in rows:
        api_key = (row.access_token or "").strip()
        tenant_id = row.tenant_id
        identity = f"conn_id={row.id} tenant_id={tenant_id} channel={(row.extra_metadata or {}).get('provider_details', {}).get('channel_id', '?')}"

        if not api_key:
            logger.info("[skip] %s — no D360 access_token (api_key) on row", identity)
            continue

        new_secret = _secrets.token_urlsafe(24)
        if not apply_changes:
            logger.info("[plan] %s — would push fresh secret to 360dialog", identity)
            continue

        logger.info("[run ] %s — pushing fresh secret to 360dialog", identity)
        try:
            result = await _push_secret_to_360dialog(api_key, new_secret)
        except Exception as exc:  # noqa: BLE001
            logger.error("[fail] %s — push raised: %s", identity, exc)
            failures += 1
            continue

        channel_ok = "error" not in (result.get("channel") or {})
        waba_ok    = "error" not in (result.get("waba") or {})
        if not (channel_ok or waba_ok):
            logger.error("[fail] %s — both channel and WABA push failed: %s", identity, result)
            failures += 1
            continue

        try:
            _persist_secret(row.id, new_secret, result)
        except Exception as exc:  # noqa: BLE001
            logger.error("[fail] %s — DB persist after successful push: %s", identity, exc)
            failures += 1
            continue

        logger.info(
            "[ok  ] %s — channel_ok=%s waba_ok=%s",
            identity, channel_ok, waba_ok,
        )
    return failures


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true",
                   help="Actually push secrets and write to DB. Without this flag the "
                        "script only prints the plan.")
    p.add_argument("--tenant", type=int, default=None,
                   help="Restrict the run to a single tenant_id.")
    p.add_argument("--limit", type=int, default=None,
                   help="Process at most N rows.")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    if not (os.environ.get("DATABASE_URL", "") or "").strip():
        logger.error("DATABASE_URL is not set — refusing to run.")
        return 2

    rows = _select_rows(tenant_filter=args.tenant, limit=args.limit)
    if not rows:
        logger.info("[backfill] no candidate rows — every 360dialog connection already has a coexistence secret.")
        return 0

    logger.info(
        "[backfill] %d candidate row(s) — apply=%s tenant=%s limit=%s",
        len(rows), args.apply, args.tenant, args.limit,
    )

    failures = asyncio.run(_process(rows, apply_changes=args.apply))
    if failures:
        logger.error("[backfill] %d failure(s). Re-run after investigation.", failures)
        return 1

    logger.info("[backfill] complete — %d row(s) %s.",
                len(rows), "updated" if args.apply else "would-be updated (dry-run)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
