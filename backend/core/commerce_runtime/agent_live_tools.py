"""The merchant's real read-only data, exposed to the loop as allowlisted tools.

Six tools — catalog search, product lookup, merchant knowledge, order
resolution, order details and shipment facts — each backed by the *same*
implementation the Commerce Agent V2 read tools call. Nothing here queries the
database itself and nothing here writes: no order is created or changed, no
payment is taken, no cancellation is made, no message is sent. A tool whose
implementation is not one of those reads cannot be registered, because the
registry refuses anything that is not declared read-only.

Scope is not negotiable and is never taken from the model. The trusted
``CommerceAgentContext`` is built once, by the runtime, from the verified
tenant, WhatsApp connection, conversation and customer; every call re-checks
that the loop's ``ToolScope`` is the scope that context was built for and
refuses otherwise. ``tenant_id``, ``customer_id`` and the phone number are not
tool arguments and cannot be supplied.

Provenance and uncertainty survive the projection. Each result keeps the
evidence references the underlying tool produced, and a lookup that found
nothing, was denied or could not run says which of those it was — it never
reports an empty answer as a fact.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import threading
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from core.commerce_runtime import agent_contracts as ac
from core.commerce_runtime import agent_tools as at

logger = logging.getLogger("nahla.commerce_runtime.agent_live_tools")

MAX_QUERY_LENGTH = 200
MAX_SEARCH_LIMIT = 5
MAX_DESCRIPTION_CHARS = 400
MAX_KNOWLEDGE_CHARS = 700
MAX_KNOWLEDGE_SECTIONS = 4
MAX_LINE_ITEMS = 12

_ORDER_PURPOSES = ("status", "shipment")


class LiveToolsUnavailable(RuntimeError):
    """The trusted context for these tools could not be established."""


@dataclasses.dataclass
class LiveToolBinding:
    """The one trusted context these tools may read, and the scope it is for.

    The database session inside ``context`` belongs to this binding alone and
    is used only by tool calls. A call that the loop abandoned on a timeout may
    still be running on that session, so the binding is *poisoned* from that
    moment: every later call is refused rather than sharing a session with a
    thread nobody is waiting for. Abandonment is local; it is not proof the
    abandoned work stopped.
    """

    context: Any
    tenant_id: int
    conversation_id: int
    _lock: threading.Lock = dataclasses.field(default_factory=threading.Lock, repr=False)
    _poisoned: Optional[str] = None
    _in_flight: int = 0

    @property
    def poisoned(self) -> Optional[str]:
        return self._poisoned

    @property
    def abandoned_calls(self) -> int:
        return self._in_flight

    def check(self, scope: at.ToolScope) -> None:
        if scope.tenant_id != self.tenant_id or scope.conversation_id != self.conversation_id:
            raise ac.ToolError(
                ac.ToolErrorCode.SCOPE_OVERRIDE_REFUSED.value,
                "this tool is bound to another tenant/conversation scope",
            )
        if self._poisoned is not None:
            raise ac.ToolError(ac.ToolErrorCode.TOOL_FAILURE.value, self._poisoned)

    def poison(self, reason: str) -> None:
        """Close the binding for good. Idempotent; the first reason is kept."""
        with self._lock:
            if self._poisoned is None:
                self._poisoned = reason

    def enter(self) -> None:
        """Take the binding for one call, or refuse because a call is still running.

        The registry runs one tool at a time and waits for it, so finding a call
        already in flight can only mean the loop abandoned an earlier one on its
        timeout while the work kept going. Two threads must not share this
        session, and the state the abandoned call may still write is not state a
        later call may read, so the binding closes permanently at that point.
        """
        with self._lock:
            if self._in_flight > 0:
                if self._poisoned is None:
                    self._poisoned = "an earlier tool call was abandoned and is still running"
                raise ac.ToolError(ac.ToolErrorCode.TOOL_FAILURE.value, self._poisoned)
            self._in_flight += 1

    def leave(self) -> None:
        """Release the binding. A refusal releases it exactly like a result does:
        this thread is finished either way."""
        with self._lock:
            self._in_flight = max(0, self._in_flight - 1)


def _run(coro: Any) -> Any:
    """Run one read coroutine to completion in this worker thread.

    The registry already runs every tool in its own thread, so there is no loop
    here to interfere with and none is left behind.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        try:
            loop.close()
        finally:
            asyncio.set_event_loop(None)


