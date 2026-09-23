"""Grounding and read-only guardrails for Commerce Agent V2."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Any, Iterable

from agents import GuardrailFunctionOutput, RunContextWrapper, input_guardrail, output_guardrail

from modules.ai.commerce_agent_v2.context import CommerceAgentContext
from modules.ai.commerce_agent_v2.output import (
    CanonicalEvidenceFact,
    CommerceReply,
    EvidenceRecord,
    FactClaim,
)
from modules.ai.commerce_agent_v2.url_grounding import (
    canonical_http_url,
    canonical_http_url_equal,
    url_fingerprint,
)


_LEGACY_MARKER_RE = re.compile(r"\[(?:PRODUCT|MEDIA_KEY|CALL):", re.IGNORECASE)
_URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
_SAR_RE = re.compile(r"(?<!\d)(\d+(?:[.,]\d+)?)\s*(?:ريال|ر\.س|SAR)\b", re.IGNORECASE)
_PRICE_UPPER_BOUND_RE = re.compile(
    r"(?:اقل|أقل)\s+من\s*(\d+(?:[.,]\d+)?)\s*(?:ريال|ر\.س|SAR)\b",
    re.IGNORECASE,
)
_CURRENCY_RE = re.compile(
    r"SAR\b|SR\b|ر\s*\.?\s*س\.?|ريال(?:\s+سعودي)?",
    re.IGNORECASE,
)
_NUMBER_RE = re.compile(r"(?<!\d)(\d+(?:[.,]\d+)?)(?!\d)")
_ORDER_ITEM_DUAL_QUANTITY_TOKENS = frozenset(
    {
        "اثنان",
        "اثنين",
        "اثنتان",
        "اثنتين",
        "قطعتان",
        "قطعتين",
        "وحدتان",
        "وحدتين",
        "حبتان",
        "حبتين",
        "عبوتان",
        "عبوتين",
    }
)
_QUANTITY_RE = re.compile(
    r"(?<!\d)(\d+)\s*(?:قطع(?:ة)?|عبو(?:ة|ات)|حب(?:ة|ات)|وحد(?:ة|ات))\b",
    re.IGNORECASE,
)
_CLAUSE_BOUNDARY_RE = re.compile(r"[.!؟\n،؛]|\s+(?:لكن|ولكن|but|however)\s+", re.IGNORECASE)
_STRONG_STOCK_RE = re.compile(
    r"في\s+(?:ال)?مخزون\b|"
    r"(?:نافد|نفد)(?:ة|ه|ان|ين|ون|ات)?(?:\s+(?:ال)?مخزون)?\b|"
    r"غير\s+(?:متوفر|متاح|قابل)(?:ة|ه|ان|ين|ون|ات)?\s+للطلب\b|"
    r"(?:متوفر|متاح|قابل|جاهز)(?:ة|ه|ان|ين|ون|ات)?\s+للطلب\b|"
    r"(?:لا\s+)?يمكن\s+طلب\w*|"
    r"out\s+of\s+stock\b|in\s+stock\b|"
    r"(?:not\s+)?available\s+to\s+order\b|"
    r"(?:can(?:not|'t)?|cannot)\s+be\s+ordered\b",
    re.IGNORECASE,
)
_AMBIGUOUS_AVAILABILITY_RE = re.compile(
    r"غير\s+(?:متوفر|متاح|موجود|قابل)(?:ة|ه|ان|ين|ون|ات)?\b|"
    r"unavailable\b|not\s+available\b|"
    r"(?:متوفر|متاح|موجود)(?:ة|ه|ان|ين|ون|ات)?\b|available\b",
    re.IGNORECASE,
)
_AVAILABILITY_RE = re.compile(
    rf"(?:{_STRONG_STOCK_RE.pattern})|(?:{_AMBIGUOUS_AVAILABILITY_RE.pattern})",
    re.IGNORECASE,
)
_INFORMATIONAL_AVAILABILITY_RE = re.compile(
    r"(?:المعلومات?|البيانات)\s+(?:ال)?(?:متوفر|متاح)(?:ة|ه|ات)?\b|"
    r"(?:ال)?(?:متوفر|متاح)(?:ة|ه|ات)?\s+لدينا\s+(?:ان|أن|من)\b",
    re.IGNORECASE,
)
_TRACKING_SCOPE_RE = re.compile(
    r"(?:رقم|رابط|بيانات|معلومات|تفاصيل)?\s*(?:ال)?تتبع\b|"
    r"\btracking(?:\s+(?:number|link|url|data|details|information))?\b",
    re.IGNORECASE,
)
_SHIPMENT_SCOPE_RE = re.compile(
    r"(?:ال)?شحن(?:ة|ه)?\b|(?:ال)?ناقل\b|شركة\s+(?:ال)?شحن\b|"
    r"\bshipment\b|\bcarrier\b|\bcourier\b",
    re.IGNORECASE,
)
_ORDER_SCOPE_RE = re.compile(
    r"(?:ال)?طلب(?:ك|كم|ها|ه)?\b|\border(?:'s|s)?\b",
    re.IGNORECASE,
)
_INFORMATION_SCOPE_RE = re.compile(
    r"(?:ال)?معلومات?\b|(?:ال)?بيانات\b|(?:ال)?تفاصيل\b|"
    r"\binformation\b|\bdata\b|\bdetails\b",
    re.IGNORECASE,
)
_PRODUCT_SCOPE_RE = re.compile(
    r"(?:ال)?منتج(?:ات|ان|ين|ون|ك|كم|ها|ه)?\b|"
    r"(?:ال)?سلع(?:ة|ه|ات)?\b|(?:ال)?صنف(?:ك|كم|ها|ه)?\b|"
    r"\bproducts?\b|\bitems?\b|\bsku\b",
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
_DESCRIPTION_RELATION_TOKENS = frozenset(
    {"وزن", "الوزن", "حجم", "الحجم", "سعه", "السعه", "capacity", "size", "weight"}
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
_ORDER_BOUND_KINDS = frozenset(
    {
        "order_reference",
        "order_status",
        "order_status_label",
        "order_total",
        "order_currency",
        "order_item_name",
        "order_item_quantity",
        "shipment_status",
        "shipment_status_label",
        "carrier",
        "tracking_number",
        "tracking_url",
        "shipment_latest_event_status",
        "shipment_latest_event_note",
        "shipment_latest_event_location",
        "shipment_latest_event_at",
        "shipment_last_verified_at",
        "shipment_data_source",
    }
)
_ORDER_EVIDENCE_SOURCES = frozenset(
    {"order_summary", "order_details", "order_shipment"}
)
_URL_FACT_KINDS = frozenset({"product_url", "image_url", "tracking_url"})
_EVIDENCE_FREE_FACT_TOKENS = frozenset(
    {
        "منتج",
        "منتجات",
        "المنتج",
        "المنتجات",
        "طلبك",
        "الطلب",
        "شحنه",
        "شحن",
        "الشحن",
        "الناقل",
        "التتبع",
        "product",
        "products",
        "order",
        "shipment",
        "carrier",
        "tracking",
    }
)
_STORE_POSSESSION_TOKENS = frozenset({"عندنا", "لدينا", "نبيع", "نوفر"})
_STORE_LOCATION_SUBJECT_TOKENS = frozenset(
    {"نحن", "متجرنا", "موقعنا", "فرعنا", "مقرنا"}
)


@dataclass(frozen=True)
class _AvailabilityMention:
    match: re.Match[str]
    clause_start: int
    clause_end: int
    scope: str
    availability: bool
    strong_stock_expression: bool


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
    if token.startswith("و") and len(token) > 4 and not token.startswith("وزن"):
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


def _contains_evidence_free_factual_assertion(value: str) -> bool:
    """Detect store/commerce assertions that social mode must never carry.

    This is an output truth guard, not an inbound intent route. It deliberately
    operates only after the model has proposed evidence-free customer text.
    """
    tokens = _TOKEN_RE.findall(_normalize_text(value))
    # A bare commerce-domain noun inside a question is not itself a factual
    # assertion (for example: "أي منتج تقصد؟"). Price, quantity, availability,
    # and URL questions remain governed by their dedicated scanners.
    if set(tokens) & _EVIDENCE_FREE_FACT_TOKENS and not (
        "?" in value or "؟" in value
    ):
        return True
    if any(token in _STORE_POSSESSION_TOKENS for token in tokens[:-1]):
        return True
    for index, token in enumerate(tokens[:-1]):
        if token not in _STORE_LOCATION_SUBJECT_TOKENS:
            continue
        tail = tokens[index + 1 : index + 4]
        if "في" in tail:
            return True
    return False


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
    if kind in {"price", "sale_price", "regular_price", "order_total"}:
        return _decimal(claim_value) == _decimal(evidence_value)
    if kind in {"currency", "order_currency"}:
        return _canonical_currency(claim_value) == _canonical_currency(evidence_value)
    if kind in {"availability", "stock_quantity", "order_item_quantity"}:
        return type(claim_value) is type(evidence_value) and claim_value == evidence_value
    if kind in {"product_name", "description"}:
        return _normalize_text(claim_value) == _normalize_text(evidence_value)
    if kind in _URL_FACT_KINDS:
        return canonical_http_url_equal(claim_value, evidence_value)
    return str(claim_value).strip() == str(evidence_value).strip()


def _expected_evidence_sources(kind: str) -> frozenset[str]:
    return {
        "merchant_knowledge": frozenset({"merchant_knowledge"}),
        "product_knowledge": frozenset({"product_knowledge"}),
        "order_reference": _ORDER_EVIDENCE_SOURCES,
        "order_status": frozenset({"order_summary"}),
        "order_status_label": frozenset({"order_summary"}),
        "order_total": frozenset({"order_details"}),
        "order_currency": frozenset({"order_details"}),
        "order_item_name": frozenset({"order_details"}),
        "order_item_quantity": frozenset({"order_details"}),
        "shipment_status": frozenset({"order_shipment"}),
        "shipment_status_label": frozenset({"order_shipment"}),
        "carrier": frozenset({"order_shipment"}),
        "tracking_number": frozenset({"order_shipment"}),
        "tracking_url": frozenset({"order_shipment"}),
        "shipment_latest_event_status": frozenset({"order_shipment"}),
        "shipment_latest_event_note": frozenset({"order_shipment"}),
        "shipment_latest_event_location": frozenset({"order_shipment"}),
        "shipment_latest_event_at": frozenset({"order_shipment"}),
        "shipment_last_verified_at": frozenset({"order_shipment"}),
        "shipment_data_source": frozenset({"order_shipment"}),
    }.get(kind, frozenset({"catalog_product"}))


def _fact_subject_binding_matches(
    record: EvidenceRecord,
    claim: FactClaim,
    fact: CanonicalEvidenceFact,
) -> bool:
    if record.source not in _expected_evidence_sources(claim.kind):
        return False
    if (
        record.source == "catalog_product"
        and claim.subject_product_id is not None
        and record.source_id != str(claim.subject_product_id)
    ):
        return False
    if (
        record.source in _ORDER_EVIDENCE_SOURCES
        and claim.subject_order_id is not None
        and record.source_id != str(claim.subject_order_id)
    ):
        return False
    return bool(
        fact.kind == claim.kind
        and fact.subject_product_id == claim.subject_product_id
        and fact.subject_order_id == claim.subject_order_id
    )


def _matching_evidence_fact(
    record: EvidenceRecord,
    claim: FactClaim,
) -> CanonicalEvidenceFact | None:
    for fact in record.facts:
        if not _fact_subject_binding_matches(record, claim, fact):
            continue
        if _fact_values_equal(claim.kind, claim.value, fact.value):
            return fact
    return None


def _action_url_has_bound_evidence(record: EvidenceRecord | None, action: Any) -> bool:
    """Validate an action directly against its canonical, subject-bound evidence.

    UI actions already carry an evidence reference. Requiring a second, duplicate
    URL ``FactClaim`` made otherwise valid catalog actions fail closed whenever
    the model emitted the action without repeating the URL claim. The record
    source and canonical fact subject remain mandatory here, so the action cannot
    borrow the same URL from another product or order.
    """
    if record is None:
        return False
    expected_kind = "product_url" if action.kind == "open_product" else "tracking_url"
    expected_source = "catalog_product" if action.kind == "open_product" else "order_shipment"
    if record.source != expected_source:
        return False
    for fact in record.facts:
        if fact.kind != expected_kind or not canonical_http_url_equal(fact.value, action.url):
            continue
        subject_id = (
            fact.subject_product_id
            if action.kind == "open_product"
            else fact.subject_order_id
        )
        if subject_id is not None and record.source_id == str(subject_id):
            return True
    return False


def _media_url_has_bound_evidence(
    record: EvidenceRecord | None,
    media_ref: Any,
) -> bool:
    """Validate catalog media directly against its subject-bound evidence.

    ``MediaReference`` intentionally carries only the URL and evidence reference.
    Requiring the model to repeat an image-only ``FactClaim`` makes an otherwise
    exact catalog image fail closed.  The canonical evidence record remains the
    authority: it must be a catalog product, the URL must match exactly after
    canonicalization, and the fact's product subject must match the record.
    """
    if record is None or record.source != "catalog_product":
        return False
    return any(
        fact.kind == "image_url"
        and canonical_http_url_equal(fact.value, media_ref.url)
        and fact.subject_product_id is not None
        and record.source_id == str(fact.subject_product_id)
        for fact in record.facts
    )


def _span_expresses_claim(
    context: CommerceAgentContext,
    record: EvidenceRecord,
    claim: FactClaim,
    reply: CommerceReply,
) -> bool:
    span = claim.text_span or ""
    if claim.kind in {"price", "sale_price", "regular_price", "order_total"}:
        expected = _decimal(claim.value)
        return any(_decimal(value) == expected for value in _NUMBER_RE.findall(span))
    if claim.kind in {"currency", "order_currency"}:
        return _canonical_currency(claim.value) in {
            _canonical_currency(match.group(0)) for match in _CURRENCY_RE.finditer(span)
        }
    if claim.kind == "availability":
        states: set[bool] = set()
        search_start = 0
        while True:
            span_start = reply.text.find(span, search_start)
            if span_start < 0:
                break
            span_end = span_start + len(span)
            for match in _AVAILABILITY_RE.finditer(reply.text, span_start, span_end):
                mention = _availability_semantic_scope(
                    context,
                    reply,
                    match,
                    [claim],
                )
                if mention.scope in {"PRODUCT", "UNKNOWN"}:
                    states.add(mention.availability)
            search_start = span_end
        return claim.value in states
    if claim.kind == "stock_quantity":
        expected = _decimal(claim.value)
        return any(_decimal(value) == expected for value in _NUMBER_RE.findall(span))
    if claim.kind == "order_item_quantity":
        expected = _decimal(claim.value)
        if any(_decimal(value) == expected for value in _NUMBER_RE.findall(span)):
            return True
        # Arabic naturally expresses a quantity of two with a dual noun and no
        # digit (for example, "قطعتين"). Accept only explicit dual quantity
        # tokens; the canonical claim must still exactly match trusted order
        # evidence before this span check runs.
        tokens = set(_TOKEN_RE.findall(_normalize_text(span)))
        return expected == Decimal(2) and bool(
            tokens & _ORDER_ITEM_DUAL_QUANTITY_TOKENS
        )
    if claim.kind in {"product_url", "tracking_url"}:
        return any(
            canonical_http_url_equal(url.rstrip(".,،؛"), claim.value)
            for url in _URL_RE.findall(span)
        ) or any(
            action.evidence_ref == claim.evidence_ref
            and canonical_http_url_equal(action.url, claim.value)
            for action in reply.ui_actions
        )
    if claim.kind in {"order_status", "shipment_status"}:
        if _normalize_text(claim.value) == _normalize_text(span):
            return True
        label_kind = (
            "order_status_label"
            if claim.kind == "order_status"
            else "shipment_status_label"
        )
        return any(
            fact.kind == label_kind
            and _normalize_text(fact.value) == _normalize_text(span)
            for fact in record.facts
        )
    if claim.kind == "image_url":
        return any(
            canonical_http_url_equal(url.rstrip(".,،؛"), claim.value)
            for url in _URL_RE.findall(span)
        ) or any(
            media.evidence_ref == claim.evidence_ref
            and canonical_http_url_equal(media.url, claim.value)
            for media in reply.media_refs
        )
    if claim.kind in {"merchant_knowledge", "product_knowledge"}:
        return _knowledge_span_supported(record, span)
    if claim.kind == "description":
        claim_tokens = _knowledge_tokens(str(claim.value))
        span_tokens = _knowledge_tokens(span) - _DESCRIPTION_RELATION_TOKENS
        claim_negation = bool(
            set(_TOKEN_RE.findall(_normalize_text(str(claim.value)))) & _NEGATION_TOKENS
        )
        span_negation = bool(
            set(_TOKEN_RE.findall(_normalize_text(span))) & _NEGATION_TOKENS
        )
        claim_numbers = {_decimal(value) for value in _NUMBER_RE.findall(str(claim.value))}
        span_numbers = {_decimal(value) for value in _NUMBER_RE.findall(span)}
        return bool(
            span_tokens
            and span_tokens <= claim_tokens
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
            "cannot be ordered",
            "can't be ordered",
            "not available",
            "نافد",
            "نفد",
            "unavailable",
            "out of stock",
        )
    )


def _clause_bounds(text: str, mention: re.Match[str]) -> tuple[int, int]:
    previous_boundaries = list(_CLAUSE_BOUNDARY_RE.finditer(text, 0, mention.start()))
    clause_start = previous_boundaries[-1].end() if previous_boundaries else 0
    next_boundary = _CLAUSE_BOUNDARY_RE.search(text, mention.end())
    clause_end = next_boundary.start() if next_boundary else len(text)
    return clause_start, clause_end


def _spans_overlap(start: int, end: int, span_start: int, span_end: int) -> bool:
    return start < span_end and span_start < end


def _clause_has_verified_kind(
    reply: CommerceReply,
    verified_claims: Iterable[FactClaim],
    *,
    clause_start: int,
    clause_end: int,
    kinds: frozenset[str],
) -> bool:
    for claim in verified_claims:
        if claim.kind not in kinds or not claim.text_span:
            continue
        search_start = 0
        while True:
            span_start = reply.text.find(claim.text_span, search_start)
            if span_start < 0:
                break
            span_end = span_start + len(claim.text_span)
            if _spans_overlap(clause_start, clause_end, span_start, span_end):
                return True
            search_start = span_end
    return False


def _clause_mentions_referenced_product(
    context: CommerceAgentContext,
    reply: CommerceReply,
    clause: str,
) -> bool:
    normalized_clause = _normalize_text(clause)
    clause_tokens = _knowledge_tokens(clause)
    for evidence_ref in reply.evidence_refs:
        record = context.evidence.get(evidence_ref)
        if record is None or record.source not in {"catalog_product", "product_knowledge"}:
            continue
        for fact in record.facts:
            if fact.kind != "product_name":
                continue
            normalized_name = _normalize_text(fact.value)
            name_tokens = _knowledge_tokens(str(fact.value))
            if normalized_name and normalized_name in normalized_clause:
                return True
            if len(name_tokens) >= 2 and name_tokens <= clause_tokens:
                return True
    return False


def _scope_signal_distance(
    mention_start: int,
    mention_end: int,
    signal: re.Match[str],
) -> int:
    if signal.end() <= mention_start:
        return mention_start - signal.end()
    if signal.start() >= mention_end:
        return signal.start() - mention_end
    return 0


def _nearest_explicit_scope(
    clause: str,
    *,
    mention_start: int,
    mention_end: int,
    strong_stock_expression: bool,
) -> str | None:
    candidates: list[tuple[int, int, str]] = []
    scope_patterns = (
        ("TRACKING", _TRACKING_SCOPE_RE),
        ("SHIPMENT", _SHIPMENT_SCOPE_RE),
        ("ORDER", _ORDER_SCOPE_RE),
        ("INFORMATION", _INFORMATION_SCOPE_RE),
        ("PRODUCT", _PRODUCT_SCOPE_RE),
    )
    priority = {
        "TRACKING": 0,
        "SHIPMENT": 1,
        "ORDER": 2,
        "INFORMATION": 3,
        "PRODUCT": 4,
    }
    for scope, pattern in scope_patterns:
        for signal in pattern.finditer(clause):
            if (
                scope == "ORDER"
                and strong_stock_expression
                and _spans_overlap(
                    mention_start,
                    mention_end,
                    signal.start(),
                    signal.end(),
                )
            ):
                # The noun in ``متاح للطلب`` means orderability, not a
                # customer order. Other order signals in the clause remain.
                continue
            candidates.append(
                (
                    _scope_signal_distance(mention_start, mention_end, signal),
                    priority[scope],
                    scope,
                )
            )
    return min(candidates)[2] if candidates else None


def _availability_semantic_scope(
    context: CommerceAgentContext,
    reply: CommerceReply,
    mention: re.Match[str],
    verified_claims: Iterable[FactClaim],
) -> _AvailabilityMention:
    clause_start, clause_end = _clause_bounds(reply.text, mention)
    clause = reply.text[clause_start:clause_end]
    rendered = mention.group(0)
    strong_stock_expression = bool(_STRONG_STOCK_RE.fullmatch(rendered))
    local_start = mention.start() - clause_start
    local_end = mention.end() - clause_start
    explicit_scope = _nearest_explicit_scope(
        clause,
        mention_start=local_start,
        mention_end=local_end,
        strong_stock_expression=strong_stock_expression,
    )

    if any(
        candidate.start() <= mention.start() < candidate.end()
        for candidate in _INFORMATIONAL_AVAILABILITY_RE.finditer(reply.text)
    ):
        scope = "INFORMATION"
    elif explicit_scope is not None:
        scope = explicit_scope
    elif strong_stock_expression:
        scope = "PRODUCT"
    elif _clause_has_verified_kind(
        reply,
        verified_claims,
        clause_start=clause_start,
        clause_end=clause_end,
        kinds=_PRODUCT_BOUND_KINDS,
    ) or _clause_mentions_referenced_product(context, reply, clause):
        scope = "PRODUCT"
    elif _clause_has_verified_kind(
        reply,
        verified_claims,
        clause_start=clause_start,
        clause_end=clause_end,
        kinds=frozenset({"merchant_knowledge"}),
    ):
        scope = "INFORMATION"
    else:
        scope = "UNKNOWN"

    return _AvailabilityMention(
        match=mention,
        clause_start=clause_start,
        clause_end=clause_end,
        scope=scope,
        availability=_availability_value(rendered),
        strong_stock_expression=strong_stock_expression,
    )


def _availability_mention_is_verified(
    text: str,
    mention: _AvailabilityMention,
    *,
    verified_availability: Iterable[FactClaim],
    verified_quantities: Iterable[FactClaim],
) -> bool:
    if mention.scope in {"ORDER", "SHIPMENT", "TRACKING", "INFORMATION"}:
        return True
    rendered = mention.match.group(0)
    return any(
        claim.value is mention.availability
        and claim.text_span is not None
        and rendered in claim.text_span
        for claim in verified_availability
    ) or _availability_mention_is_quantity_bound(
        text,
        mention.match,
        availability=mention.availability,
        verified_availability=verified_availability,
        verified_quantities=verified_quantities,
    )


def _quantity_match_is_verified(
    match: re.Match[str],
    verified_quantities: Iterable[FactClaim],
) -> bool:
    quantity = int(match.group(1))
    rendered = match.group(0)
    return any(
        claim.value == quantity
        and claim.text_span is not None
        and (rendered in claim.text_span or claim.text_span in rendered)
        for claim in verified_quantities
    )


def _availability_mention_is_quantity_bound(
    text: str,
    mention: re.Match[str],
    *,
    availability: bool,
    verified_availability: Iterable[FactClaim],
    verified_quantities: Iterable[FactClaim],
) -> bool:
    """Accept a repeated stock-state word only when typed quantity facts bind it.

    Natural Arabic often renders the same evidence as ``متوفر لدينا`` followed
    by ``المتاح حاليًا 8 عبوات``. The second availability word need not repeat
    the availability claim span when a verified quantity in the same clause and
    a same-product availability claim jointly support it.
    """
    previous_boundaries = list(
        _CLAUSE_BOUNDARY_RE.finditer(text, 0, mention.start())
    )
    clause_start = previous_boundaries[-1].end() if previous_boundaries else 0
    next_boundary = _CLAUSE_BOUNDARY_RE.search(text, mention.end())
    clause_end = next_boundary.start() if next_boundary else len(text)

    for quantity_match in _QUANTITY_RE.finditer(text, clause_start, clause_end):
        quantity = int(quantity_match.group(1))
        rendered = quantity_match.group(0)
        matching_quantities = [
            claim
            for claim in verified_quantities
            if claim.value == quantity
            and claim.text_span is not None
            and (rendered in claim.text_span or claim.text_span in rendered)
        ]
        for quantity_claim in matching_quantities:
            if any(
                availability_claim.value is availability
                and availability_claim.evidence_ref == quantity_claim.evidence_ref
                and availability_claim.subject_product_id
                == quantity_claim.subject_product_id
                for availability_claim in verified_availability
            ):
                return True
    return False


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


def _verify_fact_claims(
    context: CommerceAgentContext,
    reply: CommerceReply,
) -> tuple[list[str], list[FactClaim]]:
    errors: list[str] = []
    verified_claims: list[FactClaim] = []
    evidence = context.evidence
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
        if claim.kind in _PRODUCT_BOUND_KINDS and claim.subject_order_id is not None:
            errors.append(f"product_claim_has_order_subject:{claim.kind}")
            continue
        if claim.kind in _ORDER_BOUND_KINDS and claim.subject_order_id is None:
            errors.append(f"missing_claim_subject_order_id:{claim.kind}")
            continue
        if claim.kind in _ORDER_BOUND_KINDS and claim.subject_product_id is not None:
            errors.append(f"order_claim_has_product_subject:{claim.kind}")
            continue
        if claim.kind == "merchant_knowledge" and claim.subject_product_id is not None:
            errors.append("merchant_claim_has_product_subject")
            continue
        if claim.kind == "merchant_knowledge" and claim.subject_order_id is not None:
            errors.append("merchant_claim_has_order_subject")
            continue
        if _matching_evidence_fact(record, claim) is None:
            errors.append(f"claim_not_in_evidence:{claim.kind}")
            continue
        if claim.text_span is None and claim.kind not in {
            "product_url",
            "image_url",
            "tracking_url",
        }:
            errors.append(f"claim_span_missing:{claim.kind}")
            continue
        if claim.text_span is not None and claim.text_span not in reply.text:
            errors.append(f"claim_span_not_in_text:{claim.kind}")
            continue
        if not _span_expresses_claim(context, record, claim, reply):
            card_fact_is_evidence_bound = bool(
                record.source == "catalog_product"
                and claim.kind in {"availability", "stock_quantity"}
                and any(
                    product_ref.evidence_ref == claim.evidence_ref
                    and product_ref.product_id == claim.subject_product_id
                    for product_ref in reply.product_refs
                )
            )
            if card_fact_is_evidence_bound:
                # Catalog cards render these fields from canonical evidence, not
                # from the model's span. Keep the exact evidence-bound claim so
                # structured presentation can proceed. The independent text
                # scanners below still reject any visible availability/quantity
                # wording unless this span itself expresses the value.
                verified_claims.append(claim)
                continue
            errors.append(f"claim_span_not_equivalent:{claim.kind}")
            continue
        verified_claims.append(claim)
    return errors, verified_claims


def validate_grounded_reply(
    context: CommerceAgentContext,
    reply: CommerceReply,
) -> list[str]:
    errors: list[str] = []
    if _LEGACY_MARKER_RE.search(reply.text):
        errors.append("legacy_marker_in_text")

    evidence = context.evidence
    referenced = set(reply.evidence_refs)
    structured_commerce = bool(
        reply.fact_claims or reply.product_refs or reply.media_refs or reply.ui_actions
    )
    if reply.response_mode == "social":
        if evidence:
            errors.append("social_reply_with_tool_evidence")
        if referenced or structured_commerce:
            errors.append("social_reply_with_commercial_structure")
        if reply.safe_fallback_reason:
            errors.append("social_reply_with_safe_fallback_reason")
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

    claim_errors, verified_claims = _verify_fact_claims(context, reply)
    errors.extend(claim_errors)

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
        linked_claim_is_verified = any(
            claim.kind == "image_url"
            and claim.evidence_ref == media_ref.evidence_ref
            and canonical_http_url_equal(claim.value, media_ref.url)
            for claim in verified_claims
        )
        if record is not None and not (
            linked_claim_is_verified
            or _media_url_has_bound_evidence(record, media_ref)
        ):
            errors.append("media_url_not_in_evidence")
    for action in reply.ui_actions:
        record = evidence.get(action.evidence_ref)
        expected_kind = "product_url" if action.kind == "open_product" else "tracking_url"
        linked_claims = [
            claim
            for claim in reply.fact_claims
            if claim.kind == expected_kind and claim.evidence_ref == action.evidence_ref
        ]
        action_is_grounded = (
            any(
                claim in verified_claims
                and canonical_http_url_equal(claim.value, action.url)
                for claim in linked_claims
            )
            if linked_claims
            else _action_url_has_bound_evidence(record, action)
        )
        if record is not None and not action_is_grounded:
            errors.append("action_url_not_in_evidence")

    claimed_urls = {
        canonical_http_url(claim.value)
        for claim in verified_claims
        if claim.kind in _URL_FACT_KINDS
        and canonical_http_url(claim.value) is not None
    }
    for url in _URL_RE.findall(reply.text):
        if canonical_http_url(url.rstrip(".,،؛")) not in claimed_urls:
            errors.append("url_in_text_without_verified_claim")

    verified_prices = [
        claim
        for claim in verified_claims
        if claim.kind in {"price", "sale_price", "regular_price", "order_total"}
    ]
    verified_upper_bounds = [
        (match.span(1), _decimal(match.group(1)))
        for match in _PRICE_UPPER_BOUND_RE.finditer(reply.text)
    ]
    for match in _SAR_RE.finditer(reply.text):
        amount = _decimal(match.group(1))
        rendered = match.group(0)
        exact_price_verified = any(
            _decimal(claim.value) == amount
            and claim.text_span is not None
            and (rendered in claim.text_span or claim.text_span in rendered)
            for claim in verified_prices
        )
        derived_upper_bound_verified = any(
            amount == bound
            and match.span(1) == bound_span
            and any(
                price is not None and bound is not None and price < bound
                for price in (_decimal(claim.value) for claim in verified_prices)
            )
            for bound_span, bound in verified_upper_bounds
        )
        if not exact_price_verified and not derived_upper_bound_verified:
            errors.append("price_in_text_without_verified_claim")

    verified_quantities = [
        claim
        for claim in verified_claims
        if claim.kind in {"stock_quantity", "order_item_quantity"}
    ]
    for match in _QUANTITY_RE.finditer(reply.text):
        if not _quantity_match_is_verified(match, verified_quantities):
            errors.append("stock_quantity_in_text_without_verified_claim")

    verified_availability = [
        claim for claim in verified_claims if claim.kind == "availability"
    ]
    for match in _AVAILABILITY_RE.finditer(reply.text):
        mention = _availability_semantic_scope(
            context,
            reply,
            match,
            verified_claims,
        )
        if not _availability_mention_is_verified(
            reply.text,
            mention,
            verified_availability=verified_availability,
            verified_quantities=verified_quantities,
        ):
            errors.append("availability_in_text_without_verified_claim")

    if structured_commerce and not referenced:
        errors.append("commercial_output_without_evidence_refs")
    if not evidence and referenced:
        errors.append("references_without_tool_evidence")
    if (
        not evidence
        and (reply.response_mode == "social" or reply.safe_fallback_reason)
        and _contains_evidence_free_factual_assertion(reply.text)
    ):
        errors.append("evidence_free_commercial_or_factual_claim")
    if (
        not evidence
        and reply.response_mode != "social"
        and not reply.safe_fallback_reason
    ):
        errors.append("reply_without_tool_evidence_or_safe_fallback")
    if evidence and not referenced and not reply.safe_fallback_reason:
        errors.append("tool_evidence_not_linked_to_reply")
    referenced_order_evidence = {
        ref
        for ref in referenced
        if evidence.get(ref) is not None
        and evidence[ref].source in _ORDER_EVIDENCE_SOURCES
    }
    verified_order_refs = {
        claim.evidence_ref
        for claim in verified_claims
        if claim.kind in _ORDER_BOUND_KINDS
    }
    verified_order_action_refs = {
        action.evidence_ref
        for action in reply.ui_actions
        if action.kind == "track_shipment"
        and action.evidence_ref in referenced_order_evidence
    }
    if (
        referenced_order_evidence
        and not reply.safe_fallback_reason
        and not referenced_order_evidence
        <= (verified_order_refs | verified_order_action_refs)
    ):
        errors.append("order_evidence_without_verified_claim")
    return sorted(set(errors))


def _url_comparison_diagnostic(
    *,
    candidate_url: object,
    evidence_url: object,
    comparison_stage: str,
    claim_kind: str,
    evidence_ref: str,
    subject_product_id: int | None,
    subject_order_id: int | None,
    action_kind: str | None,
    evidence_ref_exists: bool,
    subject_binding_matches: bool,
) -> dict[str, Any]:
    candidate = candidate_url if isinstance(candidate_url, str) else None
    evidence = evidence_url if isinstance(evidence_url, str) else None
    candidate_canonical = canonical_http_url(candidate)
    evidence_canonical = canonical_http_url(evidence)
    return {
        "comparison_stage": comparison_stage,
        "claim_kind": claim_kind,
        "evidence_ref": evidence_ref,
        "subject_product_id": subject_product_id,
        "subject_order_id": subject_order_id,
        "action_kind": action_kind,
        "evidence_ref_exists": evidence_ref_exists,
        "subject_binding_matches": subject_binding_matches,
        "raw_url_sha256": url_fingerprint(candidate),
        "canonical_url_sha256": url_fingerprint(candidate_canonical),
        "evidence_raw_url_sha256": url_fingerprint(evidence),
        "evidence_canonical_url_sha256": url_fingerprint(evidence_canonical),
        "canonical_equal": canonical_http_url_equal(candidate, evidence),
    }


def _availability_rejection_diagnostics(
    context: CommerceAgentContext,
    reply: CommerceReply,
) -> list[dict[str, Any]]:
    _, verified_claims = _verify_fact_claims(context, reply)
    verified_availability = [
        claim for claim in verified_claims if claim.kind == "availability"
    ]
    verified_quantities = [
        claim
        for claim in verified_claims
        if claim.kind in {"stock_quantity", "order_item_quantity"}
    ]
    referenced_records = [
        context.evidence[ref]
        for ref in reply.evidence_refs
        if ref in context.evidence
    ]
    evidence_source_types = sorted({record.source for record in referenced_records})
    referenced_claim_kinds = sorted({claim.kind for claim in reply.fact_claims})
    verified_claim_kinds = sorted({claim.kind for claim in verified_claims})
    product_subject_present = bool(
        reply.product_refs
        or any(claim.subject_product_id is not None for claim in reply.fact_claims)
        or any(
            fact.subject_product_id is not None
            for record in referenced_records
            for fact in record.facts
        )
    )
    order_subject_present = bool(
        any(claim.subject_order_id is not None for claim in reply.fact_claims)
        or any(
            fact.subject_order_id is not None
            for record in referenced_records
            for fact in record.facts
        )
    )

    diagnostics: list[dict[str, Any]] = []
    for match in _AVAILABILITY_RE.finditer(reply.text):
        mention = _availability_semantic_scope(
            context,
            reply,
            match,
            verified_claims,
        )
        if _availability_mention_is_verified(
            reply.text,
            mention,
            verified_availability=verified_availability,
            verified_quantities=verified_quantities,
        ):
            continue
        normalized_clause = _normalize_text(
            reply.text[mention.clause_start : mention.clause_end]
        )
        diagnostics.append(
            {
                "detector_type": "product_availability_semantic_scope",
                "matched_normalized_lexeme": _normalize_text(match.group(0)),
                "semantic_scope": mention.scope,
                "strong_stock_expression": mention.strong_stock_expression,
                "referenced_evidence_source_types": evidence_source_types,
                "referenced_claim_kinds": referenced_claim_kinds,
                "verified_claim_kinds": verified_claim_kinds,
                "product_subject_present": product_subject_present,
                "order_subject_present": order_subject_present,
                "clause_sha256": sha256(normalized_clause.encode("utf-8")).hexdigest(),
                "guardrail_code": "availability_in_text_without_verified_claim",
            }
        )
    return diagnostics[:32]


def _safe_rejected_output_diagnostic(
    context: CommerceAgentContext,
    reply: CommerceReply,
    errors: list[str],
) -> dict[str, Any]:
    """Summarize rejected contracts without storing customer-visible content."""
    evidence = context.evidence
    comparisons: list[dict[str, Any]] = []

    for claim in reply.fact_claims:
        if claim.kind not in _URL_FACT_KINDS:
            continue
        record = evidence.get(claim.evidence_ref)
        candidate_facts = (
            [fact for fact in record.facts if fact.kind == claim.kind]
            if record is not None
            else []
        )
        if not candidate_facts:
            candidate_facts = [None]
        for fact in candidate_facts:
            comparisons.append(
                _url_comparison_diagnostic(
                    candidate_url=claim.value,
                    evidence_url=fact.value if fact is not None else None,
                    comparison_stage="fact_claim_to_evidence",
                    claim_kind=claim.kind,
                    evidence_ref=claim.evidence_ref,
                    subject_product_id=claim.subject_product_id,
                    subject_order_id=claim.subject_order_id,
                    action_kind=None,
                    evidence_ref_exists=record is not None,
                    subject_binding_matches=bool(
                        record is not None
                        and fact is not None
                        and _fact_subject_binding_matches(record, claim, fact)
                    ),
                )
            )

        representations: Iterable[tuple[str, str, object]]
        if claim.kind == "image_url":
            representations = (
                ("fact_claim_to_media_reference", "image", media.url)
                for media in reply.media_refs
                if media.evidence_ref == claim.evidence_ref
            )
        else:
            expected_action = (
                "open_product" if claim.kind == "product_url" else "track_shipment"
            )
            representations = (
                ("fact_claim_to_ui_action", action.kind, action.url)
                for action in reply.ui_actions
                if action.evidence_ref == claim.evidence_ref
                and action.kind == expected_action
            )
        for stage, action_kind, represented_url in representations:
            comparisons.append(
                _url_comparison_diagnostic(
                    candidate_url=represented_url,
                    evidence_url=claim.value,
                    comparison_stage=stage,
                    claim_kind=claim.kind,
                    evidence_ref=claim.evidence_ref,
                    subject_product_id=claim.subject_product_id,
                    subject_order_id=claim.subject_order_id,
                    action_kind=action_kind,
                    evidence_ref_exists=record is not None,
                    subject_binding_matches=any(
                        _fact_subject_binding_matches(record, claim, fact)
                        for fact in candidate_facts
                        if record is not None and fact is not None
                    ),
                )
            )

    for action in reply.ui_actions:
        expected_kind = "product_url" if action.kind == "open_product" else "tracking_url"
        record = evidence.get(action.evidence_ref)
        facts = (
            [fact for fact in record.facts if fact.kind == expected_kind]
            if record is not None
            else []
        ) or [None]
        linked_claim = next(
            (
                claim
                for claim in reply.fact_claims
                if claim.evidence_ref == action.evidence_ref and claim.kind == expected_kind
            ),
            None,
        )
        for fact in facts:
            comparisons.append(
                _url_comparison_diagnostic(
                    candidate_url=action.url,
                    evidence_url=fact.value if fact is not None else None,
                    comparison_stage="ui_action_to_evidence",
                    claim_kind=expected_kind,
                    evidence_ref=action.evidence_ref,
                    subject_product_id=(
                        linked_claim.subject_product_id if linked_claim is not None else None
                    ),
                    subject_order_id=(
                        linked_claim.subject_order_id if linked_claim is not None else None
                    ),
                    action_kind=action.kind,
                    evidence_ref_exists=record is not None,
                    subject_binding_matches=bool(
                        record is not None
                        and fact is not None
                        and linked_claim is not None
                        and _fact_subject_binding_matches(record, linked_claim, fact)
                    ),
                )
            )

    for media in reply.media_refs:
        record = evidence.get(media.evidence_ref)
        facts = (
            [fact for fact in record.facts if fact.kind == "image_url"]
            if record is not None
            else []
        ) or [None]
        linked_claim = next(
            (
                claim
                for claim in reply.fact_claims
                if claim.evidence_ref == media.evidence_ref and claim.kind == "image_url"
            ),
            None,
        )
        for fact in facts:
            comparisons.append(
                _url_comparison_diagnostic(
                    candidate_url=media.url,
                    evidence_url=fact.value if fact is not None else None,
                    comparison_stage="media_reference_to_evidence",
                    claim_kind="image_url",
                    evidence_ref=media.evidence_ref,
                    subject_product_id=(
                        linked_claim.subject_product_id if linked_claim is not None else None
                    ),
                    subject_order_id=None,
                    action_kind="image",
                    evidence_ref_exists=record is not None,
                    subject_binding_matches=bool(
                        record is not None
                        and fact is not None
                        and linked_claim is not None
                        and _fact_subject_binding_matches(record, linked_claim, fact)
                    ),
                )
            )

    availability_diagnostics = (
        _availability_rejection_diagnostics(context, reply)
        if "availability_in_text_without_verified_claim" in errors
        else []
    )
    return {
        "artifact": "rejected_model_commerce_reply",
        "customer_delivered": False,
        "customer_fallback_artifact": "safe_fallback_reply",
        "guardrail_error_codes": list(errors),
        "url_comparisons": comparisons[:64],
        "lexical_commercial_diagnostics": availability_diagnostics,
    }


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
    if errors and isinstance(output, CommerceReply):
        diagnostic = _safe_rejected_output_diagnostic(
            run_context.context,
            output,
            errors,
        )
        if (
            diagnostic["url_comparisons"]
            or diagnostic["lexical_commercial_diagnostics"]
        ):
            output_info["rejected_output_diagnostic"] = diagnostic
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
