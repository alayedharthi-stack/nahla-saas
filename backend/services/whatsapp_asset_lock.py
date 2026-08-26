"""Asset-scoped PostgreSQL advisory locks for WhatsApp connection commits."""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
from typing import Iterator, Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine

_LOCK_CLASS_PHONE = 877010
_LOCK_CLASS_WABA = 877011


def _normalize_asset_id(asset_id: Optional[str]) -> str:
    return str(asset_id or "").strip()


def _lock_key(provider: str, asset_type: str, asset_id: str) -> int:
    normalized = f"{provider}:{asset_type}:{_normalize_asset_id(asset_id)}"
    digest = hashlib.sha256(normalized.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big", signed=True)


def _lock_pairs(
    *,
    provider: str,
    phone_number_id: Optional[str],
    waba_id: Optional[str],
) -> list[tuple[int, int]]:
    prov = str(provider or "meta").strip().lower()
    locks: list[tuple[int, int]] = []
    phone = _normalize_asset_id(phone_number_id)
    waba = _normalize_asset_id(waba_id)
    if phone:
        locks.append((_LOCK_CLASS_PHONE, _lock_key(prov, "phone", phone)))
    if waba:
        locks.append((_LOCK_CLASS_WABA, _lock_key(prov, "waba", waba)))
    locks.sort()
    return locks


@contextmanager
def whatsapp_asset_advisory_lock_hold(
    engine: Engine,
    *,
    provider: str = "meta",
    phone_number_id: Optional[str] = None,
    waba_id: Optional[str] = None,
) -> Iterator[None]:
    """Hold advisory locks on a dedicated physical connection until the block exits."""
    locks = _lock_pairs(
        provider=provider,
        phone_number_id=phone_number_id,
        waba_id=waba_id,
    )
    if engine.dialect.name != "postgresql" or not locks:
        yield
        return

    conn = engine.connect()
    try:
        for lock_class, lock_key in locks:
            conn.execute(
                text("SELECT pg_advisory_lock(:lock_class, :lock_key)"),
                {"lock_class": lock_class, "lock_key": lock_key},
            )
        try:
            yield
        finally:
            for lock_class, lock_key in reversed(locks):
                conn.execute(
                    text("SELECT pg_advisory_unlock(:lock_class, :lock_key)"),
                    {"lock_class": lock_class, "lock_key": lock_key},
                )
    finally:
        conn.close()
