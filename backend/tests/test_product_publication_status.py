"""Truthfulness tests for product publication status."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)

from services.product_publication_status import build_product_publication_status  # noqa: E402


def _product(*, sync_status="synced", in_stock=True):
    return SimpleNamespace(
        sync_status=sync_status,
        in_stock=in_stock,
        catalog_status="active",
        extra_metadata={"sync_meta": {"waba_catalog_linked": True}},
    )


def test_synced_and_linked_does_not_imply_visible():
    pub = build_product_publication_status(
        _product(),
        waba_link_status={"ok": True, "expected_catalog_linked": True},
    )
    assert pub["meta_catalog_synced"] is True
    assert pub["waba_catalog_linked"] is True
    assert pub["visible_in_whatsapp"] is False


def test_synced_with_linked_false_visible_false():
    pub = build_product_publication_status(
        _product(),
        waba_link_status={"ok": True, "expected_catalog_linked": False},
    )
    assert pub["meta_catalog_synced"] is True
    assert pub["waba_catalog_linked"] is False
    assert pub["visible_in_whatsapp"] is False


def test_synced_with_linked_null_visible_false():
    pub = build_product_publication_status(
        SimpleNamespace(
            sync_status="synced",
            in_stock=True,
            catalog_status="active",
            extra_metadata={"currency": "SAR"},
        ),
        waba_link_status={"ok": False, "expected_catalog_linked": None},
    )
    assert pub["meta_catalog_synced"] is True
    assert pub["waba_catalog_linked"] is None
    assert pub["visible_in_whatsapp"] is False


def test_commerce_settings_absent_does_not_imply_visible():
    pub = build_product_publication_status(
        _product(),
        waba_link_status={"ok": True, "expected_catalog_linked": True},
    )
    assert pub["visible_in_whatsapp"] is False


def test_pending_sync_meta_state_preserves_meta_synced_false():
    pub = build_product_publication_status(
        _product(sync_status="pending"),
        waba_link_status={"ok": True, "expected_catalog_linked": True},
    )
    assert pub["meta_catalog_synced"] is False
    assert pub["visible_in_whatsapp"] is False


def test_failed_sync_meta_state():
    pub = build_product_publication_status(
        _product(sync_status="failed"),
        waba_link_status={"ok": True, "expected_catalog_linked": True},
    )
    assert pub["meta_catalog_synced"] is False
    assert pub["visible_in_whatsapp"] is False
