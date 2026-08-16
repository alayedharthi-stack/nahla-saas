"""
backend/tests/test_native_catalog_meta_readiness.py
────────────────────────────────────────────────────
Meta-confirmed catalog readiness + honest native catalog fallback tests.
"""
from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from unittest.mock import MagicMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.native_catalog_capability import (  # noqa: E402
    REASON_META_CATALOG_UNPUBLISHED,
    NativeCatalogCapability,
    evaluate_native_catalog_capability,
    invalidate_meta_catalog_publish_for_retailer_id,
    pick_thumbnail_retailer_id,
)
from modules.ai.brain.catalog.navigation import (  # noqa: E402
    STEP_SHOW_GROUPS,
    try_catalog_navigation_decision,
)
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
)
from core.native_catalog_fallback import (  # noqa: E402
    compose_native_catalog_failure_decision,
    compose_native_catalog_failure_reply,
)
from modules.ai.brain.commerce.checkout_route_owner import (  # noqa: E402
    CheckoutChannelCapabilities,
    evaluate_checkout_route_owner,
    has_checkout_entry_intent,
)
from services.whatsapp_platform import catalog_sender as cs  # noqa: E402


@dataclass
class _Conn:
    meta_catalog_id: Optional[str] = "CAT-777"
    catalog_enabled: bool = True
    phone_number_id: str = "PHONE1"
    status: str = "connected"
    sending_enabled: bool = True


@dataclass
class _Product:
    id: int
    title: str
    tenant_id: int = 7
    external_id: Optional[str] = None
    meta_retailer_id: Optional[str] = None
    sku: Optional[str] = None
    in_stock: bool = True
    catalog_status: str = "active"
    meta_catalog_published_at: Any = field(
        default_factory=lambda: datetime(2026, 5, 30, tzinfo=timezone.utc)
    )


def _db_with_products(products: list[_Product]) -> MagicMock:
    db = MagicMock()
    db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = (
        products
    )
    variant_q = db.query.return_value.join.return_value.filter.return_value
    variant_q.order_by.return_value.limit.return_value.all.return_value = []
    db.query.return_value.filter.return_value.all.return_value = products
    return db


def _browse_ctx(*, db: Any = None) -> BrainContext:
    state = MerchantConversationState(greeted=True, stage="discovery", turn=2)
    facts = CommerceFacts(has_products=True, product_count=5)
    ctx = BrainContext(
        tenant_id=7,
        customer_phone="966542980511",
        message="وش عندكم منتجات",
        intent=Intent(name="general", confidence=0.5, raw_message="وش عندكم منتجات"),
        state=state,
        facts=facts,
        history=[],
    )
    if db is not None:
        ctx._db = db  # type: ignore[attr-defined]
    return ctx


class TestMetaConfirmedReadiness:
    def test_unpublished_retailer_id_blocks_native_catalog(self):
        db = _db_with_products([
            _Product(
                id=1,
                title="Local only",
                meta_retailer_id="local-rid",
                meta_catalog_published_at=None,
            ),
        ])
        cap = evaluate_native_catalog_capability(db, 7, connection=_Conn())
        assert cap.eligible is False
        assert cap.reason == REASON_META_CATALOG_UNPUBLISHED

    def test_published_retailer_id_allows_native_catalog(self):
        db = _db_with_products([
            _Product(id=2, title="Published", meta_retailer_id="meta-rid-2"),
        ])
        cap = evaluate_native_catalog_capability(db, 7, connection=_Conn())
        assert cap.eligible is True
        assert cap.thumbnail_retailer_id == "meta-rid-2"

    def test_pick_thumbnail_skips_unpublished_products(self):
        db = _db_with_products([
            _Product(
                id=3,
                title="Old",
                meta_retailer_id="stale-rid",
                meta_catalog_published_at=None,
            ),
            _Product(id=4, title="Published", meta_retailer_id="good-rid"),
        ])
        assert pick_thumbnail_retailer_id(db, 7) == "good-rid"

    def test_no_tenant_or_catalog_hardcode(self):
        db = _db_with_products([
            _Product(id=5, title="Any tenant", meta_retailer_id="rid-any"),
        ])
        cap = evaluate_native_catalog_capability(db, 999, connection=_Conn(meta_catalog_id="ANY-CAT"))
        assert cap.eligible is True
        assert cap.thumbnail_retailer_id == "rid-any"


