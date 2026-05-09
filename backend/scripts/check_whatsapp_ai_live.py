"""Operator verification for migration 0047 (whatsapp_ai_live_since).

Usage (on Railway shell or locally with DATABASE_URL set):

    python backend/scripts/check_whatsapp_ai_live.py            # all tenants
    python backend/scripts/check_whatsapp_ai_live.py --tenant 33

What it shows
-------------
1. Whether the new columns exist (migration 0047 ran).
2. ``whatsapp_ai_live_since`` per connected tenant.
3. Recent inbound rows for the chosen tenant with their
   ``message_origin`` / ``historical_import`` classification + the
   WhatsApp business timestamp recorded in metadata. Useful to confirm
   that the post-migration traffic is flowing through the live path.

Read-only — never writes to the DB.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "database"))

from sqlalchemy import create_engine, text  # noqa: E402


def _engine():
    url = os.getenv("DATABASE_URL")
    if not url:
        raise SystemExit(
            "DATABASE_URL is not set. On Railway, run inside the backend "
            "service shell so it inherits the env vars."
        )
    return create_engine(url)


def _columns_present(conn) -> Dict[str, bool]:
    rows = conn.execute(
        text(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'whatsapp_connections'
              AND column_name IN (
                'whatsapp_ai_live_since',
                'whatsapp_history_sync_status',
                'history_sync_started_at',
                'history_sync_completed_at',
                'synced_conversations_count',
                'synced_messages_count'
              )
            """
        )
    ).fetchall()
    found = {r[0] for r in rows}
    return {
        c: c in found
        for c in (
            "whatsapp_ai_live_since",
            "whatsapp_history_sync_status",
            "history_sync_started_at",
            "history_sync_completed_at",
            "synced_conversations_count",
            "synced_messages_count",
        )
    }


def _cutoffs(conn, tenant_id: int | None):
    sql = text(
        """
        SELECT tenant_id, status, connected_at, whatsapp_ai_live_since,
               whatsapp_history_sync_status
          FROM whatsapp_connections
         WHERE (:t IS NULL OR tenant_id = :t)
         ORDER BY tenant_id
        """
    )
    return conn.execute(sql, {"t": tenant_id}).fetchall()


def _recent_inbound(conn, tenant_id: int, limit: int = 12):
    sql = text(
        """
        SELECT id, conversation_id, created_at,
               (metadata->>'message_origin')        AS message_origin,
               (metadata->>'historical_import')     AS historical_import,
               (metadata->>'whatsapp_timestamp')    AS wa_ts,
               left(coalesce(body, ''), 80)         AS preview
          FROM message_events
         WHERE tenant_id = :t
           AND direction = 'inbound'
         ORDER BY id DESC
         LIMIT :n
        """
    )
    return conn.execute(sql, {"t": tenant_id, "n": limit}).fetchall()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--tenant", type=int, default=None)
    p.add_argument("--inbound-limit", type=int, default=12)
    args = p.parse_args()

    eng = _engine()
    with eng.connect() as conn:
        cols = _columns_present(conn)
        print("== migration 0047 columns ==")
        for k, v in cols.items():
            mark = "OK" if v else "MISSING"
            print(f"  [{mark}] {k}")
        if not all(cols.values()):
            print(
                "\nMigration 0047 is not fully applied. Run "
                "`alembic -c database/alembic.ini upgrade head` first."
            )
            return 1

        print("\n== whatsapp_ai_live_since per connection ==")
        rows = _cutoffs(conn, args.tenant)
        if not rows:
            print("  (no rows match)")
        for r in rows:
            tid, status, conn_at, cutoff, sync_status = r
            print(
                f"  tenant={tid} status={status} "
                f"connected_at={conn_at} cutoff={cutoff} "
                f"history_sync={sync_status}"
            )

        if args.tenant is not None:
            print("\n== recent inbound classification ==")
            inbound = _recent_inbound(conn, args.tenant, args.inbound_limit)
            if not inbound:
                print("  (no inbound rows)")
            for r in inbound:
                mid, cid, ts, origin, hist, wa_ts, preview = r
                origin = origin or "(unset)"
                hist = hist or "(unset)"
                wa_ts = wa_ts or "(unset)"
                print(
                    f"  msg_id={mid} convo={cid} created_at={ts} "
                    f"origin={origin} historical_import={hist} "
                    f"wa_ts={wa_ts} preview={preview!r}"
                )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
