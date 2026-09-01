"""Isolated shadow coupon capability probe — no live routing or issuance."""
from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from modules.ai.brain.intent.coupon_capability_probe import (
    ALLOWED_CAPABILITIES,
    COUPON_CAPABILITY_PROBE_SYSTEM,
    SHADOW_PROBE_ENV,
    maybe_run_coupon_capability_probe_for_turn,
    maybe_run_shadow_coupon_capability_probe,
    parse_coupon_capability_payload,
    run_coupon_capability_probe,
    shadow_coupon_capability_probe_enabled,
)
from modules.ai.brain.intent import slot_extractor
from services.customer_request_coupon_service import (
    CUSTOMER_COUPON_LIVE_ISSUANCE,
    CUSTOMER_COUPON_LIVE_ROUTING,
)

# Evaluation utterances live in this test file only. They are not runtime mappings.
SHADOW_EVALUATION_MATRIX = (
    ("ابي كوبون خصم", "customer_coupon_request"),
    ("هل يوجد قسيمة لطلبتي؟", "customer_coupon_request"),
    ("can I get a discount code for my next order", "customer_coupon_request"),
    ("أعطوني كود تخفيض إذا أستاهل", "customer_coupon_request"),
    ("أبغى عرض شخصي ككوبون", "customer_coupon_request"),
    ("كم سعر الحذاء الرياضي الأبيض؟", "none"),
    ("عندكم قميص قطني أزرق؟", "none"),
    ("أبي أشوف المنتجات", "none"),
    ("أبي أطلب", "none"),
    ("المقاس ٤٢", "none"),
    ("وش رسوم الشحن للرياض؟", "none"),
    ("حولت الحين", "none"),
    ("وين وصل طلبي", "none"),
    ("طلباتي السابقة", "none"),
    ("وين موقعكم", "none"),
    ("حولني لموظف", "none"),
    ("السلام عليكم", "none"),
    ("الخدمة سيئة", "none"),
    ("غالي مرة", "none"),
    ("متردد في الحذاء", "none"),
)


def test_flags_off() -> None:
    assert CUSTOMER_COUPON_LIVE_ROUTING is False
    assert CUSTOMER_COUPON_LIVE_ISSUANCE is False
    assert shadow_coupon_capability_probe_enabled() is False


def test_schema_closed() -> None:
    assert ALLOWED_CAPABILITIES == {"customer_coupon_request", "none"}


def test_parser_fail_closed() -> None:
    assert parse_coupon_capability_payload("") == ("none", False)
    assert parse_coupon_capability_payload("not json") == ("none", False)
    assert parse_coupon_capability_payload({"capability": "coupon"}) == ("none", False)
    assert parse_coupon_capability_payload('{"capability":"unknown"}') == ("none", False)
    assert parse_coupon_capability_payload('{"capability":"customer_coupon_request"}') == (
        "customer_coupon_request",
        True,
    )
    assert parse_coupon_capability_payload('{"capability":"none"}') == ("none", True)
    assert parse_coupon_capability_payload(
        '```json\n{"capability":"customer_coupon_request"}\n```'
    ) == ("customer_coupon_request", True)


def test_isolated_prompt_has_no_phrase_table() -> None:
    prompt = COUPON_CAPABILITY_PROBE_SYSTEM
    assert "كوبون" not in prompt
    assert "خصم" not in prompt
    assert "قسيمة" not in prompt
    lowered = prompt.lower()
    assert "regex" not in lowered
    assert "keyword" not in lowered
    # No customer utterance examples.
    assert "ابي" not in prompt
    assert "أبغى" not in prompt
    assert "customer_coupon_request" in prompt
    assert prompt is not slot_extractor._SYSTEM


def test_slot_extractor_prompt_unchanged_by_probe() -> None:
    assert "customer_coupon_request" not in slot_extractor._SYSTEM


def test_probe_source_does_not_call_issuance() -> None:
    source = Path(
        inspect.getsourcefile(
            __import__(
                "modules.ai.brain.intent.coupon_capability_probe",
                fromlist=["coupon_capability_probe"],
            )
        )
    ).read_text(encoding="utf-8")
    assert "issue_customer_coupon" not in source
    assert "ACTION_CUSTOMER_COUPON" not in source
    assert "pick_coupon_for_level" not in source


