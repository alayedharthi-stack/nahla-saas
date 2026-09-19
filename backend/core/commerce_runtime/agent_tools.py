"""Allowlisted read-only fixture tools for the dormant agent loop.

Three tools with explicit schemas, descriptions, result kinds and error codes:
catalog search, product lookup and merchant knowledge lookup. They read an
in-memory fixture catalogue only; no database, provider, network or mutation.
Tenant, namespace and conversation scope come from the trusted runtime
context (``ToolScope``) the loop builds; model-supplied arguments cannot name
or override that scope, and a request that tries is refused before anything
runs. Results are data: they carry evidence references the verifier can check,
never instructions or authority.
"""
from __future__ import annotations

import concurrent.futures
import dataclasses
import json
from typing import Any, Callable, Dict, Mapping, Sequence, Tuple

from core.commerce_runtime import agent_contracts as ac
from core.commerce_runtime import contracts as c

_MAX_QUERY_LENGTH = 200
_MAX_RESULTS = 5


@dataclasses.dataclass(frozen=True)
class ToolScope:
    """Trusted scope of one loop run. Built by the loop, never by the provider."""

    tenant_id: int
    namespace: str
    conversation_id: int
    turn_id: int


@dataclasses.dataclass(frozen=True)
class FixtureProduct:
    ref: str                                 # evidence reference, e.g. "product:blue-shirt"
    name: str
    price: str                               # decimal as text; a fixture, not money handling
    currency: str
    in_stock: bool
    tags: Tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class FixtureKnowledge:
    ref: str                                 # evidence reference, e.g. "kb:shipping"
    topic: str
    text: str


@dataclasses.dataclass(frozen=True)
class FixtureCatalog:
    """Per-tenant fixture data. Only the scope's tenant is ever consulted."""

    products: Mapping[int, Tuple[FixtureProduct, ...]]      # tenant_id -> products
    knowledge: Mapping[int, Tuple[FixtureKnowledge, ...]]   # tenant_id -> entries


@dataclasses.dataclass(frozen=True)
class ToolResult:
    result: Mapping[str, Any]
    evidence_refs: Tuple[str, ...]


ToolFunction = Callable[[ToolScope, Mapping[str, Any]], ToolResult]


@dataclasses.dataclass(frozen=True)
class RegisteredTool:
    definition: ac.ToolDefinition
    function: ToolFunction


# ── Argument validation against the declared schema (closed, small) ──────────


def _validate_arguments(schema: Mapping[str, Any], arguments: Mapping[str, Any]) -> Dict[str, Any]:
    forbidden = sorted(set(arguments) & ac.RESERVED_SCOPE_ARGUMENTS)
    if forbidden:
        raise ac.ToolError(ac.ToolErrorCode.SCOPE_OVERRIDE_REFUSED.value,
                           f"scope is supplied by the runtime, not by arguments: {', '.join(forbidden)}")
    properties: Mapping[str, Any] = schema.get("properties", {})
    unknown = sorted(set(arguments) - set(properties))
    if unknown:
        raise ac.ToolError(ac.ToolErrorCode.INVALID_ARGUMENTS.value, f"unknown argument(s): {', '.join(unknown)}")
    for name in schema.get("required", ()):
        if name not in arguments:
            raise ac.ToolError(ac.ToolErrorCode.INVALID_ARGUMENTS.value, f"missing required argument: {name}")
    cleaned: Dict[str, Any] = {}
    for name, value in arguments.items():
        spec = properties[name]
        expected = spec.get("type")
        if expected == "string":
            if not isinstance(value, str):
                raise ac.ToolError(ac.ToolErrorCode.INVALID_ARGUMENTS.value, f"{name} must be a string")
            if len(value) > int(spec.get("maxLength", _MAX_QUERY_LENGTH)):
                raise ac.ToolError(ac.ToolErrorCode.INVALID_ARGUMENTS.value, f"{name} is too long")
        elif expected == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ac.ToolError(ac.ToolErrorCode.INVALID_ARGUMENTS.value, f"{name} must be an integer")
            if value < int(spec.get("minimum", 0)) or value > int(spec.get("maximum", 1_000_000)):
                raise ac.ToolError(ac.ToolErrorCode.INVALID_ARGUMENTS.value, f"{name} is out of range")
        else:
            raise ac.ToolError(ac.ToolErrorCode.INVALID_ARGUMENTS.value, f"{name} has an unsupported type")
        cleaned[name] = value
    return cleaned


