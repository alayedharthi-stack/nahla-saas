"""Post-compose guard chain for FactBoundPersonaComposer."""
from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("nahla.brain.persona.compose_guards")

from .facts_bundle import (
    PersonaFactsBundle,
    PHASE2_SOCIAL_SURFACES,
    PERSONA_SURFACE_CATALOG_PRODUCT_ANSWER,
    PERSONA_SURFACE_KB_PRODUCT_ANSWER,
    PERSONA_SURFACE_PAYMENT_MEDIA_INTRO,
    PERSONA_SURFACE_TRUSTED_COUPON_OFFER_ANSWER,
    PERSONA_SURFACE_CUSTOMER_CONDITIONAL_COUPON_ANSWER,
)
from .fallback_catalog import deterministic_fallback


@dataclass(frozen=True)
class PersonaGuardResult:
    text: str
    passed: bool
    failed_reason: str = ""
    repaired: bool = False
    rejected_observability: dict[str, Any] = field(default_factory=dict)


def _ambiguous_allowed_amounts(facts: dict[str, Any]) -> set[Any]:
    from modules.ai.brain.postprocess.product_claim_grounding_evidence import (  # noqa: PLC0415
        parse_price_amount,
    )

    amounts: set[Any] = set()
    for candidate in facts.get("ambiguous_catalog_candidates") or []:
        if not isinstance(candidate, dict):
            continue
        amt = parse_price_amount(candidate.get("price"))
        if amt is not None:
            amounts.add(amt)
    return amounts


def _split_bounded_clauses(text: str) -> list[str]:
    """Split reply into clause/sentence spans on language-agnostic boundaries."""
    working = str(text or "").strip()
    if not working:
        return []
    parts = re.split(r"([.!?؟\n]+)", working)
    clauses: list[str] = []
    current = ""
    for part in parts:
        if part and re.fullmatch(r"[.!?؟\n]+", part):
            current += part
            if current.strip():
                clauses.append(current.strip())
            current = ""
        else:
            current += part
    if current.strip():
        clauses.append(current.strip())
    return clauses if clauses else [working]


def _clause_is_interrogative(clause: str) -> bool:
    return "?" in str(clause or "") or "؟" in str(clause or "")


def _claimed_amounts_in_non_interrogative_clauses(text: str) -> list[Any]:
    """Return grounded price amounts that appear only in declarative clauses."""
    disallowed: list[Any] = []
    clauses = _split_bounded_clauses(text)
    if not clauses:
        clauses = [str(text or "")]
    for clause in clauses:
        if _clause_is_interrogative(clause):
            continue
        disallowed.extend(_ambiguous_claimed_amounts(clause))
    return disallowed


def _is_clarification_question_context(text: str) -> bool:
    return any(_clause_is_interrogative(clause) for clause in _split_bounded_clauses(text))


def _ambiguous_claimed_amounts(text: str) -> list[Any]:
    """Price amounts with explicit currency or price-keyword context (not product specs)."""
    from modules.ai.brain.postprocess.product_claim_grounding_evidence import (  # noqa: PLC0415
        _CURRENCY_TOKEN_RE,
        _PRICE_CONTEXT_RE,
        _REPLY_PRICE_AMOUNT_RE,
        _normalize_price_text,
        parse_price_amount,
    )

    working = _normalize_price_text(text or "")
    amounts: list[Any] = []
    for match in _REPLY_PRICE_AMOUNT_RE.finditer(working):
        start, end = match.span()
        before = working[max(0, start - 16):start]
        after = working[end: min(len(working), end + 16)]
        has_currency = bool(_CURRENCY_TOKEN_RE.search(after))
        has_price_ctx = bool(_PRICE_CONTEXT_RE.search(before))
        if not has_currency and not has_price_ctx:
            continue
        amt = parse_price_amount(match.group(0))
        if amt is not None:
            amounts.append(amt)
    return amounts


def _build_rejected_candidate_observability(
    text: str,
    facts: dict[str, Any],
    failed_reason: str,
) -> dict[str, Any]:
    raw = str(text or "")
    claimed = _ambiguous_claimed_amounts(raw)
    return {
        "guard_reason": str(failed_reason or "").strip(),
        "candidate_length": len(raw),
        "candidate_hash": hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16],
        "claim_summary": {
            "require_clarification": bool(facts.get("require_clarification")),
            "claimed_amounts": claimed,
            "allowed_amounts": sorted(_ambiguous_allowed_amounts(facts)),
            "non_interrogative_claimed_amounts": _claimed_amounts_in_non_interrogative_clauses(
                raw
            ),
            "is_question_context": _is_clarification_question_context(raw),
        },
    }


def _catalog_ambiguity_guard_fail(
    text: str,
    facts: dict[str, Any],
    reason: str,
) -> PersonaGuardResult:
    observability = _build_rejected_candidate_observability(text, facts, reason)
    facts["_rejected_compose_observability"] = observability
    logger.info(
        "[CATALOG_AMBIGUITY_GUARD] rejected reason=%s hash=%s len=%s summary=%s",
        observability.get("guard_reason"),
        observability.get("candidate_hash"),
        observability.get("candidate_length"),
        observability.get("claim_summary"),
    )
    return PersonaGuardResult(
        text=text,
        passed=False,
        failed_reason=reason,
        rejected_observability=observability,
    )


