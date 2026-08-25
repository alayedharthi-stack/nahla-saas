"""Trusted Nahla Salla embedded Communication App identity sources."""
from __future__ import annotations

import hashlib
import logging

from core.config import SALLA_EMBEDDED_APP_ID, SALLA_TEST_CLIENT_ID

logger = logging.getLogger("nahla.salla_embedded_app_identity")

INVALID_SALLA_APP_ID_CODE = "invalid_salla_app_id"


def _normalize_app_id(raw: object) -> str:
    return str(raw or "").strip()


def _configured_embedded_app_ids() -> tuple[tuple[str, str], ...]:
    """Return (env_name, normalized_value) pairs for configured trusted IDs."""
    rows: list[tuple[str, str]] = []
    for env_name, configured in (
        ("SALLA_EMBEDDED_APP_ID", SALLA_EMBEDDED_APP_ID),
        ("SALLA_TEST_CLIENT_ID", SALLA_TEST_CLIENT_ID),
    ):
        val = _normalize_app_id(configured)
        if val:
            rows.append((env_name, val))
    return tuple(rows)


def trusted_salla_embedded_app_ids() -> frozenset[str]:
    """Configured Nahla-owned Salla Communication App IDs for embedded token-login."""
    return frozenset(value for _, value in _configured_embedded_app_ids())


def trusted_salla_embedded_app_id_sources() -> tuple[str, ...]:
    """Configured env var names that contributed trusted embedded app IDs."""
    return tuple(env_name for env_name, _ in _configured_embedded_app_ids())


def mask_embedded_app_id(app_id: object) -> str:
    """Return a log-safe fingerprint for an app_id without exposing the full value."""
    val = _normalize_app_id(app_id)
    if not val:
        return "<empty>"
    digest = hashlib.sha256(val.encode("utf-8")).hexdigest()[:12]
    if len(val) <= 4:
        return f"hash={digest}"
    return f"hash={digest} suffix={val[-4:]}"


def log_rejected_embedded_app_id(
    *,
    incoming_raw: object,
    request_id: str | None = None,
    client_ip: str | None = None,
) -> None:
    trusted = trusted_salla_embedded_app_ids()
    logger.warning(
        "[SallaEmbeddedAppId] rejected code=%s incoming=%s trusted_count=%d "
        "trusted_sources=%s request_id=%s ip=%s",
        INVALID_SALLA_APP_ID_CODE,
        mask_embedded_app_id(incoming_raw),
        len(trusted),
        ",".join(trusted_salla_embedded_app_id_sources()) or "none",
        request_id or "-",
        client_ip or "-",
    )


def is_trusted_salla_embedded_app_id(app_id: object) -> bool:
    val = _normalize_app_id(app_id)
    if not val:
        return False
    return val in trusted_salla_embedded_app_ids()


def resolve_trusted_salla_embedded_app_id(raw_app_id: object) -> str | None:
    """Resolve request app_id against configured trusted IDs; None when untrusted or unconfigured."""
    incoming = _normalize_app_id(raw_app_id)
    trusted = trusted_salla_embedded_app_ids()
    if not trusted:
        return None
    if not incoming:
        default = _normalize_app_id(SALLA_EMBEDDED_APP_ID)
        if default and default in trusted:
            return default
        return None
    if incoming in trusted:
        return incoming
    return None
