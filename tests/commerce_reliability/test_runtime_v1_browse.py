"""Runtime browse tests — real V1 decision engine, executor, composer and catalog search.

Models are scripted; the catalog lives in an in-memory copy of the schema.
Recorded baseline defects raise their exact manifest marker.
"""
from __future__ import annotations

import asyncio
import json

from tests.commerce_reliability import runtime_support as rs

rs.ensure_sys_path()

from modules.ai.brain.decision.actions import ACTION_SEARCH_PRODUCTS  # noqa: E402
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.types import Intent, MerchantConversationState, OrderPreparationState  # noqa: E402

SKIRT_SINGULAR = "تنورة"
SKIRT_BROKEN_PLURAL = "تنانير"
DISCOVERY_REQUEST = "عندكم تنانير؟"  # deterministic ask_product phrasing (rules.match)


def _builder(fixture):
    from core.store_knowledge import CatalogContextBuilder  # noqa: PLC0415

    return CatalogContextBuilder(fixture.db, fixture.tenant_a)


# ── Catalog search (sqlite copy of the schema) ──────────────────────────────


def test_singular_query_and_tenant_isolation_hold(catalog_sqlite) -> None:
    import models as M  # noqa: PLC0415

    rows = _builder(catalog_sqlite).search_products(SKIRT_SINGULAR, limit=10)
    ids = {int(r["id"]) for r in rows}
    assert catalog_sqlite.product_ids["p-14"] in ids, rows
    assert catalog_sqlite.product_ids["x-1"] not in ids, "tenant B row leaked into tenant A search"
    owners = {int(catalog_sqlite.db.get(M.Product, pid).tenant_id) for pid in ids}
    assert owners == {catalog_sqlite.tenant_a}


def test_broken_plural_query_finds_singular_product(catalog_sqlite, baseline) -> None:
    rows = _builder(catalog_sqlite).search_products(SKIRT_BROKEN_PLURAL, limit=10)
    if not rows:
        baseline.defect("RB-03", "broken_plural_query_returns_no_rows", query=SKIRT_BROKEN_PLURAL)
    assert catalog_sqlite.product_ids["p-14"] in {int(r["id"]) for r in rows}, rows


# ── List pick → executor → composer ─────────────────────────────────────────


def _list_pick_chain():
    state = MerchantConversationState(
        greeted=True, stage="browsing", turn=3, last_search_candidates=rs.shown_candidates(),
    )
    ctx = rs.brain_context(
        "1", state=state,
        profile={"inbound_metadata": {"button_id": "pick_1", "button_provenance": "pick_1"}},
    )
    decision = DefaultDecisionEngine().decide(ctx)
    from modules.ai.brain.execution.search import ProductSearchHandler  # noqa: PLC0415

    result = asyncio.run(ProductSearchHandler().handle(decision, ctx))
    return ctx, decision, result


def test_list_pick_selects_the_right_candidate_before_compose() -> None:
    ctx, decision, result = _list_pick_chain()
    assert decision.action == ACTION_SEARCH_PRODUCTS, (decision.action, decision.reason)
    assert (decision.args.get("selected_product") or {}).get("id") == 11, decision.args
    assert result.success, result.data
    assert (result.data.get("product") or {}).get("id") == 11, result.data


def test_list_pick_compose_keeps_selected_product(baseline) -> None:
    from modules.ai.brain.compose.responder import DefaultComposer  # noqa: PLC0415

    ctx, decision, result = _list_pick_chain()
    scripted_reply = json.dumps({"reply": "فستان سهرة أزرق متوفر بسعر 250 ريال، تبغى تطلبه؟"}, ensure_ascii=False)
    with rs.scripted_model(scripted_reply):
        text = asyncio.run(DefaultComposer().compose(decision, result, ctx))
    if text in rs.no_products_templates():
        baseline.defect(
            "RB-04", "list_pick_compose_emits_no_products_template",
            selected_id=(result.data.get("product") or {}).get("id"),
            products_in_result=len(result.data.get("products") or []),
        )
    assert "فستان سهرة أزرق" in text, text


# ── "More products" after a shown list (interpreter browse) ─────────────────


