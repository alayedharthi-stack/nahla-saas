"""Durable, tenant-bound, single-use WhatsApp OAuth nonces.

Hashed at rest. Consumed atomically and committed before any Graph call.
Never logs the raw nonce, signed state, or OAuth code.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

logger = logging.getLogger("nahla.whatsapp_oauth_nonce")

TABLE_NAME = "whatsapp_oauth_nonces"
OAUTH_STATE_TTL_SECONDS = 600
ALLOWED_CONNECTION_MODES = frozenset({"embedded", "coexistence"})
_NONCE_HMAC_DOMAIN = b"wa_oauth_nonce:v1:"
_REDIRECT_HMAC_DOMAIN = b"wa_oauth_redirect:v1:"


class NonceStorageUnavailable(Exception):
    """Schema missing, HMAC key missing, or persistence failed. Fail closed."""


class NonceRejected(Exception):
    """Invalid, expired, replayed, or tenant/mode/redirect mismatch."""


def _hmac_key() -> bytes:
    from core.config import JWT_SECRET  # noqa: PLC0415

    secret = str(JWT_SECRET or "").encode("utf-8")
    if not secret:
        raise NonceStorageUnavailable("hmac_key_missing")
    return secret


def hash_oauth_nonce(nonce: str) -> str:
    raw = str(nonce or "")
    if not raw:
        raise NonceRejected("nonce_empty")
    digest = hmac.new(_hmac_key(), _NONCE_HMAC_DOMAIN + raw.encode("utf-8"), hashlib.sha256)
    return digest.hexdigest()


def fingerprint_redirect_uri(redirect_uri: str) -> str:
    raw = str(redirect_uri or "")
    if not raw:
        raise NonceRejected("redirect_uri_empty")
    digest = hmac.new(
        _hmac_key(),
        _REDIRECT_HMAC_DOMAIN + raw.encode("utf-8"),
        hashlib.sha256,
    )
    return digest.hexdigest()


def generate_oauth_nonce() -> str:
    return secrets.token_urlsafe(16)


def normalize_connection_mode(value: Optional[str]) -> str:
    mode = str(value or "embedded").strip().lower()
    if mode not in ALLOWED_CONNECTION_MODES:
        raise NonceRejected("connection_mode_invalid")
    return mode


def nonce_table_exists(db: Session) -> bool:
    bind = db.get_bind()
    if bind is None:
        return False
    try:
        return TABLE_NAME in inspect(bind).get_table_names()
    except Exception:
        logger.exception("[oauth_nonce] schema inspect failed")
        return False


def assert_nonce_storage_ready(db: Session) -> None:
    if not nonce_table_exists(db):
        raise NonceStorageUnavailable("schema_missing")


def persist_oauth_nonce(
    db: Session,
    *,
    nonce: str,
    tenant_id: int,
    connection_mode: str,
    redirect_uri: str,
    expires_at: datetime,
) -> None:
    """Insert hashed nonce. Caller must commit before issuing signed state."""
    assert_nonce_storage_ready(db)
    mode = normalize_connection_mode(connection_mode)
    nonce_hash = hash_oauth_nonce(nonce)
    ru_fp = fingerprint_redirect_uri(redirect_uri)
    now = datetime.now(timezone.utc)
    db.execute(
        text(
            """
            INSERT INTO whatsapp_oauth_nonces (
                nonce_hash, tenant_id, connection_mode, redirect_uri_fingerprint,
                expires_at, consumed_at, created_at
            ) VALUES (
                :nonce_hash, :tenant_id, :connection_mode, :redirect_uri_fingerprint,
                :expires_at, NULL, :created_at
            )
            """
        ),
        {
            "nonce_hash": nonce_hash,
            "tenant_id": int(tenant_id),
            "connection_mode": mode,
            "redirect_uri_fingerprint": ru_fp,
            "expires_at": expires_at,
            "created_at": now,
        },
    )


def _independent_session() -> Session:
    from session import SessionLocal  # noqa: PLC0415

    return SessionLocal()


def consume_oauth_nonce(
    *,
    nonce: str,
    tenant_id: int,
    connection_mode: str,
    redirect_uri: str,
    now: Optional[datetime] = None,
) -> int:
    """Atomically consume one unused, unexpired nonce and commit independently.

    Downstream Graph/WABA failures must not resurrect the nonce.
    """
    mode = normalize_connection_mode(connection_mode)
    nonce_hash = hash_oauth_nonce(nonce)
    ru_fp = fingerprint_redirect_uri(redirect_uri)
    stamp = now or datetime.now(timezone.utc)
    db = _independent_session()
    consumed_id: Optional[int] = None
    try:
        assert_nonce_storage_ready(db)
        result = db.execute(
            text(
                """
                UPDATE whatsapp_oauth_nonces
                SET consumed_at = :now
                WHERE nonce_hash = :nonce_hash
                  AND tenant_id = :tenant_id
                  AND connection_mode = :connection_mode
                  AND redirect_uri_fingerprint = :redirect_uri_fingerprint
                  AND consumed_at IS NULL
                  AND expires_at >= :now
                RETURNING id
                """
            ),
            {
                "now": stamp,
                "nonce_hash": nonce_hash,
                "tenant_id": int(tenant_id),
                "connection_mode": mode,
                "redirect_uri_fingerprint": ru_fp,
            },
        )
        row = result.first()
        if row is None:
            db.rollback()
            raise NonceRejected("nonce_invalid_or_replayed")
        consumed_id = int(row[0])
        db.commit()
    except NonceRejected:
        raise
    except NonceStorageUnavailable:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        logger.exception("[oauth_nonce] consume failed closed")
        raise NonceStorageUnavailable("consume_failed") from None
    finally:
        db.close()
    return int(consumed_id)


def nonce_is_consumed(*, nonce_hash: str, db: Session) -> bool:
    row = db.execute(
        text(
            """
            SELECT consumed_at FROM whatsapp_oauth_nonces
            WHERE nonce_hash = :nonce_hash
            """
        ),
        {"nonce_hash": nonce_hash},
    ).first()
    return bool(row and row[0] is not None)


def expiry_from_now(*, seconds: int = OAUTH_STATE_TTL_SECONDS) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=int(seconds))


def safe_oauth_error_fields(payload: Optional[dict[str, Any]] = None) -> dict[str, str]:
    """Log-safe Meta error projection — never includes state/code/token."""
    data = payload or {}
    return {
        "error": str(data.get("error") or "")[:80],
        "error_reason": str(data.get("error_reason") or "")[:80],
    }


__all__ = [
    "ALLOWED_CONNECTION_MODES",
    "OAUTH_STATE_TTL_SECONDS",
    "TABLE_NAME",
    "NonceRejected",
    "NonceStorageUnavailable",
    "assert_nonce_storage_ready",
    "consume_oauth_nonce",
    "expiry_from_now",
    "fingerprint_redirect_uri",
    "generate_oauth_nonce",
    "hash_oauth_nonce",
    "nonce_is_consumed",
    "nonce_table_exists",
    "normalize_connection_mode",
    "persist_oauth_nonce",
    "safe_oauth_error_fields",
]
