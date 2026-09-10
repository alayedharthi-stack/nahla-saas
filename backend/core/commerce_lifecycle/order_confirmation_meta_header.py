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
from core.commerce_lifecycle.order_confirmation_header_image_fetch import (
    HeaderImageFetchError,
    fetch_header_image_bytes_secure,
)
from services.safe_http_fetch import redact_url_for_log
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


def resolve_order_confirmation_preview_header_url(
    db: Any,
    tenant_id: int,
    components: Any,
    metadata: Optional[Dict[str, Any]] = None,
) -> str:
    """Merchant-facing preview URL for order_confirmation IMAGE header only."""
    return resolve_header_image_source_url(
        components,
        metadata,
        tenant_runtime_url=_tenant_runtime_header_image_url(db, int(tenant_id)),
    )


def resolve_header_image_source_url(
    components: Any,
    metadata: Optional[Dict[str, Any]] = None,
    *,
    tenant_runtime_url: Optional[str] = None,
) -> str:
    """
    Single source of truth for order_confirmation header image URL.

    Precedence: tenant runtime override → HEADER component URL → explicit
    ``merchant_header_image_url`` metadata → platform R2 default.
    """
    if tenant_runtime_url:
        return str(tenant_runtime_url).strip()

    header = _image_header_component(components)
    if header:
        example = dict(header.get("example") or {})
        from_component = str(example.get("header_url") or "").strip()
        if from_component:
            return from_component

    meta = dict(metadata or {})
    explicit = str(meta.get("merchant_header_image_url") or "").strip()
    if explicit:
        return explicit

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


def _tenant_runtime_header_image_url(db: Any, tenant_id: int) -> Optional[str]:
    """Read merchant runtime header override from TenantSettings (order_confirmation)."""
    try:
        from models import TenantSettings  # noqa: PLC0415

        settings = (
            db.query(TenantSettings)
            .filter(TenantSettings.tenant_id == int(tenant_id))
            .first()
        )
        if settings is None:
            return None
        extra = getattr(settings, "extra_metadata", None)
        if not isinstance(extra, dict):
            return None
        bucket = dict(extra.get("order_updates") or {})
        slot = bucket.get("order_confirmation")
        if not isinstance(slot, dict):
            return None
        runtime = slot.get("runtime")
        if not isinstance(runtime, dict):
            return None
        raw = str(runtime.get("header_image_url") or "").strip()
        return raw or None
    except Exception:  # noqa: silent-ok — runtime override is optional
        logger.exception("[order_confirmation_meta_header] tenant runtime header lookup failed")
        return None


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

    tenant_runtime = _tenant_runtime_header_image_url(db, int(tenant_id))
    source_url = resolve_header_image_source_url(
        components,
        meta,
        tenant_runtime_url=tenant_runtime,
    )
    try:
        image_bytes, mime_type = await fetch_header_image_bytes_secure(source_url)
    except HeaderImageFetchError as exc:
        logger.warning(
            "[order_confirmation_meta_header] secure fetch blocked code=%s url=%s",
            exc.error_code,
            redact_url_for_log(source_url),
        )
        raise ValueError("header_image_fetch_blocked") from exc

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
    "prepare_order_confirmation_meta_submit_components",
    "resolve_header_image_source_url",
    "resolve_order_confirmation_preview_header_url",
]
