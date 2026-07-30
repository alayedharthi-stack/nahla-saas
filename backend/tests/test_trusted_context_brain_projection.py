"""Unit tests for trusted-context Brain/Compose projection."""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.truth_surface.contract import (  # noqa: E402
    TrustedContextSnapshot,
    TrustedDomain,
    TrustedFact,
    TruthSource,
)
from modules.ai.brain.truth_surface.trusted_context import (  # noqa: E402
    build_trusted_context_snapshot,
)
from modules.ai.brain.truth_surface.trusted_context_brain_projection import (  # noqa: E402
    TrustedContextBrainProjectionError,
    project_trusted_context_brain_facts,
    selected_product_from_projection,
    validate_snapshot_scope,
)
from modules.ai.brain.types import MerchantConversationState  # noqa: E402

_PHONE = "966500000099"
_OTHER_PHONE = "966500000088"
_TENANT = 9001
_OTHER_TENANT = 9002
_CONV = 42
_OTHER_CONV = 99


def _snapshot(**kwargs) -> TrustedContextSnapshot:
    defaults = dict(
        tenant_id=_TENANT,
        customer_phone=_PHONE,
        conversation_id=_CONV,
        facts=[],
        loaded_domains=["catalog"],
        sources=["brain_state.order_prep"],
    )
    defaults.update(kwargs)
    snap = TrustedContextSnapshot(**defaults)
    snap.ensure_snapshot_id()
    return snap


def _catalog_focus_facts() -> list[TrustedFact]:
    return [
        TrustedFact(
            domain=TrustedDomain.CATALOG,
            key="product_id",
            value=501,
            source=TruthSource.PRODUCTS_TABLE,
            path="brain_state.current_product_focus.product_id",
        ),
        TrustedFact(
            domain=TrustedDomain.CATALOG,
            key="title",
            value="حذاء رياضي أبيض",
            source=TruthSource.PRODUCTS_TABLE,
            path="brain_state.current_product_focus.title",
        ),
        TrustedFact(
            domain=TrustedDomain.CATALOG,
            key="price",
            value="199.00",
            source=TruthSource.PRODUCTS_TABLE,
            path="brain_state.current_product_focus.price",
        ),
        TrustedFact(
            domain=TrustedDomain.CATALOG,
            key="available",
            value=True,
            source=TruthSource.PRODUCTS_TABLE,
            path="brain_state.current_product_focus.available",
        ),
        TrustedFact(
            domain=TrustedDomain.CATALOG,
            key="product_url",
            value="https://store.example.test/products/501",
            source=TruthSource.PRODUCTS_TABLE,
            path="brain_state.current_product_focus.product_url",
        ),
    ]


def test_scope_rejects_tenant_mismatch() -> None:
    snap = _snapshot(tenant_id=_OTHER_TENANT, facts=_catalog_focus_facts())
    with pytest.raises(TrustedContextBrainProjectionError, match="tenant_mismatch"):
        validate_snapshot_scope(
            snapshot=snap,
            tenant_id=_TENANT,
            customer_phone=_PHONE,
            conversation_id=_CONV,
        )


def test_scope_rejects_customer_mismatch() -> None:
    snap = _snapshot(customer_phone=_OTHER_PHONE, facts=_catalog_focus_facts())
    with pytest.raises(TrustedContextBrainProjectionError, match="customer_mismatch"):
        validate_snapshot_scope(
            snapshot=snap,
            tenant_id=_TENANT,
            customer_phone=_PHONE,
            conversation_id=_CONV,
        )


def test_scope_rejects_conversation_mismatch() -> None:
    snap = _snapshot(conversation_id=_OTHER_CONV, facts=_catalog_focus_facts())
    with pytest.raises(TrustedContextBrainProjectionError, match="conversation_mismatch"):
        validate_snapshot_scope(
            snapshot=snap,
            tenant_id=_TENANT,
            customer_phone=_PHONE,
            conversation_id=_CONV,
        )


def test_scope_rejects_snapshot_conversation_when_context_none() -> None:
    snap = _snapshot(conversation_id=_CONV, facts=_catalog_focus_facts())
    with pytest.raises(TrustedContextBrainProjectionError, match="conversation_mismatch"):
        validate_snapshot_scope(
            snapshot=snap,
            tenant_id=_TENANT,
            customer_phone=_PHONE,
            conversation_id=None,
        )


