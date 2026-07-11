"""Tests for Meta import ownership decisions (catalog_write_router)."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

from core.catalog import (  # noqa: E402
    OWNERSHIP_EXTERNAL_MANAGED,
    OWNERSHIP_META_READONLY,
    OWNERSHIP_NAHLA_MANAGED,
    SOURCE_META_EXISTING,
    SOURCE_NAHLA_NATIVE,
)
from core.catalog_write_router import (  # noqa: E402
    ACTION_CREATE,
    ACTION_FLAG_CONFLICT,
    ACTION_REFRESH_META,
    ACTION_SKIP_PROTECTED,
    resolve_meta_import_action,
)


def _incoming(*, meta_id: str = "META-100", retailer_id: str = "sku-100"):
    return {"meta_id": meta_id, "retailer_id": retailer_id}


def test_no_existing_product_returns_create():
    decision = resolve_meta_import_action(None, _incoming())
    assert decision.action == ACTION_CREATE
    assert decision.reason == "new_row"


def test_salla_product_skip_protected():
    existing = SimpleNamespace(
        id=10,
        source="salla",
        ownership_mode=OWNERSHIP_EXTERNAL_MANAGED,
        external_id="salla-9001",
        meta_retailer_id=None,
    )
    decision = resolve_meta_import_action(existing, _incoming(meta_id="META-NEW"))
    assert decision.action == ACTION_SKIP_PROTECTED
    assert decision.reason == "external_platform"


def test_nahla_native_flag_conflict():
    existing = SimpleNamespace(
        id=11,
        source=SOURCE_NAHLA_NATIVE,
        ownership_mode=OWNERSHIP_NAHLA_MANAGED,
        external_id=None,
        meta_retailer_id="nahla_p_11",
    )
    decision = resolve_meta_import_action(existing, _incoming())
    assert decision.action == ACTION_FLAG_CONFLICT
    assert decision.reason == "nahla_native_match"


def test_manual_native_flag_conflict():
    existing = SimpleNamespace(
        id=12,
        source="manual",
        ownership_mode=OWNERSHIP_NAHLA_MANAGED,
        external_id=None,
        meta_retailer_id=None,
    )
    decision = resolve_meta_import_action(existing, _incoming())
    assert decision.action == ACTION_FLAG_CONFLICT


def test_meta_existing_refresh_meta():
    existing = SimpleNamespace(
        id=13,
        source=SOURCE_META_EXISTING,
        ownership_mode=OWNERSHIP_META_READONLY,
        external_id="META-OLD",
        meta_retailer_id="sku-old",
    )
    decision = resolve_meta_import_action(
        existing,
        _incoming(meta_id="META-OLD", retailer_id="sku-old"),
    )
    assert decision.action == ACTION_REFRESH_META
    assert decision.reason == "meta_existing"


def test_legacy_meta_source_refresh_meta():
    existing = SimpleNamespace(
        id=14,
        source="meta",
        ownership_mode=OWNERSHIP_META_READONLY,
        external_id="META-LEGACY",
        meta_retailer_id="sku-legacy",
    )
    decision = resolve_meta_import_action(
        existing,
        _incoming(meta_id="META-LEGACY", retailer_id="sku-legacy"),
    )
    assert decision.action == ACTION_REFRESH_META
