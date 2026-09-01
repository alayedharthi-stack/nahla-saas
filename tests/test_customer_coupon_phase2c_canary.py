"""Phase 2C canary wiring — routing, facts, isolation. No live Tenant 1 enablement."""
from __future__ import annotations

import asyncio
import inspect
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from modules.ai.brain.commerce.customer_coupon_request_owner import (  # noqa: E402
    _ELIGIBLE_FALLBACK_ACTIONS,
    attach_customer_request_coupon_facts_to_reply_state,
    maybe_own_customer_coupon_request_turn,
    project_customer_request_coupon_facts,
    should_own_customer_coupon_request_turn,
)
from modules.ai.brain.compose.prompt_state_serializer import _slim_known_facts  # noqa: E402
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_CATALOG_NAVIGATE,
    ACTION_CUSTOMER_COUPON_REQUEST,
    ACTION_FAQ_REPLY,
    ACTION_HANDOFF,
    ACTION_LLM_REPLY,
    ACTION_PAYMENT_CONTINUATION_REPLY,
    ACTION_PAYMENT_TRANSFER_PROMISE,
    ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_SEARCH_PRODUCTS,
    ACTION_SEND_PAYMENT_LINK,
    ACTION_SOCIAL_REPLY,
    ACTION_SUGGEST_COUPON,
    ACTION_TRACK_ORDER,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.execution.customer_coupon_request import (  # noqa: E402
    CustomerCouponRequestHandler,
)
from modules.ai.brain.execution.executor import DefaultActionExecutor  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    INTENT_GENERAL,
    INTENT_HESITATION,
    BrainContext,
    BrainReplyState,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
)
from modules.ai.brain.commerce.permission_gate import deny_reason_for_brain_action  # noqa: E402
from services.customer_request_coupon_canary import (  # noqa: E402
    ENV_CANARY_TENANTS,
    clear_customer_coupon_canary_cache,
    is_customer_coupon_canary_tenant,
    parse_customer_coupon_canary_tenants,
)
from services.customer_request_coupon_service import (  # noqa: E402
    CUSTOMER_COUPON_LIVE_ISSUANCE,
    CUSTOMER_COUPON_LIVE_ROUTING,
    CustomerCouponIssuanceResult,
    REASON_LEVEL_NOT_ALLOWED_FOR_AI,
    REASON_NO_LEVEL,
    REASON_REUSED,
    CLOSED_REASON_CODES,
)
from test_customer_request_coupon_service import (  # noqa: E402
    PHONE_A,
    PHONE_B,
    _add_customer,
    _add_orders,
    _add_pool_coupon,
    _make_db,
)

# Test-data only. Never imported by runtime routing.
POSITIVE_CLASSIFICATION_EXAMPLES = (
    "ابي كوبون خصم",
    "هل يوجد قسيمة لطلبتي؟",
    "can I get a discount code for my next order",
    "أعطوني كود تخفيض إذا أستاهل",
    "أبغى عرض شخصي ككوبون",
    "وش الكود المتاح لي؟",
    "عندكم كوبون لي كعميل؟",
    "أبي كود خصم لطلبي الجاي",
    "هل أستحق كوبون؟",
    "send me a coupon if I qualify",
    "أرسل لي كوبون لو فيه",
    "ممكن قسيمة خصم؟",
    "I want my customer coupon",
    "فيه كود لي ولا لا؟",
    "أبغى كوبون الخصم الخاص فيني",
    "do you have a coupon code for me",
)
NEGATIVE_CLASSIFICATION_EXAMPLES = (
    "كم سعر الحذاء الرياضي الأبيض؟",
    "هذا القميص عليه خصم؟",
    "وين موقعكم",
    "رقم الآيبان",
    "وين وصل طلبي",
    "أبي أرجع القطعة",
    "أبي أشوف المنتجات",
    "خصم الكمية في العطر ورد",
    "غالي مرة",
    "الحذاء عليه تخفيض الحين؟",
    "متردد أطلب ولا لا",
    "حساب التحويل البنكي",
    "وش حالة الطلب",
    "وين عنوان الفرع",
    "استخدمت خصم قبل كذا",
    "what's the price of the blue cotton shirt",
)

