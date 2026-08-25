"""Opaque one-time launch handoff — Redis-only, fail-closed."""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
from dataclasses import dataclass
from typing import Optional

from core.redis_client import get_redis

logger = logging.getLogger("nahla.launch_handoff")

LAUNCH_HANDOFF_TTL_SECONDS = 120
_KEY_PREFIX = "launch:handoff:"


class LaunchHandoffUnavailable(Exception):
    """Raised when the shared handoff store is unavailable."""


@dataclass(frozen=True)
class LaunchHandoffRecord:
    tenant_id: int
    store_id: str
    user_id: int
    email: str
    role: str
    next_path: str


def _hash_handle(handle: str) -> str:
    return hashlib.sha256(handle.encode("utf-8")).hexdigest()


def issue_launch_handoff(
    *,
    tenant_id: int,
    store_id: str,
    user_id: int,
    email: str,
    next_path: str,
    role: str = "merchant",
) -> str:
    """Create a one-time opaque handle. Never log the raw handle."""
    if tenant_id <= 0:
        raise ValueError("launch_handoff_invalid_tenant")
    if not str(store_id or "").strip():
        raise ValueError("launch_handoff_invalid_store")
    if user_id <= 0:
        raise ValueError("launch_handoff_invalid_user")
    if not str(email or "").strip():
        raise ValueError("launch_handoff_invalid_email")
    if role != "merchant":
        raise ValueError("launch_handoff_role_must_be_merchant")

    r = get_redis()
    if r is None:
        raise LaunchHandoffUnavailable("redis_unavailable")

    handle = secrets.token_urlsafe(32)
    key = _KEY_PREFIX + _hash_handle(handle)
    payload = {
        "tenant_id": int(tenant_id),
        "store_id": str(store_id or ""),
        "user_id": int(user_id),
        "email": str(email or ""),
        "role": "merchant",
        "next_path": str(next_path or "/overview"),
    }
    try:
        stored = r.set(key, json.dumps(payload), nx=True, ex=LAUNCH_HANDOFF_TTL_SECONDS)
        if not stored:
            raise LaunchHandoffUnavailable("redis_set_failed")
    except LaunchHandoffUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("[launch_handoff] issue failed: %s", exc)
        raise LaunchHandoffUnavailable("redis_error") from exc

    logger.info(
        "[launch_handoff] issued | tenant=%s store_id=%s user_id=%s ttl=%ss",
        tenant_id,
        store_id or "-",
        user_id,
        LAUNCH_HANDOFF_TTL_SECONDS,
    )
    return handle


def consume_launch_handoff(handle: str) -> Optional[LaunchHandoffRecord]:
    """Atomically consume an opaque handle. Returns None on miss/replay/outage."""
    raw_handle = (handle or "").strip()
    if not raw_handle:
        return None

    r = get_redis()
    if r is None:
        logger.error("[launch_handoff] consume blocked — redis unavailable")
        return None

    key = _KEY_PREFIX + _hash_handle(raw_handle)
    try:
        raw = r.getdel(key)
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        data = json.loads(raw)
        return LaunchHandoffRecord(
            tenant_id=int(data["tenant_id"]),
            store_id=str(data.get("store_id") or ""),
            user_id=int(data["user_id"]),
            email=str(data.get("email") or ""),
            role="merchant",
            next_path=str(data.get("next_path") or "/overview"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[launch_handoff] consume failed: %s", exc)
        return None
