"""The verified association between an application conversation and a runtime one.

Two different tables allocate two different identifiers for what a person
experiences as one conversation: the application's ``conversations.id`` and the
commerce runtime's ``commerce_runtime_conversations.id``. They come from
independent sequences, so they are equal only by coincidence — and comparing
them is how a scope check silently refuses every tool call in production while
passing on a freshly seeded test database where both happen to be ``1``.

Nothing here compares those two numbers. The association is *established* by the
conversation reference: the runtime row is admitted under a reference derived
from the application conversation, and a link is only produced after reading the
runtime row back and finding that reference on it. A caller then binds each side
to the identifier that side actually uses.

The reference carries the application conversation id and nothing else — no
phone number, no customer name. It is a durable key in a shared table, so it
holds the one fact the association needs.
"""
from __future__ import annotations

import dataclasses
import re
from typing import Any, Optional

from core.commerce_runtime import contracts as c

REF_VERSION = "v1"
REF_PREFIX = "conv"
MAX_APP_CONVERSATION_ID = 2_147_483_647

_REF_RE = re.compile(rf"^([a-z][a-z0-9_]{{0,15}}):{REF_VERSION}:{REF_PREFIX}:([1-9][0-9]{{0,9}})$")


class ConversationLinkUnverified(c.CommerceRuntimeError):
    """The runtime conversation is not the one this application conversation owns."""

    def __init__(self, reason: str, **detail: Any) -> None:
        self.reason = reason
        self.detail = dict(detail)
        super().__init__(f"{reason}: {detail}")


@dataclasses.dataclass(frozen=True)
class TrustedConversationLink:
    """Proof that one runtime conversation belongs to one application conversation.

    Only :func:`verify_conversation_link` produces this, and only after reading
    the runtime row. Holding one means the two identifiers below were read from
    the same verified association — never inferred from their values.
    """

    tenant_id: int
    namespace: str
    channel: str
    app_conversation_id: int
    runtime_conversation_id: int
    conversation_ref: str


def validate_app_conversation_id(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise c.ValidationError("app conversation id must be an integer")
    if not 1 <= value <= MAX_APP_CONVERSATION_ID:
        raise c.ValidationError("app conversation id is out of range")
    return int(value)


def validate_channel(value: Any) -> str:
    channel = str(value or "").strip()
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,15}", channel):
        raise c.ValidationError("channel must be a short lowercase identifier")
    return channel


def conversation_ref_for(*, channel: str, app_conversation_id: int) -> str:
    """The admission reference for one application conversation on one channel."""
    return f"{validate_channel(channel)}:{REF_VERSION}:{REF_PREFIX}:" \
           f"{validate_app_conversation_id(app_conversation_id)}"


def parse_conversation_ref(ref: Any) -> Optional[tuple]:
    """``(channel, app_conversation_id)``, or ``None`` when this is not our reference.

    A reference this runtime did not mint — an older format, another producer's,
    anything unparsable — yields ``None`` rather than a guess.
    """
    match = _REF_RE.match(str(ref or ""))
    if match is None:
        return None
    return match.group(1), int(match.group(2))


def verify_conversation_link(
    foundation: Any,
    *,
    tenant_id: int,
    namespace: Any,
    channel: str,
    app_conversation_id: int,
    runtime_conversation_id: int,
) -> TrustedConversationLink:
    """Establish the link by reading the runtime row, or refuse.

    Raises :class:`ConversationLinkUnverified` when the runtime conversation does
    not exist in this tenant and namespace, when its reference is not one this
    runtime mints, or when that reference names a different application
    conversation or a different channel.
    """
    tenant = c.validate_tenant_id(tenant_id)
    ns = c.validate_namespace(namespace).value
    channel_name = validate_channel(channel)
    app_id = validate_app_conversation_id(app_conversation_id)
    runtime_id = c.validate_counter(runtime_conversation_id, field="runtime_conversation_id")
    expected = conversation_ref_for(channel=channel_name, app_conversation_id=app_id)

    try:
        snapshot = foundation.get_conversation(
            tenant_id=tenant, namespace=ns, conversation_id=runtime_id)
    except c.CommerceRuntimeError as exc:
        raise ConversationLinkUnverified(
            "runtime_conversation_not_found", tenant_id=tenant, namespace=ns,
            runtime_conversation_id=runtime_id, error=type(exc).__name__) from exc

    observed = str(getattr(snapshot, "conversation_ref", "") or "")
    parsed = parse_conversation_ref(observed)
    if parsed is None:
        raise ConversationLinkUnverified(
            "conversation_ref_unrecognised", runtime_conversation_id=runtime_id, observed=observed)
    observed_channel, observed_app_id = parsed
    if observed_channel != channel_name:
        raise ConversationLinkUnverified(
            "conversation_channel_mismatch", runtime_conversation_id=runtime_id,
            expected=channel_name, observed=observed_channel)
    if observed_app_id != app_id or observed != expected:
        raise ConversationLinkUnverified(
            "conversation_ref_mismatch", runtime_conversation_id=runtime_id,
            expected=expected, observed=observed)

    return TrustedConversationLink(
        tenant_id=tenant, namespace=ns, channel=channel_name, app_conversation_id=app_id,
        runtime_conversation_id=int(snapshot.conversation_id), conversation_ref=observed,
    )


__all__ = [
    "ConversationLinkUnverified", "MAX_APP_CONVERSATION_ID", "REF_PREFIX", "REF_VERSION",
    "TrustedConversationLink", "conversation_ref_for", "parse_conversation_ref",
    "validate_app_conversation_id", "validate_channel", "verify_conversation_link",
]
