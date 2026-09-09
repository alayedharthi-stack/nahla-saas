"""
Meta submit preparation for order_confirmation IMAGE HEADER components.

Uploads the platform header image and replaces ``header_url`` with a valid
``header_handle`` in the outbound Meta template payload.
"""
from __future__ import annotations

import copy
import logging
from typing import Any, Dict, List, Optional, Protocol

import httpx

from core.commerce_lifecycle.order_confirmation_assets import (
    ORDER_CONFIRMATION_HEADER_ASSET_KEY,
    order_confirmation_header_public_url,
)
from core.config import META_APP_ID, META_GRAPH_API_VERSION

logger = logging.getLogger("nahla.commerce_lifecycle.order_confirmation_meta_header")

GRAPH = f"https://graph.facebook.com/{META_GRAPH_API_VERSION}"


class HeaderImageUploader(Protocol):
    async def upload_template_header(
        self,
        *,
        access_token: str,
        image_bytes: bytes,
        mime_type: str,
    ) -> str: ...


def _image_header_component(components: Any) -> Optional[Dict[str, Any]]:
    for comp in components or []:
        if (
            str((comp or {}).get("type", "")).upper() == "HEADER"
            and str((comp or {}).get("format", "")).upper() == "IMAGE"
        ):
            return comp
    return None


def resolve_header_image_source_url(
    components: Any,
    metadata: Optional[Dict[str, Any]] = None,
) -> str:
    meta = dict(metadata or {})
    from_meta = str(meta.get("header_image_url") or "").strip()
    if from_meta:
        return from_meta
    header = _image_header_component(components)
    if header:
        example = dict(header.get("example") or {})
        from_comp = str(example.get("header_url") or "").strip()
        if from_comp:
            return from_comp
    return order_confirmation_header_public_url()


def prepare_order_confirmation_meta_submit_components(
    components: List[Dict[str, Any]],
    *,
    header_handle: str,
) -> List[Dict[str, Any]]:
    """Build Meta-facing payload: header_handle only, no internal header_url."""
    handle = str(header_handle or "").strip()
    if not handle:
        raise ValueError("missing_header_handle")
    out: List[Dict[str, Any]] = []
    for raw in components or []:
        comp = copy.deepcopy(raw)
        if (
            str(comp.get("type", "")).upper() == "HEADER"
            and str(comp.get("format", "")).upper() == "IMAGE"
        ):
            comp["example"] = {"header_handle": [handle]}
        out.append(comp)
    return out


async def fetch_header_image_bytes(url: str, *, timeout: float = 30.0) -> tuple[bytes, str]:
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        mime = str(resp.headers.get("content-type") or "image/jpeg").split(";")[0].strip()
        if not mime.startswith("image/"):
            raise ValueError("header_image_not_image")
        return resp.content, mime


class MetaResumableHeaderUploader:
    """Upload template header bytes via Meta resumable upload API."""

    async def upload_template_header(
        self,
        *,
        access_token: str,
        image_bytes: bytes,
        mime_type: str,
    ) -> str:
        if not META_APP_ID:
            raise ValueError("missing_meta_app_id")
        token = str(access_token or "").strip()
        if not token:
            raise ValueError("missing_access_token")
        file_len = len(image_bytes)
        file_type = mime_type or "image/jpeg"
        async with httpx.AsyncClient(timeout=60.0) as client:
            session_resp = await client.post(
                f"{GRAPH}/{META_APP_ID}/uploads",
                params={
                    "file_length": str(file_len),
                    "file_type": file_type,
                    "access_token": token,
                },
            )
            session_resp.raise_for_status()
            session_id = str((session_resp.json() or {}).get("id") or "").strip()
            if not session_id:
                raise ValueError("meta_upload_session_missing")

            upload_resp = await client.post(
                f"{GRAPH}/{session_id}",
                headers={
                    "Authorization": f"OAuth {token}",
                    "file_offset": "0",
                    "Content-Type": "application/octet-stream",
                },
                content=image_bytes,
            )
            upload_resp.raise_for_status()
            handle = str((upload_resp.json() or {}).get("h") or "").strip()
            if not handle:
                raise ValueError("meta_upload_handle_missing")
            return handle


async def ensure_order_confirmation_image_header_for_meta(
    db: Any,
    conn: Any,
    *,
    tenant_id: int,
    components: List[Dict[str, Any]],
    metadata: Optional[Dict[str, Any]] = None,
    uploader: Optional[HeaderImageUploader] = None,
) -> List[Dict[str, Any]]:
    """
    Resolve/upload IMAGE header and return Meta-safe components for submit.

    Reuses ``meta_header_handle`` from metadata when present.
    """
    if _image_header_component(components) is None:
        return list(components or [])

    meta = dict(metadata or {})
    cached = str(meta.get("meta_header_handle") or "").strip()
    if cached:
        return prepare_order_confirmation_meta_submit_components(
            components,
            header_handle=cached,
        )

    from services.whatsapp_platform.token_manager import get_token_for_operation  # noqa: PLC0415

    ctx = await get_token_for_operation(
        db,
        conn,
        tenant_id=int(tenant_id),
        operation="template_submit",
    )
    access_token = str(ctx.access_token or "").strip()
    if not access_token:
        raise ValueError("missing_access_token")

    source_url = resolve_header_image_source_url(components, meta)
    image_bytes, mime_type = await fetch_header_image_bytes(source_url)
    upload = uploader or MetaResumableHeaderUploader()
    handle = await upload.upload_template_header(
        access_token=access_token,
        image_bytes=image_bytes,
        mime_type=mime_type,
    )
    logger.info(
        "[order_confirmation_meta_header] uploaded header asset_key=%s bytes=%d",
        ORDER_CONFIRMATION_HEADER_ASSET_KEY,
        len(image_bytes),
    )
    return prepare_order_confirmation_meta_submit_components(
        components,
        header_handle=handle,
    )


__all__ = [
    "MetaResumableHeaderUploader",
    "ensure_order_confirmation_image_header_for_meta",
    "fetch_header_image_bytes",
    "prepare_order_confirmation_meta_submit_components",
    "resolve_header_image_source_url",
]