def _classify_ambiguous_catalog_reply_violation(
    text: str,
    facts: dict[str, Any],
) -> str:
    """Operational claim validation for catalog ambiguity clarifications."""
    working = str(text or "").strip()
    if not working or not facts.get("require_clarification"):
        return ""

    if not facts.get("allow_availability_mention"):
        availability_markers = (
            "متوفر",
            "غير متوفر",
            "نفذ",
            "available",
            "out of stock",
        )
        if any(m in working.lower() for m in availability_markers):
            return "invented_availability"

    allowed_amounts = _ambiguous_allowed_amounts(facts)
    claimed_amounts = _ambiguous_claimed_amounts(working)
    for claimed in claimed_amounts:
        if claimed not in allowed_amounts:
            return "invented_price_amount"

    if _claimed_amounts_in_non_interrogative_clauses(working):
        return "ambiguous_premature_price_selection"

    if facts.get("allow_price_differentiator"):
        return ""

    price_markers = ("ريال", "ر.س", "السعر", "أسعار", "بكم", "كم سعر", "سعر")
    if any(m in working for m in price_markers):
        return "invented_price"
    return ""


def _reply_contains_forbidden_phone_slot_question(text: str) -> bool:
    """Operational phone/contact slot ask — aligned with final_turn_audit coverage."""
    from ..turn.final_turn_audit import _PHONE_QUESTION_RE  # noqa: PLC0415

    return bool(_PHONE_QUESTION_RE.search(str(text or "")))


def _catalog_slot_prompt_guard_fail(
    text: str,
    facts: dict[str, Any],
) -> PersonaGuardResult:
    if facts.get("require_clarification"):
        return _catalog_ambiguity_guard_fail(text, facts, "slot_prompt")
    return PersonaGuardResult(text=text, passed=False, failed_reason="slot_prompt")


def _count_emojis(text: str) -> int:
    from ..compose.persona_template_engine import PERSONA_ALLOWED_EMOJI  # noqa: PLC0415

    return sum(1 for ch in (text or "") if ch in PERSONA_ALLOWED_EMOJI)


def _strip_excess_emojis(text: str, *, max_emojis: int) -> tuple[str, bool]:
    from ..compose.persona_template_engine import PERSONA_ALLOWED_EMOJI  # noqa: PLC0415

    raw = str(text or "")
    if not raw.strip():
        return raw, False
    kept: list[str] = []
    emoji_seen = 0
    changed = False
    for ch in raw:
        if ch in PERSONA_ALLOWED_EMOJI:
            if emoji_seen < max_emojis:
                kept.append(ch)
                emoji_seen += 1
            else:
                changed = True
            continue
        kept.append(ch)
    return "".join(kept).strip(), changed


def _scrub_non_saudi_terms(text: str) -> tuple[str, bool]:
    from .policy_terms import NON_SAUDI_ARABIC_DIALECT_TERMS  # noqa: PLC0415

    raw = str(text or "")
    if not raw.strip():
        return raw, False
    changed = False
    cleaned = raw
    for term in NON_SAUDI_ARABIC_DIALECT_TERMS:
        pattern = re.compile(rf"(?<!\S){re.escape(term)}(?!\S)", re.UNICODE)
        if pattern.search(cleaned):
            cleaned = pattern.sub("", cleaned)
            changed = True
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned, changed


def _strip_known_customer_reasks(text: str, bundle: PersonaFactsBundle) -> tuple[str, bool]:
    from .policy_terms import (  # noqa: PLC0415
        KNOWN_CUSTOMER_BLUNT_ADDRESS_ASK_PHRASES,
        KNOWN_CUSTOMER_NAME_REASK_PHRASES,
        KNOWN_CUSTOMER_PHONE_REASK_PHRASES,
    )

    ctx = bundle.customer_context or {}
    raw = str(text or "")
    if not raw.strip():
        return raw, False
    phrases: list[str] = []
    if ctx.get("has_verified_name"):
        phrases.extend(KNOWN_CUSTOMER_NAME_REASK_PHRASES)
    if ctx.get("has_whatsapp_phone"):
        phrases.extend(KNOWN_CUSTOMER_PHONE_REASK_PHRASES)
    if ctx.get("has_saved_address"):
        phrases.extend(KNOWN_CUSTOMER_BLUNT_ADDRESS_ASK_PHRASES)
    if not phrases:
        return raw, False
    changed = False
    cleaned = raw
    for phrase in phrases:
        if phrase in cleaned:
            cleaned = cleaned.replace(phrase, "").strip(" ،،.")
            changed = True
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned, changed


def _truncate_safe(text: str, max_chars: int) -> str:
    raw = str(text or "").strip()
    if len(raw) <= max_chars:
        return raw
    cut = raw[:max_chars].rstrip()
    if cut and cut[-1] in "،.!?":
        return cut
    return cut.rstrip("،. ") + "…"