RUNTIME_SCAN_PATHS = (
    REPO_ROOT / "backend/modules/ai/brain/intent/coupon_capability_probe.py",
    REPO_ROOT / "backend/services/customer_request_coupon_service.py",
    REPO_ROOT / "backend/services/customer_request_coupon_canary.py",
    REPO_ROOT / "backend/modules/ai/brain/commerce/customer_coupon_request_owner.py",
    REPO_ROOT / "backend/modules/ai/brain/execution/customer_coupon_request.py",
)


@pytest.fixture(autouse=True)
def _reset_canary_env(monkeypatch):
    monkeypatch.delenv(ENV_CANARY_TENANTS, raising=False)
    clear_customer_coupon_canary_cache()
    yield
    clear_customer_coupon_canary_cache()


def _telemetry(*, capability: str = "none", parse_ok: bool = True) -> dict:
    return {
        "coupon_capability": capability,
        "coupon_capability_parse_ok": parse_ok,
        "coupon_capability_probe_run": True,
    }


def _ctx(*, tenant_id: int, customer_id=None, db=None, message: str = "ابي كوبون") -> BrainContext:
    ctx = BrainContext(
        tenant_id=tenant_id,
        customer_phone=PHONE_A,
        message=message,
        intent=Intent(name=INTENT_GENERAL, confidence=0.4, raw_message=message),
        state=MerchantConversationState(),
        facts=CommerceFacts(),
        customer_id=customer_id,
    )
    if db is not None:
        ctx._db = db
    return ctx


def _enable_canary(monkeypatch, tenant_id: int) -> None:
    monkeypatch.setenv(ENV_CANARY_TENANTS, str(int(tenant_id)))
    clear_customer_coupon_canary_cache()


def test_global_switches_and_canary_default_off() -> None:
    assert CUSTOMER_COUPON_LIVE_ROUTING is False
    assert CUSTOMER_COUPON_LIVE_ISSUANCE is False
    parsed, err = parse_customer_coupon_canary_tenants("")
    assert err is None
    assert parsed == frozenset()
    assert is_customer_coupon_canary_tenant(1) is False
    assert is_customer_coupon_canary_tenant(None) is False


def test_canary_allowlist_is_generic_not_tenant_one_hack() -> None:
    parsed, err = parse_customer_coupon_canary_tenants("42, 7")
    assert err is None
    assert parsed == frozenset({42, 7})
    malformed, mal_err = parse_customer_coupon_canary_tenants("abc")
    assert malformed is None
    assert mal_err == "allowlist_config_malformed"
    src = (REPO_ROOT / "backend/services/customer_request_coupon_canary.py").read_text(
        encoding="utf-8"
    )
    assert "tenant_id == 1" not in src
    owner = (REPO_ROOT / "backend/modules/ai/brain/commerce/customer_coupon_request_owner.py").read_text(
        encoding="utf-8"
    )
    assert "tenant_id == 1" not in owner


def test_canary_off_does_not_own_llm_fallback() -> None:
    decision = Decision(action=ACTION_LLM_REPLY, args={}, reason="fallback")
    out = maybe_own_customer_coupon_request_turn(
        decision,
        tenant_id=9,
        coupon_capability_telemetry=_telemetry(capability="customer_coupon_request"),
    )
    assert out.action == ACTION_LLM_REPLY
    assert should_own_customer_coupon_request_turn(
        tenant_id=9,
        capability="customer_coupon_request",
        parse_ok=True,
        current_action=ACTION_LLM_REPLY,
    ) is False


def test_non_canary_tenant_cannot_route_to_issuance(monkeypatch) -> None:
    monkeypatch.setenv(ENV_CANARY_TENANTS, "42")
    clear_customer_coupon_canary_cache()
    decision = Decision(action=ACTION_LLM_REPLY, args={})
    out = maybe_own_customer_coupon_request_turn(
        decision,
        tenant_id=7,
        coupon_capability_telemetry=_telemetry(capability="customer_coupon_request"),
    )
    assert out.action == ACTION_LLM_REPLY


def test_canary_capability_none_leaves_existing_path(monkeypatch) -> None:
    _enable_canary(monkeypatch, 42)
    original = Decision(action=ACTION_LLM_REPLY, args={"topic": "general"}, reason="llm")
    out = maybe_own_customer_coupon_request_turn(
        original,
        tenant_id=42,
        coupon_capability_telemetry=_telemetry(capability="none", parse_ok=True),
    )
    assert out is original or out.action == ACTION_LLM_REPLY
    assert out.action != ACTION_CUSTOMER_COUPON_REQUEST


