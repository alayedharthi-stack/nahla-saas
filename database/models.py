from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()

class Tenant(Base):
    __tablename__ = 'tenants'
    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)
    domain = Column(String, unique=True, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    store_address = Column(Text, nullable=True)
    google_maps_link = Column(String, nullable=True)
    apple_maps_link = Column(String, nullable=True)
    same_day_delivery_enabled = Column(Boolean, default=False)
    pickup_enabled = Column(Boolean, default=True)
    branding = Column(JSONB, nullable=True)
    recommendation_controls = Column(JSONB, nullable=True)
    coupon_policy = Column(JSONB, nullable=True)

    # ── Billing provider fields ───────────────────────────────────────────────
    # billing_provider: 'stripe' (auto recurring) | 'hyperpay' (local manual)
    billing_provider        = Column(String, nullable=True, default='stripe')

    # Stripe fields (managed by Stripe webhooks — source of truth)
    stripe_customer_id      = Column(String, nullable=True)
    stripe_subscription_id  = Column(String, nullable=True)
    stripe_price_id         = Column(String, nullable=True)
    subscription_status     = Column(String, nullable=True)   # trialing | active | past_due | canceled
    trial_started_at        = Column(DateTime, nullable=True)
    trial_ends_at           = Column(DateTime, nullable=True)
    current_period_end      = Column(DateTime, nullable=True)

    # HyperPay fields (manual monthly payment flow)
    hyperpay_payment_id     = Column(String, nullable=True)
    billing_status          = Column(String, nullable=True)   # pending | paid | failed

    widget_settings = relationship('WidgetSetting', back_populates='tenant')
    whatsapp_numbers = relationship('WhatsAppNumber', back_populates='tenant')
    users = relationship('User', back_populates='tenant')
    products = relationship('Product', back_populates='tenant')
    orders = relationship('Order', back_populates='tenant')
    coupons = relationship('Coupon', back_populates='tenant')
    promotions = relationship('Promotion', back_populates='tenant')
    integrations = relationship('Integration', back_populates='tenant')
    sync_logs = relationship('SyncLog', back_populates='tenant')
    automation_rules = relationship('AutomationRule', back_populates='tenant')
    knowledge_policies = relationship('KnowledgePolicy', back_populates='tenant')
    delivery_zones = relationship('DeliveryZone', back_populates='tenant')
    shipping_fees = relationship('ShippingFee', back_populates='tenant')
    conversations = relationship('Conversation', back_populates='tenant')
    message_events = relationship('MessageEvent', back_populates='tenant')
    customer_addresses = relationship('CustomerAddress', back_populates='tenant')
    settings = relationship('TenantSettings', back_populates='tenant', uselist=False)
    campaigns = relationship('Campaign', back_populates='tenant')
    whatsapp_templates = relationship('WhatsAppTemplate', back_populates='tenant')
    smart_automations = relationship('SmartAutomation', back_populates='tenant')
    automation_events = relationship('AutomationEvent', back_populates='tenant')
    reorder_estimates = relationship('PredictiveReorderEstimate', back_populates='tenant')
    billing_plans = relationship('BillingPlan', back_populates='tenant')
    subscriptions = relationship('BillingSubscription', back_populates='tenant')
    payments = relationship('BillingPayment', back_populates='tenant')
    invoices = relationship('BillingInvoice', back_populates='tenant')
    app_installs = relationship('AppInstall', back_populates='tenant')
    app_payments = relationship('AppPayment', back_populates='tenant')

    # Goal A — WhatsApp Embedded Signup
    whatsapp_connection = relationship('WhatsAppConnection', back_populates='tenant', uselist=False)
    whatsapp_usages      = relationship('WhatsAppUsage',          back_populates='tenant')

    # Goal B — Store Knowledge Sync
    store_sync_jobs   = relationship('StoreSyncJob', back_populates='tenant')
    store_knowledge   = relationship('StoreKnowledgeSnapshot', back_populates='tenant', uselist=False)

class TenantSettings(Base):
    __tablename__ = 'tenant_settings'
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False, unique=True)
    tenant = relationship('Tenant', back_populates='settings', uselist=False)
    show_nahla_branding = Column(Boolean, default=True, nullable=False)
    branding_text = Column(String, default='🐝 Powered by Nahla', nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    extra_metadata = Column('metadata', JSONB, nullable=True)   # DB column is 'metadata' (migration 0001)
    # Structured settings groups (added migration 0004)
    whatsapp_settings = Column(JSONB, nullable=True)
    ai_settings = Column(JSONB, nullable=True)
    store_settings = Column(JSONB, nullable=True)
    notification_settings = Column(JSONB, nullable=True)

class User(Base):
    __tablename__ = 'users'
    id            = Column(Integer, primary_key=True)
    username      = Column(String, unique=True, nullable=False)
    email         = Column(String, unique=True, nullable=False)
    password_hash = Column(String, nullable=True)
    role          = Column(String, nullable=False, default='merchant', server_default='merchant')
    is_active     = Column(Boolean, nullable=False, default=True, server_default='true')
    created_at    = Column(DateTime, nullable=True)
    tenant_id     = Column(Integer, ForeignKey('tenants.id'), nullable=False)
    tenant        = relationship('Tenant', back_populates='users')

class WhatsAppNumber(Base):
    __tablename__ = 'whatsapp_numbers'
    id = Column(Integer, primary_key=True)
    number = Column(String, unique=True, nullable=False)
    config = Column(JSONB, nullable=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False)
    tenant = relationship('Tenant', back_populates='whatsapp_numbers')

class Product(Base):
    __tablename__ = 'products'
    id = Column(Integer, primary_key=True)
    external_id = Column(String, index=True, nullable=True)
    sku = Column(String, nullable=True)
    title = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    price = Column(String, nullable=True)
    # Real columns (added in migration 0025) for back-in-stock detection.
    # The store_sync upsert populates these alongside the JSONB blob so
    # transition detection can compare old-vs-new at the column level
    # without parsing JSON.
    stock_quantity = Column(Integer, nullable=True)
    in_stock = Column(Boolean, default=True, nullable=False)
    extra_metadata = Column('metadata', JSONB, nullable=True)
    recommendation_tags = Column(JSONB, nullable=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False)
    tenant = relationship('Tenant', back_populates='products')

class Order(Base):
    __tablename__ = 'orders'
    id = Column(Integer, primary_key=True)
    # Platform's internal id (e.g. Salla `id`). Stable, used for upserts.
    external_id = Column(String, index=True, nullable=True)
    # Human-visible order number from the platform (e.g. Salla `reference_id`
    # 1585297702 → shown to merchant as "#1585297702"). Falls back to
    # external_id when the platform doesn't expose a separate number.
    external_order_number = Column(String, index=True, nullable=True)
    status = Column(String, nullable=False)
    total = Column(String, nullable=True)
    # Denormalised customer name so the dashboard cell is never blank even if
    # `customer_info` JSON is empty (legacy rows / minimal webhooks).
    customer_name = Column(String, nullable=True)
    customer_info = Column(JSONB, nullable=True)
    line_items = Column(JSONB, nullable=True)
    checkout_url = Column(String, nullable=True)
    is_abandoned = Column(Boolean, default=False)
    # Origin of the order so the merchant can tell where it came from:
    #   "salla" | "zid" | "shopify" | "whatsapp" | "manual"
    source = Column(String, nullable=True, index=True)
    extra_metadata = Column('metadata', JSONB, nullable=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False)
    tenant = relationship('Tenant', back_populates='orders')

class Coupon(Base):
    __tablename__ = 'coupons'
    __table_args__ = (
        UniqueConstraint('tenant_id', 'code', name='uq_coupons_tenant_code'),
    )
    id = Column(Integer, primary_key=True)
    code = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    discount_type = Column(String, nullable=True)
    discount_value = Column(String, nullable=True)
    extra_metadata = Column('metadata', JSONB, nullable=True)
    expires_at = Column(DateTime, nullable=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False)
    tenant = relationship('Tenant', back_populates='coupons')
    rules = relationship('CouponRule', back_populates='coupon')

class CouponRule(Base):
    __tablename__ = 'coupon_rules'
    id = Column(Integer, primary_key=True)
    rule_type = Column(String, nullable=False)
    rule_config = Column(JSONB, nullable=True)
    coupon_id = Column(Integer, ForeignKey('coupons.id'), nullable=False)
    coupon = relationship('Coupon', back_populates='rules')


class Promotion(Base):
    """
    A *Promotion* is a reusable discount rule the merchant manages from the
    "العروض" page. Mirrors the Shopify/Magento split between coupon codes
    (one-off, per-customer, code in hand) and promotional rules (applied
    automatically when conditions match).

    Promotions in Nahla are the source of truth for the *terms* of an offer
    (type, value, conditions, validity window, audience). When an automation
    fires for a customer, `services.promotion_engine.materialise_for_customer`
    reads the promotion and issues a personal `Coupon` row carrying those
    terms — that way the same engine works across Salla / Zid / Shopify
    without depending on each platform's promotional API surface.

    Type catalogue (kept open-ended via String + validation in the service):
        percentage           — % off cart subtotal
        fixed                — flat amount off
        free_shipping        — zero shipping (encoded as 100% off shipping
                               line in checkout instructions)
        threshold_discount   — percentage/fixed once cart >= min_order_amount
        buy_x_get_y          — buy `x_quantity` of x, get `y_quantity` of y free
                               (materialised as a stacked coupon at issue time)
    """
    __tablename__ = 'promotions'
    __table_args__ = (
        Index('ix_promotions_tenant_status', 'tenant_id', 'status'),
        Index('ix_promotions_tenant_type', 'tenant_id', 'promotion_type'),
    )

    id              = Column(Integer, primary_key=True)
    tenant_id       = Column(Integer, ForeignKey('tenants.id'), nullable=False, index=True)

    name            = Column(String, nullable=False)
    description     = Column(Text, nullable=True)
    promotion_type  = Column(String, nullable=False)
    discount_value  = Column(Numeric(10, 2), nullable=True)

    conditions      = Column(JSONB, nullable=True)

    starts_at       = Column(DateTime, nullable=True)
    ends_at         = Column(DateTime, nullable=True)

    status          = Column(String, nullable=False, default='draft', index=True)

    usage_count     = Column(Integer, nullable=False, default=0)
    usage_limit     = Column(Integer, nullable=True)

    extra_metadata  = Column('metadata', JSONB, nullable=True)
    created_at      = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at      = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    tenant = relationship('Tenant', back_populates='promotions')

class OfferDecisionLedger(Base):
    """
    One row per *decision* the OfferDecisionService makes.

    A "decision" is the act of choosing — for a given customer at a given
    moment on a given trigger surface (campaign / chat / segment-change) —
    whether to issue a discount, and if so which one (promotion vs coupon)
    with what value and validity. The decision is then materialised into
    a `Coupon` (or no coupon at all) by the existing primitives.

    This table is the **closing of the loop**: every decision is captured
    with its inputs (`signals_snapshot`), its output (`chosen_*`), the
    explainability trail (`reason_codes`), and — once an order arrives
    that redeems the coupon — the realised outcome (`redeemed_at`,
    `order_id`, `revenue_amount`, `attributed`).

    Bandit-ready by design: `policy_version` and `experiment_arm` are
    populated from day one so a future contextual-bandit policy can be
    added behind the same `OfferDecisionService.decide(...)` interface
    without a schema migration. In v1 (deterministic policy) every row
    has `policy_version='v1.0-deterministic'` and `experiment_arm=None`.
    """
    __tablename__ = 'offer_decisions'
    __table_args__ = (
        UniqueConstraint('decision_id', name='uq_offer_decisions_decision_id'),
        Index('ix_offer_decisions_tenant_created', 'tenant_id', 'created_at'),
        Index('ix_offer_decisions_tenant_surface', 'tenant_id', 'surface'),
        Index('ix_offer_decisions_tenant_chosen', 'tenant_id', 'chosen_source'),
        Index('ix_offer_decisions_tenant_attributed', 'tenant_id', 'attributed'),
    )

    id              = Column(Integer, primary_key=True)
    tenant_id       = Column(Integer, ForeignKey('tenants.id'), nullable=False, index=True)

    # UUID string — generated at decision time, stamped into the resulting
    # `Coupon.extra_metadata.decision_id` so attribution can join on it.
    decision_id     = Column(String, nullable=False, index=True)

    # Where was the decision made?
    #   automation       — automation_engine._resolve_auto_coupon
    #   chat             — orchestrator._execute_suggest_coupon
    #   segment_change   — customer_intelligence event-driven
    surface         = Column(String, nullable=False)

    # Optional foreign-key-ish ids — kept as plain integers (no FK) so a
    # row deleted upstream doesn't cascade-delete decision history.
    automation_id   = Column(Integer, nullable=True, index=True)
    event_id        = Column(Integer, nullable=True)
    customer_id     = Column(Integer, nullable=True, index=True)

    # Snapshot of the signals fed to the policy at decision time.
    # Lets us re-run the decision offline or train a bandit later without
    # racing against mutated CustomerProfile rows.
    signals_snapshot = Column(JSONB, nullable=True)

    # ── Decision output ──────────────────────────────────────────────
    chosen_source       = Column(String, nullable=False)        # promotion | coupon | none
    chosen_promotion_id = Column(Integer, nullable=True)
    chosen_coupon_id    = Column(Integer, nullable=True)        # filled after issuance

    discount_type   = Column(String, nullable=True)             # percentage | fixed | free_shipping
    discount_value  = Column(Numeric(10, 2), nullable=True)
    validity_days   = Column(Integer, nullable=True)

    # Ordered list of short codes explaining *why* the policy made this
    # choice — e.g. ["legacy_step_override", "merchant_rule_applied",
    # "capped_by_max_discount"]. Surfaced in observability.
    reason_codes    = Column(JSONB, nullable=True)

    # Bandit-ready columns — never null in v1 (advisory string only).
    policy_version  = Column(String, nullable=False, default='v1.0-deterministic')
    experiment_arm  = Column(String, nullable=True)

    # ── Attribution (filled by OfferAttributionService) ───────────────
    redeemed_at     = Column(DateTime, nullable=True)
    order_id        = Column(Integer, nullable=True)
    revenue_amount  = Column(Numeric(12, 2), nullable=True)
    attributed      = Column(Boolean, nullable=False, default=False)

    created_at      = Column(DateTime, default=datetime.utcnow, nullable=False)


class Integration(Base):
    __tablename__ = 'integrations'
    __table_args__ = (
        UniqueConstraint('provider', 'external_store_id', name='uq_integrations_provider_external_store_id'),
    )
    id = Column(Integer, primary_key=True)
    provider = Column(String, nullable=False)
    external_store_id = Column(String, nullable=True)
    config = Column(JSONB, nullable=True)
    enabled = Column(Boolean, default=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False)
    tenant = relationship('Tenant', back_populates='integrations')

class SyncLog(Base):
    __tablename__ = 'sync_logs'
    id = Column(Integer, primary_key=True)
    resource_type = Column(String, nullable=False)
    external_id = Column(String, nullable=True)
    status = Column(String, nullable=False)
    message = Column(Text, nullable=True)
    extra_metadata = Column('metadata', JSONB, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False)
    tenant = relationship('Tenant', back_populates='sync_logs')

class AutomationRule(Base):
    __tablename__ = 'automation_rules'
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    trigger_type = Column(String, nullable=False)
    trigger_config = Column(JSONB, nullable=True)
    action_config = Column(JSONB, nullable=True)
    is_active = Column(Boolean, default=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False)
    tenant = relationship('Tenant', back_populates='automation_rules')

class DeliveryZone(Base):
    __tablename__ = 'delivery_zones'
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    zone_type = Column(String, nullable=True)
    geojson = Column(JSONB, nullable=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False)
    tenant = relationship('Tenant', back_populates='delivery_zones')

class ShippingFee(Base):
    __tablename__ = 'shipping_fees'
    id = Column(Integer, primary_key=True)
    city = Column(String, nullable=True)
    zone_name = Column(String, nullable=True)
    fee_amount = Column(String, nullable=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False)
    tenant = relationship('Tenant', back_populates='shipping_fees')

class Customer(Base):
    __tablename__ = 'customers'
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=True)
    email = Column(String, nullable=True)
    phone = Column(String, nullable=True)
    extra_metadata = Column('metadata', JSONB, nullable=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False)
    tenant = relationship('Tenant')
    addresses = relationship('CustomerAddress', back_populates='customer')