def _apply_kb_product_answer_guards(
    text: str,
    facts: dict[str, Any],
) -> PersonaGuardResult:
    working = str(text or "").strip()
    if not working:
        return PersonaGuardResult(text="", passed=False, failed_reason="empty_compose")

    if not facts.get("allow_slot_prompts", False):
        slot_markers = (
            "اسمك",
            "اسمك الكريم",
            "عنوانك",
            "وين تسكن",
            "رقم الحساب",
            "الآيبان",
            "ايبان",
            "كم الكمية",
            "كم الحبة",
            "طريقة الدفع",
        )
        if any(m in working for m in slot_markers):
            return PersonaGuardResult(
                text=working,
                passed=False,
                failed_reason="slot_prompt",
            )

    if not facts.get("allow_price_mention"):
        price_markers = ("ريال", "ر.س", "السعر", "بكم", "كم سعر")
        if any(m in working for m in price_markers):
            return PersonaGuardResult(
                text=working,
                passed=False,
                failed_reason="invented_price",
            )

    if not facts.get("allow_availability_mention"):
        availability_markers = (
            "متوفر",
            "غير متوفر",
            "نفذ",
            "available",
            "out of stock",
        )
        if any(m in working.lower() for m in availability_markers):
            return PersonaGuardResult(
                text=working,
                passed=False,
                failed_reason="invented_availability",
            )

    if not facts.get("allow_medical_claims"):
        medical_markers = (
            "يشفي",
            "يعالج",
            "علاج",
            "شفاء",
            "يقضي على",
            "يقتل الفيروس",
            "cure",
            "treat",
        )
        if any(m in working for m in medical_markers):
            return PersonaGuardResult(
                text=working,
                passed=False,
                failed_reason="medical_claim",
            )

    kb_text = str(facts.get("kb_text") or "")
    cure_markers = (
        "يشفي",
        "يعالج",
        "شفاء",
        "يقضي على",
        "يقتل الفيروس",
        "cure",
        "treat",
    )
    if any(m in working for m in cure_markers):
        if not any(m in kb_text for m in cure_markers):
            return PersonaGuardResult(
                text=working,
                passed=False,
                failed_reason="unsupported_cure_claim",
            )

    for term in ("الأفضل", "الأصلي", "مضمون"):
        if term in working and term not in kb_text:
            return PersonaGuardResult(
                text=working,
                passed=False,
                failed_reason="unsupported_superiority_claim",
            )

    return PersonaGuardResult(text=working, passed=True)


def _apply_trusted_coupon_offer_answer_guards(
    text: str,
    facts: dict[str, Any],
) -> PersonaGuardResult:
    working = str(text or "").strip()
    if not working:
        return PersonaGuardResult(text="", passed=False, failed_reason="empty_compose")

    if not facts.get("allow_code_mention"):
        code_markers = (
            "كود الخصم",
            "كود خصم",
            "الكود",
            "كوبون ",
            "coupon code",
            "discount code",
            "promo code",
        )
        if any(m.lower() in working.lower() for m in code_markers):
            return PersonaGuardResult(
                text=working,
                passed=False,
                failed_reason="coupon_code_disclosure",
            )

    applied_markers = (
        "تم تطبيق",
        "طبقنا",
        "فعلنا الكوبون",
        "applied the coupon",
        "coupon applied",
    )
    if any(m in working for m in applied_markers):
        return PersonaGuardResult(
            text=working,
            passed=False,
            failed_reason="coupon_applied_claim",
        )

    if not facts.get("allow_final_eligibility_claim"):
        final_markers = (
            "أنت مؤهل",
            "انت مؤهل",
            "مؤكد أهليتك",
            "مؤكد أهليتكم",
            "definitely eligible",
            "you are eligible",
        )
        if any(m in working for m in final_markers):
            return PersonaGuardResult(
                text=working,
                passed=False,
                failed_reason="final_eligibility_claim",
            )

    checkout_markers = (
        "نكمل الطلب",
        "اطلب الآن",
        "أرسل العنوان",
        "طريقة الدفع",
        "كم الكمية",
    )
    if any(m in working for m in checkout_markers):
        return PersonaGuardResult(
            text=working,
            passed=False,
            failed_reason="checkout_pressure",
        )

    return PersonaGuardResult(text=working, passed=True)


def _apply_customer_conditional_coupon_answer_guards(
    text: str,
    facts: dict[str, Any],
) -> PersonaGuardResult:
    from .customer_conditional_coupon_claim_classification import (  # noqa: PLC0415
        classify_customer_conditional_coupon_claim_violation,
    )

    working = str(text or "").strip()
    if not working:
        return PersonaGuardResult(text="", passed=False, failed_reason="empty_compose")

    failed_reason = classify_customer_conditional_coupon_claim_violation(working, facts)
    if failed_reason:
        return PersonaGuardResult(
            text=working,
            passed=False,
            failed_reason=failed_reason,
        )

    return PersonaGuardResult(text=working, passed=True)


