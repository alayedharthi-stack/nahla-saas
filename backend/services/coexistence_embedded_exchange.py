
"""Atomic coexistence embedded exchange helpers (no cross-tenant eviction)."""
from __future__ import annotations

import hashlib
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from sqlalchemy.orm import Session

from database.models import WhatsAppConnection


def _conn_field_snapshot(conn: WhatsAppConnection) -> Dict[str, Any]:
    return {
        "status": conn.status,
        "provider": conn.provider,
        "connection_type": conn.connection_type,
        "whatsapp_business_account_id": conn.whatsapp_business_account_id,
        "phone_number_id": conn.phone_number_id,
        "phone_number": conn.phone_number,
        "business_display_name": conn.business_display_name,
        "business_manager_id": conn.business_manager_id,
        "meta_business_account_id": conn.meta_business_account_id,
        "webhook_verified": conn.webhook_verified,
        "sending_enabled": conn.sending_enabled,
        "last_error": conn.last_error,
        "token_type": conn.token_type,
        "access_token": conn.access_token,
        "token_expires_at": conn.token_expires_at,
        "connected_at": conn.connected_at,
        "last_verified_at": conn.last_verified_at,
        "last_attempt_at": conn.last_attempt_at,
        "whatsapp_ai_live_since": conn.whatsapp_ai_live_since,
        "disconnect_reason": conn.disconnect_reason,
        "disconnected_at": conn.disconnected_at,
        "disconnected_by_user_id": conn.disconnected_by_user_id,
        "extra_metadata": deepcopy(conn.extra_metadata or {}),
    }


def restore_connection_snapshot(conn: WhatsAppConnection, snapshot: Dict[str, Any]) -> None:
    for key, value in snapshot.items():
        setattr(conn, key, value)


def coexistence_exchange_fingerprint(code: Optional[str], access_token: Optional[str]) -> str:
    raw = (code or access_token or "").strip()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def read_completed_coexistence_exchange(
    conn: WhatsAppConnection,
    fingerprint: str,
) -> Optional[Dict[str, Any]]:
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


def prepare_connection_context(
    db: Session,
    tenant_id: int,
) -> Tuple[WhatsAppConnection, Dict[str, Any], bool]:
    """Return conn, snapshot, and whether a row existed before this request."""
    existing = db.query(WhatsAppConnection).filter_by(tenant_id=tenant_id).first()
    had_row = existing is not None
    conn = existing or WhatsAppConnection(tenant_id=tenant_id)
    if not had_row:
        db.add(conn)
        db.flush()
    snapshot = _conn_field_snapshot(conn)
    return conn, snapshot, had_row


def rollback_connection_context(
    db: Session,
    conn: WhatsAppConnection,
    snapshot: Dict[str, Any],
    had_row: bool,
) -> None:
    if had_row:
        restore_connection_snapshot(conn, snapshot)
        db.commit()
    else:
        db.delete(conn)
        db.commit()
