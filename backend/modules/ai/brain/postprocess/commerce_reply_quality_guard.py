"""
commerce_reply_quality_guard.py
───────────────────────────────
Strip internal/footer/template residue and English leakage from Brain
commerce replies before WhatsApp dispatch. Replaces empty results with
safe Arabic commerce fallbacks.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Pattern, Tuple

from modules.ai.brain.intent_priority.types import (
    GOAL_GREETING_ONLY,
    GOAL_ORDER_REQUEST,
    GOAL_PRODUCT_AVAILABILITY,
    GOAL_SOCIAL_ONLY,
)
from modules.ai.brain.postprocess.stub_reply_guard_context import (
    is_lightweight_social_turn,
    should_suppress_generic_stub_injection,
)
from modules.ai.brain.turn_owner_contract import get_turn_owner_contract

logger = logging.getLogger("nahla.brain.postprocess.commerce_reply_quality_guard")


def _finalize_commerce_fallback(
    fallback: str,
    kind: str,
    *,
    inbound_text: str = "",
    inbound_metadata: Optional[dict] = None,
    intent_name: str = "",
    decision_topic: str = "",
    protected_final_reply: bool = False,
) -> Tuple[str, str]:
    """Block catalog fallback on non-catalog turns; route coupon inquiries safely."""
    try:
        from modules.ai.brain.commerce.inbound_fragment_guard import (  # noqa: PLC0415
            build_discount_coupon_support_reply,
            is_catalog_fallback_reply,
            is_discount_coupon_inquiry,
            should_block_catalog_grounding_fallback,
        )

        if is_discount_coupon_inquiry(inbound_text):
            return build_discount_coupon_support_reply(), "discount_coupon_support"

        blocked, block_reason = should_block_catalog_grounding_fallback(
            inbound_text=inbound_text,
            inbound_metadata=inbound_metadata,
            decision_topic=decision_topic,
            protected_final_reply=protected_final_reply,
        )
        if blocked and is_catalog_fallback_reply(fallback):
            if is_discount_coupon_inquiry(inbound_text):
                return build_discount_coupon_support_reply(), "discount_coupon_support"
            return "", f"catalog_containment_{block_reason or 'blocked'}"
    except Exception:  # noqa: BLE001
        logger.exception("[COMMERCE_REPLY_QUALITY] catalog_containment_failed")
    return fallback, kind


_FALLBACK_AVAILABILITY_AR = "التوفر قيد التحقق."
_FALLBACK_PRODUCT_UNRESOLVED_AR = "حدّد المنتج أو المقاس المطلوب."
_FALLBACK_DELIVERY_AR = "التوصيل لمنطقتك قيد التحقق."
_FALLBACK_GREETING_AR = "وعليكم السلام، حياك الله."

_MIN_MEANINGFUL_CHARS = 6

_FORBIDDEN_RESIDUE_RES: Tuple[Pattern[str], ...] = (
    re.compile(r"powered\s+by\s+nahla", re.IGNORECASE),
    re.compile(r"let\s+me\s+verify", re.IGNORECASE),
    re.compile(r"current\s+availability", re.IGNORECASE),
    re.compile(r"same-day\s+delivery\s+availability", re.IGNORECASE),
    re.compile(r"availability\s+for\s+your\s+area", re.IGNORECASE),
)

_GENERIC_AR_VERIFY_RES: Tuple[Pattern[str], ...] = (
    re.compile(
        r"س+[اأ]تحقق\s+من\s+توفر\s+المنتج\s+لك",
        re.UNICODE | re.IGNORECASE,
    ),
    re.compile(
        r"س+[اأ]تحقق\s+من\s+إ?م?ك?ا?ن?ي?ة?\s+التوصيل",
        re.UNICODE | re.IGNORECASE,
    ),
)

_COMMERCE_SUBSTANCE_RE = re.compile(
    r"(?:منتج|عسل|متوفر|متاح|سعر|حجم|وزن|طلب|ريال|أبشر|كيلو|جرام)",
    re.UNICODE | re.IGNORECASE,
)

_AVAILABILITY_INBOUND_RE = re.compile(
    r"(?:\u0647\u0644|\u0639\u0646\u062f\u0643\u0645|\u0639\u0646\u062f\u0643|\u0645\u062a\u0648\u0641\u0631|\u0628\u0643\u0645|\u0643\u0645\s|\u0623\u064a\s|\u0648\u0634\s)",
    re.UNICODE | re.IGNORECASE,
)

_PRICE_FOLLOWUP_INBOUND_RE = re.compile(
    r"(?:"
    r"كم\s*سعر(?:ه|ها)?|بكم|ثمن(?:ه|ها)?|كم\s*ثمن(?:ه|ها)?|"
    r"how\s*much|price\s*(?:it|this)?"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_DELIVERY_INBOUND_RE = re.compile(
    r"(?:موقع|الموقع|عنوان|العنوان|توصيل|استلام|منطقت|المنطقة|"
    r"maps\.google|goo\.gl/maps|short\s+address|العنوان\s+الوطني|"
    r"delivery|address|location)",
    re.UNICODE | re.IGNORECASE,
)

_COMMERCE_INTENTS = frozenset({
    "solution_seeking_commerce",
    "ask_product",
    "ask_price",
    "product_availability",
    "product_reference",
    "ask_shipping",
})

_LATIN_WORD_RE = re.compile(r"[A-Za-z]{2,}")
_ARABIC_CHAR_RE = re.compile(r"[\u0600-\u06FF]")
_EMOJI_CHECK_RE = re.compile(r"[✅✔️]\s*")


@dataclass(frozen=True)
class CommerceReplyQualityGuardResult:
    reply: str
    replaced: bool
    stripped_residue: bool
    stripped_english: bool
    used_fallback: bool
    fallback_kind: str = ""


def _normalize_for_match(text: str) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text)).strip().lower()
    t = re.sub(r"[\u064B-\u065F\u0670\u0640\u06D6-\u06ED]", "", t)
    return t


def inbound_is_arabic(inbound_text: str, *, locale: str = "ar") -> bool:
    loc = (locale or "ar").strip().lower()
    if loc.startswith("en"):
        return False
    text = (inbound_text or "").strip()
    if not text:
        return True
    if _ARABIC_CHAR_RE.search(text):
        return True
    return not _LATIN_WORD_RE.search(text)


def _segment_is_primarily_english(segment: str) -> bool:
    raw = (segment or "").strip()
    if not raw:
        return False
    latin = len(_LATIN_WORD_RE.findall(raw))
    arabic = len(_ARABIC_CHAR_RE.findall(raw))
    if latin >= 3 and latin >= arabic:
        return True
    if latin >= 8 and latin > arabic:
        return True
    return False


def _has_commerce_substance(text: str) -> bool:
    return bool(_COMMERCE_SUBSTANCE_RE.search(text or ""))


def _strip_forbidden_residue(text: str) -> Tuple[str, bool]:
    raw = (text or "").strip()
    if not raw:
        return "", False

    stripped_any = False
    lines_out: List[str] = []

    for line in raw.splitlines():
        ln = line.strip()
        if not ln:
            continue
        if any(pattern.search(ln) for pattern in _GENERIC_AR_VERIFY_RES):
            stripped_any = True
            continue
        cleaned = ln
        for pattern in _FORBIDDEN_RESIDUE_RES:
            new = pattern.sub("", cleaned)
            if new != cleaned:
                stripped_any = True
                cleaned = new
        cleaned = _EMOJI_CHECK_RE.sub("", cleaned)
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" .،,!…-")
        if cleaned:
            lines_out.append(cleaned)

    return "\n".join(lines_out).strip(), stripped_any


def _strip_english_from_arabic_reply(text: str) -> Tuple[str, bool]:
    raw = (text or "").strip()
    if not raw:
        return "", False

    stripped_any = False
    paragraphs: List[str] = []

    for paragraph in re.split(r"\n\s*\n", raw):
        p = paragraph.strip()
        if not p:
            continue
        if _segment_is_primarily_english(p):
            stripped_any = True
            continue

        kept_lines: List[str] = []
        for line in p.splitlines():
            ln = line.strip()
            if not ln:
                continue
            segments = re.split(r"(?<=[.!?])\s+", ln)
            kept_segments: List[str] = []
            for seg in segments:
                seg = seg.strip()
                if not seg:
                    continue
                if _segment_is_primarily_english(seg):
                    stripped_any = True
                    continue
                inline = seg
                for pattern in _FORBIDDEN_RESIDUE_RES:
                    new = pattern.sub("", inline)
                    if new != inline:
                        stripped_any = True
                        inline = new
                inline = re.sub(r"\s{2,}", " ", inline).strip(" .،,!…-")
                if inline:
                    kept_segments.append(inline)
            if kept_segments:
                kept_lines.append(" ".join(kept_segments))
        if kept_lines:
            paragraphs.append("\n".join(kept_lines))

    return "\n\n".join(paragraphs).strip(), stripped_any


def _is_delivery_turn(
    *,
    intent_name: str,
    primary_customer_goal: str,
    inbound_text: str,
) -> bool:
    intent = (intent_name or "").strip().lower()
    goal = (primary_customer_goal or "").strip().lower()
    if intent == "ask_shipping" or goal == "shipping_inquiry":
        return True
    return bool(_DELIVERY_INBOUND_RE.search(inbound_text or ""))


def _is_short_product_probe(inbound_text: str) -> bool:
    text = _normalize_for_match(inbound_text)
    if not text or len(text) > 16:
        return False
    if _AVAILABILITY_INBOUND_RE.search(inbound_text or ""):
        return False
    if _DELIVERY_INBOUND_RE.search(inbound_text or ""):
        return False
    return bool(re.search(r"[\u0600-\u06FFa-z]", inbound_text or ""))


_NON_COMMERCE_INTENTS = frozenset({
    "social",
    "greeting",
    "general",
    "persona_interaction",
    "who_are_you",
})


def _kb_negative_availability_decision(
    decision_topic: str = "",
    availability_polarity: str = "",
    chosen_path: str = "",
    kb_availability_facts: Optional[Dict[str, Any]] = None,
) -> bool:
    polarity = (availability_polarity or "").strip()
    if (
        (decision_topic or "").strip() == "kb_availability_facts"
        and polarity == "negative"
    ):
        return True
    if (chosen_path or "").strip() == "kb_availability_facts":
        facts = kb_availability_facts or {}
        if str(facts.get("availability_polarity") or "").strip() == "negative":
            return True
    return False


def select_arabic_commerce_fallback(
    *,
    intent_name: str = "",
    primary_customer_goal: str = "",
    inbound_text: str = "",
    conversation_objective: str = "",
    state: Any = None,
    inbound_metadata: Optional[dict] = None,
    decision_topic: str = "",
    availability_polarity: str = "",
    chosen_path: str = "",
    kb_availability_facts: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str]:
    if _kb_negative_availability_decision(
        decision_topic,
        availability_polarity,
        chosen_path=chosen_path,
        kb_availability_facts=kb_availability_facts,
    ):
        return "", "kb_negative_suppressed"
    try:
        from modules.ai.brain.commerce.status_reply_product_context import (  # noqa: PLC0415
            status_reply_context_blocks_availability_fallback,
        )

        if status_reply_context_blocks_availability_fallback(
            inbound_metadata=inbound_metadata,
            state=state,
        ):
            if (intent_name or "").strip().lower() == "ask_price":
                return _FALLBACK_PRODUCT_UNRESOLVED_AR, "status_reply_price"
            if _PRICE_FOLLOWUP_INBOUND_RE.search(inbound_text or ""):
                return _FALLBACK_PRODUCT_UNRESOLVED_AR, "status_reply_price"
            return "", "status_reply_suppressed"
    except Exception:  # noqa: silent-ok — status context gate must not break fallback
        pass
    if (intent_name or "").strip().lower() == "ask_price":
        return _FALLBACK_PRODUCT_UNRESOLVED_AR, "price_product_unresolved"
    try:
        from modules.ai.brain.commerce.product_ordering_prompt import (  # noqa: PLC0415
            build_short_honey_order_clarify_reply,
            is_short_honey_order_request,
        )

        if is_short_honey_order_request(inbound_text):
            return build_short_honey_order_clarify_reply(inbound_text), "short_honey_order"
    except Exception:  # noqa: silent-ok — ordering prompt must not break fallback
        pass

    try:
        from modules.ai.brain.intent.active_order_quantity_extract import (  # noqa: PLC0415
            message_has_bare_quantity_or_variant_signal,
            resolve_active_order_quantity_reply,
        )
        from modules.ai.brain.postprocess.stub_reply_guard_context import (  # noqa: PLC0415
            has_active_commerce_from_state,
        )
        from modules.ai.order_flow_v2.flags import should_skip_legacy_order_flow_reply  # noqa: PLC0415

        _suppress_qty_followup = False
        try:
            from modules.ai.brain.state.price_objection_topic import (  # noqa: PLC0415
                should_suppress_quantity_followup,
            )

            _suppress_qty_followup = should_suppress_quantity_followup(inbound_text)
        except Exception:  # noqa: silent-ok — price objection gate must not break fallback
            pass

        if (
            not _suppress_qty_followup
            and not should_skip_legacy_order_flow_reply()
        ):
            _block_qty_prompt = False
            try:
                from modules.ai.brain.commerce.catalog_order_resilience import (  # noqa: PLC0415
                    is_catalog_checkout_product_question_forbidden,
                )

                _block_qty_prompt = is_catalog_checkout_product_question_forbidden(
                    inbound_metadata=inbound_metadata,
                    message=inbound_text,
                    state=state,
                )
            except Exception:  # noqa: BLE001  # noqa: silent-ok — catalog guard must not break fallback
                _block_qty_prompt = False
            if not _block_qty_prompt and message_has_bare_quantity_or_variant_signal(inbound_text):
                qty_reply = resolve_active_order_quantity_reply(
                    inbound_text,
                    state=state,
                    active_commerce=has_active_commerce_from_state(state),
                )
                if qty_reply:
                    return qty_reply, "active_order_quantity"
    except Exception:  # noqa: silent-ok — qty fallback must not break commerce guard
        pass

    if should_suppress_generic_stub_injection(
        inbound_text=inbound_text,
        intent_name=intent_name,
        primary_customer_goal=primary_customer_goal,
        conversation_objective=conversation_objective,
        state=state,
        inbound_metadata=inbound_metadata,
    ):
        if is_lightweight_social_turn(
            inbound_text,
            intent_name=intent_name,
            primary_customer_goal=primary_customer_goal,
            inbound_metadata=inbound_metadata,
        ):
            try:
                from modules.ai.brain.compose.templates import (  # noqa: PLC0415
                    social_mirror_fallback_reply,
                )

                mirrored = social_mirror_fallback_reply(inbound_text)
                if mirrored:
                    return mirrored, "social_mirror"
            except Exception:  # noqa: silent-ok
                pass
            return "", "social_suppressed"
        if (primary_customer_goal or "").strip().lower() == GOAL_ORDER_REQUEST:
            try:
                from modules.ai.brain.commerce.checkout_slot_fallback import (  # noqa: PLC0415
                    _has_authoritative_product,
                )
                from modules.ai.brain.commerce.product_ordering_prompt import (  # noqa: PLC0415
                    build_short_honey_order_clarify_reply,
                    is_short_honey_order_request,
                )
                from modules.ai.brain.commerce.start_order_verb_guard import (  # noqa: PLC0415
                    is_bare_start_order_phrase,
                )

                if is_bare_start_order_phrase(inbound_text) and not _has_authoritative_product(state):
                    return "", "bare_start_order_no_product"
                if is_short_honey_order_request(inbound_text) and _has_authoritative_product(state):
                    return build_short_honey_order_clarify_reply(inbound_text), "order_request"
            except Exception:  # noqa: silent-ok
                pass

        try:
            from modules.ai.brain.postprocess.stub_reply_guard_context import (  # noqa: PLC0415
                has_active_commerce_from_state,
                is_staff_route_rejection_message,
                resolve_staff_rejection_commerce_resume,
            )

            if is_staff_route_rejection_message(inbound_text):
                return resolve_staff_rejection_commerce_resume(state), "staff_route_rejected_resume"
            if has_active_commerce_from_state(state):
                _skip_legacy_checkout = False
                try:
                    from modules.ai.order_flow_v2.flags import (  # noqa: PLC0415
                        should_skip_legacy_order_flow_reply,
                    )

                    _skip_legacy_checkout = should_skip_legacy_order_flow_reply()
                except Exception:  # noqa: BLE001  # noqa: silent-ok — V2 gate must not break checkout fallback
                    pass
                if not _skip_legacy_checkout:
                    from modules.ai.brain.commerce.checkout_slot_fallback import (  # noqa: PLC0415
                        build_checkout_slot_fallback_reply,
                    )

                    slot_reply = build_checkout_slot_fallback_reply(
                        state=state,
                        inbound_text=inbound_text,
                    )
                    if slot_reply:
                        return slot_reply, "checkout_slot_prompt"
        except Exception:  # noqa: silent-ok
            pass

    try:
        from modules.ai.brain.intent.education_context_classifier import (  # noqa: PLC0415
            education_clarify_reply,
            is_education_non_commerce_context,
        )

        if is_education_non_commerce_context(inbound_text):
            return education_clarify_reply(inbound_text), "education"
    except Exception:  # noqa: silent-ok — education gate must not break fallback
        pass

    if _is_delivery_turn(
        intent_name=intent_name,
        primary_customer_goal=primary_customer_goal,
        inbound_text=inbound_text,
    ):
        return _FALLBACK_DELIVERY_AR, "delivery"
    goal = (primary_customer_goal or "").strip().lower()
    intent = (intent_name or "").strip().lower()
    if goal in {GOAL_GREETING_ONLY, GOAL_SOCIAL_ONLY} or intent in _NON_COMMERCE_INTENTS:
        norm = _normalize_for_match(inbound_text)
        if norm.startswith("السلام") or "سلام عليكم" in norm:
            return _FALLBACK_GREETING_AR, "greeting"
        try:
            from modules.ai.brain.compose.templates import social_mirror_fallback_reply  # noqa: PLC0415

            mirrored = social_mirror_fallback_reply(inbound_text)
            if mirrored:
                return mirrored, "social_mirror"
        except Exception:  # noqa: silent-ok
            pass
        return "", "social_suppressed"
    if _is_short_product_probe(inbound_text) and (
        goal == GOAL_PRODUCT_AVAILABILITY
        or intent in {"ask_product", "solution_seeking_commerce", "product_availability"}
    ):
        return _FALLBACK_PRODUCT_UNRESOLVED_AR, "product_unresolved"

    try:
        from modules.ai.brain.intent.conversation_objective_guard import (  # noqa: PLC0415
            should_block_availability_fallback,
        )

        if should_block_availability_fallback(
            inbound_text=inbound_text,
            intent_name=intent_name,
            primary_customer_goal=primary_customer_goal,
            conversation_objective=conversation_objective,
        ):
            norm = _normalize_for_match(inbound_text)
            if norm.startswith("السلام") or "سلام عليكم" in norm:
                return _FALLBACK_GREETING_AR, "greeting"
            try:
                from modules.ai.brain.compose.templates import social_mirror_fallback_reply  # noqa: PLC0415

                mirrored = social_mirror_fallback_reply(inbound_text)
                if mirrored:
                    return mirrored, "social_mirror"
            except Exception:  # noqa: silent-ok
                pass
            return "", "social_suppressed"
    except Exception:  # noqa: silent-ok — objective gate must not break fallback
        pass

    if (
        goal == GOAL_PRODUCT_AVAILABILITY
        or intent in _COMMERCE_INTENTS
    ) and _AVAILABILITY_INBOUND_RE.search(inbound_text or ""):
        if (intent or "").strip().lower() == "ask_price":
            return _FALLBACK_PRODUCT_UNRESOLVED_AR, "price_product_unresolved"
        if _PRICE_FOLLOWUP_INBOUND_RE.search(inbound_text or ""):
            return _FALLBACK_PRODUCT_UNRESOLVED_AR, "price_product_unresolved"
        return _FALLBACK_AVAILABILITY_AR, "availability"
    norm = _normalize_for_match(inbound_text)
    if norm.startswith("السلام") or "سلام عليكم" in norm:
        return _FALLBACK_GREETING_AR, "greeting"
    try:
        from modules.ai.brain.compose.templates import social_mirror_fallback_reply  # noqa: PLC0415

        mirrored = social_mirror_fallback_reply(inbound_text)
        if mirrored:
            return mirrored, "social_mirror"
    except Exception:  # noqa: silent-ok
        pass
    return "", "social_suppressed"


def _meaningful_length(text: str) -> int:
    return len(re.sub(r"\s+", "", (text or "").strip()))


def apply_commerce_reply_quality_guard(
    reply: str,
    *,
    inbound_text: str = "",
    intent_name: str = "",
    primary_customer_goal: str = "",
    conversation_objective: str = "",
    locale: str = "ar",
    tenant_id: Optional[int] = None,
    conversation_id: Optional[int] = None,
    state: Any = None,
    inbound_metadata: Optional[dict] = None,
    decision_topic: str = "",
    availability_polarity: str = "",
    chosen_path: str = "",
    kb_availability_facts: Optional[Dict[str, Any]] = None,
) -> CommerceReplyQualityGuardResult:
    original = (reply or "").strip()
    kb_negative = _kb_negative_availability_decision(
        decision_topic,
        availability_polarity,
        chosen_path=chosen_path,
        kb_availability_facts=kb_availability_facts,
    )
    contract = get_turn_owner_contract(inbound_metadata=inbound_metadata)
    try:
        from modules.ai.brain.postprocess.stub_reply_guard_context import (  # noqa: PLC0415
            is_generic_stub_reply,
            is_staff_route_rejection_message,
            resolve_staff_rejection_commerce_resume,
        )

        if (
            is_staff_route_rejection_message(inbound_text)
            and not (contract is not None and contract.protected_final_reply)
        ):
            if not original or is_generic_stub_reply(original):
                resume = resolve_staff_rejection_commerce_resume(state)
                return CommerceReplyQualityGuardResult(
                    reply=resume,
                    replaced=True,
                    stripped_residue=False,
                    stripped_english=False,
                    used_fallback=True,
                    fallback_kind="staff_route_rejected_resume",
                )
    except Exception:  # noqa: silent-ok
        pass

    if not original:
        if contract is not None and contract.protected_final_reply:
            return CommerceReplyQualityGuardResult(
                reply=original,
                replaced=False,
                stripped_residue=False,
                stripped_english=False,
                used_fallback=False,
                fallback_kind="protected_final_reply_no_fallback",
            )
        fallback, kind = select_arabic_commerce_fallback(
            intent_name=intent_name,
            primary_customer_goal=primary_customer_goal,
            inbound_text=inbound_text,
            conversation_objective=conversation_objective,
            state=state,
            inbound_metadata=inbound_metadata,
            decision_topic=decision_topic,
            availability_polarity=availability_polarity,
            chosen_path=chosen_path,
            kb_availability_facts=kb_availability_facts,
        )
        fallback, kind = _finalize_commerce_fallback(
            fallback,
            kind,
            inbound_text=inbound_text,
            inbound_metadata=inbound_metadata,
            intent_name=intent_name,
            decision_topic=decision_topic,
            protected_final_reply=bool(
                contract is not None and contract.protected_final_reply
            ),
        )
        if kb_negative and kind == "kb_negative_suppressed":
            return CommerceReplyQualityGuardResult(
                reply="",
                replaced=False,
                stripped_residue=False,
                stripped_english=False,
                used_fallback=False,
                fallback_kind=kind,
            )
        if not fallback and kind == "social_suppressed":
            return CommerceReplyQualityGuardResult(
                reply="",
                replaced=False,
                stripped_residue=False,
                stripped_english=False,
                used_fallback=False,
                fallback_kind=kind,
            )
        if not fallback and kind.startswith("catalog_containment"):
            return CommerceReplyQualityGuardResult(
                reply="",
                replaced=False,
                stripped_residue=False,
                stripped_english=False,
                used_fallback=False,
                fallback_kind=kind,
            )
        return CommerceReplyQualityGuardResult(
            reply=fallback,
            replaced=True,
            stripped_residue=False,
            stripped_english=False,
            used_fallback=True,
            fallback_kind=kind,
        )

    text = original
    stripped_residue = False
    stripped_english = False

    cleaned, did_residue = _strip_forbidden_residue(text)
    if did_residue:
        stripped_residue = True
        text = cleaned

    if inbound_is_arabic(inbound_text, locale=locale):
        cleaned, did_en = _strip_english_from_arabic_reply(text)
        if did_en:
            stripped_english = True
            text = cleaned

    used_fallback = False
    fallback_kind = ""
    needs_fallback = _meaningful_length(text) < _MIN_MEANINGFUL_CHARS
    if (
        not needs_fallback
        and (stripped_residue or stripped_english)
        and text
        and not _has_commerce_substance(text)
    ):
        needs_fallback = True
    if needs_fallback:
        if contract is not None and contract.protected_final_reply:
            return CommerceReplyQualityGuardResult(
                reply=original,
                replaced=False,
                stripped_residue=stripped_residue,
                stripped_english=stripped_english,
                used_fallback=False,
                fallback_kind="protected_final_reply_no_fallback",
            )
        text, fallback_kind = select_arabic_commerce_fallback(
            intent_name=intent_name,
            primary_customer_goal=primary_customer_goal,
            inbound_text=inbound_text,
            conversation_objective=conversation_objective,
            state=state,
            inbound_metadata=inbound_metadata,
            decision_topic=decision_topic,
            availability_polarity=availability_polarity,
            chosen_path=chosen_path,
            kb_availability_facts=kb_availability_facts,
        )
        text, fallback_kind = _finalize_commerce_fallback(
            text,
            fallback_kind,
            inbound_text=inbound_text,
            inbound_metadata=inbound_metadata,
            intent_name=intent_name,
            decision_topic=decision_topic,
            protected_final_reply=bool(
                contract is not None and contract.protected_final_reply
            ),
        )
        used_fallback = True
        if kb_negative and fallback_kind == "kb_negative_suppressed":
            text = original
            used_fallback = False
            needs_fallback = False
            fallback_kind = ""
        if not text and fallback_kind == "social_suppressed":
            used_fallback = False
            needs_fallback = False

    replaced = text != original
    try:
        from modules.ai.brain.postprocess.stub_reply_guard_context import (  # noqa: PLC0415
            is_generic_stub_reply,
            is_staff_route_rejection_message,
            resolve_staff_rejection_commerce_resume,
        )

        if (
            not replaced
            and is_generic_stub_reply(text)
            and is_staff_route_rejection_message(inbound_text)
            and not (contract is not None and contract.protected_final_reply)
        ):
            text = resolve_staff_rejection_commerce_resume(state)
            used_fallback = True
            fallback_kind = "staff_route_rejected_resume"
            replaced = text != original
    except Exception:  # noqa: silent-ok
        pass

    if replaced:
        logger.info(
            "[COMMERCE_REPLY_QUALITY_GUARD] tenant=%s conversation=%s "
            "stripped_residue=%s stripped_english=%s used_fallback=%s "
            "fallback_kind=%s orig_len=%d new_len=%d intent=%s",
            tenant_id if tenant_id is not None else "-",
            conversation_id if conversation_id is not None else "-",
            stripped_residue,
            stripped_english,
            used_fallback,
            fallback_kind or "-",
            len(original),
            len(text),
            intent_name or "-",
        )

    try:
        from modules.ai.brain.commerce.catalog_order_resilience import (  # noqa: PLC0415
            is_catalog_checkout_product_question_forbidden,
            sanitize_forbidden_catalog_product_question,
        )

        if is_catalog_checkout_product_question_forbidden(
            inbound_metadata=inbound_metadata,
            message=inbound_text,
            state=state,
        ):
            sanitized = sanitize_forbidden_catalog_product_question(
                text,
                inbound_metadata=inbound_metadata,
                message=inbound_text,
                state=state,
            )
            if sanitized and sanitized != text:
                text = sanitized
                replaced = True
                used_fallback = True
                fallback_kind = fallback_kind or "catalog_checkout_safe"
        from modules.ai.brain.commerce.catalog_checkout_customer_identity import (  # noqa: PLC0415
            is_catalog_checkout_name_question_forbidden,
            reply_contains_forbidden_catalog_name_question,
            sanitize_forbidden_catalog_name_question,
        )

        if is_catalog_checkout_name_question_forbidden(state=state):
            if reply_contains_forbidden_catalog_name_question(text):
                sanitized_name = sanitize_forbidden_catalog_name_question(
                    text,
                    state=state,
                )
                if sanitized_name and sanitized_name != text:
                    text = sanitized_name
                    replaced = True
                    used_fallback = True
                    fallback_kind = fallback_kind or "catalog_checkout_name_safe"
    except Exception:  # noqa: BLE001  # noqa: silent-ok — catalog sanitize must not block reply
        pass

    return CommerceReplyQualityGuardResult(
        reply=text,
        replaced=replaced,
        stripped_residue=stripped_residue,
        stripped_english=stripped_english,
        used_fallback=used_fallback,
        fallback_kind=fallback_kind,
    )


__all__ = [
    "CommerceReplyQualityGuardResult",
    "apply_commerce_reply_quality_guard",
    "inbound_is_arabic",
    "select_arabic_commerce_fallback",
]