def test_flag_off_does_not_call_model(monkeypatch) -> None:
    monkeypatch.delenv(SHADOW_PROBE_ENV, raising=False)
    called = {"n": 0}

    async def _boom(*_a, **_k):
        called["n"] += 1
        raise AssertionError("probe must not run when flag is off")

    monkeypatch.setattr(
        "modules.ai.brain.intent.coupon_capability_probe.run_coupon_capability_probe",
        _boom,
    )
    result = asyncio.run(maybe_run_shadow_coupon_capability_probe("any message"))
    assert called["n"] == 0
    assert result["coupon_capability_probe_run"] is False
    assert result["coupon_capability"] == "none"
    assert result["coupon_capability_shadow_only"] is True


def test_flag_on_fail_closed_on_provider_error(monkeypatch) -> None:
    monkeypatch.setenv(SHADOW_PROBE_ENV, "true")

    class _Boom:
        def call(self, *_a, **_k):
            raise RuntimeError("provider down")

    with patch(
        "modules.ai.orchestrator.providers.openai_compatible_provider.OpenAICompatibleProvider",
        _Boom,
    ):
        result = asyncio.run(run_coupon_capability_probe("hello"))
    assert result["coupon_capability"] == "none"
    assert result["coupon_capability_parse_ok"] is False
    assert result["coupon_capability_shadow_only"] is True
    assert result["coupon_capability_live_routing"] is False


def test_flag_on_parses_model_json(monkeypatch) -> None:
    monkeypatch.setenv(SHADOW_PROBE_ENV, "true")

    class _Ok:
        def call(self, *_a, **_k):
            return {"reply_text": '{"capability":"customer_coupon_request"}'}

    with patch(
        "modules.ai.orchestrator.providers.openai_compatible_provider.OpenAICompatibleProvider",
        _Ok,
    ):
        result = asyncio.run(run_coupon_capability_probe("semantic request"))
    assert result["coupon_capability"] == "customer_coupon_request"
    assert result["coupon_capability_parse_ok"] is True
    assert result["coupon_capability_probe_run"] is True
    assert result["coupon_capability_shadow_only"] is True


def test_evaluation_matrix_is_test_only_not_imported_by_runtime() -> None:
    probe_src = Path(REPO_ROOT / "backend/modules/ai/brain/intent/coupon_capability_probe.py").read_text(
        encoding="utf-8"
    )
    service_src = Path(REPO_ROOT / "backend/services/customer_request_coupon_service.py").read_text(
        encoding="utf-8"
    )
    for utterance, expected in SHADOW_EVALUATION_MATRIX:
        assert utterance not in probe_src
        assert utterance not in service_src
        assert expected in ALLOWED_CAPABILITIES


def test_turn_helper_does_not_run_outside_canary_when_shadow_off(monkeypatch) -> None:
    monkeypatch.delenv(SHADOW_PROBE_ENV, raising=False)
    monkeypatch.delenv("NAHLA_CUSTOMER_COUPON_CANARY_TENANTS", raising=False)
    called = {"n": 0}

    async def _boom(*_a, **_k):
        called["n"] += 1
        raise AssertionError("probe must not run outside canary when shadow is off")

    monkeypatch.setattr(
        "modules.ai.brain.intent.coupon_capability_probe.run_coupon_capability_probe",
        _boom,
    )
    result = asyncio.run(
        maybe_run_coupon_capability_probe_for_turn("any message", tenant_id=9)
    )
    assert called["n"] == 0
    assert result["coupon_capability_probe_run"] is False
    assert result["coupon_capability"] == "none"
    assert result["coupon_capability_canary_eligible"] is False


def test_turn_helper_runs_for_canary_tenant_even_when_shadow_off(monkeypatch) -> None:
    monkeypatch.delenv(SHADOW_PROBE_ENV, raising=False)
    monkeypatch.setenv("NAHLA_CUSTOMER_COUPON_CANARY_TENANTS", "42")
    from services.customer_request_coupon_canary import clear_customer_coupon_canary_cache

    clear_customer_coupon_canary_cache()

    async def _ok(_message, **_k):
        return {
            "coupon_capability_probe_run": True,
            "coupon_capability": "customer_coupon_request",
            "coupon_capability_parse_ok": True,
            "coupon_capability_shadow_only": True,
        }

    monkeypatch.setattr(
        "modules.ai.brain.intent.coupon_capability_probe.run_coupon_capability_probe",
        _ok,
    )
    result = asyncio.run(
        maybe_run_coupon_capability_probe_for_turn("semantic request", tenant_id=42)
    )
    assert result["coupon_capability"] == "customer_coupon_request"
    assert result["coupon_capability_canary_eligible"] is True
    assert result["coupon_capability_shadow_only"] is False
    clear_customer_coupon_canary_cache()
