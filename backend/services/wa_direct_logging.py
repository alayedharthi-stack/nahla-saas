"""Structured logging helpers for WA Direct Meta Graph operations."""
from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

from core.log_redaction import redact_graph_id, redact_sensitive_log_text

logger = logging.getLogger("nahla-backend")


def _coerce_secrets(secrets: Iterable[Optional[str]]) -> list[str]:
    return [str(s) for s in secrets if s]


def meta_graph_error_fields(response: Optional[dict]) -> tuple[Optional[Any], Optional[Any], bool]:
    if not isinstance(response, dict):
        return None, None, True
    if "error" not in response:
        return None, None, True
    err = response.get("error") or {}
    return err.get("code"), err.get("error_subcode"), False


def log_wa_direct_stage(
    *,
    stage: str,
    tenant_id: Optional[int] = None,
    success: Optional[bool] = None,
    http_status: Optional[int] = None,
    error_code: Optional[Any] = None,
    error_subcode: Optional[Any] = None,
    retryable: Optional[bool] = None,
    phone_number_id: Optional[str] = None,
    waba_id: Optional[str] = None,
    level: str = "info",
    tag: str = "WA Direct",
) -> None:
    safe_phone = redact_graph_id(phone_number_id) if phone_number_id else "?"
    safe_waba = redact_graph_id(waba_id) if waba_id else "?"
    message = (
        f"[{tag}] %s tenant=%s success=%s http_status=%s error_code=%s "
        "error_subcode=%s retryable=%s phone_id=%s waba_id=%s"
    )
    args = (
        stage,
        tenant_id,
        success,
        http_status,
        error_code,
        error_subcode,
        retryable,
        safe_phone,
        safe_waba,
    )
    if level == "warning":
        logger.warning(message, *args)
    elif level == "error":
        logger.error(message, *args)
    else:
        logger.info(message, *args)


def log_wa_direct_graph_result(
    *,
    stage: str,
    tenant_id: Optional[int] = None,
    response: Optional[dict] = None,
    http_status: Optional[int] = None,
    phone_number_id: Optional[str] = None,
    waba_id: Optional[str] = None,
    retryable: Optional[bool] = None,
    tag: str = "WA Direct",
) -> None:
    error_code, error_subcode, success = meta_graph_error_fields(response)
    level = "warning" if not success else "info"
    log_wa_direct_stage(
        stage=stage,
        tenant_id=tenant_id,
        success=success,
        http_status=http_status,
        error_code=error_code,
        error_subcode=error_subcode,
        retryable=retryable,
        phone_number_id=phone_number_id,
        waba_id=waba_id,
        level=level,
        tag=tag,
    )


def log_wa_direct_exception(
    stage: str,
    exc: BaseException,
    *,
    tenant_id: Optional[int] = None,
    secrets: Optional[Iterable[Optional[str]]] = None,
    tag: str = "WA Direct",
    level: str = "warning",
) -> None:
    safe = redact_sensitive_log_text(exc, secrets=_coerce_secrets(secrets or ()))
    message = "[%s] %s exception tenant=%s: %s"
    args = (tag, stage, tenant_id, safe)
    if level == "error":
        logger.error(message, *args)
    else:
        logger.warning(message, *args)
