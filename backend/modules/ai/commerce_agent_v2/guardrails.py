"""Grounding and read-only guardrails for Commerce Agent V2."""
from __future__ import annotations

import re
from typing import Any, Iterable

from agents import GuardrailFunctionOutput, RunContextWrapper, input_guardrail, output_guardrail

from modules.ai.commerce_agent_v2.context import CommerceAgentContext
from modules.ai.commerce_agent_v2.output import CommerceReply, EvidenceRecord


_LEGACY_MARKER_RE = re.compile(r"\[(?:PRODUCT|MEDIA_KEY|CALL):", re.IGNORECASE)
_URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
_SAR_RE = re.compile(r"(?<!\d)(\d+(?:[.,]\d+)?)\s*(?:ريال|ر\.س|SAR)\b", re.IGNORECASE)


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

    for claim in reply.fact_claims:
        record = evidence.get(claim.evidence_ref)
        if record is not None and claim.value.strip() not in _evidence_values(record):
            errors.append(f"claim_not_in_evidence:{claim.kind}")

    for product_ref in reply.product_refs:
        record = evidence.get(product_ref.evidence_ref)
        if (
            record is None
            or record.source != "catalog_product"
            or record.source_id != str(product_ref.product_id)
        ):
            errors.append("invalid_product_reference")

    for media_ref in reply.media_refs:
        record = evidence.get(media_ref.evidence_ref)
        if record is not None and str(media_ref.url) not in _evidence_values(record):
            errors.append("media_url_not_in_evidence")
    for action in reply.ui_actions:
        record = evidence.get(action.evidence_ref)
        if record is not None and str(action.url) not in _evidence_values(record):
            errors.append("action_url_not_in_evidence")

    evidenced_urls = {
        value
        for record in evidence.values()
        for value in _evidence_values(record)
        if value.startswith(("http://", "https://"))
    }
    for url in _URL_RE.findall(reply.text):
        if url.rstrip(".,،؛") not in evidenced_urls:
            errors.append("url_not_in_evidence")

    price_values = {
        str(record.fields.get(field)).strip().replace(",", ".")
        for record in evidence.values()
        if record.source == "catalog_product"
        for field in ("price", "sale_price", "regular_price")
        if record.fields.get(field) not in (None, "")
    }
    for amount in _SAR_RE.findall(reply.text):
        normalized = amount.replace(",", ".")
        if normalized not in price_values:
            errors.append("price_not_in_catalog_evidence")

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
    return GuardrailFunctionOutput(
        output_info={"passed": not errors, "errors": errors},
        tripwire_triggered=bool(errors),
    )


def contains_legacy_marker(value: str) -> bool:
    return bool(_LEGACY_MARKER_RE.search(str(value or "")))
