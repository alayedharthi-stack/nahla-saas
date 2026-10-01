"""OTO token encryption uses a dedicated key; no plaintext fallback."""
import os


def _cipher():
    from cryptography.fernet import Fernet

    key = os.getenv("OTO_TOKEN_ENC_KEY")
    if not key:
        raise RuntimeError("oto_encryption_key_missing")
    return Fernet(key.encode("ascii"))


def encrypt_secret(value: str) -> str:
    if not value:
        raise ValueError("oto_secret_missing")
    return "enc1:" + _cipher().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(value: str) -> str:
    if not value or not value.startswith("enc1:"):
        raise ValueError("oto_secret_not_encrypted")
    return _cipher().decrypt(value[5:].encode("ascii")).decode("utf-8")