# Closed presenter constructions asserting the store offers something (language-level).
_CATALOG_PRESENTER_SURFACES: tuple[str, ...] = (
    "متوفر عندنا",
    "متوفر لدينا",
    "متوفرة عندنا",
    "يتوفر عندنا",
    "من الكتالوج",
    "عندنا",
    "لدينا",
)

# Closed generic lexicon for non-named catalog prose (language-level, not category-specific).
_CATALOG_GENERIC_LEXICON_RAW: tuple[str, ...] = (
    "منتجات",
    "المنتجات",
    "خيارات",
    "الخيارات",
    "تشكيلة",
    "التشكيلة",
    "أصناف",
    "الأصناف",
    "أقسام",
    "الأقسام",
)

_CATALOG_FUNCTION_WORDS_RAW: tuple[str, ...] = (
    "من",
    "في",
    "على",
    "مع",
    "عدة",
    "بعض",
    "كل",
    "هذه",
    "هذي",
    "اللي",
    "لك",
    "لكم",
    "حاليا",
)

# Closed first-person/second-person service verbs (language-level, not category-specific).
_CATALOG_SERVICE_VERBS_RAW: tuple[str, ...] = (
    "نقدر",
    "أقدر",
    "نعرض",
    "أعرض",
    "أرشح",
    "نرشح",
    "تحب",
    "تبي",
    "أقترح",
    "نوفر",
)

# Closed pro-forms / placeholders for service-verb offers (language-level, not product nouns).
_CATALOG_SERVICE_PROFORMS_RAW: tuple[str, ...] = (
    "الأنسب",
    "المناسب",
    "الانسب",
    "شي",
    "شيء",
    "اللي يناسبك",
    "الخيارات",
    "المتاح",
)

_INVENTED_PRODUCT_TITLE_SHADOW_REASON = "invented_product_title_shadow"
_SLOT_TOKEN_BOUND = 8


def _norm_presenter_text(text: str) -> str:
    from modules.ai.brain.postprocess.product_claim_grounding_evidence import (  # noqa: PLC0415
        _norm,
    )

    base = _norm(text)
    return re.sub(r"[\s.,!?؟\n]+", " ", base).strip()


def _catalog_generic_lexicon() -> frozenset[str]:
    return frozenset(_norm_presenter_text(word) for word in _CATALOG_GENERIC_LEXICON_RAW)


def _catalog_function_words() -> frozenset[str]:
    return frozenset(_norm_presenter_text(word) for word in _CATALOG_FUNCTION_WORDS_RAW)


def _catalog_service_verbs() -> frozenset[str]:
    return frozenset(_norm_presenter_text(word) for word in _CATALOG_SERVICE_VERBS_RAW)


def _catalog_service_proforms() -> frozenset[str]:
    return frozenset(_norm_presenter_text(phrase) for phrase in _CATALOG_SERVICE_PROFORMS_RAW)


def _strip_al_prefix(token: str) -> str:
    if token.startswith("ال") and len(token) > 2:
        return token[2:]
    return token


def _skip_tokens() -> frozenset[str]:
    return _catalog_function_words() | _catalog_service_verbs()


def _trusted_vocabulary(facts: dict, allowed_titles: list[str]) -> frozenset[str]:
    from modules.ai.knowledge.product_matcher import normalize_arabic, tokenize  # noqa: PLC0415

    trusted: set[str] = set(_catalog_generic_lexicon())
    trusted |= _skip_tokens()

    for title in allowed_titles:
        for token in tokenize(normalize_arabic(title)):
            norm_tok = _norm_presenter_text(token)
            if norm_tok:
                trusted.add(norm_tok)
                trusted.add(_strip_al_prefix(norm_tok))

    for product in facts.get("catalog_products") or []:
        if not isinstance(product, dict):
            continue
        category = str(product.get("category") or "").strip()
        if not category:
            continue
        for token in tokenize(normalize_arabic(category)):
            norm_tok = _norm_presenter_text(token)
            if norm_tok:
                trusted.add(norm_tok)
                trusted.add(_strip_al_prefix(norm_tok))

    return frozenset(trusted)


def _resolve_conjunct_head(conjunct: str) -> str:
    """First content token after skipping function/service words (ال prefix kept on surface)."""
    norm = _norm_presenter_text(conjunct)
    if not norm:
        return ""
    skip = _skip_tokens()
    for token in norm.split():
        bare = _strip_al_prefix(token)
        if token in skip or bare in skip:
            continue
        return token
    return ""


def _head_is_trusted(head: str, trusted: frozenset[str]) -> bool:
    if not head:
        return True
    bare = _strip_al_prefix(head)
    return head in trusted or bare in trusted


def _normalized_presenter_surfaces() -> list[tuple[str, str]]:
    return [
        (surface, _norm_presenter_text(surface))
        for surface in sorted(
            _CATALOG_PRESENTER_SURFACES,
            key=len,
            reverse=True,
        )
    ]


