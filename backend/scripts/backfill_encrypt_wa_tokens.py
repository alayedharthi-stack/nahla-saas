#!/usr/bin/env python3
"""One-shot migration: encrypt legacy plaintext WhatsAppConnection.access_token rows."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# ── Repo bootstrap ───────────────────────────────────────────────────────────
# Works from repo root (/app) or backend root (/app/backend):
#   python backend/scripts/backfill_encrypt_wa_tokens.py --dry-run
#   python scripts/backfill_encrypt_wa_tokens.py --dry-run
_SCRIPT = Path(__file__).resolve()
_REPO_ROOT = _SCRIPT.parents[2]
for _p in (_REPO_ROOT / "backend", _REPO_ROOT / "database"):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)

from session import SessionLocal  # noqa: E402
from core.wa_token_crypto import is_encrypted_at_rest  # noqa: E402
from models import WhatsAppConnection  # noqa: E402
from services.whatsapp_platform.wa_connection_secrets import maybe_reencrypt_plaintext  # noqa: E402

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("backfill_encrypt_wa_tokens")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    db = SessionLocal()
    updated = 0
    skipped = 0
    try:
        rows = (
            db.query(WhatsAppConnection)
            .filter(
                WhatsAppConnection.access_token.isnot(None),
                WhatsAppConnection.access_token != "",
            )
            .all()
        )
        for conn in rows:
            if is_encrypted_at_rest(conn.access_token):
                skipped += 1
                continue
            if args.dry_run:
                logger.info("[dry-run] would encrypt tenant=%s conn_id=%s", conn.tenant_id, conn.id)
                updated += 1
                continue
            if maybe_reencrypt_plaintext(conn, tenant_id=conn.tenant_id):
                updated += 1
        if not args.dry_run:
            db.commit()
    finally:
        db.close()

    logger.info("done updated=%s skipped_already_encrypted=%s dry_run=%s", updated, skipped, args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
