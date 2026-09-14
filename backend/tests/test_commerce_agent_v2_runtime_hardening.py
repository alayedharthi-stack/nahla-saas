"""Phase 2.6 contracts for the OpenAI runtime boundary."""
from __future__ import annotations

import pytest
from agents.retry import ModelRetryNormalizedError, RetryPolicyContext

from core import config
from modules.ai.commerce_agent_v2.agent import build_commerce_agent
from modules.ai.commerce_agent_v2.runtime import (
    build_model_retry_settings,
    classify_model_exception,
)
from modules.ai.commerce_agent_v2.tools import COMMERCE_AGENT_TOOLS


def _retry_context(
    *, status_code: int | None = None, network: bool = False, timeout: bool = False
) -> RetryPolicyContext:
    return RetryPolicyContext(
        error=RuntimeError("redacted-test-error"),
        attempt=1,
        max_retries=1,
        stream=False,
        normalized=ModelRetryNormalizedError(
            status_code=status_code,
            is_network_error=network,
            is_timeout=timeout,
        ),
    )


def test_runtime_defaults_and_agent_contract_are_hardened() -> None:
    retry = build_model_retry_settings(execution_mode="outbound", max_retries=99)
    agent = build_commerce_agent(
        model="scripted-model",
        model_timeout_seconds=75,
        retry_settings=retry,
        service_tier="fast",
    )

    assert config.COMMERCE_AGENT_V2_MODEL_TIMEOUT_SECONDS == 75
    assert config.COMMERCE_AGENT_V2_RUN_DEADLINE_SECONDS == 180
    assert retry.max_retries == 1
    assert agent.model_settings.timeout == 75
    assert agent.model_settings.reasoning.effort == "high"
    assert agent.model_settings.extra_body == {"service_tier": "fast"}
    assert len(agent.tools) == 7
    assert {tool.name for tool in agent.tools} == {tool.name for tool in COMMERCE_AGENT_TOOLS}
    assert all(tool.timeout_seconds == 8 for tool in agent.tools)
    assert all(tool.timeout_behavior == "error_as_result" for tool in agent.tools)


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [408, 429, 500, 502, 503, 504])
async def test_outbound_retries_only_approved_http_statuses(status_code: int) -> None:
    policy = build_model_retry_settings(execution_mode="outbound", max_retries=1).policy
    decision = await policy(_retry_context(status_code=status_code))
    assert decision.retry is True


@pytest.mark.asyncio
async def test_outbound_network_failure_retries_but_model_timeout_does_not() -> None:
    observed: list[dict[str, object]] = []
    policy = build_model_retry_settings(
        execution_mode="outbound",
        max_retries=1,
        observer=observed.append,
    ).policy

    network = await policy(_retry_context(network=True))
    timeout = await policy(_retry_context(network=True, timeout=True))

    assert network.retry is True
    assert timeout.retry is False
    assert timeout.reason == "model_timeout_no_retry"
    assert observed[-1]["retry"] is False
    assert "redacted-test-error" not in str(observed)


@pytest.mark.asyncio
async def test_eval_timeout_retry_requires_explicit_opt_in() -> None:
    disabled = build_model_retry_settings(
        execution_mode="eval", max_retries=1, retry_model_timeouts=False
    ).policy
    enabled = build_model_retry_settings(
        execution_mode="eval", max_retries=1, retry_model_timeouts=True
    ).policy

    assert (await disabled(_retry_context(timeout=True))).retry is False
    assert (await enabled(_retry_context(timeout=True))).retry is True


def test_retry_exhaustion_taxonomy_does_not_persist_provider_error_text() -> None:
    class ProviderError(RuntimeError):
        status_code = 429

    error = ProviderError("customer PII and provider body")
    assert classify_model_exception(error) == "model_http_429"
    assert classify_model_exception(
        error,
        attempted_retry=True,
        retry_reason="provider_suggested",
    ) == "provider_suggested_retry_exhausted"
    assert classify_model_exception(
        error,
        attempted_retry=True,
        retry_reason="http_status_429",
    ) == "model_retry_exhausted:http_429"
