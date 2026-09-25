"""The merchant's real read tools as the loop sees them (no database, no model).

Each case drives the registered tool through the loop's own ``ToolRegistry``,
with the underlying Commerce Agent V2 read implementation replaced by a double.
What is proved here is the boundary: the scope cannot be moved, the allowlist
holds nothing that writes, provenance survives the projection, an empty answer
stays an empty answer, and a call the loop abandoned closes the binding instead
of sharing a database session with a thread nobody is waiting for.
"""
from __future__ import annotations

import json
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


def test_the_registry_exposes_exactly_the_eight_read_tools(binding):
    registry = alt.build_live_registry(binding)
    assert tuple(d.name for d in registry.definitions) == alt.LIVE_TOOL_NAMES
    assert alt.LIVE_TOOL_NAMES == ("search_products", "get_product_details", "search_merchant_knowledge",
                                   "resolve_customer_order", "get_order_details", "get_order_shipment",
                                   "get_customer_addresses", "list_shareable_promotions")


def test_every_tool_the_instructions_name_is_declared_by_the_registry(binding):
    """The instructions refer to six tools by name; the registry declares those
    six plus two owner-approved reads — shareable promotions and the customer's
    saved addresses — which the instructions never mention and the model
    discovers from their declarations. Keeping them out of the instructions is
    deliberate: when to read an address, and whether to mention one, stays the
    model's call rather than a step the prompt orders."""
    from modules.ai.commerce_agent_v2.pilot_instructions import INSTRUCTION_TOOL_NAMES

    declared = tuple(d.name for d in alt.build_live_registry(binding).definitions)
    assert declared[:len(INSTRUCTION_TOOL_NAMES)] == INSTRUCTION_TOOL_NAMES
    assert set(declared) - set(INSTRUCTION_TOOL_NAMES) == set(alt.PILOT_ONLY_TOOL_NAMES)


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


# The reading of the customer a projected list was built against, as the tool
# hands it over: four countable orders reaching bronze and silver.
STANDING: Dict[str, Any] = {
    "customer_id": 41, "countable_orders": 4, "resolved_level": "silver",
    "entitled_levels": ["bronze", "silver"], "reason": "entitled_by_order_count",
    "determined": True, "first_purchase_applied": False, "served_level": "silver",
}


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
                                      evidence=[Record("promotion:coupon:5")], query_outcome="ok",
                                      entitlement=STANDING)))
    observation = run(binding, "list_shareable_promotions", {})
    assert observation.ok is True
    assert observation.evidence_refs == ("promotion:coupon:5",)
    assert observation.result["found"] is True
    assert observation.result["partial"] is False and observation.result["query_outcome"] == "ok"
    first = observation.result["promotions"][0]
    assert first["code"] == "WELCOME10" and first["evidence_ref"] == "promotion:coupon:5"
    assert first["discount_type"] == "percentage" and first["discount_value"] == "10"
    assert first["conditions"] == {"min_order_total": "100"}
    assert first["eligibility_determined"] is False


def test_the_view_carries_the_reading_of_the_customer_the_list_was_built_against(binding, monkeypatch):
    """A coupon tied to a loyalty rung reaches the list only for a customer who
    earned it, so the observation says which rung was resolved and how firmly.
    ``determined`` is the load-bearing one: a standing that could not be read is
    not a customer with no standing."""
    patch_impl(monkeypatch, "promotions", "list_shareable_promotions_impl",
               async_returning(result("ok", promotions=[promotion(
                   5, coupon_level="silver", level_eligibility="entitled", customer_level="silver",
                   level_reason="entitled_by_order_count")],
                   evidence=[Record("promotion:coupon:5")], query_outcome="ok", entitlement=STANDING)))
    observation = run(binding, "list_shareable_promotions", {})
    assert observation.result["entitlement"] == {
        "resolved_level": "silver", "served_level": "silver",
        "entitled_levels": ["bronze", "silver"], "countable_orders": 4,
        "reason": "entitled_by_order_count", "determined": True, "first_purchase_applied": False,
        # Absent from this double, and absent rather than zero in the view:
        # a source that reported no totals has not reported "none seen".
        "orders_seen": None, "orders_not_counted": None}
    first = observation.result["promotions"][0]
    assert first["coupon_level"] == "silver" and first["customer_level"] == "silver"
    assert first["level_eligibility"] == "entitled"
    assert first["level_reason"] == "entitled_by_order_count"


