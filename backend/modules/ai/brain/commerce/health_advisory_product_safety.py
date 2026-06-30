"""
health_advisory_product_safety.py
─────────────────────────────────
PR-Health-Advisory — sensitive health/product-safety ownership.

Deterministic detection of child/medical/diagnostic health inquiries.
Pauses order-slot collection, blocks staff/showroom/catalog escalation,
and supplies KB-grounded allowed_facts + forbidden medical claims for compose.
"""
from __future__ import annotations

import logging
import re
import time
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger("nahla.brain.health_advisory_product_safety")

TOPIC_HEALTH_ADVISORY = "health_advisory_product_safety"

_SESSION_KEY = "health_advisory_context"
_PENDING_KEY = "health_advisory_active"

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")

_HEALTH_KB_KINDS = frozenset({
    "cold_shipping",
    "health_advisory",
    "product_benefits",
    "faq",
    "shipping_policy",
})

_FORBIDDEN_CLAIMS = (
    "treats_autism",
    "improves_speech_delay",
    "detoxes_heavy_metals",
    "cures_constipation",
    "medical_dosage_for_children",
    "replace_doctor_advice",
    "guaranteed_health_result",
    "therapy_mix_instruction",
    "invented_medical_benefit",
    "catalog_push_during_health",
    "staff_contact_during_health",
    "showroom_location_during_health",
    "order_quantity_before_health_ack",
)