class TestNativeCatalogFallbackDecision:
    def test_meta_products_not_found_fallback_is_honest(self):
        with patch(
            "modules.ai.brain.commerce.checkout_route_owner.load_channel_capabilities",
            return_value=CheckoutChannelCapabilities(whatsapp_fast=True),
        ):
            decision = compose_native_catalog_failure_decision(
                MagicMock(),
                7,
                failure_reason="meta_products_not_found",
            )
        assert "تفضّل، اختر من الكتالوج" not in decision.text
        assert "ما ظهر الكتالوج" in decision.text

    def test_store_url_fallback_uses_cta(self):
        with patch(
            "modules.ai.brain.commerce.checkout_route_owner.load_channel_capabilities",
            return_value=CheckoutChannelCapabilities(
                whatsapp_fast=True,
                store_link=True,
                store_url="https://example.com/store",
            ),
        ):
            decision = compose_native_catalog_failure_decision(
                MagicMock(),
                7,
                failure_reason="meta_products_not_found",
            )
        assert decision.delivery_mode == "cta_url"
        assert decision.cta_url == "https://example.com/store"
        assert decision.cta_label == "فتح المتجر الإلكتروني"

    def test_no_store_url_fallback_uses_whatsapp_quick_order(self):
        with patch(
            "modules.ai.brain.commerce.checkout_route_owner.load_channel_capabilities",
            return_value=CheckoutChannelCapabilities(whatsapp_fast=True),
        ):
            decision = compose_native_catalog_failure_decision(
                MagicMock(),
                7,
                failure_reason="meta_products_not_found",
            )
        assert decision.delivery_mode == "text"
        assert "اكتب اسم المنتج" in decision.text

    def test_compose_reply_wrapper_never_claims_catalog(self):
        with patch(
            "modules.ai.brain.commerce.checkout_route_owner.load_channel_capabilities",
            return_value=CheckoutChannelCapabilities(whatsapp_fast=True),
        ):
            reply = compose_native_catalog_failure_reply(
                MagicMock(),
                7,
                failure_reason="meta_products_not_found",
            )
        assert "تفضّل، اختر من الكتالوج" not in reply


class TestCatalogSenderMetaFailure:
    def test_send_catalog_message_meta_products_not_found(self, monkeypatch):
        async def _fake_provider_send_message(*_a, **_k):
            return (
                {
                    "error": {
                        "message": "(#131009) Parameter value is not valid",
                        "code": 131009,
                        "error_data": {"details": "Products not found in FB Catalog"},
                    }
                },
                {},
            )

        monkeypatch.setattr(cs, "provider_send_message", _fake_provider_send_message)
        result = asyncio.run(
            cs.send_catalog_message(
                MagicMock(),
                _Conn(),
                tenant_id=7,
                to="966500000000",
                phone_id="PHONE1",
                thumbnail_product_retailer_id="bad-rid",
                body_text="ignored",
            )
        )
        assert result.success is False
        assert result.reason == "meta_products_not_found"


class TestInvalidatePublishStamp:
    def test_clears_publish_stamp_for_rejected_retailer_id(self):
        product = _Product(id=9, title="Rejected", meta_retailer_id="bad-rid")
        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = [product]
        cleared = invalidate_meta_catalog_publish_for_retailer_id(db, 7, "bad-rid")
        assert cleared == 1
        assert product.meta_catalog_published_at is None
        db.flush.assert_called_once()


class TestBrowseIntentCapabilityRouting:
    def test_products_browse_uses_fallback_when_meta_unconfirmed(self):
        ctx = _browse_ctx(db=MagicMock())
        ctx.message = "وش عندكم منتجات"
        ctx.intent.raw_message = "وش عندكم منتجات"
        cap = NativeCatalogCapability(
            eligible=False,
            reason=REASON_META_CATALOG_UNPUBLISHED,
        )
        with patch(
            "core.native_catalog_capability.evaluate_native_catalog_capability",
            return_value=cap,
        ), patch(
            "modules.ai.brain.catalog.navigation._load_catalog_groups",
            return_value=[{"group_name": "A"}],
        ), patch(
            "modules.ai.brain.catalog.navigation.evaluate_catalog_navigation_signals",
        ) as sig:
            sig.return_value = MagicMock(
                hard_blocked=False,
                advisory_or_comparison=False,
                catalog_browse_score=0.9,
                catalog_browse_intent=True,
                confidence=0.92,
                evidence={},
            )
            decision = try_catalog_navigation_decision(ctx)
        assert decision is not None
        assert decision.args.get("navigator_step") == STEP_SHOW_GROUPS


class TestCheckoutRouteStillWorks:
    def test_start_order_still_prompts_channel_choice(self, monkeypatch):
        monkeypatch.setenv("CHECKOUT_ROUTE_OWNER_ENABLED", "1")
        with patch(
            "modules.ai.brain.commerce.checkout_route_owner.load_checkout_route_context",
            return_value=("discovery", {}),
        ), patch(
            "modules.ai.brain.commerce.checkout_route_owner.load_channel_capabilities",
            return_value=CheckoutChannelCapabilities(
                whatsapp_fast=True,
                store_link=True,
                store_url="https://example.com/store",
                store_name="متجر",
            ),
        ), patch(
            "modules.ai.brain.commerce.checkout_route_owner.persist_checkout_route_state",
            return_value=True,
        ):
            decision = evaluate_checkout_route_owner(
                MagicMock(),
                tenant_id=7,
                customer_phone="966500000001",
                message="ابي اطلب",
            )
        assert decision is None
        assert has_checkout_entry_intent("ابي اطلب") is True
