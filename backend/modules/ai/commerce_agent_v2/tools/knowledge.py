"""Tenant-bound wrappers around the existing merchant knowledge retrieval."""
from __future__ import annotations

import re
import unicodedata
from typing import Any

from agents import RunContextWrapper

from modules.ai.brain.commerce.product_knowledge_or_comparison import (
    retrieve_catalog_candidate_kb_sections,
)
from modules.ai.commerce_agent_v2.context import CommerceAgentContext
from modules.ai.commerce_agent_v2.tool_runtime import commerce_read_tool
from modules.ai.commerce_agent_v2.output import (
    CanonicalEvidenceFact,
    EvidenceRecord,
    KnowledgeSearchResult,
    KnowledgeSectionSnapshot,
)
from modules.ai.commerce_agent_v2.tools.catalog import _catalog_search_enabled
from modules.ai.security.tenant_isolation import TenantIsolationLayer


_ARABIC_DIACRITICS_RE = re.compile(r"[\u064b-\u065f\u0670\u06d6-\u06ed]+")
_ROUTING_WORD_RE = re.compile(r"[^\w]+", re.UNICODE)


def _routing_tokens(value: str) -> set[str]:
    """Return conservative lexical topics for deterministic evidence routing."""
    normalized = unicodedata.normalize("NFKC", str(value or "").lower())
    normalized = _ARABIC_DIACRITICS_RE.sub("", normalized)
    normalized = (
        normalized.replace("أ", "ا")
        .replace("إ", "ا")
        .replace("آ", "ا")
        .replace("ى", "ي")
        .replace("ة", "ه")
    )
    tokens: set[str] = set()
    for raw in _ROUTING_WORD_RE.sub(" ", normalized).split():
        token = raw[2:] if raw.startswith("ال") and len(raw) >= 5 else raw
        if len(token) >= 3:
            tokens.add(token)
    return tokens


def _merchant_knowledge_enabled(
    run_context: RunContextWrapper[CommerceAgentContext],
    _agent: Any,
) -> bool:
    """Expose global KB search only when the current turn has global evidence.

    This is evidence-driven rather than an intent classifier.  Product-linked
    sections are already excluded by the existing tenant-safe retrieval when
    no product id is supplied.  A successful empty probe therefore means a
    merchant-wide lookup cannot add evidence for this turn.  Retrieval errors
    fail open so operational uncertainty is never mistaken for fact absence.
    """
    if not _catalog_search_enabled(run_context, _agent):
        return False
    context = run_context.context
    cached = context.merchant_knowledge_relevance
    if cached is not None:
        return cached
    query_tokens = _routing_tokens(context.run_user_input)
    if not query_tokens:
        return context.cache_merchant_knowledge_relevance(True)

    for token in sorted(query_tokens, key=lambda item: (-len(item), item)):
        payload = retrieve_catalog_candidate_kb_sections(
            context.db,
            context.tenant_id,
            subject=token,
            message=token,
            limit=6,
        )
        if payload.get("kb_retrieval_failed"):
            return context.cache_merchant_knowledge_relevance(True)
        for section in payload.get("kb_sections") or []:
            section_tokens = _routing_tokens(
                f"{section.get('title', '')} {section.get('body', '')}"
            )
            if token in section_tokens:
                return context.cache_merchant_knowledge_relevance(True)
    return context.cache_merchant_knowledge_relevance(False)


def _linked_products(context: CommerceAgentContext, section_ids: list[int]) -> dict[int, list[int]]:
    from models import MerchantKnowledgeSection, MerchantKnowledgeSectionProduct, Product

    if not section_ids:
        return {}
    rows = (
        context.db.query(
            MerchantKnowledgeSectionProduct.section_id,
            MerchantKnowledgeSectionProduct.product_id,
        )
        .join(
            MerchantKnowledgeSection,
            MerchantKnowledgeSection.id == MerchantKnowledgeSectionProduct.section_id,
        )
        .join(Product, Product.id == MerchantKnowledgeSectionProduct.product_id)
        .filter(
            MerchantKnowledgeSection.id.in_(section_ids),
            MerchantKnowledgeSection.tenant_id == context.tenant_id,
            Product.tenant_id == context.tenant_id,
        )
        .all()
    )
    linked: dict[int, list[int]] = {section_id: [] for section_id in section_ids}
    for section_id, product_id in rows:
        linked.setdefault(int(section_id), []).append(int(product_id))
    return linked


