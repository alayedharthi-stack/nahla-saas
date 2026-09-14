"""Uniform, read-only tool timeout and failure behavior."""
from __future__ import annotations

import json
from typing import Any

from agents import RunContextWrapper, function_tool


COMMERCE_READ_TOOL_TIMEOUT_SECONDS = 8.0


def _model_visible_failure(reason: str) -> str:
    return json.dumps(
        {
            "status": "error",
            "failure_reason": reason,
            "retryable": False,
        },
        separators=(",", ":"),
    )


def _failure_handler(tool_name: str):
    def handler(_context: RunContextWrapper[Any], error: Exception) -> str:
        return _model_visible_failure(
            f"tool_error:{tool_name}:{type(error).__name__}"[:160]
        )

    return handler


def _timeout_handler(tool_name: str):
    def handler(_context: RunContextWrapper[Any], _error: Exception) -> str:
        return _model_visible_failure(f"tool_timeout:{tool_name}")

    return handler


def commerce_read_tool(tool_name: str, *, is_enabled: Any):
    """Create the common SDK decorator for a named read-only Commerce tool."""
    return function_tool(
        name_override=tool_name,
        timeout=COMMERCE_READ_TOOL_TIMEOUT_SECONDS,
        timeout_behavior="error_as_result",
        timeout_error_function=_timeout_handler(tool_name),
        failure_error_function=_failure_handler(tool_name),
        is_enabled=is_enabled,
    )


__all__ = ["COMMERCE_READ_TOOL_TIMEOUT_SECONDS", "commerce_read_tool"]
