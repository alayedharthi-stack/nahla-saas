"""Fact-bound persona compose for catalog search / browse answers (P0)."""
from __future__ import annotations

import logging
import re
from dataclasses import replace
from typing import Any, Optional

from .facts_bundle import (
    PERSONA_SURFACE_CATALOG_PRODUCT_ANSWER,
    PersonaComposeResult,
    PersonaConstraints,
    PersonaFactsBundle,
)
from .integration import (
    build_persona_compose_event_metadata,
)

logger = logging.getLogger("nahla.brain.persona.catalog_product_answer")

CATALOG_GROUNDED_PERSONA_CHOSEN_PATHS = frozenset({
    "catalog_miss_resolved_subject",
    "catalog_navigation_top_products_fallback",
})

_ROUTE_METADATA_KEYS = (
    "route_provider",
    "route_model",
    "route_tier",
    "route_source",
    "route_provider_configured",
    "compose_attempt",
)


def _attempted_provider_call_route_metadata(
    attempted: Optional[PersonaComposeResult],
) -> dict[str, Any]:
    """Bounded route fields from an attempted provider call (never inferred afterward)."""
    if attempted is None:
        return {}
    from .fact_bound_composer import COMPOSE_ATTEMPT_PROVIDER_CALL  # noqa: PLC0415

    meta = dict(attempted.metadata or {})
    if str(meta.get("compose_attempt") or "").strip() != COMPOSE_ATTEMPT_PROVIDER_CALL:
        return {}
    preserved: dict[str, Any] = {}
    for key in _ROUTE_METADATA_KEYS:
        if key in meta:
            preserved[key] = meta[key]
    if preserved:
        preserved["llm_candidate_present"] = False
    return preserved


def _with_attempted_route_metadata(
    fallback: PersonaComposeResult,
    attempted: Optional[PersonaComposeResult],
) -> PersonaComposeResult:
    preserved = _attempted_provider_call_route_metadata(attempted)
    if not preserved:
        return fallback
    merged = {**dict(fallback.metadata or {}), **preserved}
    model = attempted.model if attempted and attempted.model else fallback.model
    return replace(fallback, metadata=merged, model=model)

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


def _count_eligible_catalog_products(products: list[dict[str, Any]]) -> int:
    count = 0
    for raw in products or []:
        if not isinstance(raw, dict):
            continue
        if bool(raw.get("can_checkout", raw.get("orderable", False))):
            count += 1
    return count


def _log_catalog_compose_telemetry(
    *,
    surface: str,
    outcome_category: str,
    eligible_product_count: int = 0,
    search_result_count: int = 0,
    question_kind: str = "",
) -> None:
    logger.info(
        "[CATALOG_COMPOSE] surface=%s outcome=%s eligible=%s search=%s qkind=%s",
        str(surface or "").strip(),
        str(outcome_category or "").strip(),
        int(eligible_product_count),
        int(search_result_count),
        str(question_kind or "").strip() or "-",
    )


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
    # Price turns ground on catalog price only; availability prose is reserved for
    # explicit availability questions so guards are not fighting prompt invitation.
    include_availability = qkind == "availability"
    allow_availability_mention = qkind == "availability"
    eligible_product_count = _count_eligible_catalog_products(items)

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
        "eligible_product_count": eligible_product_count,
        "has_eligible_products": eligible_product_count > 0,
        "allow_price_mention": include_price and any_price,
        "allow_availability_mention": allow_availability_mention,
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
    meta.update({
        "compose_source": result.source,
        "response_mode": "grounded_persona_compose",
        "llm_candidate_present": result.source == "persona_llm",
        "final_text_transformed": False,
        "final_transform_reasons": [],
        "final_customer_text_source": result.source,
    })
    if result.source == "fallback_deterministic":
        meta["fallback_reason"] = str(
            result.fallback_reason or "compose_unavailable"
        )
        meta["fallback_action_type"] = "catalog_product_answer"
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
    if facts.get("eligible_product_count") is not None:
        meta["eligible_product_count"] = int(facts.get("eligible_product_count") or 0)
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