def _build_result(
    context: CommerceAgentContext,
    payload: dict,
    *,
    source: str,
    required_product_id: int | None,
) -> KnowledgeSearchResult:
    from models import MerchantKnowledgeSection

    if payload.get("kb_retrieval_failed"):
        return KnowledgeSearchResult(status="error", failure_reason="knowledge_retrieval_failed")
    raw_sections = list(payload.get("kb_sections") or [])
    section_ids = [int(row["section_id"]) for row in raw_sections if row.get("section_id")]
    db_sections = (
        context.db.query(MerchantKnowledgeSection)
        .filter(
            MerchantKnowledgeSection.id.in_(section_ids),
            MerchantKnowledgeSection.tenant_id == context.tenant_id,
        )
        .all()
        if section_ids
        else []
    )
    by_id = {int(row.id): row for row in db_sections}
    if set(by_id) != set(section_ids):
        raise RuntimeError("knowledge_service_returned_out_of_scope_section")
    for row in db_sections:
        TenantIsolationLayer.assert_belongs(row, context.tenant_context)
    links = _linked_products(context, section_ids)

    snapshots: list[KnowledgeSectionSnapshot] = []
    evidence: list[EvidenceRecord] = []
    for raw in raw_sections:
        section_id = int(raw["section_id"])
        linked_ids = sorted(set(links.get(section_id, [])))
        if required_product_id is None and linked_ids:
            continue
        if required_product_id is not None and required_product_id not in linked_ids:
            continue
        record = by_id[section_id]
        body = str(raw.get("body") or "").strip()
        if not body:
            continue
        evidence_ref = f"kb:section:{section_id}"
        evidence_record = EvidenceRecord(
            ref=evidence_ref,
            source=source,
            source_id=str(section_id),
            facts=[
                CanonicalEvidenceFact(
                    kind=source,
                    value=body,
                    subject_product_id=required_product_id,
                )
            ],
            fields={
                "section_id": section_id,
                "kind": str(raw.get("kind") or getattr(record, "kind", "") or ""),
                "title": str(raw.get("title") or getattr(record, "title", "") or ""),
                "body": body,
                "linked_product_ids": linked_ids,
            },
            provenance={
                "service": (
                    "modules.ai.brain.commerce.product_knowledge_or_comparison."
                    "retrieve_catalog_candidate_kb_sections"
                ),
                "record": "merchant_knowledge_sections",
                "visibility": "ai_visible",
            },
        )
        snapshots.append(
            KnowledgeSectionSnapshot(
                section_id=section_id,
                kind=str(raw.get("kind") or getattr(record, "kind", "") or ""),
                title=str(raw.get("title") or getattr(record, "title", "") or ""),
                body=body,
                linked_product_ids=linked_ids,
                evidence_ref=evidence_ref,
            )
        )
        evidence.append(evidence_record)
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
    payload = retrieve_catalog_candidate_kb_sections(
        context.db,
        context.tenant_id,
        subject=str(query or ""),
        message=str(query or ""),
        limit=max(1, min(int(limit), 6)),
    )
    return _build_result(
        context,
        payload,
        source="merchant_knowledge",
        required_product_id=None,
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
    payload = retrieve_catalog_candidate_kb_sections(
        context.db,
        context.tenant_id,
        subject=str(query or ""),
        message=str(query or ""),
        product_id=int(product_id),
        product_ids=[int(product_id)],
        limit=max(1, min(int(limit), 6)),
    )
    return _build_result(
        context,
        payload,
        source="product_knowledge",
        required_product_id=int(product_id),
    )
