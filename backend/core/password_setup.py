"""
core/password_setup.py
──────────────────────
Set-password token issue / verify / consume.

Used by the merchant onboarding flow: a Salla / Zid OAuth callback
auto-creates a Nahla User with a random password hash that the merchant
cannot know. We email them a single-use ``set-password`` link backed by
this module so they can create a real local password without ever
calling /auth/forgot-password.

The same primitives are intentionally usable for "secure password
reset" later — the JWT-based ``/auth/forgot-password`` flow currently
in production is *not* single-use, which violates the user's explicit
spec ("invalidate token after use"). This module is the foundation for
flipping that path to DB-backed single-use tokens in a follow-up.

Token shape
───────────
* Raw value: 32 random bytes encoded as base64url (43 chars). 256 bits
  of entropy — well above the bcrypt-cracking horizon.
* Stored value: SHA-256 hex digest of the raw token (64 chars). The raw
  token only exists in the email — a DB leak cannot be replayed.
* TTL: 7 days for "welcome" purpose, 1 hour for "reset" purpose.

Public API
──────────
* ``issue_token(db, user, *, purpose, ttl_seconds=None, issued_via=None)``
  → returns the raw token string (the only place it ever exists).
* ``verify_token(db, raw_token)`` → returns the ``PasswordSetupToken`` row
  if valid, else ``None``. Idempotent — does not consume.
* ``consume_token(db, raw_token, new_password, *, ip=None)``
  → atomically marks the row as used and updates the user's password.
  Returns the User on success, raises ``InvalidToken`` / ``ExpiredToken``
  / ``UsedToken`` / ``WeakPassword`` otherwise.
"""
from __future__ import annotations

import hashlib
import logging
import secrets as _secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

logger = logging.getLogger("nahla.password_setup")

# Default TTLs per purpose. Welcome links are emailed once on auto-create;
# 7 days gives the merchant a comfortable window to act without making
# the link a long-lived secret.
_DEFAULT_TTL_SECONDS = {
    "welcome": 7 * 24 * 60 * 60,
    "reset":   60 * 60,
}

_MIN_PASSWORD_LEN = 8


# ── Exceptions ────────────────────────────────────────────────────────────────
class _SetPasswordError(Exception):
    """Base error — never raised directly, callers catch the subclasses."""


class InvalidToken(_SetPasswordError):
    """Token does not match any stored row."""


class ExpiredToken(_SetPasswordError):
    """Token row exists but ``expires_at`` is in the past."""


class UsedToken(_SetPasswordError):
    """Token row exists but was already consumed (``used_at`` set)."""


class WeakPassword(_SetPasswordError):
    """The chosen password failed minimum-strength checks."""


# ── Helpers ───────────────────────────────────────────────────────────────────
def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _resolve_ttl(purpose: str, override: Optional[int]) -> int:
    if override and override > 0:
        return int(override)
    return int(_DEFAULT_TTL_SECONDS.get(purpose, _DEFAULT_TTL_SECONDS["reset"]))


