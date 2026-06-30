#!/usr/bin/env python3
"""Read-only post-rollout verification for WhatsApp access_token encryption.

Never writes to the DB, never calls Meta, never prints tokens.

Usage (Railway Console):
    python backend/scripts/verify_wa_token_encryption_rollout.py
    python scripts/verify_wa_token_encryption_rollout.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, List, Optional

# ── Repo bootstrap ───────────────────────────────────────────────────────────
_SCRIPT = Path(__file__).resolve()
_REPO_ROOT = _SCRIPT.parents[2]
for _p in (_REPO_ROOT / "backend", _REPO_ROOT / "database"):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)


class RolloutVerification:
    with_token: int = 0
    enc1: int = 0
    plaintext_remaining: int = 0
    decrypt_ok: int = 0
    decrypt_fail: int = 0

    @property
    def passed(self) -> bool:
        return self.plaintext_remaining == 0 and self.decrypt_fail == 0


def _is_production() -> bool:
    env = (os.getenv("ENVIRONMENT") or os.getenv("ENV") or "").strip().lower()
    return env in {"prod", "production"}


def production_key_error() -> Optional[str]:
    """Return an error message when WA_TOKEN_ENC_KEY is missing/invalid in production."""
    if not _is_production():
        return None
    key = (os.getenv("WA_TOKEN_ENC_KEY") or "").strip()
    if not key:
        return "WA_TOKEN_ENC_KEY missing in production"
    try:
        from cryptography.fernet import Fernet  # noqa: PLC0415

        Fernet(key.encode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return f"WA_TOKEN_ENC_KEY invalid in production: {exc}"
    return None


def _decrypt_ok(stored: str, plain: str) -> bool:
    if not plain:
        return False
    if plain.startswith("enc1:"):
        return False
    # Meta / D360 tokens are long opaque strings; avoid printing them — length only.
    return len(plain.strip()) >= 10


def verify_connections(connections: List[Any]) -> RolloutVerification:
    from core.wa_token_crypto import is_encrypted_at_rest  # noqa: PLC0415
    from services.whatsapp_platform.wa_connection_secrets import read_access_token  # noqa: PLC0415

    result = RolloutVerification()
    for conn in connections:
        stored = str(getattr(conn, "access_token", "") or "").strip()
        if not stored:
            continue
        result.with_token += 1
        if is_encrypted_at_rest(stored):
            result.enc1 += 1
            try:
                plain = read_access_token(conn)
                if _decrypt_ok(stored, plain):
                    result.decrypt_ok += 1
                else:
                    result.decrypt_fail += 1
            except Exception:
                result.decrypt_fail += 1
        else:
            result.plaintext_remaining += 1
    return result


def _load_connections(db: Any) -> List[Any]:
    from models import WhatsAppConnection  # noqa: PLC0415

    return (
        db.query(WhatsAppConnection)
        .filter(
            WhatsAppConnection.access_token.isnot(None),
            WhatsAppConnection.access_token != "",
        )
        .all()
    )


def main() -> int:
    key_err = production_key_error()
    if key_err:
        print(f"status=FAIL")
        print(f"reason={key_err}")
        return 1

    from session import SessionLocal  # noqa: PLC0415

    db = SessionLocal()
    try:
        stats = verify_connections(_load_connections(db))
    finally:
        db.close()

    print(f"with_token={stats.with_token}")
    print(f"enc1={stats.enc1}")
    print(f"plaintext_remaining={stats.plaintext_remaining}")
    print(f"decrypt_ok={stats.decrypt_ok}")
    print(f"decrypt_fail={stats.decrypt_fail}")
    print(f"status={'PASS' if stats.passed else 'FAIL'}")
    return 0 if stats.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
