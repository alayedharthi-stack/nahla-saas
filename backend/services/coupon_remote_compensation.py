"""Durable tenant-scoped unresolved Salla coupon compensation records."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from core.coupon_log_privacy import hash_identifier, safe_exception_class
from models import TenantSettings

logger = logging.getLogger('nahla.coupon_remote_compensation')

COMPENSATIONS_KEY = 'coupon_remote_compensations'
_STATUS_PENDING = 'pending'
_STATUS_RESOLVED = 'resolved'


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _settings_row(db: Session, tenant_id: int) -> TenantSettings:
    row = db.query(TenantSettings).filter_by(tenant_id=int(tenant_id)).first()
    if row is None:
        row = TenantSettings(tenant_id=int(tenant_id), extra_metadata={})
        db.add(row)
        db.flush()
    return row


def _load_compensations(meta: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    raw = (meta or {}).get(COMPENSATIONS_KEY) or []
    if not isinstance(raw, list):
        return []
    return [dict(item) for item in raw if isinstance(item, dict)]


def _persist_compensations(db: Session, row: TenantSettings, items: List[Dict[str, Any]]) -> None:
    meta = dict(row.extra_metadata or {})
    meta[COMPENSATIONS_KEY] = items
    row.extra_metadata = meta
    flag_modified(row, 'extra_metadata')


def record_unresolved_compensation(
    db: Session,
    tenant_id: int,
    *,
    salla_coupon_id: Any,
    code_hash: str,
    error_class: str,
    idempotency_key: str,
) -> bool:
    """Record a pending compensation. Returns False when idempotency_key already exists."""
    key = str(idempotency_key or '').strip()
    if not key:
        raise ValueError('idempotency_key is required')

    provider_id = str(salla_coupon_id or '').strip()
    if not provider_id:
        raise ValueError('salla_coupon_id is required')

    row = _settings_row(db, int(tenant_id))
    items = _load_compensations(row.extra_metadata)
    for item in items:
        if str(item.get('idempotency_key') or '') == key:
            return False

    items.append(
        {
            'idempotency_key': key,
            'salla_coupon_id': provider_id,
            'code_hash': str(code_hash or ''),
            'error_class': str(error_class or 'unknown'),
            'status': _STATUS_PENDING,
            'created_at': _utc_now_iso(),
            'resolved_at': None,
            'last_retry_at': None,
            'last_retry_error_class': None,
        }
    )
    _persist_compensations(db, row, items)
    db.flush()
    logger.warning(
        '[coupon_compensation] recorded tenant_id=%s provider_id_hash=%s error_class=%s',
        int(tenant_id),
        hash_identifier(provider_id),
        str(error_class or 'unknown'),
    )
    return True


def list_pending_compensations(db: Session, tenant_id: int) -> List[Dict[str, Any]]:
    row = db.query(TenantSettings).filter_by(tenant_id=int(tenant_id)).first()
    if row is None:
        return []
    return [
        dict(item)
        for item in _load_compensations(row.extra_metadata)
        if str(item.get('status') or _STATUS_PENDING) == _STATUS_PENDING
    ]


def mark_compensation_resolved(db: Session, tenant_id: int, idempotency_key: str) -> bool:
    key = str(idempotency_key or '').strip()
    if not key:
        return False

    row = _settings_row(db, int(tenant_id))
    items = _load_compensations(row.extra_metadata)
    changed = False
    for item in items:
        if str(item.get('idempotency_key') or '') != key:
            continue
        if str(item.get('status') or _STATUS_PENDING) == _STATUS_RESOLVED:
            return True
        item['status'] = _STATUS_RESOLVED
        item['resolved_at'] = _utc_now_iso()
        changed = True
        break

    if not changed:
        return False

    _persist_compensations(db, row, items)
    db.flush()
    return True


async def _delete_remote_coupon(adapter: Any, salla_coupon_id: str) -> bool:
    provider_id = str(salla_coupon_id or '').strip()
    if not provider_id:
        return False

    delete_by_id = getattr(adapter, 'delete_coupon_by_id', None)
    if callable(delete_by_id):
        result = await delete_by_id(provider_id)
        return bool(result)

    delete_fn = getattr(adapter, '_delete', None)
    if callable(delete_fn):
        result = await delete_fn(f'/coupons/{provider_id}')
        return bool(result)

    return False


async def retry_pending_compensations(db: Session, tenant_id: int, adapter: Any) -> Dict[str, int]:
    """Attempt provider-ID deletion for pending compensations.

    Successful deletes mark the record resolved. Failures update retry metadata
    without creating duplicate records.
    """
    row = _settings_row(db, int(tenant_id))
    items = _load_compensations(row.extra_metadata)
    attempted = 0
    resolved = 0
    failed = 0
    changed = False

    for item in items:
        if str(item.get('status') or _STATUS_PENDING) != _STATUS_PENDING:
            continue
        provider_id = str(item.get('salla_coupon_id') or '').strip()
        if not provider_id:
            continue

        attempted += 1
        item['last_retry_at'] = _utc_now_iso()
        try:
            ok = await _delete_remote_coupon(adapter, provider_id)
        except Exception as exc:
            ok = False
            item['last_retry_error_class'] = safe_exception_class(exc)
            logger.warning(
                '[coupon_compensation] retry exception tenant_id=%s provider_id_hash=%s error_class=%s',
                int(tenant_id),
                hash_identifier(provider_id),
                safe_exception_class(exc),
            )
        else:
            item['last_retry_error_class'] = None if ok else 'delete_returned_false'

        if ok:
            item['status'] = _STATUS_RESOLVED
            item['resolved_at'] = _utc_now_iso()
            resolved += 1
            changed = True
            logger.info(
                '[coupon_compensation] resolved tenant_id=%s provider_id_hash=%s',
                int(tenant_id),
                hash_identifier(provider_id),
            )
        else:
            failed += 1
            changed = True
            logger.warning(
                '[coupon_compensation] retry failed tenant_id=%s provider_id_hash=%s error_class=%s',
                int(tenant_id),
                hash_identifier(provider_id),
                str(item.get('last_retry_error_class') or 'delete_returned_false'),
            )

    if changed:
        _persist_compensations(db, row, items)
        db.flush()

    return {'attempted': attempted, 'resolved': resolved, 'failed': failed}


__all__ = [
    'COMPENSATIONS_KEY',
    'record_unresolved_compensation',
    'list_pending_compensations',
    'mark_compensation_resolved',
    'retry_pending_compensations',
]
