"""Deterministic scripted reasoning provider (test double; no model call).

A script is an ordered sequence of steps. Each step is either a ready
``ProviderResult`` or a callable that receives the ``ProviderRequest`` (so a
test can make the decision depend on the observations the loop supplied) and
returns a ``ProviderResult``. Every request is recorded for assertions. When
the script is exhausted the provider reports an explicit failure, never a
fabricated reply. This is the only provider in the slice; it proves loop
orchestration, not model quality.
"""
from __future__ import annotations

from typing import Any, Callable, List, Mapping, Optional, Sequence, Tuple, Union

from core.commerce_runtime import agent_contracts as ac

ScriptStep = Union[ac.ProviderResult, Callable[[ac.ProviderRequest], ac.ProviderResult]]


class ScriptedReasoningProvider:
    def __init__(self, steps: Sequence[ScriptStep], *, capabilities: Optional[ac.ProviderCapabilities] = None,
                 before_step: Optional[Callable[[ac.ProviderRequest], None]] = None) -> None:
        self._steps: List[ScriptStep] = list(steps)
        self._capabilities = capabilities or ac.ProviderCapabilities(provider_name="scripted", parallel_tool_use=True,
                                                                     max_tool_requests_per_step=4)
        self._before_step = before_step
        self.requests: List[ac.ProviderRequest] = []

    @property
    def capabilities(self) -> ac.ProviderCapabilities:
        return self._capabilities

    def step(self, request: ac.ProviderRequest) -> ac.ProviderResult:
        self.requests.append(request)
        if self._before_step is not None:
            self._before_step(request)          # test hook: e.g. lose ownership while "the model is thinking"
        if not self._steps:
            return ac.ProviderFailure("script_exhausted")
        step = self._steps.pop(0)
        return step(request) if callable(step) else step


def tool_call(call_id: str, tool_name: str, **arguments: Any) -> ac.ToolRequest:
    return ac.ToolRequest(call_id=call_id, tool_name=tool_name, arguments=dict(arguments))


def tools(*requests: ac.ToolRequest) -> ac.ProviderToolRequests:
    return ac.ProviderToolRequests(requests=tuple(requests))


def reply(text: str, *, refs: Sequence[str] = (), kind: str = "text", commerce: bool = False,
          payload: Optional[Mapping[str, Any]] = None) -> ac.ProviderReply:
    return ac.ProviderReply(ac.ReplyDraft(text=text, kind=kind, evidence_refs=tuple(refs),
                                          claims_commerce_facts=commerce, payload=dict(payload or {})))


def observed(request: ac.ProviderRequest, call_id: str) -> Optional[ac.ToolObservation]:
    return next((o for o in request.observations if o.call_id == call_id), None)


__all__: Tuple[str, ...] = ("ScriptStep", "ScriptedReasoningProvider", "observed", "reply", "tool_call", "tools")