# ── Public API ────────────────────────────────────────────────────────────────
def issue_token(
    db: Session,
    user,
    *,
    purpose: str = "welcome",
    ttl_seconds: Optional[int] = None,
    issued_via: Optional[str] = None,
) -> str:
    """Issue a single-use set-password token for ``user``.

    Returns the raw token string — the ONLY place this value ever
    exists. The DB stores only its SHA-256 hash. Caller is expected to
    embed the raw value in a ``/set-password?token=...`` URL inside an
    email.

    Side-effect: any prior unconsumed tokens for the same
    ``(user_id, purpose)`` are marked as ``used_at = now`` so a stolen
    earlier link cannot be replayed alongside the new one. This is
    deliberate — multiple live tokens per user is a flow-confusion
    smell.
    """
    if user is None or not getattr(user, "id", None):
        raise ValueError("issue_token: user must have an id")

    from models import PasswordSetupToken  # noqa: PLC0415

    now = _utcnow()
    ttl = _resolve_ttl(purpose, ttl_seconds)

    # Invalidate prior live tokens for this user+purpose.
    try:
        prior = (
            db.query(PasswordSetupToken)
            .filter(
                PasswordSetupToken.user_id == user.id,
                PasswordSetupToken.purpose == purpose,
                PasswordSetupToken.used_at.is_(None),
                PasswordSetupToken.expires_at > now,
            )
            .all()
        )
        for row in prior:
            row.used_at = now
    except Exception as exc:  # noqa: BLE001 — invalidation is best-effort; new token is still safe to issue
        logger.warning("[password_setup] failed to invalidate prior tokens for user=%s: %s", user.id, exc)

    raw_token = _secrets.token_urlsafe(32)  # ~43 chars, 256 bits
    new_row = PasswordSetupToken(
        user_id    = user.id,
        token_hash = _hash_token(raw_token),
        purpose    = purpose,
        expires_at = now + timedelta(seconds=ttl),
        issued_via = (issued_via or "")[:64] or None,
    )
    db.add(new_row)
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise

    logger.info(
        "[password_setup] issued | user_id=%s purpose=%s ttl=%ss via=%s",
        user.id, purpose, ttl, issued_via or "?",
    )
    return raw_token


def verify_token(db: Session, raw_token: str):
    """Return the ``PasswordSetupToken`` row if the raw token is valid.

    "Valid" means: row exists, ``used_at`` is null, and ``expires_at``
    is in the future. Returns ``None`` for any other case — callers
    decide whether to surface "expired" vs "missing" vs "used" by
    inspecting the row themselves via lower-level queries (or just
    catching the corresponding ``consume_token`` exception).
    """
    if not raw_token:
        return None
    from models import PasswordSetupToken  # noqa: PLC0415

    row = (
        db.query(PasswordSetupToken)
        .filter(PasswordSetupToken.token_hash == _hash_token(raw_token))
        .first()
    )
    if row is None:
        return None
    if row.used_at is not None:
        return None
    if row.expires_at < _utcnow():
        return None
    return row


def consume_token(
    db: Session,
    raw_token: str,
    new_password: str,
    *,
    ip: Optional[str] = None,
):
    """Atomically validate the token, set the user's password, mark used.

    Returns the User on success.
    Raises ``InvalidToken`` / ``ExpiredToken`` / ``UsedToken`` /
    ``WeakPassword`` on failure — callers map each to the appropriate
    HTTP response.
    """
    if not raw_token:
        raise InvalidToken("token is empty")

    if not new_password or len(new_password) < _MIN_PASSWORD_LEN:
        raise WeakPassword(f"password must be at least {_MIN_PASSWORD_LEN} characters")

    from models import PasswordSetupToken, User  # noqa: PLC0415
    from core.auth import hash_password  # noqa: PLC0415

    row = (
        db.query(PasswordSetupToken)
        .filter(PasswordSetupToken.token_hash == _hash_token(raw_token))
        .first()
    )
    if row is None:
        raise InvalidToken("token not found")
    if row.used_at is not None:
        raise UsedToken("token already used")
    if row.expires_at < _utcnow():
        raise ExpiredToken("token expired")

    user = db.query(User).filter(User.id == row.user_id).first()
    if user is None:
        # Token row references a user that no longer exists — treat as
        # invalid rather than 500. Should never happen because of FK
        # ON DELETE CASCADE, but defensive.
        raise InvalidToken("user no longer exists")

    user.password_hash = hash_password(new_password)
    row.used_at        = _utcnow()
    row.consumed_ip    = (ip or "")[:64] or None

    try:
        db.commit()
    except Exception:
        db.rollback()
        raise

    logger.info(
        "[password_setup] consumed | user_id=%s purpose=%s",
        user.id, row.purpose,
    )
    return user
