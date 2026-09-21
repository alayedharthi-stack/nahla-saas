"""Durable tenant-scoped storage for merchant template header images."""
from __future__ import annotations

import logging
import io
import re
import uuid

from PIL import Image

from services.catalog_media_storage import (
    CatalogMediaStorageError,
    CatalogMediaValidationError,
    catalog_media_bucket,
    catalog_media_public_base_url,
    catalog_media_r2_access_key_id,
    catalog_media_r2_endpoint,
    catalog_media_r2_secret_access_key,
    is_catalog_media_storage_configured,
    MAX_UPLOAD_BYTES,
    MAX_IMAGE_PIXELS,
    sniff_image_mime,
)

logger = logging.getLogger("nahla.template_media_storage")
OBJECT_PREFIX = "template-headers"


def prepare_template_image(content: bytes) -> tuple[bytes, str]:
    """Meta IMAGE headers accept JPEG/PNG, not the catalog's WEBP output."""
    if not content:
        raise CatalogMediaValidationError("empty_file")
    if len(content) > MAX_UPLOAD_BYTES:
        raise CatalogMediaValidationError("file_too_large")
    mime = sniff_image_mime(content)
    if mime not in {"image/jpeg", "image/png"}:
        raise CatalogMediaValidationError("unsupported_image_type")
    try:
        with Image.open(io.BytesIO(content)) as image:
            if image.width * image.height > MAX_IMAGE_PIXELS:
                raise CatalogMediaValidationError("image_pixel_count_too_large")
            if getattr(image, "n_frames", 1) != 1:
                raise CatalogMediaValidationError("animated_image_not_supported")
            image.verify()
        with Image.open(io.BytesIO(content)) as image:
            image.load()
    except CatalogMediaValidationError:
        raise
    except Exception as exc:
        raise CatalogMediaValidationError("invalid_image") from exc
    return content, mime


def image_url_owned_by_tenant(tenant_id: int, url: str) -> bool:
    base = catalog_media_public_base_url().rstrip("/")
    prefix = f"{base}/{OBJECT_PREFIX}/{int(tenant_id)}/"
    if not base or not str(url).startswith(prefix):
        return False
    return bool(re.fullmatch(r"[a-f0-9]{32}\.(?:jpg|png)", url[len(prefix):]))


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
    """Validate and persist the original JPEG/PNG template header in R2."""
    image_bytes, content_type = prepare_template_image(content)
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
        "size_bytes": len(image_bytes),
    }


__all__ = ["OBJECT_PREFIX", "upload_template_header_image"]
