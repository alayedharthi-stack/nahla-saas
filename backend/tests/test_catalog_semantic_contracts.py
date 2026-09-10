"""Model-boundary fixtures prove contracts and delivery, not live comprehension."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from modules.ai.brain.postprocess import catalog_semantic_claims as claims
from modules.ai.brain.postprocess.product_availability_truth_guard import (
    apply_product_availability_truth_guard_async,
)
from modules.ai.brain.persona.catalog_product_answer import build_catalog_product_answer_facts_bundle
from modules.ai.brain.persona.fact_bound_composer import FactBoundPersonaComposer
from modules.ai.brain.commerce import catalog_request_interpreter as requests
from modules.ai.brain.types import BrainContext, CommerceFacts, Intent, MerchantConversationState

SHOE = {"id": 501, "external_id": "sku501", "title": "حذاء رياضي أبيض", "price": "249.50", "in_stock": True,
        "can_checkout": True, "orderable": True,
        "product_url": "https://store.example/shoe/p501", "image_url": "https://store.example/shoe.jpg"}
JACKET = {**SHOE, "id": 28, "external_id": "sku28", "title": "جاكيت", "price": "199",
          "product_url": "https://store.example/jacket/p28"}


def facts():
    return {"question_kind": "browse", "has_eligible_products": True,
            "eligible_product_count": 2, "pending_product_card_count_present": True,
            "catalog_products": [SHOE, JACKET], "eligible_catalog_products": [SHOE, JACKET]}


def claim(pid, attribute, value, quote, scope="product"):
    return {"scope": scope, "product_id": pid, "attribute": attribute, "value": value, "quote": quote}


def extracted(candidate, rows):
    return claims.parse_claims(json.dumps({"complete": True, "confidence": 1, "claims": rows}),
                               candidate, facts())


@pytest.mark.parametrize("candidate,rows,conflict", [
    ("متوفر حذاء رياضي أبيض وعطر ورد.",
     [claim(501, "available", True, "متوفر حذاء رياضي أبيض"),
      claim(None, "available", True, "متوفر حذاء رياضي أبيض وعطر ورد")], True),
    ("حذاء رياضي أبيض متوفر والعطر غير متوفر.",
     [claim(501, "available", True, "حذاء رياضي أبيض متوفر"),
      claim(None, "available", False, "العطر غير متوفر")], True),
    ("متوفر حذاء رياضي أبيض وعطر ورد سعره 249.50 ريال.",
     [claim(501, "available", True, "متوفر حذاء رياضي أبيض"),
      claim(None, "price", "249.50", "عطر ورد سعره 249.50 ريال")], True),
    ("متوفر حذاء رياضي أبيض وجاكيت.",
     [claim(501, "available", True, "متوفر حذاء رياضي أبيض"),
      claim(28, "available", True, "متوفر حذاء رياضي أبيض وجاكيت")], False),
    ("حذاء رياضي أبيض سعره 249.50 ريال، تقدر تشوف صورته.",
     [claim(501, "price", "249.50", "حذاء رياضي أبيض سعره 249.50 ريال")], False),
])
def test_each_claim_has_its_own_identity_and_price(candidate, rows, conflict):
    verdict = claims.contradiction_reason(candidate, facts(), extracted(candidate, rows))
    assert bool(verdict) is conflict
    if "العطر غير" in candidate:
        assert verdict != "browse_false_negative_vs_eligible_products"


@pytest.mark.parametrize("mutation", [
    {"confidence": True}, {"confidence": float("nan")}, {"complete": False},
    {"claims": [{}]}, {"claims": [{"scope": [], "product_id": 501, "attribute": "exists",
                                  "value": True, "quote": "x"}]},
    {"claims": [claim(True, "exists", True, "x")]},
    {"claims": [claim(501, "price", "NaN", "x")]},
    {"claims": [claim(501, "available", "true", "x")]},
    {"claims": [claim(501, "available", True, "absent quote")]},
])
def test_invalid_model_output_is_not_evidence(mutation):
    raw = {"complete": True, "confidence": 1, "claims": []}
    raw.update(mutation)
    assert claims.parse_claims(json.dumps(raw), "x", facts()).status == "invalid"


def test_changed_catalog_cannot_reuse_verdict():
    text = "حذاء رياضي أبيض سعره 249.50 ريال"
    parsed = extracted(text, [claim(501, "price", "249.50", text)])
    changed = {**facts(), "eligible_catalog_products": [{**SHOE, "price": "300"}]}
    assert claims.contradiction_reason(text, changed, parsed) == "browse_semantic_verification_unresolved"


def test_async_verifier_exception_fails_closed_and_off_never_calls(monkeypatch):
    calls = []

    async def broken(*args, **kwargs):
        calls.append(1)
        raise TimeoutError()

    monkeypatch.setattr(claims, "classify_catalog_claims", broken)
    for mode in ("off", "shadow", "enforce"):
        monkeypatch.setenv("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE", mode)
        before = len(calls)
        result = asyncio.run(apply_product_availability_truth_guard_async(
            reply="candidate", availability_context={"catalog_presentation_facts": facts()},
            question_kind="browse", allow_recompose=False,
        ))
        assert len(calls) - before == (0 if mode == "off" else 1)
        assert result.reply == ("" if mode == "enforce" else "candidate")


def test_correction_and_pdp_reach_real_persona_provider_boundary(monkeypatch):
    import modules.ai.brain.persona.fact_bound_composer as module
    captured = []

    def provider(**kwargs):
        captured.append(kwargs)
        return "natural reply", "test-model"

    monkeypatch.setattr(module, "call_persona_compose_provider_sync", provider)
    bundle = build_catalog_product_answer_facts_bundle(
        inbound_text="أرسل رابطه", tenant_id=1, products=[JACKET], question_kind="browse",
        decision_args={"availability_guard_correction": {
            "reason": "browse_false_negative_vs_eligible_products",
            "eligible_catalog_products": [{"id": 999, "title": "INJECTED_ROW"}],
            "instructions": "INJECTED_INSTRUCTIONS",
        }},
    )
    route = SimpleNamespace(model="test-model", tier="tiny")
    asyncio.run(FactBoundPersonaComposer()._invoke_provider_callable(bundle, SimpleNamespace(route=route)))
    wire = captured[0]
    assert "catalog_correction:" in wire["user"]
    assert "browse_false_negative_vs_eligible_products" in wire["user"]
    assert JACKET["product_url"] in wire["user"]
    assert JACKET["product_url"] not in wire["system"]
    assert "INJECTED" not in wire["user"] + wire["system"]
    assert bundle.verified_facts["question_kind"] == "browse"


def context(message="أرسل رابطه", **state):
    return BrainContext(tenant_id=1, customer_phone="test", message=message,
        intent=Intent(name="ask_product", confidence=0.95),
        state=MerchantConversationState(stage="exploring", turn=3, product_focus_turn=2,
            current_product_focus=JACKET, **state),
        facts=CommerceFacts(has_products=True, snapshot_fresh=True,
                            discovery_products=[SHOE, JACKET]))


def request_payload(capability="link", ids=None, reference="current"):
    return {"capability": capability, "product_ids": [28] if ids is None else ids,
            "query": "", "reference": reference, "confidence": 0.98}


@pytest.mark.parametrize("message,capability", [("أرسل رابطه", "link"), ("تقدر تشوف صورته؟", "image")])
def test_model_selects_capability_code_resolves_id_and_pdp(monkeypatch, message, capability):
    from modules.ai.orchestrator.providers import registry
    captured = []

    class Provider:
        def is_configured(self):
            return True

        def call(self, user, system, **kwargs):
            captured.append(json.loads(user))
            return {"reply_text": json.dumps(request_payload(capability))}

    monkeypatch.setattr(registry, "get_provider", lambda name: Provider())
    ctx = context(message)
    ctx.catalog_request = asyncio.run(requests.interpret_catalog_request(ctx))
    decision = requests.catalog_request_decision(ctx)
    assert captured[0]["latest_customer_turn"] == message
    assert captured[0]["current_product_id"] == 28
    assert decision.action == "search_products"
    assert decision.args["products"][0]["product_url"] == JACKET["product_url"]
    assert decision.args["presentation_identity_grounded"] is True


@pytest.mark.parametrize("capability", ["conversation", "clarify", "variant_question"])
def test_social_ambiguity_and_variant_questions_do_not_start_order_or_search(capability):
    ctx = context()
    payload = request_payload(capability, [28] if capability == "variant_question" else [],
                              "current" if capability == "variant_question" else "none")
    ctx.catalog_request = requests.parse_catalog_request(json.dumps(payload), requests.catalog_request_snapshot(ctx))
    decision = requests.catalog_request_decision(ctx)
    assert decision.action == "llm_reply"
    assert "variant_id" not in decision.args


def test_identity_scope_staleness_and_operational_gates():
    assert requests.catalog_request_snapshot(context("https://example.com/video")) is None
    ctx = context()
    ctx.facts.discovery_products.append({**JACKET, "id": 999, "tenant_id": 2})
    snapshot = requests.catalog_request_snapshot(ctx)
    assert 999 not in snapshot["rows"]
    forged = requests.parse_catalog_request(json.dumps(request_payload(ids=[999])), snapshot)
    assert forged.capability == "clarify"
    ctx.state.turn = 100
    stale = requests.catalog_request_snapshot(ctx)
    assert stale["current_product_id"] is None
    assert requests.parse_catalog_request(json.dumps(request_payload()), stale).capability == "clarify"
    for state in ("ordering", "payment_pending"):
        ctx.state.stage = state
        assert requests.catalog_request_snapshot(ctx) is None
    ctx.state.stage = "exploring"
    ctx.human_priority = True
    assert requests.catalog_request_snapshot(ctx) is None


def test_multiple_products_are_preserved():
    ctx = context()
    payload = request_payload("details", [501, 28], "multiple")
    ctx.catalog_request = requests.parse_catalog_request(json.dumps(payload), requests.catalog_request_snapshot(ctx))
    assert [p["id"] for p in requests.catalog_request_decision(ctx).args["products"]] == [501, 28]


def test_variant_uncertainty_reaches_user_role_without_authoring():
    ctx = context("فيه أسود؟")
    ctx.catalog_request = requests.parse_catalog_request(
        json.dumps(request_payload("variant_question")), requests.catalog_request_snapshot(ctx),
    )
    history = [{"role": "user", "content": ctx.message}]
    message, messages = requests.bind_catalog_request_data(ctx, ctx.message, history)
    assert messages[-1]["role"] == "user"
    assert messages[-1]["content"] == message
    data = json.loads(message.split("\n\n", 1)[1])
    assert data["variant_availability_confirmed"] is False
    assert data["write_action_authorized"] is False
    assert data["products"][0]["product_id"] == 28
    assert history[-1]["content"] == ctx.message


def test_semantic_diagnostics_export_without_candidate_or_prompt():
    from modules.ai.compose.reply_metadata_export import extract_reply_metadata_export

    result = extract_reply_metadata_export({
        "catalog_claim_verification_status": "ok", "availability_guard_recompose_count": 1,
        "catalog_capability": "link", "untrusted_candidate": "private candidate",
        "availability_guard_correction": {"reason": "internal"},
    })
    assert result["catalog_claim_verification_status"] == "ok"
    assert result["availability_guard_recompose_count"] == 1
    assert "untrusted_candidate" not in result
    assert "availability_guard_correction" not in result


def test_actual_decision_engine_uses_semantic_request_before_ce2():
    from modules.ai.brain.decision.engine import DefaultDecisionEngine

    ctx = context("ابي رابط الجاكيت")
    ctx.catalog_request = requests.parse_catalog_request(
        json.dumps(request_payload()), requests.catalog_request_snapshot(ctx),
    )
    decision = DefaultDecisionEngine().decide(ctx)
    assert decision.args["source"] == "catalog_semantic_request"
    assert decision.args["products"][0]["product_url"] == JACKET["product_url"]
    from modules.ai.brain.execution.search import ProductSearchHandler
    from modules.ai.brain.commerce.product_presentation_selection import (
        apply_search_product_presentation, presentation_context_from_brain,
    )
    from routers.whatsapp_webhook import build_cta_url_payload

    result = asyncio.run(ProductSearchHandler().handle(decision, ctx))
    apply_search_product_presentation(result.data, candidates=result.data["products"],
                                     **presentation_context_from_brain(ctx, decision))
    card = result.data["pending_product_cards"][0]
    payload = build_cta_url_payload(to="966500000001", body_text=card["title"],
        btn_label="View", btn_url=card["product_url"], header_image_url=card["file_url"])
    assert payload["interactive"]["action"]["parameters"]["url"] == JACKET["product_url"]
    assert payload["interactive"]["header"]["image"]["link"] == JACKET["image_url"]
