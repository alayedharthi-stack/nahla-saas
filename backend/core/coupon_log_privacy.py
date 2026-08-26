"""Privacy helpers for coupon-related logging and diagnostics."""
from __future__ import annotations

import hashlib
from typing import Any

_HASH_PREFIX_LEN = 12


def hash_identifier(value: Any) -> str:
    """Return a truncated SHA-256 hex digest for correlation without raw values."""
    if value is None:
        return ''
    normalized = str(value).strip()
    if not normalized:
        return ''
    digest = hashlib.sha256(normalized.encode('utf-8')).hexdigest()
    return digest[:_HASH_PREFIX_LEN]


def safe_exception_class(exc: BaseException) -> str:
    return type(exc).__name__


def redact_coupon_code(code: Any) -> str:
    if code is None:
        return ''
    raw = str(code).strip()
    if not raw:
        return ''
    return f'coupon_hash={hash_identifier(raw)}'


def redact_store_id(store_id: Any) -> str:
    if store_id is None:
        return ''
    raw = str(store_id).strip()
    if not raw:
        return ''
    return f'store_hash={hash_identifier(raw)}'


__all__ = [
    'hash_identifier',
    'safe_exception_class',
    'redact_coupon_code',
    'redact_store_id',
]
