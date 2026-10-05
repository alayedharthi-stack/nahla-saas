"""Durable, provider-neutral webhook delivery ledger for Nahlah AI payments.

Order of operations for any future receiver:

1. ``record_delivery`` — store the delivery *before* anything else, dedup by
   digest, authenticate with a caller-supplied authenticator, redact the body.
2. ``admit_delivery`` — bind an authenticated delivery to exactly one tenant
   and event reference. Admission is refused for unauthenticated deliveries.
3. ``complete_delivery`` — record the terminal outcome with a reason.

This module stores no secret and performs no money movement, no payment or
settlement write and no provider call. Interpreting the body (payment paid,
registration approved, settlement transferred) is the job of a later, contract
aware step; the ledger only makes every delivery durable, deduplicated,
attributable and auditable. The exact Moyasar Platform API delivery format is
unknown until the agreement is signed, so authentication is pluggable.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, FrozenSet, Mapping, Optional, Protocol, Sequence

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from .models import MerchantPaymentProviderEvent, MerchantPaymentWebhookDelivery

EVENT_CATEGORIES: FrozenSet[str] = frozenset({"unknown", "onboarding", "payment", "settlement", "payout"})
TERMINAL_STATES: FrozenSet[str] = frozenset({"processed", "rejected", "failed", "ignored"})
MAX_STORED_PAYLOAD_BYTES = 64 * 1024

# Redaction matches *normalised* keys (lower-case, non-alphanumerics removed)
# at any depth. A key is redacted when it contains one of these stems
# (card data, provider secrets/tokens/keys, bank and identity identifiers) or
# equals one of the exact personal-data keys. Over-redaction is accepted: the
# ledger needs identifiers and amounts for replay, never the values below.
REDACTED_KEY_STEMS: FrozenSet[str] = frozenset({
    "number", "pan", "cvc", "cvv", "expiry", "expmonth", "expyear", "token", "secret", "apikey",
    "secretkey", "authorization", "password", "passcode", "iban", "account", "nationalid",
    "idnumber", "crnumber", "document", "email", "phone", "mobile",
})
REDACTED_EXACT_KEYS: FrozenSet[str] = frozenset({
    "key", "name", "firstname", "lastname", "fullname", "cardholder", "holdername", "address",
})
REDACTED = "[redacted]"


class WebhookLedgerError(ValueError):
    """The ledger refuses the requested change."""


@dataclass(frozen=True)
class AuthenticationResult:
    verified: bool
    method: str


class DeliveryAuthenticator(Protocol):
    def authenticate(self, *, raw_payload: bytes, headers: Mapping[str, str]) -> AuthenticationResult:
        ...


@dataclass(frozen=True)
class SharedSecretAuthenticator:
    """Constant-time comparison of a secret the caller extracts from the delivery.

    ``extract`` receives the raw body and lower-cased headers and returns the
    supplied secret (for example from a documented body field or header). The
    expected secret is held in memory only; it is never written by the ledger.
    """

    expected_secret: str = field(repr=False)
    extract: Callable[[bytes, Mapping[str, str]], Optional[str]] = field(repr=False)
    method: str = "shared_secret"

    def authenticate(self, *, raw_payload: bytes, headers: Mapping[str, str]) -> AuthenticationResult:
        supplied = self.extract(raw_payload, headers)
        if not supplied or not self.expected_secret:
            return AuthenticationResult(False, self.method)
        ok = hmac.compare_digest(supplied.encode("utf-8"), self.expected_secret.encode("utf-8"))
        return AuthenticationResult(ok, self.method)


@dataclass(frozen=True)
class HmacSha256Authenticator:
    """Hex HMAC-SHA256 of the raw body carried in a header."""

    secret: str = field(repr=False)
    header_name: str
    method: str = "hmac_sha256"

    def authenticate(self, *, raw_payload: bytes, headers: Mapping[str, str]) -> AuthenticationResult:
        supplied = headers.get(self.header_name.lower(), "")
        if not supplied or not self.secret:
            return AuthenticationResult(False, self.method)
        computed = hmac.new(self.secret.encode("utf-8"), raw_payload, hashlib.sha256).hexdigest()
        return AuthenticationResult(hmac.compare_digest(computed, supplied.strip().lower()), self.method)


@dataclass(frozen=True)
class RecordedDelivery:
    delivery_id: int
    replay: bool
    attempts: int
    authentication_state: str


def _sensitive_key(key: str) -> bool:
    normalised = "".join(ch for ch in key.lower() if ch.isalnum())
    if normalised in REDACTED_EXACT_KEYS:
        return True
    return any(stem in normalised for stem in REDACTED_KEY_STEMS)


def redact_payload(raw_payload: bytes) -> str:
    """Return a JSON string with sensitive keys replaced; non-JSON bodies are not stored."""
    try:
        parsed = json.loads(raw_payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return json.dumps({"_nahlah_note": "non-JSON body not stored", "_bytes": len(raw_payload)})

    def scrub(node):
        if isinstance(node, dict):
            return {
                key: (REDACTED if _sensitive_key(str(key)) else scrub(value))
                for key, value in node.items()
            }
        if isinstance(node, list):
            return [scrub(item) for item in node]
        return node

    return json.dumps(scrub(parsed), ensure_ascii=False, sort_keys=True)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _delivery_key(provider: str, environment: str, payload_sha256: str) -> str:
    return _digest(f"{provider}|{environment}|{payload_sha256}".encode("utf-8"))


def record_delivery(
    engine: Engine,
    *,
    provider: str,
    environment: str,
    raw_payload: bytes,
    headers: Mapping[str, str],
    authenticator: Optional[DeliveryAuthenticator],
) -> RecordedDelivery:
    """Persist one inbound delivery; identical redeliveries collapse onto one row.

    With no authenticator the delivery is stored as ``unverified`` and can never
    be admitted. A later redelivery that authenticates upgrades the stored
    state; a failed re-authentication never downgrades a verified row.
    """
    if not provider or environment not in ("test", "live"):
        raise WebhookLedgerError("Invalid delivery scope")
    if not raw_payload:
        raise WebhookLedgerError("Empty delivery body")
    lowered = {str(k).lower(): str(v) for k, v in headers.items()}
    auth = (
        authenticator.authenticate(raw_payload=raw_payload, headers=lowered)
        if authenticator is not None
        else AuthenticationResult(False, "none")
    )
    auth_state = "verified" if auth.verified else ("unverified" if authenticator is None else "failed")

    deliveries = MerchantPaymentWebhookDelivery.__table__
    payload_sha256 = _digest(raw_payload)
    key = _delivery_key(provider, environment, payload_sha256)
    now = datetime.now(timezone.utc)

    def replay(connection, existing) -> RecordedDelivery:
        values = {"attempts": existing["attempts"] + 1, "last_received_at": now}
        if auth.verified and existing["authentication_state"] != "verified":
            values.update(authentication_state="verified", authentication_method=auth.method)
        connection.execute(
            sa.update(deliveries).where(deliveries.c.id == existing["id"]).values(**values)
        )
        return RecordedDelivery(
            existing["id"], True, values["attempts"],
            values.get("authentication_state", existing["authentication_state"]),
        )

    with engine.begin() as connection:
        existing = connection.execute(
            sa.select(deliveries).where(deliveries.c.delivery_key == key)
        ).mappings().one_or_none()
        if existing is not None:
            return replay(connection, existing)
        stored = redact_payload(raw_payload) if len(raw_payload) <= MAX_STORED_PAYLOAD_BYTES else None
        try:
            with connection.begin_nested():
                delivery_id = connection.execute(
                    sa.insert(deliveries).values(
                        provider=provider, environment=environment, delivery_key=key,
                        payload_sha256=payload_sha256, payload_size=len(raw_payload),
                        redacted_payload=stored, authentication_state=auth_state,
                        authentication_method=auth.method if authenticator is not None else None,
                        event_category="unknown", processing_state="received", attempts=1,
                        received_at=now, last_received_at=now,
                    )
                ).inserted_primary_key[0]
        except IntegrityError:
            # A concurrent identical delivery won the insert: treat ours as its replay.
            existing = connection.execute(
                sa.select(deliveries).where(deliveries.c.delivery_key == key)
            ).mappings().one()
            return replay(connection, existing)
        return RecordedDelivery(delivery_id, False, 1, auth_state)


def admit_delivery(
    engine: Engine,
    *,
    delivery_id: int,
    tenant_id: int,
    provider_event_ref: str,
    event_type: str,
    event_category: str,
    provider_event_id: Optional[int] = None,
) -> None:
    """Bind a verified delivery to one tenant and event; refuses everything else.

    A delivery already admitted for another tenant or event is a conflict, not
    an update: the ledger never re-attributes evidence.
    """
    if tenant_id <= 0 or not provider_event_ref or not event_type:
        raise WebhookLedgerError("Invalid admission scope")
    if event_category not in EVENT_CATEGORIES:
        raise WebhookLedgerError(f"Unknown event category {event_category!r}")
    deliveries = MerchantPaymentWebhookDelivery.__table__
    with engine.begin() as connection:
        row = connection.execute(
            sa.select(deliveries).where(deliveries.c.id == delivery_id)
        ).mappings().one_or_none()
        if row is None:
            raise WebhookLedgerError("Unknown delivery")
        if row["authentication_state"] != "verified":
            raise WebhookLedgerError("Only an authenticated delivery can be admitted")
        if row["processing_state"] in TERMINAL_STATES:
            raise WebhookLedgerError("Delivery already has a terminal outcome")
        if row["processing_state"] == "admitted":
            if (
                row["tenant_id"] != tenant_id
                or row["provider_event_ref"] != provider_event_ref
                or row["event_type"] != event_type
            ):
                raise WebhookLedgerError("Delivery is already admitted for a different tenant or event")
            return
        # The same provider event reference may not be admitted twice for
        # different tenants within one provider environment.
        other = connection.execute(
            sa.select(deliveries.c.tenant_id).where(
                deliveries.c.provider == row["provider"],
                deliveries.c.environment == row["environment"],
                deliveries.c.provider_event_ref == provider_event_ref,
                deliveries.c.tenant_id.is_not(None),
                deliveries.c.tenant_id != tenant_id,
            ).limit(1)
        ).scalar_one_or_none()
        if other is not None:
            raise WebhookLedgerError("Provider event reference is already attributed to another tenant")
        if provider_event_id is not None:
            events = MerchantPaymentProviderEvent.__table__
            event = connection.execute(
                sa.select(events.c.tenant_id, events.c.provider, events.c.environment, events.c.provider_event_ref)
                .where(events.c.id == provider_event_id)
            ).mappings().one_or_none()
            if event is None or (
                event["tenant_id"] != tenant_id
                or event["provider"] != row["provider"]
                or event["environment"] != row["environment"]
                or event["provider_event_ref"] != provider_event_ref
            ):
                raise WebhookLedgerError("Provider event row does not belong to this tenant and event")
        updated = connection.execute(
            sa.update(deliveries).where(
                deliveries.c.id == delivery_id, deliveries.c.processing_state == "received"
            ).values(
                tenant_id=tenant_id, provider_event_ref=provider_event_ref, event_type=event_type,
                event_category=event_category, processing_state="admitted",
                provider_event_id=provider_event_id,
            )
        ).rowcount
        if updated != 1:
            raise WebhookLedgerError("Delivery state changed concurrently; retry")


def complete_delivery(
    engine: Engine, *, delivery_id: int, outcome: str, reason: Optional[str] = None
) -> None:
    """Record the terminal outcome. ``processed`` needs prior admission; others need a reason."""
    if outcome not in TERMINAL_STATES:
        raise WebhookLedgerError(f"Unknown outcome {outcome!r}")
    if outcome != "processed" and not (reason and reason.strip()):
        raise WebhookLedgerError("A non-processed outcome requires a reason")
    deliveries = MerchantPaymentWebhookDelivery.__table__
    now = datetime.now(timezone.utc)
    with engine.begin() as connection:
        row = connection.execute(
            sa.select(deliveries.c.processing_state).where(deliveries.c.id == delivery_id)
        ).mappings().one_or_none()
        if row is None:
            raise WebhookLedgerError("Unknown delivery")
        state = row["processing_state"]
        if state in TERMINAL_STATES:
            raise WebhookLedgerError("Delivery already has a terminal outcome")
        if outcome == "processed" and state != "admitted":
            raise WebhookLedgerError("Only an admitted delivery can be marked processed")
        updated = connection.execute(
            sa.update(deliveries).where(
                deliveries.c.id == delivery_id, deliveries.c.processing_state == state
            ).values(
                processing_state=outcome,
                outcome_reason=reason.strip()[:255] if reason else None,
                processed_at=now,
            )
        ).rowcount
        if updated != 1:
            raise WebhookLedgerError("Delivery state changed concurrently; retry")


def tenant_deliveries(engine: Engine, *, tenant_id: int) -> Sequence[Mapping]:
    """Deliveries attributed to one tenant; never another tenant's, never unattributed ones."""
    if tenant_id <= 0:
        raise WebhookLedgerError("Invalid tenant")
    deliveries = MerchantPaymentWebhookDelivery.__table__
    with engine.connect() as connection:
        return connection.execute(
            sa.select(deliveries).where(deliveries.c.tenant_id == tenant_id).order_by(deliveries.c.id)
        ).mappings().all()