def _iter_presenter_hits(norm_text: str) -> list[tuple[int, int, str]]:
    """Return non-overlapping (start, end, surface) presenter hits left-to-right."""
    hits: list[tuple[int, int, str]] = []
    pos = 0
    presenters = _normalized_presenter_surfaces()
    while pos < len(norm_text):
        matched: tuple[int, int, str] | None = None
        for surface, pres_norm in presenters:
            if pres_norm and norm_text.startswith(pres_norm, pos):
                matched = (pos, pos + len(pres_norm), surface)
                break
        if matched is None:
            pos += 1
            continue
        hits.append(matched)
        pos = matched[1]
    return hits


def _extract_presenter_slot(norm_text: str, presenter_end: int) -> str:
    remainder = norm_text[presenter_end:].lstrip()
    if not remainder:
        return ""
    terminator = re.search(r"[.!?؟\n]", remainder)
    term_end = terminator.start() if terminator else len(remainder)
    tokens = remainder.split()
    token_end = len(" ".join(tokens[:_SLOT_TOKEN_BOUND])) if tokens else 0
    if len(tokens) > _SLOT_TOKEN_BOUND:
        cut = min(term_end, token_end)
    else:
        cut = term_end
    return remainder[:cut].strip()


def _split_presenter_conjuncts(slot: str) -> list[str]:
    parts = [slot]
    for separator in ("،", " و "):
        split_parts: list[str] = []
        for part in parts:
            split_parts.extend(part.split(separator))
        parts = split_parts
    return [part.strip() for part in parts if part.strip()]


def _conjunct_has_service_verb(conjunct: str) -> bool:
    service = _catalog_service_verbs()
    for token in _norm_presenter_text(conjunct).split():
        bare = _strip_al_prefix(token)
        if token in service or bare in service:
            return True
    return False


def _trim_function_edge_tokens(tokens: list[str]) -> list[str]:
    skip = _catalog_function_words()
    trimmed = list(tokens)
    while trimmed:
        bare = _strip_al_prefix(trimmed[0])
        if trimmed[0] in skip or bare in skip:
            trimmed.pop(0)
            continue
        break
    while trimmed:
        bare = _strip_al_prefix(trimmed[-1])
        if trimmed[-1] in skip or bare in skip:
            trimmed.pop()
            continue
        break
    return trimmed


def _tokens_after_last_service_verb(conjunct: str) -> list[str]:
    tokens = _norm_presenter_text(conjunct).split()
    if not tokens:
        return []
    service = _catalog_service_verbs()
    last_service_idx = -1
    for idx, token in enumerate(tokens):
        bare = _strip_al_prefix(token)
        if token in service or bare in service:
            last_service_idx = idx
    if last_service_idx < 0:
        return []
    return tokens[last_service_idx + 1 :]


def _trailing_offer_matches_proform(tokens: list[str]) -> bool:
    trimmed = _trim_function_edge_tokens(tokens)
    if not trimmed:
        return True
    phrase = " ".join(trimmed)
    proforms = _catalog_service_proforms()
    if phrase in proforms:
        return True
    last_token = trimmed[-1]
    bare_last = _strip_al_prefix(last_token)
    if last_token in proforms or bare_last in proforms:
        return True
    for proform in sorted(proforms, key=len, reverse=True):
        proform_tokens = proform.split()
        if len(trimmed) >= len(proform_tokens) and trimmed[-len(proform_tokens) :] == proform_tokens:
            return True
    return False


def _conjunct_is_service_proform_offer(conjunct: str) -> bool:
    """Accept service-verb conjunct only when the trailing offer is a closed pro-form."""
    if not _conjunct_has_service_verb(conjunct):
        return False
    return _trailing_offer_matches_proform(_tokens_after_last_service_verb(conjunct))


def _conjunct_has_ungrounded_head(conjunct: str, trusted: frozenset[str]) -> bool:
    if _conjunct_is_service_proform_offer(conjunct):
        return False
    head = _resolve_conjunct_head(conjunct)
    if not head:
        return False
    return not _head_is_trusted(head, trusted)


def _conjunct_grounded_title(conjunct: str, allowed_titles: list[str]) -> str:
    from modules.ai.brain.postprocess.product_claim_grounding_evidence import (  # noqa: PLC0415
        _text_references_product,
    )

    for title in allowed_titles:
        if _text_references_product(conjunct, title):
            return title
    return ""


def detect_ungrounded_product_titles(text: str, facts: dict) -> list[dict]:
    """Shadow detector for presenter-slot product title grounding (language-level)."""
    working = str(text or "").strip()
    if not working:
        return []

    allowed_titles = [
        str(p.get("title") or "").strip()
        for p in (facts.get("catalog_products") or [])
        if isinstance(p, dict) and str(p.get("title") or "").strip()
    ]
    catalog_empty = not allowed_titles
    trusted = _trusted_vocabulary(facts, allowed_titles)
    norm_text = _norm_presenter_text(working)
    if not norm_text:
        return []

    detections: list[dict] = []
    for _start, presenter_end, surface in _iter_presenter_hits(norm_text):
        slot = _extract_presenter_slot(norm_text, presenter_end)
        if not slot:
            continue
        conjuncts = _split_presenter_conjuncts(slot)
        if not conjuncts:
            continue

        slot_grounded = False
        slot_detections: list[dict] = []
        for conjunct in conjuncts:
            matched_title = _conjunct_grounded_title(conjunct, allowed_titles)
            if matched_title:
                slot_grounded = True
                continue
            if not _conjunct_has_ungrounded_head(conjunct, trusted):
                continue
            phrase = _norm_presenter_text(conjunct)
            slot_detections.append(
                {
                    "surface": surface,
                    "phrase": phrase,
                    "candidate_phrase": phrase,
                    "matched_title": "",
                    "reason": _INVENTED_PRODUCT_TITLE_SHADOW_REASON,
                    "mixed": False,
                    "catalog_empty": catalog_empty,
                    "would_reject_enforce": True,
                }
            )

        if slot_detections:
            mixed = slot_grounded
            for entry in slot_detections:
                entry["mixed"] = mixed
            detections.extend(slot_detections)

    return detections


