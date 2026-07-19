"""Regression tests for slot model resolution and safe Anthropic error diagnostics."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

_DEPRECATED_SLOT_MODEL = "claude-3-5-haiku-20241022"
_SECRET_MARKER = "sk-ant-secret-key-do-not-log"
_CUSTOMER_TEXT = "اسمي أحمد سالم من الرياض"
_API_URL = "https://api.anthropic.com/v1/messages"


def test_resolve_slot_model_uses_canonical_default(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_SLOT_MODEL", raising=False)
    monkeypatch.setenv("CLAUDE_MODEL", "claude-haiku-4-5")

    from modules.ai.brain.intent.slot_extractor import _resolve_slot_model

    assert _resolve_slot_model() == "claude-haiku-4-5"


def test_resolve_slot_model_respects_env_override(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_SLOT_MODEL", "claude-sonnet-4-6")
    monkeypatch.setenv("CLAUDE_MODEL", "claude-haiku-4-5")

    from modules.ai.brain.intent.slot_extractor import _resolve_slot_model

    assert _resolve_slot_model() == "claude-sonnet-4-6"


def test_slot_extractor_runtime_source_has_no_deprecated_model_literal():
    source_path = (
        Path(__file__).resolve().parents[1]
        / "modules"
        / "ai"
        / "brain"
        / "intent"
        / "slot_extractor.py"
    )
    source = source_path.read_text(encoding="utf-8")
    assert _DEPRECATED_SLOT_MODEL not in source


def test_anthropic_exception_diagnostics_exposes_cause_types():
    from modules.ai.orchestrator.anthropic_exception_diagnostics import (
        anthropic_exception_diagnostics,
    )

    class APIConnectionError(Exception):
        pass

    inner = OSError(110, f"timed out reaching {_API_URL} with {_SECRET_MARKER}")
    outer = APIConnectionError(f"connection failed for {_CUSTOMER_TEXT}")
    outer.__cause__ = inner

    diag = anthropic_exception_diagnostics(outer)
    assert diag["exc_type"] == "APIConnectionError"
    assert diag["cause_type"] == "OSError"
    assert diag["category"] == "errno_110"


def test_slot_extractor_logs_safe_diagnostics_on_api_connection_error(
    monkeypatch, caplog,
):
    from modules.ai.brain.intent import slot_extractor

    class APIConnectionError(Exception):
        pass

    inner = OSError(111, f"refused {_API_URL} key={_SECRET_MARKER}")
    conn_err = APIConnectionError(f"customer said: {_CUSTOMER_TEXT}")
    conn_err.__cause__ = inner

    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(side_effect=conn_err)

    mock_anthropic = MagicMock()
    mock_anthropic.AsyncAnthropic.return_value = mock_client

    monkeypatch.setenv("ANTHROPIC_API_KEY", _SECRET_MARKER)
    monkeypatch.delenv("ANTHROPIC_SLOT_MODEL", raising=False)
    monkeypatch.setenv("CLAUDE_MODEL", "claude-haiku-4-5")

    caplog.set_level(logging.WARNING, logger="nahla.brain.slot_extractor")

    async def _run():
        with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
            return await slot_extractor.extract_slots(_CUSTOMER_TEXT, [])

    result = asyncio.run(_run())

    assert result == slot_extractor._extract_deterministic_slots(_CUSTOMER_TEXT)
    warning_lines = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert warning_lines
    joined = "\n".join(warning_lines)
    assert "APIConnectionError" in joined
    assert "OSError" in joined
    assert "errno_111" in joined
    assert _SECRET_MARKER not in joined
    assert _CUSTOMER_TEXT not in joined
    assert _API_URL not in joined


def test_anthropic_provider_logs_safe_diagnostics_on_api_connection_error(caplog):
    from modules.ai.orchestrator.providers import anthropic_provider

    class APIConnectionError(Exception):
        pass

    inner = OSError(110, f"timed out {_API_URL} bearer {_SECRET_MARKER}")
    conn_err = APIConnectionError(f"prompt leak: {_CUSTOMER_TEXT}")
    conn_err.__cause__ = inner

    mock_sdk = MagicMock()
    mock_sdk.Anthropic.return_value.messages.create.side_effect = conn_err
    mock_sdk.AuthenticationError = type("AuthenticationError", (Exception,), {})
    mock_sdk.APIConnectionError = APIConnectionError

    caplog.set_level(logging.WARNING, logger="nahla.ai.orchestrator.engine")

    with patch.dict("os.environ", {"ANTHROPIC_API_KEY": _SECRET_MARKER}, clear=False):
        with patch.object(anthropic_provider, "_SDK_AVAILABLE", True):
            with patch.object(anthropic_provider, "_anthropic_sdk", mock_sdk):
                result = anthropic_provider.AnthropicProvider().call(
                    _CUSTOMER_TEXT,
                    f"system prompt with {_SECRET_MARKER}",
                )

    assert result["status"] == "connection_error"
    assert result["reply_text"] == ""
    warning_lines = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    joined = "\n".join(warning_lines)
    assert "diagnostics=" in joined
    assert "APIConnectionError" in joined
    assert "OSError" in joined
    assert "errno_110" in joined
    assert _SECRET_MARKER not in joined
    assert _CUSTOMER_TEXT not in joined
    assert _API_URL not in joined


def test_slot_extractor_uses_resolved_model_for_cost_audit(monkeypatch):
    from modules.ai.brain.intent import slot_extractor

    mock_client = MagicMock()
    mock_response = SimpleNamespace(
        content=[SimpleNamespace(text='{"intent_hint":"general"}')],
        usage=SimpleNamespace(
            input_tokens=1,
            output_tokens=1,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )
    mock_client.messages.create = AsyncMock(return_value=mock_response)

    mock_anthropic = MagicMock()
    mock_anthropic.AsyncAnthropic.return_value = mock_client

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("ANTHROPIC_SLOT_MODEL", "claude-sonnet-4-6")

    audited_models: list[str] = []

    def _capture_audit(**fields):
        audited_models.append(str(fields.get("model")))

    async def _run():
        with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
            with patch(
                "modules.ai.orchestrator.llm_cost_audit.emit_llm_cost_audit",
                side_effect=_capture_audit,
            ):
                await slot_extractor.extract_slots("مرحبا", [])

    asyncio.run(_run())

    assert audited_models == ["claude-sonnet-4-6"]
    mock_client.messages.create.assert_awaited_once()
    assert mock_client.messages.create.await_args.kwargs["model"] == "claude-sonnet-4-6"
