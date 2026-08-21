"""Generic structural completeness gate for pypdf PDF text extraction.

Non-empty extracted text does not imply semantically complete extraction.
This module inspects density, raster overlays, and form-like label/value
pairing — without payment keywords, filenames, or tenant context.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

_DANGLING_LABEL_RE = re.compile(r"^.{1,48}:\s*$")
_SPARSE_CHARS_PER_PAGE = 700
_SPARSE_BYTES_PER_PAGE = 40_000
_LARGE_IMAGE_PIXELS = 80_000


@dataclass(frozen=True)
class PdfExtractionCompleteness:
    complete: bool
    ocr_required: bool
    reason: str
    signals: Dict[str, Any] = field(default_factory=dict)


def _whitespace_normalize(text: str) -> str:
    return " ".join((text or "").split())


def _count_dangling_label_lines(text: str) -> int:
    count = 0
    for line in (text or "").splitlines():
        if _DANGLING_LABEL_RE.match(line.strip()):
            count += 1
    return count


def _is_value_like_token(token: str) -> bool:
    if not token:
        return False
    digit_count = sum(ch.isdigit() for ch in token)
    if digit_count >= 6:
        return True
    return len(token) >= 10 and digit_count > 0


def _count_value_like_tokens(text: str) -> int:
    return sum(1 for token in re.findall(r"\S+", text or "") if _is_value_like_token(token))


def collect_pdf_image_stats(reader: Any) -> Dict[str, int]:
    """Inspect page XObject dictionaries only; never execute embedded content."""
    large_image_count = 0
    max_image_pixels = 0
    if reader is None:
        return {
            "large_image_count": 0,
            "max_image_pixels": 0,
        }

    try:
        pages = getattr(reader, "pages", None) or []
    except Exception:  # noqa: BLE001
        return {
            "large_image_count": 0,
            "max_image_pixels": 0,
        }

    for page in pages:
        try:
            resources = page.get("/Resources") or {}
            xobjects = resources.get("/XObject") or {}
            if hasattr(xobjects, "items"):
                items = xobjects.items()
            elif isinstance(xobjects, dict):
                items = xobjects.items()
            else:
                continue
        except Exception:  # noqa: BLE001
            continue

        for _name, xobj_ref in items:
            try:
                xobj = xobj_ref.get_object() if hasattr(xobj_ref, "get_object") else xobj_ref
                subtype = str(xobj.get("/Subtype", "") or "")
                if subtype != "/Image":
                    continue
                width = int(xobj.get("/Width", 0) or 0)
                height = int(xobj.get("/Height", 0) or 0)
                pixels = max(0, width * height)
                max_image_pixels = max(max_image_pixels, pixels)
                if pixels >= _LARGE_IMAGE_PIXELS:
                    large_image_count += 1
            except Exception:  # noqa: BLE001
                continue

    return {
        "large_image_count": large_image_count,
        "max_image_pixels": max_image_pixels,
    }


def assess_pdf_extraction_completeness(
    text: str,
    file_bytes: bytes,
    page_count: int,
    *,
    reader: Any = None,
    image_stats: Optional[Dict[str, int]] = None,
) -> PdfExtractionCompleteness:
    """Return structural completeness assessment for extracted PDF text."""
    normalized_text = (text or "").strip()
    byte_size = len(file_bytes or b"")
    safe_page_count = max(int(page_count or 0), 0)
    chars = len(normalized_text)

    if image_stats is None:
        image_stats = collect_pdf_image_stats(reader)

    large_image_count = int(image_stats.get("large_image_count") or 0)
    max_image_pixels = int(image_stats.get("max_image_pixels") or 0)

    chars_per_page = (chars / safe_page_count) if safe_page_count > 0 else float(chars)
    bytes_per_page = (byte_size / safe_page_count) if safe_page_count > 0 else float(byte_size)
    text_byte_ratio = (chars / byte_size) if byte_size > 0 else 0.0
    dangling_label_lines = _count_dangling_label_lines(normalized_text)
    value_like_token_count = _count_value_like_tokens(normalized_text)

    signals: Dict[str, Any] = {
        "chars": chars,
        "page_count": safe_page_count,
        "byte_size": byte_size,
        "chars_per_page": chars_per_page,
        "bytes_per_page": bytes_per_page,
        "text_byte_ratio": text_byte_ratio,
        "large_image_count": large_image_count,
        "max_image_pixels": max_image_pixels,
        "dangling_label_lines": dangling_label_lines,
        "value_like_token_count": value_like_token_count,
    }

    if safe_page_count <= 0 and not normalized_text:
        return PdfExtractionCompleteness(
            complete=False,
            ocr_required=False,
            reason="not_applicable",
            signals=signals,
        )

    if not normalized_text and safe_page_count > 0:
        return PdfExtractionCompleteness(
            complete=False,
            ocr_required=True,
            reason="empty",
            signals=signals,
        )

    sparse_overlay = (
        chars_per_page < _SPARSE_CHARS_PER_PAGE
        and (
            bytes_per_page >= _SPARSE_BYTES_PER_PAGE
            or max_image_pixels >= _LARGE_IMAGE_PIXELS
        )
    )
    if sparse_overlay:
        return PdfExtractionCompleteness(
            complete=False,
            ocr_required=True,
            reason="sparse_overlay",
            signals=signals,
        )

    incomplete_value_coverage = (
        dangling_label_lines >= 2 and value_like_token_count == 0
    ) or (
        dangling_label_lines >= 3
        and value_like_token_count < (dangling_label_lines // 2)
    )
    if incomplete_value_coverage:
        return PdfExtractionCompleteness(
            complete=False,
            ocr_required=True,
            reason="incomplete_value_coverage",
            signals=signals,
        )

    return PdfExtractionCompleteness(
        complete=True,
        ocr_required=False,
        reason="complete",
        signals=signals,
    )


def merge_primary_and_ocr_text(primary: str, ocr: str) -> str:
    """Merge pypdf primary text with supplemental OCR text."""
    primary_text = primary or ""
    ocr_text = ocr or ""
    if not primary_text.strip():
        return ocr_text
    if not ocr_text.strip():
        return primary_text
    if _whitespace_normalize(primary_text) == _whitespace_normalize(ocr_text):
        return primary_text
    return f"{primary_text}\n{ocr_text}"
