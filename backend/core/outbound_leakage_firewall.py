"""
core/outbound_leakage_firewall.py
───────────────────────────────────
SaaS-wide outbound leakage firewall — blocks internal / system /
developer / policy / prompt scaffolding from reaching customers on
any tenant and any channel.

This is NOT a honey-specific or Progressive-Selling-only patch. All
internal instruction fingerprints live here as a single closed set.
"""
from __future__ import annotations

import logging
import re
from typing import List, Optional, Tuple

logger = logging.getLogger("nahla.security.outbound_leakage_firewall")

# ── Planner / debug field tokens ───────────────────────────────────────────
_PLANNER_PATTERNS: List[tuple[str, re.Pattern[str]]] = [
    ("response_goal",          re.compile(r"\bresponse_goal\b",                 re.IGNORECASE)),
    ("execute_pending_offer",  re.compile(r"\bexecute_pending_offer\b",         re.IGNORECASE)),
    ("resolve_ambiguous_need", re.compile(r"\bresolve_ambiguous_need\b",        re.IGNORECASE)),
    ("action_token",           re.compile(r"\bACTION_[A-Z][A-Z0-9_]*\b")),
    ("goal_constant",          re.compile(r"\bGOAL_[A-Z][A-Z0-9_]*\b")),
    ("fallback_kind_const",    re.compile(r"\bFALLBACK_KIND_[A-Z][A-Z0-9_]*\b")),
    ("intent_field",           re.compile(r"\bintent\s*[:=]",                   re.IGNORECASE)),
    ("decision_field",         re.compile(r"\bdecision\s*[:=]",                 re.IGNORECASE)),
    ("relational_frame_field", re.compile(r"\brelational_frame\s*[:=]",         re.IGNORECASE)),
    ("recommended_next_step",  re.compile(r"\brecommended_next_step\s*[:=]",    re.IGNORECASE)),
    ("fallback_kind_field",    re.compile(r"\bfallback_kind\s*[:=]",            re.IGNORECASE)),
    ("internal_word",          re.compile(r"\binternal\b",                      re.IGNORECASE)),
    ("debug_word",             re.compile(r"\bdebug\b",                         re.IGNORECASE)),
    ("planner_word",           re.compile(r"\bplanner\b",                       re.IGNORECASE)),
]

# ── Sales policy / system / developer instruction leaks ──────────────────────
_POLICY_AND_SYSTEM_PATTERNS: List[tuple[str, re.Pattern[str]]] = [
    ("progressive_selling_en",   re.compile(r"progressive\s+selling", re.IGNORECASE)),
    ("progressive_selling_ar",   re.compile(r"البيع\s*التدريجي", re.UNICODE)),
    ("rules_prefix_ar",          re.compile(r"حسب\s*قواعد\s*(?:البيع\s*)?(?:التدريجي)?", re.UNICODE)),
    ("rules_generic_ar",         re.compile(r"حسب\s*القواعد\b", re.UNICODE)),
    ("system_instructions_ar",   re.compile(r"تعليمات\s*النظام", re.UNICODE)),
    ("reply_policy_ar",          re.compile(r"سياسة\s*الرد", re.UNICODE)),
    ("internal_policy_en",       re.compile(r"\binternal\s+policy\b", re.IGNORECASE)),
    ("developer_instructions",   re.compile(r"developer\s+instructions?", re.IGNORECASE)),
    ("system_prompt_en",         re.compile(r"\bsystem\s+prompt\b", re.IGNORECASE)),
    ("decision_engine_en",       re.compile(r"\bdecision\s+engine\b", re.IGNORECASE)),
    ("routing_en",               re.compile(r"\brouting\b", re.IGNORECASE)),
    ("prompt_en",                re.compile(r"\bprompt\b", re.IGNORECASE)),
    ("classifier_en",            re.compile(r"\bclassifier\b", re.IGNORECASE)),
    ("orchestrator_en",          re.compile(r"\borchestrator\b", re.IGNORECASE)),
    ("high_priority_block",      re.compile(r"\bHIGH\s+PRIORITY\b", re.IGNORECASE)),
    ("brain_state_json",         re.compile(r"\bBrainStateJSON\b", re.IGNORECASE)),
    ("response_goal_field",      re.compile(r"\bresponse_goal\s*[:=]", re.IGNORECASE)),
    ("merchant_context_field",   re.compile(r"\bmerchant_context\s*[:=]", re.IGNORECASE)),
    ("according_to_rules_en",    re.compile(r"according\s+to\s+(?:the\s+)?rules", re.IGNORECASE)),
    ("policy_name_ar",           re.compile(r"سياسة\s*(?:البيع|الرد|النظام)", re.UNICODE)),
]

