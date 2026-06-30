"""
Central read/write for WhatsAppConnection.access_token.

All persistence goes through ``store_access_token`` (encrypts).
All operational use goes through ``read_access_token`` (decrypts).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from core.wa_token_crypto import (
    decrypt_access_token,
    encrypt_access_token,
    is_encrypted_at_rest,
    token_tail,
)

logger = logging.getLogger("nahla.wa_connection_secrets")


def access_token_present(conn: Any) -> bool:
    return bool(str(getattr(conn, "access_token", "") or "").strip())


def read_access_token(conn: Any) -> str:
    """Plaintext token for Meta / 360dialog API calls only."""
    stored = getattr(conn, "access_token", None)
    return decrypt_access_token(stored)


def store_access_token(conn: Any, plaintext: str | None) -> None:
    """Encrypt and persist. ``None`` or empty clears the column."""
    if not plaintext or not str(plaintext).strip():
        conn.access_token = None
        return
    conn.access_token = encrypt_access_token(str(plaintext).strip())


def maybe_reencrypt_plaintext(conn: Any, *, tenant_id: Optional[int] = None) -> bool:
    """Migrate legacy plaintext row to encrypted at rest. Returns True if rewritten."""
    stored = getattr(conn, "access_token", None)
    if not stored or is_encrypted_at_rest(stored):
        return False
    plain = str(stored)
    store_access_token(conn, plain)
    logger.info(
        "[wa_secrets] re-encrypted legacy plaintext token tenant=%s",
        tenant_id or getattr(conn, "tenant_id", "?"),
    )
    return True


def safe_token_tail(conn: Any, *, length: int = 6) -> str | None:
    return token_tail(getattr(conn, "access_token", None), length=length)
