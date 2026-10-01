"""Durable, tenant-scoped storage for storefront widget images."""
from __future__ import annotations

import logging
import uuid

from services.catalog_media_storage import (
    CatalogMediaStorageError,
    CatalogMediaValidationError,
    catalog_media_bucket,
    catalog_media_public_base_url,
    is_catalog_media_storage_configured,
)
from services.template_media_storage import _s3_client, prepare_template_image

logger = logging.getLogger("nahla.widget_media_storage")
OBJECT_PREFIX = "widget-logos"


def upload_widget_logo(*, tenant_id: int, content: bytes) -> dict:
    """Keep PNG transparency and store an immutable public JPEG or PNG."""
    image_bytes, content_type = prepare_template_image(content)
    if not is_catalog_media_storage_configured():
        raise CatalogMediaStorageError("widget_media_storage_not_configured")
    base = catalog_media_public_base_url().rstrip("/")
    if not base:
        raise CatalogMediaStorageError("widget_media_public_base_missing")

    media_id = uuid.uuid4().hex
    extension = "jpg" if content_type == "image/jpeg" else "png"
    key = f"{OBJECT_PREFIX}/{int(tenant_id)}/{media_id}.{extension}"
    try:
        _s3_client().put_object(
            Bucket=catalog_media_bucket(),
            Key=key,
            Body=image_bytes,
            ContentType=content_type,
            CacheControl="public, max-age=31536000, immutable",
            Metadata={
                "tenant-id": str(int(tenant_id)),
                "status": "attached",
                "purpose": "storefront-widget-logo",
            },
        )
    except (CatalogMediaStorageError, CatalogMediaValidationError):
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("[widget_media.upload] tenant=%s key=%s failed", tenant_id, key)
        raise CatalogMediaStorageError("widget_logo_upload_failed") from exc

    return {
        "image_url": f"{base}/{key}",
        "media_id": media_id,
        "content_type": content_type,
        "size_bytes": len(image_bytes),
    }
