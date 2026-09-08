"""D17 attach-stage observability via real MerchantBrain.process.

INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from contextlib import ExitStack
from types import SimpleNamespace
from typing import Any, Callable, Optional
from unittest.mock import AsyncMock, MagicMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
_REPO = os.path.abspath(os.path.join(_BACKEND, ".."))
for _p in (_REPO, _BACKEND, os.path.join(_REPO, "database")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.facts.url_context_facts import URL_CONTEXT_USER_TURN_BEGIN  # noqa: E402
from modules.ai.brain.observability.url_context_trace import UrlContextTraceRecorder  # noqa: E402
from modules.ai.brain.pipeline import get_brain  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    ActionResult,
    CommerceFacts,
    MerchantConversationState,
    OrderPreparationState,
)
from modules.ai.orchestrator.types import AIReplyPayload  # noqa: E402
from services.safe_http_fetch import SafeHttpResult  # noqa: E402
from services.url_context import reset_url_context_cache  # noqa: E402

from test_d17_url_context_enrichment import (  # noqa: E402
    CATALOG_PRODUCT_URL,
    GENERIC_MERCHANT,
    GENERIC_PRODUCT_ID,
    GENERIC_PRODUCT_TITLE,
    HTML_FIXTURE,
    MODEL_CANDIDATE,
    PUBLIC_PAGE_URL,
    SESSION_PHONE,
    TIKTOK_SHAPED_URL,
    _active_checkout_state,
    _line_item,
    _ok_fetch,
    _user_turn_json,
)

SENSITIVE_URL = (
    "https://shop.example.test/p/shirt"
    "?utm_source=secret_token&user=966511000099#preview"
)
FORBIDDEN_TRACE_PATTERNS = (
    r"utm_source",
    r"secret_token",
    r"966511000099",
    r"#preview",
    r"https://shop\.example\.test/p/shirt",
    r"<html",
    r"og:title",
    r"provider_domain",
    r"RuntimeError",
)


def _trace_bytes(trace: dict[str, Any]) -> int:
    return len(json.dumps(trace, ensure_ascii=False).encode("utf-8"))


def _trace(out: dict[str, Any]) -> dict[str, Any]:
    row = dict(out.get("url_context_trace") or {})
    assert row.get("schema_version")
    return row


def _assert_trace_privacy(blob: str) -> None:
    for pattern in FORBIDDEN_TRACE_PATTERNS:
        assert not re.search(pattern, blob, re.IGNORECASE)


def _brain_process_stack(
    *,
    brain: Any,
    state: MerchantConversationState,
    message: str,
    tenant_id: int = 9001,
    captured: Optional[dict[str, Any]] = None,
    fetch_impl: Any = None,
    extra_patches: Optional[list[Any]] = None,
) -> ExitStack:
    captured = captured if captured is not None else {}
    fetch_calls: list[str] = []
    captured["fetch_calls"] = fetch_calls

    async def _fake_fetch(url: str, **_kwargs: Any) -> SafeHttpResult:
        fetch_calls.append(url)
        if fetch_impl is not None:
            got = fetch_impl(url)
            return await got if asyncio.iscoroutine(got) else got
        return _ok_fetch(url, HTML_FIXTURE)

    def _fake_generate(**kwargs: Any) -> AIReplyPayload:
        captured["compose_count"] = int(captured.get("compose_count") or 0) + 1
        captured["message"] = str(kwargs.get("message") or "")
        captured["history"] = list(kwargs.get("history") or [])
        captured["prompt"] = str(
            (kwargs.get("prompt_overrides") or {}).get("__full_system_prompt") or ""
        )
        parsed = _user_turn_json(captured["message"], captured["history"])
        captured["parsed"] = parsed
        title = str(parsed.get("page_title") or "")
        captured["model_text"] = f"model-owned:{title}" if title else MODEL_CANDIDATE
        return AIReplyPayload(reply_text=captured["model_text"])

    stack = ExitStack()
    stack.enter_context(patch("core.billing.has_billing_access", return_value=True))
    stack.enter_context(
        patch(
            "core.wa_usage.check_limit",
            return_value=SimpleNamespace(allowed=True, used_total=0, limit=1000, reason=""),
        )
    )
    stack.enter_context(
        patch(
            "core.ai_disabled_gate.is_ai_disabled_for_conversation",
            return_value=SimpleNamespace(disabled=False, reason=None),
        )
    )
    stack.enter_context(patch("core.store_knowledge.build_merchant_context", return_value={}))
    stack.enter_context(patch("core.active_order_context.load_commerce_bundle_from_db", return_value={}))
    stack.enter_context(patch.object(brain._policy_gate, "gate", side_effect=lambda d, _ctx: d))
    stack.enter_context(patch.object(brain._state_store, "load", return_value=state))
    stack.enter_context(patch.object(brain._state_store, "save"))
    stack.enter_context(
        patch.object(
            brain._facts_loader,
            "load",
            return_value=CommerceFacts(
                store_name=GENERIC_MERCHANT,
                has_products=True,
                product_count=4,
                in_stock_count=4,
                orderable=True,
                snapshot_fresh=True,
            ),
        )
    )
    stack.enter_context(patch.object(brain._memory_updater, "update"))
    stack.enter_context(
        patch.object(
            brain._executor,
            "execute",
            new=AsyncMock(return_value=ActionResult(success=True, data={})),
        )
    )
    stack.enter_context(patch("modules.ai.brain.intent.slot_extractor.extract_slots", new=AsyncMock(return_value={})))
    stack.enter_context(patch("modules.ai.orchestrator.adapter.generate_ai_reply", side_effect=_fake_generate))
    stack.enter_context(
        patch(
            "modules.ai.brain.persona.integration.try_enforce_phatic_llm_persona_compose",
            return_value=None,
        )
    )
    stack.enter_context(patch("services.url_context.fetch_url_async", side_effect=_fake_fetch))
    stack.enter_context(patch("urllib.request.urlopen", side_effect=AssertionError("external fetch")))
    for item in extra_patches or []:
        stack.enter_context(item)
    return stack


async def _run_brain_process(
    *,
    message: str,
    tenant_id: int = 9001,
    state: Optional[MerchantConversationState] = None,
    extra_patches: Optional[list[Any]] = None,
    after_reset: Optional[Callable[[], None]] = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    reset_url_context_cache()
    if after_reset is not None:
        after_reset()
    brain = get_brain()
    state = state or _active_checkout_state()
    captured: dict[str, Any] = {"compose_count": 0}
    stack = _brain_process_stack(
        brain=brain,
        state=state,
        message=message,
        tenant_id=tenant_id,
        captured=captured,
        extra_patches=extra_patches,
    )
    with stack:
        out = await brain.process(
            db=MagicMock(),
            tenant_id=tenant_id,
            customer_phone=SESSION_PHONE,
            message=message,
            history=[],
            profile={"preferred_language": "ar", "inbound_metadata": {"source_type": "text"}},
            conversation_id=56,
        )
    return out, captured


def test_a_success_url_trace_via_merchant_brain_process() -> None:
    out, captured = asyncio.run(_run_brain_process(message=PUBLIC_PAGE_URL))
    trace = _trace(out)
    assert trace["attach_entered"] is True
    assert trace["detector_ran"] is True
    assert trace["url_count"] >= 1
    assert trace.get("candidate_count", trace["url_count"]) >= 1
    assert trace["fetch_attempted"] is True
    assert trace["external_fetch_count"] == 1
    assert trace["facts_projected"] is True
    assert trace["reply_state_url_context_present_before_compose"] is True
    assert trace["attach_completed"] is True
    assert _trace_bytes(trace) <= 700
    assert URL_CONTEXT_USER_TURN_BEGIN in captured.get("message", "")
    _assert_trace_privacy(json.dumps(trace, ensure_ascii=False))


def test_b_no_url_trace() -> None:
    out, _ = asyncio.run(_run_brain_process(message="مرحبا"))
    trace = _trace(out)
    assert trace["attach_entered"] is True
    assert trace["detector_ran"] is True
    assert trace["url_count"] == 0
    assert "fetch_attempted" not in trace
    assert "external_fetch_count" not in trace
    assert _trace_bytes(trace) <= 160


def test_c_catalog_precedence_trace() -> None:
    product = {
        "product_id": "1",
        "external_id": GENERIC_PRODUCT_ID,
        "title": GENERIC_PRODUCT_TITLE,
        "price": 10,
    }

    def _lookup(_db: Any, _tenant: int, _url: str) -> dict[str, Any]:
        return product

    out, _ = asyncio.run(
        _run_brain_process(
            message=CATALOG_PRODUCT_URL,
            extra_patches=[
                patch(
                    "services.url_context.lookup_catalog_product_by_url",
                    side_effect=_lookup,
                )
            ],
        )
    )
    trace = _trace(out)
    assert trace["catalog_lookup_ran"] is True
    assert trace["catalog_match"] is True
    assert "external_fetch_count" not in trace
    assert "fetch_attempted" not in trace
    assert trace["facts_projected"] is True
    assert trace.get("enrichment_source") == "tenant_catalog"


def test_d_rate_limited_trace() -> None:
    out, captured = asyncio.run(
        _run_brain_process(
            message=PUBLIC_PAGE_URL,
            extra_patches=[patch("services.url_context.check_rate_limit", return_value=False)],
        )
    )
    trace = _trace(out)
    assert trace["rate_limit_checked"] is True
    assert trace["rate_limit_allowed"] is False
    assert "fetch_attempted" not in trace
    assert "external_fetch_count" not in trace
    assert captured.get("compose_count") == 1


def test_e_cache_hit_trace() -> None:
    from services.url_context import _cache_put, _from_fetch  # noqa: PLC0415

    def _prime_cache() -> None:
        cached = _from_fetch(PUBLIC_PAGE_URL, _ok_fetch(PUBLIC_PAGE_URL, HTML_FIXTURE))
        _cache_put(9001, PUBLIC_PAGE_URL, cached)

    out, _ = asyncio.run(
        _run_brain_process(message=PUBLIC_PAGE_URL, after_reset=_prime_cache)
    )
    trace = _trace(out)
    assert trace["cache_status"] == "hit"
    assert "fetch_attempted" not in trace
    assert "external_fetch_count" not in trace


def test_f_fetch_unavailable_trace() -> None:
    async def _bad_fetch(url: str, **_kwargs: Any) -> SafeHttpResult:
        return SafeHttpResult(
            ok=False,
            url=url,
            final_url=url,
            status=503,
            content_type="text/html",
            body=b"",
            error_class="unavailable",
        )

    out, captured = asyncio.run(
        _run_brain_process(
            message=PUBLIC_PAGE_URL,
            extra_patches=[patch("services.url_context.fetch_url_async", side_effect=_bad_fetch)],
        )
    )
    trace = _trace(out)
    assert trace["fetch_attempted"] is True
    assert trace["fetch_completed"] is True
    assert trace["fetch_result"] == "unavailable"
    assert captured.get("compose_count") == 1


def test_g_attach_exception_records_trace() -> None:
    out, captured = asyncio.run(
        _run_brain_process(
            message=PUBLIC_PAGE_URL,
            extra_patches=[
                patch(
                    "services.url_context.begin_url_context_turn",
                    side_effect=RuntimeError("probe attach"),
                )
            ],
        )
    )
    trace = _trace(out)
    assert trace["attach_entered"] is True
    assert trace["failure_stage"] == "attach"
    assert trace["exception_class"] == "attach_error"
    assert "probe" not in json.dumps(trace)
    assert _trace_bytes(trace) <= 700
    assert captured.get("compose_count") == 1


def test_h_pair_like_process_path() -> None:
    out, captured = asyncio.run(_run_brain_process(message=TIKTOK_SHAPED_URL))
    trace = _trace(out)
    assert trace["attach_entered"] is True
    assert trace["url_count"] >= 1
    assert trace["facts_projected"] is True
    assert captured.get("compose_count") == 1
    assert "CHECKOUT_IDENTITY_SHIPPING_FACTS" not in json.dumps(out)
    assert "customer_phone" not in json.dumps(trace)


def test_i_privacy_sensitive_url() -> None:
    records: list[str] = []

    class _Cap(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(self.format(record))

    root = logging.getLogger("nahla.url_context_trace")
    handler = _Cap()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        out, captured = asyncio.run(_run_brain_process(message=SENSITIVE_URL))
    finally:
        root.removeHandler(handler)
    trace_blob = json.dumps(out.get("url_context_trace"), ensure_ascii=False)
    _assert_trace_privacy(trace_blob)
    for line in records:
        _assert_trace_privacy(line)
    prompt = str(captured.get("prompt") or "")
    assert "url_context_trace" not in prompt
    assert '"attach_entered"' not in prompt
    parsed_blob = json.dumps(captured.get("parsed") or {}, ensure_ascii=False)
    for trace_key in ("attach_entered", "detector_ran", "fetch_error_class", "failure_stage"):
        assert trace_key not in parsed_blob


def test_j_concurrent_traces_do_not_mix() -> None:
    async def _main() -> None:
        (out_a, _), (out_b, _) = await asyncio.gather(
            _run_brain_process(message=PUBLIC_PAGE_URL, tenant_id=9001),
            _run_brain_process(message="مرحبا", tenant_id=9002),
        )
        a = _trace(out_a)
        b = _trace(out_b)
        assert a["url_count"] >= 1
        assert b["url_count"] == 0
        assert a["fetch_attempted"] is True
        assert "fetch_attempted" not in b

    asyncio.run(_main())


def test_k_observability_writer_failure_non_blocking() -> None:
    records: list[str] = []

    class _Cap(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(self.format(record))

    root = logging.getLogger("nahla.url_context_trace")
    handler = _Cap()
    root.addHandler(handler)
    root.setLevel(logging.WARNING)

    try:
        out, captured = asyncio.run(
            _run_brain_process(
                message=PUBLIC_PAGE_URL,
                extra_patches=[
                    patch.object(
                        UrlContextTraceRecorder,
                        "to_sparse_public_dict",
                        side_effect=RuntimeError("trace writer failed"),
                    )
                ],
            )
        )
    finally:
        root.removeHandler(handler)

    assert out.get("reply")
    assert captured.get("compose_count") == 1
    assert any("persist_failed" in line or "trace_persist" in line for line in records)


def test_l_behavior_regression_unchanged_with_trace() -> None:
    out1, cap1 = asyncio.run(_run_brain_process(message=PUBLIC_PAGE_URL))
    out2, cap2 = asyncio.run(_run_brain_process(message=PUBLIC_PAGE_URL))
    assert cap1.get("compose_count") == cap2.get("compose_count")
    assert cap1.get("model_text") == cap2.get("model_text")
    assert _trace(out1)["external_fetch_count"] == _trace(out2)["external_fetch_count"]
