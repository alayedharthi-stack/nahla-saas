from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
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
    widget_settings = relationship('WidgetSetting', back_populates='tenant')
    whatsapp_numbers = relationship('WhatsAppNumber', back_populates='tenant')
    users = relationship('User', back_populates='tenant')
    products = relationship('Product', back_populates='tenant')
    orders = relationship('Order', back_populates='tenant')
    coupons = relationship('Coupon', back_populates='tenant')
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
    billing_plans = relationship('BillingPlan', back_populates='tenant')
    subscriptions = relationship('BillingSubscription', back_populates='tenant')
    payments = relationship('BillingPayment', back_populates='tenant')
    invoices = relationship('BillingInvoice', back_populates='tenant')
    app_installs = relationship('AppInstall', back_populates='tenant')
    app_payments = relationship('AppPayment', back_populates='tenant')

class TenantSettings(Base):
    __tablename__ = 'tenant_settings'
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False, unique=True)
    tenant = relationship('Tenant', back_populates='settings', uselist=False)
    show_nahla_branding = Column(Boolean, default=True, nullable=False)
    branding_text = Column(String, default='🐝 Powered by Nahla', nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    metadata = Column(JSONB, nullable=True)

class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    username = Column(String, unique=True, nullable=False)
    email = Column(String, unique=True, nullable=False)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False)
    tenant = relationship('Tenant', back_populates='users')

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
    metadata = Column(JSONB, nullable=True)
    recommendation_tags = Column(JSONB, nullable=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False)
    tenant = relationship('Tenant', back_populates='products')

class Order(Base):
    __tablename__ = 'orders'
    id = Column(Integer, primary_key=True)
    external_id = Column(String, index=True, nullable=True)
    status = Column(String, nullable=False)
    total = Column(String, nullable=True)
    customer_info = Column(JSONB, nullable=True)
    line_items = Column(JSONB, nullable=True)
    checkout_url = Column(String, nullable=True)
    is_abandoned = Column(Boolean, default=False)
    metadata = Column(JSONB, nullable=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False)
    tenant = relationship('Tenant', back_populates='orders')

class Coupon(Base):
    __tablename__ = 'coupons'
    id = Column(Integer, primary_key=True)
    code = Column(String, unique=True, nullable=False)
    description = Column(Text, nullable=True)
    discount_type = Column(String, nullable=True)
    discount_value = Column(String, nullable=True)
    metadata = Column(JSONB, nullable=True)
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

class Integration(Base):
    __tablename__ = 'integrations'
    id = Column(Integer, primary_key=True)
    provider = Column(String, nullable=False)
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
    metadata = Column(JSONB, nullable=True)
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
    metadata = Column(JSONB, nullable=True)
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
    metadata = Column(JSONB, nullable=True)

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
    metadata = Column(JSONB, nullable=True)

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
    metadata = Column(JSONB, nullable=True)
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
    metadata = Column(JSONB, nullable=True)
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
    metadata = Column(JSONB, nullable=True)

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
    metadata = Column(JSONB, nullable=True)
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
    metadata = Column(JSONB, nullable=True)
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
    metadata = Column(JSONB, nullable=True)

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
    metadata = Column(JSONB, nullable=True)
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
    metadata = Column(JSONB, nullable=True)

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
    last_order_at = Column(DateTime, nullable=True)
    # Segmentation
    segment = Column(String, default='new')   # new | active | at_risk | churned | vip
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
