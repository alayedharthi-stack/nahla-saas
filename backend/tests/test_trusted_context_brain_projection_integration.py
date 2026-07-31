"""Integration tests for trusted-context Brain/Compose projection wiring."""
from __future__ import annotations

import asyncio
import os
import sys
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.decision.actions import ACTION_LLM_REPLY, ACTION_SEARCH_PRODUCTS  # noqa: E402
from modules.ai.brain.truth_surface.contract import (  # noqa: E402
    TrustedContextSnapshot,
    TrustedDomain,
    TrustedFact,
    TruthSource,
)
from modules.ai.brain.truth_surface.trusted_context import (  # noqa: E402
    clear_trusted_context,
    set_current_trusted_context,
)
from modules.ai.brain.types import (  # noqa: E402
    ActionResult,
    CommerceFacts,
    Decision,
    INTENT_ASK_PRODUCT,
    Intent,
    MerchantConversationState,
)

_MERCHANT = "متجر تجريبي عام"
_PHONE = "966500000099"
_TENANT = 9001
_CONV = 42


def _run(coro):
    return asyncio.run(coro)


def _catalog_snapshot(*, include_order: bool = False) -> TrustedContextSnapshot:
    facts = [
        TrustedFact(
            domain=TrustedDomain.CATALOG,
            key="product_id",
            value=501,
            source=TruthSource.PRODUCTS_TABLE,
        ),
        TrustedFact(
            domain=TrustedDomain.CATALOG,
            key="title",
            value="حذاء رياضي أبيض",
            source=TruthSource.PRODUCTS_TABLE,
        ),
        TrustedFact(
            domain=TrustedDomain.CATALOG,
            key="price",
            value="199.00",
            source=TruthSource.PRODUCTS_TABLE,
        ),
        TrustedFact(
            domain=TrustedDomain.CATALOG,
            key="available",
            value=True,
            source=TruthSource.PRODUCTS_TABLE,
        ),
        TrustedFact(
            domain=TrustedDomain.CATALOG,
            key="product_url",
            value="https://store.example.test/products/501",
            source=TruthSource.PRODUCTS_TABLE,
        ),
        TrustedFact(
            domain=TrustedDomain.CATALOG,
            key="catalog:product_sale_offer",
            value={
                "sample_products": [
                    {"title": "قميص قطني أزرق", "sale_price": "80", "regular_price": "100"},
                ],
            },
            source=TruthSource.PRODUCTS_TABLE,
        ),
        TrustedFact(
            domain=TrustedDomain.CATALOG,
            key="product_candidates",
            value=[
                {"ref": 1, "product_id": 711, "title": "قميص قطني أزرق", "price": "80"},
                {"ref": 2, "product_id": 712, "title": "عطر ورد 100ml", "price": "149"},
            ],
            source=TruthSource.ORDER_PREPARATION_STATE,
        ),
    ]
    if include_order:
        facts.extend(
            [
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
            ]
        )
    snap = TrustedContextSnapshot(
        tenant_id=_TENANT,
        customer_phone=_PHONE,
        conversation_id=_CONV,
        facts=facts,
        loaded_domains=["catalog", "order", "shipment"],
    )
    snap.ensure_snapshot_id()
    return snap


def _commerce_facts() -> CommerceFacts:
    return CommerceFacts(
        store_name=_MERCHANT,
        store_url="https://example.test",
        store_url_resolved=True,
        store_url_source="settings",
        has_products=True,
        product_count=3,
        in_stock_count=2,
        orderable=True,
        has_coupons=False,
    )


