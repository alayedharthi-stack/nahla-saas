"""Test-only harness for Trusted Context Layer 1 mass validation."""
from __future__ import annotations

import asyncio
import json
import logging
import statistics
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

from modules.ai.brain.truth_surface.contract import (
    TrustedContextSnapshot,
    TrustedDomain,
    TrustedFact,
)
from modules.ai.brain.truth_surface.coupon_offer_loader import (
    build_coupon_eligibility_record,
    build_promotion_eligibility_record,
    load_coupon_promotion_facts,
    mask_coupon_code,
    should_load_coupon_promotion_facts,
)
from modules.ai.brain.truth_surface.trusted_context import (
    clear_trusted_context,
    current_trusted_context,
    pop_shadow_build_error_class,
    run_trusted_context_shadow,
    safe_shadow_trace_metadata,
)
from services.turn_trace import new_trace as _REAL_NEW_TRACE

FORBIDDEN_PRIVACY_MARKERS = (
    "SECRET_COUPON_ABC123",
    "PRIVATE_CUSTOMER_VALUE",
    "RAW_PROMO_CONDITION_SECRET",
    "secret-value",
    "secret body",
)


@dataclass(frozen=True)
class Layer1Scenario:
    scenario_id: str
    family: str
    contract_under_test: str
    tenant_id: int = 201
    customer_id: Optional[int] = 11
    conversation_id: int = 501
    customer_phone: str = "966500000201"
    inbound_text: str = ""
    history: Tuple[Dict[str, str], ...] = ()
    brain_state: Optional[Dict[str, Any]] = None
    inbound_metadata: Optional[Dict[str, Any]] = None
    coupons: Tuple[Dict[str, Any], ...] = ()
    promotions: Tuple[Dict[str, Any], ...] = ()
    coupon_seed: Optional[Dict[str, Any]] = None
    promotion_seed: Optional[Dict[str, Any]] = None
    eligibility_target: str = ""  # coupon | promotion | loader | build | lifecycle | relevance
    basket_total: Optional[float] = None
    applied_codes: Tuple[str, ...] = ()
    customer_profile: Optional[Dict[str, Any]] = None
    loader_side_effect: Optional[BaseException] = None
    force_offer_loader: Optional[bool] = None
    expected_status: str = "success"  # success | build_error
    expected_error_class: str = ""
    expected_domains_loaded: Tuple[str, ...] = ()
    expected_domains_not_loaded: Tuple[str, ...] = ()
    expected_eligible: Optional[bool] = None
    expected_verified: Optional[bool] = None
    expected_reason: str = ""
    expected_lazy_load: Optional[bool] = None
    handler_path: bool = False
    shadow_enabled: bool = True
    equivalence_pair: str = ""
    privacy_secrets: Tuple[str, ...] = ()
    allow_code_in_snapshot: bool = False
    lifecycle_action: str = ""
    duplicate_turn_calls: int = 1
    concurrent_turn: bool = False
    tenant_b_coupons: Tuple[Dict[str, Any], ...] = ()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def coupon_record(**overrides: Any) -> SimpleNamespace:
    defaults = dict(
        id=1,
        tenant_id=201,
        code="SAVE10",
        source_type="manual",
        expires_at=utcnow() + timedelta(days=7),
        extra_metadata={},
        rules=[],
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def promotion_record(**overrides: Any) -> SimpleNamespace:
    defaults = dict(
        id=10,
        tenant_id=201,
        status="active",
        promotion_type="percentage",
        discount_value=10,
        conditions={},
        starts_at=utcnow() - timedelta(hours=1),
        ends_at=utcnow() + timedelta(days=7),
        usage_count=0,
        usage_limit=None,
        extra_metadata={},
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class PerfCollector:
    def __init__(self) -> None:
        self.durations_ms: List[float] = []
        self.started_at = time.perf_counter()

    def record(self, duration_ms: float) -> None:
        self.durations_ms.append(duration_ms)

    def summary(self) -> Dict[str, float]:
        if not self.durations_ms:
            return {"count": 0, "p50": 0.0, "p95": 0.0, "max": 0.0, "total_suite_s": 0.0}
        ordered = sorted(self.durations_ms)
        return {
            "count": float(len(ordered)),
            "p50": statistics.median(ordered),
            "p95": ordered[max(0, int(len(ordered) * 0.95) - 1)],
            "max": ordered[-1],
            "total_suite_s": time.perf_counter() - self.started_at,
        }


class _FakeQuery:
    def __init__(self, *, model_name: str, coupons: Sequence[Any], promotions: Sequence[Any], profiles: Sequence[Any]):
        self._model_name = model_name
        self._coupons = list(coupons)
        self._promotions = list(promotions)
        self._profiles = list(profiles)
        self._tenant_id: Optional[int] = None
        self._limit: Optional[int] = None
        self._profile_filters: Dict[str, Any] = {}

    def filter(self, *criteria: Any) -> "_FakeQuery":
        for criterion in criteria:
            left = getattr(criterion, "left", None)
            right = getattr(criterion, "right", None)
            key = getattr(left, "key", None) or getattr(left, "name", None)
            value = getattr(right, "value", right)
            if key == "tenant_id" and value is not None:
                self._tenant_id = int(value)
            elif key in {"id", "customer_id"} and value is not None:
                self._profile_filters[str(key)] = int(value)
        return self

    def limit(self, count: int) -> "_FakeQuery":
        self._limit = int(count)
        return self

    def all(self) -> List[Any]:
        if self._model_name == "Coupon":
            rows = self._coupons
        elif self._model_name == "Promotion":
            rows = self._promotions
        else:
            rows = self._profiles
        if self._tenant_id is not None and self._model_name in {"Coupon", "Promotion"}:
            rows = [row for row in rows if int(getattr(row, "tenant_id", 0) or 0) == self._tenant_id]
        if self._profile_filters and self._model_name == "CustomerProfile":
            for key, value in self._profile_filters.items():
                rows = [row for row in rows if int(getattr(row, key, -1) or -1) == value]
        if self._limit is not None:
            rows = rows[: self._limit]
        return list(rows)

    def first(self) -> Any:
        rows = self.all()
        return rows[0] if rows else None


def tenant_scoped_db(
    *,
    coupons: Sequence[Any] = (),
    promotions: Sequence[Any] = (),
    profiles: Sequence[Any] = (),
) -> MagicMock:
    db = MagicMock()
    db.commit = MagicMock()
    db.add = MagicMock()
    db.flush = MagicMock()

    def _query(model: Any) -> _FakeQuery:
        return _FakeQuery(
            model_name=getattr(model, "__name__", str(model)),
            coupons=coupons,
            promotions=promotions,
            profiles=profiles,
        )

    db.query.side_effect = _query
    return db


def records_from_seed(seed: Optional[Dict[str, Any]], factory: Callable[..., SimpleNamespace]) -> List[SimpleNamespace]:
    if not seed:
        return []
    return [factory(**seed)]


def coupons_from_scenario(scenario: Layer1Scenario) -> List[SimpleNamespace]:
    rows: List[SimpleNamespace] = []
    for item in scenario.coupons:
        seed = dict(item)
        seed.setdefault("tenant_id", scenario.tenant_id)
        rows.append(coupon_record(**seed))
    return rows


def promotions_from_scenario(scenario: Layer1Scenario) -> List[SimpleNamespace]:
    rows: List[SimpleNamespace] = []
    for item in scenario.promotions:
        seed = dict(item)
        seed.setdefault("tenant_id", scenario.tenant_id)
        rows.append(promotion_record(**seed))
    return rows


def tenant_b_coupons_from_scenario(scenario: Layer1Scenario) -> List[SimpleNamespace]:
    return [coupon_record(**dict(item)) for item in scenario.tenant_b_coupons]


def line_product_ids_from_scenario(scenario: Layer1Scenario) -> Optional[Set[str]]:
    if not scenario.brain_state:
        return None
    items = scenario.brain_state.get("line_items") or []
    ids = {str(item.get("product_id")) for item in items if item.get("product_id") is not None}
    return ids or None


def profile_from_scenario(scenario: Layer1Scenario) -> List[SimpleNamespace]:
    if not scenario.customer_profile:
        return []
    return [SimpleNamespace(tenant_id=scenario.tenant_id, **scenario.customer_profile)]


@contextmanager
def patch_base_snapshot_loaders():
    from modules.ai.brain.truth_surface import trusted_context

    with patch.object(trusted_context, "_load_customer_order_facts", return_value=[]), patch.object(
        trusted_context, "_load_state_order_facts", return_value=[]
    ), patch.object(trusted_context, "_load_payment_shipment_facts", return_value=[]), patch.object(
        trusted_context, "_load_capability_facts", return_value=[]
    ), patch.object(trusted_context, "_load_merchant_policy_facts", return_value=[]), patch(
        "core.active_order_context.load_commerce_bundle_from_db", return_value={}
    ):
        yield


def assert_no_privacy_leak(text: str, *, allow_code: Optional[str] = None) -> None:
    for marker in FORBIDDEN_PRIVACY_MARKERS:
        assert marker not in text
    if allow_code:
        return
    if "SECRET_COUPON_ABC123" in text:
        raise AssertionError("raw coupon secret leaked")


def snapshot_domains(snapshot: TrustedContextSnapshot) -> Set[str]:
    return set(snapshot.loaded_domains or [])


def fact_domains(snapshot: TrustedContextSnapshot) -> Set[str]:
    return {fact.domain.value if hasattr(fact.domain, "value") else str(fact.domain) for fact in snapshot.facts}


def serialized_snapshot_for_leak_check(snapshot: TrustedContextSnapshot) -> str:
    return json.dumps(snapshot.to_log_dict(), ensure_ascii=False)


def run_relevance_contract(scenario: Layer1Scenario) -> None:
    state = SimpleNamespace(order_prep=dict(scenario.brain_state or {}), current_product_focus=None)
    lazy = should_load_coupon_promotion_facts(
        message=scenario.inbound_text,
        brain_state=state,
        inbound_metadata=dict(scenario.inbound_metadata or {}),
    )
    assert lazy is scenario.expected_lazy_load


def run_eligibility_contract(scenario: Layer1Scenario, perf: PerfCollector) -> None:
    started = time.perf_counter()
    observed = utcnow().isoformat()
    if scenario.eligibility_target == "coupon":
        coupon_seed = dict(scenario.coupon_seed or {})
        coupon_seed.setdefault("tenant_id", scenario.tenant_id)
        record = build_coupon_eligibility_record(
            coupon_record(**coupon_seed),
            tenant_id=scenario.tenant_id,
            customer_id=scenario.customer_id,
            basket_total=scenario.basket_total,
            applied_codes=set(scenario.applied_codes),
            observed_at=observed,
            line_product_ids=line_product_ids_from_scenario(scenario),
        )
    else:
        profile = SimpleNamespace(**scenario.customer_profile) if scenario.customer_profile else None
        promo_seed = dict(scenario.promotion_seed or {})
        promo_seed.setdefault("tenant_id", scenario.tenant_id)
        record = build_promotion_eligibility_record(
            promotion_record(**promo_seed),
            tenant_id=scenario.tenant_id,
            customer_profile=profile,
            basket_total=scenario.basket_total,
            observed_at=observed,
        )
    perf.record((time.perf_counter() - started) * 1000)
    if scenario.expected_eligible is not None:
        assert record["eligible"] is scenario.expected_eligible
    if scenario.expected_verified is not None:
        assert record["verified"] is scenario.expected_verified
    if scenario.expected_reason:
        assert record.get("reason_when_unavailable") == scenario.expected_reason


def run_loader_contract(scenario: Layer1Scenario, perf: PerfCollector) -> None:
    started = time.perf_counter()
    all_coupons = coupons_from_scenario(scenario) + tenant_b_coupons_from_scenario(scenario)
    db = tenant_scoped_db(
        coupons=all_coupons,
        promotions=promotions_from_scenario(scenario),
        profiles=profile_from_scenario(scenario),
    )
    prep = dict(scenario.brain_state or {})
    if scenario.basket_total is not None:
        prep.setdefault("catalog_checkout_total", scenario.basket_total)
    state = SimpleNamespace(order_prep=prep, current_product_focus=None)
    patchers: List[Any] = []
    if scenario.loader_side_effect is not None:
        patchers.append(
            patch(
                "modules.ai.brain.truth_surface.coupon_offer_loader.load_coupon_promotion_facts",
                side_effect=scenario.loader_side_effect,
            )
        )
    else:
        patchers.append(
            patch(
                "modules.ai.brain.truth_surface.coupon_offer_loader.should_load_coupon_promotion_facts",
                return_value=scenario.force_offer_loader is not False,
            )
        )
    for item in patchers:
        item.start()
    try:
        if scenario.loader_side_effect:
            with pytest_raises_build_error():
                load_coupon_promotion_facts(
                    db=db,
                    tenant_id=scenario.tenant_id,
                    customer_phone=scenario.customer_phone,
                    message=scenario.inbound_text,
                    brain_state=state,
                    inbound_metadata=dict(scenario.inbound_metadata or {}),
                    conversation=SimpleNamespace(id=scenario.conversation_id, customer_id=scenario.customer_id),
                )
        else:
            facts, obs = load_coupon_promotion_facts(
                db=db,
                tenant_id=scenario.tenant_id,
                customer_phone=scenario.customer_phone,
                message=scenario.inbound_text,
                brain_state=state,
                inbound_metadata=dict(scenario.inbound_metadata or {}),
                conversation=SimpleNamespace(id=scenario.conversation_id, customer_id=scenario.customer_id),
            )
            if scenario.contract_under_test == "tenant_scoped_coupon_query":
                coupon_ids = {
                    int(f.value.get("coupon_id"))
                    for f in facts
                    if f.domain == TrustedDomain.COUPONS and isinstance(f.value, dict) and f.value.get("coupon_id")
                }
                foreign_ids = {coupon_record(**item).id for item in scenario.tenant_b_coupons}
                assert not coupon_ids.intersection(foreign_ids)
            if scenario.expected_domains_loaded:
                domains = {f.domain.value for f in facts}
                for domain in scenario.expected_domains_loaded:
                    assert domain in domains
            assert "SECRET_COUPON_ABC123" not in json.dumps(obs, ensure_ascii=False)
            db.commit.assert_not_called()
            db.add.assert_not_called()
    finally:
        for item in reversed(patchers):
            item.stop()
    perf.record((time.perf_counter() - started) * 1000)


@contextmanager
def pytest_raises_build_error():
    try:
        yield
    except Exception as exc:
        assert exc.__class__.__name__ in {"RuntimeError", "TimeoutError"}
    else:
        raise AssertionError("expected loader failure")


def run_build_or_shadow_contract(scenario: Layer1Scenario, perf: PerfCollector) -> TrustedContextSnapshot | None:
    from modules.ai.brain.truth_surface import trusted_context

    started = time.perf_counter()
    clear_trusted_context()
    all_coupons = coupons_from_scenario(scenario) + tenant_b_coupons_from_scenario(scenario)
    db = tenant_scoped_db(
        coupons=all_coupons,
        promotions=promotions_from_scenario(scenario),
        profiles=profile_from_scenario(scenario),
    )
    prep = dict(scenario.brain_state or {})
    if scenario.basket_total is not None:
        prep.setdefault("catalog_checkout_total", scenario.basket_total)
    state = SimpleNamespace(order_prep=prep, current_product_focus=None)
    patches: List[Any] = [
        patch(
            "modules.ai.brain.truth_surface.trusted_context.is_trusted_context_shadow_enabled",
            return_value=scenario.shadow_enabled,
        ),
    ]
    if scenario.loader_side_effect is not None:
        patches.append(
            patch(
                "modules.ai.brain.truth_surface.coupon_offer_loader.load_coupon_promotion_facts",
                side_effect=scenario.loader_side_effect,
            )
        )
    if scenario.force_offer_loader is True:
        patches.append(
            patch(
                "modules.ai.brain.truth_surface.coupon_offer_loader.should_load_coupon_promotion_facts",
                return_value=True,
            )
        )
    if scenario.force_offer_loader is False:
        patches.append(
            patch(
                "modules.ai.brain.truth_surface.coupon_offer_loader.should_load_coupon_promotion_facts",
                return_value=False,
            )
        )

    with patch_base_snapshot_loaders():
        for item in patches:
            item.start()
        try:
            if scenario.lifecycle_action:
                return _run_lifecycle_action(scenario, db, state, trusted_context)
            if scenario.expected_status == "build_error":
                with patch("modules.ai.brain.truth_surface.trusted_context.logger") as logger:
                    result = run_trusted_context_shadow(
                        db=db,
                        tenant_id=scenario.tenant_id,
                        customer_phone=scenario.customer_phone,
                        message=scenario.inbound_text,
                        conversation_id=scenario.conversation_id,
                        brain_state=state,
                        inbound_metadata=dict(scenario.inbound_metadata or {}),
                    )
                warning_text = " ".join(str(arg) for call in logger.warning.call_args_list for arg in call.args)
                assert result is None
                assert scenario.expected_error_class in warning_text
                for secret in scenario.privacy_secrets:
                    assert secret not in warning_text
                assert logger.exception.call_count == 0
                assert pop_shadow_build_error_class() == scenario.expected_error_class
                pop_shadow_build_error_class()
                db.commit.assert_not_called()
                db.add.assert_not_called()
                perf.record((time.perf_counter() - started) * 1000)
                return None

            snap = trusted_context.build_trusted_context_snapshot(
                db=db,
                tenant_id=scenario.tenant_id,
                customer_phone=scenario.customer_phone,
                message=scenario.inbound_text,
                conversation_id=scenario.conversation_id,
                brain_state=state,
                inbound_metadata=dict(scenario.inbound_metadata or {}),
            )
            if scenario.shadow_enabled:
                result = run_trusted_context_shadow(
                    db=db,
                    tenant_id=scenario.tenant_id,
                    customer_phone=scenario.customer_phone,
                    message=scenario.inbound_text,
                    conversation_id=scenario.conversation_id,
                    brain_state=state,
                    inbound_metadata=dict(scenario.inbound_metadata or {}),
                )
                assert result is not None
            for domain in scenario.expected_domains_not_loaded:
                assert domain not in snapshot_domains(snap)
                assert domain not in fact_domains(snap)
            for domain in scenario.expected_domains_loaded:
                assert domain in fact_domains(snap) or domain in snapshot_domains(snap)
            leak_text = serialized_snapshot_for_leak_check(snap)
            for secret in scenario.privacy_secrets:
                if scenario.allow_code_in_snapshot and secret == "SECRET_COUPON_ABC123":
                    continue
                assert secret not in leak_text
            trace = safe_shadow_trace_metadata(snap)
            for secret in scenario.privacy_secrets:
                assert secret not in json.dumps(trace, ensure_ascii=False)
            db.commit.assert_not_called()
            db.add.assert_not_called()
            perf.record((time.perf_counter() - started) * 1000)
            return snap
        finally:
            for item in reversed(patches):
                item.stop()
            clear_trusted_context()


def _run_lifecycle_action(
    scenario: Layer1Scenario,
    db: MagicMock,
    state: SimpleNamespace,
    trusted_context: Any,
) -> TrustedContextSnapshot | None:
    with patch(
        "modules.ai.brain.truth_surface.trusted_context.is_trusted_context_shadow_enabled",
        return_value=True,
    ), patch(
        "modules.ai.brain.truth_surface.coupon_offer_loader.should_load_coupon_promotion_facts",
        return_value=False,
    ):
        if scenario.lifecycle_action == "duplicate_same_turn":
            first = run_trusted_context_shadow(
                db=db,
                tenant_id=scenario.tenant_id,
                customer_phone=scenario.customer_phone,
                conversation_id=scenario.conversation_id,
                message=scenario.inbound_text,
                brain_state=state,
            )
            second = run_trusted_context_shadow(
                db=db,
                tenant_id=scenario.tenant_id,
                customer_phone=scenario.customer_phone,
                conversation_id=scenario.conversation_id,
                message=scenario.inbound_text,
                brain_state=state,
            )
            assert first is second
            return second
        if scenario.lifecycle_action == "new_turn_new_snapshot":
            first = run_trusted_context_shadow(
                db=db,
                tenant_id=scenario.tenant_id,
                customer_phone=scenario.customer_phone,
                conversation_id=scenario.conversation_id,
                message=scenario.inbound_text,
                brain_state=state,
            )
            clear_trusted_context()
            second = run_trusted_context_shadow(
                db=db,
                tenant_id=scenario.tenant_id,
                customer_phone=scenario.customer_phone,
                conversation_id=scenario.conversation_id + 1,
                message=scenario.inbound_text,
                brain_state=state,
            )
            assert first.snapshot_id != second.snapshot_id
            return second
        if scenario.lifecycle_action == "concurrent_isolation":

            async def _turn(phone: str, conversation_id: int) -> Tuple[str, str]:
                clear_trusted_context()
                snap = _snapshot_for_lifecycle(scenario.tenant_id, phone, conversation_id)
                with patch(
                    "modules.ai.brain.truth_surface.trusted_context.build_trusted_context_snapshot",
                    return_value=snap,
                ):
                    result = run_trusted_context_shadow(
                        db=db,
                        tenant_id=scenario.tenant_id,
                        customer_phone=phone,
                        conversation_id=conversation_id,
                        message=scenario.inbound_text,
                        brain_state=state,
                    )
                current = current_trusted_context()
                return result.snapshot_id, current.snapshot_id if current else ""

            async def _run() -> List[Tuple[str, str]]:
                first = await _turn("966500000301", 601)
                second = await _turn("966500000302", 602)
                return [first, second]

            pairs = asyncio.run(_run())
            clear_trusted_context()
            assert pairs[0][0] == pairs[0][1]
            assert pairs[1][0] == pairs[1][1]
            assert pairs[0][0] != pairs[1][0]
            return None
        if scenario.lifecycle_action == "failure_then_success":
            clear_trusted_context()
            with patch(
                "modules.ai.brain.truth_surface.coupon_offer_loader.should_load_coupon_promotion_facts",
                return_value=True,
            ), patch(
                "modules.ai.brain.truth_surface.coupon_offer_loader.load_coupon_promotion_facts",
                side_effect=RuntimeError("secret-value"),
            ):
                failed = run_trusted_context_shadow(
                    db=db,
                    tenant_id=scenario.tenant_id,
                    customer_phone=scenario.customer_phone,
                    conversation_id=scenario.conversation_id,
                    message="عندكم كوبون؟",
                    brain_state=state,
                )
            assert failed is None
            assert pop_shadow_build_error_class() == "RuntimeError"
            with patch(
                "modules.ai.brain.truth_surface.coupon_offer_loader.should_load_coupon_promotion_facts",
                return_value=True,
            ):
                ok = run_trusted_context_shadow(
                    db=db,
                    tenant_id=scenario.tenant_id,
                    customer_phone=scenario.customer_phone,
                    conversation_id=scenario.conversation_id + 1,
                    message="عندكم كوبون؟",
                    brain_state=state,
                )
            assert ok is not None
            clear_trusted_context()
            return ok
    raise AssertionError(f"unknown lifecycle_action={scenario.lifecycle_action}")


def _snapshot_for_lifecycle(tenant_id: int, phone: str, conversation_id: int) -> TrustedContextSnapshot:
    snap = TrustedContextSnapshot(
        tenant_id=tenant_id,
        customer_phone=phone,
        conversation_id=conversation_id,
        facts=[],
        loaded_domains=["customer"],
        sources=["test"],
    )
    snap.ensure_snapshot_id()
    return snap


@contextmanager
def merchant_handler_patch_ctx(
    *,
    convo: SimpleNamespace,
    shadow_enabled: bool = True,
    whatsapp_send_mock: Optional[AsyncMock] = None,
):
    from contextlib import ExitStack

    state = SimpleNamespace(turn=0, stage="active", order_prep={})
    send_mock = whatsapp_send_mock or AsyncMock(return_value=True)
    with ExitStack() as stack:
        stack.enter_context(patch(
            "core.ai_disabled_gate.is_ai_disabled_for_conversation",
            return_value=SimpleNamespace(disabled=False, reason=None, conversation=convo),
        ))
        stack.enter_context(patch(
            "modules.operations.structured_admin_contact_policy.evaluate_structured_admin_contact_policy",
            return_value=None,
        ))
        stack.enter_context(patch(
            "routers.conversations._get_or_create_conversation",
            return_value=convo,
        ))
        stack.enter_context(patch("routers.whatsapp_webhook.StateManager.save_message"))
        stack.enter_context(patch(
            "routers.whatsapp_webhook.StateManager.load_history",
            return_value=list(convo.__dict__.get("_history", [])),
        ))
        stack.enter_context(patch(
            "routers.whatsapp_webhook.StateManager.load",
            return_value=state,
        ))
        stack.enter_context(patch("routers.whatsapp_webhook.StateManager.save"))
        stack.enter_context(patch(
            "core.wa_usage.check_limit",
            return_value=SimpleNamespace(allowed=True, used_total=0, limit=1000, reason=""),
        ))
        stack.enter_context(patch(
            "modules.ai.brain.commerce.conversational_priority.has_payment_outbound_consent",
            return_value=False,
        ))
        mock_brain = stack.enter_context(patch("modules.ai.brain.pipeline.get_brain"))
        stack.enter_context(patch("modules.ai.routing.conversation_mode.resolve_conversation_mode"))
        stack.enter_context(patch("modules.ai.routing.conversation_mode.save_lease"))
        stack.enter_context(patch(
            "core.ownership_state.resolve_ownership_state",
            return_value=SimpleNamespace(state="ai_active", takeover_class=""),
        ))
        stack.enter_context(patch(
            "core.ownership_state.attempt_implicit_takeover_recovery",
            return_value=SimpleNamespace(released=False, reason=""),
        ))
        stack.enter_context(patch(
            "core.ai_pause_guard.should_skip_ai",
            return_value=(False, None),
        ))
        stack.enter_context(patch(
            "modules.ai.order_flow_v2.owner.try_handle_order_flow_v2",
            return_value=SimpleNamespace(handled=False, reason="not_handled"),
        ))
        stack.enter_context(patch(
            "modules.ai.brain.commerce.inbound_fragment_guard.evaluate_duplicate_fragment_turn",
            return_value=SimpleNamespace(process_turn=True, send_clarification_once=False, reason=""),
        ))
        stack.enter_context(patch("core.store_knowledge.build_ai_context", return_value={}))
        stack.enter_context(patch(
            "routers.whatsapp_webhook._send_whatsapp_message",
            new=send_mock,
        ))
        stack.enter_context(patch(
            "services.whatsapp_platform.service.provider_post_with_context",
            new=AsyncMock(return_value={"messages": [{"id": "wamid.test"}]}),
        ))
        stack.enter_context(patch(
            "services.whatsapp_platform.service.get_token_for_operation",
            new=AsyncMock(return_value=MagicMock(token="tok", source="test")),
        ))
        cis_mock = stack.enter_context(patch("services.customer_intelligence.CustomerIntelligenceService"))
        cis_mock.return_value.upsert_lead_customer.return_value = SimpleNamespace(id=7, name="", email="")
        cis_mock.return_value.ensure_profile.return_value = SimpleNamespace(
            segment="",
            customer_status="",
            rfm_segment="",
            is_returning=False,
            total_orders=0,
            total_spend_sar=0.0,
            last_order_at=None,
        )
        stack.enter_context(patch(
            "modules.ai.brain.truth_surface.flags.is_trusted_context_shadow_enabled",
            return_value=shadow_enabled,
        ))
        stack.enter_context(patch(
            "modules.ai.brain.truth_surface.trusted_context.is_trusted_context_shadow_enabled",
            return_value=shadow_enabled,
        ))
        stack.enter_context(patch("routers.whatsapp_webhook.MERCHANT_BRAIN_ENABLED", True))
        stack.enter_context(patch("core.billing.has_billing_access", return_value=True))
        yield mock_brain, state, send_mock


def run_handler_contract(scenario: Layer1Scenario, perf: PerfCollector) -> None:
    from modules.ai.brain.truth_surface import trusted_context
    from routers.whatsapp_webhook import _handle_merchant_message

    started = time.perf_counter()
    clear_trusted_context()
    convo = SimpleNamespace(
        id=scenario.conversation_id,
        tenant_id=scenario.tenant_id,
        customer_id=scenario.customer_id,
        ai_paused=False,
        ai_paused_reason=None,
        is_human_handoff=False,
        needs_human=False,
        handoff_active=False,
        paused_by_human=False,
        taken_over_at=None,
        taken_over_by=None,
        status="active",
        extra_metadata={},
        _history=list(scenario.history),
    )
    db = tenant_scoped_db(
        coupons=coupons_from_scenario(scenario),
        promotions=promotions_from_scenario(scenario),
        profiles=profile_from_scenario(scenario),
    )
    send_mock = AsyncMock(return_value=True)
    captured_traces: List[Any] = []
    brain_reply = "رد ثابت للتحقق"

    def _capture_new_trace(**kwargs: Any) -> Any:
        trace = _REAL_NEW_TRACE(**kwargs)
        captured_traces.append(trace)
        return trace

    patchers = [
        patch.object(trusted_context, "_load_customer_order_facts", return_value=[]),
        patch.object(trusted_context, "_load_state_order_facts", return_value=[]),
        patch.object(trusted_context, "_load_payment_shipment_facts", return_value=[]),
        patch.object(trusted_context, "_load_capability_facts", return_value=[]),
        patch.object(trusted_context, "_load_merchant_policy_facts", return_value=[]),
        patch("core.active_order_context.load_commerce_bundle_from_db", return_value={}),
        patch("services.turn_trace.new_trace", side_effect=_capture_new_trace),
    ]
    if scenario.loader_side_effect is not None:
        patchers.append(
            patch(
                "modules.ai.brain.truth_surface.coupon_offer_loader.load_coupon_promotion_facts",
                side_effect=scenario.loader_side_effect,
            )
        )
    if scenario.force_offer_loader is True:
        patchers.append(
            patch(
                "modules.ai.brain.truth_surface.coupon_offer_loader.should_load_coupon_promotion_facts",
                return_value=True,
            )
        )

    for item in patchers:
        item.start()
    try:
        with merchant_handler_patch_ctx(
            convo=convo,
            shadow_enabled=scenario.shadow_enabled,
            whatsapp_send_mock=send_mock,
        ) as (mock_brain, _state, _send):
            mock_brain.return_value.process = AsyncMock(return_value={"reply": brain_reply, "buttons": []})
            asyncio.run(_handle_merchant_message(
                phone_id="PH1",
                to=scenario.customer_phone,
                text=scenario.inbound_text,
                tenant_id=scenario.tenant_id,
                db=db,
            ))
            mock_brain.return_value.process.assert_called_once()
            brain_kwargs = mock_brain.return_value.process.call_args.kwargs
            assert "trusted_context" not in brain_kwargs
            assert "projection" not in brain_kwargs
            assert "known_facts" not in brain_kwargs
            if scenario.expected_status == "build_error":
                assert captured_traces
                extra = captured_traces[0].extra
                assert extra.get("trusted_context_shadow_status") == "build_error"
                assert extra.get("trusted_context_shadow_error_class") == scenario.expected_error_class
                assert extra.get("trusted_context_shadow_stage") == "build"
            send_mock.assert_awaited()
            assert current_trusted_context() is None
            for secret in scenario.privacy_secrets:
                assert secret not in json.dumps(captured_traces[0].extra if captured_traces else {}, ensure_ascii=False)
    finally:
        for item in reversed(patchers):
            item.stop()
        clear_trusted_context()
    perf.record((time.perf_counter() - started) * 1000)


def execute_scenario(scenario: Layer1Scenario, perf: PerfCollector) -> None:
    clear_trusted_context()
    pop_shadow_build_error_class()
    try:
        if scenario.handler_path:
            run_handler_contract(scenario, perf)
            return
        if scenario.eligibility_target in {"coupon", "promotion"}:
            run_eligibility_contract(scenario, perf)
            return
        if scenario.expected_lazy_load is not None:
            run_relevance_contract(scenario)
            return
        if scenario.eligibility_target == "loader":
            run_loader_contract(scenario, perf)
            return
        run_build_or_shadow_contract(scenario, perf)
    finally:
        clear_trusted_context()
        pop_shadow_build_error_class()


def unique_contracts(scenarios: Sequence[Layer1Scenario]) -> Set[str]:
    return {scenario.contract_under_test for scenario in scenarios}
