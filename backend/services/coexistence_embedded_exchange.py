"""Atomic coexistence embedded exchange helpers (no cross-tenant eviction)."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Optional, Tuple

from sqlalchemy import text
from sqlalchemy.orm import Session

from database.models import WhatsAppConnection, WhatsAppOAuthNonce
from scripts.operators.bootstrap_migration_contract import (
    COEXISTENCE_NONCE_TABLE,
    assert_coexistence_nonce_migration_applied,
)


def assert_coexistence_nonce_storage_ready(db: Session) -> None:
    bind = db.get_bind()
    try:
        assert_coexistence_nonce_migration_applied(bind)
    except RuntimeError as exc:
        raise RuntimeError(
            f"Coexistence OAuth nonce storage is not ready ({COEXISTENCE_NONCE_TABLE} missing). "
            "Run alembic upgrade 0101 before embedded signup exchange."
        ) from exc


COEXISTENCE_TENANT_LOCK_CLASS = 877001


def hash_oauth_nonce(nonce: str) -> str:
    return hashlib.sha256(str(nonce or "").encode("utf-8")).hexdigest()


def acquire_tenant_transaction_lock(db: Session, tenant_id: int) -> None:
    """Serialize coexistence callbacks per tenant across workers."""
    bind = db.get_bind()
    if bind.dialect.name == "postgresql":
        db.execute(
            text("SELECT pg_advisory_xact_lock(:lock_class, :tenant_id)"),
            {"lock_class": COEXISTENCE_TENANT_LOCK_CLASS, "tenant_id": int(tenant_id)},
        )
        return
    # SQLite (and other local engines) serialize on a tenant-row write lock.
    db.execute(
        text("UPDATE tenants SET name = name WHERE id = :tenant_id"),
        {"tenant_id": int(tenant_id)},
    )


def persist_oauth_nonce(
    db: Session,
    *,
    nonce: str,
    tenant_id: int,
    connection_mode: str,
    expires_at: datetime,
) -> None:
    assert_coexistence_nonce_storage_ready(db)
    db.add(
        WhatsAppOAuthNonce(
            nonce_hash=hash_oauth_nonce(nonce),
            tenant_id=int(tenant_id),
            connection_mode=str(connection_mode),
            expires_at=expires_at,
            consumed_at=None,
        )
    )
    db.flush()


def consume_oauth_nonce(
    db: Session,
    *,
    nonce: str,
    tenant_id: int,
    connection_mode: str,
    now: Optional[datetime] = None,
) -> str:
    assert_coexistence_nonce_storage_ready(db)
    """Atomically consume a persisted nonce.

    Returns:
        ``consumed`` on success,
        ``already_consumed`` when the same nonce was used,
        ``expired`` / ``mismatch`` / ``missing`` otherwise.
    """
    now = now or datetime.now(timezone.utc)
    nonce_hash = hash_oauth_nonce(nonce)
    bind = db.get_bind()
    if bind.dialect.name == "postgresql":
        result = db.execute(
            text(
                "UPDATE whatsapp_oauth_nonces "
                "SET consumed_at = :now "
                "WHERE nonce_hash = :nonce_hash "
                "AND tenant_id = :tenant_id "
                "AND connection_mode = :connection_mode "
                "AND consumed_at IS NULL "
                "AND expires_at > :now "
                "RETURNING id"
            ),
            {
                "now": now,
                "nonce_hash": nonce_hash,
                "tenant_id": int(tenant_id),
                "connection_mode": str(connection_mode),
            },
        )
        if result.first() is not None:
            db.flush()
            return "consumed"
    else:
        candidate = (
            db.query(WhatsAppOAuthNonce)
            .filter(
                WhatsAppOAuthNonce.nonce_hash == nonce_hash,
                WhatsAppOAuthNonce.tenant_id == int(tenant_id),
                WhatsAppOAuthNonce.connection_mode == str(connection_mode),
                WhatsAppOAuthNonce.consumed_at.is_(None),
            )
            .first()
        )
        if candidate is not None:
            expires_at = candidate.expires_at
            if expires_at is not None and expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at is None or expires_at > now:
                candidate.consumed_at = now
                db.flush()
                return "consumed"

    row = db.query(WhatsAppOAuthNonce).filter(WhatsAppOAuthNonce.nonce_hash == nonce_hash).first()
    if row is None:
        return "missing"
    if int(row.tenant_id) != int(tenant_id) or str(row.connection_mode) != str(connection_mode):
        return "mismatch"
    if row.consumed_at is not None:
        return "already_consumed"
    return "expired"



def coexistence_exchange_fingerprint(code: Optional[str], access_token: Optional[str]) -> str:
    raw = (code or access_token or "").strip()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def read_completed_coexistence_exchange(
    conn: WhatsAppConnection,
    fingerprint: str,
) -> Optional[dict]:
    meta = dict(conn.extra_metadata or {})
    claim = dict(meta.get("coexistence_exchange_claim") or {})
    if claim.get("fingerprint") == fingerprint and claim.get("status") == "completed":
        return claim
    return None


def mark_coexistence_exchange_completed(
    conn: WhatsAppConnection,
    fingerprint: str,
    *,
    waba_id: str,
    phone_number_id: str,
    trusted_business_portfolio_id: str,
    canonical_phone_e164: str,
) -> None:
    meta = dict(conn.extra_metadata or {})
    meta["coexistence_exchange_claim"] = {
        "fingerprint": fingerprint,
        "status": "completed",
        "waba_id": waba_id,
        "phone_number_id": phone_number_id,
        "trusted_business_portfolio_id": trusted_business_portfolio_id,
        "canonical_phone_e164": canonical_phone_e164,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    conn.extra_metadata = meta


def stage_coexistence_credentials(
    conn: WhatsAppConnection,
    *,
    waba_id: str,
    access_token: str,
    token_type: Optional[str],
    connection_type: str = "embedded",
    provider: str = "meta",
) -> None:
    from services.whatsapp_platform.wa_connection_secrets import store_access_token  # noqa: PLC0415

    conn.whatsapp_business_account_id = waba_id
    store_access_token(conn, access_token)
    conn.connection_type = connection_type
    conn.provider = provider
    conn.token_type = token_type
    conn.status = "pending"
    conn.sending_enabled = False
    conn.last_error = None


def load_connection_for_update(
    db: Session,
    tenant_id: int,
) -> Tuple[WhatsAppConnection, bool]:
    """Return the tenant connection, creating an uncommitted row when needed."""
    query = db.query(WhatsAppConnection).filter_by(tenant_id=tenant_id)
    if db.get_bind().dialect.name == "postgresql":
        query = query.with_for_update()
    existing = query.first()
    if existing is not None:
        return existing, True
    conn = WhatsAppConnection(tenant_id=tenant_id)
    db.add(conn)
    db.flush()
    return conn, False


def coexistence_finalize_succeeded(payload: Optional[dict]) -> bool:
    if not isinstance(payload, dict):
        return False
    status = str(payload.get("status") or "").strip().lower()
    if status in {"failed", "coexistence_not_eligible", "configuring", "error"}:
        return False
    return payload.get("connected") is True


def commit_coexistence_transaction(db: Session, tenant_id: int) -> None:
    db.flush()
    db.commit()
    from core.whatsapp_connection_finalization import schedule_whatsapp_catalog_reconnect  # noqa: PLC0415

    schedule_whatsapp_catalog_reconnect(tenant_id)
