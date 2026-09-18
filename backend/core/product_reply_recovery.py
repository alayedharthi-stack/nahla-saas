"""
core/product_reply_recovery.py
──────────────────────────────
Grounded plain-text recovery for legacy Commerce V1 product turns.

Production incident (Tenant 1, September 2026): two broad catalog questions
were correctly understood, catalog search returned verified candidates, and
the customer still received NOTHING:

* the truth guard emptied the composed text, the arbitrary first-candidate
  card was (correctly) rejected as ``stale_or_unrelated_product`` and no
  fallback existed for ``final_delivery_mode == "failed"``;
* three candidates ``فستان / فستان / جاكيت`` produced duplicate visible
  button titles, Meta rejected the interactive payload (HTTP 400
  ``Duplicate button title``) and no plain-text recovery was sent.

This module owns the pure decisions behind the one-shot recovery the
webhook performs. It is deliberately small and deterministic:

* :func:`classify_provider_outcome` — accepted / definitive rejection /
  ambiguous / blocked, from the ``_post_wa`` result sink. Recovery is
  allowed only after a DEFINITIVE rejection (HTTP 4xx, explicit provider
  error envelope, no provider message id). Timeouts, connection loss after
  dispatch, 5xx and malformed 2xx bodies are AMBIGUOUS: the customer may
  already have the message, so nothing is resent.
* :func:`eligible_recovery_candidates` — the verified catalog candidates of
  the CURRENT turn only (``brain_state.last_search_candidates``, optionally
  narrowed by the compose ``catalog_product_ids``). Nothing is looked up
  by title, nothing is guessed.
* :func:`build_grounded_catalog_text` — a factual list built exclusively
  from fields the catalog evidence already authorises (title, verified
  price / sale price, explicit out-of-stock flag). No description, URL,
  discount or attribute is ever invented. Numbering follows the candidate
  order so a numeric pick still resolves to the same product. This is the
  constitution's *minimal emergency fallback* (``compose_source =
  fallback_deterministic``) used only after composition genuinely failed
  at the wire boundary; the guarded LLM text is reused verbatim whenever
  it survived.
* :func:`record_product_reply_outcome` — one closed-enum verdict per turn
  plus explicit lifecycle terminals so ``end_ok`` never again means "the
  handler returned" for a turn whose customer received nothing.

No customer prose is composed here beyond the factual catalog list, and no
guard (availability, commercial-claim, stale-product, focus, ownership,
dedup, locks, kill switches) is bypassed: the recovery runs strictly after
those guards and only consumes their verdicts.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger("nahla.product_reply_recovery")


# ── Closed outcome enum (delivery audit ``product_reply_outcome``) ──────────
OUTCOME_RICH_ACCEPTED = "rich_accepted"
OUTCOME_TEXT_ACCEPTED = "text_accepted"
OUTCOME_RICH_REJECTED_TEXT_RECOVERED = "rich_rejected_text_recovered"
OUTCOME_SUPPRESSED_TEXT_RECOVERED = "suppressed_text_recovered"
OUTCOME_AMBIGUOUS_PROVIDER = "ambiguous_provider_outcome"
OUTCOME_TERMINAL_FAILURE = "terminal_delivery_failure"

RECOVERED_OUTCOMES = frozenset({
    OUTCOME_RICH_REJECTED_TEXT_RECOVERED,
    OUTCOME_SUPPRESSED_TEXT_RECOVERED,
})
FAILED_OUTCOMES = frozenset({
    OUTCOME_AMBIGUOUS_PROVIDER,
    OUTCOME_TERMINAL_FAILURE,
})

# ── Provider outcome classes (from the ``_post_wa`` result sink) ────────────
PROVIDER_ACCEPTED = "accepted"
PROVIDER_DEFINITIVE_REJECTION = "definitive_rejection"
PROVIDER_AMBIGUOUS = "ambiguous"
PROVIDER_BLOCKED = "blocked"
PROVIDER_NOT_ATTEMPTED = "not_attempted"

_DEFINITIVE_CLASSIFICATIONS = frozenset({"provider_error_field"})
_AMBIGUOUS_CLASSIFICATIONS = frozenset({"exception", "missing_wamid"})
_BLOCKED_CLASSIFICATIONS = frozenset({
    "recipient_invalid",
    "automation_blocked",
    "conversation_quota_blocked",
})

FALLBACK_ACTION_TYPE = "catalog_grounded_text_recovery"
CHOSEN_PATH_TEXT_RECOVERY = "product_reply_text_recovery"

# Maximum products listed in the deterministic fallback. Mirrors the
# candidate breadth the pipeline persists for numeric picks.
GROUNDED_LIST_LIMIT = 5

_NUMERIC_PRICE_RE = re.compile(r"^\s*\d+(?:[.,]\d+)?\s*$")
_PRICE_SUFFIX = "ر.س"
_PRICE_LABEL = "السعر"
_OUT_OF_STOCK_LABEL = "غير متوفر حالياً"


# ─────────────────────────────────────────────────────────────────────────────
# Provider outcome
# ─────────────────────────────────────────────────────────────────────────────


def _http_status(sink: Mapping[str, Any]) -> Optional[int]:
    raw = sink.get("http_status")
    if raw is None:
        body = sink.get("response_body")
        if isinstance(body, Mapping):
            raw = body.get("_nahla_http_status")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def classify_provider_outcome(
    result_sink: Optional[Mapping[str, Any]],
    *,
    send_ok: bool,
) -> str:
    """Map one ``_post_wa`` attempt to a closed provider-outcome class.

    ``send_ok`` is the boolean ``_post_wa`` returned. The sink carries the
    wire classification (``ok`` / ``non_2xx`` / ``provider_error_field`` /
    ``missing_wamid`` / ``exception`` / pre-provider blocks), the HTTP
    status and the provider message id when one exists.
    """
    sink = dict(result_sink or {})
    if send_ok or (sink.get("classification") == "ok" and sink.get("wamid")):
        return PROVIDER_ACCEPTED
    classification = str(sink.get("classification") or "").strip()
    if not classification:
        # ``_post_wa`` returned False before any provider POST (throttle,
        # AI-disabled gate, quota, in-flight dedup, handoff scrub, ...).
        return PROVIDER_NOT_ATTEMPTED
    if classification in _BLOCKED_CLASSIFICATIONS:
        return PROVIDER_BLOCKED
    if classification in _AMBIGUOUS_CLASSIFICATIONS:
        return PROVIDER_AMBIGUOUS
    if classification == "non_2xx":
        status = _http_status(sink)
        if status is not None and 400 <= status < 500:
            return PROVIDER_DEFINITIVE_REJECTION
        return PROVIDER_AMBIGUOUS
    if classification in _DEFINITIVE_CLASSIFICATIONS:
        return PROVIDER_DEFINITIVE_REJECTION
    return PROVIDER_AMBIGUOUS


def provider_error_snapshot(result_sink: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Preserve the ORIGINAL provider error (status, code, detail, key).

    Never raises; returns ``{}`` when the sink carries no failure.
    """
    sink = dict(result_sink or {})
    classification = str(sink.get("classification") or "").strip()
    if not classification or classification == "ok":
        return {}
    body = sink.get("response_body")
    err: Dict[str, Any] = {}
    if isinstance(body, Mapping) and isinstance(body.get("error"), Mapping):
        err = dict(body["error"])
    details = ""
    data = err.get("error_data")
    if isinstance(data, Mapping):
        details = str(data.get("details") or "")
    message = str(err.get("message") or "")
    detail_parts = [p for p in (message, details, str(sink.get("error_text") or "")) if p]
    snapshot: Dict[str, Any] = {
        "classification": classification,
        "http_status": _http_status(sink),
        "code": err.get("code"),
        "subcode": err.get("error_subcode") or err.get("subcode"),
        "type": err.get("type"),
        "fbtrace_id": err.get("fbtrace_id"),
        "detail": " | ".join(detail_parts),
        "key": None,
    }
    try:
        from services.meta_errors import classify_meta_error  # noqa: PLC0415

        if classification == "exception":
            snapshot["key"] = "exception"
        elif classification == "missing_wamid":
            snapshot["key"] = "missing_wamid"
        elif err:
            snapshot["key"] = classify_meta_error(
                code=err.get("code"),
                subcode=snapshot["subcode"],
                error_type=err.get("type"),
                message=message,
                raw_response=body,
            ).key
        else:
            snapshot["key"] = classification
    except Exception:  # noqa: BLE001  # noqa: silent-ok — snapshot must never break delivery
        snapshot["key"] = snapshot.get("key") or classification
    return snapshot


