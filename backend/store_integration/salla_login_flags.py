"""
Salla token-login integration flags (Dual Integration Architecture).

Shared by ``/salla/token-login`` and tests so the dashboard receives
consistent ``needs_oauth`` / ``needs_api_sync`` signals.
"""
from __future__ import annotations

from typing import Any, Dict, Tuple


def derive_salla_login_integration_flags(
    cfg: Dict[str, Any] | None,
    *,
    enabled: bool = True,
) -> Tuple[bool, bool]:
    """Return ``(needs_oauth, needs_api_sync)`` for an integration config row.

    * ``needs_api_sync`` — merchant should complete the dedicated Sync OAuth
      app flow (``/api/salla/oauth/start``) when Admin API refresh is missing.
    * ``needs_oauth`` — legacy Custom OAuth on the Communication App only;
      never set for ``embedded_token`` rows (Communication App cannot OAuth).
    """
    cfg = cfg or {}
    has_refresh = bool(cfg.get("refresh_token"))
    api_key_src = (cfg.get("api_key_source") or "").lower()
    app_type = (cfg.get("app_type") or "").lower()
    is_easy_mode = app_type == "easy" or api_key_src == "easy_mode_webhook"
    api_sync_done = (
        bool(cfg.get("api_sync_enabled"))
        and has_refresh
        and enabled
    )

    needs_oauth = False
    needs_api_sync = True

    if api_sync_done or is_easy_mode:
        needs_api_sync = False

    if is_easy_mode or api_key_src == "embedded_token":
        # Easy Mode: tokens arrive via webhook. Communication App: introspect
        # session only — Sync OAuth is a separate app/flow.
        return needs_oauth, needs_api_sync

    if not has_refresh:
        needs_oauth = True

    return needs_oauth, needs_api_sync


__all__ = ["derive_salla_login_integration_flags"]
