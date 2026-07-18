"""Compose canary gate — eligibility, zero-I/O, isolation, and integration tests."""
from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.tenant import STORE_AI_MODE_ON, STORE_AI_MODE_TEST  # noqa: E402
from modules.ai.brain.compose.responder import DefaultComposer  # noqa: E402
from modules.ai.brain.pipeline import _build_reply_state  # noqa: E402
from modules.ai.brain.truth_surface.contract import (  # noqa: E402
    TrustedContextSnapshot,
    TrustedDomain,
    TrustedFact,
    TruthSource,
)
from modules.ai.brain.truth_surface.customer_conditional_coupon_compose_canary_gate import (  # noqa: E402
    AI_SETTINGS_ALLOWLIST_KEY,
    ENV_ALLOWLIST_KEY,
    REASON_ALLOWLIST_CONFIG_MALFORMED,
    REASON_ALLOWED,
    REASON_COMPOSE_MASTER_DISABLED,
    REASON_NOT_RELEVANT,
    REASON_NOT_TEST_MODE,
    REASON_PHONE_NOT_ALLOWLISTED,
    REASON_TENANT_NOT_ALLOWLISTED,
    clear_customer_conditional_coupon_compose_canary_allowlist_cache,
    compose_canary_gate_telemetry_metadata,
    evaluate_customer_conditional_coupon_compose_canary,
    should_load_customer_conditional_coupon_layer0_for_turn,
)
from modules.ai.brain.truth_surface.customer_conditional_coupon_consumption_gate import (  # noqa: E402
    maybe_customer_conditional_coupon_compose_facts,
)
from modules.ai.brain.truth_surface.customer_conditional_coupon_contract import (  # noqa: E402
    COMPLETENESS_VERIFIED,
    EVALUATION_CONDITION_SHORTFALL,
    IDENTITY_STATUS_RESOLVED,
    MIN_ORDERS_STATE_SHORTFALL,
    build_sanitized_fact_record,
)
from modules.ai.brain.truth_surface.customer_conditional_coupon_loader import (  # noqa: E402
    clear_customer_conditional_coupon_turn_cache,
    load_customer_conditional_coupon_facts,
)
from modules.ai.brain.truth_surface.trusted_context import (  # noqa: E402
    build_trusted_context_snapshot,
)
from modules.ai.brain.types import (  # noqa: E402
    ActionResult,
    BrainContext,
    Decision,
    INTENT_GENERAL,
    Intent,
    MerchantConversationState,
    SuggestionSnapshot,
)

_MERCHANT = "متجر تجريبي عام"
_TENANT_A = 9101
_TENANT_B = 9102
_PHONE = "966500000001"
_MESSAGE = "بعد كم طلب يصل الكوبون؟"
_IRRELEVANT = "مرحبا كيف الحال؟"


def _eligible_ai_settings(*, tenant_id: int = _TENANT_A, phone: str = _PHONE) -> dict:
    return {
        "store_ai_mode": STORE_AI_MODE_TEST,
        AI_SETTINGS_ALLOWLIST_KEY: [int(tenant_id)],
        "ai_test_allowed_numbers": [phone],
    }


def _layer0_fact() -> TrustedFact:
    record = build_sanitized_fact_record(
        identity_status=IDENTITY_STATUS_RESOLVED,
        customer_scope="nahla_internal_customer",
        order_history_completeness=COMPLETENESS_VERIFIED,
        order_history_completeness_source="order_customer_fk_a1_authoritative",
        completed_orders_count=1,
        min_orders_for_eligibility=3,
        orders_shortfall=2,
        min_orders_condition_state=MIN_ORDERS_STATE_SHORTFALL,
        prior_redemption_evidence_state="not_applicable",
        per_customer_usage_policy_state="verified",
        conditional_coupon_evaluation_state=EVALUATION_CONDITION_SHORTFALL,
        closed_reason_code="orders_shortfall",
        allow_min_orders_condition_claim=False,
    )
    return TrustedFact(
        domain=TrustedDomain.CUSTOMER_CONDITIONAL_COUPON,
        key="customer_conditional_coupon:eligibility",
        value=record,
        source=TruthSource.PROMOTION_TABLE,
        path="customer_conditional_coupon_loader.layer0",
    )


