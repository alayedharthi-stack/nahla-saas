"""Dormant, tenant-scoped OTO credentials. Created only by an explicit migration."""
from datetime import datetime, timezone

from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, ForeignKey, Integer, String, Table, UniqueConstraint
from sqlalchemy.orm import declarative_base

OtoBase = declarative_base()
Table("tenants", OtoBase.metadata, Column("id", Integer, primary_key=True))


class OtoConnection(OtoBase):
    __tablename__ = "oto_connections"
    __table_args__ = (
        UniqueConstraint("tenant_id", "environment", name="uq_oto_connection_tenant_environment"),
        CheckConstraint("environment IN ('staging', 'production')", name="ck_oto_connection_environment"),
    )

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)
    environment = Column(String(16), nullable=False)
    refresh_token_ciphertext = Column(String, nullable=False)
    webhook_secret_ciphertext = Column(String, nullable=True)
    pickup_location_code = Column(String(120), nullable=True)
    pickup_city = Column(String(120), nullable=True)
    enabled = Column(Boolean, nullable=False, default=False, server_default="false")
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
