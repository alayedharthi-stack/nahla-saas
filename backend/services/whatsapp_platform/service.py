from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import httpx
from sqlalchemy.orm import Session

from core.config import D360_API_BASE_URL, D360_PARTNER_API_KEY, D360_PARTNER_HUB_BASE, META_GRAPH_API_VERSION
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


async def provider_delete_template(
    db: Session,
    conn: Any,
    *,
    tenant_id: Optional[int],
    waba_id: str,
    template_name: str,
    prefer_platform: bool = False,
    timeout: float = 20,
) -> Dict[str, Any]:
    """
    Delete a template from Meta by name.

    Meta API: DELETE /{waba_id}/message_templates?name={template_name}
    360dialog: DELETE v1/configs/templates?name={template_name}
    """
    ctx = await get_token_for_operation(
        db, conn,
        tenant_id=tenant_id,
        operation="template_delete",
        prefer_platform=prefer_platform,
    )
    provider = wa_provider(conn)

    if provider == WHATSAPP_PROVIDER_360DIALOG:
        path = f"v1/configs/templates"
    else:
        path = f"{waba_id}/message_templates"

    headers = _provider_headers(conn, ctx)
    url = _provider_url(conn, path)

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.delete(url, headers=headers, params={"name": template_name})
        data = resp.json()

    logger.info(
        "[WA template_delete] tenant=%s provider=%s name=%s status=%s",
        tenant_id, provider, template_name, resp.status_code,
    )
    return data


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

    if provider == WHATSAPP_PROVIDER_360DIALOG:
        path = "v1/configs/templates"
        params: Optional[Dict[str, Any]] = None
    else:
        path = f"{waba_id}/message_templates"
        # Explicitly request fields including `status` — without this
        # parameter Meta Graph API v20+ may omit the status field entirely,
        # causing every template to default to PENDING in the sync loop
        # (`item.get("status") or "PENDING"`).
        # `limit=250` avoids missing templates behind pagination.
        params = {
            "fields": "name,status,category,language,components,rejected_reason,quality_score,id",
            "limit": "250",
        }

    data = await provider_get_with_context(
        conn,
        ctx,
        tenant_id=tenant_id,
        operation="template_sync",
        path=path,
        params=params,
        timeout=timeout,
    )

    # ── Pagination: follow `paging.next` to collect ALL templates ─────────
    # Meta returns at most `limit` items per page. For accounts with
    # hundreds of templates we must follow the cursor chain.
    if provider != WHATSAPP_PROVIDER_360DIALOG:
        all_items = list(data.get("data") or [])
        next_url = (data.get("paging") or {}).get("next")
        pages = 0
        while next_url and pages < 20:  # safety cap
            pages += 1
            try:
                headers = _provider_headers(conn, ctx)
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.get(next_url, headers=headers)
                    page = resp.json()
                all_items.extend(page.get("data") or [])
                next_url = (page.get("paging") or {}).get("next")
            except Exception as exc:
                logger.warning(
                    "[WA template_sync] pagination failed tenant=%s page=%d: %s",
                    tenant_id, pages, exc,
                )
                break
        if pages:
            logger.info(
                "[WA template_sync] tenant=%s fetched %d extra page(s), total=%d templates",
                tenant_id, pages, len(all_items),
            )
        data = {**data, "data": all_items}

    return data, ctx


async def dialog360_configure_webhook(
    *,
    api_key: str,
    url: str,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 20,
) -> Dict[str, Any]:
    """Register (POST) the channel webhook URL with 360dialog.

    The endpoint accepts a single URL plus optional custom headers that
    360dialog will replay on every webhook delivery. Nahla uses this to
    inject the per-tenant `X-Nahla-Coexistence-Secret` header.
    """
    req_headers = {
        "D360-API-KEY": api_key,
        "Content-Type": "application/json",
    }
    payload: Dict[str, Any] = {"url": url}
    if headers:
        payload["headers"] = headers
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(f"{D360_BASE}/v1/configs/webhook", headers=req_headers, json=payload)
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}
    logger.info("[WA dialog360 webhook] configure status=%s body=%s", resp.status_code, data)
    if resp.status_code >= 400 and "error" not in data:
        data = {"error": data, "status_code": resp.status_code}
    return data


