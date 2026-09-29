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


PAYMENT_TABLES = (
    MerchantPaymentProfile.__table__,
    MerchantPaymentFeePolicy.__table__,
    MerchantPaymentTransaction.__table__,
    MerchantPaymentAllocation.__table__,
    MerchantPaymentSettlement.__table__,
    MerchantPaymentProviderEvent.__table__,
)