def _brain_stack(brain, snap: TrustedContextSnapshot, *, state: MerchantConversationState):
    intent = Intent(name=INTENT_ASK_PRODUCT, confidence=0.92, slots={})
    decision = Decision(action=ACTION_LLM_REPLY, args={})
    stack = ExitStack()
    stack.enter_context(patch("core.billing.has_billing_access", return_value=True))
    stack.enter_context(
        patch(
            "core.wa_usage.check_limit",
            return_value=SimpleNamespace(
                allowed=True,
                used_total=0,
                limit=1000,
                reason="",
            ),
        )
    )
    stack.enter_context(
        patch(
            "modules.ai.brain.truth_surface.flags.is_trusted_context_brain_projection_enabled",
            return_value=True,
        )
    )
    stack.enter_context(
        patch(
            "modules.ai.brain.truth_surface.trusted_context_brain_consumption_gate.is_trusted_context_brain_projection_enabled",
            return_value=True,
        )
    )
    stack.enter_context(patch.object(brain._classifier, "classify", return_value=intent))
    stack.enter_context(patch.object(brain._decision_engine, "decide", return_value=decision))
    stack.enter_context(patch.object(brain._policy_gate, "gate", side_effect=lambda d, _ctx: d))
    stack.enter_context(patch.object(brain._state_store, "load", return_value=state))
    stack.enter_context(patch.object(brain._state_store, "save"))
    stack.enter_context(patch.object(brain._facts_loader, "load", return_value=_commerce_facts()))
    stack.enter_context(patch.object(brain._memory_updater, "update"))
    compose_mock = stack.enter_context(
        patch(
            "modules.ai.brain.persona.fact_bound_composer.FactBoundPersonaComposer.compose",
            new_callable=AsyncMock,
        )
    )
    from modules.ai.brain.persona.facts_bundle import PersonaComposeResult  # noqa: E402

    compose_mock.return_value = PersonaComposeResult(
        text="رد تجريبي من الحقائق المؤكدة.",
        source="persona_llm",
        surface="catalog_product",
        facts_hash="integration-hash",
        guard_passed=True,
        language="ar",
    )
    set_current_trusted_context(snap)
    return stack, compose_mock


def test_pipeline_attaches_projection_to_brain_context_and_reply_state() -> None:
    from modules.ai.brain import pipeline as brain_pipeline  # noqa: PLC0415
    from modules.ai.brain.pipeline import get_brain  # noqa: E402

    clear_trusted_context()
    brain = get_brain()
    snap = _catalog_snapshot(include_order=True)
    state = MerchantConversationState(stage="browsing", greeted=True)
    stack, _compose_mock = _brain_stack(brain, snap, state=state)
    captured: dict = {}
    original_build = brain_pipeline._build_reply_state

    def _capture_reply_state(**kwargs):
        captured["ctx"] = kwargs["ctx"]
        reply_state = original_build(**kwargs)
        captured["reply_state"] = reply_state
        captured["brain_attestation"] = getattr(kwargs["ctx"], "model_payload_attestation", None)
        captured["reply_attestation"] = getattr(reply_state, "model_payload_attestation", None)
        return reply_state

    with stack:
        with patch.object(brain_pipeline, "_build_reply_state", side_effect=_capture_reply_state):
            result = _run(
                brain.process(
                    db=MagicMock(),
                    tenant_id=_TENANT,
                    customer_phone=_PHONE,
                    message="هل الحذاء متوفر؟",
                    history=[],
                    profile={"preferred_language": "ar"},
                    conversation_id=_CONV,
                )
            )

    try:
        ctx = captured["ctx"]
        projection = getattr(ctx, "trusted_context_projection", None)
        assert isinstance(projection, dict)
        assert projection.get("product_identity", {}).get("product_id") == 501
        assert projection.get("order", {}).get("external_id") == "RRRD1234"
        assert projection.get("shipment", {}).get("tracking_number") == "TRK-7788"
        assert projection.get("facts_snapshot_id") == snap.snapshot_id

        reply_state = captured["reply_state"]
        known = dict(getattr(reply_state, "known_facts", None) or {})
        wired = known.get("trusted_context_projection") or {}
        assert wired.get("product_identity", {}).get("title") == "حذاء رياضي أبيض"
        assert [row["ref"] for row in wired.get("product_candidates") or []] == [1, 2]
        assert wired.get("conversational_reference", {}).get("candidate_count") == 2
        assert wired["product_candidates"][0]["product_id"] == 711
        assert wired["product_candidates"][1]["product_id"] == 712
        assert reply_state.selected_product is not None
        assert reply_state.selected_product.get("product_id") == 501

        from modules.ai.brain.truth_surface.model_payload_attestation import (  # noqa: E402
            assert_attestation_redacted,
        )

        ctx_attestation = captured.get("brain_attestation")
        reply_attestation = captured.get("reply_attestation")
        assert isinstance(ctx_attestation, dict)
        assert isinstance(reply_attestation, dict)
        assert ctx_attestation.get("stage") == "brain"
        assert reply_attestation.get("stage") == "reply_state"
        assert ctx_attestation["facts_loaded"]["facts_snapshot_id"] == snap.snapshot_id
        assert reply_attestation["facts_reaching_brain"]["product_id"] == 501
        assert reply_attestation["candidate_ids_and_order"] == [
            {"ref": 1, "product_id": 711},
            {"ref": 2, "product_id": 712},
        ]
        assert reply_attestation["selected_product_and_variant"]["product_id"] == 501
        assert_attestation_redacted(reply_attestation)
    finally:
        clear_trusted_context()


