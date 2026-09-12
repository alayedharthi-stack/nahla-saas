"""Guard decisions preserve model wording and distinguish missing verification.

Extraction responses are fixtures: these tests prove the ownership contract,
not live model comprehension.
"""
import asyncio
import json
from unittest.mock import patch

import pytest

from modules.ai.brain.postprocess import catalog_semantic_claims as semantic
from modules.ai.brain.postprocess.product_availability_truth_guard import (
    apply_product_availability_truth_guard,
    apply_product_availability_truth_guard_async,
    stamp_product_availability_guard_transform,
)
from modules.ai.compose.reply_metadata_export import extract_reply_metadata_export
from test_catalog_semantic_contracts import facts, claim
from test_merchant_brain_turn import _run


def extracted(text, rows, snapshot=None):
    return semantic.parse_claims(json.dumps({
        "complete": True, "confidence": 1, "claims": rows,
    }), text, snapshot or facts())


def guard(text, claims=None, snapshot=None, **kwargs):
    return apply_product_availability_truth_guard(
        reply=text, question_kind="browse", semantic_claims=claims,
        availability_context={"catalog_presentation_facts": snapshot or facts()},
        **kwargs,
    )


@pytest.mark.parametrize("text,pid", [
    ("متوفر لدينا جاكيت 👍", 28),
    ("الجاكيت، نعم موجود عندنا 🌷", 28),
    ("تلقين عندنا حذاء رياضي أبيض؛ تحبين تشوفينه؟", 501),
    ("  Our white sports shoe is in stock.\nWould you like a photo?  ", 501),
])
def test_verified_wording_is_preserved_exactly(monkeypatch, text, pid):
    monkeypatch.setenv("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE", "enforce")
    result = guard(text, extracted(text, [claim(pid, "available", True, text)]))
    assert result.reply == text
    assert not result.replaced
    assert not result.requires_grounded_recompose
    assert not result.availability_claim_blocked


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
@pytest.mark.parametrize("failure", ["missing", "invalid", "stale", "unknown_stock"])
def test_unresolved_is_not_a_contradiction_or_recompose_request(monkeypatch, mode, failure):
    monkeypatch.setenv("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE", mode)
    text = "متوفر لدينا جاكيت"
    snapshot = facts()
    parsed = None
    if failure == "invalid":
        parsed = semantic.parse_claims("not-json", text, snapshot)
    elif failure == "stale":
        parsed = extracted("different candidate", [])
    elif failure == "unknown_stock":
        snapshot = {**snapshot, "eligible_catalog_products": [
            {k: v for k, v in row.items() if k not in {"in_stock", "available"}}
            for row in snapshot["eligible_catalog_products"]
        ]}
        parsed = extracted(text, [claim(28, "available", True, text)], snapshot)
    result = guard(text, parsed, snapshot)
    assert result.reason == "browse_semantic_verification_unresolved"
    assert result.action == "hold_unverified_text"
    assert result.verification_unresolved is True
    assert not result.availability_claim_blocked
    assert not result.requires_grounded_recompose
    assert result.evidence is None
    assert result.reply == (text if mode == "shadow" else "")
    assert result.replaced is (mode == "enforce")


def test_verifier_timeout_never_invokes_correction(monkeypatch):
    monkeypatch.setenv("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE", "enforce")
    async def timeout(*args, **kwargs):
        raise TimeoutError()
    monkeypatch.setattr(semantic, "classify_catalog_claims", timeout)
    result = asyncio.run(apply_product_availability_truth_guard_async(
        reply="متوفر لدينا جاكيت", question_kind="browse",
        availability_context={"catalog_presentation_facts": facts()},
    ))
    assert result.verification_unresolved
    assert not result.requires_grounded_recompose


def test_proven_price_contradiction_requests_one_model_correction(monkeypatch):
    monkeypatch.setenv("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE", "enforce")
    text = "سعر الجاكيت 999 ريال"
    parsed = extracted(text, [claim(28, "price", "999", text)])
    first = guard(text, parsed)
    assert first.reply == text
    assert first.requires_grounded_recompose
    assert first.availability_claim_blocked
    second = guard(text, parsed, allow_recompose=False)
    assert second.reply == ""
    assert not second.requires_grounded_recompose
    data = {}
    stamp_product_availability_guard_transform(data, second)
    assert data["catalog_reply_withheld"] is True
    assert extract_reply_metadata_export(data)["catalog_reply_withheld"] is True


@pytest.mark.parametrize("cards", [[], [{"id": 28, "title": "جاكيت",
    "file_url": "https://cdn.example/jacket.jpg",
    "product_url": "https://store.example/jacket"}]])
def test_withheld_text_does_not_enter_canned_silent_recovery(cards):
    with patch("services.merchant_brain_turn._empty_reply_fallback") as fallback, patch(
        "modules.ai.brain.postprocess.conversation_recovery.try_guard_recovery_reply",
    ) as recovery:
        result, *_ = _run({"reply": "", "catalog_reply_withheld": True,
            "product_cards": cards, "availability_guard_reason":
            "browse_semantic_verification_unresolved"})
    assert result.reply_text == ""
    assert result.brain_product_cards == cards
    assert not result.brain_silent
    fallback.assert_not_called()
    recovery.assert_not_called()


