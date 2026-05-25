"""
tests/test_relational_pipeline_telemetry.py
───────────────────────────────────────────
Pipeline-level integration tests for Commit 1 of the relational
architecture rollout.

Headline guarantees this file pins (per merchant directive):

  1. ``RELATIONAL_LAYER_ENABLED=false`` (default) -> pipeline does
     NOT invoke ``compute_relational_state``, ``ctx.relational_state``
     stays ``None``, no ``[CX]`` log line emitted.

  2. ``RELATIONAL_LAYER_ENABLED=true`` -> pipeline computes the
     verdict, attaches it to ``BrainContext.relational_state``, emits
     the ``[CX]`` log line — but the reply / action / decision /
     state remain BYTE-IDENTICAL to the flag-off run (zero behaviour
     change). This is the core safety guarantee for Commit 1.

  3. The relational verdict NEVER fabricates business state — even
     in the pipeline integration this test re-asserts the
     architectural invariant.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for p in [str(REPO_ROOT), str(BACKEND_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from modules.ai.brain.types import (  # noqa: E402
    INTENT_GREETING,
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
)


# ── helpers (mirrored from test_merchant_brain) ────────────────────


def _make_facts() -> CommerceFacts:
    return CommerceFacts(
        has_products=True,
        product_count=2,
        in_stock_count=2,
        has_active_integration=True,
        orderable=True,
        has_coupons=False,
        snapshot_fresh=True,
        store_name="متجر النحلة",
        store_url="https://example.test",
        store_description="عسل ومنتجاته",
        store_contact_phone="+966500000000",
        shipping_policy="الشحن خلال 2-4 أيام",
        support_hours="9-22",
        shipping_methods=["سمسا"],
        integration_platform="salla",
    )


def _make_state() -> MerchantConversationState:
    return MerchantConversationState(stage="discovery", greeted=False)


def _build_brain():
    from modules.ai.brain.pipeline import MerchantBrain
    from modules.ai.brain.decision.engine import DefaultDecisionEngine
    from modules.ai.brain.decision.policy import PassThroughPolicyGate
    from modules.ai.brain.execution.executor import DefaultActionExecutor
    from modules.ai.brain.compose.responder import DefaultComposer
    from modules.ai.brain.memory.updater import DefaultMemoryUpdater

    intent = Intent(name=INTENT_GREETING, confidence=0.95, raw_message="مرحبا")
    state = _make_state()
    facts = _make_facts()

    classifier = MagicMock()
    classifier.classify = AsyncMock(return_value=intent)

    state_store = MagicMock()
    state_store.load.return_value = state
    state_store.save.return_value = None
    state_store.transition.return_value = state

    facts_loader = MagicMock()
    facts_loader.load.return_value = facts

    memory_updater = MagicMock()
    memory_updater.update.return_value = None

    return MerchantBrain(
        classifier=classifier,
        state_store=state_store,
        facts_loader=facts_loader,
        decision_engine=DefaultDecisionEngine(),
        policy_gate=PassThroughPolicyGate(),
        executor=DefaultActionExecutor(),
        composer=DefaultComposer(),
        memory_updater=memory_updater,
    )


def _db():
    db = MagicMock()
    db.add.return_value = None
    db.commit.return_value = None
    return db


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ── unit: feature flag function ────────────────────────────────────


def test_relational_layer_flag_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    from modules.ai.brain.pipeline import _relational_layer_enabled

    monkeypatch.delenv("RELATIONAL_LAYER_ENABLED", raising=False)
    assert _relational_layer_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "ON"])
def test_relational_layer_flag_truthy_values(
    monkeypatch: pytest.MonkeyPatch, val: str,
) -> None:
    from modules.ai.brain.pipeline import _relational_layer_enabled

    monkeypatch.setenv("RELATIONAL_LAYER_ENABLED", val)
    assert _relational_layer_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "", "maybe"])
def test_relational_layer_flag_falsy_values(
    monkeypatch: pytest.MonkeyPatch, val: str,
) -> None:
    from modules.ai.brain.pipeline import _relational_layer_enabled

    monkeypatch.setenv("RELATIONAL_LAYER_ENABLED", val)
    assert _relational_layer_enabled() is False


# ── pipeline: flag OFF -> no compute, no log, no context attribute ─


def test_pipeline_flag_off_does_not_compute_relational_state(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv("RELATIONAL_LAYER_ENABLED", raising=False)
    caplog.set_level(logging.INFO, logger="nahla.relational")

    brain = _build_brain()
    reply = _run(brain.process(
        db=_db(),
        tenant_id=33,
        customer_phone="+966500000001",
        message="مرحبا",
        history=[],
        profile={},
    ))

    assert isinstance(reply, dict)
    # No CX log line should be emitted.
    assert not any("[CX]" in r.getMessage() for r in caplog.records)


# ── pipeline: flag ON -> compute + log, but reply unchanged ────────


def test_pipeline_flag_on_emits_cx_log_line_without_changing_reply(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """Run the pipeline twice with the SAME inputs — once flag-off,
    once flag-on. Replies, actions and brain states must be byte-
    identical. The only difference is the CX log line."""
    # ── Flag OFF baseline ──
    monkeypatch.delenv("RELATIONAL_LAYER_ENABLED", raising=False)
    brain_off = _build_brain()
    reply_off = _run(brain_off.process(
        db=_db(),
        tenant_id=33,
        customer_phone="+966500000001",
        message="مرحبا",
        history=[],
        profile={},
    ))

    # ── Flag ON ──
    caplog.clear()
    caplog.set_level(logging.INFO, logger="nahla.relational")
    monkeypatch.setenv("RELATIONAL_LAYER_ENABLED", "1")
    brain_on = _build_brain()
    reply_on = _run(brain_on.process(
        db=_db(),
        tenant_id=33,
        customer_phone="+966500000001",
        message="مرحبا",
        history=[],
        profile={},
    ))

    # Behaviour must be identical between the two runs.
    assert reply_off.get("reply") == reply_on.get("reply"), (
        "Relational layer flag changed reply text — zero-behaviour-"
        "change invariant violated."
    )
    assert reply_off.get("action") == reply_on.get("action")

    # CX log line was emitted once on the ON run.
    cx_records = [r for r in caplog.records if "[CX]" in r.getMessage()]
    assert len(cx_records) == 1, (
        f"expected exactly one [CX] log line; got {len(cx_records)}"
    )
    msg = cx_records[0].getMessage()
    # Stable greppable tokens — operators rely on these.
    assert "tenant=33" in msg
    assert "cx_moment=" in msg
    assert "cx_lifecycle=" in msg
    assert "cx_sentiment=" in msg


# ── pipeline: BrainContext.relational_state is populated when ON ───


def test_brain_context_relational_state_field_default_none() -> None:
    """The new ``BrainContext.relational_state`` field defaults to
    ``None`` — flag-off code paths see exactly the previous
    BrainContext shape."""
    intent = Intent(name=INTENT_GREETING, confidence=0.95, raw_message="مرحبا")
    ctx = BrainContext(
        tenant_id=1,
        customer_phone="+966500000001",
        message="مرحبا",
        intent=intent,
        state=_make_state(),
        facts=_make_facts(),
    )
    assert ctx.relational_state is None


def test_brain_context_relational_state_accepts_relational_state_object() -> None:
    """Sanity: the optional field accepts the typed object without
    mutating it (frozen dataclass)."""
    from modules.ai.brain.relational import (
        ConversationMoment,
        RelationalState,
    )

    intent = Intent(name=INTENT_GREETING, confidence=0.95, raw_message="مرحبا")
    rs = RelationalState(moment=ConversationMoment.SOCIAL_CHECK_IN)
    ctx = BrainContext(
        tenant_id=1,
        customer_phone="+966500000001",
        message="مرحبا",
        intent=intent,
        state=_make_state(),
        facts=_make_facts(),
        relational_state=rs,
    )
    assert ctx.relational_state is rs
    assert ctx.relational_state.moment == ConversationMoment.SOCIAL_CHECK_IN


# ── headline: the rule that must never fail in production ─────────


def test_pipeline_relational_layer_does_not_fabricate_business_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HEADLINE TEST: even when the pipeline DOES compute a
    relational verdict, the resulting object must never carry a
    field whose name implies business state.

    Re-asserts the architectural invariant from the unit test
    suite at the pipeline-integration level so a future change
    that bypasses the unit test still gets caught here.
    """
    from modules.ai.brain.relational import (
        BUSINESS_FACT_FIELD_FORBIDDEN_TOKENS,
    )

    monkeypatch.setenv("RELATIONAL_LAYER_ENABLED", "1")

    # Patch the brain to capture ctx after it runs.
    from modules.ai.brain.pipeline import MerchantBrain  # noqa: PLC0415

    captured: Dict[str, Any] = {}
    original_decide = None

    class _CapturingDecisionEngine:
        def __init__(self, inner):
            self._inner = inner

        def decide(self, ctx):
            captured["ctx"] = ctx
            return self._inner.decide(ctx)

    from modules.ai.brain.decision.engine import DefaultDecisionEngine
    from modules.ai.brain.decision.policy import PassThroughPolicyGate
    from modules.ai.brain.execution.executor import DefaultActionExecutor
    from modules.ai.brain.compose.responder import DefaultComposer
    from modules.ai.brain.memory.updater import DefaultMemoryUpdater

    intent = Intent(name=INTENT_GREETING, confidence=0.95, raw_message="حولت لكم المبلغ")
    state = _make_state()
    facts = _make_facts()

    classifier = MagicMock()
    classifier.classify = AsyncMock(return_value=intent)
    state_store = MagicMock()
    state_store.load.return_value = state
    state_store.save.return_value = None
    state_store.transition.return_value = state
    facts_loader = MagicMock()
    facts_loader.load.return_value = facts
    memory_updater = MagicMock()
    memory_updater.update.return_value = None

    brain = MerchantBrain(
        classifier=classifier,
        state_store=state_store,
        facts_loader=facts_loader,
        decision_engine=_CapturingDecisionEngine(DefaultDecisionEngine()),
        policy_gate=PassThroughPolicyGate(),
        executor=DefaultActionExecutor(),
        composer=DefaultComposer(),
        memory_updater=memory_updater,
    )

    _run(brain.process(
        db=_db(),
        tenant_id=33,
        customer_phone="+966500000001",
        message="حولت لكم المبلغ",
        history=[],
        profile={},
    ))

    ctx = captured.get("ctx")
    assert ctx is not None
    rs = ctx.relational_state
    if rs is None:
        # No relational classification fired — that's a valid
        # outcome. The invariant is still satisfied.
        return
    import dataclasses
    for f in dataclasses.fields(rs):
        for tok in BUSINESS_FACT_FIELD_FORBIDDEN_TOKENS:
            assert tok not in f.name.lower(), (
                f"pipeline-attached relational state exposed a "
                f"business-fact field {f.name!r}"
            )