def test_build_reply_state_preserves_legacy_selected_product() -> None:
    from modules.ai.brain.pipeline import _build_reply_state  # noqa: PLC0415
    from modules.ai.brain.types import (  # noqa: PLC0415
        BrainContext,
        CommerceFacts,
        Decision,
        Intent,
        SuggestionSnapshot,
    )

    snap = _catalog_snapshot()
    set_current_trusted_context(snap)
    try:
        state = MerchantConversationState(
            stage="browsing",
            greeted=True,
            current_product_focus={
                "product_id": 999,
                "id": 999,
                "title": "قميص قطني أزرق",
                "price": "80",
            },
        )
        ctx = BrainContext(
            tenant_id=_TENANT,
            customer_phone=_PHONE,
            conversation_id=_CONV,
            message="كم سعره؟",
            intent=Intent(name=INTENT_ASK_PRODUCT, confidence=0.9, slots={}),
            state=state,
            facts=_commerce_facts(),
            trusted_context_projection={
                "product_identity": {"product_id": 501, "title": "حذاء رياضي أبيض"},
            },
        )
        reply_state = _build_reply_state(
            ctx=ctx,
            previous_state=state,
            current_state=state,
            suggestion=SuggestionSnapshot(),
            decision=Decision(action=ACTION_LLM_REPLY, args={}),
            merchant_context={},
            db=MagicMock(),
        )
        assert reply_state.selected_product is not None
        assert reply_state.selected_product.get("product_id") == 999
        projection = (reply_state.known_facts or {}).get("trusted_context_projection") or {}
        assert projection.get("product_identity", {}).get("product_id") == 501
    finally:
        clear_trusted_context()


def test_cross_tenant_snapshot_not_consumed() -> None:
    from modules.ai.brain import pipeline as brain_pipeline  # noqa: PLC0415
    from modules.ai.brain.pipeline import get_brain  # noqa: E402

    clear_trusted_context()
    brain = get_brain()
    snap = _catalog_snapshot()
    snap.tenant_id = 7777
    state = MerchantConversationState(stage="browsing", greeted=True)
    stack, _compose_mock = _brain_stack(brain, snap, state=state)
    captured: dict = {}
    original_build = brain_pipeline._build_reply_state

    def _capture_reply_state(**kwargs):
        captured["ctx"] = kwargs["ctx"]
        reply_state = original_build(**kwargs)
        captured["reply_state"] = reply_state
        return reply_state

    with stack:
        with patch.object(brain_pipeline, "_build_reply_state", side_effect=_capture_reply_state):
            _run(
                brain.process(
                    db=MagicMock(),
                    tenant_id=_TENANT,
                    customer_phone=_PHONE,
                    message="هل متوفر؟",
                    history=[],
                    profile={"preferred_language": "ar"},
                    conversation_id=_CONV,
                )
            )

    try:
        ctx = captured["ctx"]
        assert getattr(ctx, "trusted_context_projection", None) is None
        known = dict(getattr(captured["reply_state"], "known_facts", None) or {})
        assert "trusted_context_projection" not in known
    finally:
        clear_trusted_context()


def test_conversation_isolation_snapshot_conv_context_none() -> None:
    from modules.ai.brain import pipeline as brain_pipeline  # noqa: PLC0415
    from modules.ai.brain.pipeline import get_brain  # noqa: E402

    clear_trusted_context()
    brain = get_brain()
    snap = _catalog_snapshot()
    state = MerchantConversationState(stage="browsing", greeted=True)
    stack, _compose_mock = _brain_stack(brain, snap, state=state)
    captured: dict = {}
    original_build = brain_pipeline._build_reply_state

    def _capture_reply_state(**kwargs):
        captured["ctx"] = kwargs["ctx"]
        reply_state = original_build(**kwargs)
        captured["reply_state"] = reply_state
        return reply_state

    with stack:
        with patch.object(brain_pipeline, "_build_reply_state", side_effect=_capture_reply_state):
            _run(
                brain.process(
                    db=MagicMock(),
                    tenant_id=_TENANT,
                    customer_phone=_PHONE,
                    message="هل متوفر؟",
                    history=[],
                    profile={"preferred_language": "ar"},
                    conversation_id=None,
                )
            )

    try:
        ctx = captured["ctx"]
        assert getattr(ctx, "trusted_context_projection", None) is None
        known = dict(getattr(captured["reply_state"], "known_facts", None) or {})
        assert "trusted_context_projection" not in known
    finally:
        clear_trusted_context()