def test_a_standing_that_could_not_be_read_reaches_the_observation_as_undetermined(binding, monkeypatch):
    """Nothing was settled about this customer, and the observation says so
    rather than presenting an unconditioned code as verified for them."""
    undetermined = {"customer_id": None, "countable_orders": None, "resolved_level": "",
                    "entitled_levels": [], "reason": "identity_not_established",
                    "determined": False, "first_purchase_applied": False}
    patch_impl(monkeypatch, "promotions", "list_shareable_promotions_impl",
               async_returning(result("ok", promotions=[promotion(
                   5, level_eligibility="not_conditioned_on_level",
                   eligibility_note="customer_standing_not_determined:identity_not_established")],
                   evidence=[Record("promotion:coupon:5")], query_outcome="ok",
                   entitlement=undetermined)))
    observation = run(binding, "list_shareable_promotions", {})
    assert observation.result["entitlement"]["determined"] is False
    assert observation.result["entitlement"]["countable_orders"] is None
    assert observation.result["entitlement"]["resolved_level"] == ""
    first = observation.result["promotions"][0]
    assert first["level_eligibility"] == "not_conditioned_on_level"
    assert first["eligibility_determined"] is False


def test_a_result_that_carries_no_standing_at_all_is_an_empty_reading_not_a_guess(binding, monkeypatch):
    patch_impl(monkeypatch, "promotions", "list_shareable_promotions_impl",
               async_returning(result("ok", promotions=[promotion(5)],
                                      evidence=[Record("promotion:coupon:5")], query_outcome="ok")))
    observation = run(binding, "list_shareable_promotions", {})
    assert observation.result["entitlement"] == {}


def test_no_shareable_promotion_is_an_honest_empty_answer_with_no_evidence(binding, monkeypatch):
    patch_impl(monkeypatch, "promotions", "list_shareable_promotions_impl",
               async_returning(result("not_found", failure_reason="no_valid_shareable_promotions",
                                      query_outcome="NO_VALID_PROMOTIONS")))
    observation = run(binding, "list_shareable_promotions", {})
    assert observation.ok is True
    assert observation.result == {"status": "not_found", "found": False,
                                  "reason": "no_valid_shareable_promotions",
                                  "query_outcome": "NO_VALID_PROMOTIONS",
                                  "entitlement": {}, "withheld": {}}
    assert observation.evidence_refs == ()


def test_an_empty_list_carries_the_standing_it_was_built_against(binding, monkeypatch):
    """The defect this pins: an empty result used to drop the entitlement and
    the withheld counts on the floor, so "you have earned no rung", "we could
    not place you" and "this store publishes nothing" reached the model as one
    identical answer. An empty list is an answer; it has to say which one."""
    patch_impl(monkeypatch, "promotions", "list_shareable_promotions_impl",
               async_returning(result("not_found", failure_reason="no_valid_shareable_promotions",
                                      query_outcome="NO_VALID_PROMOTIONS", entitlement=STANDING,
                                      withheld={"level_not_earned_by_customer": 6,
                                                "not_published_for_general_use": 2})))
    observation = run(binding, "list_shareable_promotions", {})
    assert observation.result["found"] is False
    assert observation.result["entitlement"]["resolved_level"] == "silver"
    assert observation.result["entitlement"]["determined"] is True
    assert observation.result["withheld"] == {"level_not_earned_by_customer": 6,
                                              "not_published_for_general_use": 2}


