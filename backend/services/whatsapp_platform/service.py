from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import httpx
from sqlalchemy.orm import Session

from core.config import D360_API_BASE_URL, META_GRAPH_API_VERSION
from .provider_utils import (
    WHATSAPP_PROVIDER_360DIALOG,
    wa_provider,
)
from .token_manager import WhatsAppTokenContext, get_token_for_operation

logger = logging.getLogger("nahla.whatsapp.service")

GRAPH = f"https://graph.facebook.com/{META_GRAPH_API_VERSION}"
D360_BASE = D360_API_BASE_URL.rstrip("/")


def _provider_base_url(conn: Any) -> str:
    provider = wa_provider(conn)
    if provider == WHATSAPP_PROVIDER_360DIALOG:
        return D360_BASE
    return GRAPH


def _provider_headers(conn: Any, ctx: WhatsAppTokenContext) -> Dict[str, str]:
    provider = wa_provider(conn)
    if provider == WHATSAPP_PROVIDER_360DIALOG:
        return {
            "D360-API-KEY": ctx.token,
            "Content-Type": "application/json",
        }
    return {
        "Authorization": f"Bearer {ctx.token}",
        "Content-Type": "application/json",
    }


def _provider_url(conn: Any, path: str) -> str:
    base = _provider_base_url(conn)
    clean = path.lstrip("/")
    return f"{base}/{clean}" if clean else base


async def provider_get_with_context(
    conn: Any,
    ctx: WhatsAppTokenContext,
    *,
    tenant_id: Optional[int],
    operation: str,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    timeout: float = 20,
) -> Dict[str, Any]:
    headers = _provider_headers(conn, ctx)
    if wa_provider(conn) == WHATSAPP_PROVIDER_360DIALOG:
        headers.pop("Content-Type", None)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(_provider_url(conn, path), headers=headers, params=params or {})
        data = resp.json()
    logger.info(
        "[WA provider_get] op=%s tenant=%s provider=%s path=%s status=%s source=%s",
        operation, tenant_id, wa_provider(conn), path, resp.status_code, ctx.source,
    )
    return data


async def provider_post_with_context(
    conn: Any,
    ctx: WhatsAppTokenContext,
    *,
    tenant_id: Optional[int],
    operation: str,
    path: str,
    json: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
    timeout: float = 20,
) -> Dict[str, Any]:
    headers = _provider_headers(conn, ctx)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            _provider_url(conn, path),
            headers=headers,
            json=json or {},
            params=params or {},
        )
        data = resp.json()
    logger.info(
        "[WA provider_post] op=%s tenant=%s provider=%s path=%s status=%s source=%s",
        operation, tenant_id, wa_provider(conn), path, resp.status_code, ctx.source,
    )
    return data


async def graph_get_with_context(
    ctx: WhatsAppTokenContext,
    *,
    tenant_id: Optional[int],
    operation: str,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    timeout: float = 20,
) -> Dict[str, Any]:
    return await provider_get_with_context(
        None,
        ctx,
        tenant_id=tenant_id,
        operation=operation,
        path=path,
        params=params,
        timeout=timeout,
    )


async def graph_post_with_context(
    ctx: WhatsAppTokenContext,
    *,
    tenant_id: Optional[int],
    operation: str,
    path: str,
    json: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
    timeout: float = 20,
) -> Dict[str, Any]:
    return await provider_post_with_context(
        None,
        ctx,
        tenant_id=tenant_id,
        operation=operation,
        path=path,
        json=json,
        params=params,
        timeout=timeout,
    )


async def graph_get(
    db: Session,
    conn: Any,
    *,
    tenant_id: Optional[int],
    operation: str,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    timeout: float = 20,
) -> tuple[Dict[str, Any], WhatsAppTokenContext]:
    ctx = await get_token_for_operation(
        db,
        conn,
        tenant_id=tenant_id,
        operation=operation,
    )
    data = await provider_get_with_context(
        conn,
        ctx,
        tenant_id=tenant_id,
        operation=operation,
        path=path,
        params=params,
        timeout=timeout,
    )
    return data, ctx