def _minimal_ctx(message: str, *, tenant_id: int = _TENANT_A) -> BrainContext:
    facts = SimpleNamespace(
        store_name=_MERCHANT,
        store_url="https://example.test",
        store_url_resolved=True,
        store_url_source="settings",
        has_products=True,
        product_count=3,
        in_stock_count=2,
        orderable=True,
        shipping_policy="",
        shipping_methods=[],
        shipping_notes="",
        support_hours="",
        store_contact_phone="",
        store_contact_email="",
        has_coupons=True,
        coupon_eligibility=None,
    )
    return BrainContext(
        tenant_id=tenant_id,
        customer_phone=_PHONE,
        message=message,
        intent=Intent(name=INTENT_GENERAL, confidence=0.9),
        state=MerchantConversationState(stage="browsing", customer_goal="general_help"),
        facts=facts,
        history=[],
        profile={},
    )


@pytest.fixture(autouse=True)
def _reset_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_customer_conditional_coupon_turn_cache()
    clear_customer_conditional_coupon_compose_canary_allowlist_cache()
    monkeypatch.delenv("NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_SHADOW_ENABLED", raising=False)
    monkeypatch.delenv("NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_COMPOSE_ENABLED", raising=False)
    monkeypatch.delenv(ENV_ALLOWLIST_KEY, raising=False)


def test_master_flag_off_zero_io_all_tenants() -> None:
    with patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader."
        "resolve_conditional_coupon_subject_handle",
    ) as resolve_mock:
        facts, obs = load_customer_conditional_coupon_facts(
            db=MagicMock(),
            tenant_id=_TENANT_A,
            message=_MESSAGE,
            customer_phone=_PHONE,
            ai_settings=_eligible_ai_settings(),
        )
    resolve_mock.assert_not_called()
    assert facts == []
    assert obs["gate_skipped_reason"] == "layer0_flags_disabled"


def test_master_on_tenant_not_allowlisted_zero_io(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_COMPOSE_ENABLED", "true")
    with patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader."
        "resolve_conditional_coupon_subject_handle",
    ) as resolve_mock:
        facts, obs = load_customer_conditional_coupon_facts(
            db=MagicMock(),
            tenant_id=_TENANT_A,
            message=_MESSAGE,
            customer_phone=_PHONE,
            ai_settings={
                "store_ai_mode": STORE_AI_MODE_TEST,
                AI_SETTINGS_ALLOWLIST_KEY: [_TENANT_B],
                "ai_test_allowed_numbers": [_PHONE],
            },
        )
    resolve_mock.assert_not_called()
    assert facts == []
    assert obs["gate_skipped_reason"] == REASON_TENANT_NOT_ALLOWLISTED


def test_allowlisted_non_test_mode_zero_io(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_COMPOSE_ENABLED", "true")
    with patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader."
        "resolve_conditional_coupon_subject_handle",
    ) as resolve_mock:
        facts, obs = load_customer_conditional_coupon_facts(
            db=MagicMock(),
            tenant_id=_TENANT_A,
            message=_MESSAGE,
            customer_phone=_PHONE,
            ai_settings={
                "store_ai_mode": STORE_AI_MODE_ON,
                AI_SETTINGS_ALLOWLIST_KEY: [_TENANT_A],
                "ai_test_allowed_numbers": [_PHONE],
            },
        )
    resolve_mock.assert_not_called()
    assert facts == []
    assert obs["gate_skipped_reason"] == REASON_NOT_TEST_MODE


