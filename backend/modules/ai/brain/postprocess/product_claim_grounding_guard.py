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
from typing import Any, Dict, List, Optional, Sequence

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


def _ungrounded_prices(reply: str, evidence: ProductClaimGroundingEvidence) -> List[int]:
    prices = extract_reply_prices(reply)
    if not prices:
        return []

    thread_unreliable = (
        evidence.catalog_miss_this_turn
        or evidence.recent_catalog_miss
        or evidence.recent_no_synced
    )
    if thread_unreliable and not evidence.catalog_products_this_turn:
        return sorted(prices)

    missing = [p for p in prices if p not in evidence.grounded_prices]
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
) -> List[tuple[str, str]]:
    """Return list of (violation_kind, detail)."""
    violations: List[tuple[str, str]] = []

    ungrounded = _ungrounded_prices(reply, evidence)
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
        evidence = build_product_claim_grounding_evidence(
            db,
            tenant_id,
            availability_context=availability_context,
            executor_products=executor_products,
            chosen_path=path,
            history=history,
        )
        violations = _detect_violations(original, evidence)
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
