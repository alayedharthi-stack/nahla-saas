"""Safe logging and diagnostic summaries for 360dialog operations."""
from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

from core.log_redaction import redact_graph_id, redact_sensitive_log_text

logger = logging.getLogger("nahla-backend")

_SAFE_EXTRA_KEYS = frozenset({
    "request_id",
    "waba_id_remote_present",
    "numbers_on_this_waba_count",
    "url_matches_expected",
    "remote_url_present",
    "expected_url_present",
})


def d360_extract_remote_url(cfg: Any) -> str:
    if not isinstance(cfg, dict):
        return ""
    return (
        str(cfg.get("url") or "")
        or str((cfg.get("webhook") or {}).get("url") or "")
        or str((cfg.get("data") or {}).get("url") or "")
        or ""
    )


def d360_url_flags(remote_url: Optional[str], expected_url: Optional[str]) -> dict[str, Any]:
    remote = (remote_url or "").strip()
    expected = (expected_url or "").strip()
    remote_present = bool(remote)
    expected_present = bool(expected)
    matches = bool(remote_present and expected_present and remote.rstrip("/") == expected.rstrip("/"))
    return {
        "remote_url_present": remote_present,
        "expected_url_present": expected_present,
        "url_matches_expected": matches,
        "has_url": remote_present,
    }


def d360_response_summary(response: Any) -> dict[str, Any]:
    if not isinstance(response, dict):
        return {
            "response_received": response is not None,
            "success": False,
            "http_status": None,
            "error_type": "invalid_response",
            "error_code": None,
            "retryable": True,
            "has_waba_id": False,
            "numbers_count": 0,
        }

    http_status = response.get("status_code") if isinstance(response.get("status_code"), int) else None
    err = response.get("error")
    has_error = err is not None or response.get("error") == "fail_open"
    error_type: Optional[str] = None
    error_code: Optional[Any] = None

    _SAFE_ERROR_TYPES = {
        "fail_open", "skipped", "transport_error", "invalid_response", "remote_error",
        "invalid_api_key", "url_mismatch", "rate_limited",
    }
    if isinstance(err, str):
        error_type = err if err in _SAFE_ERROR_TYPES else "remote_error"
    elif isinstance(err, dict):
        raw_type = err.get("type") or err.get("error")
        error_type = str(raw_type) if raw_type in _SAFE_ERROR_TYPES else "remote_error"
        error_code = err.get("code")
    elif err is not None:
        error_type = type(err).__name__
    elif response.get("skipped"):
        error_type = "skipped"
    elif has_error:
        error_type = "remote_error"

    nums = response.get("numbers_on_this_waba")
    numbers_count = len(nums) if isinstance(nums, list) else 0
    waba_id = response.get("waba_id")
    retryable = bool(http_status in (429, 500, 502, 503, 504) if http_status else has_error)

    return {
        "response_received": True,
        "success": not has_error,
        "http_status": http_status,
        "error_type": error_type,
        "error_code": error_code,
        "retryable": retryable,
        "has_waba_id": bool(waba_id),
        "numbers_count": numbers_count,
    }


def d360_safe_exception_fields(
    exc: BaseException,
    *,
    secrets: Optional[Iterable[Optional[str]]] = None,
    operation: Optional[str] = None,
) -> dict[str, Any]:
    secrets_list = [str(s) for s in (secrets or ()) if s]
    redact_sensitive_log_text(exc, secrets=secrets_list)
    return {
        "error_type": type(exc).__name__,
        "operation": operation,
        "retryable": True,
    }


def d360_safe_error_payload(
    exc: BaseException,
    *,
    secrets: Optional[Iterable[Optional[str]]] = None,
    operation: Optional[str] = None,
) -> dict[str, Any]:
    fields = d360_safe_exception_fields(exc, secrets=secrets, operation=operation)
    return {
        "error": fields["error_type"],
        "error_type": fields["error_type"],
        "operation": operation,
        "retryable": fields["retryable"],
    }


