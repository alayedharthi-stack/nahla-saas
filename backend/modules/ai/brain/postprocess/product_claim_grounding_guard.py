"""
product_claim_grounding_guard.py
────────────────────────────────
Post-compose guard: block ungrounded product claims (prices, taste/medical
comparisons, recommendations of unavailable SKUs, and contradictions after
catalog-miss / no-synced signals).

Enforce mode strips unsupported claim sentences/chunks only; it does not
author canned merchant prose. When stripping leaves no usable customer-facing
content, callers may invoke grounded recompose once.

Modes (NAHLA_PRODUCT_CLAIM_GROUNDING_GUARD_MODE):
  off     — disabled
  shadow  — log only
  enforce — strip blocked claims (default)
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Set

from modules.ai.brain.postprocess.product_claim_grounding_evidence import (
    ProductClaimGroundingEvidence,
    build_product_claim_grounding_evidence,
    extract_reply_prices,
    _norm,
    _text_references_product,
)
from modules.ai.brain.turn_owner_contract import (
    POSTPROCESS_MEDICAL_CLAIM_REWRITE,
    POSTPROCESS_PRODUCT_BENEFIT_REWRITE,
    get_turn_owner_contract,
)

logger = logging.getLogger("nahla.brain.postprocess.product_claim_grounding_guard")

_USABLE_CUSTOMER_CONTENT_RE = re.compile(r"[a-zA-Z0-9\u0600-\u06FF]")

_DETERMINISTIC_ALLOW_PATHS = frozenset({
    "variant_pricing",
    "product_search_results",
    "product_card_send",
    "notify_me_back_in_stock_ack",
    "catalog_product_list",
    "catalog_navigation_groups",
    "catalog_navigation_group_products",
    "catalog_navigation_top_products_fallback",
    "order_slot_prompt",
    "checkout_slot_prompt",
})

_HEALTH_PROTECTED_TOPICS = frozenset({
    "health_advisory_product_safety",
})

_NON_HEALTH_CHANNEL_TOPICS = frozenset({
    "cold_shipping_inquiry",
    "shipping_inquiry",
    "storefront_self_checkout",
})

_MEDICAL_MARKERS = (
    "الصحه العامه",
    "الصحة العامة",
    "للصحة",
    "الصدر",
    "الجهاز الهضمي",
    "للاطفال",
    "للأطفال",
    "المناعه",
    "المناعة",
    "فوائد كبيره",
    "فوائد كبيرة",
    "علاج",
    "النشاط",
    "صحي",
)

_COMPARISON_MARKERS = (
    "اقل حلاوه",
    "أقل حلاوة",
    "اقل حلى",
    "أقل حلى",
    "احلى",
    "أحلى",
    "اخف",
    "أخف",
    "الطف",
    "ألطف",
    "اقوى",
    "أقوى",
    "غني بالطعم",
    "اكثر حلاوه",
    "أكثر حلاوة",
)

_BEST_PICK_MARKERS = (
    "الافضل",
    "الأفضل",
    "ازين نوع",
    "أزين نوع",
    "best",
)

_RECOMMENDATION_MARKERS = (
    "تبي",
    "تحب",
    "ارسل",
    "أرسل",
    "تفاصيل",
    "يناسبك",
    "الخيار الافضل",
    "الخيار الأفضل",
    "انصح",
    "أنصح",
    "ارشح",
    "أرشح",
    "اجيب",
    "أجيب",
)


def product_claim_grounding_guard_mode() -> str:
    mode = os.environ.get(
        "NAHLA_PRODUCT_CLAIM_GROUNDING_GUARD_MODE", "enforce",
    ).strip().lower()
    if mode in ("off", "shadow", "enforce"):
        return mode
    return "enforce"


@dataclass(frozen=True)
class ProductClaimGroundingGuardResult:
    reply: str
    action: str
    replaced: bool = False
    reason: str = ""
    blocked_claims: tuple[str, ...] = ()
    shadow_mode: bool = False
    would_rewrite: bool = False
    stripped: bool = False
    scrubbed_empty: bool = False
    requires_grounded_recompose: bool = False


def _is_general_category_browse_turn(
    inbound_metadata: Optional[Dict[str, Any]],
    *,
    db: Any = None,
    tenant_id: Optional[int] = None,
) -> bool:
    meta = dict(inbound_metadata or {})
    if meta.get("specific_product"):
        return False
    if meta.get("category_browse") and not meta.get("specific_product"):
        return True

    text = str(meta.get("inbound_text") or meta.get("message") or "").strip()
    if not text:
        return False

    try:
        from modules.ai.brain.commerce.commerce_browse_category_guard import (  # noqa: PLC0415
            active_category_from_state,
            extract_browse_category_scope,
        )
        from modules.ai.brain.catalog.catalog_browse_scope_resolver import (  # noqa: PLC0415
            active_catalog_group_slug_from_state,
            resolve_catalog_category_scope,
        )
    except Exception:  # noqa: BLE001
        return False

    subject = extract_browse_category_scope(text, "")
    if not subject:
        return False

    try:
        from modules.ai.brain.product_discovery_gate import (  # noqa: PLC0415
            _SKU_SPECIFICITY_RE,
            is_generic_category_noun,
        )

        if _SKU_SPECIFICITY_RE.search(text):
            return False
        words = [w for w in text.split() if w.strip()]
        if len(words) > 4:
            return False
        if not is_generic_category_noun(subject):
            return False
    except Exception:  # noqa: BLE001
        return False

    if db is not None and tenant_id is not None:
        scope = resolve_catalog_category_scope(
            db,
            int(tenant_id),
            text,
            subject,
            active_group_slug=active_catalog_group_slug_from_state(
                meta.get("brain_state"),
            ),
            active_category=str(meta.get("active_category") or ""),
        )
        return scope.must_filter_by_category and not scope.specific_product

    return True


def _filter_violations_for_category_browse(
    violations: List[tuple[str, str]],
    *,
    category_browse: bool,
) -> List[tuple[str, str]]:
    if not category_browse:
        return violations
    return [
        v for v in violations
        if v[0] not in {
            "ungrounded_price",
            "unavailable_promoted",
            "ungrounded_best_pick",
        }
    ]


def _find_markers(text: str, markers: Sequence[str]) -> List[str]:
    found: List[str] = []
    norm = _norm(text)
    for marker in markers:
        m = _norm(marker)
        if m and m in norm:
            found.append(marker)
    return found


def _decision_topic(inbound_metadata: Optional[Dict[str, Any]]) -> str:
    meta = dict(inbound_metadata or {})
    return str(meta.get("decision_topic") or meta.get("topic") or "").strip()


def _is_health_protected_turn(inbound_metadata: Optional[Dict[str, Any]]) -> bool:
    contract = get_turn_owner_contract(inbound_metadata=inbound_metadata)
    if contract is not None and contract.topic in _HEALTH_PROTECTED_TOPICS:
        return True
    return _decision_topic(inbound_metadata) in _HEALTH_PROTECTED_TOPICS


def _is_non_health_channel_turn(inbound_metadata: Optional[Dict[str, Any]]) -> bool:
    contract = get_turn_owner_contract(inbound_metadata=inbound_metadata)
    if contract is not None:
        return (
            contract.topic in _NON_HEALTH_CHANNEL_TOPICS
            or contract.block_product_benefit_rewrite
            or contract.block_medical_claim_rewrite
            or contract.blocks(POSTPROCESS_PRODUCT_BENEFIT_REWRITE)
            or contract.blocks(POSTPROCESS_MEDICAL_CLAIM_REWRITE)
        )
    return _decision_topic(inbound_metadata) in _NON_HEALTH_CHANNEL_TOPICS


def _filter_topic_scoped_violations(
    violations: List[tuple[str, str]],
    *,
    inbound_metadata: Optional[Dict[str, Any]],
) -> List[tuple[str, str]]:
    if not violations:
        return violations
    if _is_non_health_channel_turn(inbound_metadata):
        return [v for v in violations if v[0] != "ungrounded_medical"]
    return violations


def _claim_grounded_in_corpus(claim_marker: str, corpus: str) -> bool:
    norm_claim = _norm(claim_marker)
    if not norm_claim or not corpus:
        return False
    if norm_claim in corpus:
        return True
    tokens = [t for t in norm_claim.split() if len(t) >= 3]
    if not tokens:
        return False
    hits = sum(1 for t in tokens if t in corpus)
    return hits >= max(1, len(tokens) - 1)


def _ungrounded_prices(
    reply: str,
    evidence: ProductClaimGroundingEvidence,
    *,
    customer_claimed: Optional[Set[int]] = None,
) -> List[int]:
    prices = extract_reply_prices(reply)
    if not prices:
        return []

    thread_unreliable = (
        evidence.catalog_miss_this_turn
        or evidence.recent_catalog_miss
        or evidence.recent_no_synced
    )
    if thread_unreliable and not evidence.catalog_products_this_turn:
        if customer_claimed:
            return sorted(p for p in prices if p not in customer_claimed)
        return sorted(prices)

    claimed = customer_claimed or set()
    missing = [
        p for p in prices
        if p not in evidence.grounded_prices and p not in claimed
    ]
    return missing


def _unavailable_product_promoted(reply: str, evidence: ProductClaimGroundingEvidence) -> List[str]:
    promoted: List[str] = []
    norm = _norm(reply)
    rec_markers = [_norm(m) for m in _RECOMMENDATION_MARKERS]
    best_markers = [_norm(m) for m in _BEST_PICK_MARKERS]
    for row in evidence.unavailable_products:
        title = str(row.get("title") or "")
        if not title or not _text_references_product(reply, title):
            continue
        if any(m in norm for m in rec_markers + best_markers):
            promoted.append(title)
            continue
        if "غير متوفر" in norm or "غير متاح" in norm:
            continue
        if _find_markers(reply, _COMPARISON_MARKERS):
            promoted.append(title)
    return promoted


def _detect_violations(
    reply: str,
    evidence: ProductClaimGroundingEvidence,
    *,
    customer_claimed: Optional[Set[int]] = None,
) -> List[tuple[str, str]]:
    """Return list of (violation_kind, detail)."""
    violations: List[tuple[str, str]] = []

    ungrounded = _ungrounded_prices(reply, evidence, customer_claimed=customer_claimed)
    if ungrounded:
        violations.append(("ungrounded_price", ",".join(str(p) for p in ungrounded)))

    if (
        (evidence.recent_catalog_miss or evidence.recent_no_synced or evidence.catalog_miss_this_turn)
        and ungrounded
    ):
        violations.append(("contradiction_after_catalog_miss", "prices_after_miss"))

    if (
        evidence.recent_no_synced
        and not evidence.has_checkout_catalog
        and _find_markers(reply, ("ريال", "سعر", "اسعار", "أسعار"))
        and extract_reply_prices(reply)
    ):
        violations.append(("contradiction_no_synced", "prices_after_no_synced"))

    for title in _unavailable_product_promoted(reply, evidence):
        violations.append(("unavailable_promoted", title))

    medical = _find_markers(reply, _MEDICAL_MARKERS)
    for marker in medical:
        if not _claim_grounded_in_corpus(marker, evidence.grounded_text_corpus):
            violations.append(("ungrounded_medical", marker))

    comparison = _find_markers(reply, _COMPARISON_MARKERS)
    for marker in comparison:
        if not _claim_grounded_in_corpus(marker, evidence.grounded_text_corpus):
            violations.append(("ungrounded_comparison", marker))

    best = _find_markers(reply, _BEST_PICK_MARKERS)
    if best and not evidence.catalog_products_this_turn and not evidence.executor_product_ids:
        violations.append(("ungrounded_best_pick", best[0]))

    return violations


def _has_usable_customer_content(text: str) -> bool:
    stripped = str(text or "").strip()
    if not stripped:
        return False
    return bool(_USABLE_CUSTOMER_CONTENT_RE.search(stripped))


def _collect_strip_signals(
    violations: Sequence[tuple[str, str]],
) -> tuple[Set[int], List[str], List[str]]:
    prices: Set[int] = set()
    markers: List[str] = []
    unavailable_titles: List[str] = []
    for kind, detail in violations:
        if kind == "ungrounded_price":
            for part in detail.split(","):
                part = part.strip()
                if not part:
                    continue
                try:
                    prices.add(int(part))
                except ValueError:
                    continue
        elif kind in (
            "ungrounded_comparison",
            "ungrounded_medical",
            "ungrounded_best_pick",
        ):
            if detail:
                markers.append(detail)
        elif kind == "unavailable_promoted" and detail:
            unavailable_titles.append(detail)
    return prices, markers, unavailable_titles


def _chunk_contains_violation(
    chunk: str,
    *,
    prices: Set[int],
    markers: Sequence[str],
    unavailable_titles: Sequence[str],
) -> bool:
    if prices:
        chunk_prices = extract_reply_prices(chunk)
        if chunk_prices & prices:
            return True
    if markers and _find_markers(chunk, markers):
        return True
    for title in unavailable_titles:
        if _text_references_product(chunk, title):
            return True
    return False


def strip_ungrounded_product_claim_sentences(
    reply: str,
    violations: Sequence[tuple[str, str]],
) -> str:
    """Remove sentences/chunks containing unsupported product claims."""
    raw = (reply or "").strip()
    if not raw or not violations:
        return raw

    prices, markers, unavailable_titles = _collect_strip_signals(violations)
    if not prices and not markers and not unavailable_titles:
        return raw

    kept: List[str] = []
    for chunk in re.split(r"(?<=[.!?؟،])\s+|\n+", raw):
        part = chunk.strip().rstrip("،,.")
        if part and not _chunk_contains_violation(
            part,
            prices=prices,
            markers=markers,
            unavailable_titles=unavailable_titles,
        ):
            kept.append(part)
    return " ".join(kept).strip()


def stamp_product_claim_guard_provenance(
    result_data: Dict[str, Any],
    guard_result: ProductClaimGroundingGuardResult,
    *,
    recompose_requested: bool = False,
    recompose_performed: bool = False,
) -> None:
    """Stamp product-claim guard observability on pipeline result.data."""
    if guard_result.blocked_claims:
        result_data["product_claim_blocked"] = True
        result_data["product_claim_blocked_kinds"] = list(guard_result.blocked_claims)
    if guard_result.reason:
        result_data["product_claim_guard_reason"] = guard_result.reason
    result_data["product_claim_stripped"] = bool(
        result_data.get("product_claim_stripped") or guard_result.stripped
    )
    if recompose_requested:
        result_data["product_claim_recompose_requested"] = True
    if recompose_performed:
        result_data["product_claim_recompose_performed"] = True


def resolve_product_claim_second_pass_reply(
    *,
    second_pass: ProductClaimGroundingGuardResult,
    recomposed_reply: str,
    compose_source: str = "",
) -> str:
    """Choose final reply after one grounded recompose and strip-only second pass."""
    if second_pass.replaced or second_pass.stripped:
        if not second_pass.scrubbed_empty:
            return second_pass.reply
        if str(compose_source or "") == "fallback_deterministic":
            return (recomposed_reply or "").strip()
        return second_pass.reply
    return (recomposed_reply or "").strip()


def log_product_claim_grounding_guard(
    *,
    tenant_id: Optional[int],
    conversation_id: Optional[int],
    guard_mode: str,
    action: str,
    reason: str,
    violations: Sequence[str],
    would_rewrite: bool,
) -> None:
    try:
        logger.info(
            "[PRODUCT_CLAIM_GROUNDING_GUARD] tenant_id=%s conversation_id=%s "
            "mode=%s action=%s reason=%s violations=%s would_rewrite=%s",
            tenant_id,
            conversation_id,
            guard_mode or "-",
            action or "-",
            reason or "-",
            "|".join(violations) if violations else "-",
            "YES" if would_rewrite else "NO",
        )
    except Exception:  # noqa: silent-ok — telemetry emit must never raise to caller
        pass


def _product_knowledge_turn_active(
    inbound_metadata: Optional[Dict[str, Any]],
    order_state: Any,
) -> bool:
    meta = dict(inbound_metadata or {})
    topic = str(meta.get("decision_topic") or meta.get("topic") or "")
    if topic == "product_knowledge_facts":
        return True
    try:
        from modules.ai.brain.commerce.product_knowledge_or_comparison import (  # noqa: PLC0415
            get_product_knowledge_session,
        )

        if get_product_knowledge_session(order_state).get("active"):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — session probe is best-effort
        pass
    return False


def _resolve_price_objection_context(
    inbound_metadata: Optional[Dict[str, Any]],
) -> tuple[bool, Set[int], str]:
    meta = dict(inbound_metadata or {})
    inbound_text = str(meta.get("inbound_text") or meta.get("message") or "")
    is_objection = bool(meta.get("price_objection"))
    try:
        from modules.ai.brain.state.price_objection_topic import (  # noqa: PLC0415
            customer_claimed_price_numbers,
            detect_price_objection_topic_shift,
        )

        if not is_objection:
            is_objection = detect_price_objection_topic_shift(inbound_text)
        claimed = customer_claimed_price_numbers(inbound_text) if inbound_text else set()
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional price objection import
        claimed = set()
    return is_objection, claimed, inbound_text


def _is_catalog_product_price_fact_answer_allowed(
    *,
    reply: str,
    chosen_path: str,
    inbound_metadata: Optional[Dict[str, Any]],
    evidence: ProductClaimGroundingEvidence,
) -> bool:
    """Allow grounded catalog fact price answers for narrow price Q&A only."""
    meta = dict(inbound_metadata or {})
    pc = meta.get("persona_compose")
    surface = ""
    if isinstance(pc, dict):
        surface = str(pc.get("surface") or "").strip()
    if surface != "catalog_product_answer":
        return False
    if str(meta.get("question_kind") or "") != "price":
        return False
    if str(meta.get("price_source") or "") != "catalog":
        return False
    if meta.get("checkout_pressure_allowed") is not False:
        return False
    ids = [x for x in (meta.get("catalog_product_ids") or []) if x is not None]
    if not ids:
        return False
    reply_prices = extract_reply_prices(reply)
    if not reply_prices:
        return False
    return all(price in evidence.grounded_prices for price in reply_prices)


def apply_product_claim_grounding_guard(
    *,
    reply: str,
    db: Any = None,
    tenant_id: Optional[int] = None,
    conversation_id: Optional[int] = None,
    availability_context: Optional[Dict[str, Any]] = None,
    executor_products: Optional[Sequence[Dict[str, Any]]] = None,
    catalog_fact_products: Optional[Sequence[Dict[str, Any]]] = None,
    chosen_path: str = "",
    history: Optional[Sequence[Any]] = None,
    order_state: Any = None,
    inbound_metadata: Optional[Dict[str, Any]] = None,
    allow_recompose: bool = True,
) -> ProductClaimGroundingGuardResult:
    mode = product_claim_grounding_guard_mode()
    original = str(reply or "")
    contract = get_turn_owner_contract(inbound_metadata=inbound_metadata)
    meta = dict(inbound_metadata or {})
    if meta.get("product_claim_recompose_performed"):
        allow_recompose = False

    if mode == "off":
        return ProductClaimGroundingGuardResult(reply=original, action="disabled")

    if not original.strip():
        return ProductClaimGroundingGuardResult(reply=original, action="allowed")

    if _product_knowledge_turn_active(inbound_metadata, order_state):
        return ProductClaimGroundingGuardResult(
            reply=original,
            action="allowed_product_knowledge",
        )

    if _is_health_protected_turn(inbound_metadata):
        return ProductClaimGroundingGuardResult(
            reply=original,
            action="allowed_health_protected_topic",
        )

    path = str(chosen_path or "").strip()
    if path in _DETERMINISTIC_ALLOW_PATHS:
        return ProductClaimGroundingGuardResult(reply=original, action="allowed")

    try:
        from modules.ai.brain.current_turn_social_non_commerce import (  # noqa: PLC0415
            resolve_current_turn_social_non_commerce,
        )

        meta = dict(inbound_metadata or {})
        inbound_text = str(meta.get("inbound_text") or meta.get("message") or "")
        intent_obj = meta.get("intent")
        if intent_obj is None and inbound_text:
            try:
                from modules.ai.brain.intent import rules as intent_rules  # noqa: PLC0415

                intent_obj = intent_rules.match(inbound_text)
            except Exception:  # noqa: BLE001
                intent_obj = None
        last_question = str(getattr(order_state, "last_question_asked", "") or "")
        current_turn = resolve_current_turn_social_non_commerce(
            inbound_text,
            intent=intent_obj,
            state=order_state,
            inbound_metadata=inbound_metadata,
            last_question=last_question,
        )
        if current_turn.matched:
            logger.info(
                "[PRODUCT_CLAIM_GROUNDING_GUARD] allow_social_noncommerce "
                "tenant=%s conv=%s category=%s reason=%s",
                tenant_id,
                conversation_id,
                current_turn.category or "-",
                current_turn.reason or "-",
            )
            return ProductClaimGroundingGuardResult(
                reply=original,
                action="allowed_social_noncommerce",
                reason=current_turn.reason or "current_turn_social_non_commerce",
            )
    except Exception:  # noqa: BLE001
        logger.exception("[PRODUCT_CLAIM_GROUNDING_GUARD] social_non_commerce_probe_failed")

    try:
        from modules.ai.order_flow_v2.flags import is_v2_checkout_scope_active  # noqa: PLC0415
        from modules.ai.order_flow_v2.state import prep_dict, trusted_catalog_price  # noqa: PLC0415

        prep = prep_dict(getattr(order_state, "order_prep", None) if order_state is not None else {})
        if not prep and isinstance(order_state, dict):
            prep = prep_dict((order_state or {}).get("order_prep"))
        if is_v2_checkout_scope_active(prep) and trusted_catalog_price(prep, {}):
            return ProductClaimGroundingGuardResult(reply=original, action="allowed_v2_trusted_price")
    except Exception:  # noqa: BLE001  # noqa: silent-ok — V2 scope gate must not break grounding guard
        pass

    try:
        evidence = build_product_claim_grounding_evidence(
            db,
            tenant_id,
            availability_context=availability_context,
            executor_products=executor_products,
            catalog_fact_products=catalog_fact_products,
            chosen_path=path,
            history=history,
            order_state=order_state,
            inbound_metadata=inbound_metadata,
            conversation_id=conversation_id,
        )
        if _is_catalog_product_price_fact_answer_allowed(
            reply=original,
            chosen_path=path,
            inbound_metadata=inbound_metadata,
            evidence=evidence,
        ):
            return ProductClaimGroundingGuardResult(
                reply=original,
                action="allowed_catalog_product_price_fact",
            )
        is_price_objection, customer_claimed, _inbound_text = _resolve_price_objection_context(
            inbound_metadata,
        )
        violations = _detect_violations(
            original,
            evidence,
            customer_claimed=customer_claimed if is_price_objection else None,
        )
        violations = _filter_topic_scoped_violations(
            violations,
            inbound_metadata=inbound_metadata,
        )
        category_browse = _is_general_category_browse_turn(
            inbound_metadata,
            db=db,
            tenant_id=tenant_id,
        )
        violations = _filter_violations_for_category_browse(
            violations,
            category_browse=category_browse,
        )
        if (
            is_price_objection
            and evidence.grounded_prices
            and violations
            and all(v[0] == "ungrounded_price" for v in violations)
        ):
            log_product_claim_grounding_guard(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                guard_mode=mode,
                action="allowed_price_objection_customer_claims",
                reason="customer_claimed_prices_only",
                violations=[f"{k}:{d}" for k, d in violations],
                would_rewrite=False,
            )
            return ProductClaimGroundingGuardResult(
                reply=original,
                action="allowed_price_objection_customer_claims",
            )
        if not violations:
            log_product_claim_grounding_guard(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                guard_mode=mode,
                action="allowed",
                reason="grounded_or_no_claim",
                violations=(),
                would_rewrite=False,
            )
            return ProductClaimGroundingGuardResult(reply=original, action="allowed")

        kinds = [v[0] for v in violations]
        reason = kinds[0]

        log_product_claim_grounding_guard(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            guard_mode=mode,
            action="blocked",
            reason=reason,
            violations=[f"{k}:{d}" for k, d in violations],
            would_rewrite=True,
        )

        if mode == "shadow":
            return ProductClaimGroundingGuardResult(
                reply=original,
                action="blocked",
                reason=reason,
                blocked_claims=tuple(kinds),
                shadow_mode=True,
                would_rewrite=True,
            )

        if contract is not None and contract.protected_final_reply:
            return ProductClaimGroundingGuardResult(
                reply=original,
                action="blocked_protected_final_reply",
                reason=reason,
                blocked_claims=tuple(kinds),
                would_rewrite=True,
            )

        stripped = strip_ungrounded_product_claim_sentences(original, violations)
        stripped_content = bool(stripped != original)
        usable = _has_usable_customer_content(stripped)
        scrubbed_empty = not usable
        requires_recompose = bool(scrubbed_empty and allow_recompose)
        action = "stripped_empty" if scrubbed_empty else "stripped"
        return ProductClaimGroundingGuardResult(
            reply=stripped,
            action=action,
            replaced=stripped_content,
            reason=reason,
            blocked_claims=tuple(kinds),
            would_rewrite=True,
            stripped=stripped_content,
            scrubbed_empty=scrubbed_empty,
            requires_grounded_recompose=requires_recompose,
        )
    except Exception:  # noqa: silent-ok — guard failure must not block outbound send
        logger.exception(
            "[PRODUCT_CLAIM_GROUNDING_GUARD] guard failed tenant=%s",
            tenant_id,
        )
        return ProductClaimGroundingGuardResult(reply=original, action="allowed")
