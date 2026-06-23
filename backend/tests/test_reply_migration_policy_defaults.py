"""Policy defaults after deterministic reply migration."""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.cost.intent_cost_policy import (  # noqa: E402
    is_routine_llm_avoid_enabled,
    should_avoid_llm_for_intent,
)
from modules.ai.routing.layer0_router import layer0_router_enabled  # noqa: E402


def test_routine_llm_avoid_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NAHLA_ROUTINE_LLM_AVOID_ENABLED", raising=False)
    assert is_routine_llm_avoid_enabled() is False
    assert should_avoid_llm_for_intent("greeting") is False


def test_layer0_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LAYER0_ROUTER_ENABLED", raising=False)
    assert layer0_router_enabled() is False