class CustomerAddress(Base):
    __tablename__ = 'customer_addresses'
    id = Column(Integer, primary_key=True)
    raw_address = Column(Text, nullable=True)
    saudi_national_address = Column(Text, nullable=True)
    google_maps_link = Column(String, nullable=True)
    apple_maps_link = Column(String, nullable=True)
    whatsapp_location = Column(JSONB, nullable=True)
    lat = Column(String, nullable=True)
    lng = Column(String, nullable=True)
    city = Column(String, nullable=True)
    district = Column(String, nullable=True)
    address_text = Column(Text, nullable=True)
    address_type = Column(String, nullable=True)
    customer_id = Column(Integer, ForeignKey('customers.id'), nullable=True)
    order_id = Column(Integer, ForeignKey('orders.id'), nullable=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False)
    tenant = relationship('Tenant', back_populates='customer_addresses')
    customer = relationship('Customer', back_populates='addresses')

class KnowledgePolicy(Base):
    __tablename__ = 'knowledge_policies'
    id = Column(Integer, primary_key=True)
    allowed_categories = Column(JSONB, nullable=True)
    blocked_categories = Column(JSONB, nullable=True)
    escalation_rules = Column(JSONB, nullable=True)
    owner_override = Column(JSONB, nullable=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False)
    tenant = relationship('Tenant', back_populates='knowledge_policies')

