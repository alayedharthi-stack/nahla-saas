"""Durable tenant-scoped storage for merchant template header images."""
from __future__ import annotations

import logging
import uuid

from services.catalog_media_storage import (
    CatalogMediaStorageError,
    CatalogMediaValidationError,
    catalog_media_bucket,
    catalog_media_public_base_url,
    catalog_media_r2_access_key_id,
    catalog_media_r2_endpoint,
    catalog_media_r2_secret_access_key,
    is_catalog_media_storage_configured,
    prepare_catalog_product_webp,
)

logger = logging.getLogger("nahla.template_media_storage")
OBJECT_PREFIX = "template-headers"


def _s3_client():
    if not is_catalog_media_storage_configured():
        raise CatalogMediaStorageError("template_media_storage_not_configured")
    import boto3  # lazy — only needed for merchant uploads

    return boto3.client(
        "s3",
        endpoint_url=catalog_media_r2_endpoint(),
        aws_access_key_id=catalog_media_r2_access_key_id(),
        aws_secret_access_key=catalog_media_r2_secret_access_key(),
        region_name="auto",
    )


def upload_template_header_image(*, tenant_id: int, content: bytes) -> dict:
    """Validate, normalize and persist a template header image in R2."""
    webp_bytes, content_type = prepare_catalog_product_webp(content)
    media_id = uuid.uuid4().hex
    key = f"{OBJECT_PREFIX}/{int(tenant_id)}/{media_id}.webp"
    try:
        _s3_client().put_object(
            Bucket=catalog_media_bucket(),
            Key=key,
            Body=webp_bytes,
            ContentType=content_type,
            CacheControl="public, max-age=31536000, immutable",
            Metadata={
                "tenant-id": str(int(tenant_id)),
                "status": "attached",
                "purpose": "whatsapp-template-header",
            },
        )
    except (CatalogMediaStorageError, CatalogMediaValidationError):
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("[template_media.upload] tenant=%s key=%s failed", tenant_id, key)
        raise CatalogMediaStorageError("template_header_upload_failed") from exc

    base = catalog_media_public_base_url().rstrip("/")
    if not base:
        raise CatalogMediaStorageError("template_media_public_base_missing")
    return {
        "image_url": f"{base}/{key}",
        "media_id": media_id,
        "content_type": content_type,
        "size_bytes": len(webp_bytes),
    }


__all__ = ["OBJECT_PREFIX", "upload_template_header_image"]