def _text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return text[:limit]


def _refs(records: Sequence[Any]) -> Tuple[str, ...]:
    out: List[str] = []
    for record in records or ():
        ref = getattr(record, "ref", None)
        if isinstance(ref, str) and ref:
            out.append(ref)
    return tuple(dict.fromkeys(out))


def _unresolved(status: Any, failure_reason: Any, **extra: Any) -> at.ToolResult:
    """A lookup that produced no fact, said honestly as data."""
    body: Dict[str, Any] = {"status": str(status or "no_evidence"), "found": False}
    if failure_reason:
        body["reason"] = str(failure_reason)
    body.update(extra)
    return at.ToolResult(result=body, evidence_refs=())


def _product_view(snapshot: Any) -> Dict[str, Any]:
    return {
        "product_id": getattr(snapshot, "product_id", None),
        "evidence_ref": getattr(snapshot, "evidence_ref", None),
        "title": _text(getattr(snapshot, "title", ""), 200),
        "description": _text(getattr(snapshot, "description", ""), MAX_DESCRIPTION_CHARS),
        "price": getattr(snapshot, "price", None),
        "sale_price": getattr(snapshot, "sale_price", None),
        "currency": getattr(snapshot, "currency", None),
        "in_stock": getattr(snapshot, "in_stock", None),
        "stock_quantity": getattr(snapshot, "stock_quantity", None),
        "orderable": bool(getattr(snapshot, "orderable", False)),
        "product_url": _text(getattr(snapshot, "product_url", ""), 300),
        "image_url": _text(getattr(snapshot, "image_url", ""), 300),
    }


