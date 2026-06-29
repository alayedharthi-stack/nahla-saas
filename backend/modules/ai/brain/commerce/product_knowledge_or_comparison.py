"""
product_knowledge_or_comparison.py
──────────────────────────────────
PR-CE4 — product knowledge / comparison ownership.

Deterministic classification and fact bundling for comparison, batch/harvest,
value, and feature questions. Compose stays non-deterministic; operations
(what may be claimed) stay evidence-bound.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger("nahla.brain.product_knowledge")

TOPIC_PRODUCT_KNOWLEDGE_FACTS = "product_knowledge_facts"

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")

_COMPARISON_RE = re.compile(
    r"(?:"
    r"(?:وش|ايش|ما|what)\s*(?:ال)?(?:فرق|اختلاف|difference)"
    r"|(?:ي|ت)?(?:فرق|يختلف|different)\s+عن"
    r"|(?:ال)?(?:فرق|اختلاف)\s+(?:بين|عن|between)"
    r"|compare|comparison"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_FEATURES_RE = re.compile(
    r"(?:"
    r"(?:وش|ما|what)\s*(?:يميز(?:ه|ها)?|مميز(?:ات)?|خصائص|features?)"
    r"|(?:what\s+makes|what\s+distinguishes)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_VALUE_PRICE_RE = re.compile(
    r"(?:"
    r"(?:ليش|لماذا|why)\s*(?:ال)?(?:سعر(?:ه|ها)?|ثمن(?:ه|ها)?|غالي|اغلى|expensive)"
    r"|(?:ليش|why).{0,25}(?:غالي|اغلى|expensive|أغلى)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_BATCH_HARVEST_RE = re.compile(
    r"(?:"
    r"(?:نفس|same)\s+(?:ال)?(?:إنتاج|انتاج|production|batch|دفعة|قطف(?:ة|ه)?)"
    r"|(?:حق|من)\s+(?:ال)?(?:سنة|الموسم|last\s+year|previous\s+year)"
    r"|(?:إنتاج|production)\s+(?:جديد|new)"
    r"|(?:هذا|هذي|هو|هي)\s+(?:نفس|same)\s+"
    r"|before\s+(?:year|season|last)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_SEASON_RE = re.compile(
    r"(?:موسم|قطف(?:ة|ه)?|harvest|season|دفعة|batch)",
    re.UNICODE | re.IGNORECASE,
)

_NEW_PRODUCTION_RE = re.compile(
    r"(?:إنتاج\s+جديد|new\s+(?:batch|harvest|production))",
    re.UNICODE | re.IGNORECASE,
)

_COMPARISON_REF_RE = re.compile(
    r"(?:"
    r"(?:فرق|يختلف|different)\s+عن\s+(.+?)(?:\?|؟|$)"
    r"|(?:compared?\s+to|vs\.?)\s+(.+?)(?:\?|$)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_FORBIDDEN_CLAIMS = (
    "invented_harvest_year",
    "invented_batch_claim",
    "invented_medical_benefit",
    "catalog_push_during_knowledge_question",
    "staff_contact_without_request",
)

_PRODUCT_KB_KINDS = frozenset({
    "faq",
    "custom",
    "quick_update",
    "product_benefit",
    "product_usage",
    "product_info",
})


class ProductKnowledgeKind(str, Enum):
    COMPARISON = "comparison"
    BATCH = "batch"
    VALUE = "value"
    FEATURES = "features"
    SEASON = "season"
    UNKNOWN = "unknown"


@dataclass
class ProductKnowledgeFactsBundle:
    subject_product: Dict[str, Any] = field(default_factory=dict)
    comparison_reference: str = ""
    allowed_facts: Dict[str, Any] = field(default_factory=dict)
    missing_facts: List[str] = field(default_factory=list)
    kb_sections: List[Dict[str, Any]] = field(default_factory=list)


def _norm(text: str) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text).lower())
    t = _NORM_RE.sub("", t)
    t = (
        t.replace("\u0623", "\u0627")
        .replace("\u0625", "\u0627")
        .replace("\u0622", "\u0627")
        .replace("\u0649", "\u064a")
        .replace("\u0629", "\u0647")
    )
    return _WS_RE.sub(" ", t).strip()


def is_product_knowledge_message(message: str) -> bool:
    return classify_product_knowledge_kind(message) is not None


def classify_product_knowledge_kind(message: str) -> Optional[ProductKnowledgeKind]:
    raw = (message or "").strip()
    if not raw:
        return None

    if _is_price_query_not_knowledge(raw):
        return None

    if _COMPARISON_RE.search(raw):
        return ProductKnowledgeKind.COMPARISON
    if _BATCH_HARVEST_RE.search(raw):
        return ProductKnowledgeKind.BATCH
    if _NEW_PRODUCTION_RE.search(raw):
        return ProductKnowledgeKind.BATCH
    if _FEATURES_RE.search(raw):
        return ProductKnowledgeKind.FEATURES
    if _VALUE_PRICE_RE.search(raw):
        return ProductKnowledgeKind.VALUE
    if _SEASON_RE.search(raw) and re.search(
        r"(?:هل|هو|هي|same|نفس|جديد|new|سنة|year|موسم)",
        _norm(raw),
    ):
        return ProductKnowledgeKind.SEASON

    try:
        from modules.ai.brain.commerce.commerce_entry_catalog_delivery import (  # noqa: PLC0415
            _is_product_knowledge_or_comparison,
        )

        if _is_product_knowledge_or_comparison(raw):
            return ProductKnowledgeKind.COMPARISON
    except Exception:  # noqa: BLE001  # noqa: silent-ok — catalog delivery probe is best-effort
        pass

    return None


def _is_price_query_not_knowledge(message: str) -> bool:
    try:
        from modules.ai.brain.commerce.status_reply_product_context import (  # noqa: PLC0415
            _PRICE_FOLLOWUP_RE,
        )

        if _PRICE_FOLLOWUP_RE.search(message):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok
        pass
    return False


def _should_defer_to_price_objection(message: str) -> bool:
    """Defer to price_objection only for negotiation/competitor — not value explanation."""
    try:
        from modules.ai.brain.state.price_objection_topic import (  # noqa: PLC0415
            _COMPETITOR_SIGNAL_RE,
            detect_price_objection_topic_shift,
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok
        return False

    if not detect_price_objection_topic_shift(message):
        return False

    norm = _norm(message)
    if _VALUE_PRICE_RE.search(message) or _FEATURES_RE.search(message):
        if _COMPETITOR_SIGNAL_RE.search(message):
            return True
        if re.search(
            r"(?:\u062c(?:\u0645\u0644|\u0645\u0644)|\u062e\u0635\u0645|\u062a\u062e\u0641\u064a\u0636|\u0623?\u0631\u062e\u0635|\u0627\u0631\u062e\u0635)",
            norm,
        ):
            return True
        return False

    return True


def extract_comparison_reference(message: str) -> str:
    raw = (message or "").strip()
    m = _COMPARISON_REF_RE.search(raw)
    if not m:
        return ""
    ref = (m.group(1) or m.group(2) or "").strip(" ؟?!.")
    return ref


def _ensure_status_product_focus(ctx: Any) -> None:
    state = getattr(ctx, "state", None)
    if state is None:
        return
    profile = getattr(ctx, "profile", None) or {}
    inbound_meta = dict(profile.get("inbound_metadata") or {})
    try:
        from modules.ai.brain.commerce.status_reply_product_context import (  # noqa: PLC0415
            apply_status_reply_product_context_to_state,
            get_persisted_status_reply_context,
            is_status_reply_inbound,
        )

        if not is_status_reply_inbound(inbound_meta) and not get_persisted_status_reply_context(
            state,
        ).get("active"):
            return
        apply_status_reply_product_context_to_state(
            db=getattr(ctx, "_db", None),
            tenant_id=int(getattr(ctx, "tenant_id", 0) or 0),
            message=str(getattr(ctx, "message", "") or ""),
            state=state,
            inbound_metadata=inbound_meta,
        )
    except Exception as exc:  # noqa: BLE001  # noqa: silent-ok — status focus ensure is best-effort
        logger.debug("[PRODUCT_KNOWLEDGE] status focus ensure failed err=%s", exc)


def resolve_subject_product(ctx: Any, message: str) -> Dict[str, Any]:
    _ensure_status_product_focus(ctx)
    state = getattr(ctx, "state", None)
    focus = dict(getattr(state, "current_product_focus", None) or {})
    if focus.get("title") or focus.get("id"):
        try:
            from modules.ai.brain.commerce.commerce_entry_orchestrator import (  # noqa: PLC0415
                enrich_product_focus_from_catalog,
            )

            enriched = enrich_product_focus_from_catalog(
                getattr(ctx, "_db", None),
                int(getattr(ctx, "tenant_id", 0) or 0),
                focus,
            )
            if enriched:
                state.current_product_focus = enriched
                focus = enriched
        except Exception:  # noqa: BLE001  # noqa: silent-ok — catalog enrich is best-effort
            pass
        return focus

    try:
        from modules.ai.brain.commerce.status_reply_product_context import (  # noqa: PLC0415
            get_persisted_status_reply_context,
        )

        session = get_persisted_status_reply_context(state)
        if session.get("product_title"):
            return {
                "title": str(session.get("product_title") or ""),
                "id": session.get("product_id"),
                "from_status_reply": True,
            }
    except Exception:  # noqa: BLE001  # noqa: silent-ok
        pass

    ref = extract_comparison_reference(message)
    if ref:
        return {"title_hint_from_message": ref, "resolved": False}

    return {}


def _subject_tokens(text: str) -> List[str]:
    norm = _norm(text)
    if not norm:
        return []
    return [t for t in norm.split() if len(t) >= 3]


def _score_kb_section(*, title: str, body: str, subject: str) -> float:
    subj_tokens = set(_subject_tokens(subject))
    if not subj_tokens:
        return 0.0
    hay = _norm(f"{title}\n{body}")
    hits = sum(1 for t in subj_tokens if t in hay)
    if hits == 0:
        return 0.0
    return min(1.0, hits / max(1, len(subj_tokens)) + (0.2 if hits >= 2 else 0.0))


def _retrieve_product_kb_sections(
    db: Any,
    tenant_id: int,
    *,
    subject: str,
    message: str,
    limit: int = 4,
) -> List[Dict[str, Any]]:
    if not db or not tenant_id:
        return []
    probe = " ".join(x for x in (subject, message) if x).strip()
    if len(_norm(probe)) < 2:
        return []

    try:
        from core.knowledge import apply_ai_visible_kb_query_filters  # noqa: PLC0415
        from models import MerchantKnowledgeSection  # noqa: PLC0415
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional KB models in tests
        return []

    try:
        rows = (
            apply_ai_visible_kb_query_filters(db.query(MerchantKnowledgeSection))
            .filter(
                MerchantKnowledgeSection.tenant_id == int(tenant_id),
                MerchantKnowledgeSection.kind.in_(tuple(_PRODUCT_KB_KINDS)),
            )
            .order_by(
                MerchantKnowledgeSection.priority.asc(),
                MerchantKnowledgeSection.updated_at.desc(),
            )
            .limit(120)
            .all()
        )
    except Exception as exc:  # noqa: BLE001  # noqa: silent-ok — KB query is best-effort
        logger.debug("[PRODUCT_KNOWLEDGE] KB query failed tenant=%s err=%s", tenant_id, exc)
        return []

    scored: List[tuple[float, Dict[str, Any]]] = []
    for row in rows:
        title = str(getattr(row, "title", "") or "").strip()
        body = str(getattr(row, "body", "") or "").strip()
        if not title and not body:
            continue
        score = _score_kb_section(title=title, body=body, subject=subject or probe)
        if score < 0.35:
            continue
        scored.append(
            (
                score,
                {
                    "section_id": getattr(row, "id", None),
                    "title": title,
                    "body": body[:800],
                    "kind": str(getattr(row, "kind", "") or ""),
                    "match_score": round(score, 3),
                },
            )
        )
    scored.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in scored[:limit]]


def gather_product_knowledge_facts(
    ctx: Any,
    *,
    subject_product: Dict[str, Any],
    question_kind: ProductKnowledgeKind,
    message: str,
) -> ProductKnowledgeFactsBundle:
    subject_title = str(
        subject_product.get("title")
        or subject_product.get("title_hint_from_message")
        or ""
    ).strip()
    comparison_ref = extract_comparison_reference(message)

    allowed: Dict[str, Any] = {}
    missing: List[str] = []

    if subject_product.get("id"):
        allowed["product_id"] = subject_product.get("id")
    if subject_title:
        allowed["product_title"] = subject_title
    if subject_product.get("price") is not None:
        allowed["catalog_price"] = subject_product.get("price")
    if subject_product.get("description"):
        allowed["catalog_description"] = subject_product.get("description")
    elif subject_product.get("body"):
        allowed["catalog_description"] = subject_product.get("body")

    variants = list(subject_product.get("variants") or [])
    if variants:
        allowed["catalog_variants"] = [
            {
                "label": v.get("option_summary") or v.get("name") or "",
                "price": v.get("price"),
            }
            for v in variants[:8]
            if isinstance(v, dict)
        ]

    if comparison_ref:
        allowed["comparison_reference_text"] = comparison_ref

    kb_sections = _retrieve_product_kb_sections(
        getattr(ctx, "_db", None),
        int(getattr(ctx, "tenant_id", 0) or 0),
        subject=subject_title or comparison_ref,
        message=message,
    )
    if kb_sections:
        allowed["kb_sections"] = kb_sections
    elif question_kind in {
        ProductKnowledgeKind.COMPARISON,
        ProductKnowledgeKind.BATCH,
        ProductKnowledgeKind.SEASON,
    }:
        missing.append("kb_product_facts")

    if not subject_title and not subject_product.get("id"):
        missing.append("subject_product")

    if question_kind == ProductKnowledgeKind.BATCH and not any(
        k in allowed for k in ("kb_sections", "catalog_description")
    ):
        missing.append("batch_or_harvest_year")

    if question_kind == ProductKnowledgeKind.VALUE and not any(
        k in allowed for k in ("kb_sections", "catalog_description", "catalog_price")
    ):
        missing.append("value_explanation_facts")

    if question_kind == ProductKnowledgeKind.COMPARISON and not kb_sections:
        missing.append("comparison_facts")

    return ProductKnowledgeFactsBundle(
        subject_product=dict(subject_product),
        comparison_reference=comparison_ref,
        allowed_facts=allowed,
        missing_facts=missing,
        kb_sections=kb_sections,
    )


def compose_product_knowledge_response_goal(
    *,
    question_kind: ProductKnowledgeKind,
    bundle: ProductKnowledgeFactsBundle,
) -> str:
    parts = [
        "PRODUCT KNOWLEDGE compose principles: answer from allowed_facts only; "
        "natural concise Saudi Arabic; no rigid templates; no catalog browse push; "
        "no staff phone/contact unless customer explicitly asked for contact; "
        "never say تبي رقمهم or offer staff numbers unprompted.",
        f"question_kind={question_kind.value}",
    ]
    if bundle.subject_product.get("title"):
        parts.append(f"subject_product={bundle.subject_product.get('title')}")
    if bundle.comparison_reference:
        parts.append(f"comparison_reference={bundle.comparison_reference}")
    if bundle.allowed_facts:
        parts.append(f"allowed_facts_keys={','.join(sorted(bundle.allowed_facts.keys()))}")
    if bundle.missing_facts:
        parts.append(
            "missing_facts="
            + ",".join(bundle.missing_facts)
            + " — do not invent; acknowledge gap briefly and ask ONE clarifying "
            "question or state what is not confirmed in evidence."
        )
    parts.append(
        "forbidden: invented harvest year, invented batch, invented medical benefit, "
        "catalog push, unprompted staff contact."
    )
    if bundle.kb_sections:
        parts.append(
            "KB sections are authoritative for product story/comparison when present; "
            "do not expand beyond KB + catalog allowed_facts."
        )
    return " | ".join(parts)


def try_product_knowledge_decision(ctx: Any) -> Optional[Any]:
    from modules.ai.brain.decision.actions import ACTION_LLM_REPLY  # noqa: PLC0415
    from modules.ai.brain.types import Decision  # noqa: PLC0415

    message = str(getattr(ctx, "message", "") or "").strip()
    if not message:
        return None

    kind = classify_product_knowledge_kind(message)
    if kind is None:
        return None

    if kind == ProductKnowledgeKind.VALUE and _should_defer_to_price_objection(message):
        return None

    subject = resolve_subject_product(ctx, message)
    bundle = gather_product_knowledge_facts(
        ctx,
        subject_product=subject,
        question_kind=kind,
        message=message,
    )

    logger.info(
        "[PRODUCT_KNOWLEDGE] tenant=%s kind=%s subject=%r missing=%s kb=%s",
        getattr(ctx, "tenant_id", None),
        kind.value,
        subject.get("title") or subject.get("title_hint_from_message") or "-",
        bundle.missing_facts,
        len(bundle.kb_sections),
    )

    return Decision(
        action=ACTION_LLM_REPLY,
        args={
            "topic": TOPIC_PRODUCT_KNOWLEDGE_FACTS,
            "question_kind": kind.value,
            "subject_product": dict(bundle.subject_product),
            "comparison_reference": bundle.comparison_reference,
            "allowed_facts": dict(bundle.allowed_facts),
            "missing_facts": list(bundle.missing_facts),
            "forbidden_claims": list(_FORBIDDEN_CLAIMS),
            "block_catalog_escalation": True,
            "block_commerce_escalation": True,
            "block_staff_contact": True,
            "customer_action": "knowledge",
            "response_goal": compose_product_knowledge_response_goal(
                question_kind=kind,
                bundle=bundle,
            ),
        },
        reason=f"product_knowledge — {kind.value}",
        confidence=0.93,
    )


__all__ = [
    "TOPIC_PRODUCT_KNOWLEDGE_FACTS",
    "ProductKnowledgeKind",
    "ProductKnowledgeFactsBundle",
    "classify_product_knowledge_kind",
    "compose_product_knowledge_response_goal",
    "extract_comparison_reference",
    "gather_product_knowledge_facts",
    "is_product_knowledge_message",
    "resolve_subject_product",
    "try_product_knowledge_decision",
]