def d360_safe_registration_block(
    *,
    expected_url: str,
    channel_remote_url: Optional[str],
    waba_remote_url: Optional[str],
    waba_id_remote: Any = None,
    numbers_on_waba: Optional[list] = None,
) -> dict[str, Any]:
    numbers = numbers_on_waba or []
    channel_flags = d360_url_flags(channel_remote_url, expected_url)
    waba_flags = d360_url_flags(waba_remote_url, expected_url)
    phone_drift = False  # caller may override
    return {
        "expected_url_present": bool((expected_url or "").strip()),
        "channel_matches": channel_flags["url_matches_expected"],
        "waba_matches": waba_flags["url_matches_expected"],
        "channel_remote_url_present": channel_flags["remote_url_present"],
        "waba_remote_url_present": waba_flags["remote_url_present"],
        "waba_id_remote_present": bool(waba_id_remote),
        "numbers_on_this_waba_count": len(numbers),
        "phone_id_drift": phone_drift,
    }


def log_d360_verify(
    *,
    operation: str,
    tenant_id: int,
    connection_id: Optional[int] = None,
    channel_id: Optional[str] = None,
    phone_number_id: Optional[str] = None,
    waba_id: Optional[str] = None,
    api_key_present: bool = False,
    endpoint_used: str,
    response: Any,
    response_status: Optional[int] = None,
    parsed_url: Optional[str] = None,
    expected_url: Optional[str] = None,
    result: str,
    extra: Optional[dict[str, Any]] = None,
) -> None:
    summary = d360_response_summary(response)
    if response_status is not None:
        summary["http_status"] = response_status
    url_flags = d360_url_flags(parsed_url, expected_url)

    safe_extra: dict[str, Any] = {}
    if extra:
        for key, value in extra.items():
            if key in _SAFE_EXTRA_KEYS:
                safe_extra[key] = value

    fields = {
        "tenant_id": tenant_id,
        "connection_id": connection_id,
        "channel_id": redact_graph_id(channel_id) if channel_id else None,
        "phone_number_id": redact_graph_id(phone_number_id) if phone_number_id else None,
        "waba_id": redact_graph_id(waba_id) if waba_id else None,
        "api_key_present": api_key_present,
        "operation": operation,
        "endpoint_used": endpoint_used,
        "result": result,
        **summary,
        **url_flags,
        **safe_extra,
    }
    rendered = " ".join(f"{k}={v!r}" for k, v in fields.items())
    logger.info("[D360_WEBHOOK_VERIFY] %s", rendered)

def d360_live_verify_step_record(
    name: str,
    method: str,
    *,
    status_code: Optional[int],
    ok_http: bool,
    uses_channel_key: bool,
    error_type: Optional[str] = None,
) -> dict[str, Any]:
    """Safe per-step record for live-verify probes (no URLs, bodies, or raw IDs)."""
    return {
        "step": name,
        "method": str(method or "").upper(),
        "status_code": status_code,
        "ok": bool(ok_http),
        "uses_channel_key": bool(uses_channel_key),
        "error_type": error_type,
    }


def d360_sanitize_live_verify_probe(probe: Any) -> dict[str, Any]:
    """Return an API-safe live-verify probe without raw URLs, bodies, or Graph IDs."""
    if not isinstance(probe, dict):
        return {
            "coexistence_mode": False,
            "composite_alive": False,
            "channel_auth_revoked": False,
            "steps": [],
            "summary": "",
        }

    safe_steps: list[dict[str, Any]] = []
    for step in probe.get("steps") or []:
        if not isinstance(step, dict):
            continue
        safe_steps.append(
            d360_live_verify_step_record(
                str(step.get("step") or "unknown"),
                str(step.get("method") or "GET"),
                status_code=step.get("status_code") if isinstance(step.get("status_code"), int) else None,
                ok_http=bool(step.get("ok")),
                uses_channel_key=bool(step.get("uses_channel_key")),
                error_type=step.get("error_type"),
            )
        )

    return {
        "coexistence_mode": bool(probe.get("coexistence_mode")),
        "composite_alive": bool(probe.get("composite_alive")),
        "channel_auth_revoked": bool(probe.get("channel_auth_revoked")),
        "steps": safe_steps,
        "summary": str(probe.get("summary") or ""),
    }