class Conversation(Base):
    __tablename__ = 'conversations'
    id = Column(Integer, primary_key=True)
    external_id = Column(String, nullable=True, index=True)
    status = Column(String, nullable=False)
    customer_id = Column(Integer, ForeignKey('customers.id'), nullable=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False)
    tenant = relationship('Tenant', back_populates='conversations')
    customer = relationship('Customer')
    is_human_handoff = Column(Boolean, default=False)
    is_urgent = Column(Boolean, default=False)
    paused_by_human = Column(Boolean, default=False)
    extra_metadata = Column('metadata', JSONB, nullable=True)

class MessageEvent(Base):
    __tablename__ = 'message_events'
    id = Column(Integer, primary_key=True)
    conversation_id = Column(Integer, ForeignKey('conversations.id'), nullable=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False)
    tenant = relationship('Tenant', back_populates='message_events')
    direction = Column(String, nullable=True)
    body = Column(Text, nullable=True)
    event_type = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    extra_metadata = Column('metadata', JSONB, nullable=True)

class WidgetSetting(Base):
    __tablename__ = 'widget_settings'
    id = Column(Integer, primary_key=True)
    bot_name = Column(String, nullable=True)
    logo_url = Column(String, nullable=True)
    color = Column(String, nullable=True)
    welcome_text = Column(Text, nullable=True)
    show_nahla_branding = Column(Boolean, default=True, nullable=True)
    branding_text = Column(String, default='🐝 Powered by Nahla', nullable=True)
    options = Column(JSONB, nullable=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False)
    tenant = relationship('Tenant', back_populates='widget_settings')

class Developer(Base):
    __tablename__ = 'developers'
    id = Column(Integer, primary_key=True)
    username = Column(String, unique=True, nullable=False)
    email = Column(String, unique=True, nullable=False)
    company_name = Column(String, nullable=True)
    website = Column(String, nullable=True)
    extra_metadata = Column('metadata', JSONB, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    apps = relationship('App', back_populates='developer')

class App(Base):
    __tablename__ = 'apps'
    id = Column(Integer, primary_key=True)
    developer_id = Column(Integer, ForeignKey('developers.id'), nullable=False)
    developer = relationship('Developer', back_populates='apps')
    name = Column(String, nullable=False)
    slug = Column(String, unique=True, nullable=False)
    description = Column(Text, nullable=True)
    price_sar = Column(Integer, default=0)
    billing_model = Column(String, default='one_time')
    commission_rate = Column(Float, default=0.20)
    permissions = Column(JSONB, nullable=True)
    categories = Column(JSONB, nullable=True)
    icon_url = Column(String, nullable=True)
    extra_metadata = Column('metadata', JSONB, nullable=True)
    is_published = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    installs = relationship('AppInstall', back_populates='app')
    payments = relationship('AppPayment', back_populates='app')

class AppInstall(Base):
    __tablename__ = 'app_installs'
    id = Column(Integer, primary_key=True)
    app_id = Column(Integer, ForeignKey('apps.id'), nullable=False)
    app = relationship('App', back_populates='installs')
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False)
    tenant = relationship('Tenant', back_populates='app_installs')
    permissions = Column(JSONB, nullable=True)
    config = Column(JSONB, nullable=True)
    status = Column(String, default='installed')
    enabled = Column(Boolean, default=True)
    installed_at = Column(DateTime, default=datetime.utcnow)
    extra_metadata = Column('metadata', JSONB, nullable=True)

