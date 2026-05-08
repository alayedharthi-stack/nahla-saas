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
    timeout: float = 5,
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
    timeout: float = 5,
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
    timeout: float = 5,
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
    timeout: float = 5,
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


async def dialog360_resolve_channel_metadata(
    *,
    api_key: str,
    phone_number_id: Optional[str] = None,
    channel_id: Optional[str] = None,
    partner_id: Optional[str] = None,
    timeout: float = 5,
) -> Dict[str, Any]:
    """Best-effort resolver for missing 360dialog channel metadata.

    Calls every reasonable 360dialog endpoint we have credentials for and
    merges the results into a single normalised payload:

        {
          "waba_id":         str | None,
          "phone_number_id": str | None,
          "phone_number":    str | None,
          "display_name":    str | None,
          "channel_status":  str | None,
          "sources":         [str, ...],   # which endpoints contributed
          "errors":          {endpoint: error_msg},
          "raw":             {endpoint: raw response},
        }

    Resolution sources, in priority order:

      1. **Partner API** (`hub.360dialog.com/api/v2/partners/.../channels/...`)
         — Most authoritative when we have D360_PARTNER_API_KEY + channel_id.
         Returns waba_id, phone_number, status, etc.
      2. **Channel API: GET /v1/configs** with the per-tenant `D360-API-KEY`
         — Returns webhook config + sometimes ``on_behalf_of_business_info``
         and the channel's own phone metadata.
      3. **Phone object endpoint**: ``GET /<phone_number_id>`` against the
         WABA-V2 host using the api_key as a Meta-style bearer. 360dialog's
         WABA-V2 cluster mirrors Meta Cloud API for this path and returns
         ``display_phone_number`` + ``verified_name`` when the channel is
         active.

    The caller decides what to persist; the resolver itself is read-only."""
    out: Dict[str, Any] = {
        "waba_id":         None,
        "phone_number_id": phone_number_id,
        "phone_number":    None,
        "display_name":    None,
        "channel_status":  None,
        "sources":         [],
        "errors":          {},
        "raw":             {},
    }

    if not api_key and not (partner_id and channel_id):
        out["errors"]["resolver"] = "no credentials available"
        return out

    # ── 1. Partner API ─────────────────────────────────────────────────
    if partner_id and channel_id and D360_PARTNER_API_KEY:
        try:
            info = await dialog360_get_channel_info(partner_id=partner_id, channel_id=channel_id)
            out["raw"]["partner"] = info
            if isinstance(info, dict) and "error" not in info:
                out["waba_id"]        = out["waba_id"] or info.get("waba_id") or info.get("waba_account_id")
                out["phone_number"]   = out["phone_number"] or info.get("phone_number") or info.get("phone")
                out["display_name"]   = out["display_name"] or info.get("name") or info.get("verified_name")
                out["channel_status"] = out["channel_status"] or info.get("status")
                out["sources"].append("partner")
            elif isinstance(info, dict) and "error" in info:
                out["errors"]["partner"] = str(info.get("error"))[:200]
        except Exception as exc:
            out["errors"]["partner"] = f"{type(exc).__name__}: {exc}"[:200]

    # ── 2. Channel-level GET /v1/configs ───────────────────────────────
    if api_key:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(
                    f"{D360_BASE}/v1/configs",
                    headers={"D360-API-KEY": api_key},
                )
                try:
                    data = resp.json()
                except Exception:
                    data = {"raw": resp.text}
            out["raw"]["v1_configs"] = {"status_code": resp.status_code, "body": data}
            if 200 <= resp.status_code < 300 and isinstance(data, dict):
                # 360dialog mixes flat + nested shapes across product
                # versions. Probe both.
                obo = data.get("on_behalf_of_business_info") or {}
                phone = data.get("phone") or data.get("phone_number") or {}
                out["waba_id"] = (
                    out["waba_id"]
                    or data.get("waba_id")
                    or data.get("waba_account_id")
                    or obo.get("waba_id")
                    or obo.get("id")
                )
                out["phone_number_id"] = (
                    out["phone_number_id"]
                    or data.get("phone_number_id")
                    or (phone.get("id") if isinstance(phone, dict) else None)
                )
                out["phone_number"] = (
                    out["phone_number"]
                    or data.get("display_phone_number")
                    or (phone.get("display_phone_number") if isinstance(phone, dict) else None)
                )
                out["display_name"] = (
                    out["display_name"]
                    or data.get("verified_name")
                    or (phone.get("verified_name") if isinstance(phone, dict) else None)
                )
                out["sources"].append("v1_configs")
            elif resp.status_code >= 400:
                out["errors"]["v1_configs"] = f"http_{resp.status_code}: {str(data)[:200]}"
        except Exception as exc:
            out["errors"]["v1_configs"] = f"{type(exc).__name__}: {exc}"[:200]

    # ── 3. Phone object endpoint (WABA-V2 / Cloud API parity) ──────────
    pnid = out["phone_number_id"]
    if api_key and pnid:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(
                    f"{D360_BASE}/{pnid}",
                    headers={"D360-API-KEY": api_key},
                    params={"fields": "id,display_phone_number,verified_name,quality_rating,whatsapp_business_account"},
                )
                try:
                    data = resp.json()
                except Exception:
                    data = {"raw": resp.text}
            out["raw"]["phone_object"] = {"status_code": resp.status_code, "body": data}
            if 200 <= resp.status_code < 300 and isinstance(data, dict):
                wba = data.get("whatsapp_business_account") or {}
                out["waba_id"]      = out["waba_id"] or (wba.get("id") if isinstance(wba, dict) else None)
                out["phone_number"] = out["phone_number"] or data.get("display_phone_number")
                out["display_name"] = out["display_name"] or data.get("verified_name")
                out["sources"].append("phone_object")
            elif resp.status_code >= 400:
                out["errors"]["phone_object"] = f"http_{resp.status_code}: {str(data)[:200]}"
        except Exception as exc:
            out["errors"]["phone_object"] = f"{type(exc).__name__}: {exc}"[:200]

    logger.info(
        "[D360 resolver] phone_number_id=%s channel_id=%s sources=%s errors=%s "
        "→ waba=%s phone=%s name=%s",
        phone_number_id, channel_id, out["sources"], list(out["errors"].keys()),
        out["waba_id"], out["phone_number"], out["display_name"],
    )
    return out


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
