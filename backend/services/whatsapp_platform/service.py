from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import httpx
from sqlalchemy.orm import Session

from core.config import META_GRAPH_API_VERSION
from .token_manager import WhatsAppTokenContext, get_token_for_operation

logger = logging.getLogger("nahla.whatsapp.service")

GRAPH = f"https://graph.facebook.com/{META_GRAPH_API_VERSION}"


async def graph_get_with_context(
    ctx: WhatsAppTokenContext,
    *,
    tenant_id: Optional[int],
    operation: str,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    timeout: float = 20,
) -> Dict[str, Any]:
    headers = {"Authorization": f"Bearer {ctx.token}"}
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(f"{GRAPH}/{path.lstrip('/')}", headers=headers, params=params or {})
        data = resp.json()
    logger.info(
        "[WA graph_get] op=%s tenant=%s path=%s status=%s source=%s",
        operation, tenant_id, path, resp.status_code, ctx.source,
    )
    return data


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
    headers = {
        "Authorization": f"Bearer {ctx.token}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            f"{GRAPH}/{path.lstrip('/')}",
            headers=headers,
            json=json or {},
            params=params or {},
        )
        data = resp.json()
    logger.info(
        "[WA graph_post] op=%s tenant=%s path=%s status=%s source=%s",
        operation, tenant_id, path, resp.status_code, ctx.source,
    )
    return data


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
    data = await graph_get_with_context(
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
    data = await graph_post_with_context(
        ctx,
        tenant_id=tenant_id,
        operation=operation,
        path=path,
        json=json,
        params=params,
        timeout=timeout,
    )
    return data, ctx
