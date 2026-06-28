"""Tests for ReplyInstruction schema, registry, and constrained compose helpers."""
from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.constrained_operational_compose import resolve_prebrain_reply_text  # noqa: E402
from core.reply_instruction import (  # noqa: E402
    CONSTRAINT_NO_PAYMENT_CONFIRM,
    FORBIDDEN_PAYMENT_CONFIRM_MARKERS,
    ReplyInstruction,
    attach_instruction_to_decision,
    build_address_instruction,
    build_clear_intent_instruction,
    build_order_slot_instruction,
    build_payment_evidence_instruction,
    build_payment_method_instruction,
    is_operational_constrained_compose_enabled,
)
from core.reply_ownership_registry import (  # noqa: E402
    CLASS_C,
    lookup_path,
    registry_summary,
)
from modules.ai.brain.compose.operational_expression import (  # noqa: E402
    compose_operational_expression_goal,
)
from modules.ai.brain.postprocess.operational_reply_validator import (  # noqa: E402
    validate_operational_reply,
)
from modules.ai.brain.pre_commerce_gate import should_pre_commerce_shortcut  # noqa: E402
from modules.ai.brain.types import INTENT_GREETING, Intent, MerchantConversationState  # noqa: E402


def test_build_payment_evidence_instruction_shape() -> None:
    instr = build_payment_evidence_instruction(
        pe_status="needs_confirmation",
        pe_reason="bank_fields_no_completion",
        legacy_copy="وصلني الملف 👍",
        summary={"selected_product": "عسل سدر", "awaiting_payment_receipt": True},
    )
    assert instr.path == "payment_evidence_soft_ack"
    assert CONSTRAINT_NO_PAYMENT_CONFIRM in instr.constraints
    assert instr.legacy_copy.startswith("وصلني")


def test_attach_instruction_to_decision() -> None:
    instr = build_payment_evidence_instruction(
        pe_status="needs_confirmation",
        pe_reason="x",
        legacy_copy="legacy",
    )
    out = attach_instruction_to_decision({"reply_text": "legacy"}, instr)
    assert out["reply_text"] == "legacy"
    assert out["reply_instruction"]["path"] == "payment_evidence_soft_ack"


def test_registry_lookup_payment_evidence() -> None:
    entry = lookup_path("payment_evidence_soft_ack")
    assert entry is not None
    assert entry.migration_class == CLASS_C
    assert entry.layer == "pre_brain"


def test_registry_summary_counts() -> None:
    counts = registry_summary()
    assert sum(counts.values()) == len(counts) or sum(counts.values()) > 0


def test_operational_expression_goal_includes_constraints() -> None:
    instr = build_payment_evidence_instruction(
        pe_status="needs_confirmation",
        pe_reason="qr_without_receipt",
        legacy_copy="legacy",
    )
    goal = compose_operational_expression_goal(instr)
    assert "Do NOT confirm payment" in goal
    assert "needs_confirmation" in goal


def test_validator_blocks_false_payment_claim() -> None:
    instr = build_payment_evidence_instruction(
        pe_status="needs_confirmation",
        pe_reason="x",
        legacy_copy="legacy",
    )
    bad = "تم تأكيد الدفع وتم استلام الإيصال"
    result = validate_operational_reply(bad, instr)
    assert not result.ok

    ok = validate_operational_reply(
        "وصلت الصورة، بعد ما تكمل التحويل أرسل الإيصال النهائي 🌷",
        instr,
    )
    assert ok.ok


def test_validator_allows_receipt_ack_copy() -> None:
    from core.reply_instruction import build_payment_receipt_instruction  # noqa: E402

    instr = build_payment_receipt_instruction(
        legacy_copy="وصلنا إيصال التحويل",
        summary={
            "can_mention_receipt_product": True,
            "selected_product": "عسل",
        },
    )
    result = validate_operational_reply(instr.legacy_copy, instr)
    assert result.ok


def test_constrained_compose_flag_default_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPERATIONAL_CONSTRAINED_COMPOSE_ENABLED", raising=False)
    assert is_operational_constrained_compose_enabled() is True


def test_constrained_compose_flag_can_disable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPERATIONAL_CONSTRAINED_COMPOSE_ENABLED", "false")
    assert is_operational_constrained_compose_enabled() is False


