"""
Integration test — P0 draft sync must produce outbound reply (message_events contract).

Simulates the operational path (cart → catalog → bridge → draft confirmation)
that the webhook relies on before persisting MessageEvent rows.

Live DB verification: backend/scripts/verify_wa_draft_p0_live.py
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.wa_cart_catalog_resolver import ITEM_STATUS_CONFIRMED  # noqa: E402
from modules.ai.brain.types import MerchantConversationState, OrderPreparationState  # noqa: E402
from services.product_resolver import ProductResolution  # noqa: E402


def _product_resolution(**kwargs) -> ProductResolution:
    base = dict(
        id=42,
        external_id="honey-talh-ext",
        title="عسل طلح نجد",
        price="120",
        sale_price=None,
        image_url=None,
        product_url=None,
        description=None,
        in_stock=True,
        can_checkout=True,
        variants=[
            {"id": 1, "option_summary": "1kg", "salla_variant_id": "v1kg", "price": "120"},
            {"id": 2, "option_summary": "500g", "salla_variant_id": "v500", "price": "70"},
        ],
        needs_variant_choice=True,
        default_variant_id=1,
        default_variant_retailer_id="v1kg",
        has_variants=True,
        matched_query="عسل طلح",
        confidence="fts",
    )
    base.update(kwargs)
    return ProductResolution(**base)


class _OutboundRecorder:
    """Minimal stand-in for webhook MessageEvent persistence."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def record(self, *, conversation_id: int, body: str, trigger: str) -> None:
        if not (body or "").strip():
            raise AssertionError(f"silent outbound blocked | trigger={trigger}")
        self.events.append({
            "conversation_id": conversation_id,
            "direction": "outbound",
            "body": body.strip(),
            "trigger": trigger,
        })


def _run_turn(
    recorder: _OutboundRecorder,
    *,
    db: MagicMock,
    conversation_id: int,
    message: str,
    brain_state: MerchantConversationState,
    prep: OrderPreparationState,
    tenant_id: int = 33,
) -> tuple[MerchantConversationState, OrderPreparationState, bool]:
    from modules.ai.brain.commerce.cart_state import maybe_apply_cart_message  # noqa: PLC0415
    from core.wa_cart_catalog_resolver import resolve_and_enrich_cart_state  # noqa: PLC0415
    from core.wa_draft_confirmation import maybe_inject_draft_flow_reply  # noqa: PLC0415
    from services.nahla_order_bridge import sync_nahla_wa_order  # noqa: PLC0415

    brain_state.stage = brain_state.stage or "ordering"
    cart_before = list(brain_state.cart_items or [])
    maybe_apply_cart_message(
        state=brain_state,
        prep=prep,
        message=message,
        product_info=brain_state.current_product_focus,
    )
    cart_after = list(brain_state.cart_items or [])
    cart_changed = cart_before != cart_after or bool(prep.cart_deltas)

    catalog_resolution = None
    if cart_after:
        catalog_resolution = resolve_and_enrich_cart_state(db, tenant_id, brain_state, prep)

    reply = maybe_inject_draft_flow_reply(
        reply="",
        order_prep=prep,
        brain_state=brain_state,
        catalog_resolution=catalog_resolution,
        cart_changed=cart_changed,
    )

    bs = brain_state.to_dict()
    op = bs.get("order_prep") or {}
    conv = SimpleNamespace(id=conversation_id, tenant_id=tenant_id, customer_id=1, extra_metadata={})
    cust = SimpleNamespace(id=1, tenant_id=tenant_id, phone="966551309999")

    with patch("services.nahla_order_bridge._draft_bridge_enabled", return_value=True):
        order = sync_nahla_wa_order(
            db,
            tenant_id=tenant_id,
            conversation=conv,
            brain_state=bs,
            order_prep=op,
            trigger="integration_test",
            customer=cust,
        )

    if order is not None:
        recorder.record(
            conversation_id=conversation_id,
            body=reply,
            trigger=f"after_draft_sync:{message[:20]}",
        )

    return brain_state, prep, order is not None


@pytest.fixture
def honey_db() -> MagicMock:
    return MagicMock()


@pytest.fixture
def honey_resolution() -> ProductResolution:
    return _product_resolution()


def test_live_contract_talh_kabir_four_pcs_produces_outbound_per_draft_sync(
    honey_db: MagicMock,
    honey_resolution: ProductResolution,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NAHLA_ORDER_DRAFT_BRIDGE_ENABLED", "1")
    monkeypatch.setenv("NAHLA_ORDER_DRAFT_BRIDGE_TENANTS", "33")

    recorder = _OutboundRecorder()
    state = MerchantConversationState(stage="ordering")
    prep = OrderPreparationState()
    conv_id = 99001

    turns = ["أحتاج عسل طلح أسود", "كبير", "٤ حبات"]

    with patch(
        "services.product_resolver.resolve_best_effort",
        return_value=honey_resolution,
    ):
        with patch("services.nahla_order_bridge._draft_bridge_enabled", return_value=True):
            with patch(
                "services.nahla_order_bridge.sync_nahla_wa_order",
                wraps=lambda *a, **k: SimpleNamespace(
                    id=1,
                    line_items=k.get("order_prep", {}).get("line_items") or [],
                    status="draft",
                ),
            ):
                for msg in turns:
                    state, prep, synced = _run_turn(
                        recorder,
                        db=honey_db,
                        conversation_id=conv_id,
                        message=msg,
                        brain_state=state,
                        prep=prep,
                    )
                    assert synced, f"expected draft sync on {msg!r}"
                    assert recorder.events, f"expected outbound after draft sync on {msg!r}"

    assert len(recorder.events) == 3
    items = list(prep.line_items or state.cart_items or [])
    assert items
    assert items[0].get("match_status") == ITEM_STATUS_CONFIRMED
    assert items[0].get("product_id")
    assert "رجال" not in str(items[0].get("product_name") or "")
    assert any("موقع" in e["body"] or "عنوان" in e["body"] for e in recorder.events)


def test_live_contract_samr_bucket_produces_outbound_no_confirmed_free_text(
    honey_db: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NAHLA_ORDER_DRAFT_BRIDGE_ENABLED", "1")
    monkeypatch.setenv("NAHLA_ORDER_DRAFT_BRIDGE_TENANTS", "33")

    samr = _product_resolution(
        title="عسل سمر الحجاز",
        external_id="honey-samr-ext",
        variants=[
            {"id": 3, "option_summary": "1kg", "salla_variant_id": "s1"},
            {"id": 4, "option_summary": "500g", "salla_variant_id": "s500"},
        ],
    )

    recorder = _OutboundRecorder()
    state = MerchantConversationState(stage="ordering")
    prep = OrderPreparationState()

    with patch("services.product_resolver.resolve_best_effort", return_value=samr):
        with patch(
            "services.nahla_order_bridge.sync_nahla_wa_order",
            wraps=lambda *a, **k: SimpleNamespace(id=2, status="draft"),
        ):
            for msg in ("سمر", "10 كيلo سطل؟"):
                state, prep, synced = _run_turn(
                    recorder,
                    db=honey_db,
                    conversation_id=99002,
                    message=msg,
                    brain_state=state,
                    prep=prep,
                )
                assert synced
                assert recorder.events[-1]["body"].strip()

    last_body = recorder.events[-1]["body"]
    assert "10" in last_body or "سطل" in last_body or "كيلo" in last_body or "كيلو" in last_body
    items = list(prep.line_items or [])
    if items:
        assert items[0].get("match_status") != "confirmed" or items[0].get("product_id")
