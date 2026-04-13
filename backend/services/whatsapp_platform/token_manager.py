from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx
from sqlalchemy.orm import Session

from core.config import META_APP_ID, META_APP_SECRET, META_GRAPH_API_VERSION, WA_TOKEN
from .provider_utils import (
    WHATSAPP_CONNECTION_TYPE_DIRECT,
    WHATSAPP_PROVIDER_360DIALOG,
    wa_provider,
)

logger = logging.getLogger("nahla.whatsapp.token_manager")

GRAPH = f"https://graph.facebook.com/{META_GRAPH_API_VERSION}"


@dataclass
class WhatsAppTokenContext:
    token: str
    source: str
    token_status: str
    expires_at: Optional[datetime]
    oauth_session_status: str
    oauth_session_message: Optional[str]


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _read_meta(conn: Any) -> Dict[str, Any]:
    return dict(getattr(conn, "extra_metadata", None) or {})


def _merchant_token_health(conn: Any) -> Tuple[str, Optional[datetime]]:
    if not conn or not getattr(conn, "access_token", None):
        return "missing", None
    expires_at = getattr(conn, "token_expires_at", None)
    if expires_at and _now_utc() > expires_at:
        return "expired", expires_at
    if expires_at and expires_at - _now_utc() <= timedelta(days=7):
        return "expiring_soon", expires_at
    if not expires_at:
        return "expiring_soon", None
    return "healthy", expires_at


def get_oauth_session_state(conn: Any) -> tuple[str, Optional[str]]:
    if not conn:
        return "missing", None
    if wa_provider(conn) == WHATSAPP_PROVIDER_360DIALOG:
        return "not_applicable", None
    meta = _read_meta(conn)
    if getattr(conn, "connection_type", None) == WHATSAPP_CONNECTION_TYPE_DIRECT and not getattr(conn, "access_token", None):
        return "not_applicable", None
    access_token = getattr(conn, "access_token", None)
    expires_at = getattr(conn, "token_expires_at", None)
    if access_token and expires_at and _now_utc() > expires_at:
        return (
            "expired",
            "انتهت جلسة Meta الإدارية داخل نحلة. قد تحتاج إعادة التفويض لإدارة أصول Meta من داخل المنصة.",
        )
    status = str(meta.get("oauth_session_status") or ("healthy" if access_token else "missing"))
    return status, meta.get("oauth_session_message")


def build_token_context(conn: Any, *, source: str) -> WhatsAppTokenContext:
    oauth_status, oauth_message = get_oauth_session_state(conn)
    if source == "platform":
        token = WA_TOKEN or ""
        token_status = "healthy" if token else "missing"
        expires_at = None
    elif source == "dialog360":
        token = str(getattr(conn, "access_token", "") or "")
        token_status = "healthy" if token else "missing"
        expires_at = None
    elif source == "merchant_oauth":
        token = str(getattr(conn, "access_token", "") or "")
        token_status, expires_at = _merchant_token_health(conn)
    else:
        token = ""
        token_status = "missing"
        expires_at = None
    return WhatsAppTokenContext(
        token=token,
        source=source,
        token_status=token_status,
        expires_at=expires_at,
        oauth_session_status=oauth_status,
        oauth_session_message=oauth_message,
    )


def _default_prefer_platform(conn: Any) -> bool:
    return bool(
        conn
        and wa_provider(conn) != WHATSAPP_PROVIDER_360DIALOG
        and getattr(conn, "connection_type", None) == WHATSAPP_CONNECTION_TYPE_DIRECT
    )


def get_token_candidates(conn: Any, *, prefer_platform: bool = False) -> List[WhatsAppTokenContext]:
    if wa_provider(conn) == WHATSAPP_PROVIDER_360DIALOG:
        order = ["dialog360"]
    else:
        order = ["platform", "merchant_oauth"] if prefer_platform else ["merchant_oauth", "platform"]
    contexts: List[WhatsAppTokenContext] = []
    seen: set[str] = set()
    for source in order:
        ctx = build_token_context(conn, source=source)
        if not ctx.token or ctx.token in seen:
            continue
        contexts.append(ctx)
        seen.add(ctx.token)
    if not contexts:
        contexts.append(build_token_context(conn, source="missing"))
    return contexts


def get_token_context(conn: Any) -> WhatsAppTokenContext:
    candidates = get_token_candidates(conn, prefer_platform=_default_prefer_platform(conn))
    usable = next(
        (ctx for ctx in candidates if ctx.token and ctx.token_status in {"healthy", "expiring_soon"}),
        None,
    )
    return usable or next((ctx for ctx in candidates if ctx.token), build_token_context(conn, source="missing"))