def test_canary_probe_failure_fail_closed(monkeypatch) -> None:
    _enable_canary(monkeypatch, 42)
    out = maybe_own_customer_coupon_request_turn(
        Decision(action=ACTION_LLM_REPLY, args={}),
        tenant_id=42,
        coupon_capability_telemetry=_telemetry(
            capability="customer_coupon_request",
            parse_ok=False,
        ),
    )
    assert out.action == ACTION_LLM_REPLY


def test_canary_positive_capability_owns_before_llm(monkeypatch) -> None:
    _enable_canary(monkeypatch, 42)
    assert _ELIGIBLE_FALLBACK_ACTIONS == frozenset({ACTION_LLM_REPLY})
    out = maybe_own_customer_coupon_request_turn(
        Decision(action=ACTION_LLM_REPLY, args={}),
        tenant_id=42,
        coupon_capability_telemetry=_telemetry(capability="customer_coupon_request"),
    )
    assert out.action == ACTION_CUSTOMER_COUPON_REQUEST
    assert out.args["tenant_canary_enabled"] is True


def test_non_fallback_actions_not_stolen(monkeypatch) -> None:
    _enable_canary(monkeypatch, 42)
    protected = (
        ACTION_HANDOFF,
        ACTION_TRACK_ORDER,
        ACTION_PROPOSE_DRAFT_ORDER,
        ACTION_SEND_PAYMENT_LINK,
        ACTION_PAYMENT_CONTINUATION_REPLY,
        ACTION_PAYMENT_TRANSFER_PROMISE,
        ACTION_SEARCH_PRODUCTS,
        ACTION_FAQ_REPLY,
        ACTION_SOCIAL_REPLY,
        ACTION_SUGGEST_COUPON,
        ACTION_CATALOG_NAVIGATE,
    )
    for action in protected:
        out = maybe_own_customer_coupon_request_turn(
            Decision(action=action, args={}),
            tenant_id=42,
            coupon_capability_telemetry=_telemetry(capability="customer_coupon_request"),
        )
        assert out.action == action, action


def test_human_priority_does_not_issue(monkeypatch) -> None:
    _enable_canary(monkeypatch, 42)
    out = maybe_own_customer_coupon_request_turn(
        Decision(action=ACTION_LLM_REPLY, args={"human_priority": True}),
        tenant_id=42,
        coupon_capability_telemetry=_telemetry(capability="customer_coupon_request"),
    )
    assert out.action == ACTION_LLM_REPLY


def test_executor_registers_handler_and_permission_gate_does_not_map_apply_coupon() -> None:
    executor = DefaultActionExecutor()
    assert ACTION_CUSTOMER_COUPON_REQUEST in executor._handlers
    ctx = _ctx(tenant_id=3)
    assert deny_reason_for_brain_action(ctx, ACTION_CUSTOMER_COUPON_REQUEST) is None


def test_canary_positive_invokes_service_once(monkeypatch) -> None:
    db, tenant_id, _engine = _make_db()
    customer = _add_customer(db, tenant_id, PHONE_A)
    _add_orders(db, tenant_id, PHONE_A, countable=1)
    _add_pool_coupon(db, tenant_id, "NHBRZ", "bronze")
    _enable_canary(monkeypatch, tenant_id)
    calls = {"n": 0}
    real = __import__(
        "services.customer_request_coupon_service",
        fromlist=["issue_customer_coupon"],
    ).issue_customer_coupon

    async def _wrapped(*args, **kwargs):
        calls["n"] += 1
        return await real(*args, **kwargs)

    handler = CustomerCouponRequestHandler()
    ctx = _ctx(tenant_id=tenant_id, customer_id=customer.id, db=db)
    with patch(
        "modules.ai.brain.execution.customer_coupon_request.issue_customer_coupon",
        _wrapped,
    ):
        result = asyncio.run(
            handler.handle(Decision(action=ACTION_CUSTOMER_COUPON_REQUEST, args={}), ctx)
        )
    assert calls["n"] == 1
    assert result.data["service_called"] is True
    facts = result.data["customer_request_coupon_facts"]
    assert facts["requested"] is True
    assert facts["issued"] is True
    assert facts["coupon_code"] == "NHBRZ"
    assert "coupon_id" not in facts
    assert "lock" not in str(facts).lower()