async def dialog360_get_webhook_config(
    *,
    api_key: str,
    timeout: float = 15,
) -> Dict[str, Any]:
    """Read back the currently configured channel webhook from 360dialog.

    Used by the owner-panel "Verify" action: we compare the URL 360dialog has
    on file against the URL Nahla expects and surface a mismatch instead of
    silently trusting the local cache.
    """
    req_headers = {"D360-API-KEY": api_key}
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(f"{D360_BASE}/v1/configs/webhook", headers=req_headers)
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}
    logger.info("[WA dialog360 webhook] read status=%s body=%s", resp.status_code, data)
    if resp.status_code >= 400:
        return {"error": data, "status_code": resp.status_code}
    return data


# ── 360dialog Partner API helpers ─────────────────────────────────────────────

_D360_PARTNER_HUB = D360_PARTNER_HUB_BASE.rstrip("/")


async def dialog360_generate_api_key(
    *,
    partner_id: str,
    channel_id: str,
    timeout: float = 20,
) -> Dict[str, Any]:
    """
    Generate (or retrieve) the D360-API-KEY for a channel the merchant connected
    during Integrated Onboarding.

    POST https://hub.360dialog.com/api/v2/partners/{partner_id}/channels/{channel_id}/api-keys
    Authorization: Bearer {D360_PARTNER_API_KEY}
    """
    if not D360_PARTNER_API_KEY:
        return {"error": "D360_PARTNER_API_KEY not configured"}
    url = f"{_D360_PARTNER_HUB}/api/v2/partners/{partner_id}/channels/{channel_id}/api-keys"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            url,
            headers={
                "Authorization": f"Bearer {D360_PARTNER_API_KEY}",
                "Content-Type": "application/json",
            },
        )
    try:
        data = resp.json()
    except Exception:
        data = {"raw": resp.text}
    logger.info(
        "[D360 partner] generate_api_key partner=%s channel=%s status=%s",
        partner_id, channel_id, resp.status_code,
    )
    return data


async def dialog360_get_channel_info(
    *,
    partner_id: str,
    channel_id: str,
    timeout: float = 20,
) -> Dict[str, Any]:
    """
    Retrieve channel details (status, phone_number, waba_id, etc.) from Partner API.

    GET https://hub.360dialog.com/api/v2/partners/{partner_id}/channels/{channel_id}
    """
    if not D360_PARTNER_API_KEY:
        return {"error": "D360_PARTNER_API_KEY not configured"}
    url = f"{_D360_PARTNER_HUB}/api/v2/partners/{partner_id}/channels/{channel_id}"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(
            url,
            headers={"Authorization": f"Bearer {D360_PARTNER_API_KEY}"},
        )
    try:
        return resp.json()
    except Exception:
        return {"raw": resp.text}


async def fetch_meta_phone_tier(
    conn: Any,
    ctx: WhatsAppTokenContext,
    *,
    tenant_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Fetch messaging_limit and quality_rating from Meta Graph API for the phone.

    GET /{phone_number_id}?fields=messaging_limit_tier,quality_rating
    """
    phone_id = getattr(conn, "phone_number_id", None)
    if not phone_id or not ctx.token:
        return {}
    try:
        data = await provider_get_with_context(
            conn, ctx,
            tenant_id=tenant_id,
            operation="fetch_phone_tier",
            path=f"{phone_id}",
            params={"fields": "messaging_limit_tier,quality_rating"},
            timeout=15,
        )
        return {
            "messaging_limit": data.get("messaging_limit_tier"),
            "quality_rating":  data.get("quality_rating"),
        }
    except Exception as exc:
        logger.warning("[WA] fetch_meta_phone_tier failed tenant=%s: %s", tenant_id, exc)
        return {}
