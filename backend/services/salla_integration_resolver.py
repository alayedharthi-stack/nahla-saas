"""
Integration-first Salla connection resolver (A1-v3.7).

Resolves Integration row deterministically from webhook channel + store id.
No pick_active, no allow_alias_match, no tenant-first fallback.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy.orm import Session

from services.order_customer_identity_contract import (
    WEBHOOK_CHANNEL_SALLA,
    WEBHOOK_CHANNEL_SALLA_OAUTH,
)

logger = logging.getLogger("nahla.salla_integration_resolver")


@dataclass(frozen=True)
class ResolvedSallaIntegration:
    integration_id: int
    tenant_id: int
    matched_via: str


@dataclass(frozen=True)
class UnresolvedSallaIntegration:
    reason: str


def _cfg(integration: Any) -> dict:
    return dict(getattr(integration, "config", None) or {})


def _is_oauth_sync_row(cfg: dict) -> bool:
    if cfg.get("api_sync_enabled") is True:
        return True
    if str(cfg.get("app_type") or "").strip() == "custom_oauth_sync":
        return True
    if str(cfg.get("api_key_source") or "").strip() == "custom_oauth_sync":
        return True
    return False


def _is_communication_row(cfg: dict) -> bool:
    if _is_oauth_sync_row(cfg):
        return False
    if str(cfg.get("app_type") or "").strip() in ("easy", "communication"):
        return True
    if str(cfg.get("api_key_source") or "").strip() == "easy_mode_webhook":
        return True
    if cfg.get("api_sync_enabled") is False:
        return True
    # Default Communication-app row when not explicitly oauth-marked.
    return True


def _channel_filter(integration: Any, channel: str) -> bool:
    cfg = _cfg(integration)
    if channel == WEBHOOK_CHANNEL_SALLA_OAUTH:
        return _is_oauth_sync_row(cfg)
    if channel == WEBHOOK_CHANNEL_SALLA:
        return _is_communication_row(cfg)
    return False


def _canonical_store_id(raw: Any) -> str:
    return str(raw or "").strip()


def resolve_salla_integration_connection(
    db: Session,
    *,
    webhook_provider_channel: str,
    canonical_store_id: str,
) -> ResolvedSallaIntegration | UnresolvedSallaIntegration:
    """Integration-first resolver — tenant derived from Integration.tenant_id."""
    from models import Integration  # noqa: PLC0415

    store_id = _canonical_store_id(canonical_store_id)
    if not store_id:
        return UnresolvedSallaIntegration(reason="missing_store_id")

    channel = str(webhook_provider_channel or "").strip()
    if channel not in (WEBHOOK_CHANNEL_SALLA, WEBHOOK_CHANNEL_SALLA_OAUTH):
        return UnresolvedSallaIntegration(reason="unknown_channel")

    base_q = (
        db.query(Integration)
        .filter(
            Integration.provider == "salla",
            Integration.enabled == True,  # noqa: E712
        )
    )
    candidates = [row for row in base_q.all() if _channel_filter(row, channel)]

    # Tier A — external_store_id exact (global within provider filter)
    tier_a = [
        row for row in candidates
        if _canonical_store_id(getattr(row, "external_store_id", None)) == store_id
    ]
    if len(tier_a) == 1:
        row = tier_a[0]
        return ResolvedSallaIntegration(
            integration_id=int(row.id),
            tenant_id=int(row.tenant_id),
            matched_via="tier_a_external_store_id+channel",
        )
    if len(tier_a) > 1:
        return UnresolvedSallaIntegration(reason="ambiguous_tier_a")

    # Tier B — config.store_id when external_store_id IS NULL
    tier_b = []
    for row in candidates:
        if getattr(row, "external_store_id", None):
            continue
        cfg = _cfg(row)
        if _canonical_store_id(cfg.get("store_id")) == store_id:
            tier_b.append(row)
    if len(tier_b) == 1:
        row = tier_b[0]
        return ResolvedSallaIntegration(
            integration_id=int(row.id),
            tenant_id=int(row.tenant_id),
            matched_via="tier_b_config_store_id+channel",
        )
    if len(tier_b) > 1:
        return UnresolvedSallaIntegration(reason="ambiguous_tier_b")

    return UnresolvedSallaIntegration(reason="connection_not_found")


def extract_canonical_store_id_from_payload(payload: dict) -> str:
    if not isinstance(payload, dict):
        return ""
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        data = {}
    for key in ("merchant", "store_id"):
        val = payload.get(key)
        if val:
            return _canonical_store_id(val)
    for key in ("merchant", "store_id"):
        val = data.get(key)
        if val:
            return _canonical_store_id(val)
    return ""


def extract_salla_customer_ref_from_order_payload(payload: dict) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    customer = payload.get("customer") or {}
    if not isinstance(customer, dict):
        customer = {}
    raw = customer.get("id") or payload.get("customer_id")
    if raw in (None, ""):
        return None
    return str(raw).strip() or None


__all__ = [
    "ResolvedSallaIntegration",
    "UnresolvedSallaIntegration",
    "extract_canonical_store_id_from_payload",
    "extract_salla_customer_ref_from_order_payload",
    "resolve_salla_integration_connection",
]
