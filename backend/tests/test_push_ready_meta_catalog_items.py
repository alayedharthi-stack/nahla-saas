"""Tests for guarded batch Meta catalog push of ready create items."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

from services.meta_catalog_push import push_ready_meta_catalog_batch  # noqa: E402
from services.meta_catalog_readiness import (  # noqa: E402
    MetaCatalogReadinessItem,
    MetaCatalogReadinessReport,
    is_ready_create_in_stock_candidate,
    select_ready_create_push_candidates,
)


def _item(
    *,
    product_id: int = 26,
    variant_id: int = 1,
    retailer_id: str = "506596370-2037905094",
    title: str = "بلوزة",
    status: str = "ready",
    availability: str = "in stock",
    action_needed: str = "create",
    generated_name: str = "بلوزة - 36 - XS",
    price: int = 8900,
) -> MetaCatalogReadinessItem:
    return MetaCatalogReadinessItem(
        product_id=product_id,
        title=title,
        variant_id=variant_id,
        salla_variant_id="2037905094",
        retailer_id=retailer_id,
        item_group_id="506596370",
        option_summary="36 - XS",
        generated_name=generated_name,
        price=price,
        currency="SAR",
        availability=availability,
        image_url_present=True,
        url_present=True,
        status=status,
        reasons=[],
        payload_preview={"retailer_id": retailer_id, "name": generated_name},
        in_meta_live=False,
        action_needed=action_needed,
        local_name=generated_name,
    )


def _report(*items: MetaCatalogReadinessItem) -> MetaCatalogReadinessReport:
    return MetaCatalogReadinessReport(
        tenant_id=9,
        dry_run=True,
        items=list(items),
        meta_fetch={"items": 0, "http_status": 200, "error": None},
    )


def test_batch_dry_run_lists_only_ready_create_in_stock():
    items = [
        _item(),
        _item(variant_id=2, retailer_id="506596370-1996903361", generated_name="بلوزة - 40 - M"),
    ]
    with patch(
        "services.meta_catalog_readiness.build_meta_catalog_readiness_report",
        return_value=_report(*items),
    ):
        batch = push_ready_meta_catalog_batch(MagicMock(), 9, confirm=False)

    assert batch["dry_run"] is True
    assert batch["summary"]["candidate_count"] == 2
    assert batch["candidates"][0]["would_push"] is True
    assert batch["results"] == []


def test_batch_dry_run_excludes_warn_oos_blocked_skipped_noop():
    items = [
        _item(),
        _item(variant_id=2, status="warn", action_needed="create", availability="out of stock"),
        _item(variant_id=3, status="blocked", action_needed="skip"),
        _item(variant_id=4, status="skipped", action_needed="skip"),
        _item(variant_id=5, status="ready", action_needed="noop", availability="in stock"),
        _item(variant_id=6, status="ready", action_needed="update", availability="in stock"),
    ]
    selected = select_ready_create_push_candidates(items)
    assert len(selected) == 1
    assert selected[0].variant_id == 1

    with patch(
        "services.meta_catalog_readiness.build_meta_catalog_readiness_report",
        return_value=_report(*items),
    ):
        batch = push_ready_meta_catalog_batch(MagicMock(), 9, confirm=False)
    assert batch["summary"]["candidate_count"] == 1


def test_batch_confirm_pushes_ready_create_only():
    items = [
        _item(retailer_id="88001-1001"),
        _item(variant_id=2, retailer_id="88001-1002", generated_name="متجر تجريبي عام - 500g", title="متجر تجريبي عام"),
    ]
    with patch(
        "services.meta_catalog_readiness.build_meta_catalog_readiness_report",
        return_value=_report(*items),
    ), patch(
        "services.meta_catalog_push.push_one_meta_catalog_item",
        side_effect=[
            {"ok": True, "action": "create", "retailer_id": "88001-1001", "meta_product_id": "1", "meta": {"http_status": 200}},
            {"ok": True, "action": "create", "retailer_id": "88001-1002", "meta_product_id": "2", "meta": {"http_status": 200}},
        ],
    ) as push_mock:
        batch = push_ready_meta_catalog_batch(MagicMock(), 9, confirm=True)

    assert push_mock.call_count == 2
    assert batch["summary"]["succeeded"] == 2
    assert batch["summary"]["failed"] == 0


def test_batch_stop_on_first_error():
    items = [_item(retailer_id="A-1"), _item(variant_id=2, retailer_id="A-2")]
    with patch(
        "services.meta_catalog_readiness.build_meta_catalog_readiness_report",
        return_value=_report(*items),
    ), patch(
        "services.meta_catalog_push.push_one_meta_catalog_item",
        side_effect=[
            {"ok": False, "action": "create", "error": "meta_http_error", "meta": {"http_status": 400}},
            {"ok": True, "action": "create", "meta": {"http_status": 200}},
        ],
    ) as push_mock:
        batch = push_ready_meta_catalog_batch(MagicMock(), 9, confirm=True, stop_on_first_error=True)

    assert push_mock.call_count == 1
    assert batch["summary"]["failed"] == 1
    assert batch["summary"]["stopped_on_error"] is True


def test_batch_continue_on_error():
    items = [_item(retailer_id="A-1"), _item(variant_id=2, retailer_id="A-2")]
    with patch(
        "services.meta_catalog_readiness.build_meta_catalog_readiness_report",
        return_value=_report(*items),
    ), patch(
        "services.meta_catalog_push.push_one_meta_catalog_item",
        side_effect=[
            {"ok": False, "action": "create", "error": "meta_http_error", "meta": {"http_status": 400}},
            {"ok": True, "action": "create", "meta_product_id": "9", "meta": {"http_status": 200}},
        ],
    ) as push_mock:
        batch = push_ready_meta_catalog_batch(
            MagicMock(), 9, confirm=True, stop_on_first_error=False,
        )

    assert push_mock.call_count == 2
    assert batch["summary"]["failed"] == 1
    assert batch["summary"]["succeeded"] == 1
    assert batch["summary"]["stopped_on_error"] is False


def test_batch_product_id_filter():
    items = [
        _item(product_id=26, variant_id=1),
        _item(product_id=27, variant_id=2, retailer_id="1280699665-65055227", title="فستان"),
    ]
    selected = select_ready_create_push_candidates(items, product_id=26)
    assert len(selected) == 1
    assert selected[0].product_id == 26


def test_batch_limit():
    items = [
        _item(variant_id=1, retailer_id="R-1"),
        _item(variant_id=2, retailer_id="R-2"),
        _item(variant_id=3, retailer_id="R-3"),
    ]
    selected = select_ready_create_push_candidates(items, limit=2)
    assert len(selected) == 2
    assert [i.retailer_id for i in selected] == ["R-1", "R-2"]


def test_batch_no_graph_post_in_dry_run():
    with patch(
        "services.meta_catalog_readiness.build_meta_catalog_readiness_report",
        return_value=_report(_item()),
    ), patch("services.meta_catalog_push.push_one_meta_catalog_item") as push_mock:
        push_ready_meta_catalog_batch(MagicMock(), 9, confirm=False)
    push_mock.assert_not_called()


def test_batch_rejects_when_meta_live_fetch_fails():
    report = MetaCatalogReadinessReport(tenant_id=9, error="no_graph_token")
    with patch(
        "services.meta_catalog_readiness.build_meta_catalog_readiness_report",
        return_value=report,
    ), patch("services.meta_catalog_push.push_one_meta_catalog_item") as push_mock:
        batch = push_ready_meta_catalog_batch(MagicMock(), 9, confirm=True)
    assert batch["error"] == "no_graph_token"
    assert batch["summary"]["candidate_count"] == 0
    push_mock.assert_not_called()


def test_generic_commerce_neutral_items():
    item = _item(
        product_id=70,
        title="متجر تجريبي عام",
        retailer_id="55001-1001",
        generated_name="متجر تجريبي عام - 500g",
        price=9900,
    )
    assert is_ready_create_in_stock_candidate(item) is True
    row = select_ready_create_push_candidates([item])[0]
    assert row.generated_name == "متجر تجريبي عام - 500g"


def test_include_updates_flag():
    item = _item(action_needed="update")
    assert is_ready_create_in_stock_candidate(item) is False
    assert is_ready_create_in_stock_candidate(item, include_updates=True) is True


def test_batch_salla_missing_svid_does_not_create():
    items = [_item(retailer_id="nahla_v_501")]
    parent = SimpleNamespace(id=26, tenant_id=9, source="salla", external_id="88001")
    variant = SimpleNamespace(id=1, salla_variant_id="", retailer_id="nahla_v_501")
    with patch(
        "services.meta_catalog_readiness.build_meta_catalog_readiness_report",
        return_value=_report(*items),
    ), patch(
        "services.meta_catalog_push.load_variant_for_push",
        return_value=(parent, variant),
    ), patch("services.meta_catalog_push.push_one_meta_catalog_item") as push_mock:
        batch = push_ready_meta_catalog_batch(MagicMock(), 9, confirm=True)

    push_mock.assert_not_called()
    assert batch["summary"]["failed"] == 1
    assert batch["results"][0]["error"] == "ambiguous_variant_identity"