def test_reply_instruction_roundtrip() -> None:
    instr = build_payment_evidence_instruction(
        pe_status="needs_confirmation",
        pe_reason="x",
        legacy_copy="legacy",
    )
    restored = ReplyInstruction.from_dict(instr.to_dict())
    assert restored is not None
    assert restored.path == instr.path
    assert restored.forbidden_claims == instr.forbidden_claims


def test_build_hybrid_instruction_shapes() -> None:
    addr = build_address_instruction(legacy_copy="تم", summary={"selected_product": "عسل"})
    assert addr.path == "address_ingest_ack"

    pm = build_payment_method_instruction(
        legacy_copy="تم",
        payment_method="bank_transfer",
    )
    assert "bank_transfer" in pm.facts.values()

    slot = build_order_slot_instruction(
        slot="city",
        legacy_copy="ما المدينة؟",
        product={"title": "عسل"},
    )
    assert slot.facts["missing_slot"] == "city"

    clear = build_clear_intent_instruction(
        intent="price",
        legacy_copy="أبشر",
    )
    assert clear.path == "clear_intent_fallback"


def test_registry_lookup_new_paths() -> None:
    for path in (
        "address_ingest_ack",
        "payment_method_ack",
        "order_slot_prompt",
    ):
        entry = lookup_path(path)
        assert entry is not None
        assert entry.migration_class == CLASS_C


def test_pure_greeting_pre_commerce_shortcut() -> None:
    intent = Intent(
        name=INTENT_GREETING,
        confidence=0.95,
        slots={},
        raw_message="هلا",
    )
    assert should_pre_commerce_shortcut(
        intent,
        None,
        message="هلا",
        state=MerchantConversationState(),
    )


def test_resolve_prebrain_reply_uses_legacy_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPERATIONAL_CONSTRAINED_COMPOSE_ENABLED", "false")
    instr = build_payment_evidence_instruction(
        pe_status="needs_confirmation",
        pe_reason="x",
        legacy_copy="legacy copy",
    )
    decision = attach_instruction_to_decision({"reply_text": "legacy copy"}, instr)

    async def _run() -> None:
        text, meta = await resolve_prebrain_reply_text(
            db=MagicMock(),
            tenant_id=1,
            phone="966500000000",
            decision=decision,
            inbound_text="صورة",
        )
        assert text == "legacy copy"
        assert meta.get("copy_source") == "legacy_fixed"

    asyncio.run(_run())


def test_resolve_prebrain_reply_uses_llm_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPERATIONAL_CONSTRAINED_COMPOSE_ENABLED", "true")
    instr = build_payment_evidence_instruction(
        pe_status="needs_confirmation",
        pe_reason="x",
        legacy_copy="legacy copy",
    )
    decision = attach_instruction_to_decision({"reply_text": "legacy copy"}, instr)

    class _Payload:
        reply_text = "وصلت الصورة، أرسل الإيصال بعد التحويل"

    async def _run() -> None:
        with patch(
            "modules.ai.orchestrator.adapter.generate_ai_reply",
            return_value=_Payload(),
        ):
            text, meta = await resolve_prebrain_reply_text(
                db=MagicMock(),
                tenant_id=1,
                phone="966500000000",
                decision=decision,
                inbound_text="صورة",
            )
        assert text == "وصلت الصورة، أرسل الإيصال بعد التحويل"
        assert meta.get("copy_source") == "constrained_compose"
        assert meta.get("brain_called") is True

    asyncio.run(_run())


def test_resolve_prebrain_reply_falls_back_on_validation_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPERATIONAL_CONSTRAINED_COMPOSE_ENABLED", "true")
    instr = build_payment_evidence_instruction(
        pe_status="needs_confirmation",
        pe_reason="x",
        legacy_copy="legacy copy",
    )
    decision = attach_instruction_to_decision({"reply_text": "legacy copy"}, instr)

    class _Payload:
        reply_text = "تم تأكيد الدفع وتم استلام الإيصال"

    async def _run() -> None:
        with patch(
            "modules.ai.orchestrator.adapter.generate_ai_reply",
            return_value=_Payload(),
        ):
            text, meta = await resolve_prebrain_reply_text(
                db=MagicMock(),
                tenant_id=1,
                phone="966500000000",
                decision=decision,
                inbound_text="صورة",
            )
        assert text == "legacy copy"
        assert meta.get("copy_source") == "legacy_fixed"
        assert meta.get("constrained_compose_failed")

    asyncio.run(_run())