_CHILD_RE = re.compile(
    r"(?:"
    r"اطفال|الاطفال|أطفال|الأطفال|طفل|طفله|طفلة|رضيع|رضع|توأم|توام|"
    r"children|kids|child|baby|babies|toddler"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_SPEECH_DEVELOPMENT_RE = re.compile(
    r"(?:"
    r"ت(?:أ|ا?)خر\s*(?:في\s*)?(?:ال)?(?:نطق|كلام|لغه|لغة|تكلم|تلفظ)|"
    r"speech\s*delay|does\s*not\s*speak"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_AUTISM_RE = re.compile(
    r"(?:"
    r"طيف\s*توحد|توحد|autism|asd|"
    r"development(?:al)?\s*disorder"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_GUT_RE = re.compile(
    r"(?:"
    r"امعاء|الامعاء|أمعاء|الأمعاء|قولون|المعد(?:ه|ة)|"
    r"بكتيريا\s*نافع(?:ه|ة)|microbiome|gut|"
    r"براز|البراز|امساك|إمساك|constipation|stool"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_HEAVY_METAL_RE = re.compile(
    r"(?:"
    r"معادن\s*ثقيل(?:ه|ة)|heavy\s*metal|detox|تحاليل|رنين|"
    r"استشاري|specialist|diagnos"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_MEDICAL_TREATMENT_ASK_RE = re.compile(
    r"(?:"
    r"يعالج|تعالج|علاج|يشفي|يشفى|detox|prescri|جر(?:عه|عة)|"
    r"dosage|جرعات|ينفع\s*ل(?:ل)?|يفيد\s*ل(?:ل)?|"
    r"helps?\s*(?:with|for)|treats?"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_HEALTH_ADVICE_ASK_RE = re.compile(
    r"(?:"
    r"تنصحن|تنصح|ترشح|ترشد|وش\s+تنصح|ايش\s+تنصح|"
    r"ف(?:ي)?اش\s+تنصح|حسب\s+خبرت|recommend|advise"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_MIX_THERAPY_RE = re.compile(
    r"(?:"
    r"اخلط|أخلط|اخلطه|امزج|امزجه|combine|mix\s+with"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_BOT_CHALLENGE_RE = re.compile(
    r"(?:"
    r"رد\s*آلي|رد\s*الي|bot|robot|"
    r"مدري\s+انت\s+اللي\s+يرد|هل\s+انت\s+(?:بشر|انسان|آلي|الي)|"
    r"automated|automatic\s+reply"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_ROYAL_JELLY_FOLLOWUP_RE = re.compile(
    r"(?:"
    r"غذ(?:اء|يه)\s*(?:ال)?(?:ملكات|ملكه|ملكة)|royal\s*jelly|"
    r"ملكات\s*النحل"
    r")",
    re.UNICODE | re.IGNORECASE,
)


class HealthQuestionKind(str, Enum):
    HEALTH_PRODUCT_ADVICE = "health_product_advice"
    BOT_AUTHENTICITY_CHALLENGE = "bot_authenticity_challenge"
    THERAPY_MIX_FOLLOWUP = "therapy_mix_followup"
    HEALTH_FOLLOWUP = "health_followup"


@dataclass(frozen=True)
class HealthAdvisoryEvidence:
    matched: bool
    question_kind: str = HealthQuestionKind.HEALTH_PRODUCT_ADVICE.value
    sensitive_context: Dict[str, bool] = field(default_factory=dict)
    signal_count: int = 0
    reasons: tuple[str, ...] = ()


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


def _signal_flags(message: str) -> Dict[str, bool]:
    raw = message or ""
    norm = _norm(raw)
    return {
        "children": bool(_CHILD_RE.search(norm)),
        "speech_delay": bool(_SPEECH_DEVELOPMENT_RE.search(norm)),
        "autism_or_development": bool(_AUTISM_RE.search(norm)),
        "gut_constipation": bool(_GUT_RE.search(norm)),
        "heavy_metals_or_diagnostics": bool(_HEAVY_METAL_RE.search(norm)),
        "medical_treatment_ask": bool(_MEDICAL_TREATMENT_ASK_RE.search(norm)),
        "health_advice_ask": bool(_HEALTH_ADVICE_ASK_RE.search(norm)),
        "therapy_mix": bool(_MIX_THERAPY_RE.search(norm)),
        "royal_jelly": bool(_ROYAL_JELLY_FOLLOWUP_RE.search(norm)),
    }


def get_health_advisory_context(state: Any) -> Optional[Dict[str, Any]]:
    session = dict(getattr(state, "commerce_session", None) or {})
    ctx = session.get(_SESSION_KEY)
    if isinstance(ctx, dict) and ctx.get("active"):
        return dict(ctx)
    pending = session.get(_PENDING_KEY)
    if isinstance(pending, dict) and pending.get("active"):
        return dict(pending)
    return None


def has_active_health_advisory_context(state: Any) -> bool:
    return get_health_advisory_context(state) is not None


def pin_health_advisory_context(
    state: Any,
    *,
    evidence: HealthAdvisoryEvidence,
    source: str,
) -> None:
    if state is None:
        return
    session = dict(getattr(state, "commerce_session", None) or {})
    payload = {
        "active": True,
        "type": "health_advisory_product_safety",
        "source": str(source or "health_inquiry"),
        "question_kind": evidence.question_kind,
        "sensitive_context": dict(evidence.sensitive_context),
        "created_at": time.time(),
    }
    session[_SESSION_KEY] = payload
    session[_PENDING_KEY] = payload
    state.commerce_session = session
    if str(getattr(state, "last_question_asked", "") or "").strip():
        state.last_question_asked = ""
        state.last_question_answered = True


def clear_health_advisory_context(state: Any) -> None:
    if state is None:
        return
    session = dict(getattr(state, "commerce_session", None) or {})
    session.pop(_SESSION_KEY, None)
    session.pop(_PENDING_KEY, None)
    state.commerce_session = session


def classify_health_advisory(message: str, *, state: Any = None) -> HealthAdvisoryEvidence:
    raw = (message or "").strip()
    if not raw:
        return HealthAdvisoryEvidence(matched=False)

    flags = _signal_flags(raw)
    reasons: List[str] = [k for k, v in flags.items() if v]
    signal_count = len(reasons)

    active = get_health_advisory_context(state)
    bot_challenge = bool(_BOT_CHALLENGE_RE.search(_norm(raw)))
    if bot_challenge and active:
        return HealthAdvisoryEvidence(
            matched=True,
            question_kind=HealthQuestionKind.BOT_AUTHENTICITY_CHALLENGE.value,
            sensitive_context=dict((active or {}).get("sensitive_context") or flags),
            signal_count=signal_count,
            reasons=tuple(reasons + ["bot_challenge_with_health_context"]),
        )

    if active and (
        flags.get("therapy_mix")
        or flags.get("royal_jelly")
        or flags.get("health_advice_ask")
        or flags.get("medical_treatment_ask")
        or bot_challenge
    ):
        return HealthAdvisoryEvidence(
            matched=True,
            question_kind=(
                HealthQuestionKind.THERAPY_MIX_FOLLOWUP.value
                if flags.get("therapy_mix")
                else HealthQuestionKind.HEALTH_FOLLOWUP.value
            ),
            sensitive_context=dict((active or {}).get("sensitive_context") or flags),
            signal_count=signal_count,
            reasons=tuple(reasons + ["active_health_context_follow_up"]),
        )

    children = flags["children"]
    diagnosis = flags["autism_or_development"] or flags["speech_delay"]
    gut = flags["gut_constipation"]
    heavy = flags["heavy_metals_or_diagnostics"]
    treatment = flags["medical_treatment_ask"]
    advice = flags["health_advice_ask"]

    sensitive = {
        "children": children,
        "diagnosis_or_possible_diagnosis": diagnosis or heavy,
        "autism_or_development": flags["autism_or_development"],
        "speech_delay": flags["speech_delay"],
        "gut_constipation": gut,
        "heavy_metals_or_diagnostics": heavy,
    }

    strong = (
        (children and (diagnosis or gut or heavy or treatment))
        or (children and advice and (gut or diagnosis))
        or (treatment and (diagnosis or gut or children))
        or (flags["autism_or_development"] and (advice or gut or children))
    )
    moderate = signal_count >= 3 and (children or diagnosis or gut)

    if not strong and not moderate:
        return HealthAdvisoryEvidence(matched=False)

    kind = HealthQuestionKind.HEALTH_PRODUCT_ADVICE.value
    if flags["therapy_mix"]:
        kind = HealthQuestionKind.THERAPY_MIX_FOLLOWUP.value

    return HealthAdvisoryEvidence(
        matched=True,
        question_kind=kind,
        sensitive_context=sensitive,
        signal_count=signal_count,
        reasons=tuple(reasons),
    )


def should_defer_non_health_routes(message: str, *, state: Any = None) -> bool:
    """True when branch/contact/identity routes must yield to health owner."""
    ev = classify_health_advisory(message, state=state)
    return ev.matched or has_active_health_advisory_context(state)


def _hard_defer_health(ctx: Any) -> bool:
    message = str(getattr(ctx, "message", "") or "").strip()
    try:
        from modules.ai.brain.commerce.payment_evidence_turn_route import (  # noqa: PLC0415
            current_turn_has_payment_evidence,
        )

        if current_turn_has_payment_evidence(ctx):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok
        pass

    if not message:
        return True

    try:
        from modules.ai.brain.commerce.commerce_entry_catalog_delivery import (  # noqa: PLC0415
            _is_explicit_catalog_browse_request,
        )

        if _is_explicit_catalog_browse_request(message, ctx):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok
        pass

    try:
        from modules.ai.brain.commerce.product_knowledge_or_comparison import (  # noqa: PLC0415
            classify_product_knowledge_kind,
        )

        pk = classify_product_knowledge_kind(message)
        if pk is not None and not classify_health_advisory(message).matched:
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok
        pass

    return False


def _is_simple_commerce_slot_message(message: str) -> bool:
    """City/qty/address only — not health owner."""
    norm = _norm(message)
    if not norm:
        return False
    if classify_health_advisory(message).matched:
        return False
    if re.fullmatch(
        r"(?:جده|جدة|الرياض|الدمام|مكه|مكة|المدين(?:ه|ة)|الطائف|تبوك|ابها|أبها|"
        r"الخبر|القصيم|ينبع|جازان|نجران|حائل|dammam|jeddah|riyadh|madinah|"
        r"makkah|khobar|taif|tabuk|abha|hail|najran|jazan|yanbu|qassim|"
        r"buraidah|arar|jubail)(?:\s|$|[!.؟?])",
        norm,
        flags=re.UNICODE | re.IGNORECASE,
    ):
        return True
    if re.fullmatch(r"(?:\d+|واحد|واحده|كilo|كيلo|كيلو|كيلوين|نص|ربع)(?:\s|$|[!.؟?])", norm):
        return True
    return False


def _retrieve_health_kb(db: Any, tenant_id: int, message: str) -> List[Dict[str, Any]]:
    if not db or not tenant_id:
        return []
    try:
        from models import MerchantKnowledgeSection  # noqa: PLC0415
        from core.knowledge import apply_ai_visible_kb_query_filters  # noqa: PLC0415
    except Exception:  # noqa: BLE001  # noqa: silent-ok
        return []
    norm = _norm(message)
    try:
        rows = (
            apply_ai_visible_kb_query_filters(db.query(MerchantKnowledgeSection))
            .filter(
                MerchantKnowledgeSection.tenant_id == int(tenant_id),
                MerchantKnowledgeSection.kind.in_(tuple(_HEALTH_KB_KINDS)),
            )
            .order_by(
                MerchantKnowledgeSection.priority.asc(),
                MerchantKnowledgeSection.updated_at.desc(),
            )
            .limit(60)
            .all()
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok
        return []

    hits: List[Dict[str, Any]] = []
    for row in rows:
        title = str(getattr(row, "title", "") or "").strip()
        body = str(getattr(row, "body", "") or "").strip()
        if not body:
            continue
        blob = _norm(f"{title} {body}")
        if any(tok in blob for tok in ("امعاء", "املاء", "مبرد", "ملكات", "عسل", "اطفال", "صح")) or (
            "gut" in blob or "health" in blob or "royal" in blob
        ):
            if any(tok in norm for tok in ("امعاء", "املاء", "ملكات", "عسل", "اطفال", "مبرد")) or not norm:
                hits.append({
                    "section_id": int(getattr(row, "id", 0) or 0),
                    "title": title,
                    "body": body,
                    "kind": str(getattr(row, "kind", "") or ""),
                })
    return hits[:3]


def _compose_health_goal(
    *,
    evidence: HealthAdvisoryEvidence,
    kb_sections: Sequence[Dict[str, Any]],
    prior_inadequate_reply: bool = False,
) -> str:
    parts = [
        "HEALTH_ADVISORY compose principles: customer asks health-related product advice "
        "involving children and/or possible medical/developmental conditions. "
        "Respond empathetically and safely in Saudi Arabic. "
        "Do not diagnose, treat, prescribe, or claim honey/royal jelly treats autism, "
        "speech delay, heavy metals, constipation, or gut conditions. "
        "Use only merchant KB/product facts in allowed_facts. "
        "If facts are insufficient, say the store cannot give a medical recommendation "
        "for this condition and advise continuing with the child's doctor/specialist. "
        "If customer still wants food products, explain available products as general food only, "
        "not treatment. Do not continue order quantity/slot collection until health concern "
        "is acknowledged.",
        f"question_kind={evidence.question_kind}",
        f"sensitive_context={evidence.sensitive_context}",
        f"pause_order_slot_collection=true",
    ]
    if kb_sections:
        parts.append(f"kb_sections={len(kb_sections)}")
    else:
        parts.append("kb_sections=0")
    if prior_inadequate_reply or evidence.question_kind == HealthQuestionKind.BOT_AUTHENTICITY_CHALLENGE.value:
        parts.append(
            "prior_reply_inadequate=true | acknowledge prior reply did not fully answer; "
            "explain you are the store's AI assistant; return to health topic safely; "
            "no immediate quantity question"
        )
    if evidence.question_kind == HealthQuestionKind.THERAPY_MIX_FOLLOWUP.value:
        parts.append(
            "therapy_mix_followup=true | do not confirm mixing as treatment for children; "
            "specialist guidance required; offer food-only purchase path if customer wants"
        )
    return " | ".join(parts)


def try_health_advisory_product_safety_decision(ctx: Any) -> Optional[Any]:
    from modules.ai.brain.decision.actions import ACTION_LLM_REPLY  # noqa: PLC0415
    from modules.ai.brain.types import Decision  # noqa: PLC0415

    message = str(getattr(ctx, "message", "") or "").strip()
    state = getattr(ctx, "state", None)
    if not message:
        return None

    if _hard_defer_health(ctx):
        return None

    if _is_simple_commerce_slot_message(message) and not has_active_health_advisory_context(state):
        return None

    evidence = classify_health_advisory(message, state=state)
    if not evidence.matched:
        return None

    pin_health_advisory_context(state, evidence=evidence, source="health_advisory_turn")

    db = getattr(ctx, "_db", None)
    tenant_id = int(getattr(ctx, "tenant_id", 0) or 0)
    kb_sections = _retrieve_health_kb(db, tenant_id, message)

    allowed: Dict[str, Any] = {
        "health_advisory_inquiry": True,
        "question_kind": evidence.question_kind,
        "sensitive_context": dict(evidence.sensitive_context),
    }
    if kb_sections:
        allowed["kb_sections"] = list(kb_sections)

    prior_inadequate = evidence.question_kind == HealthQuestionKind.BOT_AUTHENTICITY_CHALLENGE.value

    logger.info(
        "[HEALTH_ADVISORY] tenant=%s kind=%s signals=%s preview=%r",
        tenant_id,
        evidence.question_kind,
        evidence.reasons,
        message[:80],
    )

    return Decision(
        action=ACTION_LLM_REPLY,
        args={
            "topic": TOPIC_HEALTH_ADVISORY,
            "question_kind": evidence.question_kind,
            "sensitive_context": dict(evidence.sensitive_context),
            "allowed_facts": allowed,
            "forbidden_claims": list(_FORBIDDEN_CLAIMS),
            "pause_order_slot_collection": True,
            "block_staff_contact": True,
            "block_showroom_location": True,
            "block_catalog_push": True,
            "block_commerce_escalation": True,
            "block_whatsapp_quick_order": False,
            "response_goal": _compose_health_goal(
                evidence=evidence,
                kb_sections=kb_sections,
                prior_inadequate_reply=prior_inadequate,
            ),
        },
        reason="health_advisory_product_safety — sensitive health inquiry",
        confidence=0.96,
    )


__all__ = [
    "TOPIC_HEALTH_ADVISORY",
    "HealthAdvisoryEvidence",
    "HealthQuestionKind",
    "classify_health_advisory",
    "clear_health_advisory_context",
    "get_health_advisory_context",
    "has_active_health_advisory_context",
    "pin_health_advisory_context",
    "should_defer_non_health_routes",
    "try_health_advisory_product_safety_decision",
]