def test_issued_facts_reach_compose_slim(monkeypatch) -> None:
    facts = project_customer_request_coupon_facts(
        CustomerCouponIssuanceResult(
            customer_id=11,
            countable_orders=1,
            resolved_level="bronze",
            policy_allowed=True,
            issued=True,
            coupon_id=99,
            code="NHBRZ",
            discount_type="percentage",
            discount_value="20",
            expires_at="2026-12-01T00:00:00+00:00",
            min_order_amount=0,
            reason_code="issued",
        )
    )
    assert facts["issued"] is True
    assert facts["coupon_code"] == "NHBRZ"
    assert "coupon_id" not in facts
    assert "customer_id" not in facts
    state = BrainReplyState()
    attach_customer_request_coupon_facts_to_reply_state(state, facts)
    slim = _slim_known_facts(state.known_facts)
    assert slim["customer_request_coupon_facts"]["issued"] is True
    assert slim["customer_request_coupon_facts"]["coupon_code"] == "NHBRZ"
    assert slim["answer_contract"]["status"] == "KNOWN_VALUE"
    assert slim["answer_contract"]["claimable_values"] == ["NHBRZ"]


def test_no_coupon_truthful_reason_reaches_compose() -> None:
    facts = project_customer_request_coupon_facts(
        CustomerCouponIssuanceResult(
            customer_id=11,
            countable_orders=0,
            resolved_level=None,
            policy_allowed=False,
            issued=False,
            coupon_id=None,
            code=None,
            discount_type=None,
            discount_value=None,
            expires_at=None,
            min_order_amount=None,
            reason_code=REASON_NO_LEVEL,
        )
    )
    assert facts["issued"] is False
    assert facts["reason"] == REASON_NO_LEVEL
    assert "coupon_code" not in facts
    assert facts["reason"] in CLOSED_REASON_CODES
    state = BrainReplyState()
    attach_customer_request_coupon_facts_to_reply_state(state, facts)
    slim = _slim_known_facts(state.known_facts)
    assert slim["customer_request_coupon_facts"]["issued"] is False
    assert slim["customer_request_coupon_facts"]["reason"] == REASON_NO_LEVEL
    assert slim["answer_contract"]["status"] == "KNOWN_EMPTY"
    assert slim["answer_contract"]["claimable_values"] == []


def test_second_request_reuses_same_assignment(monkeypatch) -> None:
    db, tenant_id, _engine = _make_db()
    customer = _add_customer(db, tenant_id, PHONE_A)
    _add_orders(db, tenant_id, PHONE_A, countable=1)
    pool = _add_pool_coupon(db, tenant_id, "NHBRZ", "bronze")
    spare = _add_pool_coupon(db, tenant_id, "NHSP1", "bronze")
    _enable_canary(monkeypatch, tenant_id)
    handler = CustomerCouponRequestHandler()
    ctx = _ctx(tenant_id=tenant_id, customer_id=customer.id, db=db)
    first = asyncio.run(
        handler.handle(Decision(action=ACTION_CUSTOMER_COUPON_REQUEST, args={}), ctx)
    )
    second = asyncio.run(
        handler.handle(Decision(action=ACTION_CUSTOMER_COUPON_REQUEST, args={}), ctx)
    )
    f1 = first.data["customer_request_coupon_facts"]
    f2 = second.data["customer_request_coupon_facts"]
    assert f1["issued"] is True and f2["issued"] is True
    assert f1["coupon_code"] == f2["coupon_code"] == "NHBRZ"
    assert f2["reused_assignment"] is True
    assert f2["reason"] == REASON_REUSED
    db.refresh(spare)
    assert (spare.extra_metadata or {}).get("customer_id") is None
    db.refresh(pool)
    assert int((pool.extra_metadata or {}).get("customer_id")) == customer.id