def test_the_three_causes_of_an_empty_list_reach_the_model_distinguishably(binding, monkeypatch):
    """Same visible outcome — no codes — three different truths. A merchant's
    first question is which of them happened, and the observation now answers
    it without anyone reading a transcript."""
    def observe(entitlement: Dict[str, Any], withheld: Dict[str, int]) -> Dict[str, Any]:
        patch_impl(monkeypatch, "promotions", "list_shareable_promotions_impl",
                   async_returning(result("not_found", failure_reason="no_valid_shareable_promotions",
                                          query_outcome="NO_VALID_PROMOTIONS",
                                          entitlement=entitlement, withheld=withheld)))
        return run(binding, "list_shareable_promotions", {}).result

    earned_nothing = observe({"customer_id": 41, "countable_orders": 0, "resolved_level": "",
                              "entitled_levels": [], "reason": "no_entitled_level",
                              "determined": True, "first_purchase_applied": False},
                             {"level_not_earned_by_customer": 8})
    unreadable_history = observe({"customer_id": 41, "countable_orders": None, "resolved_level": "",
                                  "entitled_levels": [], "reason": "order_history_unreadable",
                                  "determined": False, "first_purchase_applied": False},
                                 {"level_not_earned_by_customer": 8})
    store_publishes_nothing = observe(STANDING, {"not_published_for_general_use": 8})

    # Verified to have earned no rung is not the same as never having been read.
    assert earned_nothing["entitlement"]["determined"] is True
    assert earned_nothing["entitlement"]["countable_orders"] == 0
    assert unreadable_history["entitlement"]["determined"] is False
    assert unreadable_history["entitlement"]["countable_orders"] is None
    assert earned_nothing["entitlement"]["reason"] != unreadable_history["entitlement"]["reason"]
    # And neither is the same as a store that simply published nothing to the AI.
    assert store_publishes_nothing["entitlement"]["determined"] is True
    assert store_publishes_nothing["withheld"] == {"not_published_for_general_use": 8}
    assert earned_nothing["withheld"] != store_publishes_nothing["withheld"]


def test_an_unreadable_source_still_says_what_it_managed_to_settle(binding, monkeypatch):
    """``error`` is about the records, not the customer. What was settled before
    the read failed still travels, so a partial picture is not thrown away."""
    patch_impl(monkeypatch, "promotions", "list_shareable_promotions_impl",
               async_returning(result("error", failure_reason="promotion_query_failed",
                                      query_outcome="PROMOTION_QUERY_FAILED", entitlement=STANDING,
                                      withheld={"expiring_before_store_minimum": 3})))
    observation = run(binding, "list_shareable_promotions", {})
    assert observation.result["status"] == "error" and observation.result["found"] is False
    assert observation.result["entitlement"]["resolved_level"] == "silver"
    assert observation.result["withheld"] == {"expiring_before_store_minimum": 3}


def test_withheld_counts_never_carry_a_code_or_another_customers_identifier(binding, monkeypatch):
    """Reasons and counts only. The withheld view exists to explain a refusal,
    never to leak what was refused — a code held back is still a code."""
    patch_impl(monkeypatch, "promotions", "list_shareable_promotions_impl",
               async_returning(result("not_found", failure_reason="no_valid_shareable_promotions",
                                      query_outcome="NO_VALID_PROMOTIONS", entitlement=STANDING,
                                      withheld={"personal_to_another_customer": "4",
                                                "level_not_earned_by_customer": None,
                                                "unusable_record": 1})))
    observation = run(binding, "list_shareable_promotions", {})
    withheld = observation.result["withheld"]
    assert withheld == {"personal_to_another_customer": 4, "unusable_record": 1}
    assert all(isinstance(count, int) for count in withheld.values())
    assert "VIP50" not in repr(observation.result)


def test_an_unreadable_promotion_source_says_so_rather_than_reporting_none(binding, monkeypatch):
    patch_impl(monkeypatch, "promotions", "list_shareable_promotions_impl",
               async_returning(result("error", failure_reason="promotion_query_failed",
                                      query_outcome="PROMOTION_QUERY_FAILED")))
    observation = run(binding, "list_shareable_promotions", {})
    assert observation.result["status"] == "error" and observation.result["found"] is False
    assert observation.result["reason"] == "promotion_query_failed"
    assert observation.evidence_refs == ()


def test_a_partial_promotion_read_says_so_to_the_model(binding, monkeypatch):
    """A source that could not be read is not silently 'no coupons': the model
    is told the list may be incomplete."""
    patch_impl(monkeypatch, "promotions", "list_shareable_promotions_impl",
               async_returning(result("ok", promotions=[promotion(5)], evidence=[Record("promotion:coupon:5")],
                                      query_outcome="PROMOTION_PARTIAL_FAILURE", partial=True)))
    observation = run(binding, "list_shareable_promotions", {})
    assert observation.ok is True and observation.result["found"] is True
    assert observation.result["partial"] is True
    assert observation.result["query_outcome"] == "PROMOTION_PARTIAL_FAILURE"


def test_a_merchant_denial_reaches_the_model_as_a_denial_not_as_no_coupons(binding, monkeypatch):
    patch_impl(monkeypatch, "promotions", "list_shareable_promotions_impl",
               async_returning(result("denied", failure_reason="merchant_ai_coupon_policy_disabled",
                                      query_outcome="NO_VALID_PROMOTIONS")))
    observation = run(binding, "list_shareable_promotions", {})
    assert observation.result["status"] == "denied" and observation.result["found"] is False
    assert observation.result["reason"] == "merchant_ai_coupon_policy_disabled"
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


