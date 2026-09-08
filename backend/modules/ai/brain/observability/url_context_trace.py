"""Structured per-turn D17 URL-context attach observability.

Observation-only: records stage progression without URLs, HTML, or customer text.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger("nahla.url_context_trace")

SCHEMA_VERSION = "1"

_FAILURE_STAGES = frozenset(
    {
        "attach",
        "detector",
        "enrichment",
        "catalog_lookup",
        "rate_limit",
        "cache",
        "fetch",
        "extraction",
        "fact_projection",
        "trace_persist",
    }
)

_CACHE_STATUSES = frozenset({"miss", "hit", "duplicate_turn", "not_checked"})
_FETCH_RESULTS = frozenset(
    {
        "ok",
        "unavailable",
        "blocked",
        "not_attempted",
        "not_applicable",
    }
)
_EXTRACTION_STATUSES = frozenset(
    {
        "ok",
        "unavailable",
        "blocked",
        "not_applicable",
        "not_run",
    }
)


def _content_type_class(content_type: str) -> str:
    low = str(content_type or "").lower()
    if "json" in low:
        return "json"
    if "html" in low or "xml" in low:
        return "html"
    if not low:
        return "unknown"
    return "other"


def _allowlisted_error_class(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    allowed = {
        "rate_limited",
        "duplicate_turn_fetch",
        "enrich_exception",
        "metadata_missing",
        "oversized",
        "unavailable",
        "ip_blocked",
        "host_blocked",
        "scheme_blocked",
        "credentials_blocked",
        "timeout",
        "tls_error",
        "redirect_limit",
        "response_too_large",
        "non_http_scheme",
        "http_error",
        "empty_body",
    }
    return raw if raw in allowed else "other"


class UrlContextTraceRecorder:
    """Turn-local D17 attach trace. One instance per BrainContext."""

    __slots__ = ("_started", "_fields")

    def __init__(self) -> None:
        self._started = time.monotonic()
        self._fields: Dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "attach_entered": False,
            "detector_ran": False,
            "url_count": 0,
            "candidate_count": 0,
            "catalog_lookup_ran": False,
            "catalog_match": False,
            "rate_limit_checked": False,
            "rate_limit_allowed": None,
            "cache_status": "not_checked",
            "fetch_attempted": False,
            "external_fetch_count": 0,
            "fetch_completed": False,
            "fetch_result": "not_applicable",
            "fetch_error_class": "",
            "http_status": 0,
            "content_type_class": "unknown",
            "received_bytes": 0,
            "stored_decoded_bytes": 0,
            "body_truncated": False,
            "html_head_complete": False,
            "extraction_ran": False,
            "extraction_status": "not_run",
            "page_title_present": False,
            "description_present": False,
            "author_present": False,
            "preview_image_present": False,
            "facts_projected": False,
            "model_context_bound": False,
            "attach_completed": False,
            "failure_stage": "",
            "exception_class": "",
            "duration_ms": 0,
            "provider_domain": "",
            "enrichment_source": "",
        }

    def mark_attach_entered(self) -> None:
        self._fields["attach_entered"] = True

    def mark_detector(self, *, url_count: int, candidate_count: int) -> None:
        self._fields["detector_ran"] = True
        self._fields["url_count"] = max(0, int(url_count or 0))
        self._fields["candidate_count"] = max(0, int(candidate_count or 0))

    def mark_catalog_lookup(self, *, ran: bool, matched: bool) -> None:
        if ran:
            self._fields["catalog_lookup_ran"] = True
        if matched:
            self._fields["catalog_match"] = True

    def mark_rate_limit(self, *, checked: bool, allowed: Optional[bool]) -> None:
        if checked:
            self._fields["rate_limit_checked"] = True
        if allowed is not None:
            self._fields["rate_limit_allowed"] = bool(allowed)

    def mark_cache_status(self, status: str) -> None:
        value = str(status or "").strip().lower()
        if value in _CACHE_STATUSES:
            self._fields["cache_status"] = value

    def mark_fetch_attempted(self) -> None:
        self._fields["fetch_attempted"] = True
        self._fields["external_fetch_count"] = max(
            int(self._fields.get("external_fetch_count") or 0),
            1,
        )

    def mark_fetch_transport(
        self,
        *,
        fetched: Any,
    ) -> None:
        self._fields["fetch_completed"] = True
        ok = bool(getattr(fetched, "ok", False))
        err = _allowlisted_error_class(getattr(fetched, "error_class", ""))
        self._fields["fetch_error_class"] = err
        self._fields["http_status"] = int(getattr(fetched, "status", 0) or 0)
        ctype = str(getattr(fetched, "content_type", "") or "")
        self._fields["content_type_class"] = _content_type_class(ctype)
        body = getattr(fetched, "body", b"") or b""
        self._fields["received_bytes"] = len(body)
        self._fields["stored_decoded_bytes"] = len(body)
        self._fields["body_truncated"] = bool(getattr(fetched, "body_truncated", False))
        if ctype and "html" in ctype.lower():
            lower = body.lower()
            self._fields["html_head_complete"] = b"</head>" in lower
        self._fields["fetch_result"] = "ok" if ok else "unavailable"

    def mark_enrichment_result(self, result: Any) -> None:
        self._fields["extraction_ran"] = True
        status = str(getattr(result, "extraction_status", "") or "unavailable").lower()
        if status in _EXTRACTION_STATUSES:
            self._fields["extraction_status"] = status
        else:
            self._fields["extraction_status"] = "unavailable"
        self._fields["page_title_present"] = bool(str(getattr(result, "page_title", "") or "").strip())
        self._fields["description_present"] = bool(
            str(getattr(result, "safe_description", "") or "").strip()
        )
        self._fields["author_present"] = bool(
            str(getattr(result, "author_or_channel", "") or "").strip()
        )
        preview = dict(getattr(result, "preview_image_metadata", None) or {})
        self._fields["preview_image_present"] = bool(preview.get("url_present"))
        domain = str(getattr(result, "provider_domain", "") or "").strip().lower()
        if domain and "." in domain and "://" not in domain:
            self._fields["provider_domain"] = domain
        source = str(getattr(result, "source", "") or "").strip().lower()
        if source:
            self._fields["enrichment_source"] = source
        err = _allowlisted_error_class(getattr(result, "error_class", ""))
        if err:
            self._fields["fetch_error_class"] = err
        if status == "blocked":
            self._fields["fetch_result"] = "blocked"
        elif status == "ok":
            self._fields["fetch_result"] = "ok"
        elif self._fields.get("fetch_attempted"):
            self._fields["fetch_result"] = "unavailable"
        elif source == "tenant_catalog":
            self._fields["fetch_result"] = "not_applicable"
        elif source == "rate_limit":
            self._fields["fetch_result"] = "not_attempted"

    def mark_no_url(self) -> None:
        self._fields["fetch_result"] = "not_applicable"
        self._fields["extraction_status"] = "not_applicable"

    def mark_facts_projected(self, projected: bool) -> None:
        self._fields["facts_projected"] = bool(projected)

    def mark_model_context_bound(self, bound: bool) -> None:
        self._fields["model_context_bound"] = bool(bound)

    def mark_external_fetch_count(self, count: int) -> None:
        self._fields["external_fetch_count"] = max(0, int(count or 0))

    def record_failure(self, *, stage: str, exception: Optional[BaseException] = None) -> None:
        stage_name = str(stage or "").strip().lower()
        if stage_name in _FAILURE_STAGES:
            self._fields["failure_stage"] = stage_name
        else:
            self._fields["failure_stage"] = "attach"
        if exception is not None:
            self._fields["exception_class"] = type(exception).__name__

    def finalize(self, *, completed: bool = True) -> None:
        self._fields["attach_completed"] = bool(completed)
        self._fields["duration_ms"] = max(
            0,
            int((time.monotonic() - self._started) * 1000.0),
        )

    def to_public_dict(self) -> Dict[str, Any]:
        return dict(self._fields)


def log_url_context_attach_failure(
    *,
    tenant_id: int,
    trace: Optional[UrlContextTraceRecorder],
) -> None:
    """Production-visible failure log without raw URLs or exception messages."""
    if trace is None:
        return
    payload = trace.to_public_dict()
    if not payload.get("failure_stage") and not payload.get("exception_class"):
        return
    logger.warning(
        "[URL_CONTEXT] attach_failure tenant=%s stage=%s exception_class=%s "
        "url_count=%s fetch_attempted=%s fetch_result=%s",
        tenant_id,
        payload.get("failure_stage") or "-",
        payload.get("exception_class") or "-",
        payload.get("url_count"),
        payload.get("fetch_attempted"),
        payload.get("fetch_result"),
    )


def persist_url_context_trace(
    *,
    target: Dict[str, Any],
    trace: Optional[UrlContextTraceRecorder],
) -> None:
    """Attach trace to outbound operational metadata. Fail-open."""
    if trace is None:
        return
    try:
        target["url_context_trace"] = trace.to_public_dict()
    except Exception:  # noqa: BLE001  # noqa: silent-ok — observability must not block reply
        logger.debug("[URL_CONTEXT_TRACE] persist skipped", exc_info=True)


__all__ = [
    "SCHEMA_VERSION",
    "UrlContextTraceRecorder",
    "log_url_context_attach_failure",
    "persist_url_context_trace",
]
