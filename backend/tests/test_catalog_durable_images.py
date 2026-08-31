"""Durable image backfill skips historical Meta-removed rows."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

from services.catalog_durable_images import (  # noqa: E402
    _is_active_meta_readonly,
    backfill_active_meta_readonly_images,
    persist_product_display_image,
)
from services.catalog_media_storage import CatalogMediaStorageError  # noqa: E402


def _product(**kwargs):
    base = dict(
        id=140,
        tenant_id=33,
        title="زيت سم النحل",
        catalog_status="active",
        merchant_hidden_at=None,
        source="meta",
        ownership_mode="meta_readonly",
        extra_metadata={"image_url": "https://fbcdn.example/p.png"},
        variants=[],
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_active_meta_readonly_helper_excludes_removed():
    assert _is_active_meta_readonly(_product()) is True
    assert _is_active_meta_readonly(_product(catalog_status="removed_from_meta")) is False


def test_persist_skips_already_durable(monkeypatch):
    monkeypatch.setenv("NAHLA_CATALOG_MEDIA_BUCKET", "nahlah-media")
    monkeypatch.setenv("NAHLA_CATALOG_MEDIA_PUBLIC_BASE_URL", "https://pub-example.r2.dev")
    monkeypatch.setenv("NAHLA_CATALOG_MEDIA_R2_ENDPOINT", "https://account.r2.cloudflarestorage.com")
    monkeypatch.setenv("NAHLA_CATALOG_MEDIA_R2_ACCESS_KEY_ID", "k")
    monkeypatch.setenv("NAHLA_CATALOG_MEDIA_R2_SECRET_ACCESS_KEY", "s")
    row = _product(
        extra_metadata={
            "image_url": "https://pub-example.r2.dev/catalog-products/33/abc.webp",
        },
    )
    db = MagicMock()
    result = persist_product_display_image(db, row)
    assert result["ok"] is True
    assert result["skipped"] is True
    assert result["reason"] == "already_durable"


def _media_env(monkeypatch) -> None:
    monkeypatch.setenv("NAHLA_CATALOG_MEDIA_BUCKET", "nahlah-media")
    monkeypatch.setenv("NAHLA_CATALOG_MEDIA_PUBLIC_BASE_URL", "https://pub-example.r2.dev")
    monkeypatch.setenv("NAHLA_CATALOG_MEDIA_R2_ENDPOINT", "https://account.r2.cloudflarestorage.com")
    monkeypatch.setenv("NAHLA_CATALOG_MEDIA_R2_ACCESS_KEY_ID", "k")
    monkeypatch.setenv("NAHLA_CATALOG_MEDIA_R2_SECRET_ACCESS_KEY", "s")


def test_persist_retries_with_live_graph_url(monkeypatch):
    _media_env(monkeypatch)
    row = _product(meta_item_id="META-140")
    db = MagicMock()
    with patch(
        "services.catalog_durable_images.ingest_remote_catalog_image",
        side_effect=[
            CatalogMediaStorageError("remote_image_http_error"),
            {
                "image_url": "https://pub-example.r2.dev/catalog-products/33/dead.webp",
                "skipped": False,
                "reason": "ingested",
            },
        ],
    ) as ingest:
        with patch(
            "services.catalog_durable_images.fetch_live_graph_image_url",
            return_value="https://scontent.example/fresh.png",
        ) as graph_get:
            result = persist_product_display_image(db, row)
    assert result["ok"] is True
    assert result["skipped"] is False
    assert result["source_kind"] == "graph_live"
    assert row.extra_metadata["image_url"].endswith("dead.webp")
    assert ingest.call_count == 2
    graph_get.assert_called_once()
    db.add.assert_called_once()


def test_backfill_ignores_removed_from_meta_rows():
    removed = _product(id=97, catalog_status="removed_from_meta")
    active = _product(id=140)
    db = MagicMock()
    q = MagicMock()
    db.query.return_value.filter.return_value.order_by.return_value = q
    q.all.return_value = [removed, active]
    with patch(
        "services.catalog_durable_images.persist_product_display_image",
        return_value={"ok": True, "skipped": True, "reason": "already_durable", "product_id": 140},
    ) as persist:
        report = backfill_active_meta_readonly_images(db, 33, limit=28)
    assert report["ignored_removed"] == 1
    assert report["scanned"] == 1
    persist.assert_called_once()
    assert persist.call_args.args[1].id == 140
