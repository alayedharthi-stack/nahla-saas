"""
tests/test_active_order_context.py
────────────────────────────────────
Phase A — structured active_order_context persistence + resolution.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for p in [str(REPO_ROOT), str(BACKEND_DIR), str(DATABASE_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)


def _bundle(order_id: str, **ctx_overrides):
    ctx = {
        "order_id":         order_id,
        "external_id":      None,
        "order_status":     "pending_review",
        "raw_order_status": "under_review",
        "shipping_status":  "not_shipped",
        "tracking_url":     None,
        "tracking_number":  None,
        "confirmed_at":     "2026-05-29T12:00:00+00:00",
        "product_summary":  "عسل طلح 1kg",
    }
    ctx.update(ctx_overrides)
    return {
        "active_order_id":      order_id,
        "active_order_context": ctx,
        "recent_order_ids":     [order_id],
    }


class TestNormalizeOrderStatus:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("under_review", "pending_review"),
            ("awaiting_review", "pending_review"),
            ("processing", "preparing"),
            ("shipped", "shipped"),
            ("delivered", "delivered"),
        ],
    )
    def test_canonical_mapping(self, raw, expected):
        from core.active_order_context import normalize_order_status

        canonical, raw_slug = normalize_order_status(raw)
        assert canonical == expected
        assert raw_slug == raw


class TestPersistActiveOrderContext:
    def test_persist_on_receipt_patch_shape(self):
        from core.active_order_context import (
            load_commerce_bundle,
            maybe_persist_from_patch,
        )

        meta: dict = {"brain_state": {}}
        brain_state = {
            "draft_order_id": "262511443",
            "current_product_focus": {"title": "عسل طلح 1kg", "external_id": "ext-1"},
        }
        order_prep = {
            "order_status": "payment_submitted",
            "payment_receipt_received": True,
            "quantity": 1,
        }
        assert maybe_persist_from_patch(
            meta,
            brain_state=brain_state,
            order_prep=order_prep,
            state_patch={"payment_receipt_received": True, "order_status": "payment_submitted"},
        )

        bundle = load_commerce_bundle(meta)
        ctx = bundle["active_order_context"]
        assert bundle["active_order_id"] == "262511443"
        assert ctx["order_id"] == "262511443"
        assert ctx["order_status"] == "payment_submitted"
        assert ctx["raw_order_status"] == "payment_submitted"
        assert ctx["shipping_status"] == "not_shipped"
        assert ctx["tracking_url"] is None
        assert ctx["product_summary"] == "عسل طلح 1kg"
        assert "status" not in ctx

    def test_order_status_shipping_status_separate(self):
        from core.active_order_context import build_active_order_context

        ctx = build_active_order_context(
            order_id="1",
            brain_state={},
            order_prep={"order_status": "under_review"},
        )
        assert ctx["order_status"] == "pending_review"
        assert ctx["shipping_status"] == "not_shipped"

    def test_recent_order_ids_dedup_and_multi_order(self):
        from core.active_order_context import persist_active_order_context

        meta = {
            "active_order_id": "111",
            "active_order_context": {"order_id": "111"},
            "recent_order_ids": ["111"],
        }
        brain_state = {"draft_order_id": "222"}
        order_prep = {"order_status": "payment_submitted", "payment_receipt_received": True}
        persist_active_order_context(
            meta,
            brain_state=brain_state,
            order_prep=order_prep,
            write_source="apply_state_patch",
        )
        assert meta["active_order_id"] == "222"
        assert meta["recent_order_ids"] == ["222", "111"]

    def test_merge_preserves_brain_state(self):
        from core.active_order_context import maybe_persist_from_patch

        meta = {"brain_state": {"stage": "ordering", "turn": 3}}
        maybe_persist_from_patch(
            meta,
            brain_state={"draft_order_id": "99", "current_product_focus": {"title": "X"}},
            order_prep={"order_status": "payment_submitted", "payment_receipt_received": True},
            state_patch={"payment_receipt_received": True},
        )
        assert meta["brain_state"]["stage"] == "ordering"
        assert meta["active_order_id"] == "99"


class TestStructuredFirstResolution:
    def test_tracking_without_history_uses_structured(self):
        from core.active_order_context import (
            resolve_order_reference,
            resolve_order_status,
            structured_indicates_post_order,
        )
        from modules.ai.brain.intent.link_disambiguation import (
            has_active_post_order_context,
            should_use_generative_tracking_follow_up,
            build_tracking_follow_up_args,
        )

        bundle = _bundle("262511443")
        assert structured_indicates_post_order(bundle) is True
        assert has_active_post_order_context(commerce_bundle=bundle, history=[]) is True

        ref, ref_mode = resolve_order_reference(commerce_bundle=bundle, history=[])
        assert ref == "262511443"
        assert ref_mode == "structured"

        status, status_mode = resolve_order_status(commerce_bundle=bundle, history=[])
        assert status == "pending_review"
        assert status_mode == "structured"

        assert should_use_generative_tracking_follow_up(
            "رابط التتبع",
            history=[],
            commerce_bundle=bundle,
        ) is True

        args = build_tracking_follow_up_args(
            commerce_bundle=bundle,
            history=[],
            tracking_available=False,
        )
        assert args["tracking_available"] is False
        assert args["order_reference"] == "262511443"
        assert "tracking_url" not in args

    def test_fallback_when_structured_missing(self):
        from core.active_order_context import active_order_context_source, resolve_order_reference

        history = [
            {
                "direction": "outbound",
                "body": "طلبك رقم 262511443 تم تأكيده وهو الآن بانتظار المراجعة 🌷",
            },
        ]
        ref, mode = resolve_order_reference(commerce_bundle={}, history=history)
        assert ref == "262511443"
        assert mode == "inferred_history"
        assert active_order_context_source({}) == "inferred"

    def test_no_url_when_not_shipped(self):
        from core.active_order_context import tracking_available_from_bundle

        bundle = _bundle("262511443")
        assert tracking_available_from_bundle(bundle) is False

    def test_tracking_number_counts_without_url(self):
        from core.active_order_context import tracking_available_from_bundle

        bundle = _bundle("262511443", tracking_number="SF123456789CN")
        assert tracking_available_from_bundle(bundle) is True