def test_tenant_isolation_canary_and_assignments(monkeypatch) -> None:
    from database.models import Tenant, TenantSettings

    db, tenant_a, _engine = _make_db()
    other = Tenant(name="Other Coupon Tenant", is_active=True)
    db.add(other)
    db.flush()
    tenant_b = int(other.id)
    db.add(
        TenantSettings(
            tenant_id=tenant_b,
            ai_settings={"allowed_discount_levels": 40},
            extra_metadata={"coupons_dashboard": {"levels": [], "ai_policy": {"enabled": True}}},
        )
    )
    db.commit()
    customer_a = _add_customer(db, tenant_a, PHONE_A)
    _add_orders(db, tenant_a, PHONE_A, countable=1)
    _add_pool_coupon(db, tenant_a, "NHTNA", "bronze")
    monkeypatch.setenv(ENV_CANARY_TENANTS, str(tenant_a))
    clear_customer_coupon_canary_cache()
    handler = CustomerCouponRequestHandler()
    owned = asyncio.run(
        handler.handle(
            Decision(action=ACTION_CUSTOMER_COUPON_REQUEST, args={}),
            _ctx(tenant_id=tenant_a, customer_id=customer_a.id, db=db),
        )
    )
    blocked = asyncio.run(
        handler.handle(
            Decision(action=ACTION_CUSTOMER_COUPON_REQUEST, args={}),
            _ctx(tenant_id=tenant_b, customer_id=customer_a.id, db=db),
        )
    )
    assert tenant_a != tenant_b
    assert owned.data["service_called"] is True
    assert owned.data["customer_request_coupon_facts"]["issued"] is True
    assert blocked.data["service_called"] is False
    assert blocked.data["customer_request_coupon_facts"]["issued"] is False


def test_customer_isolation_same_tenant(monkeypatch) -> None:
    db, tenant_id, _engine = _make_db()
    customer_a = _add_customer(db, tenant_id, PHONE_A, name="أحمد سالم")
    customer_b = _add_customer(db, tenant_id, PHONE_B, name="نورة عبدالله")
    _add_orders(db, tenant_id, PHONE_A, countable=1)
    _add_orders(db, tenant_id, PHONE_B, countable=1)
    _add_pool_coupon(db, tenant_id, "NHA01", "bronze")
    _add_pool_coupon(db, tenant_id, "NHB01", "bronze")
    _enable_canary(monkeypatch, tenant_id)
    handler = CustomerCouponRequestHandler()
    a = asyncio.run(
        handler.handle(
            Decision(action=ACTION_CUSTOMER_COUPON_REQUEST, args={}),
            _ctx(tenant_id=tenant_id, customer_id=customer_a.id, db=db),
        )
    )
    b = asyncio.run(
        handler.handle(
            Decision(action=ACTION_CUSTOMER_COUPON_REQUEST, args={}),
            _ctx(tenant_id=tenant_id, customer_id=customer_b.id, db=db),
        )
    )
    assert a.data["customer_request_coupon_facts"]["coupon_code"] != b.data[
        "customer_request_coupon_facts"
    ]["coupon_code"]
    assert a.data["customer_request_coupon_facts"]["coupon_code"] == "NHA01"
    assert b.data["customer_request_coupon_facts"]["coupon_code"] == "NHB01"


def test_handler_outside_canary_does_not_call_service(monkeypatch) -> None:
    db, tenant_id, _engine = _make_db()
    customer = _add_customer(db, tenant_id, PHONE_A)
    _add_orders(db, tenant_id, PHONE_A, countable=1)
    _add_pool_coupon(db, tenant_id, "NHBRZ", "bronze")
    monkeypatch.delenv(ENV_CANARY_TENANTS, raising=False)
    clear_customer_coupon_canary_cache()
    called = {"n": 0}

    async def _boom(*_a, **_k):
        called["n"] += 1
        raise AssertionError("service must not run outside canary")

    with patch(
        "modules.ai.brain.execution.customer_coupon_request.issue_customer_coupon",
        _boom,
    ):
        result = asyncio.run(
            CustomerCouponRequestHandler().handle(
                Decision(action=ACTION_CUSTOMER_COUPON_REQUEST, args={}),
                _ctx(tenant_id=tenant_id, customer_id=customer.id, db=db),
            )
        )
    assert called["n"] == 0
    assert result.data["service_called"] is False


