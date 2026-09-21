"""Merchant IMAGE headers: durable draft URL, Meta sample, and send parameter.

Saving/uploading never calls Meta. Submission uses a fresh resumable-upload
handle; campaign delivery uses the persisted URL, never that sample handle.
"""
from __future__ import annotations

import copy
from urllib.parse import urlsplit

import httpx

from core.config import META_APP_ID, META_GRAPH_API_VERSION
from services.template_media_storage import image_url_owned_by_tenant

MISSING_IMAGE = "صورة رأس القالب مفقودة. ارفع صورة JPG أو PNG واحفظ المسودة أولاً."


class MerchantHeaderUploader:
    """Merchant template samples, with all documented resumable-upload fields.

    https://developers.facebook.com/docs/graph-api/guides/upload/
    The existing lifecycle uploader retains its separate ownership/behavior.
    """

    async def upload_template_header(self, *, access_token, image_bytes, mime_type):
        if not META_APP_ID or not str(access_token or '').strip():
            raise ValueError('missing_meta_upload_configuration')
        graph = f'https://graph.facebook.com/{META_GRAPH_API_VERSION}'
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(f'{graph}/{META_APP_ID}/uploads', params={
                'file_name': 'template-header.png' if mime_type == 'image/png' else 'template-header.jpg',
                'file_length': str(len(image_bytes)), 'file_type': mime_type,
                'access_token': access_token,
            })
            response.raise_for_status()
            session_id = str(response.json().get('id') or '').strip()
            if not session_id:
                raise ValueError('meta_upload_session_missing')
            response = await client.post(f'{graph}/{session_id}', headers={
                'Authorization': f'OAuth {access_token}', 'file_offset': '0',
                'Content-Type': 'application/octet-stream',
            }, content=image_bytes)
            response.raise_for_status()
            handle = str(response.json().get('h') or '').strip()
            if not handle:
                raise ValueError('meta_upload_handle_missing')
            return handle


def image_header(components):
    return next((c for c in components or [] if c.get("type", "").upper() == "HEADER"
                 and c.get("format", "").upper() == "IMAGE"), None)


def header_url(components):
    header = image_header(components)
    example = header.get("example") if header else None
    return str(example.get("header_url") or "").strip() if isinstance(example, dict) else ""


def validate_draft_image(components, *, tenant_id, previous=()):
    """New image references must originate in this tenant's authenticated upload."""
    headers = [c for c in components if c.get("type", "").upper() == "HEADER"]
    if len(headers) > 1:
        raise ValueError("يمكن إضافة رأس واحد فقط للقالب.")
    if image_header(components) is None:
        return
    url = header_url(components)
    if not url:
        raise ValueError(MISSING_IMAGE)
    if url != header_url(previous) and not image_url_owned_by_tenant(tenant_id, url):
        raise ValueError("يجب رفع صورة الرأس من حساب هذا المتجر.")


def preserve_image_on_sync(stored, received):
    """Meta returns a review sample; keep our durable sending URL after approval."""
    result = copy.deepcopy(received)
    header = image_header(result)
    url = header_url(stored)
    if header is not None and url:
        header["example"] = {**(header.get("example") or {}), "header_url": url}
    return result


def campaign_image_parameter(components):
    if image_header(components) is None:
        return None
    url = header_url(components)
    parsed = urlsplit(url)
    if not url or parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError(MISSING_IMAGE)
    return {"type": "header", "parameters": [{"type": "image", "image": {"link": url}}]}


async def prepare_merchant_image_for_meta(db, conn, *, tenant_id, components, uploader=None):
    if image_header(components) is None:
        return copy.deepcopy(components)
    from core.commerce_lifecycle.order_confirmation_meta_header import (
        prepare_order_confirmation_meta_submit_components,
    )
    from core.commerce_lifecycle.order_confirmation_header_image_fetch import fetch_header_image_bytes_secure
    from services.whatsapp_platform.token_manager import get_token_for_operation
    from services.template_media_storage import prepare_template_image

    url = header_url(components)
    if not url:
        raise ValueError(MISSING_IMAGE)
    # Public HTTPS fetch is DNS-pinned, bounded, and rejects internal addresses.
    content, _mime = await fetch_header_image_bytes_secure(url)
    content, mime = prepare_template_image(content)
    ctx = await get_token_for_operation(db, conn, tenant_id=tenant_id, operation="template_submit")
    handle = await (uploader or MerchantHeaderUploader()).upload_template_header(
        access_token=ctx.token, image_bytes=content, mime_type=mime,
    )
    return prepare_order_confirmation_meta_submit_components(components, header_handle=handle)