def _catalog_product_answer_emergency_fallback(
    bundle: PersonaFactsBundle,
    *,
    reason: str,
) -> PersonaComposeResult:
    from .fact_bound_composer import canonical_facts_hash  # noqa: PLC0415

    facts = bundle.verified_facts or {}
    qkind = str(facts.get("question_kind") or "").strip()
    if qkind == "price":
        text = "لا تتوفر تفاصيل سعر مؤكدة في الكتالوج حالياً."
    elif qkind == "availability":
        text = "لا تتوفر حالة توفر مؤكدة في الكتالوج حالياً."
    elif qkind == "browse":
        text = "لا توجد منتجات قابلة للبيع مؤكدة في الكتالوج حالياً."
    else:
        text = "لا تتوفر تفاصيل مؤكدة من الكتالوج حالياً."
    return PersonaComposeResult(
        text=text,
        source="fallback_deterministic",
        surface=PERSONA_SURFACE_CATALOG_PRODUCT_ANSWER,
        facts_hash=canonical_facts_hash(bundle.verified_facts),
        guard_passed=False,
        fallback_reason=str(reason or "compose_unavailable"),
        language=bundle.language,
        dialect=bundle.dialect,
        emoji_count=0,
        latency_ms=0,
        model=None,
    )


def build_catalog_product_answer_emergency_outcome(
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
    reason: str = "compose_unavailable",
    attempted_result: Optional[PersonaComposeResult] = None,
) -> tuple[str, PersonaComposeResult, dict[str, Any]]:
    """Audited one-line fallback for unavailable catalog product compose."""
    settings = dict(ai_settings or {})
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
    from .flags import persona_composer_allowlist_result  # noqa: PLC0415

    fallback = _with_attempted_route_metadata(
        _catalog_product_answer_emergency_fallback(bundle, reason=reason),
        attempted_result,
    )
    event_meta = build_catalog_product_answer_event_metadata(
        fallback,
        tenant_id=int(tenant_id),
        allowlist_result=persona_composer_allowlist_result(
            tenant_id=int(tenant_id),
            customer_phone=str(customer_phone or ""),
            ai_settings=settings,
        ),
        catalog_facts=bundle.verified_facts,
        catalog_fact_products=compose_fact_rows,
    )
    event_meta["llm_candidate_present"] = False
    _log_catalog_compose_telemetry(
        surface=PERSONA_SURFACE_CATALOG_PRODUCT_ANSWER,
        outcome_category="fallback_deterministic",
        eligible_product_count=int(
            bundle.verified_facts.get("eligible_product_count") or 0
        ),
        search_result_count=int(
            bundle.verified_facts.get("search_result_count") or 0
        ),
        question_kind=str(bundle.verified_facts.get("question_kind") or ""),
    )
    return fallback.text.strip(), fallback, event_meta


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
) -> tuple[str, PersonaComposeResult, dict[str, Any]]:
    """Compose catalog-grounded search/Q&A answer with one provider attempt."""
    from .fact_bound_composer import FactBoundPersonaComposer  # noqa: PLC0415

    settings = dict(ai_settings or {})
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
    from .flags import persona_composer_allowlist_result  # noqa: PLC0415

    allowlist_result = persona_composer_allowlist_result(
        tenant_id=int(tenant_id),
        customer_phone=str(customer_phone or ""),
        ai_settings=settings,
    )
    composer = FactBoundPersonaComposer(enforce_gate=False)
    try:
        result = await composer.compose(bundle)
    except Exception as exc:  # noqa: BLE001
        return build_catalog_product_answer_emergency_outcome(
            tenant_id=tenant_id,
            customer_phone=customer_phone,
            inbound_text=inbound_text,
            products=products,
            catalog_search_query=catalog_search_query,
            search_result_count=search_result_count,
            category_scope=category_scope,
            allowed_category=allowed_category,
            question_kind=question_kind,
            category_filter_dropped=category_filter_dropped,
            display_count=display_count,
            decision_args=dict(decision_args or {}),
            ai_settings=settings,
            reason=f"compose_exception:{type(exc).__name__}",
        )
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
        _log_catalog_compose_telemetry(
            surface=PERSONA_SURFACE_CATALOG_PRODUCT_ANSWER,
            outcome_category="persona_llm",
            eligible_product_count=int(
                bundle.verified_facts.get("eligible_product_count") or 0
            ),
            search_result_count=int(
                bundle.verified_facts.get("search_result_count") or 0
            ),
            question_kind=str(bundle.verified_facts.get("question_kind") or ""),
        )
        return result.text.strip(), result, event_meta

    return build_catalog_product_answer_emergency_outcome(
        tenant_id=tenant_id,
        customer_phone=customer_phone,
        inbound_text=inbound_text,
        products=products,
        catalog_search_query=catalog_search_query,
        search_result_count=search_result_count,
        category_scope=category_scope,
        allowed_category=allowed_category,
        question_kind=question_kind,
        category_filter_dropped=category_filter_dropped,
        display_count=display_count,
        decision_args=dict(decision_args or {}),
        ai_settings=settings,
        reason=(
            result.fallback_reason
            or result.guard_failed_reason
            or "compose_empty"
        ),
        attempted_result=result,
    )