def test_a_requested_order_that_is_not_this_customers_is_not_no_orders_at_all(binding, monkeypatch):
    """Both are ``not_found``; only the reason separates them. A customer who
    asked after a number that is not theirs has orders; one with an empty record
    has none. Collapsing the two lets the agent tell a returning customer they
    have never ordered — so the reason has to survive into the observation."""
    patch_impl(monkeypatch, "orders", "resolve_customer_order_impl",
               async_returning(result("not_found", selection_reason="explicit_order_number",
                                      failure_reason="explicit_order_not_found_for_customer")))
    asked = run(binding, "resolve_customer_order",
                {"purpose": "status", "order_number": "A-999"}).result
    assert asked["status"] == "not_found" and asked["found"] is False
    assert asked["reason"] == "explicit_order_not_found_for_customer"

    patch_impl(monkeypatch, "orders", "resolve_customer_order_impl",
               async_returning(result("not_found", selection_reason=None,
                                      failure_reason="no_orders_in_trusted_customer_record")))
    empty = run(binding, "resolve_customer_order", {"purpose": "status"}).result
    assert empty["status"] == "not_found" and empty["found"] is False
    assert empty["reason"] == "no_orders_in_trusted_customer_record"

    assert asked["reason"] != empty["reason"]


def test_an_order_read_the_merchant_disabled_is_a_denial_not_an_absence(binding, monkeypatch):
    """The unreadable case. A capability the merchant turned off says so; it
    never reaches the model wearing the shape of a customer with no orders."""
    patch_impl(monkeypatch, "orders", "resolve_customer_order_impl",
               async_returning(result("denied", failure_reason="order_reads_disabled")))
    body = run(binding, "resolve_customer_order", {"purpose": "status"}).result
    assert body["status"] == "denied" and body["found"] is False
    assert body["reason"] == "order_reads_disabled"


def test_with_several_orders_the_observation_says_which_one_and_why(binding, monkeypatch):
    """«تعدد الطلبات». The platform picks; the observation carries the picked
    order together with the rule that picked it, so the agent can name the order
    it is answering about instead of implying the customer has only one."""
    summary = Snapshot(order_id=31, evidence_ref="order:summary:31", order_reference="A-31",
                       status="processing", status_label="قيد التجهيز")
    patch_impl(monkeypatch, "orders", "resolve_customer_order_impl",
               async_returning(result("ok", order=summary, selection_reason="latest_open_order",
                                      evidence=[Record("order:summary:31")])))
    body = run(binding, "resolve_customer_order", {"purpose": "status"}).result
    assert body["order"]["order_id"] == 31
    assert body["order"]["order_reference"] == "A-31"
    assert body["selection_reason"] == "latest_open_order"


def test_an_order_the_conversation_never_authorized_is_reported_as_not_found(binding, monkeypatch):
    """`_load_authorized_order` refuses an id the turn never authorized, and the
    details read turns that into `not_found`. What matters here is that the
    refusal arrives as data the model can act on rather than as a tool error,
    and that it carries no field of the order it declined to read."""
    patch_impl(monkeypatch, "orders", "get_order_details_impl",
               async_returning(result("not_found", failure_reason="authorized_order_missing")))
    observation = run(binding, "get_order_details", {"order_id": 4_242})
    assert observation.ok is True
    assert observation.result == {"status": "not_found", "found": False,
                                  "reason": "authorized_order_missing"}
    assert observation.evidence_refs == ()


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


# ── What the model is handed: the one discount reading, the variant options ──


def test_the_promotion_view_carries_the_one_discount_reading(binding, monkeypatch):
    patch_impl(monkeypatch, "promotions", "list_shareable_promotions_impl",
               async_returning(result("ok", promotions=[promotion(5, discount="10%")],
                                      evidence=[Record("promotion:coupon:5")], query_outcome="ok")))
    first = run(binding, "list_shareable_promotions", {}).result["promotions"][0]
    assert first["discount"] == "10%"
    assert first["discount_type"] == "percentage" and first["discount_value"] == "10"


