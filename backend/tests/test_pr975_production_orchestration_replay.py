"""Production-path replay for Salla browse, grounded card, and named PDP link."""
from __future__ import annotations

import asyncio
import os
import sys
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_REPO = Path(__file__).resolve().parents[2]
for _path in (_REPO, _REPO / "backend", _REPO / "database"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import models as _models  # noqa: E402
from core.inbound_dedup import reset_cache  # noqa: E402
from models import Integration, Product  # noqa: E402
from modules.ai.brain.persona.fact_bound_composer import (  # noqa: E402
    FactBoundPersonaComposer,
)
from modules.ai.brain.persona.facts_bundle import PersonaComposeResult  # noqa: E402
from modules.ai.brain.postprocess.availability_context_builder import (  # noqa: E402
    build_availability_context,
)
from modules.ai.brain.postprocess.product_availability_truth_guard import (  # noqa: E402
    apply_product_availability_truth_guard,
)
from modules.ai.brain.types import CommerceFacts  # noqa: E402
from modules.ai.orchestrator.types import AIReplyPayload  # noqa: E402
from modules.ai.postprocess.safety_nets import apply_store_link_safety_net  # noqa: E402
from services.merchant_brain_turn import (  # noqa: E402
    LiveMerchantBrainPreconditions,
    LiveMerchantBrainTurnInput,
    evaluate_live_merchant_brain_turn,
)
from tests.commerce_scenario_fixtures import (  # noqa: E402
    DEFAULT_PHONE_E164,
    ScenarioWorld,
    make_scenario_db,
    seed_conversation,
    seed_customer,
    seed_tenant,
)
from tests.salla_acceptance.layer2_harness import Layer2BrainRunner  # noqa: E402

JACKET_IMAGE_URL = "https://cdn.example/catalog/jacket.jpg"
JACKET_PRODUCT_URL = "https://shop.example/products/jacket"
STOREFRONT_URL = "https://shop.example"
sys.modules.setdefault("database.models", _models)


def _trace() -> SimpleNamespace:
    return SimpleNamespace(
        brain_called=False,
        brain_silent=False,
        response_goal="",
        response_mode="",
        reply_source="",
        fallback_source="",
        chosen_path="",
        handoff_triggered=False,
    )


def _catalog_rows() -> list[dict]:
    return [
        {
            "id": 22,
            "external_id": "dress-casual",
            "title": "فستان كاجوال",
            "display_label": "فستان كاجوال",
            "price": 149,
            "in_stock": True,
            "can_checkout": True,
            "orderable": True,
            "product_url": "https://shop.example/products/dress-casual",
        },
        {
            "id": 23,
            "external_id": "dress-evening",
            "title": "فستان سهرة",
            "display_label": "فستان سهرة",
            "price": 289,
            "in_stock": True,
            "can_checkout": True,
            "orderable": True,
            "product_url": "https://shop.example/products/dress-evening",
        },
        {
            "id": 28,
            "external_id": "jacket-28",
            "title": "جاكيت",
            "display_label": "جاكيت",
            "price": 169,
            "in_stock": True,
            "can_checkout": True,
            "orderable": True,
            "image_url": JACKET_IMAGE_URL,
            "product_url": JACKET_PRODUCT_URL,
        },
    ]


def _commerce_facts(rows: list[dict]) -> CommerceFacts:
    return CommerceFacts(
        has_products=True,
        product_count=len(rows),
        in_stock_count=len(rows),
        has_active_integration=True,
        integration_platform="salla",
        orderable=True,
        snapshot_fresh=True,
        store_name="متجر تجريبي عام",
        store_url=STOREFRONT_URL,
        store_url_resolved=True,
        store_url_source="settings",
        top_products=list(rows),
        discovery_products=list(rows),
    )


def _seed_catalog(db, tenant_id: int) -> None:
    db.add(
        Integration(
            tenant_id=tenant_id,
            provider="salla",
            external_store_id=f"store-{tenant_id}",
            enabled=True,
            config={"platform": "salla", "access_token": "test-token"},
        )
    )
    for row in _catalog_rows():
        db.add(
            Product(
                id=row["id"],
                tenant_id=tenant_id,
                external_id=row["external_id"],
                meta_retailer_id=row["external_id"],
                title=row["title"],
                price=str(row["price"]),
                in_stock=True,
                has_variants=False,
                source="salla",
                extra_metadata={
                    "status": "active",
                    "in_stock": True,
                    "product_url": row["product_url"],
                    "image_url": row.get("image_url"),
                },
            )
        )
    db.commit()


async def _replay_fixture_claims(candidate, snapshot, **kwargs):
    # Model-boundary fixtures for the unchanged compose-failure replay.
    # Provider unavailability is not evidence of the denial's contradiction.
    import json
    from modules.ai.brain.postprocess.catalog_semantic_claims import parse_claims

    denial = "لا توجد منتجات قابلة للبيع مؤكدة في الكتالوج حالياً."
    assert candidate in {denial, "هذا رابط جاكيت."}
    row = ({"scope": "catalog", "product_id": None, "attribute": "exists",
            "value": False, "quote": candidate} if candidate == denial else
           {"scope": "product", "product_id": 28, "attribute": "exists",
            "value": True, "quote": candidate})
    return parse_claims(json.dumps({"complete": True, "confidence": 1,
                                   "claims": [row]}), candidate, snapshot)


def _final_fixture_claims(reply, context):
    # This replay mocks composition with a neutral catalog overview. Supply its
    # structured interpretation explicitly; do not call the live verifier here.
    import json
    from modules.ai.brain.postprocess.catalog_semantic_claims import parse_claims

    return parse_claims(json.dumps({"complete": True, "confidence": 1, "claims": [{
        "scope": "catalog", "product_id": None, "attribute": "exists",
        "value": True, "quote": reply,
    }]}), reply, context["catalog_presentation_facts"])


def test_real_orchestration_three_turn_replay_reaches_whatsapp_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from modules.ai.brain.pipeline import get_brain  # noqa: PLC0415
    from routers import whatsapp_webhook as wh  # noqa: PLC0415

    monkeypatch.delenv("NAHLA_TEST_NO_DB", raising=False)
    monkeypatch.setenv("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE", "enforce")
    monkeypatch.setattr(
        "modules.ai.brain.postprocess.catalog_semantic_claims.classify_catalog_claims",
        _replay_fixture_claims,
    )
    db, _engine = make_scenario_db()
    tenant = seed_tenant(db, name="متجر تجريبي عام")
    customer = seed_customer(db, tenant.id, phone=DEFAULT_PHONE_E164, name="نورة عبدالله")
    convo = seed_conversation(
        db,
        tenant.id,
        customer_id=customer.id,
        status="active",
    )
    _seed_catalog(db, tenant.id)
    brain = get_brain()
    messages = (
        "وش منتجاتكم؟",
        "ابي الجاكيتات",
        "ابي رابط الجاكيت",
    )
    history: list[dict] = []
    captured_wire_payloads: list[dict] = []
    state_snapshots: list[dict] = []

    async def compose(
        _composer: FactBoundPersonaComposer,
        bundle,
    ) -> PersonaComposeResult:
        if bundle.inbound_text == messages[1]:
            return PersonaComposeResult(
                text="",
                source="fallback_deterministic",
                surface=bundle.surface,
                facts_hash="integration",
                guard_passed=False,
                guard_failed_reason="invented_offer",
                fallback_reason="invented_offer",
                language=bundle.language,
                dialect=bundle.dialect,
            )
        text = (
            "عندنا فستان كاجوال وفستان سهرة وجاكيت."
            if bundle.inbound_text == messages[0]
            else "هذا رابط جاكيت."
        )
        return PersonaComposeResult(
            text=text,
            source="persona_llm",
            surface=bundle.surface,
            facts_hash="integration",
            guard_passed=True,
            language=bundle.language,
            dialect=bundle.dialect,
        )

    async def fake_post(_phone_id: str, payload: dict, **_kwargs) -> bool:
        captured_wire_payloads.append(payload)
        return True

    async def run_replay():
        outcomes = []
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
                "core.ai_disabled_gate.is_ai_disabled_for_conversation",
                return_value=SimpleNamespace(disabled=False, reason=None),
            )
        )
        stack.enter_context(
            patch("core.store_knowledge.build_merchant_context", return_value={})
        )
        stack.enter_context(
            patch.object(
                brain._facts_loader,
                "load",
                side_effect=lambda *_args, **_kwargs: _commerce_facts(_catalog_rows()),
            )
        )
        stack.enter_context(
            patch(
                "modules.ai.brain.intent.classifier._slot_mod.extract_slots",
                new=AsyncMock(return_value={}),
            )
        )
        stack.enter_context(
            patch.object(FactBoundPersonaComposer, "compose", new=compose)
        )
        stack.enter_context(
            patch(
                "modules.ai.orchestrator.adapter.generate_ai_reply",
                return_value=AIReplyPayload(
                    reply_text="استعرض المنتجات المتاحة.",
                    provider_used="test",
                ),
            )
        )
        with stack:
            for message in messages:
                outcome = await evaluate_live_merchant_brain_turn(
                    db=db,
                    tenant_id=tenant.id,
                    phone_id="PH_SCENARIO",
                    turn_input=LiveMerchantBrainTurnInput(
                        customer_phone=DEFAULT_PHONE_E164,
                        text=message,
                        conversation_id=convo.id,
                        history=list(history),
                        preconditions=LiveMerchantBrainPreconditions(),
                        profile={
                            "id": customer.id,
                            "name": customer.name,
                            "preferred_language": "ar",
                        },
                    ),
                    convo=convo,
                    trace=_trace(),
                    persona_ownership=MagicMock(),
                    brain_factory=lambda: brain,
                    brain_active=True,
                )
                outcomes.append(outcome)
                state_snapshots.append(
                    brain._state_store.load(
                        db,
                        tenant.id,
                        DEFAULT_PHONE_E164,
                    ).to_dict()
                )
                history.extend(
                    (
                        {"direction": "inbound", "body": message},
                        {"direction": "outbound", "body": outcome.reply_text},
                    )
                )

            assert state_snapshots[0].get("last_presented_products"), state_snapshots[0]
            assert int(state_snapshots[0].get("selection_context_turn") or 0) > 0, (
                state_snapshots[0]
            )
            assert outcomes[1].brain_result["decision_args"].get("source") == (
                "selection_context_category_browse_pick"
            ), {
                "turn": outcomes[1].brain_result,
                "states": state_snapshots,
            }
            for outcome in outcomes[1:]:
                card = (outcome.brain_result.get("product_cards") or [None])[0]
                assert card is not None
                with patch.object(
                    wh,
                    "_post_wa",
                    new=AsyncMock(side_effect=fake_post),
                ):
                    sent = await wh._send_cta_url(
                        phone_id="PH_SCENARIO",
                        to=DEFAULT_PHONE_E164,
                        body_text=str(card.get("caption") or card.get("title") or ""),
                        btn_label="عرض المنتج",
                        btn_url=str(card.get("product_url") or ""),
                        header_image_url=str(card.get("file_url") or ""),
                    )
                assert sent is True
        return outcomes

    try:
        turn_1, turn_2, turn_3 = asyncio.run(run_replay())
        assert all(turn.status == "evaluated" for turn in (turn_1, turn_2, turn_3))

        data_2 = turn_2.brain_result
        assert data_2 is not None
        assert data_2["question_kind"] == "browse"
        assert int(data_2["eligible_product_count"]) > 0
        assert "لا توجد منتجات" not in (turn_2.reply_text or "")
        assert data_2["availability_claim_blocked"] is True
        assert "product_availability_truth_guard" in data_2[
            "final_transform_reasons"
        ]
        assert "product_availability_truth_guard" in data_2[
            "quality_observability"
        ]["guards_triggered"]
        cards_2 = data_2.get("product_cards") or []
        assert len(cards_2) == 1
        assert int(cards_2[0].get("id") or 0) == 28
        assert cards_2[0].get("file_url") == JACKET_IMAGE_URL
        assert cards_2[0].get("product_url") == JACKET_PRODUCT_URL
        final_availability_context = build_availability_context(
            db,
            tenant.id,
            result_data={
                **data_2,
                "pending_candidates": list(
                    data_2["decision_args"].get("products") or []
                ),
                "pending_product_card_count": len(cards_2),
            },
        )
        final_guard_pass = apply_product_availability_truth_guard(
            reply=turn_2.reply_text,
            semantic_claims=_final_fixture_claims(turn_2.reply_text, final_availability_context),
            availability_context=final_availability_context,
            inbound_text=messages[1],
            chosen_path=str(data_2.get("chosen_path") or ""),
            question_kind=str(data_2.get("question_kind") or ""),
            surface=str((data_2.get("persona_compose") or {}).get("surface") or ""),
        )
        assert final_guard_pass.action == "allowed_structured_catalog_browse"
        assert final_guard_pass.replaced is False
        assert final_guard_pass.reply == turn_2.reply_text
        assert [int(state.get("turn") or 0) for state in state_snapshots] == [1, 2, 3]
        assert int(
            (state_snapshots[1].get("current_product_focus") or {}).get("id") or 0
        ) == 28
        assert (
            (state_snapshots[1].get("current_product_focus") or {}).get("product_url")
            == JACKET_PRODUCT_URL
        )

        persisted_after_replay = brain._state_store.load(
            db,
            tenant.id,
            DEFAULT_PHONE_E164,
        )
        assert int(persisted_after_replay.turn) == 3
        assert int(persisted_after_replay.current_product_focus.get("id") or 0) == 28
        assert (
            persisted_after_replay.current_product_focus.get("product_url")
            == JACKET_PRODUCT_URL
        )

        data_3 = turn_3.brain_result
        assert data_3 is not None
        assert data_3["decision_args"]["source"] == "selection_context_named_product_link"
        assert data_3["decision_args"]["presentation_identity_grounded"] is True
        cards_3 = data_3.get("product_cards") or []
        assert len(cards_3) == 1
        assert int(cards_3[0].get("id") or 0) == 28
        assert cards_3[0].get("product_url") == JACKET_PRODUCT_URL
        assert cards_3[0].get("product_url") != STOREFRONT_URL

        safety_net = apply_store_link_safety_net(
            db=db,
            tenant_id=tenant.id,
            customer_msg=messages[2],
            reply_text=turn_3.reply_text,
        )
        assert safety_net.fired is False
        assert safety_net.rewrote_reply is False

        assert len(captured_wire_payloads) == 2
        for payload in captured_wire_payloads:
            interactive = payload["interactive"]
            assert (
                interactive["action"]["parameters"]["url"]
                == JACKET_PRODUCT_URL
            )
            assert interactive["header"]["image"]["link"] == JACKET_IMAGE_URL
    finally:
        db.close()


