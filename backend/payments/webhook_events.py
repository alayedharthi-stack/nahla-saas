"""Contract-independent webhook admission for Nahlah Payments.

Moyasar documents a shared secret on webhook delivery. This module deliberately
does not parse provider money payloads or mutate payment/settlement records.
Marketplace tenant mapping remains contract-dependent and must be supplied by
an authenticated caller after the provider account/sub-merchant model is known.
"""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from .models import MerchantPaymentProfile, MerchantPaymentProviderEvent


class ProviderEventConflict(ValueError):
    """The event cannot safely be admitted to the tenant's financial ledger."""


@dataclass(frozen=True)
class AdmittedProviderEvent:
    event_id: int
    replay: bool


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def shared_secret_matches(*, supplied: str, expected: str) -> bool:
    """Constant-time comparison; secrets are never persisted by this module."""
    if not supplied or not expected:
        return False
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def admit_provider_event(
    engine: Engine,
    *,
    tenant_id: int,
    provider: str,
    environment: str,
    provider_event_ref: str,
    event_type: str,
    raw_payload: bytes,
    supplied_secret: str,
    expected_secret: str,
) -> AdmittedProviderEvent:
    """Authenticate and deduplicate one already tenant-routed provider event.

    This is storage plumbing, not an HTTP webhook route. The caller owns the
    provider-specific extraction of event id/type/secret and tenant mapping.
    """
    if (
        tenant_id <= 0 or not provider or environment not in ("test", "live")
        or not provider_event_ref or not event_type or not raw_payload
    ):
        raise ValueError("Invalid provider event scope")
    if not shared_secret_matches(supplied=supplied_secret, expected=expected_secret):
        raise ProviderEventConflict("Provider event authentication failed")

    profiles = MerchantPaymentProfile.__table__
    events = MerchantPaymentProviderEvent.__table__
    payload_sha256 = _digest(raw_payload)
    now = datetime.now(timezone.utc)

    with engine.begin() as connection:
        profile = connection.execute(
            sa.select(profiles.c.id).where(
                profiles.c.tenant_id == tenant_id,
                profiles.c.provider == provider,
                profiles.c.environment == environment,
                profiles.c.onboarding_status == "approved",
            )
        ).scalar_one_or_none()
        if profile is None:
            raise ProviderEventConflict("Merchant is not provider-approved")

        existing = connection.execute(
            sa.select(events).where(
                events.c.provider == provider,
                events.c.environment == environment,
                events.c.provider_event_ref == provider_event_ref,
            )
        ).mappings().one_or_none()
        if existing is not None:
            if (
                existing["tenant_id"] != tenant_id
                or existing["event_type"] != event_type
                or existing["payload_sha256"] != payload_sha256
            ):
                raise ProviderEventConflict("Provider event replay conflicts with stored evidence")
            return AdmittedProviderEvent(event_id=existing["id"], replay=True)

        result = connection.execute(sa.insert(events).values(
            tenant_id=tenant_id,
            provider=provider,
            environment=environment,
            provider_event_ref=provider_event_ref,
            event_type=event_type,
            payload_sha256=payload_sha256,
            authenticated_at=now,
        ))
        return AdmittedProviderEvent(event_id=result.inserted_primary_key[0], replay=False)
