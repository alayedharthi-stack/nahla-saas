"""Grounding and read-only guardrails for Commerce Agent V2."""
from __future__ import annotations

import os
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from agents import GuardrailFunctionOutput, RunContextWrapper, input_guardrail, output_guardrail

from modules.ai.commerce_agent_v2.context import CommerceAgentContext
from modules.ai.commerce_agent_v2.output import (
    CanonicalEvidenceFact,
    CommerceReply,
    EvidenceRecord,
    FactClaim,
)


_LEGACY_MARKER_RE = re.compile(r"\[(?:PRODUCT|MEDIA_KEY|CALL):", re.IGNORECASE)
_URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
_SAR_RE = re.compile(r"(?<!\d)(\d+(?:[.,]\d+)?)\s*(?:ريال|ر\.س|SAR)\b", re.IGNORECASE)
_CURRENCY_RE = re.compile(
    r"SAR\b|SR\b|ر\s*\.?\s*س\.?|ريال(?:\s+سعودي)?",
    re.IGNORECASE,
)
_NUMBER_RE = re.compile(r"(?<!\d)(\d+(?:[.,]\d+)?)(?!\d)")
_QUANTITY_RE = re.compile(
    r"(?<!\d)(\d+)\s*(?:قطع(?:ة)?|عبو(?:ة|ات)|حب(?:ة|ات)|وحد(?:ة|ات))\b",
    re.IGNORECASE,
)
_AVAILABILITY_RE = re.compile(
    r"غير\s+(?:متوفر|متاح|موجود|قابل)(?:ة|ه|ان|ين|ون|ات)?(?:\s+للطلب)?\b|"
    r"لا\s+يمكن\s+طلب\w*|(?:نافد|نفد)(?:ة|ه|ان|ين|ون|ات)?\b|"
    r"out\s+of\s+stock\b|unavailable\b|"
    r"(?:متوفر|متاح|موجود)(?:ة|ه|ان|ين|ون|ات)?\b|"
    r"(?:قابل|جاهز)(?:ة|ه|ان|ين|ون|ات)?\s+للطلب\b|يمكن\s+طلب\w*|"
    r"in\s+stock\b|available\b",
    re.IGNORECASE,
)
_ARABIC_DIACRITICS_RE = re.compile(r"[\u064B-\u065F\u0670\u06D6-\u06ED]")
_TOKEN_RE = re.compile(r"[\w\u0600-\u06FF]+", re.UNICODE)
_KNOWLEDGE_FILLER = frozenset(
    {
        "هذا",
        "هذه",
        "ذلك",
        "تلك",
        "هو",
        "هي",
        "من",
        "في",
        "على",
        "عن",
        "الى",
        "مع",
        "لدينا",
        "عندنا",
        "يوجد",
        "يتوفر",
        "تتوفر",
        "the",
        "and",
        "is",
        "are",
        "of",
        "from",
    }
)
_NEGATION_TOKENS = frozenset({"لا", "لم", "لن", "ليس", "ليست", "غير", "بدون"})
_SCOPE_TOKENS = frozenset(
    {"فقط", "حصريا", "كل", "جميع", "بعض", "مختار", "only", "all", "some", "selected"}
)
_PRODUCT_BOUND_KINDS = frozenset(
    {
        "product_name",
        "description",
        "price",
        "currency",
        "sale_price",
        "regular_price",
        "availability",
        "stock_quantity",
        "product_url",
        "image_url",
        "product_knowledge",
    }
)