def test_malformed_allowlist_fail_closed_globally(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_COMPOSE_ENABLED", "true")
    monkeypatch.setenv(ENV_ALLOWLIST_KEY, "not,valid,ids")
    with patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader."
        "resolve_conditional_coupon_subject_handle",
    ) as resolve_mock:
        facts_a, obs_a = load_customer_conditional_coupon_facts(
            db=MagicMock(),
            tenant_id=_TENANT_A,
            message=_MESSAGE,
            customer_phone=_PHONE,
            ai_settings={"store_ai_mode": STORE_AI_MODE_TEST, "ai_test_allowed_numbers": [_PHONE]},
        )
        facts_b, obs_b = load_customer_conditional_coupon_facts(
            db=MagicMock(),
            tenant_id=_TENANT_B,
            message=_MESSAGE,
            customer_phone=_PHONE,
            ai_settings=_eligible_ai_settings(tenant_id=_TENANT_B),
        )
    resolve_mock.assert_not_called()
    assert facts_a == [] and facts_b == []
    assert obs_a["gate_skipped_reason"] == REASON_ALLOWLIST_CONFIG_MALFORMED
    assert obs_b["gate_skipped_reason"] == REASON_ALLOWLIST_CONFIG_MALFORMED


def test_eligible_allowlisted_test_tenant_one_load_and_compose(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_COMPOSE_ENABLED", "true")
    snap = TrustedContextSnapshot(tenant_id=_TENANT_A, facts=[_layer0_fact()])
    snap.ensure_snapshot_id()
    with patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader."
        "resolve_conditional_coupon_subject_handle",
        return_value=SimpleNamespace(
            status=IDENTITY_STATUS_RESOLVED,
            handle=SimpleNamespace(
                subject_kind="nahla_internal_customer",
                customer_id=77,
            ),
        ),
    ), patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader."
        "bound_proof_snapshot_from_handle",
        return_value=SimpleNamespace(
            policy_eligibility_ready=lambda: True,
            authoritative_source_history_completeness=lambda: "complete",
            forward_sync_health=lambda: "healthy",
            identity_namespace=lambda: "nahla_internal_order_v1",
        ),
    ), patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader.scan_conditional_targets",
        return_value=[SimpleNamespace(id=1, conditions={"min_orders_for_eligibility": 3}, extra_metadata={})],
    ) as scan_mock, patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader.count_countable_orders_for_subject",
        return_value=1,
    ) as count_mock:
        facts, _obs = load_customer_conditional_coupon_facts(
            db=MagicMock(),
            tenant_id=_TENANT_A,
            message=_MESSAGE,
            customer_phone=_PHONE,
            ai_settings=_eligible_ai_settings(),
        )
    scan_mock.assert_called_once()
    count_mock.assert_called_once()
    assert len(facts) == 1

    gated = maybe_customer_conditional_coupon_compose_facts(
        message=_MESSAGE,
        snapshot=snap,
        tenant_id=_TENANT_A,
        customer_phone=_PHONE,
        ai_settings=_eligible_ai_settings(),
    )
    assert gated is not None

    from modules.ai.brain.truth_surface.trusted_context import (  # noqa: PLC0415
        clear_trusted_context,
        set_current_trusted_context,
    )

    clear_trusted_context()
    set_current_trusted_context(snap)
    ctx = _minimal_ctx(_MESSAGE)
    state = _build_reply_state(
        ctx=ctx,
        previous_state=ctx.state,
        current_state=ctx.state,
        suggestion=SuggestionSnapshot(suggested_next_step=""),
        decision=Decision(action="llm_reply", args={}),
        merchant_context={"ai_settings": _eligible_ai_settings()},
    )
    clear_trusted_context()
    assert "customer_conditional_coupon_facts" in state.known_facts

    result = ActionResult(success=True, data={})
    decision = Decision(action="llm_reply", args={})
    composer = DefaultComposer()
    from modules.ai.brain.types import BrainReplyState  # noqa: PLC0415

    ctx.reply_state = BrainReplyState(
        store_name=_MERCHANT,
        known_facts={"customer_conditional_coupon_facts": gated},
    )
    with patch(
        "modules.ai.brain.persona.customer_conditional_coupon_answer."
        "try_compose_customer_conditional_coupon_answer",
        new_callable=AsyncMock,
    ) as compose_mock:
        from modules.ai.brain.persona.facts_bundle import PersonaComposeResult  # noqa: PLC0415

        compose_mock.return_value = (
            "إجابة تجريبية عن شروط الكوبون.",
            PersonaComposeResult(
                text="إجابة تجريبية عن شروط الكوبون.",
                source="persona_llm",
                surface="customer_conditional_coupon_answer",
                facts_hash="probe",
                guard_passed=True,
                language="ar",
            ),
            {
                "chosen_path": "customer_conditional_coupon_compose",
                "customer_conditional_coupon_compose_active": True,
                "compose_source": "persona_llm",
            },
        )
        text = asyncio.run(composer.compose(decision, result, ctx))
    compose_mock.assert_called_once()
    assert text
    assert result.data.get("customer_conditional_coupon_compose_active") is True


