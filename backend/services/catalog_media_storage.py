"""Cloudflare R2 storage for manual catalog product images.

Dedicated to merchant catalog uploads — not AI media, not inbound media.
Public URLs are built from ``NAHLA_CATALOG_MEDIA_PUBLIC_BASE_URL`` so ops
can later switch to ``https://media.nahlah.ai`` without code changes.

Ops notes
---------
* ``NAHLA_CATALOG_MEDIA_PUBLIC_BASE_URL`` must be the bucket's **Public
  Development URL** (``*.r2.dev``) or a future custom domain — **not**
  the S3 API endpoint (``*.r2.cloudflarestorage.com``).
* Lifecycle: uploads start as ``metadata.status=pending``. Product
  create/patch promotes them to ``attached`` with ``product-id``. Any
  future cleanup job must delete **only** ``pending`` objects older than
  a safe TTL — never ``attached``.
"""
from __future__ import annotations

import io
import logging
import os
import uuid
from typing import Optional, Tuple

from PIL import Image, ImageOps

_logger = logging.getLogger("nahla.catalog_media_storage")

# ── Limits ────────────────────────────────────────────────────────────────────
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_IMAGE_DIMENSION = 4096
MAX_IMAGE_PIXELS = 16_777_216  # 4096 × 4096 — decompression-bomb guard
ALLOWED_INPUT_MIMES = frozenset({"image/jpeg", "image/png", "image/webp"})
OBJECT_PREFIX = "catalog-products"


class CatalogMediaValidationError(ValueError):
    """Raised when uploaded bytes fail catalog image validation."""


class CatalogMediaStorageError(RuntimeError):
    """Raised when R2 is misconfigured or the upload fails."""


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name, default) or default).strip()


def catalog_media_bucket() -> str:
    return _env("NAHLA_CATALOG_MEDIA_BUCKET", "nahlah-media")


def catalog_media_public_base_url() -> str:
    """HTTPS base for public object URLs (no trailing slash).

    Today this is typically the R2.dev subdomain. Later ops can point
  ``media.nahlah.ai`` here via DNS without touching upload logic.
    """
    return _env("NAHLA_CATALOG_MEDIA_PUBLIC_BASE_URL").rstrip("/")


def catalog_media_r2_endpoint() -> str:
    return _env("NAHLA_CATALOG_MEDIA_R2_ENDPOINT").rstrip("/")


def catalog_media_r2_access_key_id() -> str:
    return _env("NAHLA_CATALOG_MEDIA_R2_ACCESS_KEY_ID")


def catalog_media_r2_secret_access_key() -> str:
    return _env("NAHLA_CATALOG_MEDIA_R2_SECRET_ACCESS_KEY")


def is_catalog_media_storage_configured() -> bool:
    return bool(
        catalog_media_public_base_url()
        and catalog_media_r2_endpoint()
        and catalog_media_r2_access_key_id()
        and catalog_media_r2_secret_access_key()
        and catalog_media_bucket()
    )