async def graph_post(
    db: Session,
    conn: Any,
    *,
    tenant_id: Optional[int],
    operation: str,
    path: str,
    json: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
    timeout: float = 20,
) -> tuple[Dict[str, Any], WhatsAppTokenContext]:
    ctx = await get_token_for_operation(
        db,
        conn,
        tenant_id=tenant_id,
        operation=operation,
    )
    data = await provider_post_with_context(
        conn,
        ctx,
        tenant_id=tenant_id,
        operation=operation,
        path=path,
        json=json,
        params=params,
        timeout=timeout,
    )
    return data, ctx


async def provider_send_message(
    db: Session,
    conn: Any,
    *,
    tenant_id: Optional[int],
    operation: str,
    phone_id: str,
    payload: Dict[str, Any],
    prefer_platform: bool = False,
    timeout: float = 20,
) -> tuple[Dict[str, Any], WhatsAppTokenContext]:
    ctx = await get_token_for_operation(
        db,
        conn,
        tenant_id=tenant_id,
        operation=operation,
        prefer_platform=prefer_platform,
    )
    provider = wa_provider(conn)
    send_payload = dict(payload or {})
    if provider == WHATSAPP_PROVIDER_360DIALOG:
        send_payload.setdefault("recipient_type", "individual")
        data = await provider_post_with_context(
            conn,
            ctx,
            tenant_id=tenant_id,
            operation=operation,
            path="messages",
            json=send_payload,
            timeout=timeout,
        )
        return data, ctx
    data = await provider_post_with_context(
        conn,
        ctx,
        tenant_id=tenant_id,
        operation=operation,
        path=f"{phone_id}/messages",
        json=send_payload,
        timeout=timeout,
    )
    return data, ctx


async def provider_submit_template(
    db: Session,
    conn: Any,
    *,
    tenant_id: Optional[int],
    waba_id: str,
    payload: Dict[str, Any],
    prefer_platform: bool = False,
    timeout: float = 20,
) -> tuple[Dict[str, Any], WhatsAppTokenContext]:
    ctx = await get_token_for_operation(
        db,
        conn,
        tenant_id=tenant_id,
        operation="template_submit",
        prefer_platform=prefer_platform,
    )
    provider = wa_provider(conn)
    path = "v1/configs/templates" if provider == WHATSAPP_PROVIDER_360DIALOG else f"{waba_id}/message_templates"
    data = await provider_post_with_context(
        conn,
        ctx,
        tenant_id=tenant_id,
        operation="template_submit",
        path=path,
        json=payload,
        timeout=timeout,
    )
    return data, ctx


async def provider_list_templates(
    db: Session,
    conn: Any,
    *,
    tenant_id: Optional[int],
    waba_id: str,
    prefer_platform: bool = False,
    timeout: float = 20,
) -> tuple[Dict[str, Any], WhatsAppTokenContext]:
    ctx = await get_token_for_operation(
        db,
        conn,
        tenant_id=tenant_id,
        operation="template_sync",
        prefer_platform=prefer_platform,
    )
    provider = wa_provider(conn)
    path = "v1/configs/templates" if provider == WHATSAPP_PROVIDER_360DIALOG else f"{waba_id}/message_templates"
    data = await provider_get_with_context(
        conn,
        ctx,
        tenant_id=tenant_id,
        operation="template_sync",
        path=path,
        timeout=timeout,
    )
    return data, ctx


async def dialog360_configure_webhook(
    *,
    api_key: str,
    url: str,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 20,
) -> Dict[str, Any]:
    req_headers = {
        "D360-API-KEY": api_key,
        "Content-Type": "application/json",
    }
    payload: Dict[str, Any] = {"url": url}
    if headers:
        payload["headers"] = headers
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(f"{D360_BASE}/v1/configs/webhook", headers=req_headers, json=payload)
        data = resp.json()
    logger.info("[WA dialog360 webhook] status=%s body=%s", resp.status_code, data)
    return data
