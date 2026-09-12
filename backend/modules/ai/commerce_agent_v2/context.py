"""Trusted, tenant-bound run context for Commerce Agent V2."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from modules.ai.commerce_agent_v2.output import EvidenceRecord
from modules.ai.security.tenant_isolation import (
    TenantContext,
    TenantIsolationLayer,
    TenantIsolationViolation,
)


class CommerceContextError(RuntimeError):
    """Raised when the trusted database scope cannot be established."""


class CommerceCapabilities(BaseModel):
    """Phase-1 capabilities. No mutation or outbound capability exists."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    read_catalog: bool = True
    read_merchant_knowledge: bool = True
    read_product_knowledge: bool = True
    write_commerce: bool = False
    outbound_send: bool = False


class CommerceAgentContext(BaseModel):
    """Typed identity plus private dependencies supplied by trusted code only.

    ``tenant_id`` is intentionally absent from every tool signature. The SDK
    excludes ``RunContextWrapper`` from tool JSON schemas, so the model can
    neither provide nor override this value.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    tenant_id: int = Field(gt=0)
    tenant_name: str = ""
    conversation_id: int = Field(gt=0)
    customer_id: int | None = Field(default=None, gt=0)
    normalized_customer_phone: str = Field(min_length=1)
    channel: Literal["whatsapp"] = "whatsapp"
    connection_id: str = Field(min_length=1)
    inbound_trace_id: str = Field(min_length=1, max_length=256)
    locale: str = "ar-SA"
    timezone: str = "Asia/Riyadh"
    capabilities: CommerceCapabilities = Field(default_factory=CommerceCapabilities)

    _db: Any = PrivateAttr()
    _tenant_context: TenantContext = PrivateAttr()
    _allowed_product_ids: set[int] = PrivateAttr(default_factory=set)
    _evidence: dict[str, EvidenceRecord] = PrivateAttr(default_factory=dict)

    @classmethod
    def from_trusted_scope(
        cls,
        *,
        db: Any,
        tenant_id: int,
        conversation_id: int,
        customer_id: int | None,
        normalized_customer_phone: str,
        connection_id: str,
        inbound_trace_id: str,
        channel: Literal["whatsapp"] = "whatsapp",
    ) -> "CommerceAgentContext":
        from models import Conversation, Customer, Tenant, TenantSettings, WhatsAppConnection
        from utils.phone_utils import normalize_phone_compat

        canonical_phone = normalize_phone_compat(normalized_customer_phone)
        if not canonical_phone:
            raise CommerceContextError("invalid_customer_phone")

        tenant_ctx = TenantIsolationLayer.make_context(
            tenant_id,
            customer_phone=canonical_phone,
            customer_id=customer_id,
            request_id=inbound_trace_id,
        )
        conversation = (
            db.query(Conversation)
            .filter(
                Conversation.id == int(conversation_id),
                Conversation.tenant_id == tenant_ctx.tenant_id,
            )
            .one_or_none()
        )
        if conversation is None:
            raise CommerceContextError("conversation_not_in_tenant_scope")
        TenantIsolationLayer.assert_belongs(conversation, tenant_ctx)

        conversation_customer_id = (
            int(conversation.customer_id) if conversation.customer_id is not None else None
        )
        if customer_id is not None and conversation_customer_id != int(customer_id):
            raise CommerceContextError("customer_not_in_conversation_scope")
        resolved_customer_id = (
            int(customer_id) if customer_id is not None else conversation_customer_id
        )

        if resolved_customer_id is not None:
            customer = (
                db.query(Customer)
                .filter(
                    Customer.id == resolved_customer_id,
                    Customer.tenant_id == tenant_ctx.tenant_id,
                )
                .one_or_none()
            )
            if customer is None:
                raise CommerceContextError("customer_not_in_tenant_scope")
            TenantIsolationLayer.assert_belongs(customer, tenant_ctx)
            stored_phone = normalize_phone_compat(
                getattr(customer, "normalized_phone", None) or getattr(customer, "phone", None)
            )
            if not stored_phone or stored_phone != canonical_phone:
                raise CommerceContextError("customer_phone_not_in_identity_scope")

        connection = (
            db.query(WhatsAppConnection)
            .filter(
                WhatsAppConnection.id == int(connection_id),
                WhatsAppConnection.tenant_id == tenant_ctx.tenant_id,
            )
            .one_or_none()
            if str(connection_id).isdigit()
            else None
        )
        if connection is None:
            raise CommerceContextError("connection_not_in_tenant_scope")
        TenantIsolationLayer.assert_belongs(connection, tenant_ctx)

        tenant = (
            db.query(Tenant)
            .filter(
                Tenant.id == tenant_ctx.tenant_id,
                Tenant.is_active.is_(True),
            )
            .one_or_none()
        )
        if tenant is None:
            raise CommerceContextError("tenant_not_active")
        settings = (
            db.query(TenantSettings)
            .filter(TenantSettings.tenant_id == tenant_ctx.tenant_id)
            .one_or_none()
        )
        settings_meta = dict(getattr(settings, "extra_metadata", None) or {})
        ai_settings = dict(getattr(settings, "ai_settings", None) or {})
        instance = cls(
            tenant_id=tenant_ctx.tenant_id,
            tenant_name=str(getattr(tenant, "name", "") or ""),
            conversation_id=int(conversation.id),
            customer_id=resolved_customer_id,
            normalized_customer_phone=canonical_phone,
            channel=channel,
            connection_id=str(connection.id),
            inbound_trace_id=str(inbound_trace_id),
            locale=str(ai_settings.get("locale") or settings_meta.get("locale") or "ar-SA"),
            timezone=str(
                ai_settings.get("timezone")
                or settings_meta.get("timezone")
                or "Asia/Riyadh"
            ),
        )
        instance._db = db
        instance._tenant_context = TenantIsolationLayer.make_context(
            tenant_ctx.tenant_id,
            customer_phone=canonical_phone,
            customer_id=resolved_customer_id,
            request_id=inbound_trace_id,
        )
        return instance

    @property
    def db(self) -> Any:
        return self._db

    @property
    def tenant_context(self) -> TenantContext:
        return self._tenant_context

    @property
    def evidence(self) -> dict[str, EvidenceRecord]:
        return dict(self._evidence)

    def assert_scope(self) -> None:
        """Re-check trusted identities at every tool boundary."""
        from models import Conversation, Customer, Tenant, WhatsAppConnection
        from utils.phone_utils import normalize_phone_compat

        TenantIsolationLayer.assert_active(self._tenant_context)
        tenant = (
            self._db.query(Tenant)
            .filter(Tenant.id == self.tenant_id, Tenant.is_active.is_(True))
            .one_or_none()
        )
        if tenant is None:
            raise TenantIsolationViolation("tenant scope is no longer active")
        conversation = (
            self._db.query(Conversation)
            .filter(
                Conversation.id == self.conversation_id,
                Conversation.tenant_id == self.tenant_id,
            )
            .one_or_none()
        )
        if conversation is None:
            raise TenantIsolationViolation("conversation scope is no longer valid")
        TenantIsolationLayer.assert_belongs(conversation, self._tenant_context)
        if (
            int(conversation.customer_id) if conversation.customer_id is not None else None
        ) != self.customer_id:
            raise TenantIsolationViolation("conversation customer scope is no longer valid")
        if self.customer_id is not None:
            customer = (
                self._db.query(Customer)
                .filter(
                    Customer.id == self.customer_id,
                    Customer.tenant_id == self.tenant_id,
                )
                .one_or_none()
            )
            if customer is None:
                raise TenantIsolationViolation("customer scope is no longer valid")
            TenantIsolationLayer.assert_belongs(customer, self._tenant_context)
            stored_phone = normalize_phone_compat(
                getattr(customer, "normalized_phone", None) or getattr(customer, "phone", None)
            )
            if stored_phone != self.normalized_customer_phone:
                raise TenantIsolationViolation("customer phone scope is no longer valid")
        connection = (
            self._db.query(WhatsAppConnection)
            .filter(
                WhatsAppConnection.id == int(self.connection_id),
                WhatsAppConnection.tenant_id == self.tenant_id,
            )
            .one_or_none()
        )
        if connection is None:
            raise TenantIsolationViolation("connection scope is no longer valid")
        TenantIsolationLayer.assert_belongs(connection, self._tenant_context)

    def authorize_products(self, product_ids: list[int]) -> None:
        self._allowed_product_ids.update(int(value) for value in product_ids if int(value) > 0)

    def require_authorized_product(self, product_id: int) -> None:
        if int(product_id) not in self._allowed_product_ids:
            raise TenantIsolationViolation("product_id_not_discovered_in_this_run")

    def register_evidence(self, records: list[EvidenceRecord]) -> None:
        for record in records:
            if record.ref in self._evidence and self._evidence[record.ref] != record:
                raise TenantIsolationViolation("evidence_ref_collision")
            self._evidence[record.ref] = record