def build_catalog_search_miss_facts_bundle(
    *,
    inbound_text: str,
    resolved_subject: str,
    tenant_id: int = 0,
    customer_phone: str = "",
    catalog_search_query: str = "",
    merchant_persona: Optional[dict[str, Any]] = None,
) -> PersonaFactsBundle:
    from .fact_bound_composer import detect_language  # noqa: PLC0415

    inbound = str(inbound_text or "").strip()
    subject = str(resolved_subject or catalog_search_query or "").strip()
    language = detect_language(inbound)
    verified_facts: dict[str, Any] = {
        "surface": PERSONA_SURFACE_CATALOG_PRODUCT_ANSWER,
        "inbound_text": inbound,
        "question_kind": "search_miss",
        "resolved_subject": subject,
        "catalog_search_query": str(catalog_search_query or "").strip(),
        "search_result_count": 0,
        "catalog_products": [],
        "catalog_product_ids": [],
        "has_catalog_products": False,
        "eligible_product_count": 0,
        "has_eligible_products": False,
        "confirmed_match_count": 0,
        "allow_price_mention": False,
        "allow_availability_mention": False,
        "has_positive_availability": False,
        "allow_checkout_pressure": False,
        "allow_slot_prompts": False,
        "allow_superiority_claims": False,
    }
    return PersonaFactsBundle(
        surface=PERSONA_SURFACE_CATALOG_PRODUCT_ANSWER,
        inbound_text=inbound,
        language=language,
        dialect="saudi_arabic" if language == "ar" else None,
        verified_facts=verified_facts,
        customer_context={},
        merchant_persona=dict(merchant_persona or {}),
        constraints=PersonaConstraints(max_chars=320, max_emojis=1),
        tenant_id=int(tenant_id or 0),
        customer_phone=str(customer_phone or ""),
    )


def _catalog_search_miss_emergency_fallback(
    bundle: PersonaFactsBundle,
    *,
    reason: str,
) -> PersonaComposeResult:
    from .fact_bound_composer import canonical_facts_hash  # noqa: PLC0415

    return PersonaComposeResult(
        text="لا يوجد تطابق مؤكد في الكتالوج حالياً.",
        source="fallback_deterministic",
        surface=PERSONA_SURFACE_CATALOG_PRODUCT_ANSWER,
        facts_hash=canonical_facts_hash(bundle.verified_facts),
        guard_passed=False,
        fallback_reason=str(reason or "compose_unavailable"),
        language=bundle.language,
        dialect=bundle.dialect,
        emoji_count=0,
        latency_ms=0,
        model=None,
    )


