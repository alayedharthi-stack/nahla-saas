"""Ephemeral Salla embedded reconciliation challenge — Redis-only, fail-closed."""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from core.redis_client import get_redis

logger = logging.getLogger("nahla.salla_reconcile_challenge")

RECONCILE_CHALLENGE_TTL_SECONDS = 600
_NONCE_PREFIX = "salla:reconcile:nonce:"
_STATE_PREFIX = "salla:reconcile:state:"
_PROVIDER = "salla"


class ReconciliationChallengeUnavailable(Exception):
    """Raised when the reconciliation challenge store is unavailable."""


@dataclass(frozen=True)
class ReconciliationChallenge:
    nonce: str
    provider: str
    app_id: str
    merchant_account_id: str
    created_at: str
    expires_at: str


def _hash_value(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _nonce_key(nonce: str) -> str:
    return _NONCE_PREFIX + _hash_value(nonce)


def _state_key(oauth_state: str) -> str:
    return _STATE_PREFIX + _hash_value(oauth_state)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse_record(raw: str, *, nonce: str) -> Optional[ReconciliationChallenge]:
    try:
        data = json.loads(raw)
        return ReconciliationChallenge(
            nonce=nonce,
            provider=str(data.get("provider") or ""),
            app_id=str(data.get("app_id") or ""),
            merchant_account_id=str(data.get("merchant_account_id") or ""),
            created_at=str(data.get("created_at") or ""),
            expires_at=str(data.get("expires_at") or ""),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[salla_reconcile_challenge] parse failed: %s", exc)
        return None


def _is_expired(challenge: ReconciliationChallenge) -> bool:
    raw = (challenge.expires_at or "").strip()
    if not raw:
        return True
    try:
        expires_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return _utc_now() >= expires_at
    except Exception:
        return True


def create_reconciliation_challenge(
    *,
    provider: str,
    app_id: str,
    merchant_account_id: str,
) -> str:
    """Create an opaque reconciliation nonce bound to embedded merchant identity."""
    app = str(app_id or "").strip()
    merchant_id = str(merchant_account_id or "").strip()
    if provider != _PROVIDER:
        raise ValueError("reconcile_challenge_invalid_provider")
    if not app:
        raise ValueError("reconcile_challenge_app_id_required")
    if not merchant_id:
        raise ValueError("reconcile_challenge_merchant_account_id_required")

    r = get_redis()
    if r is None:
        raise ReconciliationChallengeUnavailable("redis_unavailable")

    now = _utc_now()
    expires = now.timestamp() + RECONCILE_CHALLENGE_TTL_SECONDS
    expires_at = datetime.fromtimestamp(expires, tz=timezone.utc)
    payload = {
        "provider": _PROVIDER,
        "app_id": app,
        "merchant_account_id": merchant_id,
        "created_at": _iso(now),
        "expires_at": _iso(expires_at),
    }

    for _ in range(5):
        nonce = secrets.token_urlsafe(32)
        key = _nonce_key(nonce)
        try:
            stored = r.set(
                key,
                json.dumps(payload),
                nx=True,
                ex=RECONCILE_CHALLENGE_TTL_SECONDS,
            )
            if stored:
                logger.info(
                    "[salla_reconcile_challenge] created | app_id=%s merchant_account_id=%s ttl=%ss",
                    app,
                    merchant_id,
                    RECONCILE_CHALLENGE_TTL_SECONDS,
                )
                return nonce
        except ReconciliationChallengeUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("[salla_reconcile_challenge] create failed: %s", exc)
            raise ReconciliationChallengeUnavailable("redis_error") from exc

    raise ReconciliationChallengeUnavailable("redis_set_failed")


def resolve_reconciliation_nonce(nonce: str) -> Optional[ReconciliationChallenge]:
    """Resolve a challenge without consuming it."""
    raw_nonce = (nonce or "").strip()
    if not raw_nonce:
        return None

    r = get_redis()
    if r is None:
        raise ReconciliationChallengeUnavailable("redis_unavailable")

    key = _nonce_key(raw_nonce)
    try:
        raw = r.get(key)
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        challenge = _parse_record(raw, nonce=raw_nonce)
        if challenge is None or challenge.provider != _PROVIDER:
            return None
        if _is_expired(challenge):
            return None
        return challenge
    except ReconciliationChallengeUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("[salla_reconcile_challenge] resolve failed: %s", exc)
        raise ReconciliationChallengeUnavailable("redis_error") from exc


def bind_oauth_state_to_reconciliation_challenge(
    oauth_state: str,
    *,
    nonce: str,
) -> None:
    """Associate generated OAuth state with an existing challenge (not consumed)."""
    state = (oauth_state or "").strip()
    raw_nonce = (nonce or "").strip()
    if not state or not raw_nonce:
        raise ValueError("reconcile_state_bind_invalid")

    challenge = resolve_reconciliation_nonce(raw_nonce)
    if challenge is None:
        raise ValueError("reconcile_challenge_missing_or_expired")

    r = get_redis()
    if r is None:
        raise ReconciliationChallengeUnavailable("redis_unavailable")

    try:
        stored = r.set(
            _state_key(state),
            raw_nonce,
            nx=True,
            ex=RECONCILE_CHALLENGE_TTL_SECONDS,
        )
        if not stored:
            raise ReconciliationChallengeUnavailable("reconcile_state_bind_conflict")
    except ReconciliationChallengeUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("[salla_reconcile_challenge] state bind failed: %s", exc)
        raise ReconciliationChallengeUnavailable("redis_error") from exc

    logger.info(
        "[salla_reconcile_challenge] state bound | app_id=%s merchant_account_id=%s",
        challenge.app_id,
        challenge.merchant_account_id,
    )


def resolve_reconciliation_challenge_for_oauth_state(
    oauth_state: str,
) -> Optional[ReconciliationChallenge]:
    """Resolve the challenge correlated to OAuth state without consuming it."""
    state = (oauth_state or "").strip()
    if not state:
        return None

    r = get_redis()
    if r is None:
        raise ReconciliationChallengeUnavailable("redis_unavailable")

    try:
        raw_nonce = r.get(_state_key(state))
        if raw_nonce is None:
            return None
        if isinstance(raw_nonce, bytes):
            raw_nonce = raw_nonce.decode("utf-8")
        return resolve_reconciliation_nonce(str(raw_nonce))
    except ReconciliationChallengeUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("[salla_reconcile_challenge] state resolve failed: %s", exc)
        raise ReconciliationChallengeUnavailable("redis_error") from exc


def consume_reconciliation_challenge(nonce: str) -> Optional[ReconciliationChallenge]:
    """Atomically consume a challenge and its state correlation. Blocks replay."""
    raw_nonce = (nonce or "").strip()
    if not raw_nonce:
        return None

    r = get_redis()
    if r is None:
        raise ReconciliationChallengeUnavailable("redis_unavailable")

    key = _nonce_key(raw_nonce)
    try:
        raw = r.getdel(key)
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        challenge = _parse_record(raw, nonce=raw_nonce)
        if challenge is None:
            return None
        if _is_expired(challenge):
            return None
        return challenge
    except ReconciliationChallengeUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("[salla_reconcile_challenge] consume failed: %s", exc)
        raise ReconciliationChallengeUnavailable("redis_error") from exc


def consume_reconciliation_challenge_for_oauth_state(
    oauth_state: str,
) -> Optional[ReconciliationChallenge]:
    """Consume challenge via OAuth state mapping and delete both keys."""
    state = (oauth_state or "").strip()
    if not state:
        return None

    r = get_redis()
    if r is None:
        raise ReconciliationChallengeUnavailable("redis_unavailable")

    try:
        raw_nonce = r.getdel(_state_key(state))
        if raw_nonce is None:
            return None
        if isinstance(raw_nonce, bytes):
            raw_nonce = raw_nonce.decode("utf-8")
        return consume_reconciliation_challenge(str(raw_nonce))
    except ReconciliationChallengeUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("[salla_reconcile_challenge] state consume failed: %s", exc)
        raise ReconciliationChallengeUnavailable("redis_error") from exc