def test_gold_not_downgraded_when_policy_blocks(monkeypatch) -> None:
    db, tenant_id, _engine = _make_db()
    customer = _add_customer(db, tenant_id, PHONE_A)
    _add_orders(db, tenant_id, PHONE_A, countable=7)
    _add_pool_coupon(db, tenant_id, "NHSLV", "silver")
    _add_pool_coupon(db, tenant_id, "NHGLD", "gold")
    _enable_canary(monkeypatch, tenant_id)
    result = asyncio.run(
        CustomerCouponRequestHandler().handle(
            Decision(action=ACTION_CUSTOMER_COUPON_REQUEST, args={}),
            _ctx(tenant_id=tenant_id, customer_id=customer.id, db=db),
        )
    )
    facts = result.data["customer_request_coupon_facts"]
    assert facts["issued"] is False
    assert facts["coupon_level"] == "gold"
    assert facts["reason"] == REASON_LEVEL_NOT_ALLOWED_FOR_AI
    assert "coupon_code" not in facts


def test_zero_orders_truthful_when_first_purchase_disabled(monkeypatch) -> None:
    db, tenant_id, _engine = _make_db(first_purchase=False)
    customer = _add_customer(db, tenant_id, PHONE_A)
    _enable_canary(monkeypatch, tenant_id)
    result = asyncio.run(
        CustomerCouponRequestHandler().handle(
            Decision(action=ACTION_CUSTOMER_COUPON_REQUEST, args={}),
            _ctx(tenant_id=tenant_id, customer_id=customer.id, db=db),
        )
    )
    facts = result.data["customer_request_coupon_facts"]
    assert facts["issued"] is False
    assert facts["reason"] == REASON_NO_LEVEL
    assert facts["countable_orders"] == 0


def test_no_phrase_or_regex_model_output_repair() -> None:
    runtime_files = (
        REPO_ROOT / "backend/modules/ai/brain/commerce/customer_coupon_request_owner.py",
        REPO_ROOT / "backend/modules/ai/brain/execution/customer_coupon_request.py",
        REPO_ROOT / "backend/services/customer_request_coupon_canary.py",
        REPO_ROOT / "backend/modules/ai/brain/pipeline.py",
    )
    forbidden = (
        "customer_coupon_request_truth_guard",
        "apply_customer_coupon_request_truth_guard",
        "_FALSE_DENIAL_MARKERS",
        "ما عندنا كوبون",
        "ما عندنا خصم",
        "لا يوجد كوبون",
        "هذا كوبونك",
        r"NH[A-Z0-9]{3}",
        "false_denial_blocked",
        "fabricated_code_blocked",
    )
    for path in runtime_files:
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in text, f"{path.name} contains {token}"
    guard_path = (
        REPO_ROOT
        / "backend/modules/ai/brain/postprocess/customer_coupon_request_truth_guard.py"
    )
    assert not guard_path.exists()


def test_positive_examples_are_test_data_only() -> None:
    runtime = "\n".join(path.read_text(encoding="utf-8") for path in RUNTIME_SCAN_PATHS)
    for utterance in POSITIVE_CLASSIFICATION_EXAMPLES + NEGATIVE_CLASSIFICATION_EXAMPLES:
        assert utterance not in runtime
    assert len(POSITIVE_CLASSIFICATION_EXAMPLES) >= 16
    assert len(NEGATIVE_CLASSIFICATION_EXAMPLES) >= 16


def test_no_issuance_on_negative_classification_cases(monkeypatch) -> None:
    _enable_canary(monkeypatch, 42)
    for utterance in NEGATIVE_CLASSIFICATION_EXAMPLES:
        out = maybe_own_customer_coupon_request_turn(
            Decision(action=ACTION_LLM_REPLY, args={"utterance": utterance}),
            tenant_id=42,
            coupon_capability_telemetry=_telemetry(capability="none", parse_ok=True),
        )
        assert out.action == ACTION_LLM_REPLY, utterance


def test_hesitation_suggest_coupon_unchanged() -> None:
    source = inspect.getsource(DefaultDecisionEngine.decide)
    assert "ACTION_SUGGEST_COUPON" in source
    assert "INTENT_HESITATION" in source
    assert "ACTION_CUSTOMER_COUPON_REQUEST" not in source
    eng = DefaultDecisionEngine()
    ctx = BrainContext(
        tenant_id=3,
        customer_phone=PHONE_A,
        message="متردد في الحذاء الرياضي الأبيض",
        intent=Intent(
            name=INTENT_HESITATION,
            confidence=0.9,
            raw_message="متردد في الحذاء الرياضي الأبيض",
        ),
        state=MerchantConversationState(
            greeted=True,
            current_product_focus={"id": 1, "title": "حذاء رياضي أبيض", "price": 120},
        ),
        facts=CommerceFacts(
            has_products=True,
            product_count=5,
            in_stock_count=5,
            has_coupons=True,
            has_active_integration=True,
            orderable=True,
        ),
    )
    decision = eng.decide(ctx)
    assert decision.action == ACTION_SUGGEST_COUPON