class AppPayment(Base):
    __tablename__ = 'app_payments'
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False)
    tenant = relationship('Tenant', back_populates='app_payments')
    app_id = Column(Integer, ForeignKey('apps.id'), nullable=False)
    app = relationship('App', back_populates='payments')
    developer_id = Column(Integer, ForeignKey('developers.id'), nullable=False)
    developer = relationship('Developer')
    amount_sar = Column(Integer, nullable=False)
    currency = Column(String, default='SAR')
    commission_rate = Column(Float, default=0.20)
    commission_amount_sar = Column(Integer, default=0)
    gateway = Column(String, nullable=True)
    status = Column(String, default='pending')
    transaction_reference = Column(String, nullable=True)
    paid_at = Column(DateTime, nullable=True)
    extra_metadata = Column('metadata', JSONB, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class BillingPlan(Base):
    __tablename__ = 'billing_plans'
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=True)
    tenant = relationship('Tenant', back_populates='billing_plans')
    slug = Column(String, unique=True, nullable=False)
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    currency = Column(String, default='SAR')
    price_sar = Column(Integer, nullable=False)
    billing_cycle = Column(String, nullable=False)
    is_enterprise = Column(Boolean, default=False)
    features = Column(JSONB, nullable=True)
    limits = Column(JSONB, nullable=True)
    extra_metadata = Column('metadata', JSONB, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class BillingSubscription(Base):
    __tablename__ = 'billing_subscriptions'
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False)
    tenant = relationship('Tenant', back_populates='subscriptions')
    plan_id = Column(Integer, ForeignKey('billing_plans.id'), nullable=False)
    plan = relationship('BillingPlan')
    status = Column(String, default='active')
    started_at = Column(DateTime, default=datetime.utcnow)
    ends_at = Column(DateTime, nullable=True)
    trial_ends_at = Column(DateTime, nullable=True)
    auto_renew = Column(Boolean, default=True)
    extra_metadata = Column('metadata', JSONB, nullable=True)

class BillingPayment(Base):
    __tablename__ = 'billing_payments'
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False)
    tenant = relationship('Tenant', back_populates='payments')
    subscription_id = Column(Integer, ForeignKey('billing_subscriptions.id'), nullable=True)
    subscription = relationship('BillingSubscription')
    amount_sar = Column(Integer, nullable=False)
    currency = Column(String, default='SAR')
    gateway = Column(String, nullable=False)
    transaction_reference = Column(String, nullable=True)
    status = Column(String, nullable=False)
    paid_at = Column(DateTime, nullable=True)
    extra_metadata = Column('metadata', JSONB, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class BillingInvoice(Base):
    __tablename__ = 'billing_invoices'
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False)
    tenant = relationship('Tenant', back_populates='invoices')
    subscription_id = Column(Integer, ForeignKey('billing_subscriptions.id'), nullable=True)
    subscription = relationship('BillingSubscription')
    amount_due_sar = Column(Integer, nullable=False)
    amount_paid_sar = Column(Integer, default=0)
    currency = Column(String, default='SAR')
    status = Column(String, nullable=False)
    issued_date = Column(DateTime, default=datetime.utcnow)
    due_date = Column(DateTime, nullable=True)
    line_items = Column(JSONB, nullable=True)
    extra_metadata = Column('metadata', JSONB, nullable=True)

class AuditLog(Base):
    __tablename__ = 'audit_logs'
    id = Column(Integer, primary_key=True)
    category = Column(String, nullable=False)
    resource_type = Column(String, nullable=True)
    resource_id = Column(String, nullable=True)
    action = Column(String, nullable=True)
    details = Column(JSONB, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False)
    tenant = relationship('Tenant')


# ── Customer Intelligence Layer ───────────────────────────────────────────────

class CustomerProfile(Base):
    """Aggregated lifetime profile for a customer — updated after each interaction."""
    __tablename__ = 'customer_profiles'
    id = Column(Integer, primary_key=True)
    customer_id = Column(Integer, ForeignKey('customers.id'), nullable=False, unique=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False)
    # Engagement
    total_orders = Column(Integer, default=0)
    total_spend_sar = Column(Float, default=0.0)
    average_order_value_sar = Column(Float, default=0.0)
    max_single_order_sar = Column(Float, default=0.0)
    first_seen_at = Column(DateTime, nullable=True)
    last_seen_at = Column(DateTime, nullable=True)
    first_order_at = Column(DateTime, nullable=True)
    last_order_at = Column(DateTime, nullable=True)
    # Segmentation
    segment = Column(String, default='new')   # new | active | at_risk | churned | vip
    customer_status = Column(String, default='lead')
    rfm_recency_score = Column(Integer, default=0)
    rfm_frequency_score = Column(Integer, default=0)
    rfm_monetary_score = Column(Integer, default=0)
    rfm_total_score = Column(Integer, default=0)
    rfm_code = Column(String, nullable=True)
    rfm_segment = Column(String, default='lead')
    metrics_computed_at = Column(DateTime, nullable=True)
    last_recomputed_reason = Column(String, nullable=True)
    churn_risk_score = Column(Float, default=0.0)   # 0.0 – 1.0
    lifetime_value_score = Column(Float, default=0.0)
    is_returning = Column(Boolean, default=False)
    # Communication
    preferred_language = Column(String, default='ar')   # ar | en | mixed
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    customer = relationship('Customer')
    tenant = relationship('Tenant')


class CustomerPreferences(Base):
    """Inferred and explicit preferences — updated by the AI after each conversation."""
    __tablename__ = 'customer_preferences'
    id = Column(Integer, primary_key=True)
    customer_id = Column(Integer, ForeignKey('customers.id'), nullable=False, unique=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False)
    preferred_categories = Column(JSONB, nullable=True)    # ["electronics", "fashion"]
    preferred_brands = Column(JSONB, nullable=True)
    price_range_min_sar = Column(Float, nullable=True)
    price_range_max_sar = Column(Float, nullable=True)
    preferred_payment_method = Column(String, nullable=True)  # cod | card | stc_pay | mada
    preferred_delivery_type = Column(String, nullable=True)   # delivery | pickup
    communication_style = Column(String, default='neutral')   # formal | casual | brief | neutral
    language = Column(String, default='ar')
    inferred_notes = Column(JSONB, nullable=True)   # freeform AI observations
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    customer = relationship('Customer')
    tenant = relationship('Tenant')


class ProductAffinity(Base):
    """Per-customer affinity score for each product — drives recommendations."""
    __tablename__ = 'product_affinities'
    id = Column(Integer, primary_key=True)
    customer_id = Column(Integer, ForeignKey('customers.id'), nullable=False)
    product_id = Column(Integer, ForeignKey('products.id'), nullable=False)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False)
    view_count = Column(Integer, default=0)
    purchase_count = Column(Integer, default=0)
    recommendation_count = Column(Integer, default=0)
    affinity_score = Column(Float, default=0.0)   # 0.0 – 1.0, higher = recommend first
    last_recommended_at = Column(DateTime, nullable=True)
    last_purchased_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    customer = relationship('Customer')
    product = relationship('Product')
    tenant = relationship('Tenant')


class PriceSensitivityScore(Base):
    """How price-sensitive a customer is — drives coupon offer strategy."""
    __tablename__ = 'price_sensitivity_scores'
    id = Column(Integer, primary_key=True)
    customer_id = Column(Integer, ForeignKey('customers.id'), nullable=False, unique=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False)
    # 0.0 = buys at full price happily, 1.0 = only buys with heavy discount
    score = Column(Float, default=0.5)
    avg_order_value_sar = Column(Float, default=0.0)
    max_observed_spend_sar = Column(Float, default=0.0)
    coupon_usage_count = Column(Integer, default=0)
    coupon_usage_rate = Column(Float, default=0.0)     # coupons_used / total_orders
    discount_response_rate = Column(Float, default=0.0) # orders_after_offer / total_offers
    # Suggested discount bucket for this customer
    recommended_discount_pct = Column(Integer, default=0)  # 0 | 5 | 10 | 15 | 20
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    customer = relationship('Customer')
    tenant = relationship('Tenant')


