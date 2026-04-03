"""
core/secrets.py
───────────────
Secret field masking helpers for settings endpoints.
Prevents raw API keys and tokens from leaking to the frontend.
"""
from __future__ import annotations

from typing import Any, Dict

# Fields that are masked before returning to the frontend.
# Key: settings group name → set of field names to mask.
_SECRET_FIELDS: Dict[str, set] = {
    "whatsapp": {"access_token", "verify_token"},
    "store":    {
        "salla_client_secret",
        "salla_access_token",
        "zid_client_secret",
        "shopify_access_token",
    },
}


def mask_secret(value: str) -> str:
    """Return a masked version: first 4 chars + **** + last 4 chars."""
    if not value or len(value) < 9:
        return value  # too short to mask meaningfully
    return value[:4] + "****" + value[-4:]


def is_masked(value: str) -> bool:
    """True if this value was previously returned as a mask (contains ****)."""
    return isinstance(value, str) and "****" in value


def apply_masks(data: Dict[str, Any], group: str) -> Dict[str, Any]:
    """Return a copy of data with secret fields replaced by masked values."""
    fields = _SECRET_FIELDS.get(group, set())
    return {
        k: (mask_secret(v) if k in fields and isinstance(v, str) else v)
        for k, v in data.items()
    }


def restore_secrets(
    incoming: Dict[str, Any],
    stored: Dict[str, Any],
    group: str,
) -> Dict[str, Any]:
    """Replace any masked values in incoming with the original stored secrets."""
    fields = _SECRET_FIELDS.get(group, set())
    result = dict(incoming)
    for field in fields:
        if field in result and is_masked(result[field]):
            result[field] = stored.get(field, "")
    return result