def _apply_catalog_product_answer_guards(
    text: str,
    facts: dict[str, Any],
) -> PersonaGuardResult:
    working = str(text or "").strip()
    if not working:
        return PersonaGuardResult(text="", passed=False, failed_reason="empty_compose")

    if not facts.get("allow_slot_prompts", False):
        slot_markers = (
            "اسمك",
            "اسمك الكريم",
            "عنوانك",
            "وين تسكن",
            "رقم الحساب",
            "الآيبان",
            "ايبان",
            "كم الكمية",
            "كم الحبة",
            "طريقة الدفع",
            "اطلبه",
            "اطلب الآن",
            "نكمل الطلب",
        )
        if any(m in working for m in slot_markers):
            return _catalog_slot_prompt_guard_fail(working, facts)
        if _reply_contains_forbidden_phone_slot_question(working):
            return _catalog_slot_prompt_guard_fail(working, facts)

    order_markers = ("تم إنشاء طلبك", "رقم الطلب", "NHL-")
    if any(m in working for m in order_markers):
        return PersonaGuardResult(
            text=working,
            passed=False,
            failed_reason="order_confirmation_claim",
        )

    require_clarification = bool(facts.get("require_clarification"))
    if require_clarification:
        ambiguity_violation = _classify_ambiguous_catalog_reply_violation(
            working,
            facts,
        )
        if ambiguity_violation:
            return _catalog_ambiguity_guard_fail(
                working,
                facts,
                ambiguity_violation,
            )
    elif not facts.get("allow_price_mention"):
        price_markers = ("ريال", "ر.س", "السعر", "بكم", "كم سعر")
        if any(m in working for m in price_markers):
            return PersonaGuardResult(
                text=working,
                passed=False,
                failed_reason="invented_price",
            )
    else:
        from modules.ai.brain.postprocess.product_claim_grounding_evidence import (  # noqa: PLC0415
            extract_reply_prices,
            parse_price_amount,
        )

        allowed_amounts = {
            amt
            for amt in (
                parse_price_amount(p.get("price"))
                for p in (facts.get("catalog_products") or [])
                if isinstance(p, dict)
            )
            if amt is not None
        }
        for claimed in extract_reply_prices(working):
            if allowed_amounts and claimed not in allowed_amounts:
                return PersonaGuardResult(
                    text=working,
                    passed=False,
                    failed_reason="invented_price_amount",
                )

    if not require_clarification:
        if not facts.get("allow_availability_mention"):
            availability_markers = (
                "متوفر",
                "غير متوفر",
                "نفذ",
                "available",
                "out of stock",
            )
            if any(m in working.lower() for m in availability_markers):
                return PersonaGuardResult(
                    text=working,
                    passed=False,
                    failed_reason="invented_availability",
                )
        elif "متوفر" in working and not facts.get("has_positive_availability"):
            return PersonaGuardResult(
                text=working,
                passed=False,
                failed_reason="unsupported_available_claim",
            )

    if not facts.get("allow_superiority_claims", False):
        for term in ("الأفضل", "الأصلي", "مضمون", "أفضل عسل"):
            if term in working:
                return PersonaGuardResult(
                    text=working,
                    passed=False,
                    failed_reason="unsupported_superiority_claim",
                )

    discount_markers = ("خصم", "تخفيض", "عرض", "%")
    if any(m in working for m in discount_markers):
        return PersonaGuardResult(
            text=working,
            passed=False,
            failed_reason="invented_offer",
        )

    scope = str(facts.get("category_scope") or facts.get("allowed_category") or "")
    if scope == "عسل":
        cross_markers = ("كريم", "زيت", "سم النحل", "عكبر")
        inbound = str(facts.get("inbound_text") or "")
        for marker in cross_markers:
            if marker in working and marker not in inbound:
                return PersonaGuardResult(
                    text=working,
                    passed=False,
                    failed_reason="category_drift",
                )

    if working.strip() in {"منتج", "المنتج", "منتجات", "المنتجات"}:
        return PersonaGuardResult(
            text=working,
            passed=False,
            failed_reason="generic_product_label",
        )

    try:
        shadow_detections = detect_ungrounded_product_titles(working, facts)
        for detection in shadow_detections:
            logger.info(
                "[INVENTED_PRODUCT_TITLE_SHADOW] surface=%s phrase=%s matched_title=%s "
                "reason=%s mixed=%s catalog_empty=%s would_reject_enforce=%s",
                detection.get("surface"),
                detection.get("phrase") or detection.get("candidate_phrase"),
                detection.get("matched_title") or "",
                detection.get("reason"),
                detection.get("mixed"),
                detection.get("catalog_empty"),
                detection.get("would_reject_enforce"),
            )
    except Exception:
        logger.exception("[INVENTED_PRODUCT_TITLE_SHADOW] detector failed")

    return PersonaGuardResult(text=working, passed=True)


