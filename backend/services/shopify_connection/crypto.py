"""
AES-256-GCM encryption for Shopify credentials at rest.

Stored format: ``sgcm1:<url-safe base64 of 12-byte nonce || ciphertext+tag>``.

The associated data binds every ciphertext to its purpose, tenant, shop and
connection generation, so a ciphertext copied to another row, another
tenant, another shop, another purpose (access vs refresh vs OAuth code) or an
older/newer install generation fails authentication instead of decrypting.

The key is supplied explicitly (``config.ShopifyConfig.encryption_key`` or an
injected test key). There is no environment fallback in this module and the
key never appears in ``repr``, ``str`` or exception text.
"""
from __future__ import annotations

import base64
import os
from dataclasses import dataclass

PREFIX = "sgcm1:"
_AAD_DOMAIN = "nahla.shopify.credential:v1"
_NONCE_BYTES = 12

PURPOSE_ACCESS_TOKEN = "access_token"
PURPOSE_REFRESH_TOKEN = "refresh_token"
PURPOSE_OAUTH_CODE = "oauth_code"
_PURPOSES = frozenset({PURPOSE_ACCESS_TOKEN, PURPOSE_REFRESH_TOKEN, PURPOSE_OAUTH_CODE})


class CredentialCryptoError(Exception):
    """Encryption or authentication failed. The message is a fixed code only."""

    def __init__(self, code: str = "credential_crypto_failed"):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class CredentialContext:
    """What a ciphertext is bound to. ``binding`` is the generation or the state hash."""

    purpose: str
    tenant_id: int
    shop_domain: str
    binding: str

    def associated_data(self) -> bytes:
        if self.purpose not in _PURPOSES or int(self.tenant_id) <= 0 or not self.shop_domain or not self.binding:
            raise CredentialCryptoError("credential_context_invalid")
        parts = (_AAD_DOMAIN, self.purpose, str(int(self.tenant_id)), self.shop_domain, str(self.binding))
        if any("|" in p for p in parts):
            raise CredentialCryptoError("credential_context_invalid")
        return "|".join(parts).encode("ascii")


def token_context(purpose: str, *, tenant_id: int, shop_domain: str, generation: int) -> CredentialContext:
    return CredentialContext(purpose, int(tenant_id), shop_domain, f"g{int(generation)}")


def code_context(*, tenant_id: int, shop_domain: str, state_hash: str) -> CredentialContext:
    return CredentialContext(PURPOSE_OAUTH_CODE, int(tenant_id), shop_domain, f"s{state_hash}")


class TokenCipher:
    def __init__(self, key: bytes):
        if not isinstance(key, (bytes, bytearray)) or len(key) != 32:
            raise CredentialCryptoError("credential_key_invalid")
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: PLC0415

        self.__aead = AESGCM(bytes(key))

    def __repr__(self) -> str:
        return "TokenCipher(<key withheld>)"

    __str__ = __repr__

    def __reduce__(self):  # never pickle key material
        raise TypeError("TokenCipher is not serialisable")

    def encrypt(self, plaintext: str, context: CredentialContext) -> str:
        if not isinstance(plaintext, str) or not plaintext:
            raise CredentialCryptoError("credential_plaintext_empty")
        aad = context.associated_data()
        nonce = os.urandom(_NONCE_BYTES)
        try:
            sealed = self.__aead.encrypt(nonce, plaintext.encode("utf-8"), aad)
        except Exception:  # noqa: BLE001 — never echo library text
            raise CredentialCryptoError() from None
        return PREFIX + base64.urlsafe_b64encode(nonce + sealed).decode("ascii")

    def decrypt(self, stored: str, context: CredentialContext) -> str:
        if not isinstance(stored, str) or not stored.startswith(PREFIX):
            raise CredentialCryptoError("credential_format_invalid")
        aad = context.associated_data()
        try:
            blob = base64.urlsafe_b64decode(stored[len(PREFIX):].encode("ascii"))
            if len(blob) <= _NONCE_BYTES + 16:
                raise ValueError("short")
            plain = self.__aead.decrypt(blob[:_NONCE_BYTES], blob[_NONCE_BYTES:], aad)
            return plain.decode("utf-8")
        except Exception:  # noqa: BLE001 — wrong key, wrong context or tampering
            raise CredentialCryptoError("credential_authentication_failed") from None
