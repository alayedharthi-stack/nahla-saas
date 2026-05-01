"""
AdapterRegistry
───────────────
Resolves the correct BaseStoreAdapter for a given tenant.

Multi-integration handling
──────────────────────────
A single tenant CAN have more than one row in `integrations` for
provider='salla' — e.g. an old manual Account-Token row left over from
before the merchant installed the Easy Mode app, plus a fresh row
created by the app.store.authorize webhook.  When that happens we MUST
prefer the Easy Mode row, otherwise sync uses the stale manual token
and Salla returns 401.

`pick_active_salla_integration(db, tenant_id)` encodes the priority
ladder and is also exported for use by the orders poller and any
diagnostic endpoints so the whole system agrees on which row is
canonical.
"""
from __future__ import annotations
import logging
import os, sys
from typing import Optional, List, Tuple

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from database.session import SessionLocal
from database.models import Integration

logger = logging.getLogger("nahla.store_integration.registry")

_ADAPTER_REGISTRY = {}  # platform -> adapter class, registered at import time

def register_adapter(platform: str):
    def decorator(cls):
        _ADAPTER_REGISTRY[platform] = cls
        return cls
    return decorator


# ── Active-integration selector ──────────────────────────────────────────────


def _is_easy_mode(intg: Integration) -> bool:
    cfg = intg.config or {}
    return (
        (cfg.get("app_type") or "").lower() == "easy"
        or (cfg.get("api_key_source") or "").lower() == "easy_mode_webhook"
    )


def _is_api_sync(intg: Integration) -> bool:
    """True when this row was provisioned by the dedicated "Sync" Custom OAuth
    app (see /api/salla/oauth/callback) AND still holds a valid refresh_token.

    The Dual Integration Architecture treats this as the canonical source of
    Admin API access — it always outranks Easy Mode and embedded tokens.
    """
    cfg = intg.config or {}
    return (
        bool(cfg.get("api_sync_enabled"))
        and bool(cfg.get("refresh_token"))
        and bool(intg.enabled)
    )


def _score_integration(intg: Integration) -> Tuple[int, int, int, int, int, int]:
    """
    Higher tuple = higher priority.  Dual Integration Architecture order:

      1. enabled                                (cannot pick a disabled row)
      2. api_sync_enabled + has refresh_token   (Custom OAuth Sync App — canonical)
      3. easy_mode (Easy Mode webhook tokens — legacy canonical)
      4. has refresh_token (any other OAuth row that can self-refresh)
      5. has api_key (embedded session token, last-resort fallback)
      6. higher row id (newer rows win remaining ties — fresh tokens beat stale)
    """
    cfg = intg.config or {}
    return (
        1 if intg.enabled else 0,
        1 if _is_api_sync(intg) else 0,        # NEW top tier
        1 if _is_easy_mode(intg) else 0,
        1 if cfg.get("refresh_token") else 0,
        1 if cfg.get("api_key") else 0,
        intg.id or 0,
    )


def _list_salla_integrations_for_tenant(db: Session, tenant_id: int) -> List[Integration]:
    return (
        db.query(Integration)
        .filter(
            Integration.tenant_id == tenant_id,
            Integration.provider == "salla",
        )
        .order_by(Integration.id.asc())
        .all()
    )