def test_eligible_irrelevant_turn_zero_conditional_io(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_COMPOSE_ENABLED", "true")
    with patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader."
        "resolve_conditional_coupon_subject_handle",
    ) as resolve_mock:
        facts, obs = load_customer_conditional_coupon_facts(
            db=MagicMock(),
            tenant_id=_TENANT_A,
            message=_IRRELEVANT,
            customer_phone=_PHONE,
            ai_settings=_eligible_ai_settings(),
        )
    resolve_mock.assert_not_called()
    assert facts == []
    assert obs["gate_skipped_reason"] == REASON_NOT_RELEVANT


def test_tenant_isolation_no_cache_leakage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_COMPOSE_ENABLED", "true")
    count_mock = MagicMock(return_value=2)

    def _resolution(tenant_id: int) -> SimpleNamespace:
        return SimpleNamespace(
            status=IDENTITY_STATUS_RESOLVED,
            handle=SimpleNamespace(
                subject_kind="nahla_internal_customer",
                customer_id=tenant_id,
            ),
        )

    with patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader."
        "resolve_conditional_coupon_subject_handle",
        side_effect=lambda **kwargs: _resolution(int(kwargs["tenant_id"])),
    ), patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader."
        "bound_proof_snapshot_from_handle",
        return_value=SimpleNamespace(
            policy_eligibility_ready=lambda: True,
            authoritative_source_history_completeness=lambda: "complete",
            forward_sync_health=lambda: "healthy",
            identity_namespace=lambda: "nahla_internal_order_v1",
        ),
    ), patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader.scan_conditional_targets",
        return_value=[SimpleNamespace(id=1, conditions={"min_orders_for_eligibility": 2}, extra_metadata={})],
    ), patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader.count_countable_orders_for_subject",
        count_mock,
    ):
        load_customer_conditional_coupon_facts(
            db=MagicMock(),
            tenant_id=_TENANT_A,
            message=_MESSAGE,
            customer_phone=_PHONE,
            ai_settings=_eligible_ai_settings(tenant_id=_TENANT_A),
        )
        load_customer_conditional_coupon_facts(
            db=MagicMock(),
            tenant_id=_TENANT_B,
            message=_MESSAGE,
            customer_phone=_PHONE,
            ai_settings=_eligible_ai_settings(tenant_id=_TENANT_B),
        )
    assert count_mock.call_count == 2


def test_shadow_only_non_canary_tenant_never_composes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_SHADOW_ENABLED", "true")
    snap = TrustedContextSnapshot(tenant_id=_TENANT_B, facts=[_layer0_fact()])
    snap.ensure_snapshot_id()
    gated = maybe_customer_conditional_coupon_compose_facts(
        message=_MESSAGE,
        snapshot=snap,
        tenant_id=_TENANT_B,
        customer_phone=_PHONE,
        ai_settings=_eligible_ai_settings(tenant_id=_TENANT_A),
    )
    assert gated is None
    decision = evaluate_customer_conditional_coupon_compose_canary(
        tenant_id=_TENANT_B,
        customer_phone=_PHONE,
        message=_MESSAGE,
        ai_settings=_eligible_ai_settings(tenant_id=_TENANT_A),
    )
    assert decision.reason == REASON_COMPOSE_MASTER_DISABLED


