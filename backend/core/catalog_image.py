"""Resolve product image URLs for catalog display (API + UI)."""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional, Sequence


def coerce_image_url(value: Any) -> str:
    """Normalize Salla/Meta image fields to a bare http(s) URL string."""
    if value is None:
        return ""
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return ""
        if s.startswith("http://") or s.startswith("https://"):
            return s
        # Dict/list serialized as string → broken <img> in browsers.
        if s.startswith("{") or s.startswith("["):
            return ""
        return ""
    if isinstance(value, Mapping):
        for key in ("url", "original", "thumbnail", "src", "full_size", "medium"):
            nested = value.get(key)
            if nested:
                resolved = coerce_image_url(nested)
                if resolved:
                    return resolved
        return ""
    if isinstance(value, (list, tuple)) and value:
        return coerce_image_url(value[0])
    return ""


def _iter_meta_image_candidates(meta: Mapping[str, Any]) -> Iterable[Any]:
    yield meta.get("image_url")
    yield meta.get("thumbnail")
    yield meta.get("image")
    additional = meta.get("additional_images") or []
    if isinstance(additional, (list, tuple)):
        yield from additional
    for opt in meta.get("options") or []:
        if not isinstance(opt, Mapping):
            continue
        for val in opt.get("values") or []:
            if isinstance(val, Mapping):
                yield val.get("image_url")
                yield val.get("image")


def resolve_product_image_url(
    *,
    meta: Optional[Mapping[str, Any]] = None,
    variants: Optional[Sequence[Any]] = None,
) -> str:
    """Best-effort parent display image for catalog grid / detail."""
    meta = meta or {}
    for candidate in _iter_meta_image_candidates(meta):
        url = coerce_image_url(candidate)
        if url:
            return url
    for variant in variants or []:
        if isinstance(variant, Mapping):
            raw = variant.get("image_url")
        else:
            raw = getattr(variant, "image_url", None)
        url = coerce_image_url(raw)
        if url:
            return url
    return ""


def extract_sync_product_image(raw: Any) -> str:
    """Canonical parent image URL for store sync → ``extra_metadata.image_url``."""
    if hasattr(raw, "dict"):
        try:
            raw = raw.dict()
        except Exception:  # noqa: BLE001
            return ""
    if not isinstance(raw, Mapping):
        return ""
    for key in ("image_url", "main_image", "image", "thumbnail"):
        url = coerce_image_url(raw.get(key))
        if url:
            return url
    return coerce_image_url(raw.get("images"))


def extract_sync_additional_images(raw: Any, *, primary: str = "") -> list[str]:
    """Collect extra gallery URLs from Salla ``images`` arrays."""
    if hasattr(raw, "dict"):
        try:
            raw = raw.dict()
        except Exception:  # noqa: BLE001
            return []
    if not isinstance(raw, Mapping):
        return []
    images = raw.get("images")
    if not isinstance(images, (list, tuple)):
        return []
    out: list[str] = []
    seen = {primary} if primary else set()
    for item in images:
        url = coerce_image_url(item)
        if url and url not in seen:
            out.append(url)
            seen.add(url)
    return out