# ── The three fixture tools ──────────────────────────────────────────────────


def _product_view(product: FixtureProduct) -> Dict[str, Any]:
    return {"ref": product.ref, "name": product.name, "price": product.price, "currency": product.currency,
            "in_stock": product.in_stock, "tags": list(product.tags)}


def _catalog_search(catalog: FixtureCatalog) -> ToolFunction:
    def run(scope: ToolScope, arguments: Mapping[str, Any]) -> ToolResult:
        query = arguments["query"].strip().lower()
        limit = int(arguments.get("limit", _MAX_RESULTS))
        products = catalog.products.get(scope.tenant_id, ())
        matches = [p for p in products if not query or query in p.name.lower() or query in " ".join(p.tags).lower()]
        chosen = matches[:limit]
        return ToolResult(result={"query": arguments["query"], "products": [_product_view(p) for p in chosen],
                                  "total_matches": len(matches)},
                          evidence_refs=tuple(p.ref for p in chosen))
    return run


def _product_lookup(catalog: FixtureCatalog) -> ToolFunction:
    def run(scope: ToolScope, arguments: Mapping[str, Any]) -> ToolResult:
        ref = arguments["product_ref"]
        for product in catalog.products.get(scope.tenant_id, ()):
            if product.ref == ref:
                return ToolResult(result={"product": _product_view(product)}, evidence_refs=(product.ref,))
        raise ac.ToolError(ac.ToolErrorCode.TOOL_FAILURE.value, "product not found for this merchant")
    return run


def _knowledge_lookup(catalog: FixtureCatalog) -> ToolFunction:
    def run(scope: ToolScope, arguments: Mapping[str, Any]) -> ToolResult:
        topic = arguments["topic"].strip().lower()
        entries = [e for e in catalog.knowledge.get(scope.tenant_id, ()) if topic in e.topic.lower()]
        chosen = entries[:_MAX_RESULTS]
        return ToolResult(result={"topic": arguments["topic"],
                                  "entries": [{"ref": e.ref, "topic": e.topic, "text": e.text} for e in chosen]},
                          evidence_refs=tuple(e.ref for e in chosen))
    return run


