"""Structured per-turn D17 URL-context attach observability.

Observation-only: records stage progression without URLs, HTML, or customer text.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import Any, Dict, Optional, Union

logger = logging.getLogger("nahla.url_context_trace")

SCHEMA_VERSION = "2"

JsonPrimitive = Union[str, int, bool]

_MAX_URL_COUNT = 32
_MAX_CANDIDATE_COUNT = 32
_MAX_FETCH_COUNT = 32
_MAX_HTTP_STATUS = 599
_MAX_RECEIVED_BYTES = 50_000_000
_MAX_DURATION_MS = 600_000

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
        "trace_merge",
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
    "trace_merge": "trace_merge_error",
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
_CONTENT_TYPE_CLASSES = frozenset({"json", "html", "other"})
_ENRICHMENT_SOURCES = frozenset(
    {
        "safe_http",
        "tenant_catalog",
        "rate_limit",
        "cache",
        "html_metadata",
        "json_metadata",
        "json_ld",
        "oembed",
        "readable_text",
    }
)
_METADATA_SOURCES = frozenset(
    {
        "html_metadata",
        "json_ld",
        "oembed",
        "readable_text",
    }
)
_METADATA_QUALITY = frozenset({"useful", "thin", "empty"})
_STANDARD_METADATA_STATUS = frozenset({"ok", "thin", "empty", "not_run"})
_STRUCTURED_DATA_STATUS = frozenset({"ok", "empty", "not_run"})
_OEMBED_DISCOVERY_STATUS = frozenset({"declared", "adapter", "none", "not_run"})
_PROVIDER_ENRICHMENT_STATUS = frozenset(
    {
        "ok",
        "failed",
        "blocked",
        "invalid_json",
        "empty",
        "skipped",
        "not_needed",
        "not_run",
    }
)
_READABLE_TEXT_STATUS = frozenset({"ok", "empty", "not_run"})
_PROVIDER_ADAPTERS = frozenset({"tiktok"})
_FETCH_ERROR_CLASSES = frozenset(
    {
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
        "other",
    }
)

_KNOWN_TRACE_KEYS = frozenset(
    {
        "schema_version",
        "attach_entered",
        "detector_ran",
        "url_count",
        "attach_completed",
        "failure_stage",
        "exception_class",
        "candidate_count",
        "catalog_lookup_ran",
        "catalog_match",
        "rate_limit_checked",
        "rate_limit_allowed",
        "cache_status",
        "fetch_attempted",
        "external_fetch_count",
        "fetch_completed",
        "fetch_result",
        "fetch_error_class",
        "http_status",
        "content_type_class",
        "received_bytes",
        "body_truncated",
        "extraction_ran",
        "extraction_status",
        "facts_projected",
        "reply_state_url_context_present_before_compose",
        "enrichment_source",
        "metadata_sources_attempted",
        "metadata_source_selected",
        "standard_metadata_status",
        "structured_data_status",
        "oembed_discovery_status",
        "provider_adapter",
        "provider_enrichment_status",
        "readable_text_status",
        "metadata_quality",
        "useful_metadata_present",
        "duration_ms",
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
    return raw if raw in _FETCH_ERROR_CLASSES else "other"


def _closed_exception_class(stage: str) -> str:
    stage_name = str(stage or "").strip().lower()
    return _STAGE_TO_EXCEPTION_CLASS.get(stage_name, "unknown_error")


def _is_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _is_nonneg_int(value: Any, *, maximum: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if 0 <= value <= maximum:
        return value
    return None



def _safe_trace_merge_rejected() -> Dict[str, JsonPrimitive]:
    return {
        "schema_version": SCHEMA_VERSION,
        "attach_entered": False,
        "detector_ran": False,
        "url_count": 0,
        "attach_completed": False,
        "failure_stage": "trace_merge",
        "exception_class": "trace_merge_error",
    }


def sanitize_url_context_trace(value: object) -> Dict[str, JsonPrimitive] | None:
    """Single owner for public sparse url_context_trace schema (v2)."""
    if not isinstance(value, Mapping):
        return None

    version = value.get("schema_version")
    if version != SCHEMA_VERSION and str(version) != SCHEMA_VERSION:
        return None

    attach_entered = _is_bool(value.get("attach_entered"))
    detector_ran = _is_bool(value.get("detector_ran"))
    url_count = _is_nonneg_int(value.get("url_count"), maximum=_MAX_URL_COUNT)
    attach_completed = _is_bool(value.get("attach_completed"))
    if attach_entered is None or detector_ran is None or url_count is None or attach_completed is None:
        return None

    typed: Dict[str, JsonPrimitive] = {
        "schema_version": SCHEMA_VERSION,
        "attach_entered": attach_entered,
        "detector_ran": detector_ran,
        "url_count": url_count,
        "attach_completed": attach_completed,
    }

    failure_stage = str(value.get("failure_stage") or "").strip().lower()
    if failure_stage in _FAILURE_STAGES:
        typed["failure_stage"] = failure_stage
        exception_class = str(value.get("exception_class") or "").strip()
        if exception_class in _EXCEPTION_CLASSES:
            typed["exception_class"] = exception_class
        else:
            typed["exception_class"] = _closed_exception_class(failure_stage)

    if url_count == 0 and not typed.get("failure_stage"):
        return dict(typed)

    candidate_count = _is_nonneg_int(value.get("candidate_count"), maximum=_MAX_CANDIDATE_COUNT)
    if candidate_count is not None and candidate_count > 0 and candidate_count != url_count:
        typed["candidate_count"] = candidate_count

    catalog_lookup_ran = _is_bool(value.get("catalog_lookup_ran"))
    if catalog_lookup_ran is True:
        typed["catalog_lookup_ran"] = True
        catalog_match = _is_bool(value.get("catalog_match"))
        if catalog_match is True:
            typed["catalog_match"] = True

    rate_limit_checked = _is_bool(value.get("rate_limit_checked"))
    rate_limit_allowed = _is_bool(value.get("rate_limit_allowed"))
    if rate_limit_checked is True and rate_limit_allowed is False:
        typed["rate_limit_checked"] = True
        typed["rate_limit_allowed"] = False

    cache_status = str(value.get("cache_status") or "").strip().lower()
    if cache_status in _CACHE_STATUSES and cache_status != "not_checked":
        typed["cache_status"] = cache_status

    fetch_attempted = _is_bool(value.get("fetch_attempted"))
    if fetch_attempted is True:
        typed["fetch_attempted"] = True
        fetch_count = _is_nonneg_int(value.get("external_fetch_count"), maximum=_MAX_FETCH_COUNT)
        if fetch_count is not None and fetch_count > 0:
            typed["external_fetch_count"] = fetch_count
        fetch_completed = _is_bool(value.get("fetch_completed"))
        if fetch_completed is True:
            typed["fetch_completed"] = True
        fetch_result = str(value.get("fetch_result") or "").strip()
        if fetch_result in _FETCH_RESULTS and fetch_result != "not_applicable":
            typed["fetch_result"] = fetch_result
        fetch_error_class = str(value.get("fetch_error_class") or "").strip().lower()
        if fetch_error_class in _FETCH_ERROR_CLASSES:
            typed["fetch_error_class"] = fetch_error_class
        http_status = _is_nonneg_int(value.get("http_status"), maximum=_MAX_HTTP_STATUS)
        if http_status is not None and http_status > 0:
            typed["http_status"] = http_status
        content_type_class = str(value.get("content_type_class") or "").strip().lower()
        if content_type_class in _CONTENT_TYPE_CLASSES:
            typed["content_type_class"] = content_type_class
        received_bytes = _is_nonneg_int(value.get("received_bytes"), maximum=_MAX_RECEIVED_BYTES)
        if received_bytes is not None and received_bytes > 0:
            typed["received_bytes"] = received_bytes
        body_truncated = _is_bool(value.get("body_truncated"))
        if body_truncated is True:
            typed["body_truncated"] = True

    extraction_ran = _is_bool(value.get("extraction_ran"))
    if extraction_ran is True:
        typed["extraction_ran"] = True
        extraction_status = str(value.get("extraction_status") or "").strip().lower()
        if extraction_status in _EXTRACTION_STATUSES and extraction_status != "not_run":
            typed["extraction_status"] = extraction_status

    facts_projected = _is_bool(value.get("facts_projected"))
    if facts_projected is True:
        typed["facts_projected"] = True

    reply_state_present = _is_bool(value.get("reply_state_url_context_present_before_compose"))
    if reply_state_present is True:
        typed["reply_state_url_context_present_before_compose"] = True

    enrichment_source = str(value.get("enrichment_source") or "").strip().lower()
    if enrichment_source in _ENRICHMENT_SOURCES:
        typed["enrichment_source"] = enrichment_source

    metadata_sources_attempted = str(value.get("metadata_sources_attempted") or "").strip().lower()
    if metadata_sources_attempted:
        parts = [part for part in metadata_sources_attempted.split(",") if part in _METADATA_SOURCES]
        if parts:
            typed["metadata_sources_attempted"] = ",".join(parts)

    metadata_source_selected = str(value.get("metadata_source_selected") or "").strip().lower()
    if metadata_source_selected in _METADATA_SOURCES:
        typed["metadata_source_selected"] = metadata_source_selected

    standard_metadata_status = str(value.get("standard_metadata_status") or "").strip().lower()
    if standard_metadata_status in _STANDARD_METADATA_STATUS and standard_metadata_status != "not_run":
        typed["standard_metadata_status"] = standard_metadata_status

    structured_data_status = str(value.get("structured_data_status") or "").strip().lower()
    if structured_data_status in _STRUCTURED_DATA_STATUS and structured_data_status != "not_run":
        typed["structured_data_status"] = structured_data_status

    oembed_discovery_status = str(value.get("oembed_discovery_status") or "").strip().lower()
    if oembed_discovery_status in _OEMBED_DISCOVERY_STATUS and oembed_discovery_status != "not_run":
        typed["oembed_discovery_status"] = oembed_discovery_status

    provider_adapter = str(value.get("provider_adapter") or "").strip().lower()
    if provider_adapter in _PROVIDER_ADAPTERS:
        typed["provider_adapter"] = provider_adapter

    provider_enrichment_status = str(value.get("provider_enrichment_status") or "").strip().lower()
    if (
        provider_enrichment_status in _PROVIDER_ENRICHMENT_STATUS
        and provider_enrichment_status != "not_run"
    ):
        typed["provider_enrichment_status"] = provider_enrichment_status

    readable_text_status = str(value.get("readable_text_status") or "").strip().lower()
    if readable_text_status in _READABLE_TEXT_STATUS and readable_text_status != "not_run":
        typed["readable_text_status"] = readable_text_status

    metadata_quality = str(value.get("metadata_quality") or "").strip().lower()
    if metadata_quality in _METADATA_QUALITY:
        typed["metadata_quality"] = metadata_quality

    useful_metadata_present = _is_bool(value.get("useful_metadata_present"))
    if useful_metadata_present is not None:
        typed["useful_metadata_present"] = useful_metadata_present

    duration_ms = _is_nonneg_int(value.get("duration_ms"), maximum=_MAX_DURATION_MS)
    if duration_ms is not None and duration_ms > 0:
        typed["duration_ms"] = duration_ms

    return dict(typed)


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
            "metadata_sources_attempted": "",
            "metadata_source_selected": "",
            "standard_metadata_status": "not_run",
            "structured_data_status": "not_run",
            "oembed_discovery_status": "not_run",
            "provider_adapter": "",
            "provider_enrichment_status": "not_run",
            "readable_text_status": "not_run",
            "metadata_quality": "empty",
            "useful_metadata_present": False,
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
        metadata_quality = str(getattr(result, "metadata_quality", "") or "").strip().lower()
        if metadata_quality in _METADATA_QUALITY:
            self._fields["metadata_quality"] = metadata_quality
        useful = getattr(result, "useful_metadata_present", None)
        if isinstance(useful, bool):
            self._fields["useful_metadata_present"] = useful
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

    def mark_pipeline_state(self, *, state: Any) -> None:
        fields = {}
        try:
            fields = state.to_trace_fields() if hasattr(state, "to_trace_fields") else {}
        except Exception:  # noqa: BLE001  # noqa: silent-ok — trace must not block enrichment
            fields = {}
        for key, value in dict(fields or {}).items():
            if value in (None, ""):
                continue
            self._fields[key] = value
        fetch_count = int(fields.get("external_fetch_count") or self._fields.get("external_fetch_count") or 0)
        if fetch_count > 0:
            self._fields["external_fetch_count"] = fetch_count

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

    def _build_sparse_candidate(self) -> Dict[str, Any]:
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

        for key in (
            "metadata_sources_attempted",
            "metadata_source_selected",
            "standard_metadata_status",
            "structured_data_status",
            "oembed_discovery_status",
            "provider_adapter",
            "provider_enrichment_status",
            "readable_text_status",
            "metadata_quality",
        ):
            value = str(raw.get(key) or "").strip()
            if value and value != "not_run":
                out[key] = value

        useful = raw.get("useful_metadata_present")
        if isinstance(useful, bool):
            out["useful_metadata_present"] = useful

        duration = int(raw.get("duration_ms") or 0)
        if duration > 0:
            out["duration_ms"] = duration

        return out

    def to_sparse_public_dict(self) -> Dict[str, JsonPrimitive]:
        sanitized = sanitize_url_context_trace(self._build_sparse_candidate())
        if sanitized is not None:
            return sanitized
        return {
            "schema_version": SCHEMA_VERSION,
            "attach_entered": False,
            "detector_ran": False,
            "url_count": 0,
            "attach_completed": False,
        }

    def to_public_dict(self) -> Dict[str, JsonPrimitive]:
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
        "[URL_CONTEXT] event=url_context_attach_failure tenant=%s failure_stage=%s error_class=%s "
        "url_count=%s fetch_attempted=%s fetch_result=%s",
        tenant_id,
        payload.get("failure_stage") or "-",
        payload.get("exception_class") or "-",
        payload.get("url_count"),
        payload.get("fetch_attempted"),
        payload.get("fetch_result"),
    )


def log_url_context_attach_skipped(*, tenant_id: int) -> None:
    """Safe attach-skip log when trace recorder is unavailable."""
    logger.warning(
        "[URL_CONTEXT] event=url_context_attach_failure tenant=%s failure_stage=attach error_class=attach_error",
        tenant_id,
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
        sanitized = sanitize_url_context_trace(trace.to_sparse_public_dict())
        if sanitized is not None:
            target["url_context_trace"] = sanitized
    except Exception:  # noqa: BLE001  # noqa: silent-ok — observability must not block reply
        logger.warning(
            "[URL_CONTEXT_TRACE] event=url_context_trace_persist_failed failure_stage=trace_persist error_class=trace_merge_error"
        )


def merge_url_context_trace_into_extra_metadata(
    target: Dict[str, Any],
    brain_result: Optional[Dict[str, Any]],
) -> None:
    """Copy sanitized url_context_trace from brain_result into outbound extra_metadata."""
    if not isinstance(target, dict) or not isinstance(brain_result, dict):
        return
    trace = brain_result.get("url_context_trace")
    if not isinstance(trace, Mapping) or not trace:
        return
    try:
        sanitized = sanitize_url_context_trace(trace)
        if sanitized is None:
            logger.warning(
                "[URL_CONTEXT_TRACE] event=url_context_trace_merge_rejected failure_stage=trace_merge error_class=trace_merge_error"
            )
            sanitized = _safe_trace_merge_rejected()
        target["url_context_trace"] = dict(sanitized)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — observability must not block reply
        logger.warning(
            "[URL_CONTEXT_TRACE] event=url_context_trace_merge_failed failure_stage=trace_merge error_class=trace_merge_error"
        )


__all__ = [
    "SCHEMA_VERSION",
    "JsonPrimitive",
    "UrlContextTraceRecorder",
    "log_url_context_attach_failure",
    "log_url_context_attach_skipped",
    "merge_url_context_trace_into_extra_metadata",
    "persist_url_context_trace",
    "sanitize_url_context_trace",
]
