"""Admin-only LLM extraction for merchant escalation authoring.

Customer-facing AI remains read-only. This module is authorized only for
admin configuration authoring. The model never receives or emits phone
numbers and may only reference tenant-scoped candidate contact_ids.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence

logger = logging.getLogger("nahla.operations.escalation_policy_admin_llm")

ALLOWED_ACTIONS = (
    "share_customer_contact",
    "whatsapp_cta",
    "notify_or_handoff",
    "handoff_conversation",
)
ALLOWED_CONDITIONS = (
    "sequence",
    "arrival",
    "no_response",
    "complaint_urgent",
)

_API_KEY = os.environ.get("OPENAI_API_KEY", "")
_API_BASE = os.environ.get("OPENAI_API_BASE", "https://api.openai.com/v1")
_MODEL = os.environ.get(
    "NAHLA_ADMIN_AUTHORING_MODEL",
    os.environ.get("OPENAI_MODEL", "gpt-4.1"),
)
_TIMEOUT = float(os.environ.get("NAHLA_ADMIN_AUTHORING_TIMEOUT", "30"))


def candidate_payload(contacts: Sequence[Any], *, branches: Sequence[Any] = ()) -> Dict[str, Any]:
    """Tenant-scoped roster for the admin model. No phone numbers."""
    contact_rows: List[Dict[str, Any]] = []
    for contact in contacts:
        vis = str(getattr(contact, "customer_visibility", "") or "")
        contact_rows.append({
            "contact_id": int(contact.id),
            "display_name": str(contact.display_name or ""),
            "role": str(contact.role or ""),
            "branch": str(getattr(contact, "branch_name", "") or ""),
            "customer_visibility": vis,
        })
    branch_rows = []
    for branch in branches:
        if isinstance(branch, Mapping):
            branch_rows.append({
                "id": int(branch.get("id") or 0),
                "name": str(branch.get("name") or ""),
            })
        else:
            branch_rows.append({
                "id": int(getattr(branch, "id", 0) or 0),
                "name": str(getattr(branch, "name", "") or ""),
            })
    return {
        "contacts": contact_rows,
        "branches": branch_rows,
        "allowed_actions": list(ALLOWED_ACTIONS),
        "allowed_trigger_conditions": list(ALLOWED_CONDITIONS),
    }


def empty_extraction(*, reason: str) -> Dict[str, Any]:
    return {
        "steps": [],
        "unresolved_references": [],
        "ambiguities": [reason],
        "summary_for_merchant": "",
        "model": _MODEL,
        "fallback_used": True,
    }


def extract_escalation_intent(
    instruction_text: str,
    *,
    candidates: Mapping[str, Any],
) -> Dict[str, Any]:
    """LLM structured extraction. Never invents contact IDs or phones."""
    text = str(instruction_text or "").strip()
    if not text:
        return empty_extraction(reason="empty_instruction")
    if not _API_KEY:
        logger.info("[ADMIN_AUTHORING] model_unavailable reason=missing_api_key")
        return empty_extraction(reason="authoring_model_unavailable")
    prompt = _system_prompt(candidates)
    try:
        raw = _call_openai_chat(prompt=prompt, user_text=text)
        parsed = _parse_json_object(raw)
    except Exception:  # noqa: BLE001
        logger.exception("[ADMIN_AUTHORING] extraction_failed")
        return empty_extraction(reason="authoring_model_unavailable")
    return _normalize_extraction(parsed, candidates=candidates)


def _system_prompt(candidates: Mapping[str, Any]) -> str:
    roster = json.dumps(candidates, ensure_ascii=False)
    return (
        "You extract a structured employee-escalation policy from a merchant's "
        "natural-language instructions. Output JSON only.\n"
        "Use only contact_id values from the candidate roster. Never invent IDs. "
        "Never emit phone numbers. If a person is not in the roster, add them to "
        "unresolved_references. If two candidates share a role and the instruction "
        "does not uniquely identify one, add an ambiguity instead of guessing.\n"
        "Schema:\n"
        "{\n"
        '  "steps": [{"contact_id": 1, "trigger_condition": "sequence",'
        ' "permitted_action": "share_customer_contact"}],\n'
        '  "unresolved_references": [{"token": "...", "reason": "unknown_person"}],\n'
        '  "ambiguities": ["ambiguous_role:خدمة العملاء"],\n'
        '  "summary_for_merchant": "short Arabic summary"\n'
        "}\n"
        f"allowed_actions={list(ALLOWED_ACTIONS)}\n"
        f"allowed_trigger_conditions={list(ALLOWED_CONDITIONS)}\n"
        f"candidates={roster}\n"
    )


def _call_openai_chat(*, prompt: str, user_text: str) -> str:
    import httpx  # noqa: PLC0415

    headers = {
        "Authorization": f"Bearer {_API_KEY}",
        "Content-Type": "application/json",
    }
    body = {
        "model": _MODEL,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_text},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "max_tokens": 1200,
    }
    with httpx.Client(timeout=_TIMEOUT) as client:
        resp = client.post(
            f"{_API_BASE}/chat/completions",
            headers=headers,
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()
    return str(data["choices"][0]["message"]["content"] or "")


def _parse_json_object(text: str) -> Dict[str, Any]:
    raw = str(text or "").strip()
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return {}
    parsed = json.loads(match.group(0))
    return parsed if isinstance(parsed, dict) else {}


def _normalize_extraction(
    parsed: Mapping[str, Any],
    *,
    candidates: Mapping[str, Any],
) -> Dict[str, Any]:
    allowed_ids = {
        int(row.get("contact_id") or 0)
        for row in (candidates.get("contacts") or [])
        if int(row.get("contact_id") or 0)
    }
    steps: List[Dict[str, Any]] = []
    unresolved: List[Dict[str, Any]] = []
    for raw in parsed.get("steps") or []:
        if not isinstance(raw, Mapping):
            continue
        try:
            cid = int(raw.get("contact_id") or 0)
        except (TypeError, ValueError):
            cid = 0
        if cid not in allowed_ids:
            unresolved.append({
                "token": str(raw.get("contact_id") or ""),
                "reason": "invalid_contact_id",
            })
            continue
        condition = str(raw.get("trigger_condition") or "sequence").strip()
        if condition not in ALLOWED_CONDITIONS:
            condition = "sequence"
        action = str(raw.get("permitted_action") or "").strip()
        if action not in ALLOWED_ACTIONS:
            action = ""
        steps.append({
            "contact_id": cid,
            "trigger_condition": condition,
            "permitted_action": action,
        })
    for raw in parsed.get("unresolved_references") or []:
        if isinstance(raw, Mapping):
            unresolved.append({
                "token": str(raw.get("token") or ""),
                "reason": str(raw.get("reason") or "unknown_person"),
            })
        elif str(raw).strip():
            unresolved.append({"token": str(raw), "reason": "unknown_person"})
    ambiguities = [
        str(item)
        for item in (parsed.get("ambiguities") or [])
        if str(item).strip()
    ]
    return {
        "steps": steps,
        "unresolved_references": unresolved,
        "ambiguities": ambiguities,
        "summary_for_merchant": str(parsed.get("summary_for_merchant") or "").strip(),
        "model": _MODEL,
        "fallback_used": False,
    }


__all__ = [
    "ALLOWED_ACTIONS",
    "ALLOWED_CONDITIONS",
    "candidate_payload",
    "empty_extraction",
    "extract_escalation_intent",
]