class ConversationHistorySummary(Base):
    """Rolling AI-written summary of a customer's conversation history with this store."""
    __tablename__ = 'conversation_history_summaries'
    id = Column(Integer, primary_key=True)
    customer_id = Column(Integer, ForeignKey('customers.id'), nullable=False, unique=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False)
    summary_text = Column(Text, nullable=True)           # AI-written prose summary
    topics_discussed = Column(JSONB, nullable=True)      # ["delivery", "returns", "products"]
    products_mentioned = Column(JSONB, nullable=True)    # list of product IDs
    coupons_used = Column(JSONB, nullable=True)          # list of coupon codes
    last_intent = Column(String, nullable=True)          # browse | order | complaint | inquiry
    sentiment = Column(String, default='neutral')        # positive | neutral | negative | frustrated
    escalation_count = Column(Integer, default=0)
    last_escalation_reason = Column(Text, nullable=True)
    total_conversations = Column(Integer, default=0)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    customer = relationship('Customer')
    tenant = relationship('Tenant')


class CommercePermissions(Base):
    """
    Per-tenant commerce permission flags.
    Controls what AI actions the orchestrator is allowed to execute for this store.
    Hardcoded-forbidden actions (delete_*, cancel_paid_*) are enforced in code —
    no DB column exists for them, making them impossible to enable.
    """
    __tablename__ = 'commerce_permissions'
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False, unique=True)
    # Allowed by default
    can_create_orders = Column(Boolean, default=True, nullable=False)
    can_create_checkout_links = Column(Boolean, default=True, nullable=False)
    can_send_payment_links = Column(Boolean, default=True, nullable=False)
    can_apply_coupons = Column(Boolean, default=True, nullable=False)
    can_auto_generate_coupons = Column(Boolean, default=True, nullable=False)
    # Opt-in only (default False — must be explicitly enabled)
    can_cancel_orders = Column(Boolean, default=False, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    tenant = relationship('Tenant')


class WhatsAppTemplate(Base):
    """
    WhatsApp message template — created in Nahla and submitted to Meta for approval.
    Mirrors the Meta template object; status is kept in sync via webhook or manual sync.
    """
    __tablename__ = 'whatsapp_templates'
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False)
    tenant = relationship('Tenant', back_populates='whatsapp_templates')
    # Meta identifiers
    meta_template_id = Column(String, nullable=True)        # ID assigned by Meta after submission
    name = Column(String, nullable=False)                    # snake_case name, unique per WABA
    language = Column(String, default='ar', nullable=False)  # ar | en | ...
    category = Column(String, nullable=False)                # MARKETING | UTILITY | AUTHENTICATION
    status = Column(String, default='PENDING', nullable=False)  # DRAFT | APPROVED | PENDING | REJECTED | DISABLED
    rejection_reason = Column(Text, nullable=True)
    # Full components payload (HEADER, BODY, FOOTER, BUTTONS)
    components = Column(JSONB, nullable=True)
    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    synced_at = Column(DateTime, nullable=True)    # last time status was confirmed from Meta
    # AI generation & lifecycle (migration 0009)
    source = Column(String, default='merchant', nullable=True)   # merchant | ai_generated
    objective = Column(String, nullable=True)                    # abandoned_cart | reorder | winback | ...
    usage_count = Column(Integer, default=0, nullable=True)
    last_used_at = Column(DateTime, nullable=True)
    health_score = Column(Float, nullable=True)                  # 0.0–1.0
    recommendation_state = Column(String, nullable=True)         # none | pending | accepted | dismissed
    recommendation_note = Column(Text, nullable=True)
    ai_generation_metadata = Column(JSONB, nullable=True)        # prompt, model, generation params


class Campaign(Base):
    """WhatsApp campaign — must be based on a Meta-approved template."""
    __tablename__ = 'campaigns'
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False)
    tenant = relationship('Tenant', back_populates='campaigns')
    name = Column(String, nullable=False)
    campaign_type = Column(String, nullable=False)  # abandoned_cart | promotion | vip | new_arrivals | broadcast
    status = Column(String, default='draft', nullable=False)  # draft | scheduled | active | completed | paused
    # Template info (from Meta WhatsApp Cloud API)
    template_id = Column(String, nullable=True)
    template_name = Column(String, nullable=True)
    template_language = Column(String, default='ar', nullable=True)
    template_category = Column(String, nullable=True)  # MARKETING | UTILITY
    template_body = Column(Text, nullable=True)        # rendered preview body
    template_variables = Column(JSONB, nullable=True)  # {"1": "اسم العميل", "2": "رابط العربة"}
    # Audience
    audience_type = Column(String, nullable=True)      # all | vip | abandoned_cart | inactive
    audience_count = Column(Integer, default=0)
    # Schedule
    schedule_type = Column(String, default='immediate', nullable=True)  # immediate | scheduled | delayed
    schedule_time = Column(DateTime, nullable=True)
    delay_minutes = Column(Integer, nullable=True)
    # Optional coupon
    coupon_code = Column(String, nullable=True)
    # Metrics
    sent_count = Column(Integer, default=0)
    delivered_count = Column(Integer, default=0)
    read_count = Column(Integer, default=0)
    clicked_count = Column(Integer, default=0)
    converted_count = Column(Integer, default=0)
    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    launched_at = Column(DateTime, nullable=True)


