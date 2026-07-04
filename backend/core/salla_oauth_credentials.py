"""
Salla dual-app OAuth credential resolution.

Communication (embedded) and Sync (Custom OAuth) apps issue refresh tokens
bound to different client_id values. Refresh must use the matching pair.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Literal, Tuple

ClientKind = Literal["sync_oauth", "legacy"]


def is_sync_oauth_integration(config: Dict[str, Any] | None) -> bool:
    """True when config row holds a Sync OAuth (Full API) token set."""
    cfg = config or {}
    app_type = (cfg.get("app_type") or "").lower()
    api_key_src = (cfg.get("api_key_source") or "").lower()
    api_client_id = str(cfg.get("api_client_id") or "").strip()
    sync_client_id = os.environ.get("SALLA_OAUTH_CLIENT_ID", "").strip()

    if app_type == "custom_oauth_sync":
        return True
    if api_key_src == "custom_oauth_sync":
        return True
    if sync_client_id and api_client_id == sync_client_id:
        return True
    if bool(cfg.get("api_sync_enabled")) and sync_client_id and api_client_id == sync_client_id:
        return True
    return False


def resolve_salla_oauth_client(
    config: Dict[str, Any] | None,
) -> Tuple[str, str, ClientKind]:
    """
    Return (client_id, client_secret, kind) for refresh_token exchange.

    Never logs secrets.
    """
    sync_client_id = os.environ.get("SALLA_OAUTH_CLIENT_ID", "").strip()
    sync_client_secret = os.environ.get("SALLA_OAUTH_CLIENT_SECRET", "").strip()
    legacy_client_id = os.environ.get("SALLA_CLIENT_ID", "").strip()
    legacy_client_secret = os.environ.get("SALLA_CLIENT_SECRET", "").strip()

    if is_sync_oauth_integration(config) and sync_client_id and sync_client_secret:
        return sync_client_id, sync_client_secret, "sync_oauth"
    return legacy_client_id, legacy_client_secret, "legacy"


def bootstrap_sync_oauth_token_metadata(
    config: Dict[str, Any],
    *,
    expires_in: Any,
    now: datetime | None = None,
) -> Dict[str, Any]:
    """
    Stamp expiry + refresh history on a freshly issued Sync OAuth token set.

    Prevents the scheduler from immediately refreshing because history fields
    are missing (condition 3 in ``_refresh_all_salla_tokens``).
    """
    cfg = dict(config)
    now_dt = now or datetime.now(timezone.utc)
    now_iso = now_dt.isoformat()
    cfg["last_token_refresh"] = now_iso
    cfg["last_token_refresh_at"] = now_iso
    cfg["token_refresh_status"] = "success"
    cfg["token_refresh_attempts"] = 0
    cfg.pop("token_refresh_error", None)
    cfg.pop("token_refresh_failed_at", None)
    cfg.pop("token_refresh_first_failed_at", None)
    if expires_in is not None:
        try:
            exp_at = (now_dt + timedelta(seconds=int(expires_in))).isoformat()
            cfg["expires_at"] = exp_at
            cfg["token_expires_at"] = exp_at
        except (TypeError, ValueError, OverflowError):
            pass
    return cfg
