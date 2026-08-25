"""Trusted Nahla Salla embedded Communication App identity sources."""
from __future__ import annotations

from core.config import SALLA_CLIENT_ID, SALLA_TEST_CLIENT_ID


def _normalize_app_id(raw: object) -> str:
    return str(raw or "").strip()


def trusted_salla_embedded_app_ids() -> frozenset[str]:
    """Configured Nahla-owned Salla Communication App IDs for embedded token-login."""
    ids: set[str] = set()
    for configured in (SALLA_CLIENT_ID, SALLA_TEST_CLIENT_ID):
        val = _normalize_app_id(configured)
        if val:
            ids.add(val)
    return frozenset(ids)


def is_trusted_salla_embedded_app_id(app_id: object) -> bool:
    val = _normalize_app_id(app_id)
    if not val:
        return False
    return val in trusted_salla_embedded_app_ids()


def resolve_trusted_salla_embedded_app_id(raw_app_id: object) -> str | None:
    """Resolve request app_id with server default; None when untrusted or unconfigured."""
    incoming = _normalize_app_id(raw_app_id)
    if not incoming:
        incoming = _normalize_app_id(SALLA_CLIENT_ID)
    if not incoming or not is_trusted_salla_embedded_app_id(incoming):
        return None
    return incoming
