"""
core/token_revocation.py
────────────────────────
JWT revocation list — backed by Redis with an in-process fallback.

Why
───
Until refresh tokens land in Phase 2, our JWTs live for hours. If a token
is leaked, the only way to invalidate it before its ``exp`` is a
revocation list keyed by the token's ``jti`` claim.

Public API
──────────
* ``revoke_jti(jti, exp_ts)``  — mark ``jti`` revoked until ``exp_ts``.
* ``is_jti_revoked(jti)``      — True if the token has been revoked.

Notes
─────
* The in-process set is bounded (``_LOCAL_MAX``) to prevent memory
  growth in dev environments without Redis. Old entries are evicted in
  insertion order — fine for dev because production runs on Redis.
* All errors are swallowed; revocation lookup MUST NOT break a request.
"""
from __future__ import annotations

import logging
import time
from collections import OrderedDict
from typing import Optional

from core.redis_client import get_redis

logger = logging.getLogger("nahla.revocation")

_LOCAL_REVOKED: "OrderedDict[str, float]" = OrderedDict()
_LOCAL_MAX = 5_000


def _local_revoke(jti: str, exp_ts: int) -> None:
    _LOCAL_REVOKED[jti] = float(exp_ts)
    while len(_LOCAL_REVOKED) > _LOCAL_MAX:
        _LOCAL_REVOKED.popitem(last=False)


def _local_is_revoked(jti: str) -> bool:
    exp = _LOCAL_REVOKED.get(jti)
    if exp is None:
        return False
    if time.time() > exp:
        _LOCAL_REVOKED.pop(jti, None)
        return False
    return True


def revoke_jti(jti: Optional[str], exp_ts: Optional[int]) -> None:
    """
    Revoke a token by its ``jti`` claim. ``exp_ts`` is the Unix timestamp
    at which the underlying JWT would have expired anyway — used as the
    Redis TTL so the revocation entry expires together with the token.
    """
    if not jti or not exp_ts:
        return
    try:
        ttl = max(1, int(exp_ts - time.time()))
    except Exception:  # noqa: silent-ok — malformed exp claim; revocation is best-effort fallback
        return

    r = get_redis()
    if r is None:
        _local_revoke(jti, exp_ts)
        return

    try:
        r.set(f"jwt:revoked:{jti}", "1", ex=ttl)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[revocation] redis SET failed for jti=%s: %s", jti, exc)
        _local_revoke(jti, exp_ts)


def is_jti_revoked(jti: Optional[str]) -> bool:
    """True iff the given ``jti`` has been revoked. Returns False on errors."""
    if not jti:
        return False
    r = get_redis()
    if r is None:
        return _local_is_revoked(jti)
    try:
        return bool(r.exists(f"jwt:revoked:{jti}"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[revocation] redis EXISTS failed for jti=%s: %s", jti, exc)
        return _local_is_revoked(jti)