def sniff_image_mime(content: bytes) -> Optional[str]:
    """Detect JPEG / PNG / WEBP from magic bytes — not from headers."""
    if len(content) < 12:
        return None
    if content[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if content[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    return None


def prepare_catalog_product_webp(content: bytes) -> Tuple[bytes, str]:
    """Validate, normalise orientation, strip metadata, encode as WEBP."""
    if not content:
        raise CatalogMediaValidationError("empty_file")
    if len(content) > MAX_UPLOAD_BYTES:
        raise CatalogMediaValidationError("file_too_large")

    sniffed = sniff_image_mime(content)
    if sniffed not in ALLOWED_INPUT_MIMES:
        raise CatalogMediaValidationError("unsupported_image_type")

    try:
        with Image.open(io.BytesIO(content)) as img:
            img = ImageOps.exif_transpose(img)
            img.load()
            width, height = img.size
            if width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION:
                raise CatalogMediaValidationError("image_dimensions_too_large")
            if width * height > MAX_IMAGE_PIXELS:
                raise CatalogMediaValidationError("image_pixel_count_too_large")

            if img.mode in ("RGBA", "LA") or (
                img.mode == "P" and "transparency" in img.info
            ):
                converted = img.convert("RGBA")
            else:
                converted = img.convert("RGB")

            out = io.BytesIO()
            converted.save(out, format="WEBP", quality=85, method=6)
            webp_bytes = out.getvalue()
    except CatalogMediaValidationError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise CatalogMediaValidationError("invalid_image") from exc

    if len(webp_bytes) > MAX_UPLOAD_BYTES:
        raise CatalogMediaValidationError("file_too_large")
    return webp_bytes, "image/webp"


def object_key_for_tenant(tenant_id: int, media_id: str) -> str:
    tid = int(tenant_id)
    mid = (media_id or "").strip()
    if not mid or "/" in mid or ".." in mid:
        raise CatalogMediaValidationError("invalid_media_id")
    return f"{OBJECT_PREFIX}/{tid}/{mid}.webp"


def public_url_for_object_key(object_key: str) -> str:
    base = catalog_media_public_base_url()
    if not base:
        raise CatalogMediaStorageError("catalog_media_public_base_missing")
    key = (object_key or "").lstrip("/")
    return f"{base}/{key}"


def tenant_image_url_prefix(tenant_id: int) -> str:
    base = catalog_media_public_base_url()
    return f"{base}/{OBJECT_PREFIX}/{int(tenant_id)}/"


def image_url_owned_by_tenant(tenant_id: int, image_url: Optional[str]) -> bool:
    """Return True when *image_url* points at this tenant's catalog prefix."""
    url = (image_url or "").strip()
    if not url:
        return True
    prefix = tenant_image_url_prefix(tenant_id)
    return url.startswith(prefix)


def object_key_from_public_url(image_url: str) -> Optional[str]:
    """Map a configured public URL back to its R2 object key, if ours."""
    url = (image_url or "").strip()
    base = catalog_media_public_base_url()
    if not base or not url.startswith(f"{base}/"):
        return None
    key = url[len(base) + 1 :]
    if not key or ".." in key or key.startswith("/"):
        return None
    return key


def is_managed_catalog_image_url(image_url: Optional[str]) -> bool:
    key = object_key_from_public_url((image_url or "").strip())
    return bool(key and key.startswith(f"{OBJECT_PREFIX}/"))


def _head_object_metadata(object_key: str) -> dict:
    client = _s3_client()
    try:
        resp = client.head_object(
            Bucket=catalog_media_bucket(),
            Key=object_key,
        )
    except Exception as exc:  # noqa: BLE001
        raise CatalogMediaStorageError("image_object_not_found") from exc
    return dict(resp.get("Metadata") or {})


def attach_catalog_product_image(
    *,
    tenant_id: int,
    image_url: str,
    product_id: int,
) -> None:
    """Mark an uploaded catalog image as attached to *product_id*.

    Must run after the product row exists (we need the id) and before
    treating the upload lifecycle as complete. A future cleanup job may
    delete only objects whose metadata ``status`` is ``pending`` — never
    ``attached``.
    """
    key = object_key_from_public_url(image_url)
    if not key:
        return
    expected_prefix = f"{OBJECT_PREFIX}/{int(tenant_id)}/"
    if not key.startswith(expected_prefix):
        raise CatalogMediaValidationError("image_url_not_owned_by_tenant")

    meta = _head_object_metadata(key)
    status = (meta.get("status") or "").strip().lower()
    bound_product = (meta.get("product-id") or "").strip()
    pid = str(int(product_id))
    if status == "attached" and bound_product and bound_product != pid:
        raise CatalogMediaValidationError("image_already_attached")

    client = _s3_client()
    try:
        client.copy_object(
            Bucket=catalog_media_bucket(),
            Key=key,
            CopySource={
                "Bucket": catalog_media_bucket(),
                "Key": key,
            },
            MetadataDirective="REPLACE",
            Metadata={
                "tenant-id": str(int(tenant_id)),
                "status": "attached",
                "purpose": "catalog-manual-product",
                "product-id": pid,
            },
            ContentType="image/webp",
            CacheControl="public, max-age=31536000, immutable",
        )
    except CatalogMediaValidationError:
        raise
    except Exception as exc:  # noqa: BLE001
        _logger.exception(
            "[catalog_media.attach] tenant=%s product=%s key=%s failed",
            tenant_id,
            product_id,
            key,
        )
        raise CatalogMediaStorageError("image_attach_failed") from exc

    _logger.info(
        "[catalog_media.attach] tenant=%s product=%s key=%s",
        tenant_id,
        product_id,
        key,
    )


def _s3_client():
    if not is_catalog_media_storage_configured():
        raise CatalogMediaStorageError("catalog_media_storage_not_configured")
    import boto3  # lazy — only needed when uploading

    return boto3.client(
        "s3",
        endpoint_url=catalog_media_r2_endpoint(),
        aws_access_key_id=catalog_media_r2_access_key_id(),
        aws_secret_access_key=catalog_media_r2_secret_access_key(),
        region_name="auto",
    )


REMOTE_FETCH_TIMEOUT_SECONDS = 30.0
SOURCE_META_IMAGE_KEY = "meta_source_image_url"


def download_remote_catalog_image(url: str) -> bytes:
    """Fetch a remote product image for durable R2 ingest. No Graph writes."""
    import httpx  # lazy — only needed on ingest

    target = (url or "").strip()
    if not (target.startswith("https://") or target.startswith("http://")):
        raise CatalogMediaValidationError("remote_image_url_invalid")

    with httpx.Client(
        timeout=REMOTE_FETCH_TIMEOUT_SECONDS,
        follow_redirects=True,
        headers={"User-Agent": "NahlaCatalogImageIngest/1.0"},
    ) as http:
        resp = http.get(target)
        if resp.status_code >= 400:
            raise CatalogMediaStorageError("remote_image_http_error")
        content = resp.content or b""
    if len(content) > MAX_UPLOAD_BYTES:
        raise CatalogMediaValidationError("file_too_large")
    if sniff_image_mime(content) not in ALLOWED_INPUT_MIMES:
        raise CatalogMediaValidationError("unsupported_image_type")
    return content


def ingest_remote_catalog_image(
    *,
    tenant_id: int,
    source_url: str,
    product_id: int,
) -> dict:
    """Download a remote image and store it on the catalog R2 prefix.

    Idempotent when *source_url* is already this tenant's managed URL.
    Does not write Meta Graph. Does not create product rows.
    """
    from core.catalog_image import coerce_image_url  # noqa: PLC0415

    src = coerce_image_url(source_url)
    if not src:
        raise CatalogMediaValidationError("remote_image_url_invalid")
    if is_managed_catalog_image_url(src) and image_url_owned_by_tenant(tenant_id, src):
        return {
            "image_url": src,
            "skipped": True,
            "reason": "already_durable",
        }

    content = download_remote_catalog_image(src)
    uploaded = upload_catalog_product_image(tenant_id=int(tenant_id), content=content)
    durable = str(uploaded.get("image_url") or "").strip()
    if not durable:
        raise CatalogMediaStorageError("upload_failed")
    attach_catalog_product_image(
        tenant_id=int(tenant_id),
        image_url=durable,
        product_id=int(product_id),
    )
    return {
        "image_url": durable,
        "skipped": False,
        "source_url": src,
        "media_id": uploaded.get("media_id"),
        "size_bytes": uploaded.get("size_bytes"),
    }


def upload_catalog_product_image(
    *,
    tenant_id: int,
    content: bytes,
) -> dict:
    """Validate, encode WEBP, upload to R2, return public URL metadata."""
    webp_bytes, content_type = prepare_catalog_product_webp(content)
    media_id = uuid.uuid4().hex
    key = object_key_for_tenant(tenant_id, media_id)
    client = _s3_client()
    try:
        client.put_object(
            Bucket=catalog_media_bucket(),
            Key=key,
            Body=webp_bytes,
            ContentType=content_type,
            CacheControl="public, max-age=31536000, immutable",
            Metadata={
                "tenant-id": str(int(tenant_id)),
                "status": "pending",
                "purpose": "catalog-manual-product",
            },
        )
    except Exception as exc:  # noqa: BLE001
        _logger.exception(
            "[catalog_media.upload] tenant=%s key=%s failed",
            tenant_id,
            key,
        )
        raise CatalogMediaStorageError("upload_failed") from exc

    image_url = public_url_for_object_key(key)
    _logger.info(
        "[catalog_media.upload] tenant=%s media_id=%s bytes=%d",
        tenant_id,
        media_id,
        len(webp_bytes),
    )
    return {
        "image_url": image_url,
        "media_id": media_id,
        "content_type": content_type,
        "size_bytes": len(webp_bytes),
    }
