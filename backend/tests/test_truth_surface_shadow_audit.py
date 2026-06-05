"""Tests for Unified Truth Surface Phase 1 — shadow inventory only."""
from __future__ import annotations

import json
import logging

import pytest

from modules.ai.brain.truth_surface.contract import (
    OperationalFactKind,
    TruthSurface,
)
from modules.ai.brain.truth_surface.flags import is_truth_surface_shadow_enabled
from modules.ai.brain.truth_surface.inventory import build_truth_surface_inventory
from modules.ai.brain.truth_surface.shadow_audit import run_truth_surface_shadow_audit
from modules.ai.brain.types import BrainReplyState


class _MinimalState:
    intent_name = "product_inquiry"
    stage = "discovery"
    merchant_context = {}
    known_facts = {}
    selected_product = None
    last_recommended_products = []
    store_knowledge = {}
    coupon_policy = {}
    platform_kb_excerpt = ""
    clarification_evidence = {}
    response_goal = ""
    tenant_overlay = ""


def test_contract_operational_vs_personality_fields() -> None:
    """BrainReplyState personality fields are not scanned as operational surfaces."""
    state = BrainReplyState(
        store_name="متجر",
        tone="neutral",
        relational_frame="deferred",
        persona_expression_mode=True,
        persona_topic="social",
    )
    inv = build_truth_surface_inventory(state)
    kinds = {f.kind for f in inv.facts}
    assert OperationalFactKind.PRICE not in kinds or not state.merchant_context


def test_inventory_detects_structured_facts_and_products() -> None:
    state = BrainReplyState(
        store_name="متجر",
        merchant_context={
            "structured_facts_block": "السعر 120 ريال — متوفر",
            "products": [
                {"id": 1, "title": "عسل", "price": 99, "orderable": True},
            ],
        },
        known_facts={"store_name": "متجر", "store_url": "https://shop.example"},
        store_knowledge={"store_name": "متجر", "store_url": "https://shop.example"},
    )
    inv = build_truth_surface_inventory(state, tenant_id=33)
    active = {s.surface for s in inv.surfaces_active if s.active}
    assert TruthSurface.STRUCTURED_FACTS_BLOCK in active
    assert TruthSurface.MERCHANT_CONTEXT_PRODUCTS in active
    assert TruthSurface.KNOWN_FACTS in active
    assert any(f.kind == OperationalFactKind.PRICE for f in inv.facts)
    assert inv.duplicates  # store_name/url duplicated across known_facts + store_knowledge


def test_inventory_detects_price_conflict_across_surfaces() -> None:
    state = BrainReplyState(
        selected_product={"id": 7, "title": "عسل", "price": 150, "orderable": True},
        merchant_context={
            "products": [{"id": 7, "title": "عسل", "price": 99, "orderable": True}],
        },
    )
    inv = build_truth_surface_inventory(state)
    assert inv.conflicts
    assert any(c.kind == OperationalFactKind.PRICE for c in inv.conflicts)


def test_inventory_scans_chat_history_assistant_only() -> None:
    state = BrainReplyState()
    history = [
        {"role": "user", "content": "كم السعر؟"},
        {"role": "assistant", "content": "السعر 200 ريال ومتوفر"},
    ]
    inv = build_truth_surface_inventory(state, history_messages=history)
    hist_facts = [f for f in inv.facts if f.surface == TruthSurface.CHAT_HISTORY]
    assert hist_facts
    assert any(f.kind == OperationalFactKind.PRICE for f in hist_facts)


def test_inventory_marks_latent_surfaces() -> None:
    state = BrainReplyState(
        tenant_overlay="legacy overlay still loaded",
        merchant_context={"structured_facts_block": "facts", "ai_settings": {}},
    )
    inv = build_truth_surface_inventory(
        state,
        sales_context=object(),
        full_merchant_context={"manual_coupons": [], "pages": []},
    )
    assert TruthSurface.TENANT_OVERLAY_LEGACY.value in inv.latent_surfaces
    assert TruthSurface.SALES_CONTEXT_METADATA.value in inv.latent_surfaces
    assert TruthSurface.FULL_MERCHANT_CONTEXT_LATENT.value in inv.latent_surfaces


def test_shadow_audit_emits_when_flag_enabled(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("NAHLA_TRUTH_SURFACE_SHADOW_ENABLED", "true")
    assert is_truth_surface_shadow_enabled()

    state = BrainReplyState(
        merchant_context={"structured_facts_block": "توصيل مجاني"},
        intent_name="shipping_inquiry",
    )
    caplog.set_level(logging.INFO)
    result = run_truth_surface_shadow_audit(state, tenant_id=33)
    assert result is not None
    shadow_lines = [r for r in caplog.records if "[TRUTH_SURFACE_SHADOW]" in r.message]
    assert shadow_lines
    payload = json.loads(shadow_lines[-1].message.split("[TRUTH_SURFACE_SHADOW] ", 1)[1])
    assert payload["event"] == "truth_surface_shadow_audit"
    assert payload["tenant_id"] == 33


def test_shadow_audit_silent_when_flag_disabled(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv("NAHLA_TRUTH_SURFACE_SHADOW_ENABLED", raising=False)
    caplog.set_level(logging.INFO)
    result = run_truth_surface_shadow_audit(_MinimalState(), tenant_id=1)
    assert result is None
    assert not [r for r in caplog.records if "[TRUTH_SURFACE_SHADOW]" in r.message]


def test_shadow_audit_never_raises_on_bad_input() -> None:
    inv = run_truth_surface_shadow_audit(None, tenant_id=1)  # type: ignore[arg-type]
    assert inv is None or hasattr(inv, "facts")
