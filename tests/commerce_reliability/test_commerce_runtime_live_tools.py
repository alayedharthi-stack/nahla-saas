"""The merchant's real read tools as the loop sees them (no database, no model).

Each case drives the registered tool through the loop's own ``ToolRegistry``,
with the underlying Commerce Agent V2 read implementation replaced by a double.
What is proved here is the boundary: the scope cannot be moved, the allowlist
holds nothing that writes, provenance survives the projection, an empty answer
stays an empty answer, and a call the loop abandoned closes the binding instead
of sharing a database session with a thread nobody is waiting for.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, List

import pytest

from core.commerce_runtime import agent_contracts as ac
from core.commerce_runtime import agent_live_tools as alt
from core.commerce_runtime import agent_tools as at
from core.commerce_runtime import conversation_link as cl

TENANT = 77
CONVERSATION = 501
SCOPE = at.ToolScope(tenant_id=TENANT, namespace="live", conversation_id=CONVERSATION, turn_id=9)
OTHER_SCOPE = at.ToolScope(tenant_id=TENANT + 1, namespace="live", conversation_id=CONVERSATION, turn_id=9)


class Snapshot:
    """A stand-in for the V2 pydantic snapshots: attributes only."""

    def __init__(self, **fields: Any) -> None:
        for key, value in fields.items():
            setattr(self, key, value)


class Record:
    def __init__(self, ref: str) -> None:
        self.ref = ref


def product(product_id: int = 1, **overrides: Any) -> Snapshot:
    fields: Dict[str, Any] = {
        "product_id": product_id, "evidence_ref": f"catalog:product:{product_id}",
        "title": "حذاء رياضي أبيض", "description": "وصف", "price": "199.00", "sale_price": None,
        "currency": "SAR", "in_stock": True, "stock_quantity": 4, "orderable": True,
        "product_url": "https://example.test/p/1", "image_url": "https://example.test/i/1.jpg",
    }
    fields.update(overrides)
    return Snapshot(**fields)


def result(status: str = "ok", **fields: Any) -> Snapshot:
    fields.setdefault("failure_reason", None)
    return Snapshot(status=status, **fields)


LINK = cl.TrustedConversationLink(
    tenant_id=TENANT, namespace="live", channel="wa",
    app_conversation_id=77_000,              # deliberately not equal to the runtime id
    runtime_conversation_id=CONVERSATION, conversation_ref="wa:v1:conv:77000",
)


@pytest.fixture()
def binding() -> alt.LiveToolBinding:
    return alt.LiveToolBinding(context=object(), link=LINK)


def patch_impl(monkeypatch: pytest.MonkeyPatch, module: str, name: str, fn: Any) -> None:
    monkeypatch.setattr(f"modules.ai.commerce_agent_v2.tools.{module}.{name}", fn, raising=True)


def async_returning(value: Any, *, record: List[Any] | None = None) -> Any:
    async def impl(context: Any, **kwargs: Any) -> Any:
        if record is not None:
            record.append((context, kwargs))
        if isinstance(value, Exception):
            raise value
        return value
    return impl


def run(binding: alt.LiveToolBinding, tool: str, arguments: Dict[str, Any], *,
        scope: at.ToolScope = SCOPE, timeout: float = 5.0) -> ac.ToolObservation:
    registry = alt.build_live_registry(binding)
    return registry.execute(scope, ac.ToolRequest(call_id="c1", tool_name=tool, arguments=arguments),
                            timeout_seconds=timeout)


# ── The allowlist ────────────────────────────────────────────────────────────


def test_the_registry_exposes_exactly_the_seven_read_tools(binding):
    registry = alt.build_live_registry(binding)
    assert tuple(d.name for d in registry.definitions) == alt.LIVE_TOOL_NAMES
    assert alt.LIVE_TOOL_NAMES == ("search_products", "get_product_details", "search_merchant_knowledge",
                                   "resolve_customer_order", "get_order_details", "get_order_shipment",
                                   "list_shareable_promotions")


def test_every_tool_the_instructions_name_is_declared_by_the_registry(binding):
    """The instructions refer to six tools by name; the registry declares those
    six plus the owner-approved read of shareable promotions, which the
    instructions never mention and the model discovers from its declaration."""
    from modules.ai.commerce_agent_v2.pilot_instructions import INSTRUCTION_TOOL_NAMES

    declared = tuple(d.name for d in alt.build_live_registry(binding).definitions)
    assert declared[:len(INSTRUCTION_TOOL_NAMES)] == INSTRUCTION_TOOL_NAMES
    assert set(declared) - set(INSTRUCTION_TOOL_NAMES) == {"list_shareable_promotions"}


def test_every_exposed_tool_is_declared_read_only(binding):
    assert all(d.read_only for d in alt.build_live_registry(binding).definitions)


def test_a_tool_that_is_not_read_only_cannot_be_registered_beside_them(binding):
    writing = at.RegisteredTool(
        definition=ac.ToolDefinition(name="order_create", description="x", input_schema={"type": "object"},
                                     result_kind="order_summary", read_only=False),
        function=lambda scope, arguments: at.ToolResult(result={}, evidence_refs=()),
    )
    with pytest.raises(Exception):
        at.ToolRegistry(list(alt.build_live_tools(binding)) + [writing])


def test_no_declared_schema_offers_a_write_a_price_or_a_quantity_the_model_could_set(binding):
    offered = {name for d in alt.build_live_registry(binding).definitions
               for name in (d.input_schema.get("properties") or {})}
    assert offered == {"query", "limit", "product_id", "order_number", "purpose", "order_id"}


# ── Scope ────────────────────────────────────────────────────────────────────


def test_a_call_for_another_scope_is_refused_and_the_implementation_never_runs(binding, monkeypatch):
    seen: List[Any] = []
    patch_impl(monkeypatch, "catalog", "search_products_impl",
               async_returning(result("ok", products=[product()], evidence=[Record("catalog:product:1")],
                                      knowledge_sections=[]), record=seen))
    observation = run(binding, "search_products", {"query": "حذاء"}, scope=OTHER_SCOPE)
    assert observation.ok is False
    assert observation.error_code == ac.ToolErrorCode.SCOPE_OVERRIDE_REFUSED.value
    assert seen == []


@pytest.mark.parametrize("argument", ["tenant_id", "conversation_id", "store_id", "owner_id", "token"])
def test_scope_arguments_cannot_be_supplied_by_the_model(binding, monkeypatch, argument):
    seen: List[Any] = []
    patch_impl(monkeypatch, "catalog", "search_products_impl", async_returning(result("ok"), record=seen))
    observation = run(binding, "search_products", {"query": "حذاء", argument: 1})
    assert observation.error_code == ac.ToolErrorCode.SCOPE_OVERRIDE_REFUSED.value
    assert seen == []


def test_the_trusted_context_and_only_it_reaches_the_implementation(binding, monkeypatch):
    seen: List[Any] = []
    patch_impl(monkeypatch, "catalog", "search_products_impl",
               async_returning(result("ok", products=[], evidence=[], knowledge_sections=[]), record=seen))
    run(binding, "search_products", {"query": "حذاء"})
    context, kwargs = seen[0]
    assert context is binding.context
    assert set(kwargs) == {"query", "limit"}


# ── Projection and provenance ────────────────────────────────────────────────


def test_a_catalog_hit_keeps_its_evidence_references_and_its_grounded_fields(binding, monkeypatch):
    patch_impl(monkeypatch, "catalog", "search_products_impl",
               async_returning(result("ok", products=[product(1), product(2)],
                                      evidence=[Record("catalog:product:1"), Record("catalog:product:2")],
                                      knowledge_sections=[])))
    observation = run(binding, "search_products", {"query": "حذاء"})
    assert observation.ok is True
    assert observation.evidence_refs == ("catalog:product:1", "catalog:product:2")
    assert observation.result["found"] is True
    first = observation.result["products"][0]
    assert first["evidence_ref"] == "catalog:product:1"
    assert first["price"] == "199.00" and first["in_stock"] is True and first["currency"] == "SAR"


def test_a_search_that_matched_nothing_is_an_honest_empty_answer_with_no_evidence(binding, monkeypatch):
    patch_impl(monkeypatch, "catalog", "search_products_impl",
               async_returning(result("not_found", failure_reason="no_catalog_product_matched")))
    observation = run(binding, "search_products", {"query": "زرافة"})
    assert observation.ok is True                       # the lookup ran; it simply found nothing
    assert observation.result == {"status": "not_found", "found": False,
                                  "reason": "no_catalog_product_matched"}
    assert observation.evidence_refs == ()


def test_a_denied_read_says_it_was_denied_rather_than_reporting_nothing_found(binding, monkeypatch):
    patch_impl(monkeypatch, "orders", "resolve_customer_order_impl",
               async_returning(result("denied", failure_reason="order_reads_disabled",
                                      selection_reason=None)))
    observation = run(binding, "resolve_customer_order", {})
    assert observation.result["status"] == "denied" and observation.result["found"] is False
    assert observation.result["reason"] == "order_reads_disabled"


def test_merchant_knowledge_keeps_the_section_body_and_its_reference(binding, monkeypatch):
    section = Snapshot(section_id=3, kind="shipping", title="التوصيل", body="التوصيل خلال ٣ أيام",
                       evidence_ref="knowledge:section:3")
    patch_impl(monkeypatch, "knowledge", "search_merchant_knowledge_impl",
               async_returning(result("ok", sections=[section], evidence=[Record("knowledge:section:3")])))
    observation = run(binding, "search_merchant_knowledge", {"query": "التوصيل"})
    assert observation.evidence_refs == ("knowledge:section:3",)
    assert observation.result["sections"][0]["body"] == "التوصيل خلال ٣ أيام"


def promotion(promotion_id: int = 5, **overrides: Any) -> Snapshot:
    fields: Dict[str, Any] = {
        "promotion_id": promotion_id, "record_kind": "coupon", "code": "WELCOME10", "name": "",
        "description": "خصم ترحيبي على أول طلب", "discount_type": "percentage", "discount_value": "10",
        "expires_at": "2027-01-01T00:00:00+00:00", "conditions": {"min_order_total": "100"},
        "eligibility_determined": False, "eligibility_note": "conditions_not_fully_evaluated",
        "evidence_ref": f"promotion:coupon:{promotion_id}",
    }
    fields.update(overrides)
    return Snapshot(**fields)


def test_a_shareable_coupon_keeps_its_code_its_conditions_and_its_reference(binding, monkeypatch):
    patch_impl(monkeypatch, "promotions", "list_shareable_promotions_impl",
               async_returning(result("ok", promotions=[promotion(5)],
                                      evidence=[Record("promotion:coupon:5")], query_outcome="ok")))
    observation = run(binding, "list_shareable_promotions", {})
    assert observation.ok is True
    assert observation.evidence_refs == ("promotion:coupon:5",)
    assert observation.result["found"] is True and observation.result["eligibility_determined"] is False
    first = observation.result["promotions"][0]
    assert first["code"] == "WELCOME10" and first["evidence_ref"] == "promotion:coupon:5"
    assert first["discount_type"] == "percentage" and first["discount_value"] == "10"
    assert first["conditions"] == {"min_order_total": "100"}
    assert first["eligibility_determined"] is False


def test_no_shareable_promotion_is_an_honest_empty_answer_with_no_evidence(binding, monkeypatch):
    patch_impl(monkeypatch, "promotions", "list_shareable_promotions_impl",
               async_returning(result("not_found", failure_reason="no_valid_shareable_promotions",
                                      query_outcome="NO_VALID_PROMOTIONS")))
    observation = run(binding, "list_shareable_promotions", {})
    assert observation.ok is True
    assert observation.result == {"status": "not_found", "found": False,
                                  "reason": "no_valid_shareable_promotions",
                                  "query_outcome": "NO_VALID_PROMOTIONS"}
    assert observation.evidence_refs == ()


def test_an_unreadable_promotion_source_says_so_rather_than_reporting_none(binding, monkeypatch):
    patch_impl(monkeypatch, "promotions", "list_shareable_promotions_impl",
               async_returning(result("error", failure_reason="promotion_query_failed",
                                      query_outcome="PROMOTION_QUERY_FAILED")))
    observation = run(binding, "list_shareable_promotions", {})
    assert observation.result["status"] == "error" and observation.result["found"] is False
    assert observation.result["reason"] == "promotion_query_failed"
    assert observation.evidence_refs == ()


def test_the_promotion_read_takes_no_argument_the_model_could_set(binding):
    definition = next(d for d in alt.build_live_registry(binding).definitions
                      if d.name == "list_shareable_promotions")
    assert definition.read_only is True
    assert definition.input_schema == {"type": "object", "properties": {}, "required": []}


def test_an_order_lookup_keeps_the_status_label_and_the_selection_reason(binding, monkeypatch):
    summary = Snapshot(order_id=12, evidence_ref="order:summary:12", order_reference="A-12",
                       status="shipped", status_label="تم الشحن")
    patch_impl(monkeypatch, "orders", "resolve_customer_order_impl",
               async_returning(result("ok", order=summary, selection_reason="latest_open_order",
                                      evidence=[Record("order:summary:12")])))
    observation = run(binding, "resolve_customer_order", {"purpose": "status"})
    assert observation.evidence_refs == ("order:summary:12",)
    assert observation.result["order"]["status_label"] == "تم الشحن"
    assert observation.result["selection_reason"] == "latest_open_order"


def test_shipment_facts_keep_the_carrier_and_tracking_exactly_as_read(binding, monkeypatch):
    shipment = Snapshot(order_id=12, evidence_ref="order:shipment:12", order_reference="A-12",
                        shipment_status="in_transit", shipment_status_label="في الطريق",
                        carrier="Aramex", tracking_number="TRK-1", tracking_url="https://track.test/1")
    patch_impl(monkeypatch, "orders", "get_order_shipment_impl",
               async_returning(result("ok", shipment=shipment, evidence=[Record("order:shipment:12")])))
    observation = run(binding, "get_order_shipment", {"order_id": 12})
    body = observation.result["shipment"]
    assert body["carrier"] == "Aramex" and body["tracking_number"] == "TRK-1"
    assert body["shipment_status_label"] == "في الطريق"


def test_order_details_keep_the_line_items_within_the_declared_bound(binding, monkeypatch):
    items = [Snapshot(name=f"صنف {i}", quantity=i) for i in range(1, alt.MAX_LINE_ITEMS + 6)]
    details = Snapshot(order_id=12, evidence_ref="order:details:12", order_reference="A-12",
                       total=399, currency="SAR", line_items=items)
    patch_impl(monkeypatch, "orders", "get_order_details_impl",
               async_returning(result("ok", order=details, evidence=[Record("order:details:12")])))
    observation = run(binding, "get_order_details", {"order_id": 12})
    assert len(observation.result["order"]["line_items"]) == alt.MAX_LINE_ITEMS
    assert observation.result["order"]["total"] == 399


def test_a_full_search_result_stays_inside_the_loop_s_observation_bound(binding, monkeypatch):
    long_text = "و" * 5_000
    products = [product(i, description=long_text, title=long_text) for i in range(1, alt.MAX_SEARCH_LIMIT + 1)]
    sections = [Snapshot(section_id=i, kind="k", title=long_text, body=long_text,
                         evidence_ref=f"knowledge:section:{i}") for i in range(1, 9)]
    patch_impl(monkeypatch, "catalog", "search_products_impl",
               async_returning(result("ok", products=products, knowledge_sections=sections,
                                      evidence=[Record(f"catalog:product:{i}") for i in range(1, 6)])))
    observation = run(binding, "search_products", {"query": "حذاء", "limit": alt.MAX_SEARCH_LIMIT})
    assert observation.ok is True and observation.error_code is None
    assert len(observation.result["products"]) == alt.MAX_SEARCH_LIMIT
    assert len(observation.result["product_knowledge"]) == alt.MAX_KNOWLEDGE_SECTIONS


# ── Arguments ────────────────────────────────────────────────────────────────


def test_a_purpose_outside_the_closed_set_is_refused_before_any_read(binding, monkeypatch):
    seen: List[Any] = []
    patch_impl(monkeypatch, "orders", "resolve_customer_order_impl",
               async_returning(result("ok"), record=seen))
    observation = run(binding, "resolve_customer_order", {"purpose": "cancel"})
    assert observation.error_code == ac.ToolErrorCode.INVALID_ARGUMENTS.value
    assert seen == []


def test_a_limit_beyond_the_declared_maximum_is_refused_by_the_registry(binding, monkeypatch):
    seen: List[Any] = []
    patch_impl(monkeypatch, "catalog", "search_products_impl", async_returning(result("ok"), record=seen))
    observation = run(binding, "search_products", {"limit": alt.MAX_SEARCH_LIMIT + 1})
    assert observation.error_code == ac.ToolErrorCode.INVALID_ARGUMENTS.value
    assert seen == []


def test_an_unknown_argument_is_refused_rather_than_ignored(binding, monkeypatch):
    patch_impl(monkeypatch, "catalog", "search_products_impl", async_returning(result("ok")))
    observation = run(binding, "search_products", {"category": "shoes"})
    assert observation.error_code == ac.ToolErrorCode.INVALID_ARGUMENTS.value


# ── Failure and abandonment ──────────────────────────────────────────────────


def test_a_failing_read_becomes_an_observation_not_a_crash(binding, monkeypatch):
    patch_impl(monkeypatch, "catalog", "search_products_impl",
               async_returning(RuntimeError("catalog_service_returned_out_of_scope_product")))
    observation = run(binding, "search_products", {"query": "حذاء"})
    assert observation.ok is False
    assert observation.error_code == ac.ToolErrorCode.TOOL_FAILURE.value
    assert observation.evidence_refs == ()


def test_a_refusal_releases_the_binding_so_the_next_read_still_runs(binding, monkeypatch):
    patch_impl(monkeypatch, "catalog", "search_products_impl",
               async_returning(RuntimeError("boom")))
    registry = alt.build_live_registry(binding)
    first = registry.execute(SCOPE, ac.ToolRequest(call_id="a", tool_name="search_products",
                                                   arguments={"query": "x"}), timeout_seconds=5.0)
    patch_impl(monkeypatch, "catalog", "search_products_impl",
               async_returning(result("ok", products=[product()], evidence=[Record("catalog:product:1")],
                                      knowledge_sections=[])))
    registry = alt.build_live_registry(binding)
    second = registry.execute(SCOPE, ac.ToolRequest(call_id="b", tool_name="search_products",
                                                    arguments={"query": "y"}), timeout_seconds=5.0)
    assert first.ok is False and binding.poisoned is None
    assert second.ok is True


def test_a_call_the_loop_abandoned_closes_the_binding_for_every_later_read(binding, monkeypatch):
    started = threading.Event()
    release = threading.Event()

    async def slow(context: Any, **kwargs: Any) -> Any:
        started.set()
        release.wait(5.0)
        return result("ok", products=[], evidence=[], knowledge_sections=[])

    patch_impl(monkeypatch, "catalog", "search_products_impl", slow)
    registry = alt.build_live_registry(binding)
    abandoned = registry.execute(SCOPE, ac.ToolRequest(call_id="a", tool_name="search_products",
                                                       arguments={"query": "x"}), timeout_seconds=0.2)
    assert abandoned.error_code == ac.ToolErrorCode.TIMEOUT.value
    assert started.is_set()

    patch_impl(monkeypatch, "knowledge", "search_merchant_knowledge_impl",
               async_returning(result("ok", sections=[], evidence=[])))
    later = alt.build_live_registry(binding).execute(
        SCOPE, ac.ToolRequest(call_id="b", tool_name="search_merchant_knowledge", arguments={"query": "x"}),
        timeout_seconds=5.0)
    assert later.ok is False and later.error_code == ac.ToolErrorCode.TOOL_FAILURE.value
    assert binding.poisoned is not None and "abandoned" in binding.poisoned
    release.set()
    # The binding stays closed even after the abandoned call finally finishes.
    time.sleep(0.05)
    assert binding.poisoned is not None


def test_one_timed_out_tool_marks_abandonment_immediately_with_no_second_call(binding,
                                                                                monkeypatch):
    """The turn may end right there. Nothing later is guaranteed to notice."""
    started = threading.Event()
    release = threading.Event()

    async def slow(context: Any, **kwargs: Any) -> Any:
        started.set()
        release.wait(5.0)
        return result("ok", products=[], evidence=[], knowledge_sections=[])

    patch_impl(monkeypatch, "catalog", "search_products_impl", slow)
    abandoned = alt.build_live_registry(binding).execute(
        SCOPE, ac.ToolRequest(call_id="a", tool_name="search_products", arguments={"query": "x"}),
        timeout_seconds=0.2)
    assert abandoned.error_code == ac.ToolErrorCode.TIMEOUT.value
    assert started.is_set()
    # No further tool call is made. The binding already knows.
    assert binding.abandoned_calls == 1
    assert binding.poisoned is not None and "abandoned" in binding.poisoned
    assert binding.session_may_be_in_use is True
    assert binding.wait_until_idle(0.1) is False
    release.set()
    assert binding.wait_until_idle(5.0) is True
    assert binding.session_may_be_in_use is False


def test_the_abandonment_notice_carries_what_the_registry_observed(binding, monkeypatch):
    async def slow(context: Any, **kwargs: Any) -> Any:
        time.sleep(1.0)
        return result("ok", products=[], evidence=[], knowledge_sections=[])

    patch_impl(monkeypatch, "catalog", "search_products_impl", slow)
    alt.build_live_registry(binding).execute(
        SCOPE, ac.ToolRequest(call_id="a", tool_name="search_products", arguments={"query": "x"}),
        timeout_seconds=0.2)
    assert "did not answer within 0.2s" in (binding.poisoned or "")


def test_a_registry_without_an_abandonment_owner_still_answers_the_loop():
    """The hook is optional: the fixture tools have none and time out normally."""
    from core.commerce_runtime import agent_tools as at

    def slow(scope: Any, arguments: Any) -> at.ToolResult:
        time.sleep(1.0)
        return at.ToolResult(result={}, evidence_refs=())

    registry = at.ToolRegistry([at.RegisteredTool(
        definition=ac.ToolDefinition(name="slow_read", description="d",
                                     input_schema={"type": "object", "properties": {}},
                                     result_kind="product", read_only=True),
        function=slow)])
    observation = registry.execute(
        at.ToolScope(tenant_id=TENANT, namespace="live", conversation_id=CONVERSATION, turn_id=1),
        ac.ToolRequest(call_id="a", tool_name="slow_read", arguments={}), timeout_seconds=0.2)
    assert observation.ok is False and observation.error_code == ac.ToolErrorCode.TIMEOUT.value


def test_an_abandonment_owner_that_raises_never_costs_the_loop_its_observation(monkeypatch):
    from core.commerce_runtime import agent_tools as at

    def slow(scope: Any, arguments: Any) -> at.ToolResult:
        time.sleep(1.0)
        return at.ToolResult(result={}, evidence_refs=())

    def explode(_reason: str) -> None:
        raise RuntimeError("the owner is broken too")

    registry = at.ToolRegistry([at.RegisteredTool(
        definition=ac.ToolDefinition(name="slow_read", description="d",
                                     input_schema={"type": "object", "properties": {}},
                                     result_kind="product", read_only=True),
        function=slow, on_abandoned=explode)])
    observation = registry.execute(
        at.ToolScope(tenant_id=TENANT, namespace="live", conversation_id=CONVERSATION, turn_id=1),
        ac.ToolRequest(call_id="a", tool_name="slow_read", arguments={}), timeout_seconds=0.2)
    assert observation.ok is False and observation.error_code == ac.ToolErrorCode.TIMEOUT.value


def test_an_explicitly_poisoned_binding_refuses_every_read(binding, monkeypatch):
    patch_impl(monkeypatch, "catalog", "search_products_impl", async_returning(result("ok")))
    binding.poison("the runtime closed this binding")
    observation = run(binding, "search_products", {"query": "x"})
    assert observation.ok is False
    assert observation.error_code == ac.ToolErrorCode.TOOL_FAILURE.value
    assert "closed this binding" in (observation.error or "")


def test_no_execution_time_left_refuses_before_touching_the_merchant_s_data(binding, monkeypatch):
    seen: List[Any] = []
    patch_impl(monkeypatch, "catalog", "search_products_impl", async_returning(result("ok"), record=seen))
    observation = run(binding, "search_products", {"query": "x"}, timeout=0.0)
    assert observation.error_code == ac.ToolErrorCode.TIMEOUT.value
    assert seen == []