def test_the_product_view_carries_the_options_the_customer_can_buy_now(binding, monkeypatch):
    """Tenant 1, September 2026: the view carried no variant, and the model
    called a white/fuchsia dress black. Colours and sizes in stock now travel
    with the product, bounded; a product without variants carries none."""
    options = {"اللون": ["أبيض", "فوشي"], "المقاس": ["38 - S"]}
    patch_impl(monkeypatch, "catalog", "search_products_impl",
               async_returning(result("ok", products=[
                   product(1, variant_options=options, variants_in_stock=2, variants_total=10),
                   product(2),
               ], evidence=[Record("catalog:product:1"), Record("catalog:product:2")],
                   knowledge_sections=[])))
    products = run(binding, "search_products", {"query": "فستان"}).result["products"]
    assert products[0]["variant_options"] == options
    assert products[0]["variants_in_stock"] == 2 and products[0]["variants_total"] == 10
    assert products[1]["variant_options"] == {} and products[1]["variants_in_stock"] is None


def test_the_product_view_bounds_the_variant_options_it_forwards(binding, monkeypatch):
    many = {f"خيار{i}": [f"قيمة{j}" for j in range(20)] for i in range(10)}
    patch_impl(monkeypatch, "catalog", "search_products_impl",
               async_returning(result("ok", products=[product(1, variant_options=many)],
                                      evidence=[Record("catalog:product:1")], knowledge_sections=[])))
    forwarded = run(binding, "search_products", {"query": "فستان"}).result["products"][0]["variant_options"]
    assert len(forwarded) == 6 and all(len(values) == 12 for values in forwarded.values())


def test_the_view_carries_what_was_seen_beside_what_was_counted(binding, monkeypatch):
    """«كم طلبًا احتُسب، ولماذا استُبعد الباقي». Six orders seen and six set
    aside resolves to the same rung as no orders at all, and the customer
    asking "but I have ordered before" is asking about exactly that gap."""
    standing = {**STANDING, "countable_orders": 0, "resolved_level": "", "entitled_levels": [],
                "reason": "no_entitled_level", "raw_orders": 6, "excluded_orders": 6}
    patch_impl(monkeypatch, "promotions", "list_shareable_promotions_impl",
               async_returning(result("not_found", failure_reason="no_valid_shareable_promotions",
                                      query_outcome="NO_VALID_PROMOTIONS", entitlement=standing)))
    view = run(binding, "list_shareable_promotions", {}).result["entitlement"]
    assert view["countable_orders"] == 0 and view["determined"] is True
    assert view["orders_seen"] == 6 and view["orders_not_counted"] == 6


# ── The carrier's last scan, and the two times that must not merge ───────────


def _shipment(**overrides):
    fields = {"order_id": 12, "evidence_ref": "order:shipment:12", "order_reference": "A-12",
              "shipment_status": "in_transit", "shipment_status_label": "في الطريق",
              "carrier": "Aramex", "tracking_number": "TRK-1", "tracking_url": None,
              "data_source": None, "latest_event_status": None, "latest_event_note": None,
              "latest_event_location": None, "latest_event_at": None, "last_verified_at": None}
    fields.update(overrides)
    return Snapshot(**fields)


def _shipment_body(binding, monkeypatch, shipment):
    patch_impl(monkeypatch, "orders", "get_order_shipment_impl",
               async_returning(result("ok", shipment=shipment,
                                      evidence=[Record("order:shipment:12")])))
    return run(binding, "get_order_shipment", {"order_id": 12}).result["shipment"]


def test_the_carriers_last_scan_reaches_the_model_with_its_own_time(binding, monkeypatch):
    """«آخر حدث متاح وموقعه ووقته ومصدره». What the carrier reported, as the
    carrier reported it."""
    body = _shipment_body(binding, monkeypatch, _shipment(
        data_source="salla", latest_event_status="out_for_delivery",
        latest_event_note="مع المندوب", latest_event_location="الرياض",
        latest_event_at="2026-09-23T09:15:00+03:00",
        last_verified_at="2026-09-23T11:40:00+00:00"))
    assert body["latest_event_status"] == "out_for_delivery"
    assert body["latest_event_note"] == "مع المندوب"
    assert body["latest_event_location"] == "الرياض"
    assert body["latest_event_at"] == "2026-09-23T09:15:00+03:00"
    assert body["data_source"] == "salla"


