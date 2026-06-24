"""
store_inquiry_compose_guard.py
──────────────────────────────
Operational guards for online-store / store-link inquiry turns.

Ensures compose, safety-net, and CTA layers share one store_url_resolver
truth — without prescribing customer-facing wording.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

from modules.ai.brain.types import (
    INTENT_ASK_STORE_INFO,
    INTENT_ONLINE_STORE_INQUIRY,
)

# Canonical no-URL operational claims (PR #274) — detection only, not output.
_NO_URL_CLAIM_FRAGMENT = "ما عندي رابط المتجر الإلكتروني محفوظ في النظام"

# Product/size slot bleed — must not ride on a store-link turn.
_SIZE_BLEED_LINE_RE = re.compile(
    r"(?:"
    r"وش\s*(?:ال)?حجم|"
    r"أ?ي\s*حجم|"
    r"الحجم\s*(?:تفض|يناسب|تبي|تب(?:ى|a)?)|"
    r"نكمل\s+اختيار\s+(?:ال)?حجم|"
    r"size\s*(?:do\s+you|would\s+you)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_WARM_ACK_ONLY_RE = re.compile(
    r"^(?:حاضر|أبشر|أبشري|تمام|يا\s+هلا|أكيد|تام)[،,.\s]*$",
    re.UNICODE | re.IGNORECASE,
)


@dataclass(frozen=True)
class StoreLinkBodyReconcileResult:
    body: str
    store_url: str
    stripped_no_url_claim: bool = False
    stripped_size_bleed: bool = False
    action: str = "unchanged"


def is_store_link_compose_turn(
    *,
    intent_name: str = "",
    decision_action: str = "",
    decision_topic: str = "",
    customer_message: str = "",
) -> bool:
    """True when this turn owns store URL delivery — not checkout slot collection."""
    name = str(intent_name or "").strip()
    if name in {INTENT_ONLINE_STORE_INQUIRY, INTENT_ASK_STORE_INFO}:
        return True
    topic = str(decision_topic or "").strip()
    if str(decision_action or "").strip() == "faq_reply" and topic == "store_info":
        return True
    try:
        from modules.ai.brain.commerce.store_url_resolver import (  # noqa: PLC0415
            is_online_store_inquiry,
        )

        return is_online_store_inquiry(customer_message or "")
    except Exception:  # noqa: silent-ok — probe must not block compose
        return False


def should_skip_order_resume_hint(
    *,
    topic: str = "",
    intent_name: str = "",
) -> bool:
    """Store-link and location FAQ turns must not resume checkout slots."""
    if str(topic or "").strip() in {"location", "store_info"}:
        return True
    return str(intent_name or "").strip() in {
        INTENT_ONLINE_STORE_INQUIRY,
        INTENT_ASK_STORE_INFO,
    }


def body_claims_no_store_url(text: str) -> bool:
    return _NO_URL_CLAIM_FRAGMENT in str(text or "")


def body_has_order_size_bleed(text: str) -> bool:
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _SIZE_BLEED_LINE_RE.search(stripped):
            return True
    return False


def strip_store_inquiry_contradictions(text: str) -> tuple[str, bool, bool]:
    """
    Remove operational false negatives and order-size bleed from store-link bodies.

    Returns ``(cleaned_text, stripped_no_url, stripped_size)``.
    """
    stripped_no_url = False
    stripped_size = False
    kept: list[str] = []

    for line in str(text or "").splitlines():
        raw = line.strip()
        if not raw:
            continue
        if _NO_URL_CLAIM_FRAGMENT in raw:
            stripped_no_url = True
            remainder = raw.replace(_NO_URL_CLAIM_FRAGMENT, "").strip(" ،,.")
            remainder = re.sub(
                r"^(?:حاضر|أبشر|أبشري|تمام|يا\s+هلا|أكيد|تام)[،,.\s]*",
                "",
                remainder,
                flags=re.UNICODE | re.IGNORECASE,
            ).strip(" ،,.")
            if remainder and not _WARM_ACK_ONLY_RE.match(remainder):
                kept.append(remainder)
            continue
        if _SIZE_BLEED_LINE_RE.search(raw):
            stripped_size = True
            continue
        kept.append(raw)

    cleaned = "\n".join(kept).strip()
    return cleaned, stripped_no_url, stripped_size


def reconcile_store_link_body_when_url_found(
    reply_text: str,
    store_url: str,
) -> StoreLinkBodyReconcileResult:
    """
    Align body with resolver ``found=true`` before CTA lift.

    Does not inject marketing copy — drops contradictory spans and keeps
    only non-conflicting remainder plus the operational URL fact.
    """
    url = str(store_url or "").strip()
    if not url:
        return StoreLinkBodyReconcileResult(
            body=str(reply_text or "").strip(),
            store_url="",
            action="no_url",
        )

    original = str(reply_text or "").strip()
    if not original:
        return StoreLinkBodyReconcileResult(
            body=url,
            store_url=url,
            action="url_only",
        )

    if body_claims_no_store_url(original) or body_has_order_size_bleed(original):
        cleaned, sn, ss = strip_store_inquiry_contradictions(original)
        if not cleaned:
            return StoreLinkBodyReconcileResult(
                body=url,
                store_url=url,
                stripped_no_url_claim=sn,
                stripped_size_bleed=ss,
                action="drop_contradictions_url_only",
            )
        sep = "\n" if cleaned.endswith("\n") else "\n\n"
        return StoreLinkBodyReconcileResult(
            body=f"{cleaned.rstrip()}{sep}{url}",
            store_url=url,
            stripped_no_url_claim=sn,
            stripped_size_bleed=ss,
            action="drop_contradictions_append_url",
        )

    if _URL_IN_TEXT_RE.search(original):
        return StoreLinkBodyReconcileResult(
            body=original,
            store_url=url,
            action="url_already_present",
        )

    sep = "\n" if original.endswith("\n") else "\n\n"
    return StoreLinkBodyReconcileResult(
        body=f"{original.rstrip()}{sep}{url}",
        store_url=url,
        action="append_url",
    )


_URL_IN_TEXT_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def apply_store_url_to_facts(
    facts: Any,
    db: Any,
    tenant_id: int,
) -> None:
    """
    Single resolver pass for compose — mirrors safety-net lookup.

    Mutates ``facts.store_url`` and resolution audit fields in place.
    """
    prior = str(getattr(facts, "store_url", "") or "").strip()
    try:
        from modules.ai.brain.commerce.store_url_resolver import (  # noqa: PLC0415
            resolve_store_url,
        )

        resolved = resolve_store_url(db, tenant_id)
        setattr(facts, "store_url_resolved", bool(resolved.found))
        setattr(facts, "store_url_source", str(resolved.source or "none"))
        setattr(facts, "store_url_resolve_reason", str(resolved.reason or ""))
        if resolved.found:
            facts.store_url = resolved.url
        elif not prior:
            facts.store_url = ""
        if prior and resolved.found and prior.rstrip("/") != resolved.url.rstrip("/"):
            import logging

            logging.getLogger("nahla.brain.store_inquiry_compose_guard").info(
                "[STORE_URL_FACTS] tenant=%s compose_resolver_replaced "
                "prior_len=%d source=%s",
                tenant_id,
                len(prior),
                resolved.source,
            )
    except Exception as exc:  # noqa: BLE001
        import logging

        logging.getLogger("nahla.brain.store_inquiry_compose_guard").warning(
            "[STORE_URL_FACTS] tenant=%s resolver_failed err=%s prior_len=%d",
            tenant_id,
            exc,
            len(prior),
        )
        setattr(facts, "store_url_resolved", bool(prior))
        setattr(facts, "store_url_source", "compose_exception")
        setattr(facts, "store_url_resolve_reason", type(exc).__name__)


__all__ = [
    "StoreLinkBodyReconcileResult",
    "apply_store_url_to_facts",
    "body_claims_no_store_url",
    "body_has_order_size_bleed",
    "is_store_link_compose_turn",
    "reconcile_store_link_body_when_url_found",
    "should_skip_order_resume_hint",
    "strip_store_inquiry_contradictions",
]
