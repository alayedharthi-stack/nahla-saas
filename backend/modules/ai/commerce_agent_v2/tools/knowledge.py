"""Tenant-bound wrappers around the deterministic merchant knowledge retrieval."""
from __future__ import annotations

from typing import Any

from agents import RunContextWrapper

from modules.ai.commerce_agent_v2.context import CommerceAgentContext
from modules.ai.commerce_agent_v2.knowledge_retrieval import (
    SCOPE_PRODUCT,
    SCOPE_TURN,
    STATUS_NO_RESULTS,
    STATUS_OK,
    build_knowledge_snapshots,
    retrieved_sections,
    run_knowledge_lookup,
)
from modules.ai.commerce_agent_v2.tool_runtime import commerce_read_tool
from modules.ai.commerce_agent_v2.output import KnowledgeSearchResult
from modules.ai.commerce_agent_v2.tools.catalog import _catalog_search_enabled


def _merchant_knowledge_enabled(
    run_context: RunContextWrapper[CommerceAgentContext],
    _agent: Any,
) -> bool:
    """Run this turn's deterministic merchant-wide lookup and expose the tool by it.

    Phase 2.7A probed the knowledge base once per token of the customer message
    purely to decide whether to show this tool, then threw the results away: the
    cost was unbounded in the number of tokens and no attempt was ever recorded.
    One bounded lookup now runs instead, its outcome is recorded in the run
    ledger, and the tool is offered when that lookup found merchant-wide
    knowledge — or when retrieval failed, so operational trouble is never
    mistaken for "the merchant documents nothing".
    """
    if not _catalog_search_enabled(run_context, _agent):
        return False
    context = run_context.context
    record = run_knowledge_lookup(
        context,
        scope=SCOPE_TURN,
        purpose="turn_store_knowledge",
        query=context.run_user_input,
    )
    status = str(record.get("status") or "")
    if status in {STATUS_OK}:
        return context.cache_merchant_knowledge_relevance(True)
    if status == STATUS_NO_RESULTS:
        return context.cache_merchant_knowledge_relevance(False)
    # timeout, error, skipped or budget exhausted: fail open.
    return context.cache_merchant_knowledge_relevance(True)


def _result_from_rows(
    context: CommerceAgentContext,
    rows: list[dict],
    *,
    source: str,
    required_product_id: int | None,
) -> KnowledgeSearchResult:
    snapshots, evidence = build_knowledge_snapshots(
        context,
        rows,
        source=source,
        required_product_id=required_product_id,
    )
    context.register_evidence(evidence)
    if not snapshots:
        return KnowledgeSearchResult(
            status="no_evidence",
            failure_reason="no_matching_knowledge",
        )
    return KnowledgeSearchResult(status="ok", sections=snapshots, evidence=evidence)


@commerce_read_tool("search_merchant_knowledge", is_enabled=_merchant_knowledge_enabled)
async def search_merchant_knowledge(
    run_context: RunContextWrapper[CommerceAgentContext],
    query: str,
    limit: int,
) -> KnowledgeSearchResult:
    """Search only global, AI-visible knowledge for the current merchant."""
    context = run_context.context
    context.assert_scope()
    text = str(query or "") or context.run_user_input
    record = run_knowledge_lookup(
        context,
        scope=SCOPE_TURN,
        purpose="model_store_knowledge",
        query=text,
    )
    if str(record.get("status") or "") not in {STATUS_OK, STATUS_NO_RESULTS}:
        return KnowledgeSearchResult(
            status="error", failure_reason="knowledge_retrieval_failed"
        )
    rows = retrieved_sections(context, scope=SCOPE_TURN, query=text)
    return _result_from_rows(
        context, rows, source="merchant_knowledge", required_product_id=None
    )


@commerce_read_tool("search_product_knowledge", is_enabled=_catalog_search_enabled)
async def search_product_knowledge(
    run_context: RunContextWrapper[CommerceAgentContext],
    product_id: int,
    query: str,
    limit: int,
) -> KnowledgeSearchResult:
    """Search AI-visible knowledge linked to an earlier discovered product."""
    context = run_context.context
    context.assert_scope()
    context.require_authorized_product(product_id)
    text = str(query or "") or context.run_user_input
    record = run_knowledge_lookup(
        context,
        scope=SCOPE_PRODUCT,
        purpose="model_product_knowledge",
        query=text,
        product_ids=[int(product_id)],
    )
    if str(record.get("status") or "") not in {STATUS_OK, STATUS_NO_RESULTS}:
        return KnowledgeSearchResult(
            status="error", failure_reason="knowledge_retrieval_failed"
        )
    rows = retrieved_sections(
        context, scope=SCOPE_PRODUCT, query=text, product_ids=[int(product_id)]
    )
    return _result_from_rows(
        context,
        rows,
        source="product_knowledge",
        required_product_id=int(product_id),
    )