def test_scope_rejects_context_conversation_when_snapshot_none() -> None:
    snap = _snapshot(conversation_id=None, facts=_catalog_focus_facts())
    with pytest.raises(TrustedContextBrainProjectionError, match="conversation_mismatch"):
        validate_snapshot_scope(
            snapshot=snap,
            tenant_id=_TENANT,
            customer_phone=_PHONE,
            conversation_id=_CONV,
        )


def test_project_product_identity_without_synthesizing_sale_samples() -> None:
    sale_record = {
        "domain": TrustedDomain.CATALOG.value,
        "bundle_namespace": "product_sale_offer",
        "question_kind": "store_wide",
        "product_sale_availability": "active_sale_present",
        "sample_products": [
            {"title": "قميص قطني أزرق", "sale_price": "80", "regular_price": "100"},
            {"title": "عطر ورد 100ml", "sale_price": "149", "regular_price": "199"},
        ],
        "allow_price_mention": True,
    }
    snap = _snapshot(
        facts=_catalog_focus_facts()
        + [
            TrustedFact(
                domain=TrustedDomain.CATALOG,
                key="catalog:product_sale_offer",
                value=sale_record,
                source=TruthSource.PRODUCTS_TABLE,
            )
        ],
        loaded_domains=["catalog", "order"],
    )
    out = project_trusted_context_brain_facts(
        snapshot=snap,
        tenant_id=_TENANT,
        customer_phone=_PHONE,
        conversation_id=_CONV,
    )
    assert out["surface"] == "trusted_context_brain_projection"
    assert out["product_identity"]["product_id"] == 501
    assert out["product_identity"]["title"] == "حذاء رياضي أبيض"
    assert out["product_identity"]["product_url"] == "https://store.example.test/products/501"
    assert "product_candidates" not in out


def test_project_product_candidates_only_from_explicit_snapshot_fact() -> None:
    snap = _snapshot(
        facts=_catalog_focus_facts()
        + [
            TrustedFact(
                domain=TrustedDomain.CATALOG,
                key="product_candidates",
                value=[
                    {"ref": 1, "title": "قميص قطني أزرق", "product_id": 701},
                    {"ref": 2, "title": "عطر ورد 100ml", "product_id": 702},
                ],
                source=TruthSource.PRODUCTS_TABLE,
            )
        ],
    )
    out = project_trusted_context_brain_facts(
        snapshot=snap,
        tenant_id=_TENANT,
        customer_phone=_PHONE,
        conversation_id=_CONV,
    )
    assert [row["ref"] for row in out["product_candidates"]] == [1, 2]
    assert out["product_candidates"][0]["title"] == "قميص قطني أزرق"
    assert out["product_candidates"][1]["product_id"] == 702
    assert out["conversational_reference"]["candidate_count"] == 2


def test_project_order_and_shipment_facts_only_when_present() -> None:
    snap = _snapshot(
        facts=[
            TrustedFact(
                domain=TrustedDomain.ORDER,
                key="external_id",
                value="RRRD1234",
                source=TruthSource.ORDER_PREPARATION_STATE,
            ),
            TrustedFact(
                domain=TrustedDomain.SHIPMENT,
                key="tracking_number",
                value="TRK-7788",
                source=TruthSource.ORDER_PREPARATION_STATE,
            ),
            TrustedFact(
                domain=TrustedDomain.SHIPMENT,
                key="tracking_present",
                value=True,
                source=TruthSource.ORDER_PREPARATION_STATE,
            ),
        ],
        loaded_domains=["order", "shipment"],
    )
    out = project_trusted_context_brain_facts(
        snapshot=snap,
        tenant_id=_TENANT,
        customer_phone=_PHONE,
        conversation_id=_CONV,
    )
    assert out["order"]["external_id"] == "RRRD1234"
    assert out["shipment"]["tracking_number"] == "TRK-7788"
    assert out["shipment"]["tracking_present"] is True
    assert "product_identity" not in out


def test_empty_snapshot_raises_empty_projection() -> None:
    snap = _snapshot(facts=[])
    with pytest.raises(TrustedContextBrainProjectionError, match="empty_projection"):
        project_trusted_context_brain_facts(
            snapshot=snap,
            tenant_id=_TENANT,
            customer_phone=_PHONE,
            conversation_id=_CONV,
        )


