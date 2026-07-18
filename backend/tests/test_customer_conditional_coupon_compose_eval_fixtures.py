"""Offline eval fixtures CC-01..CC-05 for conditional-coupon compose consumer."""
from __future__ import annotations

import asyncio
import os
import re
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.truth_surface.contract import (  # noqa: E402
    TrustedContextSnapshot,
    TrustedDomain,
    TrustedFact,
    TruthSource,
)
from modules.ai.brain.truth_surface.customer_conditional_coupon_compose_projection import (  # noqa: E402
    project_customer_conditional_coupon_compose_facts,
)
from modules.ai.brain.truth_surface.customer_conditional_coupon_consumption_gate import (  # noqa: E402
    maybe_customer_conditional_coupon_compose_facts,
)
from modules.ai.brain.truth_surface.customer_conditional_coupon_contract import (  # noqa: E402
    COMPLETENESS_VERIFIED,
    EVALUATION_CONDITION_SATISFIED,
    EVALUATION_CONDITION_SHORTFALL,
    EVALUATION_REQUIRES_CONTEXT,
    IDENTITY_STATUS_RESOLVED,
    IDENTITY_STATUS_UNRESOLVED,
    MIN_ORDERS_STATE_SATISFIED,
    MIN_ORDERS_STATE_SHORTFALL,
    build_sanitized_fact_record,
)
from modules.ai.brain.types import (  # noqa: E402
    ActionResult,
    BrainContext,
    Decision,
    INTENT_GENERAL,
    Intent,
    MerchantConversationState,
)

_MERCHANT = "متجر تجريبي عام"
_PRODUCT = "قميص قطني أزرق"
_MESSAGE = "بعد كم طلب يصل كوبون متجر تجريبي عام؟"
_TENANT = 8101
_PHONE = "966500011122"


def _eligible_ai_settings() -> dict:
    return {
        "store_ai_mode": "test",
        "customer_conditional_coupon_compose_allowlist_tenants": [_TENANT],
        "ai_test_allowed_numbers": [_PHONE],
    }

_FORBIDDEN_CLAIM_PATTERNS = (
    re.compile(r"كود\s*الكوبون", re.IGNORECASE | re.UNICODE),
    re.compile(r"\bCODE\s*[:=]", re.IGNORECASE),
    re.compile(r"تم\s*إصدار\s*الكوبون", re.IGNORECASE | re.UNICODE),
    re.compile(r"coupon\s*code\s*is", re.IGNORECASE),
)


def _record(**overrides) -> dict:
    base = build_sanitized_fact_record(
        identity_status=IDENTITY_STATUS_RESOLVED,
        customer_scope="nahla_internal_customer",
        order_history_completeness=COMPLETENESS_VERIFIED,
        order_history_completeness_source="order_customer_fk_a1_authoritative",
        completed_orders_count=3,
        min_orders_for_eligibility=3,
        orders_shortfall=None,
        min_orders_condition_state=MIN_ORDERS_STATE_SATISFIED,
        prior_redemption_evidence_state="not_applicable",
        per_customer_usage_policy_state="verified",
        conditional_coupon_evaluation_state=EVALUATION_CONDITION_SATISFIED,
        closed_reason_code=None,
        allow_min_orders_condition_claim=True,
    )
    base.update(overrides)
    return base


def _snapshot(record: dict, *, tenant_id: int = 8101) -> TrustedContextSnapshot:
    snap = TrustedContextSnapshot(
        tenant_id=tenant_id,
        facts=[
            TrustedFact(
                domain=TrustedDomain.CUSTOMER_CONDITIONAL_COUPON,
                key="customer_conditional_coupon:eligibility",
                value=record,
                source=TruthSource.PROMOTION_TABLE,
                path="customer_conditional_coupon_loader.layer0",
            )
        ],
        shadow_observability={
            "merchant_label": _MERCHANT,
            "product_context": _PRODUCT,
        },
    )
    snap.ensure_snapshot_id()
    return snap


def _assert_no_forbidden_claims(text: str) -> None:
    for pattern in _FORBIDDEN_CLAIM_PATTERNS:
        assert not pattern.search(text), f"forbidden claim matched: {pattern.pattern}"