# ─────────────────────────────────────────────────────────────────────────────
# Verified candidates → grounded text
# ─────────────────────────────────────────────────────────────────────────────


def _candidate_id(row: Mapping[str, Any]) -> Optional[str]:
    for key in ("id", "product_id"):
        val = row.get(key)
        if val not in (None, ""):
            return str(val)
    return None


def eligible_recovery_candidates(
    brain_state: Optional[Mapping[str, Any]],
    *,
    brain_result: Optional[Mapping[str, Any]] = None,
    limit: int = GROUNDED_LIST_LIMIT,
) -> List[Dict[str, Any]]:
    """Verified catalog candidates of the current turn, in stored order.

    Source of truth is ``brain_state.last_search_candidates`` — the exact
    list the pipeline persisted for this search (already filtered for
    orderability and breadth). When the compose result also names
    ``catalog_product_ids`` the list is narrowed to those ids; if that
    intersection is empty the stored list is kept (the ids may be
    external ids). Nothing is fetched or resolved by title here.
    """
    state = dict(brain_state or {})
    rows = [
        dict(r) for r in (state.get("last_search_candidates") or [])
        if isinstance(r, Mapping) and str(r.get("title") or "").strip()
    ]
    if not rows:
        return []
    result = dict(brain_result or {})
    ids = {
        str(x) for x in (result.get("catalog_product_ids") or [])
        if x not in (None, "")
    }
    if ids:
        narrowed = [r for r in rows if (_candidate_id(r) in ids)]
        if narrowed:
            rows = narrowed
    return rows[: max(1, int(limit or GROUNDED_LIST_LIMIT))]


