"""Deterministic, tenant-scoped merchant knowledge retrieval for one turn.

Phase 2.7A shipped knowledge as two model-elective tools, so whether a product
or store-information turn ever consulted the merchant's knowledge base was the
model's choice.  This module makes the lookup part of the orchestration
instead: the turn-scope lookup runs on every turn that can still use catalog
evidence, and the product-scope lookup runs as soon as catalog tools discover
products.  Every attempt is recorded — including one that finds nothing, times
out or fails — so an absent fact can be told apart from an absent lookup.

Retrieval is mandatory; using what it returns is not.  Relevance filtering,
tenant scoping and AI visibility stay in the existing retrieval service, and a
failure here never blocks an answer grounded in structured Salla evidence.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from typing import Any

from modules.ai.brain.commerce.product_knowledge_or_comparison import (
    _norm as normalize_knowledge_text,
    retrieve_catalog_candidate_kb_sections,
)
from modules.ai.commerce_agent_v2.context import CommerceAgentContext
from modules.ai.commerce_agent_v2.output import (
    CanonicalEvidenceFact,
    EvidenceRecord,
    KnowledgeSectionSnapshot,
)

logger = logging.getLogger("nahla.commerce_v2.knowledge")

# Scopes are the two deterministic purposes, not intents.
SCOPE_TURN = "turn"
SCOPE_PRODUCT = "product"

# Bounded by design: the merchant knowledge base is never dumped into context.
KNOWLEDGE_RESULT_LIMIT = 4
KNOWLEDGE_TIMEOUT_SECONDS = 2.0

STATUS_OK = "ok"
STATUS_NO_RESULTS = "no_results"
STATUS_TIMEOUT = "timeout"
STATUS_ERROR = "error"
STATUS_SKIPPED_NO_QUERY = "skipped_no_query"


# Longest query the lookup will carry. Relevance is scored per token, so a
# whole message of small talk would dilute a genuine match; the cap also keeps
# the retrieval bounded for very long customer messages.
MAX_QUERY_TOKENS = 12
MIN_QUERY_TOKEN_LENGTH = 3
_TOKEN_SPLIT_RE = re.compile(r"[^\w]+", re.UNICODE)


def normalize_lookup_query(text: str) -> str:
    """Fold a customer message into the tokens the retrieval scorer compares.

    This is tokenization, not routing: it applies the retrieval service's own
    text folding, drops punctuation and one- or two-letter fragments, and keeps
    the first tokens in the order the customer wrote them.  No token carries a
    meaning of its own here and nothing is matched against a curated list.
    """
    folded = normalize_knowledge_text(str(text or ""))
    tokens: list[str] = []
    for raw in _TOKEN_SPLIT_RE.sub(" ", folded).split():
        # The scorer matches substrings, so dropping the Arabic definite
        # article keeps "التغليف" and "تغليف" the same token either way.
        token = raw[2:] if raw.startswith("ال") and len(raw) >= 5 else raw
        if len(token) < MIN_QUERY_TOKEN_LENGTH:
            continue
        if token in tokens:
            continue
        tokens.append(token)
        if len(tokens) >= MAX_QUERY_TOKENS:
            break
    return " ".join(tokens)


def query_fingerprint(query: str) -> str:
    """Stable, non-reversible query identity for evidence without storing text."""
    return hashlib.sha256(str(query or "").strip().encode("utf-8")).hexdigest()[:16]


def build_product_anchor(
    *,
    product_titles: list[str] | None = None,
    product_aliases: list[str] | None = None,
) -> str:
    """The product subject a product-scoped KB lookup is anchored on.

    A customer who says only «أبغى تفاصيل أول منتج عندكم» or «طيب وش مصدره؟»
    names no product, so their words alone share no vocabulary with a section
    titled «مصدر الجاكيت».  The retriever already scores the product subject
    and the customer's question as two independent dimensions and keeps the
    better one, so the resolved product's title and alias are passed as that
    subject rather than mixed into the question — mixing them would dilute both
    scores instead of lifting either, and the relevance floor stays untouched.

    The anchor is only ever built from catalog evidence the turn already
    resolved and authorized, never from a guess.

    Aliases are accepted but callers pass them only when they are words a
    merchant would actually write.  A SKU such as ``T-JACKET`` never appears in
    merchant prose, so adding it does not match anything and only lowers the
    ratio of matched tokens — it would push a section that should be found back
    under the floor.
    """
    parts: list[str] = []
    for value in list(product_titles or []) + list(product_aliases or []):
        text = str(value or "").strip()
        if text and text not in parts:
            parts.append(text)
    return normalize_lookup_query(" ".join(parts))


def lookup_signature(
    *, scope: str, query: str, product_ids: list[int], subject: str = ""
) -> str:
    parts = [
        str(scope),
        query_fingerprint(query),
        query_fingerprint(subject),
        ",".join(str(pid) for pid in sorted(product_ids)),
    ]
    return "|".join(parts)


def _section_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    seen: set[int] = set()
    rows: list[dict[str, Any]] = []
    for raw in payload.get("kb_sections") or []:
        try:
            section_id = int(raw.get("section_id"))
        except (TypeError, ValueError):
            continue
        if section_id in seen:
            continue
        seen.add(section_id)
        rows.append(raw)
    return rows[:KNOWLEDGE_RESULT_LIMIT]


def _retrieve(
    context: CommerceAgentContext,
    *,
    query: str,
    product_ids: list[int],
    limit: int,
    subject: str = "",
) -> dict[str, Any]:
    # The retriever scores the product subject and the customer question as two
    # independent dimensions and keeps the better one, so they are passed
    # separately: the resolved product anchors the subject, the customer's own
    # words stay the question.
    return retrieve_catalog_candidate_kb_sections(
        context.db,
        context.tenant_id,
        subject=str(subject or query or ""),
        message=str(query or ""),
        product_id=product_ids[0] if len(product_ids) == 1 else None,
        product_ids=list(product_ids) or None,
        limit=limit,
    )


def _record(
    context: CommerceAgentContext,
    *,
    scope: str,
    purpose: str,
    query: str,
    product_ids: list[int],
    status: str,
    sections: list[dict[str, Any]],
    duration_ms: int,
    failure_reason: str | None = None,
    subject: str = "",
) -> dict[str, Any]:
    """Append one attempt to the run ledger; bodies never enter the record."""
    record = {
        "scope": scope,
        "purpose": purpose,
        "tenant_id": int(context.tenant_id),
        "conversation_id": int(context.conversation_id),
        "query_fingerprint": query_fingerprint(query),
        "query_length": len(str(query or "").strip()),
        "product_ids": sorted(int(pid) for pid in product_ids),
        "limit": KNOWLEDGE_RESULT_LIMIT,
        "status": status,
        "attempted": True,
        "hit_count": len(sections),
        "section_ids": [int(row["section_id"]) for row in sections],
        "evidence_refs": [f"kb:section:{int(row['section_id'])}" for row in sections],
        "duration_ms": int(duration_ms),
        "failure_reason": failure_reason,
    }
    signature = lookup_signature(
        scope=scope, query=query, product_ids=product_ids, subject=subject
    )
    context.cache_knowledge_rows(signature, sections)
    return context.record_knowledge_lookup(record, signature=signature)


def run_knowledge_lookup(
    context: CommerceAgentContext,
    *,
    scope: str,
    purpose: str,
    query: str,
    product_ids: list[int] | None = None,
    limit: int = KNOWLEDGE_RESULT_LIMIT,
    subject: str = "",
) -> dict[str, Any]:
    """Run one bounded tenant-scoped lookup and record the attempt.

    Never raises: a retrieval failure is recorded as an outcome so the caller
    can answer from structured evidence, and so a later "not documented" reply
    can prove a lookup actually ran.
    """
    ids = sorted({int(pid) for pid in (product_ids or []) if int(pid) > 0})
    query = normalize_lookup_query(query)
    subject = normalize_lookup_query(subject)
    signature = lookup_signature(scope=scope, query=query, product_ids=ids, subject=subject)
    if context.knowledge_lookup_seen(signature):
        for existing in reversed(context.knowledge_lookups):
            if existing.get("scope") == scope and existing.get("product_ids") == ids:
                return existing
    if not str(query or "").strip():
        return _record(
            context, scope=scope, purpose=purpose, query=query, product_ids=ids, subject=subject,
            status=STATUS_SKIPPED_NO_QUERY, sections=[], duration_ms=0,
        )
    started = time.monotonic()
    try:
        payload = _retrieve(
            context, query=query, product_ids=ids, limit=limit, subject=subject
        )
    except Exception as exc:  # noqa: BLE001 — recorded as an outcome, never fatal
        logger.debug(
            "[COMMERCE_V2_KB] lookup failed tenant=%s scope=%s error_class=%s",
            context.tenant_id, scope, type(exc).__name__,
        )
        return _record(
            context, scope=scope, purpose=purpose, query=query, product_ids=ids, subject=subject,
            status=STATUS_ERROR, sections=[],
            duration_ms=int((time.monotonic() - started) * 1000),
            failure_reason=f"knowledge_retrieval_exception:{type(exc).__name__}",
        )
    duration_ms = int((time.monotonic() - started) * 1000)
    if payload.get("kb_retrieval_failed"):
        return _record(
            context, scope=scope, purpose=purpose, query=query, product_ids=ids, subject=subject,
            status=STATUS_ERROR, sections=[], duration_ms=duration_ms,
            failure_reason="knowledge_retrieval_failed",
        )
    rows = _section_rows(payload)
    return _record(
        context, scope=scope, purpose=purpose, query=query, product_ids=ids, subject=subject,
        status=STATUS_OK if rows else STATUS_NO_RESULTS,
        sections=rows, duration_ms=duration_ms,
    )


async def run_knowledge_lookup_async(
    context: CommerceAgentContext,
    *,
    scope: str,
    purpose: str,
    query: str,
    product_ids: list[int] | None = None,
    timeout_seconds: float = KNOWLEDGE_TIMEOUT_SECONDS,
    subject: str = "",
) -> dict[str, Any]:
    """Bounded async wrapper: a slow knowledge base never holds up a Salla answer."""
    ids = sorted({int(pid) for pid in (product_ids or []) if int(pid) > 0})
    started = time.monotonic()
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(
                run_knowledge_lookup,
                context,
                scope=scope,
                purpose=purpose,
                query=query,
                product_ids=ids,
                subject=subject,
            ),
            timeout=float(timeout_seconds),
        )
    except (asyncio.TimeoutError, TimeoutError):
        logger.debug(
            "[COMMERCE_V2_KB] lookup timed out tenant=%s scope=%s", context.tenant_id, scope
        )
        return _record(
            context, scope=scope, purpose=purpose, query=query, product_ids=ids, subject=subject,
            status=STATUS_TIMEOUT, sections=[],
            duration_ms=int((time.monotonic() - started) * 1000),
            failure_reason="knowledge_retrieval_timeout",
        )


def linked_products(context: CommerceAgentContext, section_ids: list[int]) -> dict[int, list[int]]:
    """Tenant-scoped section→product links for the sections just retrieved."""
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


def build_knowledge_snapshots(
    context: CommerceAgentContext,
    rows: list[dict[str, Any]],
    *,
    source: str,
    required_product_id: int | None,
    allowed_product_ids: list[int] | None = None,
    deduplicate: bool = True,
) -> tuple[list[KnowledgeSectionSnapshot], list[EvidenceRecord]]:
    """Re-read every retrieved section under tenant scope and type it as evidence.

    The retrieval service is trusted to filter, but the rows are still re-read
    and asserted against the tenant context here: a section that cannot be
    re-read in scope is a safety failure, never a silent drop.

    A turn may look a section up more than once — the deterministic catalog
    lookup finds it, then the model calls the knowledge tool and finds it
    again.  Every one of those attempts stays in the ledger, but the section
    itself reaches the model exactly once: ``deduplicate`` drops a repeat by
    tenant-scoped section identity, so the same text is never presented, cited
    or counted twice.  Identity is the id, so two distinct sections that happen
    to share wording are both kept.
    """
    from models import MerchantKnowledgeSection
    from modules.ai.security.tenant_isolation import TenantIsolationLayer

    section_ids = []
    for row in rows:
        try:
            section_ids.append(int(row["section_id"]))
        except (KeyError, TypeError, ValueError):
            continue
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
    links = linked_products(context, section_ids)
    permitted = (
        {int(pid) for pid in allowed_product_ids}
        if allowed_product_ids is not None
        else ({int(required_product_id)} if required_product_id is not None else set())
    )

    snapshots: list[KnowledgeSectionSnapshot] = []
    evidence: list[EvidenceRecord] = []
    for raw in rows:
        try:
            section_id = int(raw["section_id"])
        except (KeyError, TypeError, ValueError):
            continue
        linked_ids = sorted(set(links.get(section_id, [])))
        if not permitted and linked_ids:
            # A product-linked section needs a product in scope for this turn.
            continue
        if permitted and linked_ids and permitted.isdisjoint(linked_ids):
            continue
        record = by_id[section_id]
        body = str(raw.get("body") or "").strip()
        if not body:
            continue
        if deduplicate and context.knowledge_section_emitted(section_id):
            continue
        evidence_ref = f"kb:section:{section_id}"
        subject_product_id = None
        if linked_ids:
            in_scope = sorted(permitted.intersection(linked_ids))
            subject_product_id = in_scope[0] if in_scope else None
        evidence.append(
            EvidenceRecord(
                ref=evidence_ref,
                source=source,
                source_id=str(section_id),
                facts=[
                    CanonicalEvidenceFact(
                        kind=source,
                        value=body,
                        subject_product_id=subject_product_id,
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
        if deduplicate:
            context.mark_knowledge_section_emitted(section_id)
    return snapshots, evidence


def retrieved_sections(
    context: CommerceAgentContext,
    *,
    scope: str,
    query: str,
    product_ids: list[int] | None = None,
    subject: str = "",
) -> list[dict[str, Any]]:
    """The raw sections one recorded lookup returned, without re-querying."""
    ids = sorted({int(pid) for pid in (product_ids or []) if int(pid) > 0})
    return context.cached_knowledge_rows(
        lookup_signature(
            scope=scope,
            query=normalize_lookup_query(query),
            product_ids=ids,
            subject=normalize_lookup_query(subject),
        )
    )


def knowledge_evidence_refs(context: CommerceAgentContext) -> list[str]:
    """Knowledge evidence registered for this turn, in stable order."""
    return sorted(
        ref for ref, record in context.evidence.items()
        if record.source in {"merchant_knowledge", "product_knowledge"}
    )


def detect_catalog_conflicts(context: CommerceAgentContext) -> list[dict[str, Any]]:
    """Flag merchant knowledge that states a price the live catalog contradicts.

    Only digits are compared, so this stays language-neutral and never becomes a
    phrase list.  Structured Salla evidence always wins: the conflict is recorded
    for the operator, and the guardrail independently refuses to let a knowledge
    section support a commercial claim in the reply.
    """
    conflicts: list[dict[str, Any]] = []
    catalog_prices: dict[int, Any] = {}
    knowledge: list[EvidenceRecord] = []
    for record in context.evidence.values():
        if record.source == "catalog_product":
            for fact in record.facts:
                if fact.kind in {"price", "sale_price"} and fact.subject_product_id:
                    catalog_prices.setdefault(int(fact.subject_product_id), fact.value)
        elif record.source in {"merchant_knowledge", "product_knowledge"}:
            knowledge.append(record)
    if not catalog_prices or not knowledge:
        return conflicts
    for record in knowledge:
        numbers = _price_like_numbers(str(record.fields.get("body") or ""))
        if not numbers:
            continue
        for product_id in record.fields.get("linked_product_ids") or []:
            catalog_value = catalog_prices.get(int(product_id))
            if catalog_value is None:
                continue
            try:
                catalog_number = float(catalog_value)
            except (TypeError, ValueError):
                continue
            if catalog_number in numbers:
                continue
            conflicts.append(
                {
                    "kind": "price",
                    "section_id": int(record.fields.get("section_id") or 0),
                    "evidence_ref": record.ref,
                    "product_id": int(product_id),
                    "knowledge_values": sorted(numbers),
                    "catalog_value": catalog_number,
                    "resolution": "structured_catalog_wins",
                }
            )
    return conflicts


def _price_like_numbers(body: str) -> set[float]:
    numbers: set[float] = set()
    digits = ""
    for char in str(body or ""):
        if char.isdigit() or (char == "." and digits):
            digits += char
        else:
            if digits:
                try:
                    numbers.add(float(digits.rstrip(".")))
                except ValueError:
                    pass
                digits = ""
    if digits:
        try:
            numbers.add(float(digits.rstrip(".")))
        except ValueError:
            pass
    return numbers


__all__ = [
    "KNOWLEDGE_RESULT_LIMIT",
    "KNOWLEDGE_TIMEOUT_SECONDS",
    "SCOPE_PRODUCT",
    "SCOPE_TURN",
    "STATUS_ERROR",
    "STATUS_NO_RESULTS",
    "STATUS_OK",
    "STATUS_SKIPPED_NO_QUERY",
    "STATUS_TIMEOUT",
    "build_knowledge_snapshots",
    "build_product_anchor",
    "detect_catalog_conflicts",
    "knowledge_evidence_refs",
    "lookup_signature",
    "normalize_lookup_query",
    "query_fingerprint",
    "retrieved_sections",
    "run_knowledge_lookup",
    "run_knowledge_lookup_async",
]