def apply_persona_compose_guards(
    text: str,
    bundle: PersonaFactsBundle,
    *,
    db: Any = None,
    tenant_id: Optional[int] = None,
) -> PersonaGuardResult:
    """Run the fixed guard order from the rollout design doc."""
    working = str(text or "").strip()
    if not working:
        return PersonaGuardResult(text="", passed=False, failed_reason="empty_compose")

    repaired = False
    lang = str(bundle.language or "ar").lower()

    # 1–2 Language / non-Saudi dialect + malformed كا suffix repair
    if lang.startswith("ar"):
        from .policy_terms import (  # noqa: PLC0415
            find_malformed_saudi_ka_suffix_tokens,
            find_non_saudi_arabic_terms,
            repair_malformed_saudi_ka_suffix,
        )

        repaired_ka, did_ka = repair_malformed_saudi_ka_suffix(working)
        if did_ka and repaired_ka.strip():
            working = repaired_ka
            repaired = True
        elif find_malformed_saudi_ka_suffix_tokens(working):
            return PersonaGuardResult(
                text=working,
                passed=False,
                failed_reason="malformed_saudi_ka_suffix",
            )

        if find_non_saudi_arabic_terms(working):
            scrubbed, did = _scrub_non_saudi_terms(working)
            if did and scrubbed.strip():
                working = scrubbed
                repaired = True
            elif find_non_saudi_arabic_terms(working):
                return PersonaGuardResult(
                    text=working,
                    passed=False,
                    failed_reason="non_saudi_dialect",
                )

    # 3 Credential / payment — immediate fallback, no repair
    from .policy_terms import looks_like_invented_payment_credential  # noqa: PLC0415

    if looks_like_invented_payment_credential(working):
        return PersonaGuardResult(
            text=working,
            passed=False,
            failed_reason="payment_credential",
        )
    try:
        from ..postprocess.payment_credential_guard import (  # noqa: PLC0415
            apply_payment_credential_guard,
        )

        pcg = apply_payment_credential_guard(
            working,
            db=db,
            tenant_id=tenant_id or bundle.tenant_id,
            inbound_text=bundle.inbound_text,
        )
        if pcg.replaced:
            return PersonaGuardResult(
                text=working,
                passed=False,
                failed_reason="payment_credential_guard",
            )
        working = (pcg.reply or working).strip()
    except Exception:  # noqa: BLE001  # noqa: silent-ok — guard import must not break chain
        pass

    # 4 Fake operational claims on social / payment intro surfaces
    facts = bundle.verified_facts or {}
    if bundle.surface in PHASE2_SOCIAL_SURFACES:
        fake_markers = (
            "تم الشحن",
            "وصل الإيصال",
            "تم الدفع",
            "تم تأكيد الطلب",
        )
        if any(m in working for m in fake_markers):
            return PersonaGuardResult(
                text=working,
                passed=False,
                failed_reason="fake_operational_claim",
            )

    if bundle.surface == PERSONA_SURFACE_PAYMENT_MEDIA_INTRO:
        if not facts.get("allow_paid_claim"):
            paid_markers = (
                "تم الدفع",
                "تم تأكيد الدفع",
                "تم استلام الدفع",
                "تم اعتماد الدفع",
            )
            if any(m in working for m in paid_markers):
                return PersonaGuardResult(
                    text=working,
                    passed=False,
                    failed_reason="fake_paid_claim",
                )
        if not facts.get("media_url_present"):
            sent_markers = (
                "تفضل الباركود",
                "هذا الباركود",
                "هذا باركود",
                "تفضل رمز",
                "هذا رمز الدفع",
                "صورة الباركود",
                "تفضل صورة",
            )
            if any(m in working for m in sent_markers):
                return PersonaGuardResult(
                    text=working,
                    passed=False,
                    failed_reason="media_not_present_claim",
                )
        if not facts.get("allow_receipt_request"):
            receipt_markers = (
                "أرسل الإيصال",
                "أرسل صورة الإيصال",
                "بعد التحويل أرسل",
            )
            if any(m in working for m in receipt_markers):
                return PersonaGuardResult(
                    text=working,
                    passed=False,
                    failed_reason="receipt_ask_on_confirmed",
                )

    if bundle.surface == PERSONA_SURFACE_KB_PRODUCT_ANSWER:
        kb_guard = _apply_kb_product_answer_guards(working, facts)
        if not kb_guard.passed:
            return kb_guard
        working = kb_guard.text

    if bundle.surface == PERSONA_SURFACE_CATALOG_PRODUCT_ANSWER:
        catalog_guard = _apply_catalog_product_answer_guards(working, facts)
        if not catalog_guard.passed:
            return catalog_guard
        working = catalog_guard.text

    if bundle.surface == PERSONA_SURFACE_TRUSTED_COUPON_OFFER_ANSWER:
        coupon_guard = _apply_trusted_coupon_offer_answer_guards(working, facts)
        if not coupon_guard.passed:
            return coupon_guard
        working = coupon_guard.text

    if bundle.surface == PERSONA_SURFACE_CUSTOMER_CONDITIONAL_COUPON_ANSWER:
        conditional_guard = _apply_customer_conditional_coupon_answer_guards(working, facts)
        if not conditional_guard.passed:
            return conditional_guard
        working = conditional_guard.text

    # 5 Checkout-pressure guard
    if bundle.surface in PHASE2_SOCIAL_SURFACES or bundle.surface in {
        PERSONA_SURFACE_KB_PRODUCT_ANSWER,
        PERSONA_SURFACE_CATALOG_PRODUCT_ANSWER,
        PERSONA_SURFACE_TRUSTED_COUPON_OFFER_ANSWER,
        PERSONA_SURFACE_CUSTOMER_CONDITIONAL_COUPON_ANSWER,
    }:
        try:
            from ..postprocess.social_checkout_pressure_guard import (  # noqa: PLC0415
                apply_social_checkout_pressure_guard,
            )

            scpg = apply_social_checkout_pressure_guard(
                working,
                inbound_text=bundle.inbound_text,
                tenant_id=tenant_id or bundle.tenant_id,
            )
            working = (scpg.reply or "").strip()
            if scpg.stripped and not working:
                return PersonaGuardResult(
                    text=working,
                    passed=False,
                    failed_reason="checkout_pressure_empty",
                )
        except Exception:  # noqa: BLE001  # noqa: silent-ok
            pass

    # 6 Known customer re-ask
    working, did_reask = _strip_known_customer_reasks(working, bundle)
    if did_reask:
        repaired = True
    if not working.strip():
        return PersonaGuardResult(
            text="",
            passed=False,
            failed_reason="known_customer_reask_strip",
        )

    # 7 Emoji density
    max_emoji = int(bundle.constraints.max_emojis or 1)
    working, emoji_stripped = _strip_excess_emojis(working, max_emojis=max_emoji)
    if emoji_stripped:
        repaired = True
    from .policy_terms import rejects_fixed_emoji_template_opener  # noqa: PLC0415

    if rejects_fixed_emoji_template_opener(working):
        return PersonaGuardResult(
            text=working,
            passed=False,
            failed_reason="emoji_opener_spam",
        )

    # 8 Length
    if len(working) > bundle.constraints.max_chars:
        working = _truncate_safe(working, bundle.constraints.max_chars)
        repaired = True

    # 9 No silence
    if not working.strip():
        return PersonaGuardResult(text="", passed=False, failed_reason="empty_after_guards")

    from .policy_terms import rejects_social_support_bot_phrase  # noqa: PLC0415

    if bundle.surface in PHASE2_SOCIAL_SURFACES and rejects_social_support_bot_phrase(working):
        return PersonaGuardResult(
            text=working,
            passed=False,
            failed_reason="banned_support_bot_opener",
        )

    return PersonaGuardResult(
        text=working,
        passed=True,
        repaired=repaired,
    )