def test_when_the_carrier_last_moved_is_never_when_we_last_asked(binding, monkeypatch):
    """The distinction the handoff insists on. A shipment that has not moved for
    two days but was checked a minute ago must not read as fresh: the event time
    and the verification time stay two fields, and neither substitutes."""
    body = _shipment_body(binding, monkeypatch, _shipment(
        latest_event_status="in_transit",
        latest_event_at="2026-09-21T06:00:00+03:00",
        last_verified_at="2026-09-23T11:59:00+00:00"))
    assert body["latest_event_at"] == "2026-09-21T06:00:00+03:00"
    assert body["last_verified_at"] == "2026-09-23T11:59:00+00:00"
    assert body["latest_event_at"] != body["last_verified_at"]


def test_a_verification_with_no_event_reports_no_event(binding, monkeypatch):
    """Having checked is not having news. A successful refresh that returned no
    scan leaves the event fields absent rather than borrowing the check's time."""
    body = _shipment_body(binding, monkeypatch, _shipment(
        last_verified_at="2026-09-23T11:59:00+00:00"))
    assert body["last_verified_at"] == "2026-09-23T11:59:00+00:00"
    assert body["latest_event_at"] is None
    assert body["latest_event_status"] is None and body["latest_event_location"] is None


def test_a_missing_location_stays_missing(binding, monkeypatch):
    """«عدم اختراع حدث أو موقع مفقود». A scan the carrier sent without a place
    has no place — not the delivery address, not the merchant's city."""
    body = _shipment_body(binding, monkeypatch, _shipment(
        latest_event_status="in_transit", latest_event_at="2026-09-22T10:00:00+03:00"))
    assert body["latest_event_status"] == "in_transit"
    assert body["latest_event_location"] is None


def test_the_shipment_view_carries_nothing_about_the_recipient(binding, monkeypatch):
    """The platform row holds a name, a phone, an address, coordinates, label
    paths and an internal shipment id. None of them is a tracking fact, and the
    projection is an allowlist so a later column cannot arrive by accident."""
    body = _shipment_body(binding, monkeypatch, _shipment(
        latest_event_status="delivered", latest_event_location="جدة",
        latest_event_at="2026-09-23T08:00:00+03:00"))
    assert set(body) == {
        "order_id", "evidence_ref", "order_reference", "shipment_status",
        "shipment_status_label", "carrier", "tracking_number", "tracking_url",
        "data_source", "latest_event_status", "latest_event_note",
        "latest_event_location", "latest_event_at", "last_verified_at"}
    # ``shipment_status_label`` is a status word, so the check is on the
    # platform row's own field names rather than on substrings.
    blob = json.dumps(body, ensure_ascii=False)
    for forbidden in ("recipient_name", "recipient_phone", "address_type", "address_text",
                      "address_url", "latitude", "longitude", "label_url", "label_pdf_path",
                      "cod_amount", "external_shipment_id", "extra_metadata"):
        assert forbidden not in blob and forbidden not in body


# ── The addresses the platform actually holds ────────────────────────────────


def _address_fact(**overrides):
    fact = {"address": {"city": "الرياض", "district": "النخيل",
                        "address_line": "شارع الملك عبدالعزيز", "country": "SA",
                        "short_address_code": "RRRD1234", "maps_url": ""},
            "source": "salla_customer_profile", "selection_state": "candidate",
            "selected": False, "sufficient": True, "has_delivery_evidence": True,
            "has_location_pin": False, "provenance_known": True,
            "missing_requirements": []}
    fact.update(overrides)
    return fact


class _AddressContext:
    """The trusted context as the address read sees it: a session and the
    identity the conversation established. Never an identity from arguments."""

    def __init__(self, tenant_id: int = TENANT, customer_id: int = 4_100) -> None:
        self.db = object()
        self.tenant_id = tenant_id
        self.customer_id = customer_id


@pytest.fixture()
def address_binding() -> alt.LiveToolBinding:
    return alt.LiveToolBinding(context=_AddressContext(), link=LINK)


def _patch_addresses(monkeypatch, facts):
    monkeypatch.setattr("core.customer_address_candidates."
                        "customer_address_facts_for_trusted_context",
                        lambda db, *, tenant_id, customer_id: facts, raising=True)


def _addresses(address_binding, monkeypatch, facts):
    _patch_addresses(monkeypatch, facts)
    return run(address_binding, "get_customer_addresses", {}).result


