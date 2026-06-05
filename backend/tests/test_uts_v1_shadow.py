"""Tests for UTS v1 Phase 2 — shadow manifest, dedup, integrity gate."""
from __future__ import annotations

import json
import logging

import pytest

from modules.ai.brain.truth_surface import (
    EffectiveFactStatus,
    UTS_V1_INGEST_SURFACES,
    build_uts_v1_manifest,
    is_uts_v1_shadow_enabled,
    run_uts_v1_shadow,
)
from modules.ai.brain.truth_surface.contract import TruthSurface
from modules.ai.brain.types import BrainReplyState


def test_uts_v1_ingest_surfaces_count() -> None:
    assert len(UTS_V1_INGEST_SURFACES) == 9


def test_collector_ingests_all_scoped_surfaces() -> None:
    state = BrainReplyState(
        intent_name="product_inquiry",
        stage="discovery",
        platform_kb_excerpt="اشتراك نحلة",
        selected_product={"id": 1, "title": "عسل", "price": 120, "orderable": True},
        last_recommended_products=[{"id": 2, "title": "دهن", "price": 80}],
        known_facts={
            "store_name": "متجر",
            "shipping_policy": "شحن 25",
            "checkout_preparation": {"order_status": "awaiting_payment"},
        },
        merchant_context={
            "structured_facts_block": "سياسة الإرجاع 14 يوم",
            "products": [{"id": 1, "title": "عسل", "price": 99, "orderable": True}],
            "policies": {"shipping": "شحن مجاني"},
        },
        response_goal="should not be ingested",
    )
    bundle = {
        "goal": "القولون",
        "usage_guidance": ["ملعقة صباحاً"],
        "items": [{"title": "عسل القولون", "resolved": True}],
    }
    result = build_uts_v1_manifest(
        state,
        tenant_id=10,
        goal_regimen_bundle=bundle,
    )
    ingested = set(result.manifest.ingested_surfaces)
    assert "structured_facts_block" in ingested
    assert "merchant_context.products" in ingested
    assert "selected_product" in ingested
    assert "last_recommended_products" in ingested
    assert "known_facts.checkout_preparation" in ingested
    assert "merchant_context.policies" in ingested
    assert "known_facts" in ingested
    assert "platform_kb_excerpt" in ingested
    assert "goal_regimen_bundle" in ingested
    assert result.manifest.operational_facts_block is not None
    assert result.manifest.active_fact_count >= 1


def test_dedup_selected_product_when_in_catalog() -> None:
    state = BrainReplyState(
        selected_product={"id": 5, "title": "عسل", "price": 150},
        merchant_context={
            "products": [{"id": 5, "title": "عسل", "price": 99, "orderable": True}],
        },
    )
    result = build_uts_v1_manifest(state)
    deduped = [
        f for f in result.manifest.effective_facts
        if f.status == EffectiveFactStatus.DEDUPED
        and f.source_surface == TruthSurface.SELECTED_PRODUCT
    ]
    assert deduped
    assert result.manifest.deduped_count >= 1


def test_dedup_shipping_known_facts_when_policies_present() -> None:
    state = BrainReplyState(
        known_facts={"shipping_policy": "شحن 30", "store_name": "متجر"},
        merchant_context={"policies": {"shipping": "شحن مجاني"}},
    )
    result = build_uts_v1_manifest(state)
    shipping = [
        f for f in result.manifest.effective_facts
        if f.fact_key == "store:shipping_policy"
    ]
    assert shipping
    assert shipping[0].status == EffectiveFactStatus.DEDUPED


def test_price_conflict_detected() -> None:
    state = BrainReplyState(
        merchant_context={
            "products": [
                {"id": 3, "title": "X", "price": 50, "orderable": True},
                {"id": 3, "title": "X", "price": 70, "orderable": True},
            ],
        },
    )
    result = build_uts_v1_manifest(state)
    conflicts = [
        f for f in result.manifest.effective_facts
        if f.status == EffectiveFactStatus.CONFLICT
    ]
    assert conflicts


def test_integrity_gate_counts_external_surfaces() -> None:
    state = BrainReplyState(
        store_knowledge={"store_name": "متجر"},
        coupon_policy={"has_coupons": True},
        response_goal="offer product",
        merchant_context={"products": [{"id": 1, "title": "A", "price": 10}]},
    )
    history = [{"role": "assistant", "content": "السعر 200 ريال"}]
    result = build_uts_v1_manifest(state, history_messages=history)
    assert result.integrity.external_operational_surfaces_count >= 1
    assert result.integrity.external_operational_facts_count >= 1


def test_shadow_emits_log_when_flag_enabled(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("NAHLA_UTS_V1_SHADOW_ENABLED", "true")
    assert is_uts_v1_shadow_enabled()
    caplog.set_level(logging.INFO)
    run_uts_v1_shadow(
        BrainReplyState(
            merchant_context={"structured_facts_block": "test kb"},
        ),
        tenant_id=33,
    )
    lines = [r for r in caplog.records if "[UTS_V1_SHADOW]" in r.message]
    assert lines
    payload = json.loads(lines[-1].message.split("[UTS_V1_SHADOW] ", 1)[1])
    assert payload["event"] == "uts_v1_shadow_audit"
    assert "effective_facts_count" in payload
    assert "integrity_gate" in payload


def test_enforce_flag_does_not_change_prompt_builder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default flags: prompt_builder output unchanged (no enforce wiring)."""
    from modules.ai.brain.compose.prompt_builder import build_brain_reply_prompt

    monkeypatch.delenv("NAHLA_UTS_V1_ENFORCE_ENABLED", raising=False)
    monkeypatch.delenv("NAHLA_UTS_V1_SHADOW_ENABLED", raising=False)
    state = BrainReplyState(
        store_name="متجر",
        merchant_context={"structured_facts_block": "حقائق"},
    )
    prompt = build_brain_reply_prompt(state)
    assert "UTS v1 — shadow" not in prompt
    assert "حقائق" in prompt