def _render_price(value: Any) -> str:
    text = str(value if value is not None else "").strip()
    if not text:
        return ""
    if _NUMERIC_PRICE_RE.match(text):
        return f"{text} {_PRICE_SUFFIX}"
    return text


def _render_candidate_line(index: int, row: Mapping[str, Any]) -> str:
    title = " ".join(str(row.get("title") or "").split())
    parts = [f"{index}. {title}"]
    price = _render_price(row.get("price"))
    sale = _render_price(row.get("sale_price"))
    if price and sale and sale != price:
        parts.append(f"{_PRICE_LABEL}: ~~{price}~~ {sale}")
    elif price:
        parts.append(f"{_PRICE_LABEL}: {price}")
    elif sale:
        parts.append(f"{_PRICE_LABEL}: {sale}")
    if row.get("in_stock") is False:
        parts.append(_OUT_OF_STOCK_LABEL)
    return " — ".join(parts)


def build_grounded_catalog_text(candidates: Sequence[Mapping[str, Any]]) -> str:
    """Deterministic factual catalog list from verified candidates only.

    One line per product: ``<n>. <title> — السعر: <verified price>`` plus an
    explicit out-of-stock note only when the catalog evidence says so. The
    numbering follows the stored candidate order so ``1`` / ``2`` picks keep
    resolving to the same rows. Returns ``""`` when there is nothing verified
    to list — callers must then NOT send anything.
    """
    lines: List[str] = []
    for index, row in enumerate(list(candidates or []), 1):
        if not isinstance(row, Mapping):
            continue
        if not str(row.get("title") or "").strip():
            continue
        lines.append(_render_candidate_line(index, row))
    return "\n".join(lines).strip()


def choose_recovery_text(
    *,
    guarded_reply: str,
    candidates: Sequence[Mapping[str, Any]],
) -> Tuple[str, str]:
    """Return ``(text, source)``: the surviving guarded text verbatim when it
    exists, else the deterministic grounded list (``fallback_deterministic``).
    """
    reply = str(guarded_reply or "").strip()
    if reply:
        return reply, "guarded_reply"
    text = build_grounded_catalog_text(candidates)
    if text:
        return text, "fallback_deterministic"
    return "", ""