def _persona_event_with_chosen_path(
    result: PersonaComposeResult,
    *,
    tenant_id: int,
    allowlist_result: str,
    chosen_path: str,
    catalog_facts: Optional[dict[str, Any]] = None,
    catalog_fact_products: Optional[list[dict[str, Any]]] = None,
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    event_meta = build_catalog_product_answer_event_metadata(
        result,
        tenant_id=int(tenant_id),
        allowlist_result=allowlist_result,
        catalog_facts=dict(catalog_facts or {}),
        catalog_fact_products=catalog_fact_products,
    )
    event_meta["chosen_path"] = str(chosen_path or "").strip()
    event_meta.update({
        "compose_source": result.source,
        "response_mode": "grounded_persona_compose",
        "llm_candidate_present": result.source == "persona_llm",
        "final_text_transformed": False,
        "final_transform_reasons": [],
        "final_customer_text_source": result.source,
    })
    if result.source == "fallback_deterministic":
        event_meta["fallback_reason"] = str(
            result.fallback_reason or "compose_unavailable"
        )
    if extra:
        event_meta.update(extra)
    return event_meta


def build_catalog_search_miss_emergency_outcome(
    *,
    tenant_id: int,
    customer_phone: str,
    inbound_text: str,
    resolved_subject: str,
    catalog_search_query: str = "",
    chosen_path: str = "catalog_miss_resolved_subject",
    ai_settings: Optional[dict[str, Any]] = None,
    reason: str = "compose_unavailable",
    attempted_result: Optional[PersonaComposeResult] = None,
) -> tuple[str, PersonaComposeResult, dict[str, Any]]:
    """Audited one-line fallback for an unavailable search-miss compose."""
    settings = dict(ai_settings or {})
    bundle = build_catalog_search_miss_facts_bundle(
        inbound_text=inbound_text,
        resolved_subject=resolved_subject,
        tenant_id=tenant_id,
        customer_phone=customer_phone,
        catalog_search_query=catalog_search_query,
        merchant_persona=settings,
    )
    from .flags import persona_composer_allowlist_result  # noqa: PLC0415

    fallback = _with_attempted_route_metadata(
        _catalog_search_miss_emergency_fallback(bundle, reason=reason),
        attempted_result,
    )
    event_meta = _persona_event_with_chosen_path(
        fallback,
        tenant_id=int(tenant_id),
        allowlist_result=persona_composer_allowlist_result(
            tenant_id=int(tenant_id),
            customer_phone=str(customer_phone or ""),
            ai_settings=settings,
        ),
        chosen_path=chosen_path,
        catalog_facts=bundle.verified_facts,
        extra={
            "question_kind": "search_miss",
            "fallback_action_type": "catalog_search_miss",
        },
    )
    return fallback.text.strip(), fallback, event_meta


async def try_compose_catalog_search_miss_answer(
    *,
    tenant_id: int,
    customer_phone: str,
    inbound_text: str,
    resolved_subject: str,
    catalog_search_query: str = "",
    chosen_path: str = "catalog_miss_resolved_subject",
    ai_settings: Optional[dict[str, Any]] = None,
) -> tuple[str, PersonaComposeResult, dict[str, Any]]:
    """LLM-owned prose for resolved-subject catalog search miss."""
    from .fact_bound_composer import FactBoundPersonaComposer  # noqa: PLC0415

    settings = dict(ai_settings or {})
    bundle = build_catalog_search_miss_facts_bundle(
        inbound_text=inbound_text,
        resolved_subject=resolved_subject,
        tenant_id=tenant_id,
        customer_phone=customer_phone,
        catalog_search_query=catalog_search_query,
        merchant_persona=settings,
    )
    from .flags import persona_composer_allowlist_result  # noqa: PLC0415

    allowlist_result = persona_composer_allowlist_result(
        tenant_id=int(tenant_id),
        customer_phone=str(customer_phone or ""),
        ai_settings=settings,
    )
    composer = FactBoundPersonaComposer(enforce_gate=False)
    try:
        result = await composer.compose(bundle)
    except Exception as exc:  # noqa: BLE001
        return build_catalog_search_miss_emergency_outcome(
            tenant_id=tenant_id,
            customer_phone=customer_phone,
            inbound_text=inbound_text,
            resolved_subject=resolved_subject,
            catalog_search_query=catalog_search_query,
            chosen_path=chosen_path,
            ai_settings=settings,
            reason=f"compose_exception:{type(exc).__name__}",
        )
    if (
        result.source == "persona_llm"
        and result.guard_passed
        and (result.text or "").strip()
    ):
        event_meta = _persona_event_with_chosen_path(
            result,
            tenant_id=int(tenant_id),
            allowlist_result=allowlist_result,
            chosen_path=chosen_path,
            catalog_facts=bundle.verified_facts,
            extra={"question_kind": "search_miss"},
        )
        _log_catalog_compose_telemetry(
            surface=PERSONA_SURFACE_CATALOG_PRODUCT_ANSWER,
            outcome_category="persona_llm",
            eligible_product_count=0,
            search_result_count=0,
            question_kind="search_miss",
        )
        return result.text.strip(), result, event_meta

    return build_catalog_search_miss_emergency_outcome(
        tenant_id=tenant_id,
        customer_phone=customer_phone,
        inbound_text=inbound_text,
        resolved_subject=resolved_subject,
        catalog_search_query=catalog_search_query,
        chosen_path=chosen_path,
        ai_settings=settings,
        reason=(
            result.fallback_reason
            or result.guard_failed_reason
            or "compose_empty"
        ),
        attempted_result=result,
    )


def _catalog_navigation_emergency_fallback(
    bundle: PersonaFactsBundle,
    *,
    reason: str,
) -> PersonaComposeResult:
    from .fact_bound_composer import canonical_facts_hash  # noqa: PLC0415

    return PersonaComposeResult(
        text="تعذر عرض المنتجات حالياً.",
        source="fallback_deterministic",
        surface=PERSONA_SURFACE_CATALOG_PRODUCT_ANSWER,
        facts_hash=canonical_facts_hash(bundle.verified_facts),
        guard_passed=False,
        fallback_reason=str(reason or "compose_unavailable"),
        language=bundle.language,
        dialect=bundle.dialect,
        emoji_count=0,
        latency_ms=0,
        model=None,
    )


def _build_catalog_navigation_bundle(
    *,
    tenant_id: int,
    customer_phone: str,
    inbound_text: str,
    products: list[dict[str, Any]],
    navigator_no_groups_fallback: bool,
    decision_args: Optional[dict[str, Any]],
    settings: dict[str, Any],
) -> tuple[PersonaFactsBundle, list[dict[str, Any]]]:
    compose_fact_rows = catalog_fact_product_rows(products)
    args = dict(decision_args or {})
    qkind = classify_catalog_question_kind(
        inbound_text,
        decision_args=args,
    )
    bundle = build_catalog_product_answer_facts_bundle(
        inbound_text=inbound_text,
        tenant_id=tenant_id,
        customer_phone=customer_phone,
        products=products,
        catalog_search_query="",
        search_result_count=len(compose_fact_rows),
        question_kind=qkind,
        display_count=len(compose_fact_rows),
        decision_args=args,
        merchant_persona=settings,
    )
    verified = dict(bundle.verified_facts)
    verified["navigation_browse"] = qkind == "browse"
    verified["navigator_no_groups_fallback"] = bool(navigator_no_groups_fallback)
    eligible_product_count = _count_eligible_catalog_products(compose_fact_rows)
    verified["eligible_product_count"] = eligible_product_count
    verified["has_eligible_products"] = eligible_product_count > 0
    if not compose_fact_rows:
        verified["catalog_products"] = []
        verified["catalog_product_ids"] = []
        verified["has_catalog_products"] = False
    return PersonaFactsBundle(
        surface=bundle.surface,
        inbound_text=bundle.inbound_text,
        language=bundle.language,
        dialect=bundle.dialect,
        verified_facts=verified,
        customer_context=bundle.customer_context,
        merchant_persona=bundle.merchant_persona,
        constraints=bundle.constraints,
        tenant_id=bundle.tenant_id,
        customer_phone=bundle.customer_phone,
    ), compose_fact_rows


def build_catalog_navigation_emergency_outcome(
    *,
    tenant_id: int,
    customer_phone: str,
    inbound_text: str,
    products: list[dict[str, Any]],
    chosen_path: str = "catalog_navigation_top_products_fallback",
    navigator_no_groups_fallback: bool = False,
    decision_args: Optional[dict[str, Any]] = None,
    ai_settings: Optional[dict[str, Any]] = None,
    reason: str = "compose_unavailable",
    attempted_result: Optional[PersonaComposeResult] = None,
) -> tuple[str, PersonaComposeResult, dict[str, Any]]:
    """Audited one-line fallback for unavailable catalog navigation compose."""
    settings = dict(ai_settings or {})
    bundle, compose_fact_rows = _build_catalog_navigation_bundle(
        tenant_id=tenant_id,
        customer_phone=customer_phone,
        inbound_text=inbound_text,
        products=products,
        navigator_no_groups_fallback=navigator_no_groups_fallback,
        decision_args=decision_args,
        settings=settings,
    )
    from .flags import persona_composer_allowlist_result  # noqa: PLC0415

    fallback = _with_attempted_route_metadata(
        _catalog_navigation_emergency_fallback(bundle, reason=reason),
        attempted_result,
    )
    nav_qkind = str(bundle.verified_facts.get("question_kind") or "browse").strip()
    event_meta = _persona_event_with_chosen_path(
        fallback,
        tenant_id=int(tenant_id),
        allowlist_result=persona_composer_allowlist_result(
            tenant_id=int(tenant_id),
            customer_phone=str(customer_phone or ""),
            ai_settings=settings,
        ),
        chosen_path=chosen_path,
        catalog_facts=bundle.verified_facts,
        catalog_fact_products=compose_fact_rows,
        extra={
            "question_kind": nav_qkind,
            # Names the navigation compose handler entry point, not inbound question_kind.
            "fallback_action_type": "catalog_navigation_browse",
        },
    )
    return fallback.text.strip(), fallback, event_meta


async def try_compose_catalog_navigation_browse_answer(
    *,
    tenant_id: int,
    customer_phone: str,
    inbound_text: str,
    products: list[dict[str, Any]],
    chosen_path: str = "catalog_navigation_top_products_fallback",
    navigator_no_groups_fallback: bool = False,
    decision_args: Optional[dict[str, Any]] = None,
    ai_settings: Optional[dict[str, Any]] = None,
) -> tuple[str, PersonaComposeResult, dict[str, Any]]:
    """LLM-owned browse prose for catalog navigation top-products fallback."""
    from .fact_bound_composer import FactBoundPersonaComposer  # noqa: PLC0415

    settings = dict(ai_settings or {})
    bundle, compose_fact_rows = _build_catalog_navigation_bundle(
        tenant_id=tenant_id,
        customer_phone=customer_phone,
        inbound_text=inbound_text,
        products=products,
        navigator_no_groups_fallback=navigator_no_groups_fallback,
        decision_args=decision_args,
        settings=settings,
    )
    from .flags import persona_composer_allowlist_result  # noqa: PLC0415

    allowlist_result = persona_composer_allowlist_result(
        tenant_id=int(tenant_id),
        customer_phone=str(customer_phone or ""),
        ai_settings=settings,
    )
    composer = FactBoundPersonaComposer(enforce_gate=False)
    try:
        result = await composer.compose(bundle)
    except Exception as exc:  # noqa: BLE001
        return build_catalog_navigation_emergency_outcome(
            tenant_id=tenant_id,
            customer_phone=customer_phone,
            inbound_text=inbound_text,
            products=products,
            chosen_path=chosen_path,
            navigator_no_groups_fallback=navigator_no_groups_fallback,
            decision_args=decision_args,
            ai_settings=settings,
            reason=f"compose_exception:{type(exc).__name__}",
        )
    if (
        result.source == "persona_llm"
        and result.guard_passed
        and (result.text or "").strip()
    ):
        nav_qkind = str(bundle.verified_facts.get("question_kind") or "browse").strip()
        event_meta = _persona_event_with_chosen_path(
            result,
            tenant_id=int(tenant_id),
            allowlist_result=allowlist_result,
            chosen_path=chosen_path,
            catalog_facts=bundle.verified_facts,
            catalog_fact_products=compose_fact_rows,
            extra={"question_kind": nav_qkind},
        )
        _log_catalog_compose_telemetry(
            surface=PERSONA_SURFACE_CATALOG_PRODUCT_ANSWER,
            outcome_category="persona_llm",
            eligible_product_count=int(
                bundle.verified_facts.get("eligible_product_count") or 0
            ),
            search_result_count=int(
                bundle.verified_facts.get("search_result_count") or 0
            ),
            question_kind=nav_qkind,
        )
        return result.text.strip(), result, event_meta

    return build_catalog_navigation_emergency_outcome(
        tenant_id=tenant_id,
        customer_phone=customer_phone,
        inbound_text=inbound_text,
        products=products,
        chosen_path=chosen_path,
        navigator_no_groups_fallback=navigator_no_groups_fallback,
        decision_args=decision_args,
        ai_settings=settings,
        reason=(
            result.fallback_reason
            or result.guard_failed_reason
            or "compose_empty"
        ),
        attempted_result=result,
    )