def test_campaign_autopilot_salla_sync_owners_untouched() -> None:
    owner = (REPO_ROOT / "backend/modules/ai/brain/commerce/customer_coupon_request_owner.py").read_text(
        encoding="utf-8"
    )
    handler = (REPO_ROOT / "backend/modules/ai/brain/execution/customer_coupon_request.py").read_text(
        encoding="utf-8"
    )
    for blob in (owner, handler):
        assert "pick_coupon_for_segment" not in blob
        assert "pick_coupon_for_level" not in blob
        assert "salla_coupons_poller" not in blob
        assert "ACTION_SUGGEST_COUPON" not in blob


def test_product_card_owners_not_imported_by_2c_runtime() -> None:
    files = (
        REPO_ROOT / "backend/modules/ai/brain/commerce/customer_coupon_request_owner.py",
        REPO_ROOT / "backend/modules/ai/brain/execution/customer_coupon_request.py",
        REPO_ROOT / "backend/services/customer_request_coupon_canary.py",
    )
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert "product_card" not in text
        assert "catalog_navigate" not in text
        assert "presented_identity" not in text


def test_no_canned_arabic_in_handler_or_owner() -> None:
    for rel in (
        "backend/modules/ai/brain/commerce/customer_coupon_request_owner.py",
        "backend/modules/ai/brain/execution/customer_coupon_request.py",
    ):
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert "هذا كوبونك" not in text
        assert "ما عندنا كوبون" not in text
        assert "ما عندنا خصم" not in text


@pytest.mark.skipif(
    os.getenv("NAHLA_CUSTOMER_COUPON_PROBE_LIVE_EVAL", "").strip() not in {"1", "true", "yes"},
    reason="live tiny-model probe eval is owner-gated, not CI",
)
def test_live_tiny_model_probe_matrix_optional() -> None:
    from modules.ai.brain.intent.coupon_capability_probe import run_coupon_capability_probe
    from modules.ai.orchestrator.customer_chat_models import resolve_tiny_customer_chat_model

    model = resolve_tiny_customer_chat_model()
    latencies = []
    failures = 0
    positives_ok = 0
    negatives_ok = 0
    for utterance in POSITIVE_CLASSIFICATION_EXAMPLES:
        result = asyncio.run(run_coupon_capability_probe(utterance))
        latencies.append(int(result.get("coupon_capability_probe_ms") or 0))
        if not result.get("coupon_capability_parse_ok"):
            failures += 1
        if result.get("coupon_capability") == "customer_coupon_request":
            positives_ok += 1
    for utterance in NEGATIVE_CLASSIFICATION_EXAMPLES:
        result = asyncio.run(run_coupon_capability_probe(utterance))
        latencies.append(int(result.get("coupon_capability_probe_ms") or 0))
        if not result.get("coupon_capability_parse_ok"):
            failures += 1
        if result.get("coupon_capability") == "none":
            negatives_ok += 1
    sample = len(latencies)
    avg = sum(latencies) / sample if sample else 0
    p95 = sorted(latencies)[max(0, int(sample * 0.95) - 1)] if sample else 0
    print(
        f"PROBE_MODEL={model} SAMPLE={sample} AVG_MS={avg:.1f} P95_MS={p95} "
        f"FAIL_RATE={failures / sample if sample else 1:.3f} "
        f"POS_PASS={positives_ok}/{len(POSITIVE_CLASSIFICATION_EXAMPLES)} "
        f"NEG_PASS={negatives_ok}/{len(NEGATIVE_CLASSIFICATION_EXAMPLES)}"
    )
    assert positives_ok == len(POSITIVE_CLASSIFICATION_EXAMPLES)
    assert negatives_ok == len(NEGATIVE_CLASSIFICATION_EXAMPLES)
