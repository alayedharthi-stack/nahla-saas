"""Tenant-bound wrappers around the existing catalog domain service."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from agents import RunContextWrapper

from core.store_knowledge import CatalogContextBuilder
from modules.ai.commerce_agent_v2.context import CommerceAgentContext
from modules.ai.commerce_agent_v2.knowledge_retrieval import (
    SCOPE_PRODUCT,
    build_knowledge_snapshots,
    build_product_anchor,
    retrieved_sections,
    run_knowledge_lookup,
)
from modules.ai.commerce_agent_v2.tool_runtime import commerce_read_tool
from modules.ai.commerce_agent_v2.output import (
    CatalogSearchResult,
    CanonicalEvidenceFact,
    EvidenceRecord,
    KnowledgeSectionSnapshot,
    ProductDetailsResult,
    ProductSnapshot,
)
from modules.ai.security.tenant_isolation import TenantIsolationLayer


_GENERAL_BROWSE_EVIDENCE_LIMIT = 5


_MAX_CONSECUTIVE_CATALOG_MISSES = 2

_WEAK_PRODUCT_REFERENCE_RE = re.compile(
    r"(?:^|\s)(?:هذا|هذه|هذي|ذا|هو|هي|this|that|it)(?=\s|$|[؟?،,.])",
    re.IGNORECASE,
)
_EXPLICIT_PRODUCT_ORDINAL_RE = re.compile(
    r"(?:^|\s)(?:الاول|الأول|الاولى|الأولى|الثاني|الثانية|الثانيه|"
    r"الثالث|الثالثة|الثالثه|first|second|third|1st|2nd|3rd)(?=\s|$|[؟?،,.])",
    re.IGNORECASE,
)
_WEAK_QUERY_VALUES = frozenset(
    {"هذا", "هذه", "هذي", "ذا", "هو", "هي", "this", "that", "it"}
)

# A decimal number followed directly by another decimal ("11.53820.5125...")
# can only come from numeric cells whose separators were lost upstream.
_FUSED_DECIMAL_RUN_RE = re.compile(r"\d+[.,]\d+[.,]\d+")
# Arabic letters fused to digits with no whitespace ("المقاس36نصف") show the
# same loss between words and numbers; together the two rules single out
# flattened merchant tables and leave dotted identifiers in normal prose alone.
_ARABIC_DIGIT_ADJACENCY_RE = re.compile(r"[؀-ۿ]\d|\d[؀-ۿ]")
DESCRIPTION_WITHHELD_FUSED_NUMERIC_TEXT = "fused_numeric_text"


def citable_description(raw: Any) -> tuple[str, str | None]:
    """Return the description the model may cite, or "" plus a withhold reason.

    Salla can return a merchant's size chart flattened with no separators at
    all (Phase 2.7A run 1, product 23:
    ``المقاس36نصف محيط الخصر19.5الطول125الكم11.53820.512511.5...``). The
    numeric boundaries in such text are irrecoverable, so no faithful
    rendering can satisfy the grounding guardrail's number check; the model's
    only outcomes are a rejected reply and a safe fallback. Withholding the
    text from evidence lets the turn stay grounded on the product facts that
    are verifiable (name, price, availability, media). Nothing is added to
    evidence here; the check only removes unverifiable text.
    """
    text = str(raw or "")
    if _FUSED_DECIMAL_RUN_RE.search(text) and _ARABIC_DIGIT_ADJACENCY_RE.search(text):
        return "", DESCRIPTION_WITHHELD_FUSED_NUMERIC_TEXT
    return text, None


def _normalise_reference_text(value: Any) -> str:
    text = str(value or "").strip().casefold()
    return " ".join(
        text.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا").split()
    )


def _persisted_product_ids(metadata: dict[str, Any]) -> set[int]:
    """Read product identities already persisted by trusted response contracts."""
    product_ids: set[int] = set()

    artifact = metadata.get("artifact")
    if isinstance(artifact, dict):
        structured = artifact.get("structured_reply")
        if isinstance(structured, dict):
            for item in structured.get("product_refs") or []:
                if not isinstance(item, dict):
                    continue
                try:
                    product_ids.add(int(item.get("product_id")))
                except (TypeError, ValueError):
                    continue
        presentation = artifact.get("presentation_bundle")
        if isinstance(presentation, dict):
            for item in presentation.get("actions") or []:
                if not isinstance(item, dict) or item.get("kind") != "product":
                    continue
                payload = item.get("payload")
                try:
                    product_ids.add(int(payload.get("id")))
                except (AttributeError, TypeError, ValueError):
                    continue

    bundle = metadata.get("response_bundle")
    if isinstance(bundle, dict):
        for item in bundle.get("presentations") or []:
            product = item.get("product") if isinstance(item, dict) else None
            try:
                product_ids.add(int(product.get("id")))
            except (AttributeError, TypeError, ValueError):
                continue
    return {product_id for product_id in product_ids if product_id > 0}


def _ambiguous_reference_has_multiple_candidates(
    context: CommerceAgentContext,
    *,
    query: str,
) -> bool:
    """Fail closed when a weak reference follows a structured multi-product reply."""
    user_input = _normalise_reference_text(context.run_user_input)
    if not _WEAK_PRODUCT_REFERENCE_RE.search(user_input):
        return False
    if _EXPLICIT_PRODUCT_ORDINAL_RE.search(user_input):
        return False

    normalised_query = _normalise_reference_text(query)
    if (
        normalised_query
        and normalised_query not in _WEAK_QUERY_VALUES
        and normalised_query in user_input
    ):
        return False

    from models import MessageEvent  # noqa: PLC0415

    previous_outbound = (
        context.db.query(MessageEvent)
        .filter(
            MessageEvent.tenant_id == context.tenant_id,
            MessageEvent.conversation_id == context.conversation_id,
            MessageEvent.direction.in_(("out", "outbound", "internal_e2e_outbound")),
        )
        .order_by(MessageEvent.created_at.desc(), MessageEvent.id.desc())
        .first()
    )
    if previous_outbound is None:
        return False
    return len(_persisted_product_ids(dict(previous_outbound.extra_metadata or {}))) > 1


def _catalog_search_enabled(
    run_context: RunContextWrapper[CommerceAgentContext],
    _agent: Any,
) -> bool:
    """Keep read tools available until catalog exploration is conclusively empty.

    Two consecutive misses represent the initial lookup plus one useful
    reformulation.  The predicate is shared by every Phase-1 tool so the next
    model turn must conclude from those results instead of moving the same
    unsuccessful lookup to an unrelated read tool.
    """
    return (
        run_context.context.consecutive_catalog_misses
        < _MAX_CONSECUTIVE_CATALOG_MISSES
    )


def _attach_product_knowledge(
    context: CommerceAgentContext,
    products: list[Any],
) -> list[KnowledgeSectionSnapshot]:
    """Retrieve merchant knowledge for the products this tool just grounded.

    Deterministic by construction: the lookup follows the catalog evidence
    rather than an intent guess, runs once per (question, product set), and is
    recorded even when it returns nothing.  A retrieval failure returns no
    sections and never disturbs the catalog answer the customer asked for.

    The query carries the resolved product's own title and alias alongside the
    customer's words, because the customer often names no product at all
    («أبغى تفاصيل أول منتج عندكم»).  The anchor is the catalog evidence this
    tool just authorized, so a pronoun resolves only to a product the turn has
    actually grounded.
    """
    ids: list[int] = []
    for item in products:
        try:
            product_id = int(getattr(item, "product_id", 0) or 0)
        except (TypeError, ValueError):
            continue
        if product_id > 0 and product_id not in ids:
            ids.append(product_id)
    ids = sorted(set(ids))
    if not ids:
        return []
    titles, _aliases = context.authorized_product_anchors(ids)
    subject = build_product_anchor(product_titles=titles)
    query = context.run_user_input
    run_knowledge_lookup(
        context,
        scope=SCOPE_PRODUCT,
        purpose="catalog_product_knowledge",
        query=query,
        product_ids=ids,
        subject=subject,
    )
    rows = retrieved_sections(
        context, scope=SCOPE_PRODUCT, query=query, product_ids=ids, subject=subject
    )
    if not rows:
        return []
    snapshots, evidence = build_knowledge_snapshots(
        context,
        rows,
        source="product_knowledge",
        required_product_id=None,
        allowed_product_ids=ids,
    )
    context.register_evidence(evidence)
    return snapshots


def _canonical_money(value: Any) -> int | float | None:
    if value in (None, ""):
        return None
    raw = str(value).strip()
    if "," in raw and "." not in raw:
        whole, fraction = raw.rsplit(",", 1)
        raw = f"{whole}.{fraction}" if len(fraction) <= 2 else raw.replace(",", "")
    else:
        raw = raw.replace(",", "")
    try:
        amount = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite():
        return None
    if amount == amount.to_integral_value():
        return int(amount)
    return float(amount)


def _product_evidence(row: dict[str, Any]) -> tuple[ProductSnapshot, EvidenceRecord]:
    product_id = int(row["id"])
    evidence_ref = f"catalog:product:{product_id}"
    description, description_withheld = citable_description(row.get("description"))
    fields = {
        "product_id": product_id,
        "external_id": row.get("external_id"),
        "title": str(row.get("title") or ""),
        "description": description,
        "price": row.get("price"),
        "sale_price": row.get("sale_price"),
        "regular_price": row.get("regular_price"),
        "currency": str(row.get("currency") or "").strip().upper() or None,
        "in_stock": row.get("in_stock"),
        "stock_quantity": row.get("stock_qty"),
        "image_url": str(row.get("image_url") or ""),
        "product_url": str(row.get("product_url") or ""),
        "orderable": bool(row.get("orderable")),
    }
    facts: list[CanonicalEvidenceFact] = []

    def add_fact(kind: str, value: Any) -> None:
        if value in (None, ""):
            return
        facts.append(
            CanonicalEvidenceFact(
                kind=kind,
                value=value,
                subject_product_id=product_id,
            )
        )

    add_fact("product_name", fields["title"])
    add_fact("description", fields["description"])
    add_fact("price", _canonical_money(fields["price"]))
    add_fact("sale_price", _canonical_money(fields["sale_price"]))
    add_fact("regular_price", _canonical_money(fields["regular_price"]))
    add_fact("currency", fields["currency"])
    add_fact("availability", fields["in_stock"])
    add_fact("stock_quantity", fields["stock_quantity"])
    add_fact("image_url", fields["image_url"])
    add_fact("product_url", fields["product_url"])
    provenance = {
        "service": "core.store_knowledge.CatalogContextBuilder",
        "record": "products",
        "freshness": "synced_catalog",
    }
    if description_withheld is not None:
        provenance["description_withheld"] = description_withheld
    evidence = EvidenceRecord(
        ref=evidence_ref,
        source="catalog_product",
        source_id=str(product_id),
        facts=facts,
        fields=fields,
        provenance=provenance,
    )
    snapshot = ProductSnapshot(
        product_id=product_id,
        external_id=(str(row.get("external_id")) if row.get("external_id") else None),
        title=fields["title"],
        description=fields["description"],
        price=(str(row.get("price")) if row.get("price") not in (None, "") else None),
        sale_price=(
            str(row.get("sale_price")) if row.get("sale_price") not in (None, "") else None
        ),
        regular_price=(
            str(row.get("regular_price"))
            if row.get("regular_price") not in (None, "")
            else None
        ),
        currency=fields["currency"],
        in_stock=row.get("in_stock"),
        stock_quantity=row.get("stock_qty"),
        image_url=fields["image_url"],
        product_url=fields["product_url"],
        orderable=fields["orderable"],
        evidence_ref=evidence_ref,
    )
    return snapshot, evidence


def _assert_catalog_rows_belong_to_tenant(
    context: CommerceAgentContext,
    rows: list[dict[str, Any]],
) -> None:
    from models import Product

    ids = [int(row["id"]) for row in rows]
    if not ids:
        return
    db_rows = (
        context.db.query(Product)
        .filter(Product.id.in_(ids), Product.tenant_id == context.tenant_id)
        .all()
    )
    found = {int(row.id) for row in db_rows}
    if found != set(ids):
        raise RuntimeError("catalog_service_returned_out_of_scope_product")
    for row in db_rows:
        TenantIsolationLayer.assert_belongs(row, context.tenant_context)


async def search_products_impl(
    context: CommerceAgentContext,
    query: str,
    limit: int,
) -> CatalogSearchResult:
    """SDK-free implementation of the ``search_products`` read tool.

    The model-visible name, signature and description stay on the
    decorated wrapper below; this body is unchanged and is the single
    implementation shared by the Agents-SDK tool and by the commerce
    runtime's own loop, which passes the trusted context directly.
    """
    context.assert_scope()
    bounded_limit = max(1, min(int(limit), 10))
    catalog = CatalogContextBuilder(context.db, context.tenant_id)
    clean_query = str(query or "").strip()
    if clean_query and _ambiguous_reference_has_multiple_candidates(
        context,
        query=clean_query,
    ):
        context.record_catalog_search_outcome(found=False)
        return CatalogSearchResult(
            status="not_found",
            failure_reason="ambiguous_product_reference_requires_clarification",
        )
    if clean_query:
        domain_result = catalog.search_products(
            clean_query,
            limit=bounded_limit,
            include_non_orderable_facts=True,
        )
        rows = [
            *list(domain_result.products or []),
            *list(domain_result.catalog_fact_products or []),
        ]
    else:
        # General browsing otherwise sends ten full product/evidence records
        # into the compose turn. Live Phase 2.7A evidence showed that payload
        # repeatedly drove the provider attempt past the 75s runtime budget.
        # Five grounded choices preserve useful discovery while bounding the
        # model context; specific searches keep their existing 1..10 contract.
        bounded_limit = min(bounded_limit, _GENERAL_BROWSE_EVIDENCE_LIMIT)
        rows = list(catalog.get_top_products(limit=bounded_limit) or [])
    _assert_catalog_rows_belong_to_tenant(context, rows)
    snapshots: list[ProductSnapshot] = []
    evidence: list[EvidenceRecord] = []
    for row in rows:
        snapshot, record = _product_evidence(row)
        snapshots.append(snapshot)
        evidence.append(record)
    context.authorize_products(
        [item.product_id for item in snapshots],
        titles={
            int(item.product_id): str(getattr(item, "title", "") or "")
            for item in snapshots
        },
        aliases={
            int(item.product_id): str(getattr(item, "external_id", "") or "")
            for item in snapshots
        },
    )
    context.register_evidence(evidence)
    if not snapshots:
        misses = context.record_catalog_search_outcome(found=False)
        return CatalogSearchResult(
            status="not_found",
            failure_reason=(
                "no_catalog_product_matched_after_reformulation"
                if misses >= _MAX_CONSECUTIVE_CATALOG_MISSES
                else "no_catalog_product_matched"
            ),
        )
    context.record_catalog_search_outcome(found=True)
    knowledge_sections = _attach_product_knowledge(context, snapshots)
    return CatalogSearchResult(
        status="ok",
        products=snapshots,
        evidence=evidence,
        knowledge_sections=knowledge_sections,
    )


async def get_product_details_impl(
    context: CommerceAgentContext,
    product_id: int,
) -> ProductDetailsResult:
    """SDK-free implementation of the ``get_product_details`` read tool.

    The model-visible name, signature and description stay on the
    decorated wrapper below; this body is unchanged and is the single
    implementation shared by the Agents-SDK tool and by the commerce
    runtime's own loop, which passes the trusted context directly.
    """
    context.assert_scope()
    context.require_authorized_product(product_id)
    row = CatalogContextBuilder(context.db, context.tenant_id).get_by_id(int(product_id))
    if row is None:
        return ProductDetailsResult(
            status="not_found",
            failure_reason="product_not_found_in_tenant_catalog",
        )
    _assert_catalog_rows_belong_to_tenant(context, [row])
    snapshot, evidence = _product_evidence(row)
    context.register_evidence([evidence])
    knowledge_sections = _attach_product_knowledge(context, [snapshot])
    return ProductDetailsResult(
        status="ok",
        product=snapshot,
        evidence=[evidence],
        knowledge_sections=knowledge_sections,
    )


@commerce_read_tool("search_products", is_enabled=_catalog_search_enabled)
async def search_products(
    run_context: RunContextWrapper[CommerceAgentContext],
    query: str,
    limit: int,
) -> CatalogSearchResult:
    """Search the current merchant's synced catalog.

    Use an empty query to browse the merchant's top available products. Limit
    must be between 1 and 10. Tenant identity is taken only from trusted context.
    """
    return await search_products_impl(run_context.context, query=query, limit=limit)


@commerce_read_tool("get_product_details", is_enabled=_catalog_search_enabled)
async def get_product_details(
    run_context: RunContextWrapper[CommerceAgentContext],
    product_id: int,
) -> ProductDetailsResult:
    """Get exact details for a product returned earlier by search_products."""
    return await get_product_details_impl(run_context.context, product_id=product_id)