@pytest.mark.parametrize("scenario", ["verified", "unresolved", "corrected", "still_wrong"])
def test_guard_through_real_webhook_and_wire(monkeypatch, scenario):
    from unittest.mock import AsyncMock
    from core.inbound_dedup import reset_cache
    from modules.ai.brain.pipeline import get_brain
    from modules.ai.brain.persona.fact_bound_composer import FactBoundPersonaComposer
    from modules.ai.brain.persona.facts_bundle import PersonaComposeResult
    from modules.ai.orchestrator.types import AIReplyPayload
    from tests.commerce_scenario_fixtures import (
        ScenarioWorld, make_scenario_db,
        seed_conversation, seed_customer, seed_tenant,
    )
    from tests.salla_acceptance.layer2_harness import Layer2BrainRunner
    # Import before provider patching: the webhook binds provider_send_message
    # at module load, otherwise later cases send into the first fake recorder.
    from routers import whatsapp_webhook  # noqa: F401
    from test_pr975_production_orchestration_replay import _seed_catalog, _catalog_rows, _commerce_facts

    monkeypatch.delenv("NAHLA_TEST_NO_DB", raising=False)
    monkeypatch.setenv("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE", "enforce")
    monkeypatch.setenv("ORDER_FLOW_V2_ENABLED", "false")
    monkeypatch.setenv("ORDER_FLOW_V2_SHADOW_ENABLED", "true")
    reset_cache()
    db, _engine = make_scenario_db()
    tenant = seed_tenant(db, name="متجر تجريبي عام")
    phone_e164 = "+96650000001" + str(["verified", "unresolved", "corrected", "still_wrong"].index(scenario))
    customer = seed_customer(db, tenant.id, phone=phone_e164, name="نورة عبدالله")
    convo = seed_conversation(db, tenant.id, customer_id=customer.id, status="active")
    _seed_catalog(db, tenant.id)
    world = ScenarioWorld(db=db, tenant=tenant, customer=customer, conversation=convo,
        phone=phone_e164.lstrip("+"), phone_e164=phone_e164)
    runner = Layer2BrainRunner(world, phone_id="PH_GUARD_OWNERSHIP")
    brain = get_brain()
    candidates = []
    verifications = []
    correct = "متوفر لدينا جاكيت 👍"
    wrong = "سعر الجاكيت 999 ريال"

    async def compose(_composer, bundle):
        candidate = correct
        if scenario == "still_wrong" or (scenario == "corrected" and not candidates):
            candidate = wrong
        candidates.append(candidate)
        return PersonaComposeResult(text=candidate, source="persona_llm",
            surface=bundle.surface, facts_hash="fixture", guard_passed=True)

    async def verify(candidate, snapshot, **kwargs):
        verifications.append(candidate)
        if scenario == "unresolved":
            return semantic.parse_claims("not-json", candidate, snapshot)
        rows = [claim(28, "price", "999", wrong)] if candidate == wrong else [
            claim(28, "available", True, candidate)]
        return extracted(candidate, rows, snapshot)

    monkeypatch.setattr(FactBoundPersonaComposer, "compose", compose)
    monkeypatch.setattr(semantic, "classify_catalog_claims", verify)
    monkeypatch.setattr(brain._facts_loader, "load", lambda *a, **kw: _commerce_facts(_catalog_rows()))
    monkeypatch.setattr("core.store_knowledge.build_merchant_context", lambda *a, **kw: {})
    monkeypatch.setattr("modules.ai.brain.intent.classifier._slot_mod.extract_slots", AsyncMock(return_value={}))
    from modules.ai.brain.commerce import catalog_request_interpreter as requests
    async def interpret(ctx):
        snapshot = requests.catalog_request_snapshot(ctx)
        assert snapshot is not None
        return requests.parse_catalog_request(json.dumps({
            "capability": "search", "product_ids": [28], "query": "جاكيت",
            "reference": "named", "confidence": 1,
        }), snapshot)
    monkeypatch.setattr(requests, "interpret_catalog_request", interpret)
    monkeypatch.setattr("modules.ai.orchestrator.adapter.generate_ai_reply", lambda *a, **kw:
        AIReplyPayload(reply_text="unexpected generic fallback", provider_used="test"))
    try:
        with patch("modules.ai.brain.postprocess.conversation_recovery.try_guard_recovery_reply") as recovery:
            turn = runner.run_turn("وش عندكم من الجاكيتات؟", label="guard-ownership", provider_msg_id="wamid.guard." + scenario)
        assert turn.brain_called
        assert not runner.errors, runner.errors
        data = runner._last_brain_result
        assert len(candidates) == (2 if scenario in {"corrected", "still_wrong"} else 1), data
        assert len(verifications) == len(candidates)
        wire = [record.payload for record in runner.fake_sender.sent]
        text_bodies = [p["text"]["body"] for p in wire if p.get("type") == "text"]
        cards = [p for p in wire if (p.get("interactive") or {}).get("type") == "cta_url"]
        assert cards, wire
        if scenario in {"unresolved", "still_wrong"}:
            assert data["catalog_reply_withheld"] is True
            assert not text_bodies, text_bodies
        else:
            assert data["reply"] == correct
            assert correct in text_bodies, wire
        recovery.assert_not_called()
    finally:
        reset_cache()
        db.close()
        _engine.dispose()
