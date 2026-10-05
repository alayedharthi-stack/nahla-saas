"""Per-merchant payment activation for Nahlah AI; dormant until evidence enables it.

Onboarding (``MerchantPaymentProfile.onboarding_status``) says what the provider
decided. Activation says whether Nahlah AI may use this merchant's provider
account at all. The two are separate so a provider approval never silently
turns live payments on. ``payment_acceptance_enabled`` is the single
deterministic gate any future route must consult; with no row it is ``False``.

Credentials are never stored: ``credential_ref`` and ``webhook_secret_ref`` are
the names of entries in the deployment's secret manager.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Tuple

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from .models import MerchantPaymentActivation, MerchantPaymentFeePolicy, MerchantPaymentProfile
from .secret_refs import validate_secret_ref


class ActivationError(ValueError):
    """The activation change is not allowed by current state or evidence."""


@dataclass(frozen=True)
class ActivationReadiness:
    """Structured readiness facts; ``blockers`` empty means enable_* may proceed."""

    tenant_id: int
    provider: str
    environment: str
    onboarding_status: str
    activation_state: str
    fee_policy_present: bool
    credential_ref_present: bool
    webhook_secret_ref_present: bool
    blockers: Tuple[str, ...]

    @property
    def ready_to_enable(self) -> bool:
        return not self.blockers

    @property
    def enabled(self) -> bool:
        return self.activation_state == "enabled" and self.onboarding_status == "approved"


def _validate_scope(tenant_id: int, provider: str, environment: str) -> None:
    if tenant_id <= 0 or not provider or environment not in ("test", "live"):
        raise ValueError("Invalid activation scope")


def _load(connection, *, tenant_id: int, provider: str, environment: str):
    profiles = MerchantPaymentProfile.__table__
    activations = MerchantPaymentActivation.__table__
    fees = MerchantPaymentFeePolicy.__table__
    scope = lambda table: (  # noqa: E731
        table.c.tenant_id == tenant_id,
        table.c.provider == provider,
        table.c.environment == environment,
    )
    profile = connection.execute(sa.select(profiles).where(*scope(profiles))).mappings().one_or_none()
    activation = connection.execute(
        sa.select(activations).where(*scope(activations))
    ).mappings().one_or_none()
    fee_policy = connection.execute(
        sa.select(fees.c.id).where(
            *scope(fees), fees.c.effective_at <= datetime.now(timezone.utc)
        ).limit(1)
    ).scalar_one_or_none()
    return profile, activation, fee_policy is not None


def activation_readiness(
    engine: Engine, *, tenant_id: int, provider: str, environment: str
) -> ActivationReadiness:
    _validate_scope(tenant_id, provider, environment)
    with engine.connect() as connection:
        return _readiness_in(connection, tenant_id, provider, environment)


def payment_acceptance_enabled(
    engine: Engine, *, tenant_id: int, provider: str, environment: str
) -> bool:
    """Deterministic gate: True only for an approved profile with an enabled row."""
    return activation_readiness(
        engine, tenant_id=tenant_id, provider=provider, environment=environment
    ).enabled


def register_secret_refs(
    engine: Engine,
    *,
    tenant_id: int,
    provider: str,
    environment: str,
    credential_ref: Optional[str] = None,
    webhook_secret_ref: Optional[str] = None,
) -> int:
    """Record secret-manager *names* on the dormant activation row (never values)."""
    _validate_scope(tenant_id, provider, environment)
    values = {}
    if credential_ref is not None:
        values["credential_ref"] = validate_secret_ref(credential_ref, field="credential_ref")
    if webhook_secret_ref is not None:
        values["webhook_secret_ref"] = validate_secret_ref(webhook_secret_ref, field="webhook_secret_ref")
    if not values:
        raise ActivationError("Nothing to register")
    activations = MerchantPaymentActivation.__table__
    now = datetime.now(timezone.utc)
    with engine.begin() as connection:
        profile, activation, _ = _load(
            connection, tenant_id=tenant_id, provider=provider, environment=environment
        )
        if profile is None:
            raise ActivationError("No payment profile for this tenant, provider and environment")
        if activation is None:
            return connection.execute(
                sa.insert(activations).values(
                    tenant_id=tenant_id, provider=provider, environment=environment,
                    activation_state="dormant", created_at=now, updated_at=now, **values,
                )
            ).inserted_primary_key[0]
        if activation["activation_state"] == "enabled":
            raise ActivationError("Disable payments before rotating secret references")
        connection.execute(
            sa.update(activations).where(activations.c.id == activation["id"]).values(updated_at=now, **values)
        )
        return activation["id"]


def enable_merchant_payments(
    engine: Engine, *, tenant_id: int, provider: str, environment: str, evidence_ref: str
) -> ActivationReadiness:
    """Flip the switch only when every readiness blocker is clear; records evidence."""
    _validate_scope(tenant_id, provider, environment)
    if not evidence_ref or not evidence_ref.strip():
        raise ActivationError("Enabling payments requires an owner approval evidence reference")
    activations = MerchantPaymentActivation.__table__
    now = datetime.now(timezone.utc)
    with engine.begin() as connection:
        readiness = _readiness_in(connection, tenant_id, provider, environment)
        if readiness.blockers:
            raise ActivationError("Not ready to enable: " + ", ".join(readiness.blockers))
        if readiness.activation_state == "enabled":
            return readiness
        updated = connection.execute(
            sa.update(activations).where(
                activations.c.tenant_id == tenant_id,
                activations.c.provider == provider,
                activations.c.environment == environment,
                activations.c.activation_state != "enabled",
            ).values(
                activation_state="enabled", enabled_at=now, enabled_evidence_ref=evidence_ref.strip(),
                disabled_at=None, disabled_reason=None, updated_at=now,
            )
        ).rowcount
        if updated != 1:
            raise ActivationError("Activation changed concurrently; retry")
        return _readiness_in(connection, tenant_id, provider, environment)


def disable_merchant_payments(
    engine: Engine, *, tenant_id: int, provider: str, environment: str, reason: str
) -> ActivationReadiness:
    """Always allowed; disabling needs a reason, never evidence."""
    _validate_scope(tenant_id, provider, environment)
    if not reason or not reason.strip():
        raise ActivationError("Disabling payments requires a reason")
    activations = MerchantPaymentActivation.__table__
    now = datetime.now(timezone.utc)
    with engine.begin() as connection:
        profile, activation, _ = _load(
            connection, tenant_id=tenant_id, provider=provider, environment=environment
        )
        if profile is None:
            raise ActivationError("No payment profile for this tenant, provider and environment")
        values = dict(
            activation_state="disabled", disabled_at=now, disabled_reason=reason.strip()[:255],
            updated_at=now,
        )
        if activation is None:
            connection.execute(
                sa.insert(activations).values(
                    tenant_id=tenant_id, provider=provider, environment=environment,
                    created_at=now, **values,
                )
            )
        else:
            connection.execute(
                sa.update(activations).where(activations.c.id == activation["id"]).values(**values)
            )
        return _readiness_in(connection, tenant_id, provider, environment)


def _readiness_in(connection, tenant_id: int, provider: str, environment: str) -> ActivationReadiness:
    profile, activation, fee_present = _load(
        connection, tenant_id=tenant_id, provider=provider, environment=environment
    )
    onboarding = profile["onboarding_status"] if profile else "not_started"
    state = activation["activation_state"] if activation else "dormant"
    cred = bool(activation and activation["credential_ref"])
    hook = bool(activation and activation["webhook_secret_ref"])
    blockers = []
    if onboarding != "approved":
        blockers.append("profile_not_provider_approved")
    if not fee_present:
        blockers.append("no_effective_fee_policy")
    if not cred:
        blockers.append("credential_ref_missing")
    if not hook:
        blockers.append("webhook_secret_ref_missing")
    return ActivationReadiness(
        tenant_id=tenant_id, provider=provider, environment=environment,
        onboarding_status=onboarding, activation_state=state,
        fee_policy_present=fee_present, credential_ref_present=cred,
        webhook_secret_ref_present=hook, blockers=tuple(blockers),
    )
