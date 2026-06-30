"""
core/wa_token_crypto.py
───────────────────────
Fernet encryption for WhatsAppConnection.access_token at rest.

Stored format: ``enc1:<fernet-ciphertext>`` in the String column.
Legacy plaintext rows (no prefix) are still readable — see
``decrypt_access_token`` and ``maybe_encrypt_plaintext_on_read``.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("nahla.wa_token_crypto")

_ENC_PREFIX = "enc1:"


def _is_production() -> bool:
    env = (os.getenv("ENVIRONMENT") or os.getenv("ENV") or "").strip().lower()
    return env in {"prod", "production"}


_PROD_KEY_REQUIRED_MSG = (
    "WA_TOKEN_ENC_KEY is required in production. Generate: "
    'python -c "from cryptography.fernet import Fernet; '
    'print(Fernet.generate_key().decode())"'
)


def _fernet_from_key(key: str, *, key_label: str):
    from cryptography.fernet import Fernet  # noqa: PLC0415

    try:
        return Fernet(key.encode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"{key_label} is set but invalid (must be a Fernet key). Error: {exc}"
        ) from exc


def _dev_jwt_fallback_fernet():
    import base64
    import hashlib

    from cryptography.fernet import Fernet  # noqa: PLC0415

    seed = (os.getenv("JWT_SECRET") or "nahla-dev-wa-token-fallback").encode("utf-8")
    dev_key = base64.urlsafe_b64encode(hashlib.sha256(seed).digest())
    logger.warning(
        "[wa_token_crypto] WA_TOKEN_ENC_KEY not set — using JWT_SECRET dev fallback. "
        "DO NOT use in production."
    )
    return Fernet(dev_key)


def _fernet():
    wa_key = (os.getenv("WA_TOKEN_ENC_KEY") or "").strip()

    if _is_production():
        if not wa_key:
            raise RuntimeError(_PROD_KEY_REQUIRED_MSG)
        return _fernet_from_key(wa_key, key_label="WA_TOKEN_ENC_KEY")

    if wa_key:
        return _fernet_from_key(wa_key, key_label="WA_TOKEN_ENC_KEY")

    totp_key = (os.getenv("TOTP_ENC_KEY") or "").strip()
    if totp_key:
        logger.warning(
            "[wa_token_crypto] WA_TOKEN_ENC_KEY not set — using TOTP_ENC_KEY fallback. "
            "Dev/test only — DO NOT use in production."
        )
        return _fernet_from_key(totp_key, key_label="TOTP_ENC_KEY")

    return _dev_jwt_fallback_fernet()


def is_encrypted_at_rest(stored: str | None) -> bool:
    return bool(stored and str(stored).startswith(_ENC_PREFIX))


def encrypt_access_token(plaintext: str) -> str:
    if not plaintext:
        return ""
    blob = _fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")
    return f"{_ENC_PREFIX}{blob}"


def decrypt_access_token(stored: str | None) -> str:
    """Return plaintext token. Legacy plaintext values pass through unchanged."""
    if not stored:
        return ""
    raw = str(stored)
    if raw.startswith(_ENC_PREFIX):
        return _fernet().decrypt(raw[len(_ENC_PREFIX):].encode("utf-8")).decode("utf-8")
    return raw


def token_tail(stored: str | None, *, length: int = 6) -> str | None:
    """Safe log/audit tail — never returns full token."""
    plain = decrypt_access_token(stored) if stored else ""
    if not plain:
        return None
    if len(plain) <= length:
        return "***"
    return plain[-length:]
