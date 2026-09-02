"""Internal staff-escalation operational-claim classifier.

INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
PERSONA_CHANGED=NO
PHRASE_MAP_CHANGED=NO
KEYWORD_ROUTER_CHANGED=NO
CUSTOMER_REGEX_CHANGED=NO

INTERNAL_VERIFIER_SCOPE=D2_OPERATIONAL_CLAIM_CLASSIFICATION_ONLY

This module classifies claims present in a candidate reply. It does not
authorize send, classify customer intent, route, or select staff.
Fail closed on missing key, timeout, invalid schema, or transport error.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional

from modules.ai.brain.postprocess.staff_escalation_semantic_claims import (
    StaffEscalationCandidateClaims,
    StaffEscalationTruthCapabilities,
)

logger = logging.getLogger("nahla.brain.staff_escalation_semantic_verifier")

INTERNAL_VERIFIER_SCOPE = "D2_OPERATIONAL_CLAIM_CLASSIFICATION_ONLY"
INTERNAL_VERIFIER_MODEL_DEFAULT = "gpt-5.6-luna"

_CLAIM_BOOL_KEYS = (
    "claims_request_registered",
    "claims_queued",
    "claims_staff_assigned",
    "claims_staff_notified",
    "claims_future_followup",
    "claims_contact_delivered",
)

_INTERNAL_INSTRUCTION = """You are an internal operational-claim classifier for a commerce support platform.
Scope: classify which staff-escalation operational claims are present in the candidate customer-facing text.
You are not classifying customer intent, routing, persona, sales, merchant policy, or staff selection.

Return a JSON object only, with these boolean fields:
- claims_request_registered: the text says the customer request/message was received or registered.
- claims_queued: the text says the request is in a queue or waiting list.
- claims_staff_assigned: the text says a specific staff member or agent was assigned.
- claims_staff_notified: the text says staff/team were notified, alerted, or messaged.
- claims_future_followup: the text promises that staff/team will later contact, follow up, reply, continue, or handle the customer.
- claims_contact_delivered: the text says a staff phone number, vCard, or contact details were delivered to the customer.
- confidence: number from 0 to 1.

Rules:
- Classify the candidate text only. Allowed-capability facts are context, not permission to mark claims true.
- Acknowledgement of receipt is request_registered, not future follow-up.
- A queue/waiting-list statement is queued, not staff_notified.
- Staff notified is not staff assigned.
- Staff notified is not a future follow-up commitment.
- Any promise that the team/store will later follow up, continue, or get back to the customer is claims_future_followup=true.
- If a claim type is absent, set it false.
- Do not invent claims from the allowed-capability facts.
- Do not output customer-facing wording. JSON only.
"""


def _verifier_model() -> str:
    return (
        os.environ.get("NAHLA_STAFF_ESCALATION_CLAIM_VERIFIER_MODEL")
        or os.environ.get("OPENAI_MODEL")
        or INTERNAL_VERIFIER_MODEL_DEFAULT
    ).strip() or INTERNAL_VERIFIER_MODEL_DEFAULT


def _api_key() -> str:
    return str(os.environ.get("OPENAI_API_KEY") or "").strip()


def _api_base() -> str:
    return str(os.environ.get("OPENAI_API_BASE") or "https://api.openai.com/v1").rstrip("/")


def _timeout_seconds() -> float:
    try:
        return float(os.environ.get("NAHLA_STAFF_ESCALATION_CLAIM_VERIFIER_TIMEOUT") or "10")
    except (TypeError, ValueError):
        return 10.0


def _extract_json_object(raw: str) -> Optional[Dict[str, Any]]:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def parse_staff_escalation_claim_payload(raw: str) -> StaffEscalationCandidateClaims:
    payload = _extract_json_object(raw)
    if not isinstance(payload, dict):
        return StaffEscalationCandidateClaims(valid_parse=False, provenance="invalid")
    values: Dict[str, bool] = {}
    for key in _CLAIM_BOOL_KEYS:
        value = payload.get(key)
        if not isinstance(value, bool):
            return StaffEscalationCandidateClaims(valid_parse=False, provenance="invalid")
        values[key] = value
    confidence_raw = payload.get("confidence", 0.0)
    try:
        confidence = float(confidence_raw)
    except (TypeError, ValueError):
        confidence = 0.0
    return StaffEscalationCandidateClaims(
        claims_request_registered=values["claims_request_registered"],
        claims_queued=values["claims_queued"],
        claims_staff_assigned=values["claims_staff_assigned"],
        claims_staff_notified=values["claims_staff_notified"],
        claims_future_followup=values["claims_future_followup"],
        claims_contact_delivered=values["claims_contact_delivered"],
        valid_parse=True,
        confidence=confidence,
        provenance="ok",
    )


def _failed(provenance: str) -> StaffEscalationCandidateClaims:
    return StaffEscalationCandidateClaims(valid_parse=False, provenance=provenance)


def _user_payload(candidate_text: str, capabilities: StaffEscalationTruthCapabilities) -> str:
    allowed = capabilities.as_dict()
    return json.dumps(
        {
            "candidate_text": str(candidate_text or ""),
            "allowed_operational_capabilities": allowed,
        },
        ensure_ascii=False,
    )


def _chat_body(model: str, candidate_text: str, capabilities: StaffEscalationTruthCapabilities) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": _INTERNAL_INSTRUCTION},
            {"role": "user", "content": _user_payload(candidate_text, capabilities)},
        ],
        "response_format": {"type": "json_object"},
        "max_completion_tokens": 256,
    }
    if not str(model).startswith("gpt-5.6-"):
        body["temperature"] = 0
    return body


async def classify_staff_escalation_claims(
    candidate_text: str,
    capabilities: StaffEscalationTruthCapabilities,
) -> StaffEscalationCandidateClaims:
    """Classify operational claims in candidate text. Never authorizes send."""
    if not str(candidate_text or "").strip():
        return _failed("empty_candidate")
    key = _api_key()
    if not key:
        return _failed("unavailable")
    model = _verifier_model()
    try:
        import httpx  # noqa: PLC0415
    except ImportError:
        logger.warning("[STAFF_ESCALATION_SEMANTIC_VERIFY] httpx_unavailable")
        return _failed("unavailable")

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    timeout = _timeout_seconds()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{_api_base()}/chat/completions",
                headers=headers,
                json=_chat_body(model, candidate_text, capabilities),
            )
            response.raise_for_status()
            payload = response.json()
    except httpx.TimeoutException:
        logger.warning("[STAFF_ESCALATION_SEMANTIC_VERIFY] timeout model=%s", model)
        return _failed("timeout")
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[STAFF_ESCALATION_SEMANTIC_VERIFY] transport_failed model=%s err=%s",
            model,
            type(exc).__name__,
        )
        return _failed("unavailable")

    try:
        raw = str(payload["choices"][0]["message"]["content"] or "")
    except (KeyError, IndexError, TypeError):
        return _failed("invalid")
    parsed = parse_staff_escalation_claim_payload(raw)
    if not parsed.valid_parse:
        return _failed(parsed.provenance or "invalid")
    return parsed
