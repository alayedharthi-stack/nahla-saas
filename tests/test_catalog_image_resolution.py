"""Catalog image resolution for dashboard display."""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for _p in (REPO_ROOT, BACKEND_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from core.catalog import SOURCE_META, SOURCE_SALLA, product_source  # noqa: E402
from core.catalog_image import coerce_image_url, resolve_product_image_url  # noqa: E402


class TestCoerceImageUrl:
    def test_https_string_passthrough(self):
        assert coerce_image_url("https://cdn.example.com/a.jpg") == "https://cdn.example.com/a.jpg"

    def test_salla_object_with_url(self):
        assert coerce_image_url({"url": "https://cdn.example.com/b.jpg"}) == "https://cdn.example.com/b.jpg"

    def test_rejects_dict_serialized_as_string(self):
        assert coerce_image_url("{'url': 'https://x'}") == ""

    def test_first_list_entry(self):
        assert coerce_image_url(["https://cdn.example.com/c.jpg"]) == "https://cdn.example.com/c.jpg"


class TestResolveProductImageUrl:
    def test_meta_image_url(self):
        url = resolve_product_image_url(meta={"image_url": "https://cdn.example.com/p.jpg"})
        assert url == "https://cdn.example.com/p.jpg"

    def test_variant_fallback_when_parent_empty(self):
        class V:
            image_url = "https://cdn.example.com/v.jpg"

        url = resolve_product_image_url(meta={}, variants=[V()])
        assert url == "https://cdn.example.com/v.jpg"

    def test_additional_images_fallback(self):
        url = resolve_product_image_url(
            meta={"additional_images": ["https://cdn.example.com/extra.jpg"]},
        )
        assert url == "https://cdn.example.com/extra.jpg"

    def test_option_value_image_fallback(self):
        url = resolve_product_image_url(meta={
            "options": [{"values": [{"image_url": "https://cdn.example.com/opt.jpg"}]}],
        })
        assert url == "https://cdn.example.com/opt.jpg"

    def test_thumbnail_object_coerced(self):
        url = resolve_product_image_url(meta={
            "thumbnail": {"url": "https://cdn.example.com/thumb.jpg"},
        })
        assert url == "https://cdn.example.com/thumb.jpg"


class TestMetaExportCandidateSkip:
    """Future Meta export must skip products imported from Meta."""

    def test_meta_imported_source_is_meta(self):
        class P:
            source = SOURCE_META
            extra_metadata = {"source": SOURCE_META}

        assert product_source(P()) == SOURCE_META

    def test_salla_source_not_meta(self):
        class P:
            source = SOURCE_SALLA
            extra_metadata = {}

        assert product_source(P()) != SOURCE_META