def test_a_selected_address_is_the_one_the_resolver_selected(address_binding, monkeypatch):
    selected = _address_fact(selection_state="selected", selected=True,
                             source="order_confirmed_shipping")
    body = _addresses(address_binding, monkeypatch, {
        "address_read_status": "available", "address_read_reason": "ok",
        "address_resolution": "selected_address",
        "saved_addresses": [selected], "selected_delivery_address": selected,
        "prior_order_addresses": [selected], "requires_explicit_selection": False})
    assert body["status"] == "ok" and body["found"] is True
    assert body["selected_delivery_address"]["city"] == "الرياض"
    assert body["selected_delivery_address"]["selected"] is True
    assert body["requires_explicit_selection"] is False


def test_several_candidates_and_no_choice_never_produce_a_current_address(address_binding, monkeypatch):
    """«عند تعدد المرشحين غير المختارين… دون عنوان افتراضي ضمني». The inventory
    is reported and the selection requirement is reported; what is not reported
    is a guess about which one the customer meant."""
    candidates = [_address_fact(), _address_fact(address={"city": "جدة", "district": "",
                                                          "address_line": "", "country": "SA",
                                                          "short_address_code": "", "maps_url": ""})]
    body = _addresses(address_binding, monkeypatch, {
        "address_read_status": "available", "address_read_reason": "ok",
        "address_resolution": "multiple_candidates",
        "saved_addresses": candidates, "selected_delivery_address": None,
        "prior_order_addresses": [], "requires_explicit_selection": True})
    assert body["requires_explicit_selection"] is True
    assert body["selected_delivery_address"] is None
    assert len(body["saved_addresses"]) == 2
    assert all(row["selected"] is False for row in body["saved_addresses"])


def test_an_unreadable_address_source_is_never_reported_as_having_none(address_binding, monkeypatch):
    """The distinction the handoff puts first: an exception is `unavailable`,
    never silently converted to "no address". An outage must not reach the
    customer as a fact about their account."""
    body = _addresses(address_binding, monkeypatch, {
        "address_read_status": "unavailable",
        "address_read_reason": "resolver_unavailable",
        "address_resolution": None, "saved_addresses": [],
        "selected_delivery_address": None, "prior_order_addresses": [],
        "requires_explicit_selection": False})
    assert body["status"] == "unresolved"
    assert body["address_read_status"] == "unavailable"
    assert body["address_read_reason"] == "resolver_unavailable"
    assert body["found"] is False


def test_a_customer_with_no_saved_address_says_exactly_that(address_binding, monkeypatch):
    """And the other side of it: a read that ran and found nothing is
    `available` with an empty inventory, which is a fact about the customer."""
    body = _addresses(address_binding, monkeypatch, {
        "address_read_status": "available", "address_read_reason": "ok",
        "address_resolution": "no_address", "saved_addresses": [],
        "selected_delivery_address": None, "prior_order_addresses": [],
        "requires_explicit_selection": False})
    assert body["status"] == "ok" and body["address_read_status"] == "available"
    assert body["address_resolution"] == "no_address" and body["found"] is False


def test_a_prior_order_address_is_distinguishable_from_an_imported_candidate(address_binding, monkeypatch):
    """Both are "saved"; only one was confirmed on an order. Calling every row a
    prior order address is how an imported profile guess becomes a fact the
    customer supposedly gave us."""
    imported = _address_fact(source="salla_customer_profile")
    confirmed = _address_fact(source="order_confirmed_shipping",
                              address={"city": "جدة", "district": "", "address_line": "",
                                       "country": "SA", "short_address_code": "", "maps_url": ""})
    body = _addresses(address_binding, monkeypatch, {
        "address_read_status": "available", "address_read_reason": "ok",
        "address_resolution": "multiple_candidates",
        "saved_addresses": [imported, confirmed], "selected_delivery_address": None,
        "prior_order_addresses": [confirmed], "requires_explicit_selection": True})
    assert [row["source"] for row in body["saved_addresses"]] == [
        "salla_customer_profile", "order_confirmed_shipping"]
    assert len(body["prior_order_addresses"]) == 1
    assert body["prior_order_addresses"][0]["city"] == "جدة"


