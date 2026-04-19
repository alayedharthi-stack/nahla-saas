from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from modules.ai.commerce.permissions import CommercePermissionSet
from modules.ai.orchestrator.types import AIReplyPayload

REPO_ROOT = Path(__file__).resolve().parents[1]
ORCH_DIR = REPO_ROOT / "services" / "ai-orchestrator"
if str(ORCH_DIR) not in sys.path:
    sys.path.insert(0, str(ORCH_DIR))


def _run(coro):
    return asyncio.run(coro)


def _memory_context() -> dict:
    return {
        "store_name": "متجر تجريبي",
        "preferred_language": "ar",
        "segment": "active",
        "is_returning": True,
        "customer_id": 7,
        "customer_name": "سارة",
        "branding": "",
        "recent_messages": [],
    }


def _vet_passthrough(text: str, _grounding, mentioned_coupon_codes=None):
    del mentioned_coupon_codes
    return SimpleNamespace(vetted_text=text, was_modified=False, claims=[])


def test_generate_orchestrate_response_executes_empty_track_order_payload():
    from modules.ai.orchestrator.adapter import generate_orchestrate_response

    runtime = MagicMock()
    runtime.execute = AsyncMock(
        return_value=SimpleNamespace(
            ok=True,
            payload={
                "order": {
                    "id": "ord_1",
                    "reference_id": "REF-123",
                    "status": "قيد الشحن",
                    "total": 149.0,
                    "currency": "SAR",
                }
            },
            error=None,
        )
    )
    runtime_db = MagicMock()

    with patch(
        "modules.ai.orchestrator.adapter.generate_ai_reply",
        return_value=AIReplyPayload(
            reply_text="",
            provider_used="anthropic",
            raw_model_output={"actions": [{"type": "track_order", "payload": {}}]},
            metadata={"model": "claude-test"},
        ),
    ), patch(
        "modules.ai.orchestrator.adapter._build_runtime",
        return_value=(runtime, runtime_db),
    ), patch(
        "memory.loader.load_customer_memory",
        return_value=_memory_context(),
    ), patch(
        "commerce.permission_guard.load_permissions",
        return_value=CommercePermissionSet(tenant_id=55),
    ), patch(
        "fact_guard.data_fetcher.fetch_grounding_data",
        return_value=object(),
    ), patch(
        "fact_guard.checker.extract_coupon_codes_from_text",
        return_value=[],
    ), patch(
        "fact_guard.checker.vet_reply",
        side_effect=_vet_passthrough,
    ), patch(
        "memory.updater.update_customer_memory",
        return_value=None,
    ):
        result = _run(
            generate_orchestrate_response(
                tenant_id=55,
                customer_phone="+966500000000",
                message="وين وصل طلبي؟",
            )
        )

    runtime.execute.assert_awaited_once_with("track_order", {})
    runtime_db.close.assert_called_once()
    assert "حالة طلبك رقم REF-123: قيد الشحن" in result["reply"]
    assert "الإجمالي: 149 ريال" in result["reply"]
    assert result["actions"][0]["executable"] is True
    assert result["actions"][0]["final_payload"]["runtime_ok"] is True
    assert result["actions"][0]["final_payload"]["runtime_result"]["order"]["reference_id"] == "REF-123"
    assert result["model_used"] == "claude-test"


def test_generate_orchestrate_response_synthesizes_web_search_with_citations():
    from modules.ai.orchestrator.adapter import generate_orchestrate_response

    runtime = MagicMock()
    runtime.execute = AsyncMock(
        return_value=SimpleNamespace(
            ok=True,
            payload={
                "summary": "العسل يستخدم غذائياً وله استعمالات عامة شائعة.",
                "results": [
                    {
                        "title": "فوائد العسل",
                        "snippet": "العسل يستخدم غذائياً وله استعمالات عامة شائعة.",
                        "url": "https://example.com/honey",
                    }
                ],
                "citations": ["https://example.com/honey"],
            },
            error=None,
        )
    )
    runtime_db = MagicMock()

    with patch(
        "modules.ai.orchestrator.adapter.generate_ai_reply",
        return_value=AIReplyPayload(
            reply_text="",
            provider_used="anthropic",
            raw_model_output={"actions": [{"type": "web_search", "payload": {"query": "فوائد العسل"}}]},
            metadata={"model": "claude-test"},
        ),
    ), patch(
        "modules.ai.orchestrator.adapter._build_runtime",
        return_value=(runtime, runtime_db),
    ), patch(
        "memory.loader.load_customer_memory",
        return_value=_memory_context(),
    ), patch(
        "commerce.permission_guard.load_permissions",
        return_value=CommercePermissionSet(tenant_id=55),
    ), patch(
        "fact_guard.data_fetcher.fetch_grounding_data",
        return_value=object(),
    ), patch(
        "fact_guard.checker.extract_coupon_codes_from_text",
        return_value=[],
    ), patch(
        "fact_guard.checker.vet_reply",
        side_effect=_vet_passthrough,
    ), patch(
        "memory.updater.update_customer_memory",
        return_value=None,
    ):
        result = _run(
            generate_orchestrate_response(
                tenant_id=55,
                customer_phone="+966500000000",
                message="ما فوائد العسل؟",
            )
        )

    runtime.execute.assert_awaited_once_with("web_search", {"query": "فوائد العسل"})
    runtime_db.close.assert_called_once()
    assert "العسل يستخدم غذائياً وله استعمالات عامة شائعة." in result["reply"]
    assert "المصادر:" in result["reply"]
    assert "https://example.com/honey" in result["reply"]
    assert result["actions"][0]["final_payload"]["runtime_result"]["citations"] == ["https://example.com/honey"]
