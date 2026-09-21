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
from typing import Any, Callable, Dict, Iterator, Literal, Mapping, Optional, Tuple


INTERNAL_E2E_EGRESS_DENIED = "internal_e2e_egress_denied"
INTERNAL_E2E_CAPTURE_UNAVAILABLE = "internal_e2e_outbound_capture_unavailable"
_SAFE_AUDIT_VALUE = re.compile(r"^[a-zA-Z0-9_.:-]{1,96}$")
_CAPTURED_DELIVERY_ID = re.compile(r"^captured\.[0-9a-f]{32}$")


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


# ── Outbound transport capture ────────────────────────────────────────────
#
# An internal-E2E run must exercise the REAL send boundary — dedup, the wire
# sanitizer, the receipt derived from the payload — without a byte leaving
# the process. ``deny_external_egress`` already makes dispatch impossible,
# but a denial aborts the send and drives the caller's FAILURE path, which
# is a different thing from delivery and measures the wrong branch.
#
# So the capture below stands in for the transport, and it is fail-closed in
# the strict sense: while an acceptance context is installed, a send either
# reaches a valid capture sink or it RAISES. There is no path from here to
# ``provider_send_message``. An absent sink, a sink that is not callable, a
# sink belonging to another tenant, a sink that raises, and a sink returning
# an id this module did not mint are all the same answer — stop the turn.


@dataclass(frozen=True)
class CapturedOutbound:
    """One outbound payload that was captured instead of dispatched."""

    capture_id: str
    delivery_id: str
    mode: str
    session_id: str
    tenant_id: int
    requested_tenant_id: int
    egress_kind: str
    operation: str
    phone_id: str
    payload: Mapping[str, Any]

    def to_audit_dict(self) -> Dict[str, object]:
        return {
            "capture_id": self.capture_id,
            "delivery_id": self.delivery_id,
            "mode": self.mode,
            "session_id": self.session_id,
            "tenant_id": self.tenant_id,
            "requested_tenant_id": self.requested_tenant_id,
            "egress_kind": self.egress_kind,
            "operation": self.operation,
            "transport": "captured",
        }


class InternalE2EOutboundCaptureUnavailable(RuntimeError):
    """Raised instead of dispatching when capture cannot be honoured."""

    code = INTERNAL_E2E_CAPTURE_UNAVAILABLE

    def __init__(self, reason: str, *, egress_kind: str, operation: str):
        self.reason = reason
        self.egress_kind = egress_kind
        self.operation = operation
        super().__init__(
            f"{self.code}: reason={reason} "
            f"egress_kind={egress_kind} operation={operation}"
        )

    def to_audit_dict(self) -> Dict[str, object]:
        return {
            "code": self.code,
            "reason": self.reason,
            "egress_kind": self.egress_kind,
            "operation": self.operation,
            "transport": "not_captured",
        }


_CAPTURE_SINK: ContextVar[Optional[Callable[[CapturedOutbound], None]]] = ContextVar(
    "nahla_internal_conversational_e2e_capture_sink",
    default=None,
)
_CAPTURED: ContextVar[Tuple[CapturedOutbound, ...]] = ContextVar(
    "nahla_internal_conversational_e2e_captured",
    default=(),
)


def captured_outbound_payloads() -> Tuple[CapturedOutbound, ...]:
    return _CAPTURED.get()


@contextmanager
def outbound_capture_sink(
    sink: Callable[[CapturedOutbound], None],
) -> Iterator[None]:
    """Install a capture sink for the duration of one turn.

    The sink and the recorded captures are both context-scoped, so two
    turns — and two concurrent tasks — never observe each other's
    payloads, and neither survives the block that installed it.
    """
    sink_token = _CAPTURE_SINK.set(sink)
    captured_token = _CAPTURED.set(())
    try:
        yield
    finally:
        _CAPTURED.reset(captured_token)
        _CAPTURE_SINK.reset(sink_token)


def capture_outbound_payload(
    *,
    egress_kind: str,
    operation: str,
    tenant_id: object,
    phone_id: str,
    payload: Mapping[str, Any],
) -> str:
    """Capture this payload locally, or refuse the send.

    Returns ``""`` when no acceptance context is installed — production is
    unchanged and the caller dispatches as usual. Under an acceptance
    context it returns a synthetic delivery id, which proves LOCAL
    processing only and never that a provider accepted anything.
    """
    context = current_acceptance_context()
    if context is None:
        return ""

    kind = _safe_audit_value(egress_kind, field="egress_kind")
    safe_operation = _safe_audit_value(operation, field="operation")

    sink = _CAPTURE_SINK.get()
    if sink is None or not callable(sink):
        raise InternalE2EOutboundCaptureUnavailable(
            "capture_sink_absent", egress_kind=kind, operation=safe_operation,
        )
    if type(tenant_id) is not int or tenant_id <= 0:
        raise InternalE2EOutboundCaptureUnavailable(
            "requested_tenant_invalid", egress_kind=kind, operation=safe_operation,
        )
    if int(tenant_id) != context.tenant_id:
        raise InternalE2EOutboundCaptureUnavailable(
            "tenant_mismatch", egress_kind=kind, operation=safe_operation,
        )
    if not isinstance(payload, Mapping):
        raise InternalE2EOutboundCaptureUnavailable(
            "payload_invalid", egress_kind=kind, operation=safe_operation,
        )

    record = CapturedOutbound(
        capture_id=str(uuid.uuid4()),
        delivery_id=f"captured.{uuid.uuid4().hex}",
        mode=context.mode,
        session_id=context.session_id,
        tenant_id=context.tenant_id,
        requested_tenant_id=int(tenant_id),
        egress_kind=kind,
        operation=safe_operation,
        phone_id=str(phone_id or ""),
        payload=dict(payload),
    )
    if not _CAPTURED_DELIVERY_ID.fullmatch(record.delivery_id):
        raise InternalE2EOutboundCaptureUnavailable(
            "capture_delivery_id_invalid", egress_kind=kind, operation=safe_operation,
        )
    try:
        sink(record)
    except Exception as exc:  # noqa: BLE001
        raise InternalE2EOutboundCaptureUnavailable(
            "capture_sink_failed", egress_kind=kind, operation=safe_operation,
        ) from exc
    _CAPTURED.set((*_CAPTURED.get(), record))
    return record.delivery_id