def pick_active_salla_integration(db: Session, tenant_id: int) -> Optional[Integration]:
    """
    Return the single canonical Salla integration row for this tenant.

    Also performs auto-housekeeping when more than one row is found:
      • The chosen row's config is annotated with `is_canonical=True`.
      • Every loser row is `enabled=False` + `superseded_by_easy_mode=True`
        so subsequent .first() queries elsewhere can't pick the stale
        manual token by accident.

    Returns None when the tenant has no Salla rows at all.
    """
    rows = _list_salla_integrations_for_tenant(db, tenant_id)
    if not rows:
        return None

    if len(rows) == 1:
        return rows[0]

    # Sort highest-priority first
    sorted_rows = sorted(rows, key=_score_integration, reverse=True)
    winner = sorted_rows[0]
    losers = sorted_rows[1:]

    # Log the contest exactly once so ops can see the dedupe decision
    logger.warning(
        "[Registry] tenant=%s has %d salla integrations — picked id=%s "
        "(api_sync=%s easy_mode=%s enabled=%s has_refresh=%s) | "
        "superseding losers=%s",
        tenant_id, len(rows),
        winner.id, _is_api_sync(winner), _is_easy_mode(winner), winner.enabled,
        bool((winner.config or {}).get("refresh_token")),
        [l.id for l in losers],
    )

    # Auto-housekeeping (once): mark losers superseded
    try:
        now = datetime.now(timezone.utc).isoformat()
        winner_cfg = dict(winner.config or {})
        if not winner_cfg.get("is_canonical"):
            winner_cfg["is_canonical"]    = True
            winner_cfg["canonical_since"] = now
            winner.config = winner_cfg

        for loser in losers:
            loser_cfg = dict(loser.config or {})
            loser_cfg["superseded_by_easy_mode"] = True
            loser_cfg["superseded_at"]            = now
            loser_cfg["superseded_by_id"]         = winner.id
            loser_cfg["superseded_reason"]        = (
                "Sync OAuth integration is canonical for this tenant"
                if _is_api_sync(winner)
                else "Easy Mode integration is canonical for this tenant"
                if _is_easy_mode(winner)
                else "newer integration row is canonical for this tenant"
            )
            loser.config  = loser_cfg
            loser.enabled = False

        db.commit()
    except Exception as _hk:
        logger.warning(
            "[Registry] housekeeping failed tenant=%s: %s — sync continues with winner anyway",
            tenant_id, _hk,
        )
        try:
            db.rollback()
        except Exception:
            pass

    return winner


def get_adapter(tenant_id: int):
    """
    Returns a BaseStoreAdapter instance for the tenant, or None if no
    store integration is configured.

    When a tenant has multiple Salla rows (manual + Easy Mode), the
    Easy Mode row wins via `pick_active_salla_integration`.
    """
    try:
        import store_adapters.salla_adapter  # noqa: F401
    except ImportError:
        pass

    db = SessionLocal()
    try:
        integration = pick_active_salla_integration(db, tenant_id)

        if not integration:
            logger.info("[Registry] tenant=%s — no integration found at all", tenant_id)
            return None

        if not integration.enabled:
            cfg = integration.config or {}
            logger.warning(
                "[Registry] tenant=%s canonical salla integration is DISABLED | "
                "id=%s store_id=%s has_token=%s has_refresh=%s",
                tenant_id, integration.id,
                cfg.get("store_id", ""),
                bool(cfg.get("api_key")),
                bool(cfg.get("refresh_token")),
            )
            return None

        adapter_cls = _ADAPTER_REGISTRY.get(integration.provider)
        if not adapter_cls:
            logger.warning("[Registry] No adapter class for provider=%s", integration.provider)
            return None

        cfg = integration.config or {}
        has_token   = bool(cfg.get("api_key"))
        has_refresh = bool(cfg.get("refresh_token"))
        logger.info(
            "[Registry] tenant=%s → adapter=salla integration_id=%s store_id=%s "
            "api_sync=%s easy_mode=%s has_token=%s has_refresh=%s",
            tenant_id, integration.id,
            cfg.get("store_id", ""),
            _is_api_sync(integration), _is_easy_mode(integration),
            has_token, has_refresh,
        )
        if not has_token:
            logger.error(
                "[Registry] tenant=%s — integration enabled but api_key is EMPTY — sync will fail",
                tenant_id,
            )

        return adapter_cls(
            api_key=cfg.get("api_key", ""),
            store_id=cfg.get("store_id", ""),
            refresh_token=cfg.get("refresh_token", ""),
            tenant_id=tenant_id,
        )
    except Exception as exc:
        logger.error("[Registry] tenant=%s error: %s", tenant_id, exc)
        return None
    finally:
        db.close()
