"""
product_claim_grounding_guard.py
────────────────────────────────
Post-compose guard: block ungrounded product claims (prices, taste/medical
comparisons, recommendations of unavailable SKUs, and contradictions after
catalog-miss / no-synced signals).

Modes (NAHLA_PRODUCT_CLAIM_GROUNDING_GUARD_MODE):
  off     — disabled
  shadow  — log only
  enforce — rewrite blocked claims (default)
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Set

from modules.ai.brain.postprocess.product_claim_grounding_evidence import (
    ProductClaimGroundingEvidence,
    build_product_claim_grounding_evidence,
    extract_reply_prices,
    _norm,
    _text_references_product,
)

logger = logging.getLogger("nahla.brain.postprocess.product_claim_grounding_guard")

_SAFE_NO_GROUNDED_PRICE_AR = (
    "ما ظهر عندي سعر مؤكد من الكتالوج الآن. "
    "أقدر أوضح المتوفر حالياً أو أحولك للموظف."
)

_SAFE_NO_GROUNDED_COMPARISON_AR = (
    "يختلف الطعم حسب المرعى والموسم. "
    "أقدر أوضح لك المتوفر عندنا حالياً حسب الكتالوج."
)

_SAFE_UNAVAILABLE_ONLY_AR = (
    "هذا المنتج غير متوفر حالياً. "
    "أقدر أرسل لك الخيارات المتوفرة الآن من الكتالوج."
)

_SAFE_CONTRADICTION_AR = (
    "ما ظهر عندي تطابقاً أو أسعاراً مؤكدة من الكتالوج في هذه اللحظة. "
    "أرسل اسم المنتج كما في المتجر أو اطلب «أكثر مبيعاً»."
)

_SAFE_MEDICAL_CLAIM_AR = (
    "ما عندي وصف موثق من المتجر عن فوائد صحية محددة لهذا المنتج. "
    "أقدر أوضح المتوفر والأسعار المؤكدة."
)

_SAFE_BEST_PICK_NO_SOURCE_AR = (
    "ما عندي ترشيح موثق من الكتالوج لنوع واحد «الأفضل». "
    "أقدر أرسل الخيارات المتوفرة حالياً."
)

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


def _rewrite_for_violations(
    violations: List[tuple[str, str]],
    evidence: ProductClaimGroundingEvidence,
) -> str:
    kinds = {v[0] for v in violations}
    if "contradiction_after_catalog_miss" in kinds or "contradiction_no_synced" in kinds:
        return _SAFE_CONTRADICTION_AR
    if "ungrounded_price" in kinds:
        return _SAFE_NO_GROUNDED_PRICE_AR
    if "unavailable_promoted" in kinds:
        avail_titles = [
            str(p.get("title") or "").strip()
            for p in evidence.available_products[:3]
            if p.get("title")
        ]
        if avail_titles:
            joined = " / ".join(avail_titles)
            return (
                f"{_SAFE_UNAVAILABLE_ONLY_AR}\n"
                f"المتوفر الآن: {joined}."
            )
        return _SAFE_UNAVAILABLE_ONLY_AR
    if "ungrounded_medical" in kinds:
        return _SAFE_MEDICAL_CLAIM_AR
    if "ungrounded_best_pick" in kinds:
        return _SAFE_BEST_PICK_NO_SOURCE_AR
    if "ungrounded_comparison" in kinds:
        return _SAFE_NO_GROUNDED_COMPARISON_AR
    return _SAFE_NO_GROUNDED_COMPARISON_AR


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


def apply_product_claim_grounding_guard(
    *,
    reply: str,
    db: Any = None,
    tenant_id: Optional[int] = None,
    conversation_id: Optional[int] = None,
    availability_context: Optional[Dict[str, Any]] = None,
    executor_products: Optional[Sequence[Dict[str, Any]]] = None,
    chosen_path: str = "",
    history: Optional[Sequence[Any]] = None,
    order_state: Any = None,
    inbound_metadata: Optional[Dict[str, Any]] = None,
) -> ProductClaimGroundingGuardResult:
    mode = product_claim_grounding_guard_mode()
    original = str(reply or "")

    if mode == "off":
        return ProductClaimGroundingGuardResult(reply=original, action="disabled")

    if not original.strip():
        return ProductClaimGroundingGuardResult(reply=original, action="allowed")

    path = str(chosen_path or "").strip()
    if path in _DETERMINISTIC_ALLOW_PATHS:
        return ProductClaimGroundingGuardResult(reply=original, action="allowed")

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
            chosen_path=path,
            history=history,
            order_state=order_state,
            inbound_metadata=inbound_metadata,
            conversation_id=conversation_id,
        )
        is_price_objection, customer_claimed, _inbound_text = _resolve_price_objection_context(
            inbound_metadata,
        )
        violations = _detect_violations(
            original,
            evidence,
            customer_claimed=customer_claimed if is_price_objection else None,
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

        new_reply = _rewrite_for_violations(violations, evidence)
        return ProductClaimGroundingGuardResult(
            reply=new_reply,
            action="blocked",
            replaced=True,
            reason=reason,
            blocked_claims=tuple(kinds),
            would_rewrite=True,
        )
    except Exception:  # noqa: silent-ok — guard failure must not block outbound send
        logger.exception(
            "[PRODUCT_CLAIM_GROUNDING_GUARD] guard failed tenant=%s",
            tenant_id,
        )
        return ProductClaimGroundingGuardResult(reply=original, action="allowed")