def build_fixture_tools(catalog: FixtureCatalog) -> Tuple[RegisteredTool, ...]:
    return (
        RegisteredTool(
            ac.ToolDefinition(
                name="catalog_search",
                description="Search the merchant's product catalogue by name or tag. Read-only. Returns up to "
                            "five products with price, currency and stock, each with an evidence reference.",
                input_schema={"type": "object", "additionalProperties": False,
                              "properties": {"query": {"type": "string", "maxLength": _MAX_QUERY_LENGTH},
                                             "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_RESULTS}},
                              "required": ["query"]},
                result_kind="product_list"),
            _catalog_search(catalog)),
        RegisteredTool(
            ac.ToolDefinition(
                name="product_lookup",
                description="Look up one product of the merchant by the evidence reference a previous catalogue "
                            "search returned. Read-only.",
                input_schema={"type": "object", "additionalProperties": False,
                              "properties": {"product_ref": {"type": "string", "maxLength": ac.MAX_EVIDENCE_REF_LENGTH}},
                              "required": ["product_ref"]},
                result_kind="product"),
            _product_lookup(catalog)),
        RegisteredTool(
            ac.ToolDefinition(
                name="merchant_knowledge_lookup",
                description="Look up the merchant's own knowledge entries (shipping, returns, opening hours) by "
                            "topic. Read-only. Returns entries with evidence references.",
                input_schema={"type": "object", "additionalProperties": False,
                              "properties": {"topic": {"type": "string", "maxLength": _MAX_QUERY_LENGTH}},
                              "required": ["topic"]},
                result_kind="knowledge_entry"),
            _knowledge_lookup(catalog)),
    )


# ── Registry: allowlist, validation, bounded execution ───────────────────────


class ToolRegistry:
    """The allowlist the loop exposes, and the sole authority on how a request
    is validated.

    The registry keeps its own private copy of every schema and never hands it
    out: ``definitions`` returns detached copies built fresh on each call, so
    nothing a provider (or any other holder) does to what it was given can
    change what the registry validates against. A tool whose definition is not
    read-only is refused at registration.
    """

    def __init__(self, tools: Sequence[RegisteredTool]) -> None:
        self._tools: Dict[str, RegisteredTool] = {}
        self._schemas: Dict[str, Dict[str, Any]] = {}
        for tool in tools:
            name = ac.validate_tool_name(tool.definition.name)
            if not tool.definition.read_only:
                raise c.ValidationError(f"tool {name} is not read-only; only read-only tools may be registered")
            if name in self._tools:
                raise c.ValidationError(f"tool {name} registered twice")
            if not isinstance(tool.definition.input_schema, Mapping):
                raise c.ValidationError(f"tool {name} must declare a mapping input schema")
            self._tools[name] = tool
            # The authoritative schema: a private deep copy, never shared.
            self._schemas[name] = ac.public_copy(tool.definition.input_schema)

    @property
    def definitions(self) -> Tuple[ac.ToolDefinition, ...]:
        """Provider-visible definitions, detached from the authoritative ones."""
        return tuple(
            dataclasses.replace(tool.definition, input_schema=ac.public_copy(self._schemas[name]))
            for name, tool in self._tools.items()
        )

    def execute(self, scope: ToolScope, request: ac.ToolRequest, *, timeout_seconds: float) -> ac.ToolObservation:
        """Validate, then run the tool in a worker thread bounded by ``timeout_seconds``.

        Refusals (unknown tool, invalid arguments, scope override) never run
        anything. A timeout abandons the call and the observation says so; the
        abandoned thread may still be running and its late value is discarded,
        which is a local abandonment, not proof that the work stopped.
        """
        if timeout_seconds <= 0:
            return self._refusal(request, ac.ToolErrorCode.TIMEOUT.value,
                                 "no execution time remained inside the turn's deadline")
        tool = self._tools.get(request.tool_name)
        if tool is None:
            return self._refusal(request, ac.ToolErrorCode.UNKNOWN_TOOL.value, "no such tool is exposed")
        try:
            # Validated against the registry's private schema, never against a
            # copy any caller could have altered.
            arguments = _validate_arguments(self._schemas[request.tool_name], request.arguments)
        except ac.ToolError as exc:
            return self._refusal(request, exc.code, str(exc))
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(tool.function, scope, arguments)
            try:
                outcome = future.result(timeout=timeout_seconds)
            except concurrent.futures.TimeoutError:
                return self._refusal(request, ac.ToolErrorCode.TIMEOUT.value,
                                     f"the tool did not answer within {timeout_seconds}s; its result is discarded")
            except ac.ToolError as exc:
                return self._refusal(request, exc.code, str(exc))
            except Exception as exc:  # noqa: BLE001 - a tool bug is an observation, never a crash of the loop
                return self._refusal(request, ac.ToolErrorCode.TOOL_FAILURE.value, type(exc).__name__)
        finally:
            executor.shutdown(wait=False)
        result = ac.public_copy(outcome.result)
        if len(json.dumps(result, ensure_ascii=False, sort_keys=True).encode("utf-8")) > ac.MAX_OBSERVATION_BYTES:
            return self._refusal(request, ac.ToolErrorCode.RESULT_TOO_LARGE.value, "the tool result exceeds the bound")
        refs = tuple(ac.validate_evidence_ref(r) for r in outcome.evidence_refs)
        return ac.ToolObservation(call_id=request.call_id, tool_name=request.tool_name, ok=True, result=result,
                                  error_code=None, error=None, evidence_refs=refs)

    @staticmethod
    def _refusal(request: ac.ToolRequest, code: str, message: str) -> ac.ToolObservation:
        return ac.ToolObservation(call_id=request.call_id, tool_name=request.tool_name, ok=False, result=None,
                                  error_code=code, error=message, evidence_refs=())


__all__ = ["FixtureCatalog", "FixtureKnowledge", "FixtureProduct", "RegisteredTool", "ToolRegistry", "ToolResult",
           "ToolScope", "build_fixture_tools"]
