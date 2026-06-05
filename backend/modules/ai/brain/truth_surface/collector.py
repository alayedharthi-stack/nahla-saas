"""
truth_surface/collector.py
──────────────────────────
UTS v1 — collect operational facts from the 9 approved ingest surfaces only.
"""
from __future__ import annotations

from typing import Any, List, Optional

from .contract import (
    EffectiveFact,
    EffectiveFactStatus,
    FactDomain,
    OperationalFactKind,
    TruthSource,
    TruthSurface,
)
from .extractors import (
    bundle_to_dict,
    norm_val,
    policy_text,
    product_id,
    product_record,
)


def _fact(
    *,
    fact_key: str,
    fact_domain: FactDomain,
    value: str,
    source_surface: TruthSurface,
    source: TruthSource,
    path: str,
    kind: OperationalFactKind,
    confidence: float = 1.0,
    status: EffectiveFactStatus = EffectiveFactStatus.ACTIVE,
    reason: str = "",
) -> EffectiveFact:
    return EffectiveFact(
        fact_key=fact_key,
        fact_domain=fact_domain,
        value=value,
        source_surface=source_surface,
        source=source,
        confidence=confidence,
        status=status,
        reason=reason,
        path=path,
        kind=kind,
    )


def collect_uts_v1_facts(
    reply_state: Any,
    *,
    goal_regimen_bundle: Any = None,
) -> tuple[List[EffectiveFact], List[str]]:
    """Return raw collected facts (pre-dedup) and list of active ingest surfaces."""
    facts: List[EffectiveFact] = []
    ingested: List[str] = []

    mc = dict(getattr(reply_state, "merchant_context", None) or {})
    kf = dict(getattr(reply_state, "known_facts", None) or {})

    sfb = str(mc.get("structured_facts_block") or "").strip()
    if sfb:
        ingested.append(TruthSurface.STRUCTURED_FACTS_BLOCK.value)
        facts.append(
            _fact(
                fact_key="knowledge:structured_facts_block",
                fact_domain=FactDomain.KNOWLEDGE,
                value=sfb,
                source_surface=TruthSurface.STRUCTURED_FACTS_BLOCK,
                source=TruthSource.MERCHANT_KNOWLEDGE_SECTIONS,
                path="merchant_context.structured_facts_block",
                kind=OperationalFactKind.POLICY,
                confidence=0.95,
            )
        )

    pexcerpt = str(getattr(reply_state, "platform_kb_excerpt", "") or "").strip()
    if pexcerpt:
        ingested.append(TruthSurface.PLATFORM_KB_EXCERPT.value)
        facts.append(
            _fact(
                fact_key="platform:kb_excerpt",
                fact_domain=FactDomain.PLATFORM,
                value=pexcerpt,
                source_surface=TruthSurface.PLATFORM_KB_EXCERPT,
                source=TruthSource.MANUAL_KNOWLEDGE_BASE,
                path="platform_kb_excerpt",
                kind=OperationalFactKind.PLATFORM_SUBSCRIPTION,
                confidence=0.95,
            )
        )

    products = list(mc.get("products") or [])
    if products:
        ingested.append(TruthSurface.MERCHANT_CONTEXT_PRODUCTS.value)
        for idx, product in enumerate(products):
            if not isinstance(product, dict):
                continue
            rec = product_record(
                product,
                surface=TruthSurface.MERCHANT_CONTEXT_PRODUCTS,
                source=TruthSource.PRODUCTS_TABLE,
                path=f"merchant_context.products[{idx}]",
            )
            pid = rec["product_key"]
            if rec["title"]:
                facts.append(
                    _fact(
                        fact_key=f"catalog:{pid}:title",
                        fact_domain=FactDomain.CATALOG,
                        value=rec["title"],
                        source_surface=TruthSurface.MERCHANT_CONTEXT_PRODUCTS,
                        source=TruthSource.PRODUCTS_TABLE,
                        path=rec["path"],
                        kind=OperationalFactKind.PRODUCT_TITLE,
                    )
                )
            if rec["price"]:
                facts.append(
                    _fact(
                        fact_key=f"catalog:{pid}:price",
                        fact_domain=FactDomain.CATALOG,
                        value=rec["price"],
                        source_surface=TruthSurface.MERCHANT_CONTEXT_PRODUCTS,
                        source=TruthSource.PRODUCTS_TABLE,
                        path=rec["path"],
                        kind=OperationalFactKind.PRICE,
                    )
                )
            if rec["orderable"]:
                facts.append(
                    _fact(
                        fact_key=f"catalog:{pid}:orderable",
                        fact_domain=FactDomain.CATALOG,
                        value=rec["orderable"],
                        source_surface=TruthSurface.MERCHANT_CONTEXT_PRODUCTS,
                        source=TruthSource.PRODUCTS_TABLE,
                        path=rec["path"],
                        kind=OperationalFactKind.AVAILABILITY,
                    )
                )
            if rec["product_url"]:
                facts.append(
                    _fact(
                        fact_key=f"catalog:{pid}:url",
                        fact_domain=FactDomain.CATALOG,
                        value=rec["product_url"],
                        source_surface=TruthSurface.MERCHANT_CONTEXT_PRODUCTS,
                        source=TruthSource.PRODUCTS_TABLE,
                        path=rec["path"],
                        kind=OperationalFactKind.PRODUCT_LINK,
                    )
                )

    sel = getattr(reply_state, "selected_product", None)
    if isinstance(sel, dict) and sel:
        ingested.append(TruthSurface.SELECTED_PRODUCT.value)
        rec = product_record(
            sel,
            surface=TruthSurface.SELECTED_PRODUCT,
            source=TruthSource.PRODUCTS_TABLE,
            path="selected_product",
        )
        pid = rec["product_key"]
        for field, kind in (
            ("title", OperationalFactKind.PRODUCT_TITLE),
            ("price", OperationalFactKind.PRICE),
            ("orderable", OperationalFactKind.AVAILABILITY),
            ("product_url", OperationalFactKind.PRODUCT_LINK),
        ):
            val = rec.get(field if field != "product_url" else "product_url")
            if val:
                facts.append(
                    _fact(
                        fact_key=f"catalog:{pid}:{field}",
                        fact_domain=FactDomain.CATALOG,
                        value=val,
                        source_surface=TruthSurface.SELECTED_PRODUCT,
                        source=TruthSource.PRODUCTS_TABLE,
                        path="selected_product",
                        kind=kind,
                        reason="projection:selected_product",
                    )
                )

    recs = list(getattr(reply_state, "last_recommended_products", None) or [])
    if recs:
        ingested.append(TruthSurface.LAST_RECOMMENDED_PRODUCTS.value)
        for idx, product in enumerate(recs):
            if not isinstance(product, dict):
                continue
            rec = product_record(
                product,
                surface=TruthSurface.LAST_RECOMMENDED_PRODUCTS,
                source=TruthSource.PRODUCTS_TABLE,
                path=f"last_recommended_products[{idx}]",
            )
            pid = rec["product_key"]
            if rec["title"]:
                facts.append(
                    _fact(
                        fact_key=f"catalog:{pid}:title",
                        fact_domain=FactDomain.CATALOG,
                        value=rec["title"],
                        source_surface=TruthSurface.LAST_RECOMMENDED_PRODUCTS,
                        source=TruthSource.PRODUCTS_TABLE,
                        path=rec["path"],
                        kind=OperationalFactKind.PRODUCT_TITLE,
                        reason="projection:last_recommended",
                    )
                )
            if rec["price"]:
                facts.append(
                    _fact(
                        fact_key=f"catalog:{pid}:price",
                        fact_domain=FactDomain.CATALOG,
                        value=rec["price"],
                        source_surface=TruthSurface.LAST_RECOMMENDED_PRODUCTS,
                        source=TruthSource.PRODUCTS_TABLE,
                        path=rec["path"],
                        kind=OperationalFactKind.PRICE,
                        reason="projection:last_recommended",
                    )
                )

    checkout = dict(kf.get("checkout_preparation") or {})
    if checkout:
        ingested.append(TruthSurface.CHECKOUT_PREPARATION.value)
        for ck, cv in checkout.items():
            if cv is None or not norm_val(cv):
                continue
            kind = OperationalFactKind.ORDER_STATUS
            if "payment" in ck:
                kind = OperationalFactKind.PAYMENT_STATE
            facts.append(
                _fact(
                    fact_key=f"order:{ck}",
                    fact_domain=FactDomain.ORDER,
                    value=norm_val(cv),
                    source_surface=TruthSurface.CHECKOUT_PREPARATION,
                    source=TruthSource.ORDER_PREPARATION_STATE,
                    path=f"known_facts.checkout_preparation.{ck}",
                    kind=kind,
                )
            )

    policies = mc.get("policies")
    pol_text = policy_text(policies)
    if pol_text:
        ingested.append(TruthSurface.MERCHANT_CONTEXT_POLICIES.value)
        facts.append(
            _fact(
                fact_key="policy:merchant_context.policies",
                fact_domain=FactDomain.POLICY,
                value=pol_text,
                source_surface=TruthSurface.MERCHANT_CONTEXT_POLICIES,
                source=TruthSource.STORE_SNAPSHOT,
                path="merchant_context.policies",
                kind=OperationalFactKind.POLICY,
            )
        )

    store_fields = (
        "store_name", "store_url", "orderable", "product_count",
        "in_stock_count", "support_hours", "contact_phone", "contact_email",
        "shipping_policy", "shipping_methods", "shipping_notes",
    )
    kf_has_any = any(kf.get(f) is not None and norm_val(kf.get(f)) for f in store_fields)
    if kf_has_any:
        ingested.append(TruthSurface.KNOWN_FACTS.value)
        for field in store_fields:
            val = kf.get(field)
            if val is None or not norm_val(val):
                continue
            kind = OperationalFactKind.STORE_IDENTITY
            domain = FactDomain.STORE
            if field.startswith("shipping"):
                kind = OperationalFactKind.SHIPPING
                domain = FactDomain.POLICY
            elif field in {"contact_phone", "contact_email"}:
                kind = OperationalFactKind.CONTACT
            elif field in {"orderable", "product_count", "in_stock_count"}:
                kind = OperationalFactKind.AVAILABILITY
            facts.append(
                _fact(
                    fact_key=f"store:{field}",
                    fact_domain=domain,
                    value=norm_val(val),
                    source_surface=TruthSurface.KNOWN_FACTS,
                    source=TruthSource.STORE_SNAPSHOT,
                    path=f"known_facts.{field}",
                    kind=kind,
                )
            )

    bundle = bundle_to_dict(goal_regimen_bundle)
    if bundle:
        ingested.append(TruthSurface.GOAL_REGIMEN_BUNDLE.value)
        goal = norm_val(bundle.get("goal"))
        if goal:
            facts.append(
                _fact(
                    fact_key="goal:discovered_goal",
                    fact_domain=FactDomain.GOAL,
                    value=goal,
                    source_surface=TruthSurface.GOAL_REGIMEN_BUNDLE,
                    source=TruthSource.GOAL_KB_RETRIEVAL,
                    path="goal_regimen_bundle.goal",
                    kind=OperationalFactKind.USAGE_GUIDANCE,
                )
            )
        for idx, ug in enumerate(bundle.get("usage_guidance") or []):
            if norm_val(ug):
                facts.append(
                    _fact(
                        fact_key=f"goal:usage_guidance:{idx}",
                        fact_domain=FactDomain.GOAL,
                        value=norm_val(ug)[:500],
                        source_surface=TruthSurface.GOAL_REGIMEN_BUNDLE,
                        source=TruthSource.GOAL_KB_RETRIEVAL,
                        path=f"goal_regimen_bundle.usage_guidance[{idx}]",
                        kind=OperationalFactKind.USAGE_GUIDANCE,
                        confidence=0.9,
                    )
                )
        for idx, item in enumerate(bundle.get("items") or []):
            if not isinstance(item, dict):
                continue
            title = norm_val(item.get("title"))
            if title:
                pid = product_id(item) or f"goal_item:{idx}"
                facts.append(
                    _fact(
                        fact_key=f"goal:catalog:{pid}:title",
                        fact_domain=FactDomain.GOAL,
                        value=title,
                        source_surface=TruthSurface.GOAL_REGIMEN_BUNDLE,
                        source=TruthSource.GOAL_KB_RETRIEVAL,
                        path=f"goal_regimen_bundle.items[{idx}]",
                        kind=OperationalFactKind.PRODUCT_TITLE,
                        confidence=0.85,
                    )
                )

    return facts, sorted(set(ingested))


__all__ = ["collect_uts_v1_facts"]