def fallback_provenance_metadata(*, fallback_reason: str) -> Dict[str, Any]:
    """Constitution metadata for the deterministic grounded list."""
    return {
        "compose_source": "fallback_deterministic",
        "response_mode": "catalog_grounded_list",
        "chosen_path": CHOSEN_PATH_TEXT_RECOVERY,
        "llm_candidate_present": False,
        "final_text_transformed": True,
        "final_transform_reasons": [CHOSEN_PATH_TEXT_RECOVERY],
        "final_customer_text_source": "fallback_deterministic",
        "final_expression_owner": CHOSEN_PATH_TEXT_RECOVERY,
        "fallback_reason": str(fallback_reason or "rich_presentation_undeliverable"),
        "fallback_action_type": FALLBACK_ACTION_TYPE,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Outcome recording
# ─────────────────────────────────────────────────────────────────────────────


def new_recovery_audit_fields() -> Dict[str, Any]:
    return {
        "product_reply_outcome": None,
        "text_recovery_attempts": 0,
        "text_recovery_wamid": None,
        "text_recovery_source": None,
        "text_recovery_skip_reason": None,
        "text_recovery_duplicate_suppressed": False,
        "product_text_recovery_sent": False,
        "original_provider_error": None,
        "provider_outcome": None,
    }


def record_product_reply_outcome(
    delivery_audit: Optional[Dict[str, Any]],
    *,
    outcome: str,
    final_mode: str,
    detail: str = "",
) -> Dict[str, Any]:
    """Stamp the closed-enum verdict on the audit and the lifecycle trace.

    * recovered outcomes → ``delivery_text_recovered`` marker +
      ``end_delivery_recovered`` terminal
    * failed outcomes → ``end_delivery_failed`` terminal whose detail names
      ``ambiguous_provider_outcome`` or ``terminal_delivery_failure``
    * accepted outcomes → no terminal override (``end_ok`` inference stays)

    Never raises. Returns a snapshot of the audit for telemetry/tests.
    """
    audit = delivery_audit if isinstance(delivery_audit, dict) else {}
    for key, value in new_recovery_audit_fields().items():
        audit.setdefault(key, value)
    audit["product_reply_outcome"] = str(outcome or OUTCOME_TERMINAL_FAILURE)
    audit["final_delivery_mode"] = str(final_mode or "")
    try:
        from core.inbound_lifecycle import (  # noqa: PLC0415
            EVENT_DELIVERY_TEXT_RECOVERED,
            EVENT_END_DELIVERY_FAILED,
            EVENT_END_DELIVERY_RECOVERED,
            record_lifecycle,
        )

        summary = (
            f"outcome={audit['product_reply_outcome']} mode={final_mode or ''} "
            f"recovery_attempts={int(audit.get('text_recovery_attempts') or 0)} "
            f"wamid_present={'1' if audit.get('text_recovery_wamid') else '0'}"
        )
        if detail:
            summary = f"{summary} {detail}"
        if outcome in RECOVERED_OUTCOMES:
            record_lifecycle(EVENT_DELIVERY_TEXT_RECOVERED, detail=summary)
            record_lifecycle(EVENT_END_DELIVERY_RECOVERED, detail=summary)
        elif outcome in FAILED_OUTCOMES:
            record_lifecycle(EVENT_END_DELIVERY_FAILED, detail=summary)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — lifecycle telemetry must never break delivery
        pass
    return dict(audit)


__all__ = [
    "CHOSEN_PATH_TEXT_RECOVERY",
    "FALLBACK_ACTION_TYPE",
    "FAILED_OUTCOMES",
    "GROUNDED_LIST_LIMIT",
    "OUTCOME_AMBIGUOUS_PROVIDER",
    "OUTCOME_RICH_ACCEPTED",
    "OUTCOME_RICH_REJECTED_TEXT_RECOVERED",
    "OUTCOME_SUPPRESSED_TEXT_RECOVERED",
    "OUTCOME_TERMINAL_FAILURE",
    "OUTCOME_TEXT_ACCEPTED",
    "PROVIDER_ACCEPTED",
    "PROVIDER_AMBIGUOUS",
    "PROVIDER_BLOCKED",
    "PROVIDER_DEFINITIVE_REJECTION",
    "PROVIDER_NOT_ATTEMPTED",
    "RECOVERED_OUTCOMES",
    "build_grounded_catalog_text",
    "choose_recovery_text",
    "classify_provider_outcome",
    "eligible_recovery_candidates",
    "fallback_provenance_metadata",
    "new_recovery_audit_fields",
    "provider_error_snapshot",
    "record_product_reply_outcome",
]
