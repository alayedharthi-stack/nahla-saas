"""
modules/ai/brain/postprocess/context_leakage_guard.py
──────────────────────────────────────────────────────
Block platform-internal prompt literals from reaching customers.

Modes (NAHLA_CONTEXT_LEAKAGE_GUARD_MODE):
  off     — guard disabled
  shadow  — log + CVI-LEAK telemetry; never rewrite (Phase 1 default)
  enforce — strip blocked terms; tenant-authorized beneficiaries pass through

Instructor-zone few-shots must never become customer-visible text.
Authorized merchant account names from tenant KB are always allowed.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any, List, Optional, Sequence, Tuple

logger = logging.getLogger("nahla.brain.postprocess.context_leakage_guard")

_NORMALISE_AR_RE = re.compile(r"[\u064B-\u065F\u0670]")

# Platform-internal literals — not merchant KB, not customer-facing.
_PLATFORM_INTERNAL_TERMS: Tuple[str, ...] = (
    "نحلة الذهبية",
    "nahla golden",
    "nahla althahabiya",
)

# Placeholder tokens from instructor-zone few-shots — must not reach customers.
_INSTRUCTOR_PLACEHOLDER_TERMS: Tuple[str, ...] = (
    "{beneficiary_name}",
    "{transfer_phone}",
    "{bank_label}",
    "{BENEFICIARY_NAME}",
    "{TRANSFER_PHONE}",
    "{BANK_LABEL}",
)


def context_leakage_guard_mode() -> str:
    mode = os.environ.get(
        "NAHLA_CONTEXT_LEAKAGE_GUARD_MODE", "shadow",
    ).strip().lower()
    if mode in ("off", "shadow", "enforce"):
        return mode
    return "shadow"


def _norm(text: Optional[str]) -> str:
    if not text or not isinstance(text, str):
        return ""
    t = _NORMALISE_AR_RE.sub("", text)
    t = t.replace("ـ", "")
    t = (
        t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
         .replace("ى", "ي").replace("ة", "ه")
    )
    return t.lower().strip()


def _blocked_terms() -> Tuple[str, ...]:
    return _PLATFORM_INTERNAL_TERMS + _INSTRUCTOR_PLACEHOLDER_TERMS


def load_tenant_authorized_account_names(
    db: Any,
    *,
    tenant_id: Optional[int],
) -> List[str]:
    """Beneficiary names registered in tenant payment KB — always allowed."""
    if db is None or not tenant_id:
        return []
    try:
        from core.tenant_payment_accounts import load_tenant_payment_accounts  # noqa: PLC0415

        accounts = load_tenant_payment_accounts(db, tenant_id=int(tenant_id))
        return [str(b).strip() for b in (accounts.beneficiaries or []) if str(b).strip()]
    except Exception:  # noqa: BLE001
        return []


def _term_allowed_for_tenant(
    term: str,
    authorized_names: Sequence[str],
) -> bool:
    norm_term = _norm(term)
    if not norm_term:
        return False
    for name in authorized_names:
        norm_name = _norm(name)
        if not norm_name:
            continue
        if norm_term == norm_name or norm_term in norm_name or norm_name in norm_term:
            return True
    return False


def detect_context_leakage(
    reply: Optional[str],
    *,
    authorized_names: Optional[Sequence[str]] = None,
) -> List[str]:
    """Return platform-internal terms present in reply (tenant-authorized excluded)."""
    text = str(reply or "")
    if not text.strip():
        return []
    norm_reply = _norm(text)
    if not norm_reply:
        return []

    allowed = list(authorized_names or [])
    found: List[str] = []
    for term in _blocked_terms():
        norm_term = _norm(term)
        if not norm_term:
            continue
        if norm_term not in norm_reply and term not in text:
            continue
        if _term_allowed_for_tenant(term, allowed):
            continue
        found.append(term)
    return found


def _strip_terms_from_reply(reply: str, terms: Sequence[str]) -> str:
    out = reply
    for term in terms:
        if term in out:
            out = out.replace(term, "")
        # Case-insensitive Latin fallback
        if term.isascii():
            pattern = re.compile(re.escape(term), re.IGNORECASE)
            out = pattern.sub("", out)
    # Collapse stranded label lines: "اسم:" or "اسم الحساب:" alone
    out = re.sub(r"(?m)^\s*اسم(?:\s*الحساب)?\s*:\s*$", "", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


@dataclass(frozen=True)
class ContextLeakageGuardResult:
    reply: str
    action: str
    replaced: bool = False
    reason: str = ""
    leaked_terms: Tuple[str, ...] = ()


def log_context_leakage_guard(
    *,
    tenant_id: Optional[int],
    conversation_id: Optional[int],
    action: str,
    reason: str,
    leaked_terms: Sequence[str],
    mode: str,
) -> None:
    terms = ",".join(leaked_terms) if leaked_terms else "-"
    try:
        logger.info(
            "[CONTEXT_LEAKAGE_GUARD] tenant_id=%s conversation_id=%s "
            "mode=%s action=%s reason=%s leaked_terms=%s",
            tenant_id,
            conversation_id,
            mode or "-",
            action,
            reason or "-",
            terms,
        )
    except Exception:  # noqa: BLE001
        pass


def log_cvi_leak(
    *,
    tenant_id: Optional[int],
    conversation_id: Optional[int],
    leaked_terms: Sequence[str],
    action: str,
    mode: str,
) -> None:
    """Customer Visible Incident telemetry for leakage."""
    terms = ",".join(leaked_terms) if leaked_terms else "-"
    try:
        logger.info(
            "[CVI_LEAK] tenant_id=%s conversation_id=%s mode=%s "
            "action=%s leaked_terms=%s",
            tenant_id,
            conversation_id,
            mode or "-",
            action,
            terms,
        )
    except Exception:  # noqa: BLE001
        pass


def apply_context_leakage_guard(
    *,
    reply: str,
    db: Any = None,
    tenant_id: Optional[int] = None,
    conversation_id: Optional[int] = None,
) -> ContextLeakageGuardResult:
    mode = context_leakage_guard_mode()
    original = str(reply or "")
    if not original.strip() or mode == "off":
        return ContextLeakageGuardResult(reply=original, action="allowed")

    authorized = load_tenant_authorized_account_names(db, tenant_id=tenant_id)
    leaked = detect_context_leakage(original, authorized_names=authorized)
    if not leaked:
        log_context_leakage_guard(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            action="allowed",
            reason="no_leakage_detected",
            leaked_terms=(),
            mode=mode,
        )
        return ContextLeakageGuardResult(reply=original, action="allowed")

    if mode == "shadow":
        log_context_leakage_guard(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            action="would_rewrite",
            reason="platform_internal_term_detected",
            leaked_terms=leaked,
            mode=mode,
        )
        log_cvi_leak(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            leaked_terms=leaked,
            action="would_rewrite",
            mode=mode,
        )
        return ContextLeakageGuardResult(
            reply=original,
            action="would_rewrite",
            reason="platform_internal_term_detected",
            leaked_terms=tuple(leaked),
        )

    cleaned = _strip_terms_from_reply(original, leaked)
    log_context_leakage_guard(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        action="rewrote",
        reason="platform_internal_term_stripped",
        leaked_terms=leaked,
        mode=mode,
    )
    log_cvi_leak(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        leaked_terms=leaked,
        action="rewrote",
        mode=mode,
    )
    return ContextLeakageGuardResult(
        reply=cleaned,
        action="rewrote",
        replaced=True,
        reason="platform_internal_term_stripped",
        leaked_terms=tuple(leaked),
    )


__all__ = [
    "ContextLeakageGuardResult",
    "apply_context_leakage_guard",
    "context_leakage_guard_mode",
    "detect_context_leakage",
    "load_tenant_authorized_account_names",
    "log_cvi_leak",
]