def test_conversation_isolation_context_conv_snapshot_none() -> None:
    from modules.ai.brain import pipeline as brain_pipeline  # noqa: PLC0415
    from modules.ai.brain.pipeline import get_brain  # noqa: E402

    clear_trusted_context()
    brain = get_brain()
    snap = _catalog_snapshot()
    snap.conversation_id = None
    state = MerchantConversationState(stage="browsing", greeted=True)
    stack, _compose_mock = _brain_stack(brain, snap, state=state)
    captured: dict = {}
    original_build = brain_pipeline._build_reply_state

    def _capture_reply_state(**kwargs):
        captured["ctx"] = kwargs["ctx"]
        reply_state = original_build(**kwargs)
        captured["reply_state"] = reply_state
        return reply_state

    with stack:
        with patch.object(brain_pipeline, "_build_reply_state", side_effect=_capture_reply_state):
            _run(
                brain.process(
                    db=MagicMock(),
                    tenant_id=_TENANT,
                    customer_phone=_PHONE,
                    message="هل متوفر؟",
                    history=[],
                    profile={"preferred_language": "ar"},
                    conversation_id=_CONV,
                )
            )

    try:
        ctx = captured["ctx"]
        assert getattr(ctx, "trusted_context_projection", None) is None
        known = dict(getattr(captured["reply_state"], "known_facts", None) or {})
        assert "trusted_context_projection" not in known
    finally:
        clear_trusted_context()


def test_missing_trusted_context_leaves_legacy_compose_path() -> None:
    from modules.ai.brain import pipeline as brain_pipeline  # noqa: PLC0415
    from modules.ai.brain.pipeline import get_brain  # noqa: E402

    clear_trusted_context()
    brain = get_brain()
    state = MerchantConversationState(
        stage="browsing",
        greeted=True,
        current_product_focus={"product_id": 12, "title": "منتج قديم", "price": "50"},
    )
    stack = ExitStack()
    stack.enter_context(patch("core.billing.has_billing_access", return_value=True))
    stack.enter_context(
        patch(
            "core.wa_usage.check_limit",
            return_value=SimpleNamespace(
                allowed=True,
                used_total=0,
                limit=1000,
                reason="",
            ),
        )
    )
    stack.enter_context(
        patch(
            "modules.ai.brain.truth_surface.flags.is_trusted_context_brain_projection_enabled",
            return_value=True,
        )
    )
    stack.enter_context(
        patch.object(
            brain._classifier,
            "classify",
            return_value=Intent(name=INTENT_ASK_PRODUCT, confidence=0.9, slots={}),
        )
    )
    stack.enter_context(
        patch.object(
            brain._decision_engine,
            "decide",
            return_value=Decision(action=ACTION_LLM_REPLY, args={}),
        )
    )
    stack.enter_context(patch.object(brain._policy_gate, "gate", side_effect=lambda d, _ctx: d))
    stack.enter_context(patch.object(brain._state_store, "load", return_value=state))
    stack.enter_context(patch.object(brain._state_store, "save"))
    stack.enter_context(patch.object(brain._facts_loader, "load", return_value=_commerce_facts()))
    stack.enter_context(patch.object(brain._memory_updater, "update"))
    captured: dict = {}
    original_build = brain_pipeline._build_reply_state

    def _capture_reply_state(**kwargs):
        reply_state = original_build(**kwargs)
        captured["reply_state"] = reply_state
        return reply_state

    with stack:
        with patch.object(brain_pipeline, "_build_reply_state", side_effect=_capture_reply_state):
            result = _run(
                brain.process(
                    db=MagicMock(),
                    tenant_id=_TENANT,
                    customer_phone=_PHONE,
                    message="كم سعره؟",
                    history=[],
                    profile={"preferred_language": "ar"},
                    conversation_id=_CONV,
                )
            )

    assert isinstance(result, dict)
    known = dict(getattr(captured.get("reply_state"), "known_facts", None) or {})
    assert "trusted_context_projection" not in known


