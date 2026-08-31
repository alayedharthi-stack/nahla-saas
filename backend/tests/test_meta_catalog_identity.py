"""Unit tests for canonical sibling identity (no database)."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

from services.meta_catalog_identity import (  # noqa: E402
    ACTION_BLOCK,
    ACTION_CREATE,
    ACTION_LINK,
    CANONICAL_SIBLING_RULE,
    ERROR_AMBIGUOUS_SIBLING,
    IDENTITY_CANONICAL_SIBLING,
    REASON_CONTENT,
    REASON_FOREIGN_META,
    REASON_LINEAGE,
    REASON_MULTIPLE,
    canonical_sibling_retailer_ids,
    evaluate_canonical_sibling_bind,
    existing_identity_retailer_id,
    live_canonical_sibling_hits,
    sibling_content_mismatches,
)


def _parent(**overrides):
    base = dict(
        id=23,
        tenant_id=1,
        title="تنورة طويلة",
        external_id="398551325",
        meta_item_id=None,
        catalog_status="active",
        source="salla",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _variant(svid, rid=None):
    return SimpleNamespace(
        salla_variant_id=svid,
        retailer_id=rid or f"398551325-{svid}",
    )


def _payload(**overrides):
    body = {
        "price": 12000,
        "currency": "SAR",
        "availability": "in stock",
        "url": "https://store.example/p/skirt",
        "image_url": "https://cdn.example/skirt.jpg",
        "name": "تنورة طويلة",
    }
    body.update(overrides)
    return body


def _live(**overrides):
    item = {
        "id": "META-SIB",
        "retailer_id": "398551325-591001",
        "price": "120.00",
        "currency": "SAR",
        "availability": "in stock",
        "url": "https://store.example/p/skirt",
        "image_url": "https://scontent.xx.fbcdn.net/v.jpg",
        "name": "اسم ميتا المختلف",
    }
    item.update(overrides)
    return item


def test_canonical_keys_are_hyphenated_lineage_only():
    parent = _parent()
    variants = [_variant("591001"), _variant("", rid="398551325")]
    parent.variants = variants
    keys = canonical_sibling_retailer_ids(parent, exclude_rid="398551325", variants=variants)
    assert keys == ["398551325-591001"]
    assert parent.title not in keys
    assert "398551325" not in keys
    assert "591001" not in keys


def test_name_is_never_content_evidence():
    mismatches = sibling_content_mismatches(
        _payload(name="محلي"),
        _live(name="مختلف تماما"),
    )
    assert mismatches == []


def test_unique_safe_sibling_links():
    parent = _parent()
    variants = [_variant("591001"), _variant("", rid="398551325")]
    decision = evaluate_canonical_sibling_bind(
        parent,
        current_rid="398551325",
        variants=variants,
        live_by_rid={"398551325-591001": _live()},
        sibling_payloads={"398551325-591001": _payload()},
    )
    assert decision.action == ACTION_LINK
    assert decision.identity_class == IDENTITY_CANONICAL_SIBLING
    assert decision.canonical_rule == CANONICAL_SIBLING_RULE
    assert decision.allow_create is False


def test_multiple_siblings_block():
    parent = _parent()
    variants = [_variant("591001"), _variant("591002")]
    decision = evaluate_canonical_sibling_bind(
        parent,
        current_rid="398551325",
        variants=variants,
        live_by_rid={
            "398551325-591001": _live(),
            "398551325-591002": _live(id="META-B", retailer_id="398551325-591002"),
        },
        sibling_payloads={
            "398551325-591001": _payload(),
            "398551325-591002": _payload(),
        },
    )
    assert decision.action == ACTION_BLOCK
    assert decision.error == ERROR_AMBIGUOUS_SIBLING
    assert decision.reason == REASON_MULTIPLE
    assert decision.allow_link is False
    assert decision.allow_create is False


def test_foreign_meta_item_blocks():
    parent = _parent()
    variants = [_variant("591001")]
    decision = evaluate_canonical_sibling_bind(
        parent,
        current_rid="398551325",
        variants=variants,
        live_by_rid={"398551325-591001": _live()},
        occupied_meta_item_ids={"META-SIB": 99},
        sibling_payloads={"398551325-591001": _payload()},
    )
    assert decision.reason == REASON_FOREIGN_META
    assert decision.allow_create is False


def test_lineage_mismatch_blocks():
    parent = _parent()
    variants = [_variant("591001")]
    decision = evaluate_canonical_sibling_bind(
        parent,
        current_rid="398551325",
        variants=variants,
        live_by_rid={"398551325-591001": _live(retailer_id="99001-1")},
        sibling_payloads={"398551325-591001": _payload()},
    )
    assert decision.reason == REASON_LINEAGE
    assert decision.allow_create is False


def test_price_mismatch_blocks():
    parent = _parent()
    variants = [_variant("591001")]
    decision = evaluate_canonical_sibling_bind(
        parent,
        current_rid="398551325",
        variants=variants,
        live_by_rid={"398551325-591001": _live(price=50)},
        sibling_payloads={"398551325-591001": _payload()},
    )
    assert decision.reason == REASON_CONTENT
    assert "price" in decision.content_mismatches
    assert decision.allow_create is False


def test_foreign_product_live_rid_does_not_link():
    parent = _parent()
    variants = [_variant("591001")]
    decision = evaluate_canonical_sibling_bind(
        parent,
        current_rid="398551325",
        variants=variants,
        live_by_rid={"99001-1": _live(id="META-OTHER", retailer_id="99001-1")},
        sibling_payloads={"398551325-591001": _payload()},
    )
    assert decision.action == ACTION_CREATE
    assert decision.allow_link is False


def test_idempotent_same_meta_item_id():
    parent = _parent(meta_item_id="META-SIB")
    variants = [_variant("591001")]
    first = evaluate_canonical_sibling_bind(
        parent,
        current_rid="398551325",
        variants=variants,
        live_by_rid={"398551325-591001": _live()},
        sibling_payloads={"398551325-591001": _payload()},
    )
    second = evaluate_canonical_sibling_bind(
        parent,
        current_rid="398551325",
        variants=variants,
        live_by_rid={"398551325-591001": _live()},
        sibling_payloads={"398551325-591001": _payload()},
    )
    assert first.meta_product_id == second.meta_product_id == "META-SIB"
    assert second.idempotent is True
    assert second.allow_create is False


def test_idempotent_link_still_enforces_content_and_occupancy():
    parent = _parent(meta_item_id="META-SIB")
    variants = [_variant("591001")]
    content = evaluate_canonical_sibling_bind(
        parent,
        current_rid="398551325",
        variants=variants,
        live_by_rid={"398551325-591001": _live(price=50)},
        sibling_payloads={"398551325-591001": _payload()},
    )
    assert content.action == ACTION_BLOCK
    assert content.reason == REASON_CONTENT
    occupied = evaluate_canonical_sibling_bind(
        parent,
        current_rid="398551325",
        variants=variants,
        live_by_rid={"398551325-591001": _live()},
        occupied_meta_item_ids={"META-SIB": 99},
        sibling_payloads={"398551325-591001": _payload()},
    )
    assert occupied.reason == REASON_FOREIGN_META
    assert occupied.allow_create is False


def test_existing_identity_requires_unique_hit():
    parent = _parent()
    variants = [_variant("591001"), _variant("591002")]
    live = {"398551325-591001", "398551325-591002"}
    assert live_canonical_sibling_hits(parent, live, current_rid="398551325", variants=variants) == [
        "398551325-591001",
        "398551325-591002",
    ]
    assert existing_identity_retailer_id(
        parent, live, current_rid="398551325", variants=variants,
    ) is None
    assert existing_identity_retailer_id(
        parent, {"398551325-591001"}, current_rid="398551325", variants=variants,
    ) == "398551325-591001"


def test_already_bound_without_live_hit_blocks_create():
    parent = _parent(meta_item_id="META-SIB")
    variants = [_variant("591001")]
    decision = evaluate_canonical_sibling_bind(
        parent,
        current_rid="398551325",
        variants=variants,
        live_by_rid={},
        sibling_payloads={"398551325-591001": _payload()},
    )
    assert decision.action == ACTION_BLOCK
    assert decision.reason == "bound_identity_unproven"
    assert decision.allow_create is False


def test_idless_live_row_blocks():
    parent = _parent()
    variants = [_variant("591001")]
    decision = evaluate_canonical_sibling_bind(
        parent,
        current_rid="398551325",
        variants=variants,
        live_by_rid={"398551325-591001": _live(id="")},
        sibling_payloads={"398551325-591001": _payload()},
    )
    assert decision.action == ACTION_BLOCK
    assert decision.reason == "lookup_unproven"
    assert decision.allow_create is False


def test_non_salla_source_with_sibling_hit_blocks():
    parent = _parent(source="unknown")
    variants = [_variant("591001")]
    decision = evaluate_canonical_sibling_bind(
        parent,
        current_rid="398551325",
        variants=variants,
        live_by_rid={"398551325-591001": _live()},
        sibling_payloads={"398551325-591001": _payload()},
    )
    assert decision.reason == REASON_LINEAGE
    assert decision.allow_create is False
    assert decision.allow_link is False


def test_lookalike_cdn_host_is_not_meta():
    parent = _parent()
    variants = [_variant("591001")]
    decision = evaluate_canonical_sibling_bind(
        parent,
        current_rid="398551325",
        variants=variants,
        live_by_rid={
            "398551325-591001": _live(
                image_url="https://scontent.evil.example/v.jpg",
            ),
        },
        sibling_payloads={
            "398551325-591001": _payload(image_url="https://cdn.example/other.jpg"),
        },
    )
    assert decision.reason == REASON_CONTENT
    assert "image_url" in decision.content_mismatches


def test_url_query_mismatch_blocks():
    parent = _parent()
    variants = [_variant("591001")]
    decision = evaluate_canonical_sibling_bind(
        parent,
        current_rid="398551325",
        variants=variants,
        live_by_rid={"398551325-591001": _live(url="https://store.example/p/skirt?variant=B")},
        sibling_payloads={"398551325-591001": _payload(url="https://store.example/p/skirt?variant=A")},
    )
    assert decision.reason == REASON_CONTENT
    assert "url" in decision.content_mismatches


def test_integer_price_does_not_match_undotted_major():
    parent = _parent()
    variants = [_variant("591001")]
    decision = evaluate_canonical_sibling_bind(
        parent,
        current_rid="398551325",
        variants=variants,
        live_by_rid={"398551325-591001": _live(price="120")},
        sibling_payloads={"398551325-591001": _payload(price=12000)},
    )
    assert decision.reason == REASON_CONTENT
    assert "price" in decision.content_mismatches