def _knowledge_view(sections: Sequence[Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for section in list(sections or ())[:MAX_KNOWLEDGE_SECTIONS]:
        out.append({
            "evidence_ref": getattr(section, "evidence_ref", None),
            "kind": _text(getattr(section, "kind", ""), 64),
            "title": _text(getattr(section, "title", ""), 160),
            "body": _text(getattr(section, "body", ""), MAX_KNOWLEDGE_CHARS),
        })
    return out


def _order_view(summary: Any) -> Dict[str, Any]:
    return {
        "order_id": getattr(summary, "order_id", None),
        "evidence_ref": getattr(summary, "evidence_ref", None),
        "order_reference": getattr(summary, "order_reference", None),
        "status": getattr(summary, "status", None),
        "status_label": getattr(summary, "status_label", None),
    }


# ── Tool bodies ──────────────────────────────────────────────────────────────


def _guarded(binding: LiveToolBinding, body: Callable[[Mapping[str, Any]], at.ToolResult]) -> at.ToolFunction:
    def run(scope: at.ToolScope, arguments: Mapping[str, Any]) -> at.ToolResult:
        binding.check(scope)
        binding.enter()
        try:
            return body(arguments)
        finally:
            binding.leave()
    return run


def _catalog_search(binding: LiveToolBinding) -> at.ToolFunction:
    from modules.ai.commerce_agent_v2.tools.catalog import search_products_impl

    def body(arguments: Mapping[str, Any]) -> at.ToolResult:
        limit = int(arguments.get("limit") or MAX_SEARCH_LIMIT)
        result = _run(search_products_impl(
            binding.context, query=str(arguments.get("query") or ""), limit=min(limit, MAX_SEARCH_LIMIT)))
        if getattr(result, "status", "") != "ok":
            return _unresolved(getattr(result, "status", None), getattr(result, "failure_reason", None))
        products = [_product_view(p) for p in (getattr(result, "products", None) or ())]
        payload: Dict[str, Any] = {"status": "ok", "found": bool(products), "products": products}
        sections = _knowledge_view(getattr(result, "knowledge_sections", None) or ())
        if sections:
            payload["product_knowledge"] = sections
        return at.ToolResult(result=payload, evidence_refs=_refs(getattr(result, "evidence", None) or ()))

    return _guarded(binding, body)


def _product_lookup(binding: LiveToolBinding) -> at.ToolFunction:
    from modules.ai.commerce_agent_v2.tools.catalog import get_product_details_impl

    def body(arguments: Mapping[str, Any]) -> at.ToolResult:
        result = _run(get_product_details_impl(binding.context, product_id=int(arguments["product_id"])))
        if getattr(result, "status", "") != "ok":
            return _unresolved(getattr(result, "status", None), getattr(result, "failure_reason", None))
        payload: Dict[str, Any] = {"status": "ok", "found": True,
                                   "product": _product_view(getattr(result, "product", None))}
        sections = _knowledge_view(getattr(result, "knowledge_sections", None) or ())
        if sections:
            payload["product_knowledge"] = sections
        return at.ToolResult(result=payload, evidence_refs=_refs(getattr(result, "evidence", None) or ()))

    return _guarded(binding, body)


def _merchant_knowledge(binding: LiveToolBinding) -> at.ToolFunction:
    from modules.ai.commerce_agent_v2.tools.knowledge import search_merchant_knowledge_impl

    def body(arguments: Mapping[str, Any]) -> at.ToolResult:
        result = _run(search_merchant_knowledge_impl(
            binding.context, query=str(arguments.get("query") or ""),
            limit=int(arguments.get("limit") or MAX_KNOWLEDGE_SECTIONS)))
        if getattr(result, "status", "") != "ok":
            return _unresolved(getattr(result, "status", None), getattr(result, "failure_reason", None))
        sections = _knowledge_view(getattr(result, "sections", None) or ())
        return at.ToolResult(
            result={"status": "ok", "found": bool(sections), "sections": sections},
            evidence_refs=_refs(getattr(result, "evidence", None) or ()),
        )

    return _guarded(binding, body)


def _order_lookup(binding: LiveToolBinding) -> at.ToolFunction:
    from modules.ai.commerce_agent_v2.tools.orders import resolve_customer_order_impl

    def body(arguments: Mapping[str, Any]) -> at.ToolResult:
        purpose = str(arguments.get("purpose") or "status")
        if purpose not in _ORDER_PURPOSES:
            raise ac.ToolError(ac.ToolErrorCode.INVALID_ARGUMENTS.value,
                               f"purpose must be one of {', '.join(_ORDER_PURPOSES)}")
        result = _run(resolve_customer_order_impl(
            binding.context, order_number=str(arguments.get("order_number") or ""), purpose=purpose))
        if getattr(result, "status", "") != "ok":
            return _unresolved(getattr(result, "status", None), getattr(result, "failure_reason", None),
                               selection_reason=getattr(result, "selection_reason", None))
        return at.ToolResult(
            result={"status": "ok", "found": True, "order": _order_view(getattr(result, "order", None)),
                    "selection_reason": getattr(result, "selection_reason", None)},
            evidence_refs=_refs(getattr(result, "evidence", None) or ()),
        )

    return _guarded(binding, body)


def _order_details(binding: LiveToolBinding) -> at.ToolFunction:
    from modules.ai.commerce_agent_v2.tools.orders import get_order_details_impl

    def body(arguments: Mapping[str, Any]) -> at.ToolResult:
        result = _run(get_order_details_impl(binding.context, order_id=int(arguments["order_id"])))
        if getattr(result, "status", "") != "ok":
            return _unresolved(getattr(result, "status", None), getattr(result, "failure_reason", None))
        details = getattr(result, "order", None)
        items = []
        for item in list(getattr(details, "line_items", None) or ())[:MAX_LINE_ITEMS]:
            items.append({"name": _text(getattr(item, "name", ""), 160),
                          "quantity": getattr(item, "quantity", None)})
        return at.ToolResult(
            result={"status": "ok", "found": True,
                    "order": {"order_id": getattr(details, "order_id", None),
                              "evidence_ref": getattr(details, "evidence_ref", None),
                              "order_reference": getattr(details, "order_reference", None),
                              "total": getattr(details, "total", None),
                              "currency": getattr(details, "currency", None),
                              "line_items": items}},
            evidence_refs=_refs(getattr(result, "evidence", None) or ()),
        )

    return _guarded(binding, body)


def _shipment_lookup(binding: LiveToolBinding) -> at.ToolFunction:
    from modules.ai.commerce_agent_v2.tools.orders import get_order_shipment_impl

    def body(arguments: Mapping[str, Any]) -> at.ToolResult:
        result = _run(get_order_shipment_impl(binding.context, order_id=int(arguments["order_id"])))
        if getattr(result, "status", "") != "ok":
            return _unresolved(getattr(result, "status", None), getattr(result, "failure_reason", None))
        shipment = getattr(result, "shipment", None)
        return at.ToolResult(
            result={"status": "ok", "found": True,
                    "shipment": {"order_id": getattr(shipment, "order_id", None),
                                 "evidence_ref": getattr(shipment, "evidence_ref", None),
                                 "order_reference": getattr(shipment, "order_reference", None),
                                 "shipment_status": getattr(shipment, "shipment_status", None),
                                 "shipment_status_label": getattr(shipment, "shipment_status_label", None),
                                 "carrier": getattr(shipment, "carrier", None),
                                 "tracking_number": getattr(shipment, "tracking_number", None),
                                 "tracking_url": getattr(shipment, "tracking_url", None)}},
            evidence_refs=_refs(getattr(result, "evidence", None) or ()),
        )

    return _guarded(binding, body)


# ── Declarations ─────────────────────────────────────────────────────────────

_QUERY = {"type": "string", "maxLength": MAX_QUERY_LENGTH}
_LIMIT = {"type": "integer", "minimum": 1, "maximum": MAX_SEARCH_LIMIT}
_ID = {"type": "integer", "minimum": 1, "maximum": 2_147_483_647}

_DECLARATIONS: Tuple[Tuple[str, str, Dict[str, Any], str, Callable[[LiveToolBinding], at.ToolFunction]], ...] = (
    (
        "catalog_search",
        "Search this merchant's synced catalog. An empty query browses the merchant's "
        "top available products. Returns products with their evidence references.",
        {"type": "object", "properties": {"query": _QUERY, "limit": _LIMIT}, "required": []},
        "product_list",
        _catalog_search,
    ),
    (
        "product_lookup",
        "Exact details for one product already returned by catalog_search in this turn.",
        {"type": "object", "properties": {"product_id": _ID}, "required": ["product_id"]},
        "product",
        _product_lookup,
    ),
    (
        "merchant_knowledge_lookup",
        "Search this merchant's own published knowledge, such as delivery, returns, "
        "branches or policies.",
        {"type": "object", "properties": {"query": _QUERY, "limit": _LIMIT}, "required": []},
        "knowledge_entry",
        _merchant_knowledge,
    ),
    (
        "order_lookup",
        "Resolve one order belonging to this customer. order_number is only an optional "
        "lookup key; the customer is taken from trusted context. Use purpose 'shipment' "
        "for a shipping or tracking question and 'status' otherwise.",
        {"type": "object",
         "properties": {"order_number": {"type": "string", "maxLength": 64},
                        "purpose": {"type": "string", "maxLength": 16}},
         "required": []},
        "order_summary",
        _order_lookup,
    ),
    (
        "order_details",
        "Total and line items for an order returned by order_lookup in this turn.",
        {"type": "object", "properties": {"order_id": _ID}, "required": ["order_id"]},
        "order_details",
        _order_details,
    ),
    (
        "shipment_lookup",
        "Shipment, carrier and tracking facts for an order returned by order_lookup in this turn.",
        {"type": "object", "properties": {"order_id": _ID}, "required": ["order_id"]},
        "shipment",
        _shipment_lookup,
    ),
)

LIVE_TOOL_NAMES: Tuple[str, ...] = tuple(name for name, *_ in _DECLARATIONS)


def build_live_tools(binding: LiveToolBinding) -> Tuple[at.RegisteredTool, ...]:
    """The allowlisted read-only tools for one trusted context."""
    return tuple(
        at.RegisteredTool(
            definition=ac.ToolDefinition(name=name, description=description, input_schema=schema,
                                         result_kind=kind, read_only=True),
            function=factory(binding),
        )
        for name, description, schema, kind, factory in _DECLARATIONS
    )


def build_live_registry(binding: LiveToolBinding) -> at.ToolRegistry:
    return at.ToolRegistry(build_live_tools(binding))


__all__ = [
    "LIVE_TOOL_NAMES", "LiveToolBinding", "LiveToolsUnavailable", "MAX_SEARCH_LIMIT",
    "build_live_registry", "build_live_tools",
]
