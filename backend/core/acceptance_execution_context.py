"""Fail-closed execution context for internal conversational E2E.

The context is intentionally narrow: it can allow LLM inference, but it
cannot grant any external provider, integration, automation, campaign, tool,
or financial capability.  With no installed context, callers are unchanged.
"""
from __future__ import annotations

import re
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator, Literal, Optional


INTERNAL_E2E_EGRESS_DENIED = "internal_e2e_egress_denied"
_SAFE_AUDIT_VALUE = re.compile(r"^[a-zA-Z0-9_.:-]{1,96}$")


@dataclass(frozen=True)
class InternalConversationalE2EContext:
    mode: Literal["internal_conversational_e2e"]
    session_id: str
    tenant_id: int
    allow_llm_inference: bool = False

    def __post_init__(self) -> None:
        if self.mode != "internal_conversational_e2e":
            raise ValueError("internal_e2e_mode_invalid")
        try:
            parsed_session_id = uuid.UUID(str(self.session_id or ""))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("internal_e2e_session_id_invalid") from exc
        if str(parsed_session_id) != self.session_id:
            raise ValueError("internal_e2e_session_id_invalid")
        if type(self.tenant_id) is not int or self.tenant_id <= 0:
            raise ValueError("internal_e2e_tenant_id_invalid")
        if type(self.allow_llm_inference) is not bool:
            raise ValueError("internal_e2e_llm_allowance_invalid")


@dataclass(frozen=True)
class EgressDenialAudit:
    denial_id: str
    mode: str
    session_id: str
    tenant_id: int
    requested_tenant_id: int
    egress_kind: str
    operation: str
    reason: str


class InternalE2EEgressDenied(RuntimeError):
    """Typed, non-PII denial raised before external E2E egress."""

    code = INTERNAL_E2E_EGRESS_DENIED

    def __init__(self, audit: EgressDenialAudit):
        self.audit = audit
        self.denial_id = audit.denial_id
        self.egress_kind = audit.egress_kind
        self.operation = audit.operation
        self.tenant_id = audit.tenant_id
        self.requested_tenant_id = audit.requested_tenant_id
        self.session_id = audit.session_id
        super().__init__(
            f"{self.code}: denial_id={audit.denial_id} "
            f"egress_kind={audit.egress_kind} operation={audit.operation} "
            f"reason={audit.reason}"
        )

    def to_audit_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "denial_id": self.denial_id,
            "mode": self.audit.mode,
            "session_id": self.session_id,
            "tenant_id": self.tenant_id,
            "requested_tenant_id": self.requested_tenant_id,
            "egress_kind": self.egress_kind,
            "operation": self.operation,
            "reason": self.audit.reason,
        }


_CURRENT_CONTEXT: ContextVar[Optional[InternalConversationalE2EContext]] = ContextVar(
    "nahla_internal_conversational_e2e_context",
    default=None,
)
_DENIAL_AUDIT: ContextVar[tuple[EgressDenialAudit, ...]] = ContextVar(
    "nahla_internal_conversational_e2e_denials",
    default=(),
)


def current_acceptance_context() -> Optional[InternalConversationalE2EContext]:
    return _CURRENT_CONTEXT.get()


def recorded_egress_denials() -> tuple[EgressDenialAudit, ...]:
    return _DENIAL_AUDIT.get()


def _safe_audit_value(value: str, *, field: str) -> str:
    normalized = str(value or "").strip()
    if not _SAFE_AUDIT_VALUE.fullmatch(normalized):
        return f"{field}_invalid"
    return normalized


def deny_external_egress(
    *,
    egress_kind: str,
    operation: str,
    tenant_id: object,
) -> None:
    """No-op in production; raise an audited denial in internal E2E mode."""
    context = current_acceptance_context()
    if context is None:
        return

    kind = _safe_audit_value(egress_kind, field="egress_kind")
    safe_operation = _safe_audit_value(operation, field="operation")
    if type(tenant_id) is not int or tenant_id <= 0:
        requested_tenant_id = 0
        reason = "requested_tenant_invalid"
    else:
        requested_tenant_id = tenant_id
        reason = (
            "tenant_mismatch"
            if requested_tenant_id != context.tenant_id
            else "external_egress_closed"
        )

    audit = EgressDenialAudit(
        denial_id=str(uuid.uuid4()),
        mode=context.mode,
        session_id=context.session_id,
        tenant_id=context.tenant_id,
        requested_tenant_id=requested_tenant_id,
        egress_kind=kind,
        operation=safe_operation,
        reason=reason,
    )
    _DENIAL_AUDIT.set((*_DENIAL_AUDIT.get(), audit))
    raise InternalE2EEgressDenied(audit)


@contextmanager
def internal_conversational_e2e_context(
    *,
    session_id: str,
    tenant_id: int,
    allow_llm_inference: bool = False,
) -> Iterator[InternalConversationalE2EContext]:
    context = InternalConversationalE2EContext(
        mode="internal_conversational_e2e",
        session_id=session_id,
        tenant_id=tenant_id,
        allow_llm_inference=allow_llm_inference,
    )
    context_token = _CURRENT_CONTEXT.set(context)
    denial_token = _DENIAL_AUDIT.set(())
    try:
        yield context
    finally:
        _DENIAL_AUDIT.reset(denial_token)
        _CURRENT_CONTEXT.reset(context_token)
