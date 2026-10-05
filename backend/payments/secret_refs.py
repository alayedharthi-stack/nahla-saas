"""Validation for *references* to secrets; the secrets themselves never enter here.

A credential or webhook-secret reference is the name under which the deployment's
secret manager stores the value (for example ``MOYASAR_TEST_SECRET_KEY_T42``). The
payments tables store only that name. These checks refuse values that look like
an actual provider key or token so a key can never be persisted by mistake.
"""
from __future__ import annotations

import re

MAX_REF_LENGTH = 120
_REF_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_./:-]{2,119}$")
# Provider key prefixes and generic token shapes that must never be stored.
_FORBIDDEN_PREFIXES = ("sk_", "pk_", "rk_", "whsec_", "bearer ", "basic ")
_LONG_RANDOM = re.compile(r"[A-Za-z0-9+/=_-]{32,}")


class InvalidSecretReference(ValueError):
    """The value is not an acceptable secret reference."""


def validate_secret_ref(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidSecretReference(f"{field} must be a non-empty secret reference")
    candidate = value.strip()
    lowered = candidate.lower()
    if len(candidate) > MAX_REF_LENGTH or not _REF_PATTERN.match(candidate):
        raise InvalidSecretReference(f"{field} must be a short secret manager name, not a raw value")
    if lowered.startswith(_FORBIDDEN_PREFIXES):
        raise InvalidSecretReference(f"{field} looks like a provider key; store only its reference")
    if _LONG_RANDOM.search(candidate) and "_" not in candidate and "." not in candidate and ":" not in candidate:
        raise InvalidSecretReference(f"{field} looks like a raw token; store only its reference")
    return candidate