class SmartAutomation(Base):
    """
    A toggleable marketing automation — triggered by an event and sends
    a WhatsApp template message to the matched audience.
    """
    __tablename__ = 'smart_automations'
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False)
    tenant = relationship('Tenant', back_populates='smart_automations')
    automation_type = Column(String, nullable=False)
    # abandoned_cart | predictive_reorder | customer_winback |
    # vip_upgrade | new_product_alert | back_in_stock |
    # unpaid_order_reminder | seasonal_offer | salary_payday_offer
    name = Column(String, nullable=False)
    enabled = Column(Boolean, default=False, nullable=False)
    # Which "engine" this automation belongs to in the merchant dashboard:
    # recovery | growth | experience | intelligence
    # Drives grouping in /automations/engines/summary and the SmartAutomations UI.
    engine = Column(String, nullable=False, default='recovery', index=True)
    config = Column(JSONB, nullable=True)          # delays, conditions, coupon_code, etc.
    template_id = Column(Integer, ForeignKey('whatsapp_templates.id'), nullable=True)
    template = relationship('WhatsAppTemplate')
    # Event-driven engine: which AutomationEvent.event_type triggers this automation
    trigger_event = Column(String, nullable=True)
    # Aggregate stats
    stats_triggered = Column(Integer, default=0, nullable=False)
    stats_sent = Column(Integer, default=0, nullable=False)
    stats_converted = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AutomationEvent(Base):
    """
    An event emitted by the system (cart abandoned, order placed, etc.)
    that automations listen to and act on.
    """
    __tablename__ = 'automation_events'
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False)
    tenant = relationship('Tenant', back_populates='automation_events')
    event_type = Column(String, nullable=False)
    # cart_abandoned | order_completed | product_back_in_stock |
    # customer_inactive | predictive_reorder_due | vip_customer_upgrade | product_created
    customer_id = Column(Integer, ForeignKey('customers.id'), nullable=True)
    customer = relationship('Customer')
    payload = Column(JSONB, nullable=True)    # event-specific data
    processed = Column(Boolean, default=False, nullable=False)
    automation_id = Column(Integer, ForeignKey('smart_automations.id'), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class AutomationExecution(Base):
    """
    Records every attempt the automation engine makes to execute a SmartAutomation
    in response to an AutomationEvent.  Provides idempotency and an audit trail.
    """
    __tablename__ = 'automation_executions'
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False)
    automation_id = Column(Integer, ForeignKey('smart_automations.id'), nullable=False)
    event_id = Column(Integer, ForeignKey('automation_events.id'), nullable=False)
    customer_id = Column(Integer, ForeignKey('customers.id'), nullable=True)
    # sent | skipped | failed
    status = Column(String, nullable=False)
    # Reason for skipping or failing
    skip_reason = Column(String, nullable=True)
    # What was actually sent: {template_name, to, vars, response}
    action_taken = Column(JSONB, nullable=True)
    error_message = Column(Text, nullable=True)
    executed_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class PredictiveReorderEstimate(Base):
    """
    Predicted reorder date for a customer + product combination,
    computed from purchase history and product consumption rates.
    """
    __tablename__ = 'predictive_reorder_estimates'
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False)
    tenant = relationship('Tenant', back_populates='reorder_estimates')
    customer_id = Column(Integer, ForeignKey('customers.id'), nullable=False)
    customer = relationship('Customer')
    product_id = Column(Integer, ForeignKey('products.id'), nullable=False)
    product = relationship('Product')
    quantity_purchased = Column(Float, nullable=True)    # e.g. 500 (grams) or 1 (unit)
    purchase_date = Column(DateTime, nullable=True)
    consumption_rate_days = Column(Integer, nullable=True)  # average days to consume
    predicted_reorder_date = Column(DateTime, nullable=True)
    notified = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ProductInterest(Base):
    """
    "Notify me when back in stock" waitlist row.

    One row = one customer asking to be alerted the next time a specific
    product transitions from out-of-stock to in-stock. Created by the
    storefront widget (POST /products/{id}/notify-me) or by the AI sales
    flow when a customer asks for a sold-out item. Consumed by the
    automation engine when store_sync detects a 0 → >0 stock transition
    and fans out one `product_back_in_stock` event per still-pending row.

    The (tenant_id, product_id, customer_id) triple is unique while the
    row is pending — once we send the notification we mark it `notified`
    so a future restock doesn't re-spam the same customer for the same
    product unless they re-subscribe.
    """
    __tablename__ = 'product_interests'
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False)
    product_id = Column(Integer, ForeignKey('products.id'), nullable=False)
    customer_id = Column(Integer, ForeignKey('customers.id'), nullable=False)
    customer_phone = Column(String, nullable=True)
    source = Column(String, nullable=True)   # widget | whatsapp | ai_sales | manual
    notified = Column(Boolean, default=False, nullable=False)
    notified_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    extra_metadata = Column('metadata', JSONB, nullable=True)

    tenant = relationship('Tenant')
    product = relationship('Product')
    customer = relationship('Customer')

    __table_args__ = (
        UniqueConstraint(
            'tenant_id', 'product_id', 'customer_id', 'notified',
            name='uq_product_interest_pending_per_customer',
        ),
    )


class AIActionLog(Base):
    """Audit trail of every action Claude proposed and whether the policy guard approved it."""
    __tablename__ = 'ai_action_logs'
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False)
    customer_id = Column(Integer, ForeignKey('customers.id'), nullable=True)
    # What Claude proposed
    action_type = Column(String, nullable=False)   # suggest_product | suggest_coupon | suggest_bundle | propose_order
    proposed_payload = Column(JSONB, nullable=True)
    # What the policy guard decided
    policy_result = Column(String, nullable=False)  # approved | modified | blocked
    policy_notes = Column(Text, nullable=True)
    final_payload = Column(JSONB, nullable=True)
    # What the commerce permission guard decided (added in migration 0003)
    permission_result = Column(String, nullable=True)   # permitted | denied | n/a
    permission_notes = Column(Text, nullable=True)
    # Fact guard audit (added in migration 0003)
    fact_guard_claims = Column(JSONB, nullable=True)
    reply_was_modified_by_fact_guard = Column(Boolean, default=False)
    applied = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    customer = relationship('Customer')
    tenant = relationship('Tenant')


# ── Payment Sessions ──────────────────────────────────────────────────────────

class PaymentSession(Base):
    """Tracks a Moyasar (or other gateway) payment session tied to an Order."""
    __tablename__ = 'payment_sessions'
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False)
    order_id = Column(Integer, ForeignKey('orders.id'), nullable=True)
    gateway = Column(String, default='moyasar', nullable=False)
    gateway_payment_id = Column(String, nullable=True, index=True)   # Moyasar invoice id
    amount_sar = Column(Float, nullable=False)
    currency = Column(String, default='SAR', nullable=False)
    status = Column(String, default='pending', nullable=False)  # pending|paid|failed|expired
    payment_link = Column(String, nullable=True)
    callback_data = Column(JSONB, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)
    tenant = relationship('Tenant')


# ── Handoff Sessions ──────────────────────────────────────────────────────────

class HandoffSession(Base):
    """Tracks a human handoff for an AI Sales conversation."""
    __tablename__ = 'handoff_sessions'
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False)
    customer_phone = Column(String, nullable=False, index=True)
    customer_name = Column(String, nullable=True)
    status = Column(String, default='active', nullable=False)   # active | resolved
    handoff_reason = Column(Text, nullable=True)
    last_message = Column(Text, nullable=True)
    notification_sent = Column(Boolean, default=False)
    resolved_by = Column(String, nullable=True)
    resolved_at = Column(DateTime, nullable=True)
    context_snapshot = Column(JSONB, nullable=True)  # last few messages/products
    created_at = Column(DateTime, default=datetime.utcnow)
    tenant = relationship('Tenant')


# ── System Event Timeline ─────────────────────────────────────────────────────

class SystemEvent(Base):
    """Unified event log for all major subsystems — drives the Event Timeline UI."""
    __tablename__ = 'system_events'
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False)
    category = Column(String, nullable=False, index=True)
    # payment | ai_sales | handoff | order | orchestrator | system
    event_type = Column(String, nullable=False)
    # e.g. payment.completed, handoff.triggered, order.created
    severity = Column(String, default='info', nullable=False)   # info | warning | error
    summary = Column(String, nullable=True)                     # one-line human-readable
    payload = Column(JSONB, nullable=True)
    reference_id = Column(String, nullable=True, index=True)    # order id, session id, etc.
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    tenant = relationship('Tenant')


# ── Conversation Trace ────────────────────────────────────────────────────────

# ── WhatsApp Embedded Signup Connection ───────────────────────────────────────