def update_token_state(
    conn: Any,
    *,
    token_source: Optional[str] = None,
    token_status: Optional[str] = None,
    token_expires_at: Optional[datetime] = None,
    oauth_session_status: Optional[str] = None,
    oauth_session_message: Optional[str] = None,
    debug_info: Optional[Dict[str, Any]] = None,
) -> None:
    if not conn:
        return
    meta = _read_meta(conn)
    now = _now_utc()
    meta["last_token_check_at"] = now.isoformat()
    if token_source is not None:
        meta["active_graph_token_source"] = token_source
        meta["active_token_source"] = token_source
    if token_status is not None:
        meta["token_status"] = token_status
        meta["token_health"] = token_status
    if token_expires_at is not None:
        meta["operational_token_expires_at"] = token_expires_at.isoformat()
        conn.token_expires_at = token_expires_at
    elif token_source in {"platform", "dialog360"}:
        meta["operational_token_expires_at"] = None
    if oauth_session_status is not None:
        meta["oauth_session_status"] = oauth_session_status
        meta["oauth_session_needs_reauth"] = oauth_session_status in {"expired", "invalid", "missing"}
    if oauth_session_message:
        meta["oauth_session_message"] = oauth_session_message
        meta["last_token_error"] = oauth_session_message
    elif oauth_session_status is not None:
        meta.pop("oauth_session_message", None)
        meta.pop("last_token_error", None)
    if debug_info:
        meta["oauth_debug"] = {
            "is_valid": debug_info.get("is_valid"),
            "expires_at": debug_info.get("expires_at"),
            "scopes": debug_info.get("scopes"),
            "granular_scopes": debug_info.get("granular_scopes"),
        }
    conn.extra_metadata = meta


def persist_token_context(
    db: Session,
    conn: Any,
    *,
    tenant_id: Optional[int],
    operation: str,
    ctx: WhatsAppTokenContext,
) -> None:
    update_token_state(
        conn,
        token_source=ctx.source,
        token_status=ctx.token_status,
        token_expires_at=ctx.expires_at if ctx.source == "merchant_oauth" else None,
        oauth_session_status=ctx.oauth_session_status,
        oauth_session_message=ctx.oauth_session_message,
    )
    if conn is not None:
        db.flush()
    logger.info(
        "[WA token] op=%s tenant=%s source=%s token_status=%s token_expiry=%s oauth_session_status=%s",
        operation,
        tenant_id,
        ctx.source,
        ctx.token_status,
        ctx.expires_at.isoformat() if ctx.expires_at else None,
        ctx.oauth_session_status,
    )


async def _refresh_merchant_long_lived_token(conn: Any) -> Optional[WhatsAppTokenContext]:
    if not conn or not getattr(conn, "access_token", None):
        return None
    if wa_provider(conn) == WHATSAPP_PROVIDER_360DIALOG:
        return None
    if not META_APP_ID or not META_APP_SECRET:
        return None
    health, expires_at = _merchant_token_health(conn)
    if health == "healthy" and expires_at and expires_at - _now_utc() > timedelta(days=7):
        return None
    logger.info(
        "[WA token] attempting refresh — health=%s expires_at=%s tenant=%s",
        health, expires_at.isoformat() if expires_at else "unknown",
        getattr(conn, "tenant_id", "?"),
    )
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                f"{GRAPH}/oauth/access_token",
                params={
                    "grant_type": "fb_exchange_token",
                    "client_id": META_APP_ID,
                    "client_secret": META_APP_SECRET,
                    "fb_exchange_token": conn.access_token,
                },
            )
            data = resp.json()
    except Exception as exc:
        logger.warning("[WA token] refresh failed with network error: %s", exc)
        return None
    if "error" in data:
        err_code = int(data.get("error", {}).get("code") or 0)
        if err_code == 190:
            conn.token_expires_at = _now_utc() - timedelta(seconds=1)
            logger.warning(
                "[WA token] token truly expired (190) — marked expired in DB | tenant=%s",
                getattr(conn, "tenant_id", "?"),
            )
        else:
            logger.warning("[WA token] refresh rejected by Meta: %s", data)
        return None
    new_token = data.get("access_token")
    if not new_token:
        return None
    conn.access_token = new_token
    conn.token_type = "long_lived"
    new_expires_at = _now_utc() + timedelta(seconds=int(data.get("expires_in") or 5183944))
    conn.token_expires_at = new_expires_at
    logger.info(
        "[WA token] refreshed merchant long-lived token — new_exp=%s tenant=%s",
        new_expires_at.isoformat(), getattr(conn, "tenant_id", "?"),
    )
    return build_token_context(conn, source="merchant_oauth")


async def get_token_for_operation(
    db: Session,
    conn: Any,
    *,
    tenant_id: Optional[int],
    operation: str,
    prefer_platform: bool = False,
    require_token: bool = True,
) -> WhatsAppTokenContext:
    candidates = get_token_candidates(conn, prefer_platform=prefer_platform)
    usable = next(
        (ctx for ctx in candidates if ctx.token and ctx.token_status in {"healthy", "expiring_soon"}),
        None,
    )
    if usable is None:
        refreshed = await _refresh_merchant_long_lived_token(conn)
        if refreshed and refreshed.token_status in {"healthy", "expiring_soon"}:
            candidates = get_token_candidates(conn, prefer_platform=prefer_platform)
            usable = next(
                (ctx for ctx in candidates if ctx.token and ctx.token_status in {"healthy", "expiring_soon"}),
                None,
            )
    selected = usable or next((ctx for ctx in candidates if ctx.token), build_token_context(conn, source="missing"))
    persist_token_context(db, conn, tenant_id=tenant_id, operation=operation, ctx=selected)
    if require_token and (not selected.token or selected.token_status == "expired"):
        raise RuntimeError(f"Missing WhatsApp operational token for operation={operation}")
    return selected