def test_layer2_webhook_three_turn_replay_reaches_provider_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the actual merchant webhook, state store, and outbound dispatcher."""
    from modules.ai.brain.pipeline import get_brain  # noqa: PLC0415

    monkeypatch.delenv("NAHLA_TEST_NO_DB", raising=False)
    monkeypatch.setenv("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE", "enforce")
    monkeypatch.setattr(
        "modules.ai.brain.postprocess.catalog_semantic_claims.classify_catalog_claims",
        _replay_fixture_claims,
    )
    monkeypatch.setenv("ORDER_FLOW_V2_ENABLED", "false")
    monkeypatch.setenv("ORDER_FLOW_V2_SHADOW_ENABLED", "true")
    reset_cache()

    db, _engine = make_scenario_db()
    tenant = seed_tenant(db, name="متجر تجريبي عام")
    customer = seed_customer(
        db,
        tenant.id,
        phone=DEFAULT_PHONE_E164,
        name="نورة عبدالله",
    )
    convo = seed_conversation(
        db,
        tenant.id,
        customer_id=customer.id,
        status="active",
    )
    _seed_catalog(db, tenant.id)
    world = ScenarioWorld(
        db=db,
        tenant=tenant,
        customer=customer,
        conversation=convo,
        phone=DEFAULT_PHONE_E164.lstrip("+"),
        phone_e164=DEFAULT_PHONE_E164,
    )
    runner = Layer2BrainRunner(world, phone_id="PH_PR975_LAYER2")
    brain = get_brain()
    messages = (
        "وش منتجاتكم؟",
        "ابي الجاكيتات",
        "ابي رابط الجاكيت",
    )
    store_link_results: list[tuple[str, object]] = []

    async def compose(
        _composer: FactBoundPersonaComposer,
        bundle,
    ) -> PersonaComposeResult:
        if bundle.inbound_text == messages[1]:
            return PersonaComposeResult(
                text="",
                source="fallback_deterministic",
                surface=bundle.surface,
                facts_hash="layer2",
                guard_passed=False,
                guard_failed_reason="invented_offer",
                fallback_reason="invented_offer",
                language=bundle.language,
                dialect=bundle.dialect,
            )
        text = (
            "عندنا فستان كاجوال وفستان سهرة وجاكيت."
            if bundle.inbound_text == messages[0]
            else "هذا رابط جاكيت."
        )
        return PersonaComposeResult(
            text=text,
            source="persona_llm",
            surface=bundle.surface,
            facts_hash="layer2",
            guard_passed=True,
            language=bundle.language,
            dialect=bundle.dialect,
        )

    def capture_store_link(*args, **kwargs):
        result = apply_store_link_safety_net(*args, **kwargs)
        store_link_results.append((str(kwargs.get("customer_msg") or ""), result))
        return result

    try:
        stack = ExitStack()
        stack.enter_context(
            patch(
                "core.store_knowledge.build_merchant_context",
                return_value={},
            )
        )
        stack.enter_context(
            patch.object(
                brain._facts_loader,
                "load",
                side_effect=lambda *_args, **_kwargs: _commerce_facts(
                    _catalog_rows()
                ),
            )
        )
        stack.enter_context(
            patch.object(FactBoundPersonaComposer, "compose", new=compose)
        )
        stack.enter_context(
            patch(
                "modules.ai.orchestrator.adapter.generate_ai_reply",
                return_value=AIReplyPayload(
                    reply_text="استعرض المنتجات المتاحة.",
                    provider_used="test",
                ),
            )
        )
        stack.enter_context(
            patch(
                "modules.ai.postprocess.safety_nets.apply_store_link_safety_net",
                new=capture_store_link,
            )
        )

        turns = []
        brain_results: list[dict] = []
        state_snapshots: list[dict] = []
        wire_by_turn: list[list] = []
        with stack:
            for index, message in enumerate(messages, start=1):
                outbound_before = len(runner.fake_sender.sent)
                turn = runner.run_turn(
                    message,
                    label=f"pr975-layer2-turn-{index}",
                    provider_msg_id=f"wamid.pr975.layer2.{index}",
                )
                turns.append(turn)
                brain_results.append(dict(runner._last_brain_result))
                state_snapshots.append(
                    brain._state_store.load(
                        db,
                        tenant.id,
                        world.phone,
                    ).to_dict()
                )
                wire_by_turn.append(
                    list(runner.fake_sender.sent[outbound_before:])
                )

        assert all(turn.brain_called for turn in turns), [
            turn.to_dict() for turn in turns
        ]
        assert not runner.errors, runner.errors
        assert [int(state.get("turn") or 0) for state in state_snapshots] == [
            1,
            2,
            3,
        ]
        assert [
            int((state.get("current_product_focus") or {}).get("id") or 0)
            for state in state_snapshots[1:]
        ] == [28, 28]

        data_2 = brain_results[1]
        assert data_2["question_kind"] == "browse"
        assert int(data_2["eligible_product_count"]) > 0
        assert "لا توجد منتجات" not in (turns[1].outbound_reply or "")
        assert data_2["availability_claim_blocked"] is True
        assert "product_availability_truth_guard" in data_2[
            "final_transform_reasons"
        ]
        assert "product_availability_truth_guard" in data_2[
            "quality_observability"
        ]["guards_triggered"]
        cards_2 = data_2.get("product_cards") or []
        assert len(cards_2) == 1
        assert int(cards_2[0].get("id") or 0) == 28
        assert cards_2[0].get("file_url") == JACKET_IMAGE_URL
        assert cards_2[0].get("product_url") == JACKET_PRODUCT_URL

        final_context = build_availability_context(
            db,
            tenant.id,
            result_data={
                **data_2,
                "pending_candidates": list(
                    data_2["decision_args"].get("products") or []
                ),
                "pending_product_card_count": len(cards_2),
            },
        )
        final_guard_pass = apply_product_availability_truth_guard(
            reply=turns[1].outbound_reply,
            semantic_claims=_final_fixture_claims(turns[1].outbound_reply, final_context),
            availability_context=final_context,
            inbound_text=messages[1],
            chosen_path=str(data_2.get("chosen_path") or ""),
            question_kind=str(data_2.get("question_kind") or ""),
            surface=str((data_2.get("persona_compose") or {}).get("surface") or ""),
        )
        assert final_guard_pass.action == "allowed_structured_catalog_browse"
        assert final_guard_pass.replaced is False
        assert final_guard_pass.reply == turns[1].outbound_reply

        data_3 = brain_results[2]
        assert (
            data_3["decision_args"]["source"]
            == "selection_context_named_product_link"
        )
        assert data_3["decision_args"]["presentation_identity_grounded"] is True
        cards_3 = data_3.get("product_cards") or []
        assert len(cards_3) == 1
        assert int(cards_3[0].get("id") or 0) == 28
        assert cards_3[0].get("product_url") == JACKET_PRODUCT_URL
        assert cards_3[0].get("product_url") != STOREFRONT_URL

        turn_3_store_link = [
            result
            for message, result in store_link_results
            if message == messages[2]
        ]
        assert len(turn_3_store_link) == 1
        assert turn_3_store_link[0].fired is False
        assert turn_3_store_link[0].rewrote_reply is False

        for records in wire_by_turn[1:]:
            cta_payloads = [
                record.payload
                for record in records
                if (record.payload.get("interactive") or {}).get("type")
                == "cta_url"
            ]
            assert len(cta_payloads) == 1, [record.payload for record in records]
            interactive = cta_payloads[0]["interactive"]
            assert (
                interactive["action"]["parameters"]["url"]
                == JACKET_PRODUCT_URL
            )
            assert interactive["header"]["image"]["link"] == JACKET_IMAGE_URL
        assert all(
            record.path
            in {"provider_post_with_context", "provider_send_message"}
            for records in wire_by_turn[1:]
            for record in records
        )
        assert runner.fake_sender.real_send_attempted is True
    finally:
        reset_cache()
        db.close()