class WhatsAppConnection(Base):
    """
    Per-tenant WhatsApp / Meta connection state.
    Persists all Meta identifiers and the (server-side-only) access token.
    The access_token column is NEVER serialised to the frontend.
    """
    __tablename__ = 'whatsapp_connections'
    id           = Column(Integer, primary_key=True)
    tenant_id    = Column(Integer, ForeignKey('tenants.id'), nullable=False, unique=True)

    # State machine ─────────────────────────────────────────────────────────────
    # not_connected | pending | connected | error | disconnected | needs_reauth
    status       = Column(String, default='not_connected', nullable=False)

    # Meta identifiers (safe to log / return to frontend) ─────────────────────
    meta_business_account_id     = Column(String, nullable=True)
    whatsapp_business_account_id = Column(String, nullable=True)
    phone_number_id              = Column(String, nullable=True)
    phone_number                 = Column(String, nullable=True)
    business_display_name        = Column(String, nullable=True)
    business_manager_id          = Column(String, nullable=True)

    # Provider / connection type ──────────────────────────────────────────────
    # provider: 'meta' | 'dialog360'
    provider          = Column(String, nullable=False, default='meta')
    # connection_type: 'direct' (platform adds number to shared WABA)
    #                | 'embedded' (merchant's own WABA)
    #                | 'coexistence' (merchant keeps WA Business App + API via 360dialog)
    connection_type   = Column(String, nullable=True, default='direct')

    # Token — backend-only, NEVER send to frontend ────────────────────────────
    access_token      = Column(String, nullable=True)
    token_type        = Column(String, nullable=True)   # short_lived | long_lived
    token_expires_at  = Column(DateTime, nullable=True)

    # Timestamps and audit ────────────────────────────────────────────────────
    connected_at      = Column(DateTime, nullable=True)
    last_verified_at  = Column(DateTime, nullable=True)
    last_attempt_at   = Column(DateTime, nullable=True)
    last_error        = Column(Text, nullable=True)

    # Disconnect audit — structured record of every explicit disconnect event.
    # Values: 'merchant_requested_disconnect' | 'admin_forced_disconnect'
    # Cleared to NULL when the merchant initiates a reconnect.
    disconnect_reason       = Column(String,   nullable=True)
    disconnected_at         = Column(DateTime, nullable=True)
    disconnected_by_user_id = Column(Integer,  nullable=True)

    # Prerequisites flags ─────────────────────────────────────────────────────
    webhook_verified  = Column(Boolean, default=False)
    sending_enabled   = Column(Boolean, default=False)

    # Guardian: last time a real inbound webhook was received for this tenant
    last_webhook_received_at = Column(DateTime(timezone=True), nullable=True)

    extra_metadata    = Column(JSONB, nullable=True)
    created_at        = Column(DateTime, default=datetime.utcnow)
    updated_at        = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    tenant = relationship('Tenant', back_populates='whatsapp_connection')


# ── Store Knowledge Sync ──────────────────────────────────────────────────────

class StoreSyncJob(Base):
    """Tracks a single store-sync run (full or incremental) for a tenant."""
    __tablename__ = 'store_sync_jobs'
    id           = Column(Integer, primary_key=True)
    tenant_id    = Column(Integer, ForeignKey('tenants.id'), nullable=False)

    # pending | running | completed | failed | partial
    status       = Column(String, default='pending', nullable=False)
    # full | incremental | webhook
    sync_type    = Column(String, default='full', nullable=False)
    triggered_by = Column(String, nullable=True)    # merchant | system | webhook

    started_at   = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    # Progress counters ────────────────────────────────────────────────────────
    products_synced   = Column(Integer, default=0)
    categories_synced = Column(Integer, default=0)
    orders_synced     = Column(Integer, default=0)
    shipping_synced   = Column(Integer, default=0)
    coupons_synced    = Column(Integer, default=0)
    customers_synced  = Column(Integer, default=0)

    error_message  = Column(Text, nullable=True)
    extra_metadata = Column(JSONB, nullable=True)
    created_at     = Column(DateTime, default=datetime.utcnow)

    tenant = relationship('Tenant', back_populates='store_sync_jobs')


class StoreKnowledgeSnapshot(Base):
    """
    Normalised, AI-ready snapshot of a tenant's store data.
    Updated after every full or incremental sync.
    The AI reads this to answer questions accurately.
    """
    __tablename__ = 'store_knowledge_snapshots'
    id        = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False, unique=True)

    # Normalised knowledge blocks (JSONB) ──────────────────────────────────────
    store_profile    = Column(JSONB, nullable=True)   # name, logo, url, contact
    catalog_summary  = Column(JSONB, nullable=True)   # top products, categories
    shipping_summary = Column(JSONB, nullable=True)   # methods, zones, estimates
    policy_summary   = Column(JSONB, nullable=True)   # return, payment, support
    coupon_summary   = Column(JSONB, nullable=True)   # active coupons/offers

    # Sync metadata ───────────────────────────────────────────────────────────
    last_full_sync_at        = Column(DateTime, nullable=True)
    last_incremental_sync_at = Column(DateTime, nullable=True)

    # Entity counts (displayed in dashboard) ──────────────────────────────────
    product_count  = Column(Integer, default=0)
    category_count = Column(Integer, default=0)
    order_count    = Column(Integer, default=0)
    coupon_count   = Column(Integer, default=0)
    customer_count = Column(Integer, default=0)

    sync_version = Column(Integer, default=0)   # bumped on every full sync
    created_at   = Column(DateTime, default=datetime.utcnow)
    updated_at   = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    tenant = relationship('Tenant', back_populates='store_knowledge')


class ConversationTrace(Base):
    """Per-turn debug trace for every AI Sales conversation step."""
    __tablename__ = 'conversation_traces'
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False)
    customer_phone = Column(String, nullable=False, index=True)
    session_id = Column(String, nullable=True, index=True)  # date-scoped session key
    turn = Column(Integer, default=1)
    # Input
    message = Column(Text, nullable=True)
    # Detection
    detected_intent = Column(String, nullable=True)
    confidence = Column(Float, nullable=True)
    response_type = Column(String, nullable=True)
    # Orchestrator
    orchestrator_used = Column(Boolean, default=False)
    model_used = Column(String, nullable=True)
    fact_guard_modified = Column(Boolean, default=False)
    fact_guard_claims = Column(JSONB, nullable=True)
    # Actions
    actions_triggered = Column(JSONB, nullable=True)
    # Output
    response_text = Column(Text, nullable=True)
    order_started = Column(Boolean, default=False)
    payment_link_sent = Column(Boolean, default=False)
    handoff_triggered = Column(Boolean, default=False)
    # Performance
    latency_ms = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    tenant = relationship('Tenant')


# ── WhatsApp Conversation Usage Tracking ─────────────────────────────────────