# ── Merchant/admin troubleshooting leaks (June 2026) ───────────────────────
# Customer-facing replies must never tell the buyer to sync products,
# open the merchant dashboard, or diagnose store configuration.
_MERCHANT_TROUBLESHOOTING_PATTERNS: List[tuple[str, re.Pattern[str]]] = [
    ("merchant_dashboard_ar",       re.compile(r"لوحة\s*التحكم", re.UNICODE)),
    ("product_sync_ar",             re.compile(r"مزامنة\s*الم(?:نتجات|تجر)", re.UNICODE)),
    ("store_needs_sync_ar",         re.compile(r"يحتاج\s*الم(?:تجر|تجر)", re.UNICODE)),
    ("store_settings_ar",           re.compile(r"إعدادات\s*الم(?:تجر|تجر)", re.UNICODE)),
    ("internal_technical_issue_ar", re.compile(r"مشكلة\s*تقنية\s*داخلية", re.UNICODE)),
    ("if_problem_persists_sync_ar", re.compile(
        r"إذا\s*استمرت\s*المشكلة.*(?:مزامنة|لوحة\s*التحكم|المتجر)",
        re.UNICODE | re.IGNORECASE,
    )),
]

_ALL_LEAK_PATTERNS: List[tuple[str, re.Pattern[str]]] = (
    _PLANNER_PATTERNS + _POLICY_AND_SYSTEM_PATTERNS + _MERCHANT_TROUBLESHOOTING_PATTERNS
)


def contains_outbound_leak(text: str) -> Optional[str]:
    """Return leak fingerprint name or ``None`` if customer-safe."""
    if not text or not isinstance(text, str):
        return None
    for name, pattern in _ALL_LEAK_PATTERNS:
        if pattern.search(text):
            return name
    return None


def extract_customer_facing_segment(text: str) -> Optional[str]:
    """Drop paragraphs/lines contaminated with internal leaks."""
    if not text or not isinstance(text, str):
        return None

    paragraphs = re.split(r"\n\s*\n+", text.strip())
    clean_paragraphs = [
        p.strip()
        for p in paragraphs
        if p.strip() and contains_outbound_leak(p) is None
    ]
    if clean_paragraphs:
        recovered = "\n\n".join(clean_paragraphs).strip()
        if recovered:
            return recovered

    lines = text.splitlines()
    clean_lines = [
        ln.strip()
        for ln in lines
        if ln.strip() and contains_outbound_leak(ln) is None
    ]
    if clean_lines:
        recovered = "\n".join(clean_lines).strip()
        if len(recovered) >= 3:
            return recovered

    return None


def drop_leaky_sentences(text: str) -> str:
    if not text:
        return text or ""
    chunks = re.split(r"(?<=[.!?؟\n])\s+", text.strip())
    clean = [
        c.strip()
        for c in chunks
        if c.strip() and contains_outbound_leak(c) is None
    ]
    return " ".join(clean).strip()


def firewall_outbound_text(
    text: str,
    *,
    tenant_id: Optional[int] = None,
    recipient: Optional[str] = None,
    fallback_text: str = "",
) -> Tuple[str, bool]:
    """Rewrite or recover customer-facing text; never pass leaks through."""
    if not text or not isinstance(text, str):
        return text or "", False

    leak = contains_outbound_leak(text)
    if not leak:
        return text, False

    recovered = extract_customer_facing_segment(text)
    if recovered and recovered != text and contains_outbound_leak(recovered) is None:
        logger.warning(
            "[OUTBOUND_LEAKAGE_FIREWALL] tenant=%s to=%s marker=%s "
            "outcome=recovered_segment original_len=%d preview=%r",
            tenant_id, recipient, leak, len(text), text[:140],
        )
        return recovered, True

    sentence_scrub = drop_leaky_sentences(text)
    if (
        sentence_scrub
        and len(sentence_scrub) >= 12
        and contains_outbound_leak(sentence_scrub) is None
    ):
        logger.warning(
            "[OUTBOUND_LEAKAGE_FIREWALL] tenant=%s to=%s marker=%s "
            "outcome=recovered_sentence_scrub original_len=%d preview=%r",
            tenant_id, recipient, leak, len(text), text[:140],
        )
        return sentence_scrub, True

    _fb = fallback_text or (
        "أعتذر، حصل خلل بسيط في الرد. لو تكرر معك، أعد السؤال وأنا معك 🌷"
    )
    logger.warning(
        "[OUTBOUND_LEAKAGE_FIREWALL] tenant=%s to=%s marker=%s "
        "outcome=fallback original_len=%d preview=%r",
        tenant_id, recipient, leak, len(text), text[:140],
    )
    return _fb, True


__all__ = [
    "contains_outbound_leak",
    "drop_leaky_sentences",
    "extract_customer_facing_segment",
    "firewall_outbound_text",
]
