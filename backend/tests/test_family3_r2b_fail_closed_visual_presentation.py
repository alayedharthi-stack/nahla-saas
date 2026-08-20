"""Family 3 R2-B — bind visual/title fallback after Meta membership fail-closed.

AD-F3-R2B-1: after canonical membership fails closed, presentation stays on
the structured Product identity. Title FTS / inbound text must not invent a
sibling SKU. Unstructured visual asks without a canonical id keep the existing
title cascade (not AI-D02).
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
_REPO = _BACKEND.parent
for _p in (str(_BACKEND), str(_REPO)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.fail_closed_visual_presentation import (  # noqa: E402
    REASON_NO_SAFE_VISUAL,
    REASON_UNBOUND,
    REASON_VARIANT_MISS,
    SOURCE_STRUCTURED_IDENTITY,
    bind_from_local_facts,
    extract_structured_product_id,
    extract_structured_variant_id,
    is_membership_fail_closed,
    resolved_sku_matches_canonical,
    rewrite_queued_card_after_membership_fail_closed,
    should_block_title_query_substitution,
    stamp_membership_fail_closed,
)
from core.meta_catalog_membership import (  # noqa: E402
    PROVENANCE_GRAPH_RECONCILE,
    MetaCatalogMembershipFact,
)
from core.native_catalog_capability import (  # noqa: E402
    REASON_META_CATALOG_UNVERIFIED,
)
from services.catalog_product_orchestrator import (  # noqa: E402
    ProductCardSendAction,
    evaluate_product_card_send,
)
from services.product_resolver import ProductResolution  # noqa: E402
from services.visual_product_dispatch import maybe_enforce_visual_product_card  # noqa: E402

_CATALOG_A = "catalog-aaa"
_PUBLISHED = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _conn(catalog_id: str = _CATALOG_A, **kw):
    defaults = dict(
        status="connected",
        sending_enabled=True,
        phone_number_id="1234567890",
        catalog_enabled=True,
        meta_catalog_id=catalog_id,
        provider="meta",
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _product(*, product_id: int = 101, title: str = "قميص قطني أزرق", published: bool = False):
    return SimpleNamespace(
        id=product_id,
        tenant_id=7,
        title=title,
        external_id=f"ext-{product_id}",
        meta_retailer_id=None,
        in_stock=True,
        catalog_status="active",
        meta_catalog_published_at=_PUBLISHED if published else None,
        has_variants=False,
        default_variant_id=None,
    )


def _attachment(*, product_id: int = 101, title: str = "قميص قطني أزرق", **kw):
    base = dict(
        kind="product_card",
        id=product_id,
        title=title,
        external_id=f"ext-{product_id}",
        file_url="https://cdn.example/shirt.jpg",
        product_url="https://shop.example/shirt",
        in_stock=True,
        confidence="fts",
    )
    base.update(kw)
    return base


def _membership(*, product_id: int = 101, retailer_id: str = "meta-rid-101"):
    return MetaCatalogMembershipFact(
        tenant_id=7,
        catalog_id=_CATALOG_A,
        retailer_id=retailer_id,
        product_id=product_id,
        variant_id=None,
        meta_item_id="mg-1",
        verified_at=_PUBLISHED,
        provenance=PROVENANCE_GRAPH_RECONCILE,
    )


def _resolution(
    *,
    product_id: int,
    title: str,
    image: str = "https://cdn.example/p.jpg",
    url: str = "https://shop.example/p",
):
    return ProductResolution(
        id=product_id,
        external_id=f"ext-{product_id}",
        title=title,
        price="100",
        sale_price=None,
        image_url=image or None,
        product_url=url or None,
        description=None,
        in_stock=True,
        can_checkout=True,
        confidence="exact",
    )


def _dispatch_kwargs(**over):
    base = dict(
        db=object(),
        tenant_id=7,
        inbound_message="أبغى أشوف صورة المنتج",
        reply_text="",
        brain_action="product_visual_request",
        brain_state={},
        product_attachments=[],
        media_attachments=[],
        product_escalation_blocked=False,
        fulfillment_discovery_blocked=False,
        allow_product_cards=True,
        dispatch_guard_reason="",
        catalog_card_limit=2,
    )
    base.update(over)
    return base


class TestStructuredIdentityExtraction:
    def test_ignores_title_only_rows(self):
        pid, ext = extract_structured_product_id({"title": "عطر ورد 100ml"})
        assert pid is None
        assert ext == ""

    def test_prefers_numeric_id_over_later_title_row(self):
        pid, ext = extract_structured_product_id(
            {"title": "قميص قطني أزرق"},
            {"id": 101, "title": "قميص قطني أزرق"},
        )
        assert pid == 101
        assert ext == ""

    def test_tenant_scoped_id_is_not_swapped_by_same_title(self):
        assert resolved_sku_matches_canonical(
            canonical_product_id=101, resolved_product_id=202,
        ) is False
        assert resolved_sku_matches_canonical(
            canonical_product_id=101, resolved_product_id=101,
        ) is True


class TestMembershipFailClosedDetection:
    def test_orchestrator_stamps_fail_closed_and_keeps_canonical_id(self):
        d = evaluate_product_card_send(
            tenant_id=7,
            connection=_conn(),
            attachment=_attachment(),
            product_row=_product(published=False),
        )
        assert d.action == ProductCardSendAction.FALLBACK_LEGACY
        assert d.reason == REASON_META_CATALOG_UNVERIFIED
        assert d.diagnostics["membership_fail_closed"] is True
        assert d.diagnostics["canonical_product_id"] == 101
        assert is_membership_fail_closed(d) is True

    def test_valid_membership_still_authorizes_native_send(self):
        row = _product(published=True)
        row.meta_retailer_id = "meta-rid-101"
        d = evaluate_product_card_send(
            tenant_id=7,
            connection=_conn(),
            attachment=_attachment(),
            product_row=row,
            membership=_membership(),
        )
        assert d.action == ProductCardSendAction.SEND_CATALOG
        assert d.diagnostics.get("membership_fail_closed") is not True
        assert is_membership_fail_closed(d) is False

    def test_collision_fallback_is_not_membership_fail_closed(self):
        d = evaluate_product_card_send(
            tenant_id=7,
            connection=_conn(),
            attachment=_attachment(),
            product_row=_product(),
            collision_peer_ids=[202],
        )
        assert d.action == ProductCardSendAction.FALLBACK_LEGACY
        assert d.diagnostics.get("membership_fail_closed") is not True
        assert is_membership_fail_closed(d) is False

    def test_stamp_writes_caller_owned_audit_only(self):
        d = evaluate_product_card_send(
            tenant_id=7,
            connection=_conn(),
            attachment=_attachment(),
            product_row=_product(published=False),
        )
        audit: dict = {}
        stamp_membership_fail_closed(audit, d)
        assert audit["membership_fail_closed"] is True
        assert audit["canonical_product_id"] == 101

    def test_stamp_preserves_bound_variant_id(self):
        d = evaluate_product_card_send(
            tenant_id=7,
            connection=_conn(),
            attachment=_attachment(picked_variant_id=9, needs_variant_choice=False),
            product_row=_product(published=False),
        )
        assert d.diagnostics["canonical_variant_id"] == 9
        audit: dict = {}
        stamp_membership_fail_closed(audit, d)
        assert audit["canonical_variant_id"] == 9
        assert extract_structured_variant_id(audit) == 9


class TestFailClosedPresentationBind:
    def test_safe_visual_stays_on_canonical_referent(self):
        bound = bind_from_local_facts(
            product_id=101,
            title="حذاء رياضي أبيض",
            image_url="https://cdn.example/shoe.jpg",
            product_url="https://shop.example/shoe",
        )
        assert bound.allow_presentation is True
        assert bound.product_id == 101
        assert bound.source == SOURCE_STRUCTURED_IDENTITY

    def test_no_safe_visual_fails_closed_without_inventing_sku(self):
        bound = bind_from_local_facts(product_id=101, title="حذاء رياضي أبيض")
        assert bound.canonical_present is True
        assert bound.allow_presentation is False
        assert bound.reason == REASON_NO_SAFE_VISUAL

    def test_unbound_referent_is_not_a_canonical_hit(self):
        bound = bind_from_local_facts(title="عطر ورد 100ml")
        assert bound.canonical_present is False
        assert bound.reason == REASON_UNBOUND

    def test_title_query_blocked_after_membership_fail_closed(self):
        assert should_block_title_query_substitution(membership_fail_closed=True) is True
        assert should_block_title_query_substitution(canonical_product_id=101) is True
        assert should_block_title_query_substitution(canonical_variant_id=9) is True
        assert should_block_title_query_substitution() is False

    def test_selected_variant_keeps_variant_image_not_parent(self):
        bound = bind_from_local_facts(
            product_id=101,
            variant_id=9,
            title="حذاء رياضي أبيض — 42",
            image_url="https://cdn.example/shoe-42.jpg",
            product_url="https://shop.example/shoe",
        )
        assert bound.variant_id == 9
        assert bound.image_url == "https://cdn.example/shoe-42.jpg"
        assert resolved_sku_matches_canonical(
            canonical_product_id=101,
            resolved_product_id=101,
            canonical_variant_id=9,
            resolved_variant_id=9,
        ) is True
        assert resolved_sku_matches_canonical(
            canonical_product_id=101,
            resolved_product_id=101,
            canonical_variant_id=9,
            resolved_variant_id=None,
        ) is False

    def test_non_default_variant_without_image_fails_closed(self):
        bound = bind_from_local_facts(
            product_id=101,
            variant_id=9,
            title="حذاء رياضي أبيض — 42",
            image_url="",
            product_url="",
        )
        assert bound.canonical_present is True
        assert bound.variant_id == 9
        assert bound.allow_presentation is False
        assert bound.reason == REASON_NO_SAFE_VISUAL


class TestVisualDispatchStructuredBind:
    def test_structured_focus_does_not_use_same_title_sibling(self):
        sibling = _resolution(product_id=202, title="قميص قطني أزرق")
        canonical = _resolution(product_id=101, title="قميص قطني أزرق")
        with patch(
            "modules.observability.customer_wants_product_or_image",
            return_value=True,
        ), patch(
            "modules.observability.has_visual_marker",
            return_value=False,
        ), patch(
            "services.product_resolver.resolve_by_product_id",
            return_value=canonical,
        ), patch(
            "services.product_resolver.resolve_by_query",
            return_value=sibling,
        ) as title_query, patch(
            "services.product_resolver.format_product_card_caption",
            return_value="shirt-card",
        ):
            cards, enforced = maybe_enforce_visual_product_card(
                **_dispatch_kwargs(
                    brain_state={
                        "current_product_focus": {
                            "id": 101,
                            "title": "قميص قطني أزرق",
                        }
                    }
                )
            )
        assert enforced is True
        assert cards[0]["id"] == 101
        assert cards[0]["candidate_origin"] == SOURCE_STRUCTURED_IDENTITY
        title_query.assert_not_called()

    def test_selected_variant_dispatch_does_not_emit_parent_only_card(self):
        parent = _resolution(
            product_id=101,
            title="حذاء رياضي أبيض",
            image="https://cdn.example/parent.jpg",
        )
        with patch(
            "modules.observability.customer_wants_product_or_image",
            return_value=True,
        ), patch(
            "modules.observability.has_visual_marker",
            return_value=False,
        ), patch(
            "services.product_resolver.resolve_by_product_id",
            return_value=parent,
        ), patch(
            "core.fail_closed_visual_presentation.load_bound_variant_facts",
            return_value={
                "variant_id": 9,
                "image_url": "https://cdn.example/size-42.jpg",
                "option_summary": "42",
                "in_stock": True,
                "is_default": False,
            },
        ), patch(
            "services.product_resolver.resolve_by_query",
        ) as title_query:
            cards, enforced = maybe_enforce_visual_product_card(
                **_dispatch_kwargs(
                    brain_state={
                        "current_product_focus": {
                            "id": 101,
                            "title": "حذاء رياضي أبيض",
                            "picked_variant_id": 9,
                        }
                    }
                )
            )
        assert enforced is True
        assert cards[0]["id"] == 101
        assert cards[0]["picked_variant_id"] == 9
        assert cards[0]["file_url"] == "https://cdn.example/size-42.jpg"
        assert cards[0]["file_url"] != "https://cdn.example/parent.jpg"
        assert cards[0]["title"] == "حذاء رياضي أبيض — 42"
        assert str(cards[0]["caption"]).splitlines()[0] == "حذاء رياضي أبيض — 42"
        title_query.assert_not_called()

    def test_missing_variant_row_fails_closed_not_parent_photo(self):
        parent = _resolution(
            product_id=101,
            title="حذاء رياضي أبيض",
            image="https://cdn.example/parent.jpg",
        )
        with patch(
            "modules.observability.customer_wants_product_or_image",
            return_value=True,
        ), patch(
            "modules.observability.has_visual_marker",
            return_value=False,
        ), patch(
            "services.product_resolver.resolve_by_product_id",
            return_value=parent,
        ), patch(
            "core.fail_closed_visual_presentation.load_bound_variant_facts",
            return_value=None,
        ), patch(
            "services.product_resolver.resolve_by_query",
        ) as title_query:
            cards, enforced = maybe_enforce_visual_product_card(
                **_dispatch_kwargs(
                    brain_state={
                        "current_product_focus": {
                            "id": 101,
                            "picked_variant_id": 9,
                        }
                    }
                )
            )
        assert enforced is False
        assert cards == []
        title_query.assert_not_called()

        from core.fail_closed_visual_presentation import bind_structured_visual_referent

        with patch(
            "services.product_resolver.resolve_by_product_id",
            return_value=parent,
        ), patch(
            "core.fail_closed_visual_presentation.load_bound_variant_facts",
            return_value=None,
        ):
            miss = bind_structured_visual_referent(
                object(),
                7,
                brain_state={"current_product_focus": {"id": 101, "picked_variant_id": 9}},
            )
        assert miss.reason == REASON_VARIANT_MISS
        assert miss.variant_id == 9
        assert miss.allow_presentation is False

    def test_canonical_without_visual_fails_closed_not_title_fts(self):
        empty = _resolution(product_id=101, title="عطر ورد 100ml", image="", url="")
        with patch(
            "modules.observability.customer_wants_product_or_image",
            return_value=True,
        ), patch(
            "modules.observability.has_visual_marker",
            return_value=False,
        ), patch(
            "services.product_resolver.resolve_by_product_id",
            return_value=empty,
        ), patch(
            "services.product_resolver.resolve_by_query",
        ) as title_query:
            cards, enforced = maybe_enforce_visual_product_card(
                **_dispatch_kwargs(
                    brain_state={"current_product_focus": {"id": 101, "title": "عطر ورد 100ml"}}
                )
            )
        assert enforced is False
        assert cards == []
        title_query.assert_not_called()

    def test_tenant_miss_fails_closed_not_cross_tenant_title_match(self):
        with patch(
            "modules.observability.customer_wants_product_or_image",
            return_value=True,
        ), patch(
            "modules.observability.has_visual_marker",
            return_value=False,
        ), patch(
            "services.product_resolver.resolve_by_product_id",
            return_value=None,
        ), patch(
            "services.product_resolver.resolve_by_external_id",
            return_value=None,
        ), patch(
            "services.product_resolver.resolve_by_query",
        ) as title_query:
            cards, enforced = maybe_enforce_visual_product_card(
                **_dispatch_kwargs(
                    tenant_id=8,
                    brain_state={"current_product_focus": {"id": 101, "external_id": "ext-101"}},
                )
            )
        assert enforced is False
        assert cards == []
        title_query.assert_not_called()

    def test_unstructured_visual_ask_still_uses_title_cascade(self):
        found = _resolution(product_id=303, title="حذاء رياضي أبيض")
        with patch(
            "modules.observability.customer_wants_product_or_image",
            return_value=True,
        ), patch(
            "modules.observability.has_visual_marker",
            return_value=False,
        ), patch(
            "modules.observability.pick_best_candidate_title",
            return_value=("حذاء رياضي أبيض", "inbound_text_fuzzy"),
        ), patch(
            "services.product_resolver.resolve_by_query",
            return_value=found,
        ) as title_query, patch(
            "services.product_resolver.format_product_card_caption",
            return_value="shoe-card",
        ):
            cards, enforced = maybe_enforce_visual_product_card(**_dispatch_kwargs())
        assert enforced is True
        assert cards[0]["id"] == 303
        title_query.assert_called_once()


class TestQueuedCardRewriteAfterMembershipFailClosed:
    def test_queued_parent_photo_is_replaced_with_bound_variant(self):
        parent = _resolution(
            product_id=101,
            title="حذاء رياضي أبيض",
            image="https://cdn.example/parent.jpg",
        )
        queued = _attachment(
            product_id=101,
            title="حذاء رياضي أبيض",
            picked_variant_id=9,
        )
        queued["file_url"] = "https://cdn.example/parent.jpg"
        queued["caption"] = "حذاء رياضي أبيض\nالسعر: 100 ر.س"
        with patch(
            "services.product_resolver.resolve_by_product_id",
            return_value=parent,
        ), patch(
            "core.fail_closed_visual_presentation.load_bound_variant_facts",
            return_value={
                "variant_id": 9,
                "image_url": "https://cdn.example/size-42.jpg",
                "option_summary": "42",
                "in_stock": True,
                "is_default": False,
            },
        ):
            out = rewrite_queued_card_after_membership_fail_closed(
                object(),
                7,
                queued,
                audit={
                    "membership_fail_closed": True,
                    "canonical_product_id": 101,
                    "canonical_variant_id": 9,
                },
            )
        assert out is not None
        assert out["file_url"] == "https://cdn.example/size-42.jpg"
        assert out["file_url"] != queued["file_url"]
        assert out["title"] == "حذاء رياضي أبيض — 42"
        assert out["caption"] == "حذاء رياضي أبيض — 42"
        assert out["picked_variant_id"] == 9

    def test_queued_missing_variant_fails_closed_not_parent_photo(self):
        parent = _resolution(
            product_id=101,
            title="حذاء رياضي أبيض",
            image="https://cdn.example/parent.jpg",
        )
        queued = _attachment(product_id=101, picked_variant_id=9)
        queued["file_url"] = "https://cdn.example/parent.jpg"
        with patch(
            "services.product_resolver.resolve_by_product_id",
            return_value=parent,
        ), patch(
            "core.fail_closed_visual_presentation.load_bound_variant_facts",
            return_value=None,
        ):
            out = rewrite_queued_card_after_membership_fail_closed(
                object(),
                7,
                queued,
                audit={
                    "membership_fail_closed": True,
                    "canonical_product_id": 101,
                    "canonical_variant_id": 9,
                },
            )
        assert out is None

    def test_unstructured_queued_card_without_id_is_unchanged(self):
        queued = {
            "kind": "product_card",
            "title": "حذاء رياضي أبيض",
            "file_url": "https://cdn.example/fts.jpg",
        }
        out = rewrite_queued_card_after_membership_fail_closed(
            object(),
            7,
            queued,
            audit={"membership_fail_closed": True},
        )
        assert out is not None
        assert out["file_url"] == queued["file_url"]
        assert out["title"] == queued["title"]


def test_inbound_title_rescue_blocked_when_membership_failed_closed():
    assert should_block_title_query_substitution(
        membership_fail_closed=True,
        canonical_product_id=101,
    ) is True
    assert should_block_title_query_substitution(
        membership_fail_closed=False,
        canonical_product_id=None,
        canonical_external_id="",
    ) is False
