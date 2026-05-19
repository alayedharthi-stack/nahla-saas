"""
core/totp_crypto.py
───────────────────
Phase 2A Sprint 1 — TOTP secret encryption + recovery-code generation.

Why a dedicated module
──────────────────────
* All Fernet handling for the 2FA secret lives in one place so a future
  key-rotation routine (Sprint N) only has to touch this file.
* Keeps ``routers/twofa.py`` focused on HTTP shape + audit emission.

Encryption
──────────
* Algorithm: ``cryptography.fernet.Fernet`` (AES-128-CBC + HMAC-SHA256).
* Key: read from ``TOTP_ENC_KEY`` env var; MUST be a valid Fernet key
  (urlsafe base64 of 32 random bytes). Generate one with:

      python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

  In dev, if the key is absent we fall back to a stable but obviously
  insecure value derived from JWT_SECRET so the test suite + local
  Docker setups don't break — production refuses to start without a
  real key (see :func:`_require_prod_key`).

Recovery codes
──────────────
* 10 codes per user, each 12 chars from a 32-char Crockford-style
  alphabet (no 0/O/1/I/L/U to avoid hand-copy errors) split as
  4-4-4 with hyphens, e.g. ``4XR2-9KFM-PZ8H``.
* Each code is bcrypt-hashed (cost 10 — same as the password column)
  before persistence; the plaintext is shown to the user ONCE.
* Verification is constant-time per attempt; the router loops over
  unused rows and is rate-limited to 5 attempts / 15 min per user.

Public API
──────────
* ``generate_secret_b32()``                — 32-char base32 TOTP secret.
* ``encrypt_secret(secret_b32)``           — bytes, persisted to DB.
* ``decrypt_secret(blob)``                 — base32 string for pyotp.
* ``generate_recovery_codes(n=10)``        — list[str] of plaintext codes.
* ``hash_recovery_code(code)``             — bcrypt hash for DB.
* ``verify_recovery_code(code, hash)``     — bool, constant-time.
* ``totp_provisioning_uri(email, secret)`` — ``otpauth://totp/…`` URL
   for QR code rendering on the dashboard.
"""
from __future__ import annotations

import logging
import os
import secrets
from typing import List

logger = logging.getLogger("nahla.totp")

_RECOVERY_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"  # 30 chars; no 0/O/1/I/L/U
_RECOVERY_GROUPS = 3
_RECOVERY_GROUP_LEN = 4
_RECOVERY_TOTAL = _RECOVERY_GROUPS * _RECOVERY_GROUP_LEN
_RECOVERY_DEFAULT_N = 10

_TOTP_ISSUER = "Nahla AI"  # shown to the user inside Google Authenticator etc.


def _is_production() -> bool:
    env = (os.getenv("ENVIRONMENT") or os.getenv("ENV") or "").strip().lower()
    return env in {"prod", "production"}


def _fernet():
    """Return a process-wide Fernet instance, or raise ``RuntimeError`` in prod."""
    from cryptography.fernet import Fernet  # noqa: PLC0415

    key = (os.getenv("TOTP_ENC_KEY") or "").strip()
    if key:
        try:
            return Fernet(key.encode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"TOTP_ENC_KEY is set but invalid (must be a Fernet key — "
                f"urlsafe base64 of 32 bytes). Underlying error: {exc}"
            ) from exc

    if _is_production():
        raise RuntimeError(
            "TOTP_ENC_KEY is required in production. Generate one with: "
            "python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\""
        )

    # Dev / CI fallback — derive a stable but obviously-non-secret key
    # from JWT_SECRET so unit tests + local Docker setups don't break.
    # Anything written to disk with this key is junk-encrypted; never
    # accept it in prod (guarded above).
    import base64
    import hashlib

    seed = (os.getenv("JWT_SECRET") or "nahla-dev-totp-fallback").encode("utf-8")
    derived = hashlib.sha256(seed).digest()  # 32 bytes
    dev_key = base64.urlsafe_b64encode(derived)
    logger.warning(
        "[totp] TOTP_ENC_KEY not set — using DEV fallback derived from JWT_SECRET. "
        "DO NOT use this in production."
    )
    return Fernet(dev_key)


