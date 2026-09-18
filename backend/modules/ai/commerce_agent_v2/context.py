"""Trusted, tenant-bound run context for Commerce Agent V2."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from modules.ai.commerce_agent_v2.internal_e2e_identity import (
    INTERNAL_E2E_CHANNEL,
    INTERNAL_E2E_CONNECTION_ID,
    InternalE2EAlias,
    internal_e2e_customer_identity,
    metadata_matches_internal_e2e_identity,
    normalize_internal_e2e_alias,
)
from modules.ai.commerce_agent_v2.output import EvidenceRecord
from modules.ai.security.tenant_isolation import (
    TenantContext,
    TenantIsolationLayer,
    TenantIsolationViolation,
)

# One turn may spend at most this many tenant knowledge lookups: the two
# deterministic ones (turn scope, product scope) plus a small margin for a
# model-initiated follow-up. A retrying model cannot multiply them further.
MAX_KNOWLEDGE_LOOKUPS_PER_TURN = 4


class CommerceContextError(RuntimeError):
    """Raised when the trusted database scope cannot be established."""


class CommerceCapabilities(BaseModel):
    """Read-only Commerce Agent capabilities. No mutation or outbound capability exists."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    read_catalog: bool = True
    read_merchant_knowledge: bool = True
    read_product_knowledge: bool = True
    read_orders: bool = True
    read_shipments: bool = True
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
    channel: Literal["whatsapp", "internal_e2e"] = "whatsapp"
    synthetic_customer_alias: InternalE2EAlias | None = None
    connection_id: str = Field(min_length=1)
    inbound_trace_id: str = Field(min_length=1, max_length=256)
    locale: str = "ar-SA"
    timezone: str = "Asia/Riyadh"
    capabilities: CommerceCapabilities = Field(default_factory=CommerceCapabilities)

    _db: Any = PrivateAttr()
    _tenant_context: TenantContext = PrivateAttr()
    _allowed_product_ids: set[int] = PrivateAttr(default_factory=set)
    _allowed_order_ids: set[int] = PrivateAttr(default_factory=set)
    _evidence: dict[str, EvidenceRecord] = PrivateAttr(default_factory=dict)
    _consecutive_catalog_misses: int = PrivateAttr(default=0)
    _run_user_input: str = PrivateAttr(default="")
    _merchant_knowledge_relevant: bool | None = PrivateAttr(default=None)
    _verified_customer_name: str = PrivateAttr(default="")
    _session_history_provenance: list[dict[str, Any]] = PrivateAttr(default_factory=list)
    _session_history_query_count: int = PrivateAttr(default=0)
    _grounding_retry_active: bool = PrivateAttr(default=False)
    _knowledge_lookups: list[dict[str, Any]] = PrivateAttr(default_factory=list)
    _knowledge_lookup_signatures: set[str] = PrivateAttr(default_factory=set)
    _knowledge_rows: dict[str, list[dict[str, Any]]] = PrivateAttr(default_factory=dict)
    _knowledge_sections_emitted: set[str] = PrivateAttr(default_factory=set)
    _authorized_product_titles: dict[int, str] = PrivateAttr(default_factory=dict)
    _authorized_product_aliases: dict[int, str] = PrivateAttr(default_factory=dict)

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
        channel: Literal["whatsapp", "internal_e2e"] = "whatsapp",
        synthetic_customer_alias: InternalE2EAlias | None = None,
    ) -> "CommerceAgentContext":
        from models import Conversation, Customer, Tenant, TenantSettings, WhatsAppConnection
        from utils.phone_utils import normalize_phone_compat

        internal_alias: InternalE2EAlias | None = None
        if channel == INTERNAL_E2E_CHANNEL:
            try:
                internal_alias = normalize_internal_e2e_alias(synthetic_customer_alias)
                canonical_phone = internal_e2e_customer_identity(tenant_id, internal_alias)
            except (TypeError, ValueError) as exc:
                raise CommerceContextError("internal_e2e_identity_invalid") from exc
            if str(normalized_customer_phone or "") != canonical_phone:
                raise CommerceContextError("internal_e2e_identity_mismatch")
            if str(connection_id or "") != INTERNAL_E2E_CONNECTION_ID:
                raise CommerceContextError("internal_e2e_connection_invalid")
        else:
            if synthetic_customer_alias is not None:
                raise CommerceContextError("synthetic_alias_forbidden_for_whatsapp")
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
        if internal_alias is not None and (
            str(getattr(conversation, "external_id", "") or "") != canonical_phone
            or not metadata_matches_internal_e2e_identity(
                getattr(conversation, "extra_metadata", None),
                tenant_id=tenant_ctx.tenant_id,
                alias=internal_alias,
            )
        ):
            raise CommerceContextError("conversation_not_internal_e2e_scoped")
        if internal_alias is not None and conversation.customer_id is not None:
            raise CommerceContextError("internal_e2e_customer_row_forbidden")

        conversation_customer_id = (
            int(conversation.customer_id) if conversation.customer_id is not None else None
        )
        if customer_id is not None and conversation_customer_id != int(customer_id):
            raise CommerceContextError("customer_not_in_conversation_scope")
        resolved_customer_id = (
            int(customer_id) if customer_id is not None else conversation_customer_id
        )

        verified_customer_name = ""
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
            verified_customer_name = str(getattr(customer, "name", "") or "").strip()

        connection = None
        if internal_alias is None:
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
            synthetic_customer_alias=internal_alias,
            connection_id=(
                INTERNAL_E2E_CONNECTION_ID if internal_alias is not None else str(connection.id)
            ),
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
        instance._verified_customer_name = verified_customer_name
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

    @property
    def authorized_product_ids(self) -> set[int]:
        """Trusted product ids discovered by tools during this run."""
        return set(self._allowed_product_ids)

    @property
    def authorized_order_ids(self) -> set[int]:
        """Trusted order ids discovered by tools during this run."""
        return set(self._allowed_order_ids)

    @property
    def session_history_provenance(self) -> list[dict[str, Any]]:
        """Non-content provenance recorded by the canonical Session adapter."""
        return [dict(item) for item in self._session_history_provenance]

    @property
    def session_history_query_count(self) -> int:
        return self._session_history_query_count

    def begin_session_history_query(self) -> None:
        self._session_history_query_count += 1

    def record_session_history_row(
        self,
        *,
        message_id: int,
        tenant_id: int,
        conversation_id: int,
        direction: str,
        metadata: dict[str, Any],
    ) -> None:
        """Record identity provenance only; message bodies never enter safety proof."""
        self._session_history_provenance.append(
            {
                "message_id": int(message_id),
                "tenant_id": int(tenant_id),
                "conversation_id": int(conversation_id),
                "direction": str(direction),
                "synthetic_customer_alias": metadata.get("synthetic_customer_alias"),
                "identity": metadata.get("identity"),
                "metadata_tenant_id": metadata.get("tenant_id"),
                "channel": metadata.get("channel"),
                "synthetic": metadata.get("synthetic"),
                "test_only": metadata.get("test_only"),
            }
        )

    def redact_unexposed_customer_identity(self, text: str) -> str:
        """Remove the stored customer name from model history until a name tool exists."""
        value = str(text or "")
        if self._verified_customer_name:
            value = value.replace(self._verified_customer_name, "[redacted-customer-name]")
        return value

    @property
    def consecutive_catalog_misses(self) -> int:
        """Run-local catalog miss count; never persisted into the Session."""
        return self._consecutive_catalog_misses

    def record_catalog_search_outcome(self, *, found: bool) -> int:
        """Track consecutive empty searches while allowing successful exploration."""
        self._consecutive_catalog_misses = (
            0 if found else self._consecutive_catalog_misses + 1
        )
        return self._consecutive_catalog_misses

    def bind_run_user_input(self, user_input: str) -> None:
        """Bind ephemeral turn text for evidence-aware tool availability.

        The value is private run state: it is neither persisted into the
        Conversation Session nor included in tracing or model-visible schemas.
        """
        self._run_user_input = str(user_input or "").strip()
        self._merchant_knowledge_relevant = None
        self._knowledge_lookups = []
        self._knowledge_lookup_signatures = set()
        self._knowledge_rows = {}
        self._knowledge_sections_emitted = set()

    @property
    def grounding_retry_active(self) -> bool:
        return self._grounding_retry_active

    def activate_grounding_retry(self) -> None:
        """Mark the single fail-closed re-grounding attempt for read tools."""
        self._grounding_retry_active = True

    @property
    def run_user_input(self) -> str:
        return self._run_user_input

    @property
    def merchant_knowledge_relevance(self) -> bool | None:
        return self._merchant_knowledge_relevant

    def cache_merchant_knowledge_relevance(self, relevant: bool) -> bool:
        self._merchant_knowledge_relevant = bool(relevant)
        return self._merchant_knowledge_relevant

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
        if self.channel == INTERNAL_E2E_CHANNEL:
            if self.synthetic_customer_alias is None:
                raise TenantIsolationViolation("internal E2E alias is no longer valid")
            expected_identity = internal_e2e_customer_identity(
                self.tenant_id, self.synthetic_customer_alias
            )
            if (
                self.connection_id != INTERNAL_E2E_CONNECTION_ID
                or self.normalized_customer_phone != expected_identity
                or str(getattr(conversation, "external_id", "") or "") != expected_identity
                or not metadata_matches_internal_e2e_identity(
                    getattr(conversation, "extra_metadata", None),
                    tenant_id=self.tenant_id,
                    alias=self.synthetic_customer_alias,
                )
            ):
                raise TenantIsolationViolation("internal E2E conversation scope is no longer valid")
            if conversation.customer_id is not None or self.customer_id is not None:
                raise TenantIsolationViolation("internal E2E customer row is forbidden")
        elif self.synthetic_customer_alias is not None:
            raise TenantIsolationViolation("synthetic alias is forbidden for WhatsApp")
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
                getattr(customer, "normalized_phone", None)
                or getattr(customer, "phone", None)
            )
            if stored_phone != self.normalized_customer_phone:
                raise TenantIsolationViolation("customer phone scope is no longer valid")
        if self.channel == "whatsapp":
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

    def authorize_products(
        self,
        product_ids: list[int],
        *,
        titles: dict[int, str] | None = None,
        aliases: dict[int, str] | None = None,
    ) -> None:
        self._allowed_product_ids.update(int(value) for value in product_ids if int(value) > 0)
        for product_id, title in (titles or {}).items():
            text = str(title or "").strip()
            if int(product_id) > 0 and text:
                self._authorized_product_titles[int(product_id)] = text
        for product_id, alias in (aliases or {}).items():
            text = str(alias or "").strip()
            if int(product_id) > 0 and text:
                self._authorized_product_aliases[int(product_id)] = text

    def authorized_product_anchors(
        self, product_ids: list[int]
    ) -> tuple[list[str], list[str]]:
        """Titles and aliases of products this run already authorized.

        Only a product the catalog tools grounded appears here, so a query
        anchored on it can never name a product the turn never saw.  Both the
        deterministic catalog lookup and the model's own knowledge tool read
        the anchor from here, so the two compose the same query and the second
        resolves to the first recorded attempt instead of re-querying.
        """
        titles: list[str] = []
        aliases: list[str] = []
        for product_id in product_ids:
            title = self._authorized_product_titles.get(int(product_id), "")
            if title and title not in titles:
                titles.append(title)
            alias = self._authorized_product_aliases.get(int(product_id), "")
            if alias and alias not in aliases:
                aliases.append(alias)
        return titles, aliases

    def require_authorized_product(self, product_id: int) -> None:
        if int(product_id) not in self._allowed_product_ids:
            raise TenantIsolationViolation("product_id_not_discovered_in_this_run")

    def authorize_orders(self, order_ids: list[int]) -> None:
        """Authorize only customer-scoped orders discovered in this trusted run."""
        self._allowed_order_ids.update(int(value) for value in order_ids if int(value) > 0)

    def require_authorized_order(self, order_id: int) -> None:
        if int(order_id) not in self._allowed_order_ids:
            raise TenantIsolationViolation("order_id_not_discovered_in_this_run")

    @property
    def knowledge_lookups(self) -> list[dict[str, Any]]:
        """Every tenant knowledge lookup this turn attempted, in order.

        The ledger is run-local and never persisted into the Session.  It
        records the attempt itself — including a lookup that returned nothing,
        timed out or failed — so "the merchant documents nothing about this"
        can be told apart from "nobody looked".
        """
        return [dict(item) for item in self._knowledge_lookups]

    @property
    def knowledge_lookup_attempted(self) -> bool:
        return bool(self._knowledge_lookups)

    def knowledge_lookup_seen(self, signature: str) -> bool:
        """Has an identical lookup already run this turn?"""
        return str(signature) in self._knowledge_lookup_signatures

    def record_knowledge_lookup(self, record: dict[str, Any], *, signature: str) -> dict[str, Any]:
        """Append one bounded lookup record; repeated signatures never duplicate.

        The cap keeps a retrying model from multiplying lookups: once the
        budget is spent the ledger keeps the outcomes it already has.
        """
        entry = dict(record)
        entry["sequence"] = len(self._knowledge_lookups) + 1
        if len(self._knowledge_lookups) >= MAX_KNOWLEDGE_LOOKUPS_PER_TURN:
            entry["status"] = "budget_exhausted"
            entry["sections"] = []
            entry["section_ids"] = []
            entry["hit_count"] = 0
        self._knowledge_lookup_signatures.add(str(signature))
        self._knowledge_lookups.append(entry)
        return entry

    def cache_knowledge_rows(self, signature: str, rows: list[dict[str, Any]]) -> None:
        """Keep one lookup's retrieved sections so a repeat never re-queries."""
        self._knowledge_rows[str(signature)] = [dict(row) for row in rows]

    def cached_knowledge_rows(self, signature: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self._knowledge_rows.get(str(signature), [])]

    def knowledge_budget_available(self) -> bool:
        return len(self._knowledge_lookups) < MAX_KNOWLEDGE_LOOKUPS_PER_TURN

    @staticmethod
    def knowledge_section_key(tenant_id: int, section_id: int) -> str:
        """Canonical identity of one merchant knowledge section.

        Tenant is part of the key so two tenants' sections can never collapse
        into one another, and the id is the stable identity rather than the
        body: two different sections that happen to share wording stay
        distinct.
        """
        return f"t{int(tenant_id)}:section:{int(section_id)}"

    def knowledge_section_emitted(self, section_id: int) -> bool:
        """True when this section already reached the model this turn."""
        return (
            self.knowledge_section_key(self.tenant_id, section_id)
            in self._knowledge_sections_emitted
        )

    def mark_knowledge_section_emitted(self, section_id: int) -> None:
        self._knowledge_sections_emitted.add(
            self.knowledge_section_key(self.tenant_id, section_id)
        )

    @property
    def knowledge_sections_emitted(self) -> list[str]:
        return sorted(self._knowledge_sections_emitted)

    def register_evidence(self, records: list[EvidenceRecord]) -> None:
        for record in records:
            if record.ref in self._evidence and self._evidence[record.ref] != record:
                raise TenantIsolationViolation("evidence_ref_collision")
            self._evidence[record.ref] = record
