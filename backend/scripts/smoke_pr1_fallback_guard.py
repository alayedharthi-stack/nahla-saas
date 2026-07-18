#!/usr/bin/env python3
"""Post-deploy smoke: PR1 fallback guard scenarios."""
from __future__ import annotations

import argparse
import os
import sys

_backend = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _backend not in sys.path:
    sys.path.insert(0, _backend)

from modules.ai.brain.commerce.fallback_guard import (  # noqa: E402
    detect_semantic_dead_end,
    resolve_active_topic,
)
from modules.ai.brain.commerce.solution_seeking import (  # noqa: E402
    classify_solution_seeking_commerce,
    contextual_non_product_clarification,
    detect_solution_seeking_suppression,
)
from modules.ai.brain.decision.actions import ACTION_CLARIFY, ACTION_LLM_REPLY  # noqa: E402
from modules.ai.brain.product_discovery_gate import clarify_instead_of_top_products  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
)


def _ctx(
    msg: str,
    state: MerchantConversationState | None,
    *,
    tenant_id: int,
    history: list | None = None,
) -> BrainContext:
    return BrainContext(
        tenant_id=tenant_id,
        customer_phone="966500000001",
        message=msg,
        intent=Intent(name="general", confidence=0.5, raw_message=msg),
        state=state or MerchantConversationState(),
        facts=CommerceFacts(has_products=True, orderable=True),
        history=history,
    )


def scenario_delivery_location(*, tenant_id: int) -> None:
    print("=== 1) delivery then location ===")
    state = MerchantConversationState(turn=2)
    d1 = clarify_instead_of_top_products(
        _ctx("فيه توصيل؟", state, tenant_id=tenant_id),
        reason="smoke",
    )
    assert d1.action == ACTION_LLM_REPLY, d1
    assert (d1.args or {}).get("topic") == "ask_shipping", d1.args
    assert classify_solution_seeking_commerce("فيه توصيل؟") is None

    state.turn = 4
    topic = resolve_active_topic(
        "موقعي البيعة",
        state,
        [{"direction": "in", "body": "فيه توصيل؟"}],
    )
    assert topic in {"delivery_intent", "location_intent"}, topic
    d2 = clarify_instead_of_top_products(
        _ctx("موقعي البيعة", state, tenant_id=tenant_id),
        reason="smoke",
    )
    assert d2.action == ACTION_LLM_REPLY, d2
    assert (d2.args or {}).get("topic") in {
        "ask_shipping",
        "fulfillment_location",
    }, d2.args
    print("OK — delivery context preserved, no product advisory")


def scenario_price_all_sizes(*, tenant_id: int) -> None:
    print("=== 2) price loop then all sizes ===")
    history = [
        {"direction": "in", "body": "كم السعر"},
    ]
    goal = detect_semantic_dead_end("كل الحجام", history=history, state=None)
    assert goal == "all_variant_prices", goal

    state = MerchantConversationState(turn=3, customer_goal="")
    d = clarify_instead_of_top_products(
        _ctx("كل الحجام", state, tenant_id=tenant_id, history=history),
        reason="smoke",
    )
    assert d.action == ACTION_LLM_REPLY, d
    assert (d.args or {}).get("topic") == "show_all_variants_prices", d.args
    print("OK — dead-end routed to show_all_variants_prices")


def scenario_payment_short(*, tenant_id: int) -> None:
    print("=== 3) payment clarify ===")
    msg = "لك فلوس معاي"
    assert detect_solution_seeking_suppression(msg) == "payment_intent"
    assert classify_solution_seeking_commerce(msg) is None
    q = contextual_non_product_clarification(msg)
    assert q and len(q) < 80, q

    state = MerchantConversationState(turn=2)
    d = clarify_instead_of_top_products(
        _ctx(msg, state, tenant_id=tenant_id),
        reason="smoke",
    )
    assert d.action == ACTION_CLARIFY, d
    assert "منتج" not in (d.args or {}).get("question", "").lower()
    assert (d.args or {}).get("topic") != "solution_seeking_commerce"

    state.last_fallback_fingerprint = __import__(
        "modules.ai.brain.commerce.fallback_guard", fromlist=["fallback_fingerprint"]
    ).fallback_fingerprint(q or "")
    state.last_fallback_turn = 2
    state.turn = 3
    d2 = clarify_instead_of_top_products(
        _ctx(msg, state, tenant_id=tenant_id),
        reason="smoke",
    )
    assert d2.action == ACTION_LLM_REPLY
    assert (d2.args or {}).get("topic") == "ask_payment_info"
    print("OK — short payment clarify, repeat blocked")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant-id", type=int, required=True, help="Tenant id for BrainContext")
    args = parser.parse_args()
    scenario_delivery_location(tenant_id=args.tenant_id)
    scenario_price_all_sizes(tenant_id=args.tenant_id)
    scenario_payment_short(tenant_id=args.tenant_id)
    print("\nAll PR1 smoke scenarios passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