@pytest.mark.parametrize(
    ("fixture_id", "record_overrides", "expect_allow"),
    [
        (
            "CC-01",
            {},
            True,
        ),
        (
            "CC-02",
            {
                "completed_orders_count": 1,
                "orders_shortfall": 2,
                "min_orders_condition_state": MIN_ORDERS_STATE_SHORTFALL,
                "conditional_coupon_evaluation_state": EVALUATION_CONDITION_SHORTFALL,
                "closed_reason_code": "orders_shortfall",
                "allow_min_orders_condition_claim": False,
            },
            False,
        ),
        (
            "CC-03",
            {
                "identity_status": IDENTITY_STATUS_UNRESOLVED,
                "completed_orders_count": None,
                "min_orders_for_eligibility": None,
                "orders_shortfall": None,
                "min_orders_condition_state": "not_evaluated",
                "conditional_coupon_evaluation_state": EVALUATION_REQUIRES_CONTEXT,
                "closed_reason_code": "customer_unverified",
                "allow_min_orders_condition_claim": False,
                "order_history_completeness": "unverified",
                "order_history_completeness_source": None,
            },
            False,
        ),
    ],
)
def test_cc_projection_fixtures(
    fixture_id: str,
    record_overrides: dict,
    expect_allow: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_COMPOSE_ENABLED",
        "true",
    )
    record = _record(**record_overrides)
    snap = _snapshot(record)
    projected = project_customer_conditional_coupon_compose_facts(snapshot=snap)
    gated = maybe_customer_conditional_coupon_compose_facts(
        message=_MESSAGE,
        snapshot=snap,
        tenant_id=_TENANT,
        customer_phone=_PHONE,
        ai_settings=_eligible_ai_settings(),
    )
    assert gated is not None, fixture_id
    assert projected["allow_min_orders_condition_claim"] is expect_allow, fixture_id


def test_cc_04_both_flags_off_zero_compose_io(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(
        "NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_COMPOSE_ENABLED",
        raising=False,
    )
    snap = _snapshot(_record())
    assert (
        maybe_customer_conditional_coupon_compose_facts(
            message=_MESSAGE,
            snapshot=snap,
        )
        is None
    )


def test_cc_05_compose_end_to_end_metadata_and_no_forbidden_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_COMPOSE_ENABLED",
        "true",
    )
    compose_facts = project_customer_conditional_coupon_compose_facts(
        snapshot=_snapshot(_record()),
    )

    async def _run() -> tuple[str, dict]:
        from modules.ai.brain.compose.responder import DefaultComposer  # noqa: PLC0415
        from modules.ai.brain.types import BrainReplyState  # noqa: PLC0415

        ctx = BrainContext(
            tenant_id=_TENANT,
            customer_phone=_PHONE,
            message=_MESSAGE,
            intent=Intent(name=INTENT_GENERAL, confidence=0.9),
            state=MerchantConversationState(stage="browsing", customer_goal="general_help"),
            facts=SimpleNamespace(store_name=_MERCHANT),
            history=[],
            profile={},
        )
        ctx.reply_state = BrainReplyState(
            store_name=_MERCHANT,
            known_facts={"customer_conditional_coupon_facts": compose_facts},
        )
        result = ActionResult(success=True, data={})
        composer = DefaultComposer()
        with patch(
            "modules.ai.brain.persona.customer_conditional_coupon_answer."
            "evaluate_customer_conditional_coupon_compose_canary",
        ) as canary_mock, patch(
            "modules.ai.brain.persona.fact_bound_composer.FactBoundPersonaComposer.compose",
            new_callable=AsyncMock,
        ) as compose_mock:
            from modules.ai.brain.truth_surface.customer_conditional_coupon_compose_canary_gate import (  # noqa: PLC0415
                CustomerConditionalCouponComposeCanaryDecision,
                REASON_ALLOWED,
            )

            canary_mock.return_value = CustomerConditionalCouponComposeCanaryDecision(
                allowed=True,
                reason=REASON_ALLOWED,
                compose_master_enabled=True,
            )
            from modules.ai.brain.persona.facts_bundle import PersonaComposeResult  # noqa: PLC0415

            compose_mock.return_value = PersonaComposeResult(
                text="بعد 3 طلبات مكتملة يتفعل عرض الكوبون حسب بيانات المتجر.",
                source="persona_llm",
                surface="customer_conditional_coupon_answer",
                facts_hash="cc05",
                guard_passed=True,
                language="ar",
            )
            text = await composer.compose(
                Decision(action="llm_reply", args={}),
                result,
                ctx,
            )
        return text, dict(result.data)

    text, meta = asyncio.run(_run())
    _assert_no_forbidden_claims(text)
    assert meta.get("compose_source") == "persona_llm"
    assert meta.get("response_mode") == "customer_conditional_coupon_answer"
    assert meta.get("chosen_path") == "customer_conditional_coupon_compose"
    assert meta.get("llm_candidate_present") is True
    assert meta.get("final_text_transformed") is False
    assert meta.get("facts_snapshot_id")
