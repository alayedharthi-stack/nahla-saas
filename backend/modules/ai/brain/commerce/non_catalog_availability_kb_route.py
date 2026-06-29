"""
non_catalog_availability_kb_route.py
────────────────────────────────────
Deterministic route owner: non-catalog availability/service inquiries with a
matching KB section → ``kb_availability_facts`` LLM compose (facts only).
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Set

logger = logging.getLogger("nahla.brain.non_catalog_availability_kb_route")

TOPIC_KB_AVAILABILITY_FACTS = "kb_availability_facts"
CHOSEN_PATH_KB_AVAILABILITY_FACTS = "kb_availability_facts"

_AVAIL_KB_KINDS = frozenset({
    "quick_update", "custom", "faq", "product_benefit", "product_usage",
})

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")

_AVAIL_POS = re.compile(
    r"(?:\u0645\u062a\u0648\u0641\u0631|\u0645\u062a\u0627\u062d|available|in\s*stock)",
    re.I,
)
_AVAIL_NEG = re.compile(
    r"(?:\u063a\u064a\u0631\s*\u0645\u062a\u0648\u0641\u0631|\u063a\u064a\u0631\s*\u0645\u062a\u0627\u062d|"
    r"\u0646\u0641\u062f|\u0646\u0641\u0630|unavailable|out\s*of\s*stock)",
    re.I,
)

_SERVICE_SIGNAL_RE = re.compile(
    r"(?:"
    r"\u062d\u062c\u0632|\u062a\u0633\u062c\u064a\u0644|\u0627\u0644\u062d\u062c\u0632|\u0627\u0644\u062a\u0633\u062c\u064a\u0644|"
    r"\u062a\u0648\u0627\u0635\u0644|\u0627\u0644\u062a\u0648\u0627\u0635\u0644|\u062e\u062f\u0645\u0629|\u0627\u0644\u062e\u062f\u0645\u0629|"
    r"booking|reservation|register"
    r")",
    re.I | re.UNICODE,
)

_SUBJECT_STOP_TOKENS = frozenset({
    "هل", "في", "فيه", "عند", "عندكم", "عندك", "لديكم", "لديك",
    "متوفر", "موجود", "available",
})


@dataclass(frozen=True)
class KBAvailabilityHit:
    section_id: int
    title: str
    body: str
    kind: str
    subject: str
    availability_polarity: str
    match_score: float
    match_reason: str


def _norm(text: str) -> str:
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", str(text).strip().lower())
    s = _NORM_RE.sub("", s)
    s = (
        s.replace("\u0623", "\u0627")
        .replace("\u0625", "\u0627")
        .replace("\u0622", "\u0627")
        .replace("\u0649", "\u064a")
        .replace("\u0629", "\u0647")
    )
    return _WS_RE.sub(" ", s).strip()


def _subject_tokens(subject: str) -> Set[str]:
    from modules.ai.knowledge.product_matcher import normalize_arabic, tokenize  # noqa: PLC0415

    return {
        t
        for t in tokenize(normalize_arabic(subject or ""))
        if len(t) >= 2 and t not in _SUBJECT_STOP_TOKENS
    }


def _kb_polarity(title: str, body: str) -> str:
    joined = f"{title}\n{body}"
    # Negative phrases contain «متوفر» — check neg before pos.
    if _AVAIL_NEG.search(joined):
        return "negative"
    if _AVAIL_POS.search(joined):
        return "positive"
    return "unknown"


def _section_has_availability_answer(title: str, body: str) -> bool:
    pol = _kb_polarity(title, body)
    if pol in ("positive", "negative"):
        return True
    combined = f"{title}\n{body}"
    return bool(_SERVICE_SIGNAL_RE.search(combined))


def _score_section_match(
    *,
    title: str,
    body: str,
    subject: str,
) -> tuple[float, str]:
    subj_norm = _norm(subject)
    if not subj_norm:
        return 0.0, "no_subject"

    hay_norm = _norm(f"{title}\n{body}")
    if subj_norm in hay_norm:
        return 1.0, "substring"

    subj_tokens = _subject_tokens(subject)
    if not subj_tokens:
        return 0.0, "no_subject_tokens"

    from modules.ai.knowledge.product_matcher import normalize_arabic, tokenize  # noqa: PLC0415

    title_tokens = set(tokenize(normalize_arabic(title or "")))
    body_tokens = set(tokenize(normalize_arabic(body or "")))
    hay_tokens = title_tokens | body_tokens
    overlap = subj_tokens & hay_tokens

    if not overlap:
        for tok in subj_tokens:
            if len(tok) >= 3 and tok in hay_norm:
                overlap.add(tok)

    if not overlap:
        return 0.0, "no_overlap"

    score = len(overlap) / max(len(subj_tokens), 1)
    if score >= 0.5 or len(overlap) >= 2:
        return min(0.95, 0.55 + score * 0.4), "token_overlap"
    if len(subj_tokens) == 1 and next(iter(subj_tokens)) in hay_norm:
        return 0.75, "single_token"
    return score, "weak_overlap"


def _catalog_skus_from_ctx(ctx: Any) -> List[Dict[str, Any]]:
    facts = getattr(ctx, "facts", None)
    skus = getattr(facts, "catalog_skus", None) if facts is not None else None
    if isinstance(skus, list) and skus:
        return list(skus)
    db = getattr(ctx, "_db", None) or getattr(ctx, "db", None)
    tenant_id = int(getattr(ctx, "tenant_id", 0) or 0)
    if not db or not tenant_id:
        return []
    try:
        from modules.ai.brain.postprocess.availability_context_builder import (  # noqa: PLC0415
            build_availability_context,
        )

        ctx_bundle = build_availability_context(db, tenant_id)
        return list(ctx_bundle.get("catalog_skus") or [])
    except Exception:  # noqa: BLE001
        return []


def _subject_is_established_catalog_product(
    message: str,
    subject: str,
    catalog_skus: Sequence[Dict[str, Any]],
    ctx: Any,
) -> bool:
    if not catalog_skus:
        return False

    from core.product_entity_resolution import resolve_availability_entity  # noqa: PLC0415
    from modules.ai.knowledge.product_matcher import (  # noqa: PLC0415
        CatalogProductForMatch,
        match_products,
        normalize_arabic,
    )

    products = [
        CatalogProductForMatch(
            id=int(p["id"]),
            title=str(p.get("title") or ""),
            sku=p.get("sku"),
            external_id=p.get("external_id"),
        )
        for p in catalog_skus
        if p.get("id") is not None
    ]
    matches = match_products(message or "", products, limit=3, min_confidence=0.5)
    if len(matches) == 1 and matches[0].confidence >= 0.62:
        return True
    if len(matches) >= 2:
        by_id = {int(p["id"]): p for p in catalog_skus if p.get("id") is not None}
        family_keys = {
            str(by_id.get(m.product_id, {}).get("family_key") or "")
            for m in matches
            if m.product_id in by_id
        }
        family_keys.discard("")
        if len(family_keys) == 1:
            return True

    state = getattr(ctx, "state", None)
    focus = getattr(state, "current_product_focus", None) if state else None
    rec_ids = list(getattr(state, "last_recommended_products", None) or []) if state else []

    entity = resolve_availability_entity(
        focus_product=focus if isinstance(focus, dict) else None,
        recommended_product_ids=rec_ids,
        inbound_text=message or "",
        catalog_skus=catalog_skus,
    )
    if not entity.resolved:
        return False

    mode = str(entity.resolution_mode or "")
    if mode in {"focus_id", "inbound_match", "recommended_id", "family"}:
        return True

    if mode == "inbound_family":
        subj_tokens = _subject_tokens(subject)
        if not subj_tokens:
            return False
        by_id = {int(p["id"]): p for p in catalog_skus if p.get("id") is not None}
        member_titles = [
            str(by_id.get(int(pid), {}).get("title") or "")
            for pid in (entity.candidate_product_ids or ())
            if int(pid) in by_id
        ]
        for tok in subj_tokens:
            if not any(tok in normalize_arabic(title) for title in member_titles):
                return False
        return True

    return False


def retrieve_non_catalog_availability_kb_hit(
    db: Any,
    tenant_id: int,
    *,
    subject: str,
    message: str,
) -> Optional[KBAvailabilityHit]:
    if not db or not tenant_id or not (subject or "").strip():
        return None

    try:
        from models import MerchantKnowledgeSection  # noqa: PLC0415
        from core.knowledge import apply_ai_visible_kb_query_filters  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        logger.exception("[NON_CATALOG_AVAILABILITY_KB_ROUTE] kb_route_probe_failed")
        return None

    try:
        rows = (
            apply_ai_visible_kb_query_filters(
                db.query(MerchantKnowledgeSection)
            )
            .filter(
                MerchantKnowledgeSection.tenant_id == int(tenant_id),
                MerchantKnowledgeSection.kind.in_(tuple(_AVAIL_KB_KINDS)),
            )
            .order_by(
                MerchantKnowledgeSection.priority.asc(),
                MerchantKnowledgeSection.updated_at.desc(),
            )
            .limit(120)
            .all()
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[KB_AVAILABILITY_ROUTE] query_failed tenant=%s err=%s",
            tenant_id,
            exc,
        )
        return None

    best: Optional[KBAvailabilityHit] = None
    best_score = 0.0

    for row in rows:
        title = str(getattr(row, "title", "") or "").strip()
        body = str(getattr(row, "body", "") or "").strip()
        if not _section_has_availability_answer(title, body):
            continue

        score, reason = _score_section_match(title=title, body=body, subject=subject)
        if score < 0.5:
            continue

        section_id = int(getattr(row, "id", 0) or 0)
        hit = KBAvailabilityHit(
            section_id=section_id,
            title=title,
            body=body,
            kind=str(getattr(row, "kind", "") or "").strip(),
            subject=subject.strip(),
            availability_polarity=_kb_polarity(title, body),
            match_score=score,
            match_reason=reason,
        )
        if score > best_score:
            best = hit
            best_score = score

    return best


def build_kb_availability_allowed_facts(hit: KBAvailabilityHit) -> Dict[str, Any]:
    return {
        "inquiry_subject": hit.subject,
        "kb_section_id": hit.section_id,
        "kb_section_ids": [hit.section_id],
        "kb_section_title": hit.title,
        "kb_section_body": hit.body,
        "kb_section_kind": hit.kind,
        "availability_polarity": hit.availability_polarity,
        "kb_match_score": hit.match_score,
        "kb_match_reason": hit.match_reason,
    }


def build_kb_availability_forbidden_claims(polarity: str) -> List[str]:
    claims = [
        "catalog_product_list",
        "catalog_card_send",
        "variant_size_followup_without_catalog_evidence",
    ]
    if polarity == "negative":
        claims.extend([
            "positive_availability",
            "motawfir_beed_khiyarat",
        ])
    elif polarity == "unknown":
        claims.append("positive_availability_without_kb_evidence")
    return claims


def compose_kb_availability_facts_goal(args: Optional[Dict[str, Any]] = None) -> str:
    payload = dict(args or {})
    facts = dict(payload.get("allowed_facts") or {})
    polarity = str(payload.get("availability_polarity") or facts.get("availability_polarity") or "unknown")
    forbidden = list(payload.get("forbidden_claims") or [])

    lines = [
        "kb_availability_facts — Customer asks availability or service status for a "
        "non-catalog item. Answer ONLY from KB_AVAILABILITY_FACTS in known_facts.",
        "Compose natural Saudi Arabic from those facts — no rigid templates.",
        "Do NOT send catalog cards, product lists, [PRODUCT: markers, or browse offers.",
        "Do NOT invent availability, prices, or staff contact beyond KB facts.",
    ]
    if polarity == "negative":
        lines.append(
            "KB confirms NOT available — never claim متوفر or suggest catalog alternatives "
            "unless KB facts explicitly mention them."
        )
    elif polarity == "positive":
        lines.append(
            "KB confirms availability — state only what KB facts support; do not expand "
            "into catalog variant lists unless KB mentions them."
        )
    else:
        lines.append(
            "KB does not state clear availability polarity — do NOT claim متوفر; "
            "you may give service/booking guidance from KB or ask one brief clarifier."
        )
    if facts.get("kb_section_title"):
        lines.append(f"KB section title: {facts['kb_section_title']}")
    if facts.get("kb_section_body"):
        body = str(facts["kb_section_body"]).strip()
        if len(body) > 480:
            body = body[:477] + "…"
        lines.append(f"KB facts body: {body}")
    if forbidden:
        lines.append("Forbidden claims: " + ", ".join(forbidden))
    return " | ".join(lines)


def try_non_catalog_availability_kb_decision(
    ctx: Any,
    *,
    route: str = "",
) -> Optional[Any]:
    from modules.ai.brain.commerce.commerce_inquiry_boundary import (  # noqa: PLC0415
        extract_inquiry_subject,
    )
    from modules.ai.brain.commerce.solution_seeking import (  # noqa: PLC0415
        _is_bare_availability_inquiry,
        classify_solution_seeking_commerce,
    )
    from modules.ai.brain.decision.actions import ACTION_LLM_REPLY  # noqa: PLC0415
    from modules.ai.brain.types import Decision  # noqa: PLC0415

    message = (getattr(ctx, "message", None) or "").strip()
    if not message:
        return None

    if classify_solution_seeking_commerce(message):
        return None
    if not _is_bare_availability_inquiry(message):
        return None

    subject = extract_inquiry_subject(message)
    if not subject:
        return None

    tenant_id = int(getattr(ctx, "tenant_id", 0) or 0)
    db = getattr(ctx, "_db", None) or getattr(ctx, "db", None)
    if not db or not tenant_id:
        return None

    catalog_skus = _catalog_skus_from_ctx(ctx)
    if _subject_is_established_catalog_product(message, subject, catalog_skus, ctx):
        return None

    hit = retrieve_non_catalog_availability_kb_hit(
        db,
        tenant_id,
        subject=subject,
        message=message,
    )
    if hit is None:
        return None

    allowed_facts = build_kb_availability_allowed_facts(hit)
    forbidden_claims = build_kb_availability_forbidden_claims(hit.availability_polarity)

    logger.info(
        "[KB_AVAILABILITY_ROUTE] tenant=%s route=%s subject=%r section_id=%s "
        "polarity=%s score=%.2f reason=%s",
        tenant_id,
        route or "-",
        subject[:60],
        hit.section_id,
        hit.availability_polarity,
        hit.match_score,
        hit.match_reason,
    )

    return Decision(
        action=ACTION_LLM_REPLY,
        args={
            "topic": TOPIC_KB_AVAILABILITY_FACTS,
            "chosen_path": CHOSEN_PATH_KB_AVAILABILITY_FACTS,
            "subject": hit.subject,
            "kb_section_ids": [hit.section_id],
            "kb_evidence": allowed_facts,
            "availability_polarity": hit.availability_polarity,
            "allowed_facts": allowed_facts,
            "forbidden_claims": forbidden_claims,
            "block_commerce_escalation": True,
            "response_goal": compose_kb_availability_facts_goal({
                "allowed_facts": allowed_facts,
                "availability_polarity": hit.availability_polarity,
                "forbidden_claims": forbidden_claims,
            }),
        },
        reason=(
            "non-catalog availability inquiry with KB section hit — "
            f"section_id={hit.section_id} polarity={hit.availability_polarity}"
        ),
        confidence=min(0.97, 0.82 + hit.match_score * 0.12),
    )


__all__ = [
    "CHOSEN_PATH_KB_AVAILABILITY_FACTS",
    "KBAvailabilityHit",
    "TOPIC_KB_AVAILABILITY_FACTS",
    "build_kb_availability_allowed_facts",
    "build_kb_availability_forbidden_claims",
    "compose_kb_availability_facts_goal",
    "retrieve_non_catalog_availability_kb_hit",
    "try_non_catalog_availability_kb_decision",
]
