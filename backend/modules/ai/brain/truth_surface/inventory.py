"""
truth_surface/inventory.py
──────────────────────────
Collect operational facts from each truth surface — read-only, no mutation.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import asdict, is_dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .contract import (
    ConflictGroup,
    DuplicateGroup,
    OperationalFact,
    OperationalFactKind,
    SurfacePresence,
    TruthSource,
    TruthSurface,
    TruthSurfaceInventory,
)

_PRICE_RE = re.compile(
    r"(\d[\d.,]*)\s*(?:ريال|SAR|ر\.?\s*س|rs)",
    re.IGNORECASE,
)
_AVAIL_POS_RE = re.compile(
    r"(?:متوفر|متاح|available|in\s*stock)",
    re.IGNORECASE,
)
_AVAIL_NEG_RE = re.compile(
    r"(?:غير\s*متوفر|غير\s*متاح|نفد|نفذ|unavailable|out\s*of\s*stock)",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_PRODUCT_MARKER_RE = re.compile(r"\[PRODUCT:([^\]]+)\]", re.IGNORECASE)
_MEDIA_KEY_RE = re.compile(r"\[MEDIA_KEY:([^\]]+)\]", re.IGNORECASE)


def _norm_val(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return str(value).strip()


def _facts_from_text(
    text: str,
    *,
    surface: TruthSurface,
    source: TruthSource,
    path_prefix: str,
) -> List[OperationalFact]:
    if not (text or "").strip():
        return []
    out: List[OperationalFact] = []
    for idx, m in enumerate(_PRICE_RE.finditer(text)):
        out.append(
            OperationalFact(
                kind=OperationalFactKind.PRICE,
                key=f"{path_prefix}:text_price:{idx}",
                value=m.group(0).strip(),
                surface=surface,
                source=source,
                path=path_prefix,
            )
        )
    if _AVAIL_POS_RE.search(text) and not _AVAIL_NEG_RE.search(text):
        out.append(
            OperationalFact(
                kind=OperationalFactKind.AVAILABILITY,
                key=f"{path_prefix}:availability",
                value="available",
                surface=surface,
                source=source,
                path=path_prefix,
            )
        )
    elif _AVAIL_NEG_RE.search(text):
        out.append(
            OperationalFact(
                kind=OperationalFactKind.AVAILABILITY,
                key=f"{path_prefix}:availability",
                value="unavailable",
                surface=surface,
                source=source,
                path=path_prefix,
            )
        )
    for idx, m in enumerate(_URL_RE.finditer(text)):
        out.append(
            OperationalFact(
                kind=OperationalFactKind.PRODUCT_LINK,
                key=f"{path_prefix}:url:{idx}",
                value=m.group(0).strip(),
                surface=surface,
                source=source,
                path=path_prefix,
            )
        )
    for m in _PRODUCT_MARKER_RE.finditer(text):
        title = m.group(1).strip()
        out.append(
            OperationalFact(
                kind=OperationalFactKind.PRODUCT_TITLE,
                key=f"product_title:{title.casefold()}",
                value=title,
                surface=surface,
                source=source,
                path=path_prefix,
            )
        )
    for m in _MEDIA_KEY_RE.finditer(text):
        slug = m.group(1).strip()
        out.append(
            OperationalFact(
                kind=OperationalFactKind.MEDIA_KEY,
                key=f"media_key:{slug}",
                value=slug,
                surface=surface,
                source=source,
                path=path_prefix,
            )
        )
    return out


def _product_facts(
    product: Dict[str, Any],
    *,
    surface: TruthSurface,
    source: TruthSource,
    path: str,
) -> List[OperationalFact]:
    if not isinstance(product, dict):
        return []
    pid = product.get("id") or product.get("external_id") or product.get("title") or path
    prefix = f"product:{pid}"
    out: List[OperationalFact] = []
    title = _norm_val(product.get("title"))
    if title:
        out.append(
            OperationalFact(
                kind=OperationalFactKind.PRODUCT_TITLE,
                key=f"{prefix}:title",
                value=title,
                surface=surface,
                source=source,
                path=path,
            )
        )
    price = product.get("price")
    if price is not None and _norm_val(price):
        out.append(
            OperationalFact(
                kind=OperationalFactKind.PRICE,
                key=f"{prefix}:price",
                value=_norm_val(price),
                surface=surface,
                source=source,
                path=path,
            )
        )
    orderable = product.get("orderable")
    if orderable is None:
        orderable = product.get("can_checkout")
    if orderable is not None:
        out.append(
            OperationalFact(
                kind=OperationalFactKind.AVAILABILITY,
                key=f"{prefix}:orderable",
                value=_norm_val(orderable),
                surface=surface,
                source=source,
                path=path,
            )
        )
    url = _norm_val(product.get("product_url") or product.get("url"))
    if url:
        out.append(
            OperationalFact(
                kind=OperationalFactKind.PRODUCT_LINK,
                key=f"{prefix}:url",
                value=url,
                surface=surface,
                source=source,
                path=path,
            )
        )
    return out


def _presence(
    surface: TruthSurface,
    content: Any,
    *,
    source: TruthSource = TruthSource.UNKNOWN,
    fact_count: int = 0,
) -> SurfacePresence:
    if isinstance(content, str):
        char_count = len(content.strip())
        active = char_count > 0
    elif isinstance(content, (list, dict)):
        char_count = len(str(content))
        active = bool(content)
    elif content is None:
        char_count = 0
        active = False
    else:
        char_count = len(str(content))
        active = bool(content)
    return SurfacePresence(
        surface=surface,
        active=active,
        char_count=char_count,
        fact_count=fact_count,
        source=source,
    )


def _bundle_to_dict(bundle: Any) -> Dict[str, Any]:
    if bundle is None:
        return {}
    if isinstance(bundle, dict):
        return bundle
    if is_dataclass(bundle) and not isinstance(bundle, type):
        return asdict(bundle)
    if hasattr(bundle, "to_dict") and callable(bundle.to_dict):
        return bundle.to_dict()
    return {}


def build_truth_surface_inventory(
    reply_state: Any,
    *,
    tenant_id: Optional[int] = None,
    history_messages: Optional[Sequence[Dict[str, Any]]] = None,
    goal_regimen_bundle: Any = None,
    sales_context: Any = None,
    full_merchant_context: Optional[Dict[str, Any]] = None,
) -> TruthSurfaceInventory:
    """Scan all known surfaces on an LLM-bound turn. Pure read — no side effects."""
    facts: List[OperationalFact] = []
    presences: List[SurfacePresence] = []
    latent: List[str] = []

    intent = str(getattr(reply_state, "intent_name", "") or "")
    stage = str(getattr(reply_state, "stage", "") or "")
    mc = dict(getattr(reply_state, "merchant_context", None) or {})

    # ── Structured facts block ─────────────────────────────────────────────
    sfb = str(mc.get("structured_facts_block") or "").strip()
    sfb_facts = _facts_from_text(
        sfb,
        surface=TruthSurface.STRUCTURED_FACTS_BLOCK,
        source=TruthSource.MERCHANT_KNOWLEDGE_SECTIONS,
        path_prefix="structured_facts_block",
    )
    facts.extend(sfb_facts)
    presences.append(
        _presence(
            TruthSurface.STRUCTURED_FACTS_BLOCK,
            sfb,
            source=TruthSource.MERCHANT_KNOWLEDGE_SECTIONS,
            fact_count=len(sfb_facts),
        )
    )

    # ── Platform excerpt ───────────────────────────────────────────────────
    pexcerpt = str(getattr(reply_state, "platform_kb_excerpt", "") or "").strip()
    pex_facts = _facts_from_text(
        pexcerpt,
        surface=TruthSurface.PLATFORM_KB_EXCERPT,
        source=TruthSource.MANUAL_KNOWLEDGE_BASE,
        path_prefix="platform_kb_excerpt",
    )
    facts.extend(pex_facts)
    presences.append(
        _presence(
            TruthSurface.PLATFORM_KB_EXCERPT,
            pexcerpt,
            source=TruthSource.MANUAL_KNOWLEDGE_BASE,
            fact_count=len(pex_facts),
        )
    )

    # ── Clarification evidence ─────────────────────────────────────────────
    clarify = dict(getattr(reply_state, "clarification_evidence", None) or {})
    clarify_facts: List[OperationalFact] = []
    if clarify:
        for k, v in clarify.items():
            if k in {"product_focus_title", "product_focus_id", "stage", "has_order_prep"}:
                kind = OperationalFactKind.ORDER_STATUS if "order" in k or k == "stage" else OperationalFactKind.OTHER_OPERATIONAL
                clarify_facts.append(
                    OperationalFact(
                        kind=kind,
                        key=f"clarify:{k}",
                        value=_norm_val(v),
                        surface=TruthSurface.CLARIFICATION_EVIDENCE,
                        source=TruthSource.ORDER_PREPARATION_STATE,
                        path=f"clarification_evidence.{k}",
                    )
                )
    facts.extend(clarify_facts)
    presences.append(
        _presence(
            TruthSurface.CLARIFICATION_EVIDENCE,
            clarify,
            source=TruthSource.ORDER_PREPARATION_STATE,
            fact_count=len(clarify_facts),
        )
    )

    # ── known_facts ────────────────────────────────────────────────────────
    kf = dict(getattr(reply_state, "known_facts", None) or {})
    kf_facts: List[OperationalFact] = []
    for field in (
        "store_name", "store_url", "shipping_policy", "shipping_methods",
        "shipping_notes", "support_hours", "contact_phone", "contact_email",
        "orderable", "product_count", "in_stock_count",
    ):
        val = kf.get(field)
        if val is not None and _norm_val(val):
            kind = OperationalFactKind.STORE_IDENTITY
            if "shipping" in field:
                kind = OperationalFactKind.SHIPPING
            elif field in {"contact_phone", "contact_email"}:
                kind = OperationalFactKind.CONTACT
            elif field in {"orderable", "product_count", "in_stock_count"}:
                kind = OperationalFactKind.AVAILABILITY
            kf_facts.append(
                OperationalFact(
                    kind=kind,
                    key=f"known_facts:{field}",
                    value=_norm_val(val),
                    surface=TruthSurface.KNOWN_FACTS,
                    source=TruthSource.STORE_SNAPSHOT,
                    path=f"known_facts.{field}",
                )
            )
    checkout = dict(kf.get("checkout_preparation") or {})
    for ck, cv in checkout.items():
        if cv is None or not _norm_val(cv):
            continue
        kind = OperationalFactKind.ORDER_STATUS
        if "payment" in ck:
            kind = OperationalFactKind.PAYMENT_STATE
        kf_facts.append(
            OperationalFact(
                kind=kind,
                key=f"checkout:{ck}",
                value=_norm_val(cv),
                surface=TruthSurface.CHECKOUT_PREPARATION,
                source=TruthSource.ORDER_PREPARATION_STATE,
                path=f"known_facts.checkout_preparation.{ck}",
            )
        )
    facts.extend(kf_facts)
    presences.append(
        _presence(
            TruthSurface.KNOWN_FACTS,
            kf,
            source=TruthSource.STORE_SNAPSHOT,
            fact_count=len(kf_facts),
        )
    )

    # ── merchant_context.products ──────────────────────────────────────────
    products = list(mc.get("products") or [])
    prod_facts: List[OperationalFact] = []
    for idx, p in enumerate(products):
        prod_facts.extend(
            _product_facts(
                p,
                surface=TruthSurface.MERCHANT_CONTEXT_PRODUCTS,
                source=TruthSource.PRODUCTS_TABLE,
                path=f"merchant_context.products[{idx}]",
            )
        )
    facts.extend(prod_facts)
    presences.append(
        _presence(
            TruthSurface.MERCHANT_CONTEXT_PRODUCTS,
            products,
            source=TruthSource.PRODUCTS_TABLE,
            fact_count=len(prod_facts),
        )
    )

    # ── selected_product ───────────────────────────────────────────────────
    sel = getattr(reply_state, "selected_product", None)
    sel_facts = _product_facts(
        sel or {},
        surface=TruthSurface.SELECTED_PRODUCT,
        source=TruthSource.PRODUCTS_TABLE,
        path="selected_product",
    )
    facts.extend(sel_facts)
    presences.append(
        _presence(
            TruthSurface.SELECTED_PRODUCT,
            sel,
            source=TruthSource.PRODUCTS_TABLE,
            fact_count=len(sel_facts),
        )
    )

    # ── last_recommended_products ─────────────────────────────────────────
    recs = list(getattr(reply_state, "last_recommended_products", None) or [])
    rec_facts: List[OperationalFact] = []
    for idx, p in enumerate(recs):
        rec_facts.extend(
            _product_facts(
                p,
                surface=TruthSurface.LAST_RECOMMENDED_PRODUCTS,
                source=TruthSource.PRODUCTS_TABLE,
                path=f"last_recommended_products[{idx}]",
            )
        )
    facts.extend(rec_facts)
    presences.append(
        _presence(
            TruthSurface.LAST_RECOMMENDED_PRODUCTS,
            recs,
            source=TruthSource.PRODUCTS_TABLE,
            fact_count=len(rec_facts),
        )
    )

    # ── store_knowledge ────────────────────────────────────────────────────
    sk = dict(getattr(reply_state, "store_knowledge", None) or {})
    sk_facts: List[OperationalFact] = []
    for field in ("store_name", "store_url", "business_hours", "description"):
        val = sk.get(field)
        if val and _norm_val(val):
            sk_facts.append(
                OperationalFact(
                    kind=OperationalFactKind.STORE_IDENTITY,
                    key=f"store_knowledge:{field}",
                    value=_norm_val(val),
                    surface=TruthSurface.STORE_KNOWLEDGE,
                    source=TruthSource.STORE_SNAPSHOT,
                    path=f"store_knowledge.{field}",
                )
            )
    facts.extend(sk_facts)
    presences.append(
        _presence(
            TruthSurface.STORE_KNOWLEDGE,
            sk,
            source=TruthSource.STORE_SNAPSHOT,
            fact_count=len(sk_facts),
        )
    )

    # ── policies ───────────────────────────────────────────────────────────
    policies = dict(mc.get("policies") or {})
    pol_facts = _facts_from_text(
        str(policies),
        surface=TruthSurface.MERCHANT_CONTEXT_POLICIES,
        source=TruthSource.STORE_SNAPSHOT,
        path_prefix="merchant_context.policies",
    )
    facts.extend(pol_facts)
    presences.append(
        _presence(
            TruthSurface.MERCHANT_CONTEXT_POLICIES,
            policies,
            source=TruthSource.STORE_SNAPSHOT,
            fact_count=len(pol_facts),
        )
    )

    # ── manual_knowledge_base in ai_settings (JSON echo) ───────────────────
    ai_settings = dict(mc.get("ai_settings") or {})
    mkb = str(ai_settings.get("manual_knowledge_base") or "").strip()
    mkb_facts = _facts_from_text(
        mkb,
        surface=TruthSurface.MERCHANT_CONTEXT_AI_SETTINGS,
        source=TruthSource.MANUAL_KNOWLEDGE_BASE,
        path_prefix="merchant_context.ai_settings.manual_knowledge_base",
    )
    facts.extend(mkb_facts)
    presences.append(
        _presence(
            TruthSurface.MERCHANT_CONTEXT_AI_SETTINGS,
            mkb,
            source=TruthSource.MANUAL_KNOWLEDGE_BASE,
            fact_count=len(mkb_facts),
        )
    )

    # ── FAQ approved ───────────────────────────────────────────────────────
    faq = list(mc.get("faq_approved") or [])
    faq_facts: List[OperationalFact] = []
    for idx, item in enumerate(faq):
        if isinstance(item, dict):
            q = _norm_val(item.get("question") or item.get("q"))
            a = _norm_val(item.get("answer") or item.get("a"))
            if q or a:
                faq_facts.append(
                    OperationalFact(
                        kind=OperationalFactKind.FAQ,
                        key=f"faq:{idx}",
                        value=f"{q} => {a}"[:500],
                        surface=TruthSurface.MERCHANT_CONTEXT_FAQ,
                        source=TruthSource.MERCHANT_KNOWLEDGE_SECTIONS,
                        path=f"merchant_context.faq_approved[{idx}]",
                    )
                )
    facts.extend(faq_facts)
    presences.append(
        _presence(
            TruthSurface.MERCHANT_CONTEXT_FAQ,
            faq,
            source=TruthSource.MERCHANT_KNOWLEDGE_SECTIONS,
            fact_count=len(faq_facts),
        )
    )

    # ── coupon_policy ──────────────────────────────────────────────────────
    cp = dict(getattr(reply_state, "coupon_policy", None) or {})
    cp_facts: List[OperationalFact] = []
    for ck, cv in cp.items():
        if cv is not None and _norm_val(cv):
            cp_facts.append(
                OperationalFact(
                    kind=OperationalFactKind.COUPON,
                    key=f"coupon_policy:{ck}",
                    value=_norm_val(cv),
                    surface=TruthSurface.COUPON_POLICY,
                    source=TruthSource.COUPON_TABLE,
                    path=f"coupon_policy.{ck}",
                )
            )
    facts.extend(cp_facts)
    presences.append(
        _presence(
            TruthSurface.COUPON_POLICY,
            cp,
            source=TruthSource.COUPON_TABLE,
            fact_count=len(cp_facts),
        )
    )

    # ── response_goal ──────────────────────────────────────────────────────
    rg = str(getattr(reply_state, "response_goal", "") or "").strip()
    rg_facts = _facts_from_text(
        rg,
        surface=TruthSurface.RESPONSE_GOAL,
        source=TruthSource.UNKNOWN,
        path_prefix="response_goal",
    )
    facts.extend(rg_facts)
    presences.append(
        _presence(
            TruthSurface.RESPONSE_GOAL,
            rg,
            fact_count=len(rg_facts),
        )
    )

    # ── resolver overlay ───────────────────────────────────────────────────
    ro = str(mc.get("resolver_overlay") or "").strip()
    ro_facts = _facts_from_text(
        ro,
        surface=TruthSurface.RESOLVER_OVERLAY,
        source=TruthSource.MEDIA_REGISTRY,
        path_prefix="resolver_overlay",
    )
    facts.extend(ro_facts)
    presences.append(
        _presence(
            TruthSurface.RESOLVER_OVERLAY,
            ro,
            source=TruthSource.MEDIA_REGISTRY,
            fact_count=len(ro_facts),
        )
    )

    # ── goal regimen bundle ────────────────────────────────────────────────
    bundle = _bundle_to_dict(goal_regimen_bundle)
    bundle_facts: List[OperationalFact] = []
    if bundle:
        for idx, item in enumerate(bundle.get("items") or []):
            if isinstance(item, dict):
                bundle_facts.extend(
                    _product_facts(
                        item,
                        surface=TruthSurface.GOAL_REGIMEN_BUNDLE,
                        source=TruthSource.GOAL_KB_RETRIEVAL,
                        path=f"goal_regimen_bundle.items[{idx}]",
                    )
                )
        for idx, ug in enumerate(bundle.get("usage_guidance") or []):
            if _norm_val(ug):
                bundle_facts.append(
                    OperationalFact(
                        kind=OperationalFactKind.USAGE_GUIDANCE,
                        key=f"goal:usage_guidance:{idx}",
                        value=_norm_val(ug)[:300],
                        surface=TruthSurface.GOAL_REGIMEN_BUNDLE,
                        source=TruthSource.GOAL_KB_RETRIEVAL,
                        path=f"goal_regimen_bundle.usage_guidance[{idx}]",
                    )
                )
    facts.extend(bundle_facts)
    presences.append(
        _presence(
            TruthSurface.GOAL_REGIMEN_BUNDLE,
            bundle,
            source=TruthSource.GOAL_KB_RETRIEVAL,
            fact_count=len(bundle_facts),
        )
    )

    # ── BrainStateJSON aggregate (always active when reply_state exists) ───
    presences.append(
        _presence(
            TruthSurface.BRAIN_STATE_JSON,
            reply_state,
            source=TruthSource.UNKNOWN,
            fact_count=len(facts),
        )
    )

    # ── Chat history (assistant turns only) ─────────────────────────────────
    hist_facts: List[OperationalFact] = []
    hist_chars = 0
    for idx, msg in enumerate(history_messages or []):
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "").lower()
        if role not in {"assistant", "model"}:
            continue
        body = str(msg.get("content") or "").strip()
        if not body:
            continue
        hist_chars += len(body)
        hist_facts.extend(
            _facts_from_text(
                body,
                surface=TruthSurface.CHAT_HISTORY,
                source=TruthSource.CONVERSATION_HISTORY,
                path_prefix=f"chat_history[{idx}]",
            )
        )
    facts.extend(hist_facts)
    presences.append(
        _presence(
            TruthSurface.CHAT_HISTORY,
            "x" * hist_chars if hist_chars else "",
            source=TruthSource.CONVERSATION_HISTORY,
            fact_count=len(hist_facts),
        )
    )

    # ── Latent surfaces (loaded but not in primary prompt) ───────────────────
    overlay = str(getattr(reply_state, "tenant_overlay", "") or "").strip()
    if overlay:
        latent.append(TruthSurface.TENANT_OVERLAY_LEGACY.value)

    if sales_context is not None:
        latent.append(TruthSurface.SALES_CONTEXT_METADATA.value)

    if full_merchant_context:
        slim_keys = set(mc.keys())
        extra_keys = set(full_merchant_context.keys()) - slim_keys
        if extra_keys:
            latent.append(TruthSurface.FULL_MERCHANT_CONTEXT_LATENT.value)

    if mkb and sfb:
        latent.append(
            "overlay_facts_fallback_suppressed"
        )

    duplicates, conflicts = _detect_duplicates_and_conflicts(facts)

    return TruthSurfaceInventory(
        tenant_id=tenant_id,
        intent=intent,
        stage=stage,
        surfaces_active=presences,
        facts=facts,
        duplicates=duplicates,
        conflicts=conflicts,
        latent_surfaces=latent,
    )


def _canonical_conflict_key(fact: OperationalFact) -> str:
    """Group facts that refer to the same operational entity."""
    key = fact.key
    if key.startswith("product:") and ":price" in key:
        return key.rsplit(":", 1)[0] + ":price"
    if key.startswith("product:") and ":orderable" in key:
        return key.rsplit(":", 1)[0] + ":orderable"
    if key in {"known_facts:store_name", "store_knowledge:store_name"}:
        return "store:name"
    if key in {"known_facts:store_url", "store_knowledge:store_url"}:
        return "store:url"
    if fact.kind == OperationalFactKind.PRODUCT_TITLE and key.startswith("product_title:"):
        return key
    return f"{fact.kind.value}:{key}"


def _detect_duplicates_and_conflicts(
    facts: Iterable[OperationalFact],
) -> tuple[List[DuplicateGroup], List[ConflictGroup]]:
    by_key: Dict[str, List[OperationalFact]] = defaultdict(list)
    for f in facts:
        ck = _canonical_conflict_key(f)
        by_key[ck].append(f)

    duplicates: List[DuplicateGroup] = []
    conflicts: List[ConflictGroup] = []

    for ck, group in by_key.items():
        if len(group) < 2:
            continue
        surfaces = sorted({g.surface.value for g in group})
        values = sorted({g.value for g in group})
        if len(surfaces) > 1:
            if len(values) > 1:
                conflicts.append(
                    ConflictGroup(
                        key=ck,
                        kind=group[0].kind,
                        entries=[
                            {
                                "surface": g.surface.value,
                                "value": g.value,
                                "path": g.path,
                                "source": g.source.value,
                            }
                            for g in group
                        ],
                    )
                )
            else:
                duplicates.append(
                    DuplicateGroup(
                        key=ck,
                        kind=group[0].kind,
                        surfaces=surfaces,
                        values=values,
                    )
                )
        elif len(group) > 1:
            duplicates.append(
                DuplicateGroup(
                    key=ck,
                    kind=group[0].kind,
                    surfaces=surfaces,
                    values=values,
                )
            )

    return duplicates, conflicts


__all__ = ["build_truth_surface_inventory"]