def test_more_products_request_does_not_repeat_shown_candidates(baseline) -> None:
    from modules.ai.brain.commerce.catalog_request_interpreter import (  # noqa: PLC0415
        catalog_request_decision,
        interpret_catalog_request,
    )

    shown = rs.shown_candidates()
    state = MerchantConversationState(
        greeted=True, stage="browsing", turn=4, last_search_candidates=shown, last_browse_query="",
        catalog_browse_pool=rs.catalog_pool(), catalog_browse_offset=3,
    )
    message = "وش غيرها؟"
    ctx = rs.brain_context(message, intent=Intent(name="general", confidence=0.5, raw_message=message), state=state)
    scripted = json.dumps({"capability": "browse", "product_ids": [], "query": "", "reference": "none", "confidence": 0.95})
    with rs.scripted_model(scripted):
        request = asyncio.run(interpret_catalog_request(ctx))
    assert request is not None and getattr(request, "capability", None) == "browse", request
    ctx.catalog_request = request
    decision = catalog_request_decision(ctx)
    assert decision is not None, "interpreter produced no decision"
    offered = [int(p["id"]) for p in (decision.args.get("products") or [])]
    repeated = sorted(set(offered) & {int(p["id"]) for p in shown})
    if repeated:
        baseline.defect("RB-01", "interpreter_browse_repeats_shown_ids", offered=offered, repeated=repeated)
    assert offered and not repeated, (offered, repeated)


# ── Fulfillment lock vs. a new discovery request ────────────────────────────


def _locked_state() -> MerchantConversationState:
    return MerchantConversationState(
        greeted=True, stage="ordering", turn=12, product_focus_turn=11,
        current_product_focus={"id": 11, "title": "فستان سهرة أزرق", "external_id": "p-11", "price": "250"},
        order_prep=OrderPreparationState(product_id="p-11", missing_fields=["city", "delivery_address"]),
        last_search_candidates=rs.shown_candidates(),
    )


def test_unlocked_discovery_request_reaches_product_search() -> None:
    state = MerchantConversationState(
        greeted=True, stage="browsing", turn=3, last_search_candidates=rs.shown_candidates(),
    )
    ctx = rs.brain_context(DISCOVERY_REQUEST, state=state)
    assert ctx.intent.name == "ask_product", ctx.intent
    decision = DefaultDecisionEngine().decide(ctx)
    assert decision.action == ACTION_SEARCH_PRODUCTS, (decision.action, decision.reason)
    assert decision.args.get("query") == SKIRT_BROKEN_PLURAL, decision.args


def test_locked_fulfillment_session_still_allows_new_discovery(baseline) -> None:
    """Same request as the unlocked control above, inside a locked fulfillment session."""
    from modules.ai.brain.commerce.catalog_request_interpreter import (  # noqa: PLC0415
        interpret_catalog_request,
    )
    from modules.ai.brain.order_context_gate import (  # noqa: PLC0415
        is_fulfillment_session_locked,
        try_fulfillment_lock_continuation,
    )

    # Unlocked control, same request: must reach product search (see the test above).
    control_ctx = rs.brain_context(DISCOVERY_REQUEST, state=MerchantConversationState(
        greeted=True, stage="browsing", turn=3, last_search_candidates=rs.shown_candidates(),
    ))
    control_action = DefaultDecisionEngine().decide(control_ctx).action

    ctx = rs.brain_context(DISCOVERY_REQUEST, state=_locked_state())
    assert ctx.intent.name == "ask_product", ctx.intent
    locked = is_fulfillment_session_locked(ctx)
    lock_decision = try_fulfillment_lock_continuation(ctx)
    scripted = json.dumps({"capability": "search", "product_ids": [], "query": SKIRT_BROKEN_PLURAL,
                           "reference": "none", "confidence": 0.95})
    with rs.scripted_model(scripted) as model:
        ctx.catalog_request = asyncio.run(interpret_catalog_request(ctx))
    engine_decision = DefaultDecisionEngine().decide(ctx)
    # Only the exact recorded observation raises the allowance marker: locked
    # session, llm_reply with the recorded "active order" reason, interpreter
    # gated off, and the unlocked control reaching search. Anything else is a
    # changed cause and fails on its own.
    observation = rs.classify_lock_decision(
        locked=locked, action=engine_decision.action, reason=engine_decision.reason,
        interpreter_result=ctx.catalog_request, control_action=control_action,
    )
    assert not observation.startswith("changed_cause:"), (
        observation, engine_decision.action, engine_decision.reason, getattr(lock_decision, "action", None),
        bool(model.calls),
    )
    if observation == "recorded_defect":
        baseline.defect(
            "RB-05", "fulfillment_lock_blocks_discovery",
            engine_action=engine_decision.action, engine_reason=engine_decision.reason[:80],
            lock_action=getattr(lock_decision, "action", None), interpreter_ran=bool(model.calls),
        )
    assert engine_decision.action == ACTION_SEARCH_PRODUCTS, (engine_decision.action, engine_decision.reason)