# ── TOTP secret ────────────────────────────────────────────────────────────────
def generate_secret_b32() -> str:
    """160-bit base32 secret — RFC 4226 §4 recommended length."""
    import pyotp  # noqa: PLC0415

    return pyotp.random_base32(length=32)


def encrypt_secret(secret_b32: str) -> bytes:
    """Return Fernet-encrypted bytes ready to store in ``user_totp.secret_enc``."""
    return _fernet().encrypt(secret_b32.encode("utf-8"))


def decrypt_secret(blob: bytes) -> str:
    """Return the plaintext base32 secret. Raises on bad ciphertext or wrong key."""
    if isinstance(blob, memoryview):
        blob = bytes(blob)
    return _fernet().decrypt(blob).decode("utf-8")


def totp_provisioning_uri(email: str, secret_b32: str) -> str:
    """
    Build the ``otpauth://totp/…`` URI shown as a QR on the dashboard.

    ``pyotp.TOTP.provisioning_uri`` already URL-encodes both ``name``
    and ``issuer_name`` and prepends the issuer to the label as
    ``issuer:name`` per the de facto Google Authenticator spec, so we
    pass plain strings here — pre-encoding would double-escape.
    """
    import pyotp  # noqa: PLC0415

    return pyotp.TOTP(secret_b32).provisioning_uri(
        name=email,
        issuer_name=_TOTP_ISSUER,
    )


def verify_totp(secret_b32: str, code: str, valid_window: int = 1) -> bool:
    """
    Verify a 6-digit TOTP code. ``valid_window=1`` accepts the previous,
    current, and next 30-second windows — covers ±30s of clock drift
    between the server and the user's device, which is the default
    Google Authenticator behaviour.
    """
    import pyotp  # noqa: PLC0415

    if not code or not code.strip().isdigit():
        return False
    return bool(pyotp.TOTP(secret_b32).verify(code.strip(), valid_window=valid_window))


# ── Recovery codes ─────────────────────────────────────────────────────────────
def _random_code() -> str:
    """One 12-char alphanumeric code, formatted as ``XXXX-XXXX-XXXX``."""
    raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(_RECOVERY_TOTAL))
    parts = [
        raw[i * _RECOVERY_GROUP_LEN : (i + 1) * _RECOVERY_GROUP_LEN]
        for i in range(_RECOVERY_GROUPS)
    ]
    return "-".join(parts)


def generate_recovery_codes(n: int = _RECOVERY_DEFAULT_N) -> List[str]:
    """Return ``n`` plaintext recovery codes. Shown to the user ONCE."""
    return [_random_code() for _ in range(n)]


def _normalize_code(code: str) -> str:
    """Strip whitespace + uppercase + reinsert hyphens so case/whitespace don't matter."""
    if not code:
        return ""
    cleaned = "".join(ch for ch in code.upper() if ch.isalnum())
    if len(cleaned) != _RECOVERY_TOTAL:
        return cleaned  # let bcrypt fail naturally rather than reformatting bad input
    parts = [
        cleaned[i * _RECOVERY_GROUP_LEN : (i + 1) * _RECOVERY_GROUP_LEN]
        for i in range(_RECOVERY_GROUPS)
    ]
    return "-".join(parts)


def hash_recovery_code(code: str) -> str:
    """bcrypt hash of a normalized plaintext recovery code."""
    import bcrypt  # noqa: PLC0415

    norm = _normalize_code(code)
    return bcrypt.hashpw(norm.encode("utf-8"), bcrypt.gensalt(rounds=10)).decode("utf-8")


def verify_recovery_code(code: str, hashed: str) -> bool:
    """Constant-time compare a user-typed code against a stored bcrypt hash."""
    import bcrypt  # noqa: PLC0415

    if not code or not hashed:
        return False
    norm = _normalize_code(code)
    try:
        return bcrypt.checkpw(norm.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:  # noqa: BLE001 — malformed hash; treat as miss
        return False
