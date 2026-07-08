"""Fact-bound persona compose for catalog search / browse answers (P0)."""
from __future__ import annotations

import re
from typing import Any, Optional

from .facts_bundle import (
    PERSONA_SURFACE_CATALOG_PRODUCT_ANSWER,
    PersonaComposeResult,
    PersonaConstraints,
    PersonaFactsBundle,
)
from .integration import (
    build_persona_compose_event_metadata,
    should_enforce_persona_compose_for_surface,
)

_PRICE_ASK_RE = re.compile(
    r"(?:بكم|كم\s*سعر|سعر|ثمن|تكلفة|how\s*much|price)",
    re.UNICODE | re.IGNORECASE,
)
_AVAILABILITY_ASK_RE = re.compile(
    r"(?:عندكم|عندك|لديكم|لديك|هل\s+.*متوفر|فيه|في\s+عندكم|available)",
    re.UNICODE | re.IGNORECASE,
)


_SOFT_BROWSE_RE = re.compile(
    r"(?:وش|ايش|ايه|ما)\s+عندكم",
    re.UNICODE | re.IGNORECASE,
)


def catalog_fact_product_rows(products: Any) -> list[dict[str, Any]]:
    """Normalize compose/catalog rows for grounding evidence (price-bearing)."""
    rows: list[dict[str, Any]] = []
    for item in list(products or []):
        if isinstance(item, dict):
            rows.append(dict(item))
            continue
        try:
            if hasattr(item, "items"):
                rows.append(dict(item))  # type: ignore[arg-type]
                continue
            attrs = getattr(item, "__dict__", None)
            if isinstance(attrs, dict) and attrs:
                rows.append({
                    k: v for k, v in attrs.items()
                    if not str(k).startswith("_")
                })
        except Exception:  # noqa: BLE001
            continue
    return rows


def resolve_catalog_visible_price(raw: dict[str, Any]) -> Any:
    """Resolve parseable catalog-visible price from formatted product fields only."""
    from modules.ai.brain.postprocess.product_claim_grounding_evidence import (  # noqa: PLC0415
        parse_price_amount,
    )

    for key in ("price", "sale_price", "regular_price"):
        value = raw.get(key)
        if value is None:
            continue
        if parse_price_amount(value) is not None:
            return value
    return None


def classify_catalog_question_kind(
    message: str,
    *,
    query: str = "",
    decision_args: Optional[dict[str, Any]] = None,
) -> str:
    """Classify P0 catalog turns: browse | availability | price."""
    msg = str(message or "").strip()
    args = dict(decision_args or {})
    if str(args.get("question_kind") or "").strip() in {
        "browse",
        "availability",
        "price",
    }:
        return str(args["question_kind"]).strip()

    if _SOFT_BROWSE_RE.search(msg) and not _PRICE_ASK_RE.search(msg):
        return "browse"

    if _PRICE_ASK_RE.search(msg):
        return "price"
    if _AVAILABILITY_ASK_RE.search(msg) and not _PRICE_ASK_RE.search(msg):
        return "availability"
    q = str(query or args.get("query") or "").strip()
    if q and _PRICE_ASK_RE.search(f"{msg} {q}"):
        return "price"
    return "browse"


def _catalog_rows_from_products(
    products: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[Any], list[Any], bool, bool]:
    catalog_product_ids: list[Any] = []
    variant_ids: list[Any] = []
    rows: list[dict[str, Any]] = []
    any_price = False
    any_availability = False
    any_positive_availability = False
    for raw in products or []:
        if not isinstance(raw, dict):
            continue
        pid = raw.get("id")
        if pid is not None:
            catalog_product_ids.append(pid)
        vid = raw.get("variant_id")
        if vid is not None:
            variant_ids.append(vid)
        row: dict[str, Any] = {
            "id": pid,
            "title": str(raw.get("title") or "").strip(),
        }
        category = str(raw.get("category") or "").strip()
        if category:
            row["category"] = category
        price = resolve_catalog_visible_price(raw)
        if price is not None:
            row["price"] = price
            any_price = True
        orderable = raw.get("can_checkout", raw.get("orderable"))
        if orderable is not None:
            row["orderable"] = bool(orderable)
            row["available"] = bool(orderable)
            any_availability = True
            if bool(orderable):
                any_positive_availability = True
        if row.get("title"):
            rows.append(row)
    return rows, catalog_product_ids, variant_ids, any_price, any_availability, any_positive_availability


