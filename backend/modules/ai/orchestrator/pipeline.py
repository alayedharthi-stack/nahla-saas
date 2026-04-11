"""
backend/modules/ai/orchestrator/pipeline.py
────────────────────────────────────────────
Canonical orchestration pipeline for Nahla AI.

Responsibilities:
  - normalize inbound request context
  - build prompt input (via engine → prompt builder)
  - load/enforce commerce permissions
  - apply policy validation using the static permission map
  - resolve provider chain metadata
  - delegate final generation to the orchestration engine
  - produce one normalized final payload with full observability metadata

Internal wiring:
  - uses modules.ai.commerce.permissions.CommercePermissionSet
    for permission snapshot and action gate
  - engine delegates internally to modules.ai.prompts.builder
  - provider chain resolved via modules.ai.orchestrator.provider_router
  - engine consumes provider_chain to execute provider fallback order
"""
from __future__ import annotations

import logging

from modules.ai.commerce.permissions import CommercePermissionSet
from modules.ai.orchestrator.engine import AIOrchestratorEngine
from modules.ai.orchestrator.provider_router import ProviderChainConfig, get_provider_chain
from modules.ai.orchestrator.types import AIOrchestrationRequest, AIReplyPayload

logger = logging.getLogger("nahla.ai.orchestrator.pipeline")


class AIOrchestrationPipeline:
    """
    Shared pipeline intended to become the single AI entrypoint used by:
      - WhatsApp
      - Campaigns
      - Conversations
      - Widgets

    Internal wiring:
      - permission checks: modules.ai.commerce.permissions.CommercePermissionSet
      - prompt building:   modules.ai.prompts.builder (via engine)
      - reply generation:  AIOrchestratorEngine.generate_reply (scaffold)
    """

    def __init__(self, engine: AIOrchestratorEngine | None = None) -> None:
        self.engine = engine or AIOrchestratorEngine()

    # ── Provider chain layer ───────────────────────────────────────────────────

    def resolve_provider_chain(self, request: AIOrchestrationRequest) -> ProviderChainConfig:
        """
        Build the canonical provider chain config for this request.

        Current behavior:
        - provider_hint is forwarded and recorded in the chain metadata
        - the engine consumes the returned provider_chain during execution
        - provider_hint does NOT yet reorder the chain dynamically
          (recorded for observability and future activation only)

        No DB calls.  No network calls.  Safe for scaffolding.
        """
        chain = get_provider_chain(hint=request.provider_hint)
        logger.debug(
            "[pipeline] provider chain resolved | hint=%s chain=%s",
            request.provider_hint,
            chain.providers,
        )
        return chain

    # ── Permission layer ──────────────────────────────────────────────────────

    def build_permission_snapshot(self, request: AIOrchestrationRequest) -> CommercePermissionSet:
        """
        Build a static permission snapshot for this request's tenant.

        Current behavior: uses default permission flags from CommercePermissionSet.
        Future behavior: will load persisted tenant flags from the DB.

        No DB calls here — safe for scaffolding.
        """
        return CommercePermissionSet(tenant_id=request.context.tenant_id or 0)

    def apply_policy_validation(
        self,
        request: AIOrchestrationRequest,
        permissions: CommercePermissionSet,
    ) -> tuple[list[str], list[str], list[str]]:
        """
        Gate each requested tool/action through the commerce permission map.

        Returns:
          allowed_actions  — actions the tenant is permitted to take
          blocked_actions  — actions rejected by the permission map
          policy_notes     — human-readable denial reasons for blocked actions

        Uses only the static action→permission mapping in
        modules.ai.commerce.permissions — no DB, no external calls.
        """
        allowed: list[str] = []
        blocked: list[str] = []
        notes:   list[str] = []

        for action in request.tools_requested:
            if permissions.is_permitted(action):
                allowed.append(action)
            else:
                blocked.append(action)
                reason = permissions.denial_reason(action)
                if reason:
                    notes.append(reason)

        return allowed, blocked, notes

    # ── Pipeline entrypoint ───────────────────────────────────────────────────

    def run(self, request: AIOrchestrationRequest) -> AIReplyPayload:
        """
        Canonical pipeline entrypoint.

        Execution order:
          1. build_permission_snapshot  — commerce gate (static, no DB)
          2. apply_policy_validation    — action allow/block list
          3. engine.generate_reply      — prompt build + provider-chain execution
          4. enrich payload metadata    — observability surface

        No behavioral changes to any existing path.
        """
        # Step 1 — commerce permissions (static, no DB)
        permissions = self.build_permission_snapshot(request)
        allowed, blocked, notes = self.apply_policy_validation(request, permissions)

        # Step 2 — provider chain (resolved here, executed by the engine)
        provider_chain = self.resolve_provider_chain(request)

        # Step 3 — generate reply via engine using the resolved provider chain
        payload = self.engine.generate_reply(request, provider_chain=provider_chain)
        payload.allowed_actions = allowed
        payload.blocked_actions = blocked
        payload.policy_notes    = notes
        payload.metadata.update(
            {
                "pipeline":              "backend.modules.ai.orchestrator.pipeline",
                "status":                "active",
                "permissions_module":    "modules.ai.commerce.permissions",
                "prompt_module":         "modules.ai.prompts.builder",
                "permission_snapshot":   permissions.to_dict(),
                "tenant_id":             request.context.tenant_id,
                "channel":               request.context.channel,
                # Provider chain metadata — active execution metadata
                "provider_chain":        provider_chain.providers,
                "provider_hint":         request.provider_hint,
                "provider_routing":      "active",
            }
        )
        return payload

