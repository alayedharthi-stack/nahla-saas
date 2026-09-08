"""Structured per-turn D17 URL-context attach observability.

Observation-only: records stage progression without URLs, HTML, or customer text.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger("nahla.url_context_trace")

SCHEMA_VERSION = "2"

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

_EXCEPTION_CLASSES = frozenset(
    {
        "detector_error",
        "catalog_lookup_error",
        "rate_limit_error",
        "cache_error",
        "fetch_error",
        "extraction_error",
        "projection_error",
        "attach_error",
        "trace_merge_error",
        "unknown_error",
    }
)

_STAGE_TO_EXCEPTION_CLASS = {
    "attach": "attach_error",
    "detector": "detector_error",
    "enrichment": "fetch_error",
    "catalog_lookup": "catalog_lookup_error",
    "rate_limit": "rate_limit_error",
    "cache": "cache_error",
    "fetch": "fetch_error",
    "extraction": "extraction_error",
    "fact_projection": "projection_error",
    "trace_persist": "trace_merge_error",
}

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


def _closed_exception_class(stage: str) -> str:
    stage_name = str(stage or "").strip().lower()
    return _STAGE_TO_EXCEPTION_CLASS.get(stage_name, "unknown_error")


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
            "reply_state_url_context_present_before_compose": False,
            "attach_completed": False,
            "failure_stage": "",
            "exception_class": "",
            "duration_ms": 0,
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

    def mark_reply_state_url_context_present_before_compose(self, present: bool) -> None:
        self._fields["reply_state_url_context_present_before_compose"] = bool(present)

    def mark_external_fetch_count(self, count: int) -> None:
        self._fields["external_fetch_count"] = max(0, int(count or 0))

    def record_failure(self, *, stage: str, exception: Optional[BaseException] = None) -> None:
        stage_name = str(stage or "").strip().lower()
        if stage_name in _FAILURE_STAGES:
            self._fields["failure_stage"] = stage_name
        else:
            self._fields["failure_stage"] = "attach"
        self._fields["exception_class"] = _closed_exception_class(stage_name)

    def finalize(self, *, completed: bool = True) -> None:
        self._fields["attach_completed"] = bool(completed)
        self._fields["duration_ms"] = max(
            0,
            int((time.monotonic() - self._started) * 1000.0),
        )

    def to_sparse_public_dict(self) -> Dict[str, Any]:
        raw = self._fields
        out: Dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "attach_entered": bool(raw.get("attach_entered")),
            "detector_ran": bool(raw.get("detector_ran")),
            "url_count": max(0, int(raw.get("url_count") or 0)),
            "attach_completed": bool(raw.get("attach_completed")),
        }
        failure = str(raw.get("failure_stage") or "").strip()
        if failure:
            out["failure_stage"] = failure
        exc = str(raw.get("exception_class") or "").strip()
        if exc in _EXCEPTION_CLASSES:
            out["exception_class"] = exc

        if out["url_count"] == 0 and not failure:
            return out

        candidate_count = int(raw.get("candidate_count") or 0)
        if candidate_count > 0 and candidate_count != out["url_count"]:
            out["candidate_count"] = candidate_count

        if raw.get("catalog_lookup_ran"):
            out["catalog_lookup_ran"] = True
            if raw.get("catalog_match"):
                out["catalog_match"] = True

        if raw.get("rate_limit_checked"):
            allowed = raw.get("rate_limit_allowed")
            if allowed is False:
                out["rate_limit_checked"] = True
                out["rate_limit_allowed"] = False

        cache = str(raw.get("cache_status") or "").strip()
        if cache and cache != "not_checked":
            out["cache_status"] = cache

        if raw.get("fetch_attempted"):
            out["fetch_attempted"] = True
            fetch_count = int(raw.get("external_fetch_count") or 0)
            if fetch_count > 0:
                out["external_fetch_count"] = fetch_count
            if raw.get("fetch_completed"):
                out["fetch_completed"] = True
            fetch_result = str(raw.get("fetch_result") or "").strip()
            if fetch_result in _FETCH_RESULTS and fetch_result != "not_applicable":
                out["fetch_result"] = fetch_result
            err = str(raw.get("fetch_error_class") or "").strip()
            if err:
                out["fetch_error_class"] = err
            status = int(raw.get("http_status") or 0)
            if status > 0:
                out["http_status"] = status
            ctype = str(raw.get("content_type_class") or "").strip()
            if ctype and ctype != "unknown":
                out["content_type_class"] = ctype
            received = int(raw.get("received_bytes") or 0)
            if received > 0:
                out["received_bytes"] = received
            if raw.get("body_truncated"):
                out["body_truncated"] = True

        if raw.get("extraction_ran"):
            out["extraction_ran"] = True
            est = str(raw.get("extraction_status") or "").strip()
            if est and est not in {"not_run"}:
                out["extraction_status"] = est

        if raw.get("facts_projected"):
            out["facts_projected"] = True
        if raw.get("reply_state_url_context_present_before_compose"):
            out["reply_state_url_context_present_before_compose"] = True

        source = str(raw.get("enrichment_source") or "").strip()
        if source:
            out["enrichment_source"] = source

        duration = int(raw.get("duration_ms") or 0)
        if duration > 0:
            out["duration_ms"] = duration

        return out

    def to_public_dict(self) -> Dict[str, Any]:
        return self.to_sparse_public_dict()


def log_url_context_attach_failure(
    *,
    tenant_id: int,
    trace: Optional[UrlContextTraceRecorder],
) -> None:
    """Production-visible failure log without raw URLs or exception messages."""
    if trace is None:
        return
    payload = trace.to_sparse_public_dict()
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
    """Attach sparse trace to brain outbound dict. Fail-open."""
    if trace is None:
        return
    try:
        target["url_context_trace"] = trace.to_sparse_public_dict()
    except Exception:  # noqa: BLE001  # noqa: silent-ok — observability must not block reply
        logger.warning("[URL_CONTEXT_TRACE] persist_failed")


def merge_url_context_trace_into_extra_metadata(
    target: Dict[str, Any],
    brain_result: Optional[Dict[str, Any]],
) -> None:
    """Copy sparse url_context_trace from brain_result into outbound extra_metadata."""
    if not isinstance(target, dict) or not isinstance(brain_result, dict):
        return
    trace = brain_result.get("url_context_trace")
    if not isinstance(trace, dict) or not trace:
        return
    try:
        target["url_context_trace"] = json.loads(json.dumps(trace))
    except Exception:  # noqa: BLE001  # noqa: silent-ok — observability must not block reply
        logger.warning("[URL_CONTEXT_TRACE] merge_failed")


__all__ = [
    "SCHEMA_VERSION",
    "UrlContextTraceRecorder",
    "log_url_context_attach_failure",
    "merge_url_context_trace_into_extra_metadata",
    "persist_url_context_trace",
]
