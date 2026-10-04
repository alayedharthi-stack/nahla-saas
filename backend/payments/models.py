"""Tenant-bound accounting references for marketplace payments.

This metadata is deliberately separate from ``database.models.Base``. Backend
startup runs ``Base.metadata.create_all``; importing these classes must never
create financial tables in production ahead of an explicit migration.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import declarative_base


PaymentBase = declarative_base()

# Only the referenced key is mirrored. This table is never created by the
# payments migration; it resolves the foreign keys in this separate metadata.
Table("tenants", PaymentBase.metadata, Column("id", Integer, primary_key=True))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MerchantPaymentProfile(PaymentBase):
    """Opaque provider identity and provider-backed approval evidence, no KYC PII."""

    __tablename__ = "merchant_payment_profiles"
    __table_args__ = (
        UniqueConstraint("tenant_id", "provider", "environment", name="uq_mpp_tenant_provider"),
        UniqueConstraint("provider", "environment", "provider_merchant_ref", name="uq_mpp_provider_ref"),
        CheckConstraint("environment IN ('test', 'live')", name="ck_mpp_environment"),
        CheckConstraint(
            "onboarding_status IN ('not_started', 'pending', 'approved', 'rejected', 'suspended')",
            name="ck_mpp_onboarding_status",
        ),
        CheckConstraint(
            "onboarding_status != 'approved' OR "
            "(provider_merchant_ref IS NOT NULL AND approval_evidence_ref IS NOT NULL AND approved_at IS NOT NULL)",
            name="ck_mpp_approval_evidence",
        ),
    )

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    provider = Column(String(40), nullable=False)
    environment = Column(String(4), nullable=False)
    provider_merchant_ref = Column(String(255), nullable=True)
    onboarding_status = Column(String(24), nullable=False, default="not_started", server_default="not_started")
    approval_evidence_ref = Column(String(255), nullable=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    # Provider's opaque account reference only: no IBAN, ID, documents or key.
    settlement_account_ref = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class MerchantPaymentFeePolicy(PaymentBase):
    """Versioned fee choice; no default percentage is silently assigned."""

    __tablename__ = "merchant_payment_fee_policies"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "provider", "environment"],
            ["merchant_payment_profiles.tenant_id", "merchant_payment_profiles.provider",
             "merchant_payment_profiles.environment"],
            name="fk_mpfee_profile",
        ),
        UniqueConstraint("tenant_id", "provider", "environment", "effective_at", name="uq_mpfee_effective"),
        Index("ix_mpfee_tenant_provider_effective", "tenant_id", "provider", "environment", "effective_at"),
        CheckConstraint("environment IN ('test', 'live')", name="ck_mpfee_environment"),
        CheckConstraint("rate_percent >= 0 AND rate_percent <= 100", name="ck_mpfee_rate"),
    )

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    provider = Column(String(40), nullable=False)
    environment = Column(String(4), nullable=False)
    rate_percent = Column(Numeric(7, 4), nullable=False)
    effective_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class MerchantPaymentTransaction(PaymentBase):
    """Provider payment observation; an unverified row is never a receivable."""

    __tablename__ = "merchant_payment_transactions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "provider", "environment"],
            ["merchant_payment_profiles.tenant_id", "merchant_payment_profiles.provider",
             "merchant_payment_profiles.environment"],
            name="fk_mpt_profile",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_mpt_tenant_id"),
        UniqueConstraint("tenant_id", "id", "gross_amount", name="uq_mpt_tenant_gross"),
        UniqueConstraint("provider", "environment", "provider_payment_ref", name="uq_mpt_provider_payment"),
        Index("ix_mpt_tenant_created", "tenant_id", "created_at"),
        CheckConstraint("environment IN ('test', 'live')", name="ck_mpt_environment"),
        CheckConstraint("gross_amount > 0", name="ck_mpt_gross_positive"),
        CheckConstraint("length(currency) = 3", name="ck_mpt_currency"),
        CheckConstraint(
            "verification_state IN ('unverified', 'provider_confirmed')",
            name="ck_mpt_verification_state",
        ),
        CheckConstraint(
            "verification_state != 'provider_confirmed' OR "
            "(provider_payment_ref IS NOT NULL AND provider_observed_at IS NOT NULL)",
            name="ck_mpt_verified_evidence",
        ),
    )

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    provider = Column(String(40), nullable=False)
    environment = Column(String(4), nullable=False)
    provider_payment_ref = Column(String(255), nullable=True)
    currency = Column(String(3), nullable=False)
    gross_amount = Column(Numeric(18, 2), nullable=False)
    verification_state = Column(String(24), nullable=False, default="unverified", server_default="unverified")
    provider_status = Column(String(80), nullable=True)
    provider_observed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class MerchantPaymentAllocation(PaymentBase):
    """Fee quote snapshot, never evidence of an actual provider split/transfer."""

    __tablename__ = "merchant_payment_allocations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "transaction_id", "gross_amount"],
            ["merchant_payment_transactions.tenant_id", "merchant_payment_transactions.id",
             "merchant_payment_transactions.gross_amount"],
            name="fk_mpa_tenant_transaction",
        ),
        UniqueConstraint("transaction_id", name="uq_mpa_transaction"),
        Index("ix_mpa_tenant_transaction", "tenant_id", "transaction_id"),
        CheckConstraint("gross_amount > 0", name="ck_mpa_gross_positive"),
        CheckConstraint(
            "platform_fee_amount >= 0 AND merchant_gross_share >= 0 AND "
            "platform_fee_amount + merchant_gross_share = gross_amount",
            name="ck_mpa_components",
        ),
        CheckConstraint("fee_rate_percent >= 0 AND fee_rate_percent <= 100", name="ck_mpa_rate"),
    )

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    transaction_id = Column(Integer, nullable=False)
    gross_amount = Column(Numeric(18, 2), nullable=False)
    platform_fee_amount = Column(Numeric(18, 2), nullable=False)
    merchant_gross_share = Column(Numeric(18, 2), nullable=False)
    fee_rate_percent = Column(Numeric(7, 4), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class MerchantPaymentSettlement(PaymentBase):
    """Read-only record of a provider settlement, not an instruction to pay out."""

    __tablename__ = "merchant_payment_settlements"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "provider", "environment"],
            ["merchant_payment_profiles.tenant_id", "merchant_payment_profiles.provider",
             "merchant_payment_profiles.environment"],
            name="fk_mps_profile",
        ),
        UniqueConstraint("provider", "environment", "provider_settlement_ref", name="uq_mps_provider_settlement"),
        Index("ix_mps_tenant_observed", "tenant_id", "provider_observed_at"),
        CheckConstraint("environment IN ('test', 'live')", name="ck_mps_environment"),
        CheckConstraint("length(currency) = 3", name="ck_mps_currency"),
    )

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    provider = Column(String(40), nullable=False)
    environment = Column(String(4), nullable=False)
    provider_settlement_ref = Column(String(255), nullable=False)
    recipient_ref = Column(String(255), nullable=True)
    currency = Column(String(3), nullable=False)
    reported_amount = Column(Numeric(18, 2), nullable=False)
    provider_status = Column(String(80), nullable=False)
    provider_observed_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class MerchantPaymentProviderEvent(PaymentBase):
    """Deduplication key and digest only; never raw card data or webhook secret."""

    __tablename__ = "merchant_payment_provider_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "provider", "environment"],
            ["merchant_payment_profiles.tenant_id", "merchant_payment_profiles.provider",
             "merchant_payment_profiles.environment"],
            name="fk_mp_event_profile",
        ),
        UniqueConstraint("provider", "environment", "provider_event_ref", name="uq_mp_event_provider_ref"),
        Index("ix_mp_event_tenant_received", "tenant_id", "received_at"),
        CheckConstraint("environment IN ('test', 'live')", name="ck_mp_event_environment"),
        CheckConstraint("length(payload_sha256) = 64", name="ck_mp_event_digest"),
    )

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    provider = Column(String(40), nullable=False)
    environment = Column(String(4), nullable=False)
    provider_event_ref = Column(String(255), nullable=False)
    event_type = Column(String(80), nullable=False)
    payload_sha256 = Column(String(64), nullable=False)
    authenticated_at = Column(DateTime(timezone=True), nullable=False)
    received_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    processed_at = Column(DateTime(timezone=True), nullable=True)



# ── Revision 0116: dormant readiness tables ─────────────────────────────────
# Everything below is created only by migration 0116, never by 0115. The 0115
# relations are left untouched: readiness state lives in separate tables that
# reference the merchant profile through its composite key. No API key, webhook
# secret, IBAN, identity document or card data is ever stored here; credential
# columns hold only the *name* of a secret in the deployment's secret manager.


class MerchantPaymentActivation(PaymentBase):
    """Per-profile activation switch; dormant until explicit evidence enables it."""

    __tablename__ = "merchant_payment_activations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "provider", "environment"],
            ["merchant_payment_profiles.tenant_id", "merchant_payment_profiles.provider",
             "merchant_payment_profiles.environment"],
            name="fk_mpact_profile",
        ),
        UniqueConstraint("tenant_id", "provider", "environment", name="uq_mpact_tenant_provider"),
        CheckConstraint("environment IN ('test', 'live')", name="ck_mpact_environment"),
        CheckConstraint(
            "activation_state IN ('dormant', 'enabled', 'disabled')",
            name="ck_mpact_state",
        ),
        CheckConstraint(
            "activation_state != 'enabled' OR "
            "(credential_ref IS NOT NULL AND webhook_secret_ref IS NOT NULL "
            "AND enabled_at IS NOT NULL AND enabled_evidence_ref IS NOT NULL)",
            name="ck_mpact_enabled_evidence",
        ),
        CheckConstraint(
            "activation_state != 'disabled' OR (disabled_at IS NOT NULL AND disabled_reason IS NOT NULL)",
            name="ck_mpact_disabled_reason",
        ),
    )

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    provider = Column(String(40), nullable=False)
    environment = Column(String(4), nullable=False)
    activation_state = Column(String(16), nullable=False, default="dormant", server_default="dormant")
    # Names of secrets in the secret manager, never the secret values.
    credential_ref = Column(String(120), nullable=True)
    webhook_secret_ref = Column(String(120), nullable=True)
    enabled_at = Column(DateTime(timezone=True), nullable=True)
    enabled_evidence_ref = Column(String(255), nullable=True)
    disabled_at = Column(DateTime(timezone=True), nullable=True)
    disabled_reason = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


class MerchantPaymentOnboardingEvent(PaymentBase):
    """Append-only audit of every onboarding status transition and its evidence."""

    __tablename__ = "merchant_payment_onboarding_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "provider", "environment"],
            ["merchant_payment_profiles.tenant_id", "merchant_payment_profiles.provider",
             "merchant_payment_profiles.environment"],
            name="fk_mpobe_profile",
        ),
        Index("ix_mpobe_tenant_recorded", "tenant_id", "recorded_at"),
        Index("ix_mpobe_registration_ref", "provider", "environment", "provider_registration_ref"),
        CheckConstraint("environment IN ('test', 'live')", name="ck_mpobe_environment"),
        CheckConstraint(
            "from_status IN ('not_started', 'pending', 'approved', 'rejected', 'suspended')",
            name="ck_mpobe_from_status",
        ),
        CheckConstraint(
            "to_status IN ('not_started', 'pending', 'approved', 'rejected', 'suspended')",
            name="ck_mpobe_to_status",
        ),
        CheckConstraint(
            "source IN ('operator', 'provider_webhook', 'provider_read')",
            name="ck_mpobe_source",
        ),
        CheckConstraint(
            "to_status != 'approved' OR (evidence_ref IS NOT NULL AND provider_merchant_ref IS NOT NULL)",
            name="ck_mpobe_approval_evidence",
        ),
    )

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    provider = Column(String(40), nullable=False)
    environment = Column(String(4), nullable=False)
    from_status = Column(String(24), nullable=False)
    to_status = Column(String(24), nullable=False)
    source = Column(String(24), nullable=False)
    evidence_ref = Column(String(255), nullable=True)
    provider_merchant_ref = Column(String(255), nullable=True)
    provider_registration_ref = Column(String(255), nullable=True)
    # Provider's own status word, stored verbatim and never interpreted as money.
    provider_status = Column(String(80), nullable=True)
    recorded_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class MerchantPaymentWebhookDelivery(PaymentBase):
    """Durable, provider-neutral record of every inbound delivery before any effect.

    A delivery is stored before it is authenticated, mapped to a tenant or
    interpreted. Identical redeliveries collapse onto one row by digest and
    only increment ``attempts``. The stored body is redacted; the webhook
    secret is never stored.
    """

    __tablename__ = "merchant_payment_webhook_deliveries"
    __table_args__ = (
        UniqueConstraint("delivery_key", name="uq_mpwd_delivery_key"),
        Index("ix_mpwd_tenant_received", "tenant_id", "received_at"),
        Index("ix_mpwd_state_received", "processing_state", "received_at"),
        Index("ix_mpwd_event_ref", "provider", "environment", "provider_event_ref"),
        CheckConstraint("environment IN ('test', 'live')", name="ck_mpwd_environment"),
        CheckConstraint("length(delivery_key) = 64", name="ck_mpwd_delivery_key"),
        CheckConstraint("length(payload_sha256) = 64", name="ck_mpwd_digest"),
        CheckConstraint("payload_size > 0", name="ck_mpwd_size"),
        CheckConstraint("attempts >= 1", name="ck_mpwd_attempts"),
        CheckConstraint(
            "authentication_state IN ('unverified', 'verified', 'failed')",
            name="ck_mpwd_auth_state",
        ),
        CheckConstraint(
            "event_category IN ('unknown', 'onboarding', 'payment', 'settlement', 'payout')",
            name="ck_mpwd_category",
        ),
        CheckConstraint(
            "processing_state IN ('received', 'admitted', 'rejected', 'processed', 'failed', 'ignored')",
            name="ck_mpwd_processing_state",
        ),
        CheckConstraint(
            "processing_state != 'admitted' OR (authentication_state = 'verified' AND tenant_id IS NOT NULL "
            "AND provider_event_ref IS NOT NULL AND event_type IS NOT NULL)",
            name="ck_mpwd_admission_requires_auth_and_tenant",
        ),
        CheckConstraint(
            "processing_state NOT IN ('rejected', 'failed', 'ignored') OR outcome_reason IS NOT NULL",
            name="ck_mpwd_outcome_reason",
        ),
        CheckConstraint(
            "processing_state NOT IN ('processed', 'rejected', 'failed', 'ignored') OR processed_at IS NOT NULL",
            name="ck_mpwd_processed_at",
        ),
    )

    id = Column(Integer, primary_key=True)
    provider = Column(String(40), nullable=False)
    environment = Column(String(4), nullable=False)
    delivery_key = Column(String(64), nullable=False)
    payload_sha256 = Column(String(64), nullable=False)
    payload_size = Column(Integer, nullable=False)
    redacted_payload = Column(Text, nullable=True)
    authentication_state = Column(String(16), nullable=False, default="unverified", server_default="unverified")
    authentication_method = Column(String(40), nullable=True)
    # Nullable until an authenticated mapping to a merchant tenant exists.
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True)
    provider_event_ref = Column(String(255), nullable=True)
    event_type = Column(String(80), nullable=True)
    event_category = Column(String(16), nullable=False, default="unknown", server_default="unknown")
    processing_state = Column(String(16), nullable=False, default="received", server_default="received")
    outcome_reason = Column(String(255), nullable=True)
    # Row in merchant_payment_provider_events once the event is admitted there.
    provider_event_id = Column(Integer, ForeignKey("merchant_payment_provider_events.id"), nullable=True)
    attempts = Column(Integer, nullable=False, default=1, server_default="1")
    received_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    last_received_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    processed_at = Column(DateTime(timezone=True), nullable=True)


class MerchantPaymentSettlementLine(PaymentBase):
    """Provider-reported settlement line; the only evidence that a payment settled."""

    __tablename__ = "merchant_payment_settlement_lines"
    __table_args__ = (
        ForeignKeyConstraint(
            ["provider", "environment", "provider_settlement_ref"],
            ["merchant_payment_settlements.provider", "merchant_payment_settlements.environment",
             "merchant_payment_settlements.provider_settlement_ref"],
            name="fk_mpsl_settlement",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "transaction_id"],
            ["merchant_payment_transactions.tenant_id", "merchant_payment_transactions.id"],
            name="fk_mpsl_tenant_transaction",
        ),
        UniqueConstraint(
            "provider", "environment", "provider_settlement_ref", "provider_line_ref",
            name="uq_mpsl_provider_line",
        ),
        Index("ix_mpsl_tenant_settlement", "tenant_id", "provider_settlement_ref"),
        Index("ix_mpsl_tenant_transaction", "tenant_id", "transaction_id"),
        CheckConstraint("environment IN ('test', 'live')", name="ck_mpsl_environment"),
        CheckConstraint("length(currency) = 3", name="ck_mpsl_currency"),
        CheckConstraint("amount != 0", name="ck_mpsl_amount_nonzero"),
        CheckConstraint(
            "line_type IN ('payment', 'fee', 'refund', 'chargeback', 'adjustment', 'other')",
            name="ck_mpsl_line_type",
        ),
    )

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    provider = Column(String(40), nullable=False)
    environment = Column(String(4), nullable=False)
    provider_settlement_ref = Column(String(255), nullable=False)
    provider_line_ref = Column(String(255), nullable=False)
    line_type = Column(String(16), nullable=False)
    # Set only when the line names a payment this tenant already observed.
    transaction_id = Column(Integer, nullable=True)
    provider_payment_ref = Column(String(255), nullable=True)
    currency = Column(String(3), nullable=False)
    amount = Column(Numeric(18, 2), nullable=False)
    provider_observed_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


PAYMENT_TABLES = (
    MerchantPaymentProfile.__table__,
    MerchantPaymentFeePolicy.__table__,
    MerchantPaymentTransaction.__table__,
    MerchantPaymentAllocation.__table__,
    MerchantPaymentSettlement.__table__,
    MerchantPaymentProviderEvent.__table__,
)

# Created only by migration 0116. Kept apart from PAYMENT_TABLES so that 0115
# keeps creating exactly the six foundation relations it was reviewed with.
PAYMENT_READINESS_TABLES = (
    MerchantPaymentActivation.__table__,
    MerchantPaymentOnboardingEvent.__table__,
    MerchantPaymentWebhookDelivery.__table__,
    MerchantPaymentSettlementLine.__table__,
)

ALL_PAYMENT_TABLES = PAYMENT_TABLES + PAYMENT_READINESS_TABLES
