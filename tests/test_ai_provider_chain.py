from modules.ai.orchestrator.engine import AIOrchestratorEngine
from modules.ai.orchestrator.provider_router import ProviderChainConfig


class _FakeProvider:
    def __init__(self, name, configured=True, reply_text="", status="ok"):
        self.provider_name = name
        self._configured = configured
        self._reply_text = reply_text
        self._status = status

    def is_configured(self):
        return self._configured

    def call(self, message, prompt, *, history=None):
        return {
            "provider": self.provider_name,
            "model": f"{self.provider_name}-model",
            "reply_text": self._reply_text,
            "status": self._status,
            "history_len": len(history or []),
        }


def test_call_with_chain_falls_through_until_success(monkeypatch):
    engine = AIOrchestratorEngine()

    providers = {
        "anthropic": _FakeProvider("anthropic", configured=True, reply_text=""),
        "openai_compatible": _FakeProvider("openai_compatible", configured=True, reply_text="hello"),
        "gemini": _FakeProvider("gemini", configured=True, reply_text="unused"),
    }

    monkeypatch.setattr(
        "modules.ai.orchestrator.engine.get_provider",
        lambda name: providers.get(name),
    )
    monkeypatch.setattr(
        "modules.ai.orchestrator.engine.call_with_resilience",
        lambda provider_name, call_fn, timeout: call_fn(),
    )
    engine._provider = providers["anthropic"]

    chain = ProviderChainConfig(
        providers=["anthropic", "openai_compatible", "gemini"],
        hint=None,
        allow_mock=False,
    )
    raw = engine._call_with_chain("hello", "system prompt", chain)

    assert raw["provider"] == "openai_compatible"
    assert raw["reply_text"] == "hello"


def test_call_with_chain_passes_history_to_provider(monkeypatch):
    engine = AIOrchestratorEngine()
    providers = {
        "anthropic": _FakeProvider("anthropic", configured=True, reply_text="hello"),
    }
    monkeypatch.setattr(
        "modules.ai.orchestrator.engine.get_provider",
        lambda name: providers.get(name),
    )
    monkeypatch.setattr(
        "modules.ai.orchestrator.engine.call_with_resilience",
        lambda provider_name, call_fn, timeout: call_fn(),
    )
    engine._provider = providers["anthropic"]

    from modules.ai.orchestrator.types import AIContext, AIMessage, AIOrchestrationRequest

    request = AIOrchestrationRequest(
        context=AIContext(tenant_id=1, customer_phone="+9665", channel="whatsapp"),
        message="new",
        history=[
            AIMessage(role="user", content="old user"),
            AIMessage(role="assistant", content="old bot"),
        ],
    )
    raw = engine.call_provider(request, "prompt", provider_chain=None)
    assert raw["history_len"] == 2


def test_call_with_chain_uses_default_provider_when_all_chain_calls_fail(monkeypatch):
    engine = AIOrchestratorEngine()

    fallback_provider = _FakeProvider("anthropic", configured=True, reply_text="fallback")
    providers = {
        "anthropic": _FakeProvider("anthropic", configured=True, reply_text=""),
        "openai_compatible": _FakeProvider("openai_compatible", configured=True, reply_text=""),
        "gemini": _FakeProvider("gemini", configured=False, reply_text=""),
    }

    monkeypatch.setattr(
        "modules.ai.orchestrator.engine.get_provider",
        lambda name: providers.get(name),
    )
    monkeypatch.setattr(
        "modules.ai.orchestrator.engine.call_with_resilience",
        lambda provider_name, call_fn, timeout: call_fn() if provider_name != "openai_compatible" else None,
    )
    engine._provider = fallback_provider

    chain = ProviderChainConfig(
        providers=["anthropic", "openai_compatible", "gemini"],
        hint=None,
        allow_mock=False,
    )
    raw = engine._call_with_chain("hello", "system prompt", chain)

    assert raw["provider"] == "anthropic"
    assert raw["reply_text"] == "fallback"
