"""
Sanitize 360dialog coexistence ``client_id`` values persisted in
``whatsapp_connections.extra_metadata["provider_details"]["client_id"]``.

Operators occasionally save UI button labels ("Verify", "Test") or JS
sentinels ("undefined", "null") — those must never be treated as real
OAuth client IDs or block admin dashboards.
"""
from __future__ import annotations

from typing import Any, FrozenSet, Optional

_PLACEHOLDER_LOWER: FrozenSet[str] = frozenset(
    {"verify", "test", "undefined", "null", ""},
)


def sanitize_coexistence_client_id(raw: Optional[Any]) -> Optional[str]:
    """Return a usable client_id string, or None if the value is junk."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if s.lower() in _PLACEHOLDER_LOWER:
        return None
    return s


def client_id_is_present_for_integration(raw: Optional[Any]) -> bool:
    """True only when client_id is non-placeholder (for logging / completeness)."""
    return sanitize_coexistence_client_id(raw) is not None