def build_catalog_product_answer_facts_bundle(
    *,
    inbound_text: str,
    tenant_id: int = 0,
    customer_phone: str = "",
    products: Optional[list[dict[str, Any]]] = None,
    catalog_search_query: str = "",
    search_result_count: int = 0,
    category_scope: str = "",
    allowed_category: str = "",
    question_kind: str = "",
    category_filter_dropped: int = 0,
    display_count: int = 0,
    decision_args: Optional[dict[str, Any]] = None,
    merchant_persona: Optional[dict[str, Any]] = None,
) -> PersonaFactsBundle:
    from .fact_bound_composer import detect_language  # noqa: PLC0415

    inbound = str(inbound_text or "").strip()
    language = detect_language(inbound)
    args = dict(decision_args or {})
    items = catalog_fact_product_rows(products)
    rows, catalog_product_ids, variant_ids, any_price, any_availability, any_positive = (
        _catalog_rows_from_products(items)
    )
    qkind = str(question_kind or "").strip() or classify_catalog_question_kind(
        inbound,
        query=catalog_search_query,
        decision_args=args,
    )
    scope = str(category_scope or args.get("category_scope") or "").strip()
    allowed = str(allowed_category or scope or "").strip()
    include_price = qkind == "price" and any_price
    include_availability = qkind in {"availability", "price"} and any_availability

    verified_facts: dict[str, Any] = {
        "surface": PERSONA_SURFACE_CATALOG_PRODUCT_ANSWER,
        "inbound_text": inbound,
        "question_kind": qkind,
        "catalog_search_query": str(catalog_search_query or args.get("query") or "").strip(),
        "search_result_count": int(search_result_count or len(items)),
        "category_scope": scope,
        "allowed_category": allowed,
        "catalog_products": rows,
        "catalog_product_ids": catalog_product_ids,
        "variant_ids": variant_ids,
        "display_count": int(display_count or len(items)),
        "category_filter_dropped": int(category_filter_dropped or 0),
        "allow_price_mention": include_price and any_price,
        "allow_availability_mention": include_availability and any_availability,
        "has_positive_availability": any_positive,
        "allow_checkout_pressure": False,
        "allow_slot_prompts": False,
        "allow_superiority_claims": False,
        "has_catalog_products": bool(rows),
    }
    if include_price and any_price:
        verified_facts["price_source"] = "catalog"
    if include_availability and any_availability:
        verified_facts["availability_source"] = "catalog"
    return PersonaFactsBundle(
        surface=PERSONA_SURFACE_CATALOG_PRODUCT_ANSWER,
        inbound_text=inbound,
        language=language,
        dialect="saudi_arabic" if language == "ar" else None,
        verified_facts=verified_facts,
        customer_context={},
        merchant_persona=dict(merchant_persona or {}),
        constraints=PersonaConstraints(max_chars=420, max_emojis=2),
        tenant_id=int(tenant_id or 0),
        customer_phone=str(customer_phone or ""),
    )


