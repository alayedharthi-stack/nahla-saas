"""
final_reply_source.py
─────────────────────
Safe telemetry for the customer-facing reply assembly path.
Never logs full inbound text, reply text, or KB content.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Dict, Optional

_log = logging.getLogger("nahla.brain.final_reply_source")


def reply_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def resolve_final_source(
    *,
    chosen_path: str,
    guard_replaced: Dict[str, bool],
    humanizer_changed: bool,
) -> str:
    if humanizer_changed:
        if guard_replaced.get("product_availability_truth_guard"):
            return "style_layer_after_availability_guard"
        if guard_replaced.get("commerce_reply_quality_guard"):
            return "style_layer_after_quality_guard"
        return "humanizer"
    if guard_replaced.get("product_availability_truth_guard"):
        return "availability_guard_operational"
    if guard_replaced.get("commerce_reply_quality_guard"):
        return "quality_guard_operational"
    path = (chosen_path or "").strip().lower()
    if path.startswith("llm"):
        return "llm_compose"
    if path:
        return path
    return "unknown"


def log_final_reply_source(
    *,
    tenant_id: Optional[int] = None,
    intent: str = "",
    chosen_path: str = "",
    final_source: str = "",
    llm_model: str = "",
    llm_provider: str = "",
    truth_guard_changed: bool = False,
    quality_guard_changed: bool = False,
    humanizer_changed: bool = False,
    post_guard_rewrite_applied: bool = False,
    product_category: str = "",
    emoji_bucket: str = "",
    style_signature: str = "",
    reply_text: str = "",
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    try:
        payload: Dict[str, Any] = {
            "tenant_id": tenant_id,
            "intent": (intent or "").strip().lower() or None,
            "chosen_path": chosen_path or None,
            "final_source": final_source or None,
            "llm_model": llm_model or None,
            "llm_provider": llm_provider or None,
            "truth_guard_changed": bool(truth_guard_changed),
            "quality_guard_changed": bool(quality_guard_changed),
            "humanizer_changed": bool(humanizer_changed),
            "post_guard_rewrite_applied": bool(post_guard_rewrite_applied),
            "product_category": product_category or None,
            "emoji_bucket": emoji_bucket or None,
            "style_signature": style_signature or None,
            "reply_hash": reply_hash(reply_text) if reply_text else None,
        }
        if extra:
            for key, value in extra.items():
                if value is not None:
                    payload[key] = value
        payload = {k: v for k, v in payload.items() if v is not None}
        _log.info("[FINAL_REPLY_SOURCE] %s", json.dumps(payload, ensure_ascii=False))
    except Exception:  # noqa: BLE001
        _log.exception("[FINAL_REPLY_SOURCE] tagging_failed")


__all__ = ["log_final_reply_source", "reply_hash", "resolve_final_source"]