def _snapshot_without_candidates() -> TrustedContextSnapshot:
    snap = TrustedContextSnapshot(
        tenant_id=_TENANT,
        customer_phone=_PHONE,
        conversation_id=_CONV,
        facts=[
            TrustedFact(
                domain=TrustedDomain.ORDER,
                key="external_id",
                value="RRRD1234",
                source=TruthSource.ORDER_PREPARATION_STATE,
            ),
        ],
        loaded_domains=["order"],
    )
    snap.ensure_snapshot_id()
    return snap


_SEARCH_PRODUCTS = [
    {
        "id": 701,
        "product_id": 701,
        "title": "فستان سهرة أزرق",
        "price": 289.0,
        "can_checkout": True,
        "orderable": True,
        "external_id": "ext-dress-701",
    },
    {
        "id": 702,
        "product_id": 702,
        "title": "فستان صيفي وردي",
        "price": 149.0,
        "can_checkout": True,
        "orderable": True,
        "external_id": "ext-dress-702",
    },
]


def test_pipeline_refreshes_search_candidates_before_compose() -> None:
    """Post-execute search must reach Brain/Compose projection on the same turn."""
    from modules.ai.brain import pipeline as brain_pipeline  # noqa: PLC0415
    from modules.ai.brain.pipeline import get_brain  # noqa: E402

    clear_trusted_context()
    brain = get_brain()
    snap = _snapshot_without_candidates()
    state = MerchantConversationState(stage="browsing", greeted=True)
    intent = Intent(name=INTENT_ASK_PRODUCT, confidence=0.92, slots={"product_query": "فساتين"})
    decision = Decision(
        action=ACTION_SEARCH_PRODUCTS,
        args={"query": "فساتين"},
        reason="browse dresses",
    )
    stack = ExitStack()
    stack.enter_context(patch("core.billing.has_billing_access", return_value=True))
    stack.enter_context(
        patch(
            "core.wa_usage.check_limit",
            return_value=SimpleNamespace(
                allowed=True,
                used_total=0,
                limit=1000,
                reason="",
            ),
        )
    )
    stack.enter_context(
        patch(
            "modules.ai.brain.truth_surface.flags.is_trusted_context_shadow_enabled",
            return_value=True,
        )
    )
    stack.enter_context(
        patch(
            "modules.ai.brain.truth_surface.flags.is_trusted_context_brain_projection_enabled",
            return_value=True,
        )
    )
    stack.enter_context(
        patch(
            "modules.ai.brain.truth_surface.trusted_context_brain_consumption_gate.is_trusted_context_brain_projection_enabled",
            return_value=True,
        )
    )
    stack.enter_context(patch.object(brain._classifier, "classify", return_value=intent))
    stack.enter_context(patch.object(brain._decision_engine, "decide", return_value=decision))
    stack.enter_context(patch.object(brain._policy_gate, "gate", side_effect=lambda d, _ctx: d))
    stack.enter_context(patch.object(brain._state_store, "load", return_value=state))
    saved_state: dict = {}

    def _save_state(_db, _tid, _phone, st):
        saved_state["state"] = st

    stack.enter_context(patch.object(brain._state_store, "save", side_effect=_save_state))
    stack.enter_context(patch.object(brain._facts_loader, "load", return_value=_commerce_facts()))
    stack.enter_context(patch.object(brain._memory_updater, "update"))
    stack.enter_context(
        patch(
            "modules.ai.brain.commerce.commerce_browse_category_guard.filter_products_for_browse_turn",
            side_effect=lambda products, **kwargs: list(products),
        )
    )
    stack.enter_context(
        patch.object(
            brain._executor,
            "execute",
            new=AsyncMock(
                return_value=ActionResult(
                    success=True,
                    data={"products": list(_SEARCH_PRODUCTS), "count": 2, "query": "فساتين"},
                ),
            ),
        )
    )
    compose_mock = stack.enter_context(
        patch(
            "modules.ai.brain.persona.fact_bound_composer.FactBoundPersonaComposer.compose",
            new_callable=AsyncMock,
        )
    )
    from modules.ai.brain.persona.facts_bundle import PersonaComposeResult  # noqa: E402

    compose_mock.return_value = PersonaComposeResult(
        text="رد تجريبي بعد البحث.",
        source="persona_llm",
        surface="catalog_product",
        facts_hash="search-refresh-hash",
        guard_passed=True,
        language="ar",
    )
    set_current_trusted_context(snap)
    captured: dict = {}
    original_build = brain_pipeline._build_reply_state

    def _capture_reply_state(**kwargs):
        captured["ctx"] = kwargs["ctx"]
        reply_state = original_build(**kwargs)
        captured["reply_state"] = reply_state
        captured["reply_attestation"] = getattr(reply_state, "model_payload_attestation", None)
        return reply_state

    with stack:
        with patch.object(brain_pipeline, "_build_reply_state", side_effect=_capture_reply_state):
            _run(
                brain.process(
                    db=MagicMock(),
                    tenant_id=_TENANT,
                    customer_phone=_PHONE,
                    message="اعرض لي فساتين",
                    history=[],
                    profile={"preferred_language": "ar"},
                    conversation_id=_CONV,
                )
            )

    try:
        persisted = saved_state.get("state")
        assert persisted is not None
        assert len(persisted.last_search_candidates or []) == 2

        projection = getattr(captured["ctx"], "trusted_context_projection", None)
        assert isinstance(projection, dict)
        assert [row["ref"] for row in projection.get("product_candidates") or []] == [1, 2]
        assert projection["product_candidates"][0]["product_id"] == 701
        assert projection["product_candidates"][1]["product_id"] == 702

        known = dict(getattr(captured["reply_state"], "known_facts", None) or {})
        wired = known.get("trusted_context_projection") or {}
        assert wired.get("conversational_reference", {}).get("candidate_count") == 2

        reply_attestation = captured.get("reply_attestation")
        assert isinstance(reply_attestation, dict)
        assert reply_attestation.get("candidate_ids_and_order") == [
            {"ref": 1, "product_id": 701},
            {"ref": 2, "product_id": 702},
        ]
        assert reply_attestation["facts_reaching_brain"]["candidate_count"] == 2
        assert "catalog" in reply_attestation["facts_loaded"]["loaded_domains"]
    finally:
        clear_trusted_context()


