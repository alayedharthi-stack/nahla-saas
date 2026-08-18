"""
backfill_coexistence_record.py
──────────────────────────────
One-shot backfill script for a 360dialog Coexistence integration record
where activation finished without a WABA ID / channel ID, leaving the
merchant page reporting `missing_waba_id` while the owner panel happily
reports "مفعّل".

It writes the canonical fields to `whatsapp_connections` for a given
tenant, mirrors the values into `extra_metadata.provider_details`, and
promotes the connection to `connected` if the record is now complete.

Usage (Railway shell, from repo root):

    python backend/scripts/backfill_coexistence_record.py \
        --tenant 33 \
        --waba-id 112119171976402 \
        --channel-id ZVXPG8CH \
        --phone "+966555906901"

Optional:
    --phone-number-id <id>   override; defaults to channel-id (360dialog
                              uses the channel id as phone_number_id in
                              the WABA-V2 cluster).
    --display-name <text>    override the merchant's business name.
    --no-promote             leave status untouched even if record is now
                              complete (default: promote to "connected").
    --dry-run                print the planned changes without writing.

The script is idempotent — re-running it never overwrites a non-empty
field with the same value, and it never clears an existing API key.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict

# ── Path bootstrap ────────────────────────────────────────────────────────────
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
for p in (
    os.path.join(ROOT, "backend"),
    os.path.join(ROOT, "database"),
):
    if p not in sys.path:
        sys.path.insert(0, p)

from sqlalchemy.orm.attributes import flag_modified  # noqa: E402

from core.database import SessionLocal               # noqa: E402
from models import WhatsAppConnection                # noqa: E402
from services.whatsapp_platform.provider_utils import (  # noqa: E402
    WHATSAPP_CONNECTION_TYPE_COEXISTENCE,
    WHATSAPP_PROVIDER_360DIALOG,
)

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("backfill_coexistence_record")


def backfill(
    *,
    tenant_id: int,
    waba_id: str,
    channel_id: str,
    phone_number: str,
    phone_number_id: str | None,
    display_name: str | None,
    promote: bool,
    dry_run: bool,
) -> int:
    db = SessionLocal()
    try:
        conn: WhatsAppConnection | None = (
            db.query(WhatsAppConnection)
            .filter(WhatsAppConnection.tenant_id == tenant_id)
            .first()
        )
        if not conn:
            log.error("No WhatsAppConnection row for tenant_id=%s — aborting.", tenant_id)
            return 1

        pnid = (phone_number_id or channel_id).strip()

        # ── Plan the diff ────────────────────────────────────────────
        plan: Dict[str, Any] = {}
        if not conn.whatsapp_business_account_id:
            plan["whatsapp_business_account_id"] = waba_id
        if not conn.phone_number_id:
            plan["phone_number_id"] = pnid
        if not conn.phone_number:
            plan["phone_number"] = phone_number
        if display_name and not conn.business_display_name:
            plan["business_display_name"] = display_name

        meta = dict(conn.extra_metadata or {})
        provider_details = dict(meta.get("provider_details") or {})
        meta_changes: Dict[str, Any] = {}
        if not provider_details.get("channel_id"):
            meta_changes["provider_details.channel_id"] = channel_id
        if not provider_details.get("phone_number_id"):
            meta_changes["provider_details.phone_number_id"] = pnid

        promoted = False
        had_token = bool(conn.access_token)
        will_be_complete = (
            (conn.whatsapp_business_account_id or plan.get("whatsapp_business_account_id"))
            and (conn.phone_number_id or plan.get("phone_number_id"))
            and (conn.phone_number or plan.get("phone_number"))
            and had_token
        )
        if promote and will_be_complete and conn.status != "connected":
            promoted = True

        log.info("── Plan for tenant_id=%s ──", tenant_id)
        log.info("Current record:")
        log.info(
            json.dumps({
                "id":                conn.id,
                "provider":          conn.provider,
                "connection_type":   conn.connection_type,
                "status":            conn.status,
                "waba_id":           conn.whatsapp_business_account_id,
                "phone_number_id":   conn.phone_number_id,
                "phone_number":      conn.phone_number,
                "display_name":      conn.business_display_name,
                "has_api_key":       had_token,
                "channel_id":        provider_details.get("channel_id"),
            }, ensure_ascii=False, indent=2),
        )
        log.info("Field changes: %s", json.dumps(plan, ensure_ascii=False, indent=2) if plan else "(none)")
        log.info("Metadata changes: %s", json.dumps(meta_changes, ensure_ascii=False, indent=2) if meta_changes else "(none)")
        log.info("Promote to 'connected': %s", promoted)

        if dry_run:
            log.info("--dry-run set, no DB writes performed.")
            return 0

        # ── Apply ────────────────────────────────────────────────────
        for k, v in plan.items():
            setattr(conn, k, v)

        # Make sure provider/connection_type are correct even if the
        # record was created via some legacy path.
        conn.provider        = WHATSAPP_PROVIDER_360DIALOG
        conn.connection_type = WHATSAPP_CONNECTION_TYPE_COEXISTENCE
        if not conn.token_type:
            conn.token_type = "dialog360_api_key"

        provider_details.setdefault("channel_id", channel_id)
        provider_details.setdefault("phone_number_id", pnid)
        provider_details["webhook_url"]          = "https://api.nahlah.ai/webhook/whatsapp/360dialog"
        provider_details["coexistence_url"]      = "https://api.nahlah.ai/webhook/whatsapp/360dialog/coexistence"
        provider_details["status_url"]           = "https://api.nahlah.ai/webhook/whatsapp/360dialog/status"
        provider_details["internal_header_name"] = "X-Nahla-Coexistence-Secret"
        meta["provider_details"] = provider_details

        coex = dict(meta.get("coexistence") or {})
        coex["last_backfill"] = {
            "at":         datetime.now(timezone.utc).isoformat(),
            "script":     "backfill_coexistence_record.py",
            "filled":     list(plan.keys()) + list(meta_changes.keys()),
        }
        meta["coexistence"] = coex
        conn.extra_metadata = meta
        flag_modified(conn, "extra_metadata")

        if promoted:
            conn.sending_enabled = True
            conn.last_error = None
            from core.whatsapp_connection_finalization import (  # noqa: PLC0415
                WhatsAppConnectionFinalizationError,
                finalize_successful_whatsapp_connection,
            )
            try:
                finalize_successful_whatsapp_connection(db, conn)
            except WhatsAppConnectionFinalizationError as exc:
                log.error("✗ Canonical finalization failed: %s", exc)
                return 1
        else:
            db.commit()
        log.info("✓ Backfill applied.")
        return 0
    finally:
        db.close()


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backfill a 360dialog Coexistence integration record.")
    p.add_argument("--tenant",         type=int, required=True, help="Target tenant_id (e.g. 33).")
    p.add_argument("--waba-id",        type=str, required=True, help="WhatsApp Business Account ID.")
    p.add_argument("--channel-id",     type=str, required=True, help="360dialog channel_id (e.g. ZVXPG8CH).")
    p.add_argument("--phone",          type=str, required=True, help="Display phone number, e.g. +966555906901.")
    p.add_argument("--phone-number-id", dest="phone_number_id", type=str, default=None,
                   help="Override phone_number_id; defaults to --channel-id.")
    p.add_argument("--display-name",   dest="display_name", type=str, default=None)
    p.add_argument("--no-promote",     dest="promote", action="store_false", default=True,
                   help="Don't auto-promote status to 'connected' even if record is complete.")
    p.add_argument("--dry-run",        action="store_true", help="Print plan without writing.")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    rc = backfill(
        tenant_id       = args.tenant,
        waba_id         = args.waba_id,
        channel_id      = args.channel_id,
        phone_number    = args.phone,
        phone_number_id = args.phone_number_id,
        display_name    = args.display_name,
        promote         = args.promote,
        dry_run         = args.dry_run,
    )
    sys.exit(rc)
