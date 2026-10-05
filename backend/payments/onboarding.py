"""Deterministic merchant onboarding state for Nahlah AI payments.

The profile's ``onboarding_status`` is operational state, so every change goes
through one transition table, requires evidence where the status claims
provider approval, and is recorded in an append-only audit. The provider's own
status vocabulary (Platform API registration/KYB webhooks) is stored verbatim as
``provider_status`` and mapped to Nahlah AI's closed vocabulary by the caller
only after the real webhook contract is known; nothing here guesses it.

No route imports this module. A transition never calls the provider.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import FrozenSet, Mapping, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from .models import MerchantPaymentOnboardingEvent, MerchantPaymentProfile

ONBOARDING_STATUSES: FrozenSet[str] = frozenset(
    {"not_started", "pending", "approved", "rejected", "suspended"}
)
TRANSITION_SOURCES: FrozenSet[str] = frozenset({"operator", "provider_webhook", "provider_read"})

# Closed transition table. ``approved`` is reachable only from a submitted or
# suspended registration, never directly from ``not_started``.
ALLOWED_TRANSITIONS: Mapping[str, FrozenSet[str]] = {
    "not_started": frozenset({"pending"}),
    "pending": frozenset({"approved", "rejected"}),
    "approved": frozenset({"suspended"}),
    "suspended": frozenset({"approved", "rejected"}),
    "rejected": frozenset({"pending"}),
}


class OnboardingTransitionError(ValueError):
    """The requested onboarding change is not allowed or lacks evidence."""


@dataclass(frozen=True)
class OnboardingTransition:
    profile_id: int
    from_status: str
    to_status: str
    event_id: Optional[int]
    replay: bool


def _validate_scope(tenant_id: int, provider: str, environment: str) -> None:
    if tenant_id <= 0 or not provider or environment not in ("test", "live"):
        raise ValueError("Invalid onboarding scope")


def ensure_profile(engine: Engine, *, tenant_id: int, provider: str, environment: str) -> int:
    """Create the dormant ``not_started`` profile row if missing; return its id."""
    _validate_scope(tenant_id, provider, environment)
    profiles = MerchantPaymentProfile.__table__
    with engine.begin() as connection:
        existing = connection.execute(
            sa.select(profiles.c.id).where(
                profiles.c.tenant_id == tenant_id,
                profiles.c.provider == provider,
                profiles.c.environment == environment,
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        return connection.execute(
            sa.insert(profiles).values(
                tenant_id=tenant_id, provider=provider, environment=environment,
                onboarding_status="not_started",
            )
        ).inserted_primary_key[0]


def transition_onboarding(
    engine: Engine,
    *,
    tenant_id: int,
    provider: str,
    environment: str,
    to_status: str,
    source: str,
    evidence_ref: Optional[str] = None,
    provider_merchant_ref: Optional[str] = None,
    provider_registration_ref: Optional[str] = None,
    provider_status: Optional[str] = None,
) -> OnboardingTransition:
    """Apply one allowed transition atomically and record its audit event.

    ``approved`` requires both ``provider_merchant_ref`` and ``evidence_ref``
    (a provider-side reference an operator can verify). Repeating the current
    status with the same merchant reference is a harmless replay and writes
    nothing; a replay carrying a different merchant reference is a conflict.
    """
    _validate_scope(tenant_id, provider, environment)
    if to_status not in ONBOARDING_STATUSES:
        raise OnboardingTransitionError(f"Unknown onboarding status {to_status!r}")
    if source not in TRANSITION_SOURCES:
        raise OnboardingTransitionError(f"Unknown transition source {source!r}")
    if to_status == "approved" and (not provider_merchant_ref or not evidence_ref):
        raise OnboardingTransitionError("Approval requires provider merchant reference and evidence")

    profiles = MerchantPaymentProfile.__table__
    events = MerchantPaymentOnboardingEvent.__table__
    now = datetime.now(timezone.utc)

    with engine.begin() as connection:
        profile = connection.execute(
            sa.select(profiles).where(
                profiles.c.tenant_id == tenant_id,
                profiles.c.provider == provider,
                profiles.c.environment == environment,
            )
        ).mappings().one_or_none()
        if profile is None:
            raise OnboardingTransitionError("No payment profile for this tenant, provider and environment")

        current = profile["onboarding_status"]
        if to_status == current:
            if provider_merchant_ref and profile["provider_merchant_ref"] not in (None, provider_merchant_ref):
                raise OnboardingTransitionError("Replay conflicts with the stored provider merchant reference")
            return OnboardingTransition(profile["id"], current, current, None, replay=True)
        if to_status not in ALLOWED_TRANSITIONS[current]:
            raise OnboardingTransitionError(f"Transition {current!r} -> {to_status!r} is not allowed")
        if (
            provider_merchant_ref
            and profile["provider_merchant_ref"]
            and profile["provider_merchant_ref"] != provider_merchant_ref
        ):
            raise OnboardingTransitionError("Provider merchant reference does not match this profile")

        values = {"onboarding_status": to_status}
        if to_status == "approved":
            values.update(
                provider_merchant_ref=provider_merchant_ref,
                approval_evidence_ref=evidence_ref,
                approved_at=now,
            )
        elif provider_merchant_ref and not profile["provider_merchant_ref"]:
            values["provider_merchant_ref"] = provider_merchant_ref

        updated = connection.execute(
            sa.update(profiles).where(
                profiles.c.id == profile["id"],
                profiles.c.onboarding_status == current,  # optimistic guard against races
            ).values(**values)
        ).rowcount
        if updated != 1:
            raise OnboardingTransitionError("Onboarding status changed concurrently; retry")

        event_id = connection.execute(
            sa.insert(events).values(
                tenant_id=tenant_id, provider=provider, environment=environment,
                from_status=current, to_status=to_status, source=source,
                evidence_ref=evidence_ref,
                provider_merchant_ref=provider_merchant_ref or profile["provider_merchant_ref"],
                provider_registration_ref=provider_registration_ref,
                provider_status=provider_status,
                recorded_at=now,
            )
        ).inserted_primary_key[0]
        return OnboardingTransition(profile["id"], current, to_status, event_id, replay=False)


def tenant_for_provider_reference(
    engine: Engine, *, provider: str, environment: str, reference: str
) -> Optional[int]:
    """Resolve a provider merchant or registration reference to exactly one tenant.

    Returns ``None`` when unknown. Raises when the reference is ambiguous, so a
    webhook can never be attributed to the wrong merchant.
    """
    if not provider or environment not in ("test", "live") or not reference:
        raise ValueError("Invalid provider reference scope")
    profiles = MerchantPaymentProfile.__table__
    events = MerchantPaymentOnboardingEvent.__table__
    with engine.connect() as connection:
        by_merchant = connection.execute(
            sa.select(profiles.c.tenant_id).where(
                profiles.c.provider == provider,
                profiles.c.environment == environment,
                profiles.c.provider_merchant_ref == reference,
            )
        ).scalars().all()
        by_registration = connection.execute(
            sa.select(sa.distinct(events.c.tenant_id)).where(
                events.c.provider == provider,
                events.c.environment == environment,
                events.c.provider_registration_ref == reference,
            )
        ).scalars().all()
    tenants = set(by_merchant) | set(by_registration)
    if not tenants:
        return None
    if len(tenants) > 1:
        raise OnboardingTransitionError("Provider reference maps to more than one tenant")
    return tenants.pop()
