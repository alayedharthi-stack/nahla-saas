"""Operational retry boundary for catalog-navigation natural composition."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from modules.ai.brain.persona.catalog_product_answer import (
    build_catalog_navigation_emergency_outcome,
)

CatalogComposeCallable = Callable[..., Awaitable[tuple[Any, Any, Any]]]


async def _retry_catalog_navigation_timeout_once(
    compose_callable: CatalogComposeCallable,
    nav_kwargs: dict[str, Any],
) -> tuple[Any, Any, Any]:
    """Retry one transient timeout while leaving all prose model-owned."""
    first_text, first_result, first_event = await compose_callable(**nav_kwargs)
    if str(getattr(first_result, "fallback_reason", "") or "").strip() != "timeout":
        return first_text, first_result, first_event

    try:
        text, compose_result, event = await compose_callable(**nav_kwargs)
    except Exception as exc:  # noqa: BLE001
        event = dict(first_event or {})
        event["compose_retry_count"] = 1
        event["first_compose_failure"] = "timeout"
        event["compose_retry_failure"] = f"compose_exception:{type(exc).__name__}"
        return first_text, first_result, event

    event = dict(event or {})
    event["compose_retry_count"] = 1
    event["first_compose_failure"] = "timeout"
    return text, compose_result, event


async def try_compose_catalog_navigation_browse_answer(
    **nav_kwargs: Any,
) -> tuple[Any, Any, Any]:
    """Run the existing fact-bound composer with one timeout-only retry."""
    from modules.ai.brain.persona.catalog_product_answer import (  # noqa: PLC0415
        try_compose_catalog_navigation_browse_answer as compose_once,
    )

    return await _retry_catalog_navigation_timeout_once(compose_once, nav_kwargs)


__all__ = [
    "build_catalog_navigation_emergency_outcome",
    "try_compose_catalog_navigation_browse_answer",
]