def test_selected_product_from_projection_maps_identity() -> None:
    projection = {
        "product_identity": {
            "product_id": 77,
            "title": "عطر ورد 100ml",
            "price": "149",
            "available": True,
        }
    }
    selected = selected_product_from_projection(projection)
    assert selected is not None
    assert selected["id"] == 77
    assert selected["product_id"] == 77
    assert selected["title"] == "عطر ورد 100ml"


def test_gate_rejects_malformed_empty_snapshot() -> None:
    from modules.ai.brain.truth_surface.trusted_context_brain_consumption_gate import (  # noqa: PLC0415
        maybe_trusted_context_brain_projection,
    )

    snap = _snapshot(facts=[])
    with patch(
        "modules.ai.brain.truth_surface.trusted_context_brain_consumption_gate.is_trusted_context_brain_projection_enabled",
        return_value=True,
    ):
        assert (
            maybe_trusted_context_brain_projection(
                snapshot=snap,
                tenant_id=_TENANT,
                customer_phone=_PHONE,
                conversation_id=_CONV,
            )
            is None
        )


def test_gate_default_on_flag_enabled() -> None:
    from modules.ai.brain.truth_surface.flags import (  # noqa: PLC0415
        is_trusted_context_brain_projection_enabled,
    )

    with patch.dict(os.environ, {"NAHLA_TRUSTED_CONTEXT_BRAIN_PROJECTION_ENABLED": "true"}):
        assert is_trusted_context_brain_projection_enabled() is True


def test_gate_disabled_returns_none() -> None:
    from modules.ai.brain.truth_surface.trusted_context_brain_consumption_gate import (  # noqa: PLC0415
        maybe_trusted_context_brain_projection,
    )

    snap = _snapshot(facts=_catalog_focus_facts())
    with patch(
        "modules.ai.brain.truth_surface.trusted_context_brain_consumption_gate.is_trusted_context_brain_projection_enabled",
        return_value=False,
    ):
        assert (
            maybe_trusted_context_brain_projection(
                snapshot=snap,
                tenant_id=_TENANT,
                customer_phone=_PHONE,
                conversation_id=_CONV,
            )
            is None
        )


def _build_snapshot_from_brain_state(
    brain_state: MerchantConversationState,
) -> TrustedContextSnapshot:
    with patch(
        "modules.ai.brain.truth_surface.trusted_context._load_customer_order_facts",
        return_value=[],
    ), patch(
        "modules.ai.brain.truth_surface.trusted_context._load_state_order_facts",
        return_value=[],
    ), patch(
        "modules.ai.brain.truth_surface.trusted_context._load_payment_shipment_facts",
        return_value=[],
    ), patch(
        "modules.ai.brain.truth_surface.trusted_context._load_capability_facts",
        return_value=[],
    ), patch(
        "modules.ai.brain.truth_surface.trusted_context._load_merchant_policy_facts",
        return_value=[],
    ), patch(
        "modules.ai.brain.truth_surface.coupon_offer_loader.should_load_coupon_promotion_facts",
        return_value=False,
    ), patch(
        "modules.ai.brain.truth_surface.product_sale_offer_loader.should_load_product_sale_offer_facts",
        return_value=False,
    ), patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_compose_canary_gate.should_load_customer_conditional_coupon_layer0_for_turn",
        return_value=(False, "test_skip"),
    ):
        return build_trusted_context_snapshot(
            db=None,
            tenant_id=_TENANT,
            customer_phone=_PHONE,
            conversation_id=_CONV,
            brain_state=brain_state,
        )


def test_build_snapshot_emits_ordered_candidates_from_last_search_candidates() -> None:
    state = MerchantConversationState(
        last_search_candidates=[
            {"id": 701, "title": "قميص قطني أزرق", "price": "80"},
            {"id": 702, "title": "عطر ورد 100ml", "price": "149"},
        ],
    )
    snap = _build_snapshot_from_brain_state(state)
    candidate_facts = [
        fact
        for fact in snap.facts
        if fact.domain == TrustedDomain.CATALOG and fact.key == "product_candidates"
    ]
    assert len(candidate_facts) == 1
    rows = candidate_facts[0].value
    assert [row["ref"] for row in rows] == [1, 2]
    assert rows[0]["product_id"] == 701
    assert rows[1]["product_id"] == 702
    assert "catalog" in snap.loaded_domains
    assert "brain_state.last_search_candidates" in snap.sources

    out = project_trusted_context_brain_facts(
        snapshot=snap,
        tenant_id=_TENANT,
        customer_phone=_PHONE,
        conversation_id=_CONV,
    )
    assert [row["ref"] for row in out["product_candidates"]] == [1, 2]
    assert out["conversational_reference"]["source"] == "brain_state.last_search_candidates"
    assert out["conversational_reference"]["ordering"] == "list_index_1_based"
    assert out["conversational_reference"]["candidate_count"] == 2