def test_the_address_view_carries_no_internal_control_values(address_binding, monkeypatch):
    """The platform's projection already drops the primary key, the content
    fingerprint and the selection-operation reference. The shape is closed here
    too, so a field added upstream cannot reach a customer-facing turn without
    somebody deciding it should."""
    noisy = _address_fact(address_id=41, content_fingerprint="deadbeef",
                          selection_operation_ref="op-99", latitude=24.7, longitude=46.6)
    body = _addresses(address_binding, monkeypatch, {
        "address_read_status": "available", "address_read_reason": "ok",
        "address_resolution": "single_candidate", "saved_addresses": [noisy],
        "selected_delivery_address": None, "prior_order_addresses": [],
        "requires_explicit_selection": False})
    assert set(body["saved_addresses"][0]) == {
        "city", "district", "address_line", "country", "short_address_code", "maps_url",
        "source", "selection_state", "selected", "sufficient_for_delivery",
        "missing_requirements"}
    blob = json.dumps(body, ensure_ascii=False)
    for forbidden in ("address_id", "content_fingerprint", "selection_operation_ref",
                      "latitude", "longitude"):
        assert forbidden not in blob


def test_the_read_is_scoped_to_this_conversations_tenant_and_customer(address_binding, monkeypatch):
    """The tool passes the binding's identity and never accepts one as an
    argument, so a model cannot ask for somebody else's addresses."""
    seen = {}

    def _capture(db, *, tenant_id, customer_id):
        seen.update({"tenant_id": tenant_id, "customer_id": customer_id})
        return {"address_read_status": "available", "address_read_reason": "ok",
                "address_resolution": "no_address", "saved_addresses": [],
                "selected_delivery_address": None, "prior_order_addresses": [],
                "requires_explicit_selection": False}

    monkeypatch.setattr("core.customer_address_candidates."
                        "customer_address_facts_for_trusted_context", _capture, raising=True)

    # The identity it does use is the binding's, on an ordinary call.
    run(address_binding, "get_customer_addresses", {})
    assert seen == {"tenant_id": TENANT, "customer_id": 4_100}

    # And an identity offered as an argument never reaches the read at all: the
    # declared schema takes no properties, so the call is refused before the
    # body runs rather than having the argument quietly ignored.
    seen.clear()
    refused = run(address_binding, "get_customer_addresses",
                  {"tenant_id": 999, "customer_id": 999})
    assert refused.ok is False
    assert seen == {}


def test_the_address_tool_declares_itself_read_only(binding):
    definition = next(d for d in alt.build_live_registry(binding).definitions
                      if d.name == "get_customer_addresses")
    assert definition.read_only is True
    assert definition.input_schema["properties"] == {}


def test_the_rung_the_store_would_actually_serve_reaches_the_observation(binding, monkeypatch):
    """The fact the whole coupon fix turns on, at the boundary the model reads.

    A gold customer in a store that keeps gold from the assistant is served
    silver, and the two facts travel together: without the served rung in the
    observation, a list holding one silver code for a gold customer looks like
    a mistake to apologise for instead of the complete answer it is. The rung
    only — which of the merchant's gates closed which rung stays private.
    """
    capped = {**STANDING, "resolved_level": "gold", "entitled_levels": ["gold"],
              "countable_orders": 7, "served_level": "silver"}
    patch_impl(monkeypatch, "promotions", "list_shareable_promotions_impl",
               async_returning(result("ok", promotions=[promotion(5, coupon_level="silver")],
                                      evidence=[Record("promotion:coupon:5")], query_outcome="ok",
                                      entitlement=capped)))
    observation = run(binding, "list_shareable_promotions", {})
    assert observation.result["entitlement"]["resolved_level"] == "gold"
    assert observation.result["entitlement"]["served_level"] == "silver"


def test_an_empty_list_from_an_unreadable_ladder_says_so_and_names_no_rung(binding, monkeypatch):
    """The read tool could not reach an answer, so the observation carries the
    failure rather than an empty store. ``served_level`` is empty because none
    was selected — not because the customer has none."""
    patch_impl(monkeypatch, "promotions", "list_shareable_promotions_impl",
               async_returning(result("error", failure_reason="coupon_level_policy_unreadable:OSError",
                                      query_outcome="NO_VALID_PROMOTIONS",
                                      entitlement={**STANDING, "resolved_level": "gold",
                                                   "served_level": ""},
                                      withheld={"level_policy_unreadable": 2})))
    observation = run(binding, "list_shareable_promotions", {})
    assert observation.result["status"] == "error"
    assert observation.result["reason"] == "coupon_level_policy_unreadable:OSError"
    assert observation.result["withheld"] == {"level_policy_unreadable": 2}
    assert observation.result["entitlement"]["resolved_level"] == "gold"
    assert observation.result["entitlement"]["served_level"] == ""