class WhatsAppUsage(Base):
    """
    Monthly WhatsApp conversation usage counter per tenant.

    Meta bills per "conversation" (24-hour window), not per message.
    This table tracks how many Meta conversations a tenant has consumed
    this month so we can enforce plan limits and protect platform costs.

    One row per (tenant_id, year, month).
    """
    __tablename__ = 'whatsapp_usage'

    id                          = Column(Integer, primary_key=True)
    tenant_id                   = Column(Integer, ForeignKey('tenants.id'), nullable=False, index=True)

    # Calendar period — unique per (tenant, year, month) enforced by DB index
    year                        = Column(Integer, nullable=False)
    month                       = Column(Integer, nullable=False)

    # Counters split by Meta category
    service_conversations_used  = Column(Integer, default=0, nullable=False)
    marketing_conversations_used = Column(Integer, default=0, nullable=False)
    conversations_limit         = Column(Integer, default=1000, nullable=False)

    # Alert state (prevent duplicate notifications per month)
    alert_80_sent               = Column(Boolean, default=False, nullable=False)
    alert_100_sent              = Column(Boolean, default=False, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    tenant = relationship('Tenant', back_populates='whatsapp_usages')


# ── Per-customer Conversation Window (race-safe 24h tracking) ─────────────────

class WaConversationWindow(Base):
    """
    One row per (tenant_id, customer_phone).
    Tracks the start timestamp of the CURRENT open Meta conversation window
    for each customer. Used to determine whether a new inbound/outbound message
    opens a NEW billable window (>24 h since last window_start) or falls
    inside an already-counted one.

    SELECT FOR UPDATE on this row serialises concurrent webhook calls for the
    same customer, eliminating race conditions.
    """
    __tablename__ = 'wa_conversation_windows'

    id             = Column(Integer, primary_key=True)
    tenant_id      = Column(Integer, ForeignKey('tenants.id'), nullable=False)
    customer_phone = Column(String, nullable=False)
    window_start   = Column(DateTime, nullable=False)   # UTC, naive
    category       = Column(String, default='service', nullable=False)  # service | marketing
    created_at     = Column(DateTime, default=datetime.utcnow)
    updated_at     = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    tenant = relationship('Tenant')


# ── Conversation Audit Log ────────────────────────────────────────────────────

class ConversationLog(Base):
    """
    Immutable audit record written every time a NEW billable Meta conversation
    window opens for a tenant's customer.

    Purpose:
      - Explain counter increments to merchants ("why did my count go up?")
      - Support cost analysis (service vs. marketing per day)
      - Multi-tenant isolation — every query filters on tenant_id
    """
    __tablename__ = 'conversation_logs'

    id                       = Column(Integer, primary_key=True)
    tenant_id                = Column(Integer, ForeignKey('tenants.id'), nullable=False, index=True)
    customer_phone           = Column(String, nullable=False, index=True)
    conversation_started_at  = Column(DateTime, nullable=False)          # UTC, naive
    # source: inbound | campaign | template | api
    source                   = Column(String, default='inbound', nullable=False)
    # category: service | marketing
    category                 = Column(String, default='service', nullable=False)
    created_at               = Column(DateTime, default=datetime.utcnow)

    tenant = relationship('Tenant')


# ── Merchant Addons ────────────────────────────────────────────────────────────

class MerchantAddon(Base):
    """
    Stores per-tenant addon state.
    Each row = one addon for one tenant.
    settings_json holds addon-specific configuration.
    """
    __tablename__ = 'merchant_addons'
    __table_args__ = (
        UniqueConstraint('tenant_id', 'addon_key', name='uq_merchant_addon_tenant_key'),
    )

    id            = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id     = Column(Integer, ForeignKey('tenants.id'), nullable=False, index=True)
    addon_key     = Column(String(64), nullable=False)
    is_enabled    = Column(Boolean, default=False, nullable=False)
    settings_json = Column(JSONB, nullable=True, default=dict)
    created_at    = Column(DateTime, default=datetime.utcnow)
    updated_at    = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    tenant = relationship('Tenant')


class MerchantWidget(Base):
    """
    Conversion Widgets System — visual sales tools rendered inside merchant stores.

    Each row = one widget type for one tenant.
    widget_key    : unique identifier (whatsapp_widget | discount_popup | slide_offer | …)
    settings_json : widget-specific UI configuration (phone, colors, texts …)
    display_rules : when/how/where to show the widget (delay, pages, trigger, show_once …)
    """
    __tablename__ = 'merchant_widgets'
    __table_args__ = (
        UniqueConstraint('tenant_id', 'widget_key', name='uq_merchant_widget_tenant_key'),
    )

    id            = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id     = Column(Integer, ForeignKey('tenants.id'), nullable=False, index=True)
    widget_key    = Column(String(64), nullable=False)
    is_enabled    = Column(Boolean, default=False, nullable=False)
    settings_json = Column(JSONB, nullable=True, default=dict)
    display_rules = Column(JSONB, nullable=True, default=dict)
    created_at    = Column(DateTime, default=datetime.utcnow)
    updated_at    = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    tenant = relationship('Tenant')


# ── Webhook Guardian Audit Log ────────────────────────────────────────────────

class IntegrityEvent(Base):
    """
    Append-only structured audit trail for all identity-resolution and
    cross-tenant conflict events detected or blocked by the integrity layer.

    event values:
        tenant_resolved        – normal routing: phone_number_id → tenant
        duplicate_identity     – same phone/waba/store_id found on >1 tenant
        cross_tenant_conflict  – WA connection and store on different tenants
        write_blocked          – write rejected by integrity guard
        reconciliation_started – merge workflow initiated
        reconciliation_done    – merge workflow completed
        orphaned_wa_connection – WA conn exists but no store integration
        orphaned_store         – store integration exists but no WA conn
    """
    __tablename__ = 'integrity_events'

    id              = Column(Integer, primary_key=True)
    event           = Column(String, nullable=False, index=True)
    tenant_id       = Column(Integer, nullable=True, index=True)
    other_tenant_id = Column(Integer, nullable=True)
    phone_number_id = Column(String, nullable=True)
    waba_id         = Column(String, nullable=True)
    store_id        = Column(String, nullable=True)
    provider        = Column(String, nullable=True)
    action          = Column(String, nullable=True)
    result          = Column(String, nullable=True)
    detail          = Column(Text, nullable=True)
    actor           = Column(String, nullable=True)
    dry_run         = Column(Boolean, nullable=True)
    created_at      = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class WebhookGuardianLog(Base):
    """
    Structured audit trail of every webhook-reliability action taken by the
    guardian background worker or admin tooling.

    event values:
        webhook_subscribed        – WABA was just subscribed (first time)
        webhook_resubscribed      – guardian re-subscribed a stalled connection
        webhook_verification_failed – subscribed_apps call returned false/error
        webhook_recovered         – connection went from stalled → healthy
        webhook_stalled           – guardian detected no inbound for >15 min
        critical_error_detected   – webhook_verified=false while status=connected
    """
    __tablename__ = 'webhook_guardian_log'

    id              = Column(Integer, primary_key=True)
    tenant_id       = Column(Integer, ForeignKey('tenants.id', ondelete='CASCADE'), nullable=False, index=True)
    phone_number_id = Column(String, nullable=True)
    waba_id         = Column(String, nullable=True)
    # event type (see docstring above)
    event           = Column(String, nullable=False, index=True)
    success         = Column(Boolean, nullable=False, default=True)
    detail          = Column(Text, nullable=True)
    created_at      = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    tenant = relationship('Tenant')


class WebhookEvent(Base):
    """
    Durable inbound-webhook queue.

    Every external webhook (Salla, Zid, WhatsApp, Moyasar, ...) is FIRST
    persisted to this table — before any business processing — so that:

      • a 200 OK from the receiver means the event is durably stored.
      • async processing can retry on failure with exponential backoff.
      • any failure eventually lands in status='dead_letter' visible to admins.
      • the raw body + headers are preserved for offline debugging / replay.

    Finite state machine for `status`:
        received   → dispatcher has not yet claimed this row
        processing → dispatcher has claimed it (heartbeat via updated_at)
        processed  → business logic completed successfully
        failed     → transient error; will retry at next_retry_at
        dead_letter→ exhausted retries; requires manual replay by admin
    """
    __tablename__ = 'webhook_events'

    id                 = Column(Integer, primary_key=True)
    tenant_id          = Column(Integer, nullable=True, index=True)
    provider           = Column(String, nullable=False, index=True)
    event_type         = Column(String, nullable=True, index=True)
    external_event_id  = Column(String, nullable=True)
    store_id           = Column(String, nullable=True)
    raw_headers        = Column(JSONB, nullable=True)
    raw_body           = Column(Text, nullable=True)
    parsed_payload     = Column(JSONB, nullable=True)
    signature_valid    = Column(Boolean, nullable=True)
    status             = Column(String, nullable=False, default='received', index=True)
    attempts           = Column(Integer, nullable=False, default=0)
    last_error         = Column(Text, nullable=True)
    last_error_at      = Column(DateTime(timezone=True), nullable=True)
    next_retry_at      = Column(DateTime(timezone=True), nullable=True)
    received_at        = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    processed_at       = Column(DateTime(timezone=True), nullable=True)
    created_at         = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at         = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