def test_search_candidate_isolation_across_customers() -> None:
    from modules.ai.brain.truth_surface.trusted_context import (  # noqa: PLC0415
        clear_trusted_context,
        refresh_trusted_context_brain_state,
        set_current_trusted_context,
    )

    phone_a = "966500000001"
    phone_b = "966500000002"
    state_a = MerchantConversationState(
        last_search_candidates=[
            {"id": 801, "title": "قميص قطني أزرق", "price": 80},
        ],
    )
    state_b = MerchantConversationState(last_search_candidates=[])

    empty_snap = TrustedContextSnapshot(
        tenant_id=_TENANT,
        customer_phone=phone_a,
        conversation_id=_CONV,
        facts=[],
        loaded_domains=[],
    )
    empty_snap.ensure_snapshot_id()
    set_current_trusted_context(empty_snap)

    with patch(
        "modules.ai.brain.truth_surface.trusted_context.is_trusted_context_shadow_enabled",
        return_value=True,
    ), patch(
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
    ):
        refreshed_a = refresh_trusted_context_brain_state(
            db=None,
            tenant_id=_TENANT,
            customer_phone=phone_a,
            conversation_id=_CONV,
            brain_state=state_a,
        )
        assert refreshed_a is not None
        rows_a = next(
            fact.value
            for fact in refreshed_a.facts
            if fact.domain == TrustedDomain.CATALOG and fact.key == "product_candidates"
        )
        assert len(rows_a) == 1
        assert rows_a[0]["product_id"] == 801

        refreshed_b = refresh_trusted_context_brain_state(
            db=None,
            tenant_id=_TENANT,
            customer_phone=phone_b,
            conversation_id=_CONV,
            brain_state=state_b,
        )
        assert refreshed_b is not None
        candidate_facts_b = [
            fact
            for fact in refreshed_b.facts
            if fact.domain == TrustedDomain.CATALOG and fact.key == "product_candidates"
        ]
        assert candidate_facts_b == []
        assert refreshed_b.customer_phone == phone_b
    clear_trusted_context()