def build_catalog_product_answer_event_metadata(
    result: PersonaComposeResult,
    *,
    tenant_id: int,
    allowlist_result: str,
    catalog_facts: dict[str, Any],
    catalog_fact_products: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Outbound metadata for catalog-grounded persona compose."""
    meta = build_persona_compose_event_metadata(
        result,
        tenant_id=int(tenant_id),
        allowlist_result=str(allowlist_result or ""),
    )
    facts = dict(catalog_facts or {})
    meta["question_kind"] = str(facts.get("question_kind") or "").strip()
    meta["catalog_search_query"] = str(facts.get("catalog_search_query") or "").strip()
    meta["search_result_count"] = int(facts.get("search_result_count") or 0)
    meta["category_scope"] = str(facts.get("category_scope") or "").strip()
    meta["allowed_category"] = str(facts.get("allowed_category") or "").strip()
    meta["checkout_pressure_allowed"] = False
    ids = list(facts.get("catalog_product_ids") or [])
    if ids:
        meta["catalog_product_ids"] = ids
    vids = list(facts.get("variant_ids") or [])
    if vids:
        meta["variant_ids"] = vids
    if facts.get("price_source"):
        meta["price_source"] = facts["price_source"]
    if facts.get("availability_source"):
        meta["availability_source"] = facts["availability_source"]
    qkind = str(facts.get("question_kind") or meta.get("question_kind") or "").strip()
    if qkind in _CATALOG_QA_KINDS:
        fact_rows = catalog_fact_product_rows(catalog_fact_products)
        if fact_rows:
            meta["catalog_fact_products"] = fact_rows
    return meta


_CATALOG_QA_KINDS = frozenset({"price", "availability"})
_PRESSURE_MARKERS = (
    "اختر رقم",
    "اسمك",
    "عنوانك",
    "طريقة الدفع",
    "نكمل الطلب",
    "كم الكمية",
)


def catalog_product_answer_deterministic_fallback(
    bundle: PersonaFactsBundle,
) -> str:
    """Fact-bound price/availability reply when LLM compose is unavailable."""
    facts = bundle.verified_facts or {}
    qkind = str(facts.get("question_kind") or "").strip()
    if qkind not in _CATALOG_QA_KINDS:
        return ""

    rows = [
        dict(row)
        for row in (facts.get("catalog_products") or [])
        if isinstance(row, dict) and str(row.get("title") or "").strip()
    ]
    if not rows:
        return ""

    lines: list[str] = []
    if qkind == "price":
        if not facts.get("allow_price_mention"):
            return ""
        for row in rows[:3]:
            title = str(row.get("title") or "").strip()
            price = row.get("price")
            if price is None:
                continue
            price_text = str(price).strip()
            if not price_text:
                continue
            line = f"{title} سعره {price_text}"
            if row.get("orderable") is False:
                line += "، والمنتج غير متاح للطلب حالياً"
            lines.append(line)
    else:
        if not facts.get("allow_availability_mention"):
            return ""
        for row in rows[:3]:
            title = str(row.get("title") or "").strip()
            if row.get("orderable") is True:
                lines.append(f"{title} متوفر للطلب حالياً")
            else:
                lines.append(f"{title} موجود في الكتالوج لكن غير متاح للطلب حالياً")

    if not lines:
        return ""
    if len(lines) == 1:
        text = lines[0]
    else:
        text = "من الكتالوج:\n" + "\n".join(f"• {line}" for line in lines)
    lower = text.lower()
    if any(marker in text or marker in lower for marker in _PRESSURE_MARKERS):
        return ""
    return text


def _catalog_deterministic_compose_result(
    *,
    bundle: PersonaFactsBundle,
    text: str,
    prior: PersonaComposeResult,
) -> PersonaComposeResult:
    return PersonaComposeResult(
        text=text.strip(),
        source="catalog_deterministic_fallback",
        surface=PERSONA_SURFACE_CATALOG_PRODUCT_ANSWER,
        facts_hash=prior.facts_hash,
        guard_passed=False,
        guard_failed_reason=prior.guard_failed_reason or "llm_or_guard_failed",
        fallback_reason="catalog_deterministic_fallback",
        language=prior.language,
        dialect=prior.dialect,
        emoji_count=sum(1 for ch in text if ch in {"😊", "🌷", "🤍", "🍯"}),
        latency_ms=prior.latency_ms,
        model=prior.model,
    )


async def try_compose_catalog_product_answer(
    *,
    tenant_id: int,
    customer_phone: str,
    inbound_text: str,
    products: list[dict[str, Any]],
    catalog_search_query: str = "",
    search_result_count: int = 0,
    category_scope: str = "",
    allowed_category: str = "",
    question_kind: str = "",
    category_filter_dropped: int = 0,
    display_count: int = 0,
    decision_args: Optional[dict[str, Any]] = None,
    ai_settings: Optional[dict[str, Any]] = None,
) -> tuple[Optional[str], Optional[PersonaComposeResult], Optional[dict[str, Any]]]:
    """Compose catalog-grounded search answer when test-mode gate passes."""
    from .fact_bound_composer import FactBoundPersonaComposer  # noqa: PLC0415

    settings = dict(ai_settings or {})
    if not should_enforce_persona_compose_for_surface(
        tenant_id=tenant_id,
        customer_phone=customer_phone,
        surface=PERSONA_SURFACE_CATALOG_PRODUCT_ANSWER,
        ai_settings=settings,
    ):
        return None, None, None

    if not products:
        return None, None, None

    compose_fact_rows = catalog_fact_product_rows(products)
    bundle = build_catalog_product_answer_facts_bundle(
        inbound_text=inbound_text,
        tenant_id=tenant_id,
        customer_phone=customer_phone,
        products=products,
        catalog_search_query=catalog_search_query,
        search_result_count=search_result_count,
        category_scope=category_scope,
        allowed_category=allowed_category,
        question_kind=question_kind,
        category_filter_dropped=category_filter_dropped,
        display_count=display_count,
        decision_args=dict(decision_args or {}),
        merchant_persona=settings,
    )
    if not bundle.verified_facts.get("has_catalog_products"):
        return None, None, None

    from .flags import persona_composer_allowlist_result  # noqa: PLC0415

    allowlist_result = persona_composer_allowlist_result(
        tenant_id=int(tenant_id),
        customer_phone=str(customer_phone or ""),
        ai_settings=settings,
    )
    composer = FactBoundPersonaComposer(enforce_gate=False)
    result = await composer.compose(bundle)
    if (
        result.source == "persona_llm"
        and result.guard_passed
        and (result.text or "").strip()
    ):
        event_meta = build_catalog_product_answer_event_metadata(
            result,
            tenant_id=int(tenant_id),
            allowlist_result=allowlist_result,
            catalog_facts=bundle.verified_facts,
            catalog_fact_products=compose_fact_rows,
        )
        return result.text.strip(), result, event_meta

    qkind = str(bundle.verified_facts.get("question_kind") or "").strip()
    if qkind in _CATALOG_QA_KINDS:
        fallback_text = catalog_product_answer_deterministic_fallback(bundle)
        if fallback_text.strip():
            fallback_result = _catalog_deterministic_compose_result(
                bundle=bundle,
                text=fallback_text,
                prior=result,
            )
            event_meta = build_catalog_product_answer_event_metadata(
                fallback_result,
                tenant_id=int(tenant_id),
                allowlist_result=allowlist_result,
                catalog_facts=bundle.verified_facts,
                catalog_fact_products=compose_fact_rows,
            )
            return fallback_text.strip(), fallback_result, event_meta

    return None, None, None