def apply_guards_or_fallback(
    text: str,
    bundle: PersonaFactsBundle,
    *,
    ctx: Any = None,
    db: Any = None,
    tenant_id: Optional[int] = None,
) -> tuple[str, PersonaGuardResult]:
    """One repair attempt on dialect scrub failures, then deterministic fallback."""
    guard = apply_persona_compose_guards(
        text,
        bundle,
        db=db,
        tenant_id=tenant_id,
    )
    if guard.passed:
        return guard.text, guard

    if bundle.surface == PERSONA_SURFACE_CUSTOMER_CONDITIONAL_COUPON_ANSWER:
        return "", PersonaGuardResult(
            text="",
            passed=False,
            failed_reason=guard.failed_reason or "conditional_coupon_guard_failed",
        )

    if guard.failed_reason == "non_saudi_dialect":
        scrubbed, _ = _scrub_non_saudi_terms(text)
        if scrubbed.strip():
            retry = apply_persona_compose_guards(
                scrubbed,
                bundle,
                db=db,
                tenant_id=tenant_id,
            )
            if retry.passed:
                return retry.text, retry

    fb = deterministic_fallback(bundle, ctx=ctx, reason=guard.failed_reason)
    fb_guard = apply_persona_compose_guards(
        fb,
        bundle,
        db=db,
        tenant_id=tenant_id,
    )
    if fb_guard.passed and fb_guard.text.strip():
        return fb_guard.text, PersonaGuardResult(
            text=fb_guard.text,
            passed=False,
            failed_reason=guard.failed_reason,
        )
    emergency = unicodedata.normalize("NFKC", (fb or "حياك الله 😊").strip())
    return emergency, PersonaGuardResult(
        text=emergency,
        passed=False,
        failed_reason=guard.failed_reason or "fallback_failed",
    )