def _flatten_values(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for nested in value.values():
            yield from _flatten_values(nested)
    elif isinstance(value, (list, tuple, set)):
        for nested in value:
            yield from _flatten_values(nested)
    elif value is not None:
        yield str(value).strip()


def _evidence_values(record: EvidenceRecord) -> set[str]:
    return {value for value in _flatten_values(record.fields) if value}


def _normalize_text(value: Any) -> str:
    text = _ARABIC_DIACRITICS_RE.sub("", str(value or "").strip().casefold())
    text = text.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    text = text.replace("ى", "ي").replace("ة", "ه").replace("ـ", "")
    return " ".join(text.split())


def _light_token(token: str) -> str:
    if token.startswith("ال") and len(token) > 4:
        token = token[2:]
    if token.startswith("و") and len(token) > 4:
        token = token[1:]
    for suffix in ("هما", "كم", "كن", "هم", "هن", "ها", "ه"):
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            return token[: -len(suffix)]
    return token


def _knowledge_tokens(value: str) -> set[str]:
    return {
        _light_token(token)
        for token in _TOKEN_RE.findall(_normalize_text(value))
        if token not in _KNOWLEDGE_FILLER and token not in _NEGATION_TOKENS
    }


def _knowledge_span_supported(record: EvidenceRecord, span: str) -> bool:
    supporting_text = " ".join(
        str(record.fields.get(field) or "") for field in ("title", "body")
    ).strip()
    normalized_span = _normalize_text(span)
    normalized_support = _normalize_text(supporting_text)
    if not normalized_span or not normalized_support:
        return False
    if normalized_span == normalized_support:
        return True

    span_negation = bool(set(_TOKEN_RE.findall(normalized_span)) & _NEGATION_TOKENS)
    support_negation = bool(set(_TOKEN_RE.findall(normalized_support)) & _NEGATION_TOKENS)
    if span_negation != support_negation:
        return False
    span_numbers = set(_NUMBER_RE.findall(normalized_span))
    support_numbers = set(_NUMBER_RE.findall(normalized_support))
    if not span_numbers <= support_numbers:
        return False
    span_tokens = _knowledge_tokens(span)
    support_tokens = _knowledge_tokens(supporting_text)
    body_tokens = _knowledge_tokens(str(record.fields.get("body") or ""))
    span_scope = span_tokens & _SCOPE_TOKENS
    body_scope = body_tokens & _SCOPE_TOKENS
    if span_scope != body_scope:
        return False
    overlap = span_tokens & body_tokens
    return (
        len(span_tokens) >= 2
        and len(overlap) >= 2
        and len(overlap) / len(span_tokens) >= 0.75
        and len(overlap) / len(body_tokens) >= 0.60
        and span_tokens <= support_tokens
    )


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    raw = str(value).strip()
    if "," in raw and "." not in raw:
        whole, fraction = raw.rsplit(",", 1)
        raw = f"{whole}.{fraction}" if len(fraction) <= 2 else raw.replace(",", "")
    else:
        raw = raw.replace(",", "")
    try:
        parsed = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _canonical_currency(value: Any) -> str:
    normalized = _normalize_text(value).replace(".", "").replace(" ", "")
    if normalized in {"sar", "sr", "رس", "ريال", "ريالسعودي"}:
        return "SAR"
    return str(value or "").strip().upper()


def _fact_values_equal(kind: str, claim_value: Any, evidence_value: Any) -> bool:
    if kind in {"price", "sale_price", "regular_price"}:
        return _decimal(claim_value) == _decimal(evidence_value)
    if kind == "currency":
        return _canonical_currency(claim_value) == _canonical_currency(evidence_value)
    if kind in {"availability", "stock_quantity"}:
        return type(claim_value) is type(evidence_value) and claim_value == evidence_value
    if kind in {"product_name", "description"}:
        return _normalize_text(claim_value) == _normalize_text(evidence_value)
    return str(claim_value).strip() == str(evidence_value).strip()


def _matching_evidence_fact(
    record: EvidenceRecord,
    claim: FactClaim,
) -> CanonicalEvidenceFact | None:
    expected_source = {
        "merchant_knowledge": "merchant_knowledge",
        "product_knowledge": "product_knowledge",
    }.get(claim.kind, "catalog_product")
    if record.source != expected_source:
        return None
    if (
        record.source == "catalog_product"
        and claim.subject_product_id is not None
        and record.source_id != str(claim.subject_product_id)
    ):
        return None
    for fact in record.facts:
        if fact.kind != claim.kind:
            continue
        if fact.subject_product_id != claim.subject_product_id:
            continue
        if _fact_values_equal(claim.kind, claim.value, fact.value):
            return fact
    return None


def _span_expresses_claim(
    record: EvidenceRecord,
    claim: FactClaim,
    reply: CommerceReply,
) -> bool:
    span = claim.text_span or ""
    if claim.kind in {"price", "sale_price", "regular_price"}:
        expected = _decimal(claim.value)
        return any(_decimal(value) == expected for value in _NUMBER_RE.findall(span))
    if claim.kind == "currency":
        return _canonical_currency(claim.value) in {
            _canonical_currency(match.group(0)) for match in _CURRENCY_RE.finditer(span)
        }
    if claim.kind == "availability":
        normalized_span = _normalize_text(span)
        states = {
            _availability_value(match.group(0))
            for match in _AVAILABILITY_RE.finditer(normalized_span)
        }
        return claim.value in states
    if claim.kind == "stock_quantity":
        expected = _decimal(claim.value)
        return any(_decimal(value) == expected for value in _NUMBER_RE.findall(span))
    if claim.kind == "product_url":
        return str(claim.value).strip() in span or any(
            action.evidence_ref == claim.evidence_ref
            and str(action.url) == str(claim.value)
            for action in reply.ui_actions
        )
    if claim.kind == "image_url":
        return str(claim.value).strip() in span or any(
            media.evidence_ref == claim.evidence_ref
            and str(media.url) == str(claim.value)
            for media in reply.media_refs
        )
    if claim.kind in {"merchant_knowledge", "product_knowledge"}:
        return _knowledge_span_supported(record, span)
    if claim.kind == "description":
        claim_tokens = _knowledge_tokens(str(claim.value))
        span_tokens = _knowledge_tokens(span)
        claim_negation = bool(
            set(_TOKEN_RE.findall(_normalize_text(str(claim.value)))) & _NEGATION_TOKENS
        )
        span_negation = bool(
            set(_TOKEN_RE.findall(_normalize_text(span))) & _NEGATION_TOKENS
        )
        claim_numbers = {_decimal(value) for value in _NUMBER_RE.findall(str(claim.value))}
        span_numbers = {_decimal(value) for value in _NUMBER_RE.findall(span)}
        overlap = span_tokens & claim_tokens
        return bool(
            len(overlap) >= 2
            and len(overlap) / len(span_tokens) >= 0.60
            and len(overlap) / len(claim_tokens) >= 0.50
            and span_negation == claim_negation
            and span_numbers <= claim_numbers
        )
    claim_tokens = _knowledge_tokens(str(claim.value))
    span_tokens = _knowledge_tokens(span)
    return bool(claim_tokens) and claim_tokens <= span_tokens


def _availability_value(value: str) -> bool:
    normalized = _normalize_text(value)
    return not any(
        term in normalized
        for term in (
            "غير متوفر",
            "غير متاح",
            "غير موجود",
            "غير قابل",
            "لا يمكن طلب",
            "نافد",
            "نفد",
            "unavailable",
            "out of stock",
        )
    )


@input_guardrail(name="commerce_v2_trusted_read_only_scope", run_in_parallel=False)
async def trusted_read_only_scope_guardrail(
    run_context: RunContextWrapper[CommerceAgentContext],
    _agent: Any,
    _input: Any,
) -> GuardrailFunctionOutput:
    context = run_context.context
    reason = ""
    try:
        context.assert_scope()
        if context.capabilities.write_commerce or context.capabilities.outbound_send:
            reason = "phase1_capabilities_are_not_read_only"
    except Exception as exc:  # noqa: BLE001 — guardrail fails closed
        reason = type(exc).__name__
    return GuardrailFunctionOutput(
        output_info={"passed": not reason, "reason": reason},
        tripwire_triggered=bool(reason),
    )


def validate_grounded_reply(
    context: CommerceAgentContext,
    reply: CommerceReply,
) -> list[str]:
    errors: list[str] = []
    if _LEGACY_MARKER_RE.search(reply.text):
        errors.append("legacy_marker_in_text")

    evidence = context.evidence
    referenced = set(reply.evidence_refs)
    nested_refs = {
        *(claim.evidence_ref for claim in reply.fact_claims),
        *(item.evidence_ref for item in reply.product_refs),
        *(item.evidence_ref for item in reply.media_refs),
        *(item.evidence_ref for item in reply.ui_actions),
    }
    undeclared_nested_refs = sorted(nested_refs - referenced)
    if undeclared_nested_refs:
        errors.append("nested_refs_missing_from_evidence_refs:" + ",".join(undeclared_nested_refs))
    missing_refs = sorted(ref for ref in referenced if ref not in evidence)
    if missing_refs:
        errors.append("unknown_evidence_refs:" + ",".join(missing_refs))

    verified_claims: list[FactClaim] = []
    for claim in reply.fact_claims:
        record = evidence.get(claim.evidence_ref)
        if record is None:
            continue
        if not record.facts:
            errors.append("evidence_without_canonical_facts")
            continue
        if claim.kind in _PRODUCT_BOUND_KINDS and claim.subject_product_id is None:
            errors.append(f"missing_claim_subject_product_id:{claim.kind}")
            continue
        if claim.kind == "merchant_knowledge" and claim.subject_product_id is not None:
            errors.append("merchant_claim_has_product_subject")
            continue
        if _matching_evidence_fact(record, claim) is None:
            errors.append(f"claim_not_in_evidence:{claim.kind}")
            continue
        if claim.text_span is None and claim.kind not in {"product_url", "image_url"}:
            errors.append(f"claim_span_missing:{claim.kind}")
            continue
        if claim.text_span is not None and claim.text_span not in reply.text:
            errors.append(f"claim_span_not_in_text:{claim.kind}")
            continue
        if not _span_expresses_claim(record, claim, reply):
            errors.append(f"claim_span_not_equivalent:{claim.kind}")
            continue
        verified_claims.append(claim)

    for product_ref in reply.product_refs:
        record = evidence.get(product_ref.evidence_ref)
        valid_catalog_ref = bool(
            record is not None
            and record.source == "catalog_product"
            and record.source_id == str(product_ref.product_id)
        )
        valid_linked_knowledge_ref = bool(
            record is not None
            and record.source == "product_knowledge"
            and any(
                fact.subject_product_id == product_ref.product_id for fact in record.facts
            )
        )
        if not (valid_catalog_ref or valid_linked_knowledge_ref):
            errors.append("invalid_product_reference")

    for media_ref in reply.media_refs:
        record = evidence.get(media_ref.evidence_ref)
        if record is not None and not any(
            fact.kind == "image_url" and str(fact.value) == str(media_ref.url)
            for fact in record.facts
        ):
            errors.append("media_url_not_in_evidence")
    for action in reply.ui_actions:
        record = evidence.get(action.evidence_ref)
        if record is not None and not any(
            fact.kind == "product_url" and str(fact.value) == str(action.url)
            for fact in record.facts
        ):
            errors.append("action_url_not_in_evidence")

    claimed_urls = {
        str(claim.value)
        for claim in verified_claims
        if claim.kind in {"product_url", "image_url"}
    }
    for url in _URL_RE.findall(reply.text):
        if url.rstrip(".,،؛") not in claimed_urls:
            errors.append("url_in_text_without_verified_claim")

    verified_prices = [
        claim
        for claim in verified_claims
        if claim.kind in {"price", "sale_price", "regular_price"}
    ]
    for match in _SAR_RE.finditer(reply.text):
        amount = _decimal(match.group(1))
        rendered = match.group(0)
        if not any(
            _decimal(claim.value) == amount
            and claim.text_span is not None
            and (rendered in claim.text_span or claim.text_span in rendered)
            for claim in verified_prices
        ):
            errors.append("price_in_text_without_verified_claim")

    verified_quantities = [
        claim for claim in verified_claims if claim.kind == "stock_quantity"
    ]
    for match in _QUANTITY_RE.finditer(reply.text):
        quantity = int(match.group(1))
        rendered = match.group(0)
        if not any(
            claim.value == quantity
            and claim.text_span is not None
            and (rendered in claim.text_span or claim.text_span in rendered)
            for claim in verified_quantities
        ):
            errors.append("stock_quantity_in_text_without_verified_claim")

    if not reply.safe_fallback_reason:
        verified_availability = [
            claim for claim in verified_claims if claim.kind == "availability"
        ]
        for match in _AVAILABILITY_RE.finditer(reply.text):
            availability = _availability_value(match.group(0))
            rendered = match.group(0)
            if not any(
                claim.value is availability
                and claim.text_span is not None
                and rendered in claim.text_span
                for claim in verified_availability
            ):
                errors.append("availability_in_text_without_verified_claim")

    if (reply.fact_claims or reply.product_refs or reply.media_refs or reply.ui_actions) and not referenced:
        errors.append("commercial_output_without_evidence_refs")
    if not evidence and referenced:
        errors.append("references_without_tool_evidence")
    if not evidence and not reply.safe_fallback_reason:
        errors.append("reply_without_tool_evidence_or_safe_fallback")
    if evidence and not referenced and not reply.safe_fallback_reason:
        errors.append("tool_evidence_not_linked_to_reply")
    return sorted(set(errors))


@output_guardrail(name="commerce_v2_grounded_structured_output")
async def grounded_output_guardrail(
    run_context: RunContextWrapper[CommerceAgentContext],
    _agent: Any,
    output: Any,
) -> GuardrailFunctionOutput:
    errors = (
        validate_grounded_reply(run_context.context, output)
        if isinstance(output, CommerceReply)
        else ["malformed_commerce_reply"]
    )
    output_info: dict[str, Any] = {"passed": not errors, "errors": errors}
    if (
        errors
        and isinstance(output, CommerceReply)
        and os.environ.get("NAHLA_RUN_COMMERCE_V2_LIVE_EVAL") == "1"
    ):
        output_info["rejected_eval_reply"] = output.model_dump(mode="json")
    return GuardrailFunctionOutput(
        output_info=output_info,
        tripwire_triggered=bool(errors),
    )


def contains_legacy_marker(value: str) -> bool:
    return bool(_LEGACY_MARKER_RE.search(str(value or "")))