def test_general_llm_provenance_only_when_canary_passed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_COMPOSE_ENABLED", "true")
    snap = TrustedContextSnapshot(tenant_id=_TENANT_A, facts=[_layer0_fact()])
    snap.ensure_snapshot_id()
    denied = maybe_customer_conditional_coupon_compose_facts(
        message=_MESSAGE,
        snapshot=snap,
        tenant_id=_TENANT_A,
        customer_phone=_PHONE,
        ai_settings={
            "store_ai_mode": STORE_AI_MODE_TEST,
            AI_SETTINGS_ALLOWLIST_KEY: [_TENANT_A],
            "ai_test_allowed_numbers": ["966500000099"],
        },
    )
    assert denied is None

    allowed = maybe_customer_conditional_coupon_compose_facts(
        message=_MESSAGE,
        snapshot=snap,
        tenant_id=_TENANT_A,
        customer_phone=_PHONE,
        ai_settings=_eligible_ai_settings(),
    )
    assert allowed is not None
    assert allowed.get("facts_snapshot_id")


def test_allowlist_parsing_whitespace_duplicates_bounds_and_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_COMPOSE_ENABLED", "true")
    monkeypatch.setenv(ENV_ALLOWLIST_KEY, " 9101 , 9101 , 9102 ")
    decision = evaluate_customer_conditional_coupon_compose_canary(
        tenant_id=9102,
        customer_phone=_PHONE,
        message=_MESSAGE,
        ai_settings={
            "store_ai_mode": STORE_AI_MODE_TEST,
            "ai_test_allowed_numbers": [_PHONE],
        },
        require_relevance=True,
    )
    telemetry = compose_canary_gate_telemetry_metadata(decision)
    assert decision.allowed is True
    assert "9101" not in str(telemetry)
    assert _PHONE not in str(telemetry)
    assert "conditional_coupon_compose_canary_reason" in telemetry

    clear_customer_conditional_coupon_compose_canary_allowlist_cache()
    monkeypatch.setenv(ENV_ALLOWLIST_KEY, ",".join(str(9000 + i) for i in range(65)))
    malformed = evaluate_customer_conditional_coupon_compose_canary(
        tenant_id=_TENANT_A,
        customer_phone=_PHONE,
        message=_MESSAGE,
        ai_settings={"store_ai_mode": STORE_AI_MODE_TEST, "ai_test_allowed_numbers": [_PHONE]},
    )
    assert malformed.reason == REASON_ALLOWLIST_CONFIG_MALFORMED


def test_trusted_context_records_canary_telemetry_when_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_COMPOSE_ENABLED", "true")
    with patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader."
        "load_customer_conditional_coupon_facts",
    ) as loader:
        snap = build_trusted_context_snapshot(
            db=MagicMock(),
            tenant_id=_TENANT_A,
            customer_phone=_PHONE,
            message=_MESSAGE,
            ai_settings=_eligible_ai_settings(tenant_id=_TENANT_B),
        )
    loader.assert_not_called()
    canary_obs = snap.shadow_observability.get("customer_conditional_coupon_compose_canary") or {}
    assert canary_obs.get("conditional_coupon_compose_canary_allowed") is False
    assert canary_obs.get("conditional_coupon_compose_canary_reason") == REASON_TENANT_NOT_ALLOWLISTED


def test_shadow_path_independent_of_compose_canary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_SHADOW_ENABLED", "true")
    should_load, reason = should_load_customer_conditional_coupon_layer0_for_turn(
        tenant_id=_TENANT_B,
        customer_phone="",
        message=_MESSAGE,
        ai_settings=None,
    )
    assert should_load is True
    assert reason == REASON_ALLOWED