def test_build_snapshot_candidate_rows_keep_id_and_url_binding() -> None:
    state = MerchantConversationState(
        last_search_candidates=[
            {
                "id": 801,
                "title": "حذاء رياضي أبيض",
                "product_url": "https://store.example.test/products/801",
                "image_url": "https://cdn.example.test/801.jpg",
            },
            {
                "id": 802,
                "title": "فستان سهرة",
                "product_url": "https://store.example.test/products/802",
                "cart_url": "https://store.example.test/cart/802",
            },
        ],
    )
    snap = _build_snapshot_from_brain_state(state)
    rows = next(
        fact.value
        for fact in snap.facts
        if fact.domain == TrustedDomain.CATALOG and fact.key == "product_candidates"
    )
    assert rows[0]["product_id"] == 801
    assert rows[0]["product_url"] == "https://store.example.test/products/801"
    assert rows[0]["image_url"] == "https://cdn.example.test/801.jpg"
    assert rows[1]["product_id"] == 802
    assert rows[1]["product_url"] == "https://store.example.test/products/802"
    assert rows[1]["cart_url"] == "https://store.example.test/cart/802"
    assert "image_url" not in rows[1]


def test_build_snapshot_skips_malformed_candidates() -> None:
    state = MerchantConversationState(
        last_search_candidates=[
            "not-a-dict",
            {"title": "بدون معرف"},
            {"id": 901, "title": "منتج صالح"},
            {},
        ],
    )
    snap = _build_snapshot_from_brain_state(state)
    rows = next(
        fact.value
        for fact in snap.facts
        if fact.domain == TrustedDomain.CATALOG and fact.key == "product_candidates"
    )
    assert len(rows) == 1
    assert rows[0]["ref"] == 1
    assert rows[0]["product_id"] == 901
    assert rows[0]["title"] == "منتج صالح"


def test_projection_reaches_slim_compose_payload() -> None:
    from modules.ai.brain.compose.prompt_state_serializer import (  # noqa: PLC0415
        serialize_commerce_brain_state,
    )
    from modules.ai.brain.types import BrainReplyState  # noqa: PLC0415

    snap = _snapshot(
        facts=[
            TrustedFact(
                domain=TrustedDomain.CATALOG,
                key="product_candidates",
                value=[
                    {"ref": 1, "product_id": 701, "title": "قميص قطني أزرق"},
                    {"ref": 2, "product_id": 702, "title": "عطر ورد 100ml"},
                ],
                source=TruthSource.ORDER_PREPARATION_STATE,
            )
        ],
    )
    projection = project_trusted_context_brain_facts(
        snapshot=snap,
        tenant_id=_TENANT,
        customer_phone=_PHONE,
        conversation_id=_CONV,
    )
    reply_state = BrainReplyState(
        known_facts={"trusted_context_projection": projection},
    )
    slim = serialize_commerce_brain_state(
        {"known_facts": reply_state.known_facts},
        reply_state,
        kb_in_prompt_block=False,
    )
    wired = (slim.get("known_facts") or {}).get("trusted_context_projection") or {}
    assert wired.get("conversational_reference", {}).get("candidate_count") == 2
    assert wired["product_candidates"][0]["product_id"] == 701
    assert wired["product_candidates"][1]["product_id"] == 702


def test_cross_scope_snapshot_candidates_not_projected() -> None:
    snap = _snapshot(
        tenant_id=_OTHER_TENANT,
        facts=[
            TrustedFact(
                domain=TrustedDomain.CATALOG,
                key="product_candidates",
                value=[{"ref": 1, "product_id": 701, "title": "قميص قطني أزرق"}],
                source=TruthSource.ORDER_PREPARATION_STATE,
            )
        ],
    )
    with pytest.raises(TrustedContextBrainProjectionError, match="tenant_mismatch"):
        project_trusted_context_brain_facts(
            snapshot=snap,
            tenant_id=_TENANT,
            customer_phone=_PHONE,
            conversation_id=_CONV,
        )
