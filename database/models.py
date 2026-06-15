from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
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
    # When True, this tenant is the Nahla *platform* sales workspace —
    # inbound WhatsApp messages route to the deterministic Platform-Brain
    # sales flow (CTA buttons → trial signup) instead of a merchant's
    # store-AI assistant. Default False so every newly-created tenant is
    # treated as a merchant store, never as the platform.
    is_platform_tenant = Column(Boolean, default=False, nullable=False, server_default='false')
    created_at = Column(DateTime, default=datetime.utcnow)
    store_address = Column(Text, nullable=True)
    google_maps_link = Column(String, nullable=True)
    apple_maps_link = Column(String, nullable=True)
    same_day_delivery_enabled = Column(Boolean, default=False)
    pickup_enabled = Column(Boolean, default=True)
    branding = Column(JSONB, nullable=True)
    recommendation_controls = Column(JSONB, nullable=True)
    coupon_policy = Column(JSONB, nullable=True)
    # AI loop guard — list of normalized phone numbers (digits-only) whose
    # inbound messages must NEVER be passed to the LLM. Set/cleared via the
    # /conversations/blocklist endpoints. The system also adds well-known
    # internal numbers (Nahla / Shawahid / staff) at runtime via env config.
    ai_blocked_numbers = Column(JSONB, nullable=True)

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
    merchant_branches = relationship('MerchantBranch', back_populates='tenant')

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


# ── Phase 2A Sprint 1 — TOTP 2FA ──────────────────────────────────────────────
# One row per user that has *started* 2FA enrolment. The row is created on
# /auth/2fa/setup/confirm AFTER the first OTP has been proven; pending
# enrolments live in a short-lived JWT (type=2fa_setup), not in this table.
class UserTotp(Base):
    __tablename__ = 'user_totp'
    user_id         = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), primary_key=True)
    # Fernet-encrypted base32 TOTP secret. Plaintext NEVER touches the DB.
    secret_enc      = Column(LargeBinary, nullable=False)
    confirmed_at    = Column(DateTime(timezone=True), nullable=True)
    last_used_at    = Column(DateTime(timezone=True), nullable=True)
    # Soft-lock counter; rate limiting at the router layer is the first
    # line of defence — this exists so a sustained attack on a single
    # user's OTP eventually trips a row-level lock visible to ops.
    failed_attempts = Column(Integer, nullable=False, default=0, server_default='0')
    locked_until    = Column(DateTime(timezone=True), nullable=True)
    created_at      = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at      = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class UserRecoveryCode(Base):
    __tablename__ = 'user_recovery_codes'
    id          = Column(Integer, primary_key=True, autoincrement=True)
    user_id     = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    # bcrypt hash of the plaintext code. We never store the code itself —
    # the user gets it ONCE in the enrolment response and is told to
    # download/copy. Lost codes can only be regenerated via the
    # /auth/2fa/recovery/regenerate endpoint (Sprint 2).
    code_hash   = Column(String(255), nullable=False)
    created_at  = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    used_at     = Column(DateTime(timezone=True), nullable=True)

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
    # ── Meta WhatsApp Catalog (migration 0061) ─────────────────────────────
    # Override the retailer id used when sending this product via Meta
    # Catalog messages (``interactive.type = "product"`` /
    # ``"product_list"``). When NULL the runtime helper
    # ``effective_retailer_id(product)`` falls back to ``external_id`` —
    # which is the convention Salla's Meta Commerce auto-publish uses, so
    # 95% of merchants need zero manual mapping. Populate this column
    # only when a merchant publishes products to Meta with custom ids.
    meta_retailer_id = Column(String(255), nullable=True)
    # Last time we observed / verified this product is live in the
    # merchant's Meta Catalog. Stays NULL until a future "publish to Meta"
    # job populates it. Reading code MUST tolerate NULL — absence here
    # never blocks a send attempt, it only suppresses the freshness badge.
    meta_catalog_published_at = Column(DateTime(timezone=True), nullable=True)
    # ── Product source (migration 0062) ────────────────────────────────────
    # Which adapter / channel produced this row. The catalog feature is
    # explicitly *source-agnostic* — it consumes any product regardless
    # of where it came from. This column exists so:
    #   • diagnostics can render a "current source" badge in the UI
    #     (Salla / Manual / Mixed / Unknown);
    #   • per-source resync / purge endpoints can scope themselves
    #     without scanning every row's ``extra_metadata`` JSONB;
    #   • future writers (Shopify / WooCommerce / CSV upload) plug in
    #     by setting this string and nothing else.
    # Allowed values are intentionally not constrained at the DB level
    # so new writers can plug in without a migration. The string
    # ``"manual"`` is reserved for products entered through the Nahla
    # dashboard CRUD; ``"salla"`` for the Salla sync; ``"zid"`` for
    # the Zid sync; ``"unknown"`` for legacy rows whose origin we
    # can't determine.
    source = Column(String(32), nullable=True, index=True)
    # ── Catalog visibility (migration 0072 — P1-G1) ───────────────────────
    # Soft lifecycle for Meta reconciliation + merchant hide. Never hard-
    # delete rows that orders / affinities / KB links may reference.
    catalog_status = Column(
        String(32), nullable=False, server_default=sa.text("'active'"), default="active",
    )
    merchant_hidden_at = Column(DateTime(timezone=True), nullable=True)
    meta_last_seen_at = Column(DateTime(timezone=True), nullable=True)
    meta_removed_at = Column(DateTime(timezone=True), nullable=True)
    archived_at = Column(DateTime(timezone=True), nullable=True)
    # ── Parent / variants intelligence layer (migration 0064) ──────────────
    # ``Product`` is now treated as the PARENT (non-sellable when
    # ``has_variants=True``). Every sellable SKU lives as a row in the
    # new ``product_variants`` table (one default variant is created
    # automatically for simple products by the migration backfill so
    # downstream code can always go "give me the variant" without
    # branching on legacy rows).
    #
    # ``has_variants`` is true iff the row has 2+ sellable variants OR
    # the upstream platform (Salla) flagged the product as requiring
    # option selection. The brain reads this to decide whether to ship
    # the product card directly or first ask the customer "which size
    # / color?".
    #
    # ``default_variant_id`` points at the variant the sender should
    # use when ``has_variants`` is False (one-variant products) — it's
    # the cheapest read path because we don't have to JOIN+ORDER on
    # every send. The FK is intentionally string-typed so SQLAlchemy's
    # declarative resolver can handle the forward reference to
    # ``product_variants`` (defined below). Nullable because the
    # column is populated by the migration backfill / sync writer, not
    # at row-insert time.
    has_variants       = Column(Boolean, nullable=False,
                                server_default=sa.text("false"), default=False)
    # ``use_alter=True`` + ``post_create=True`` so SQLAlchemy creates
    # the table without this FK then ALTERs it in after both tables
    # exist — breaks the otherwise unresolvable cycle between
    # ``products.default_variant_id → product_variants.id`` and
    # ``product_variants.product_id → products.id`` at table-create
    # time. The migration mirrors this by adding the column with
    # ``ADD COLUMN`` after the variant table exists.
    default_variant_id = Column(
        Integer,
        ForeignKey(
            "product_variants.id",
            use_alter=True,
            name="fk_products_default_variant",
        ),
        nullable=True, index=True,
    )
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False)
    tenant = relationship('Tenant', back_populates='products')
    variants = relationship(
        'ProductVariant',
        back_populates='product',
        foreign_keys='ProductVariant.product_id',
        cascade='all, delete-orphan',
        lazy='select',
    )
    default_variant = relationship(
        'ProductVariant',
        foreign_keys=[default_variant_id],
        post_update=True,
    )


class ProductVariant(Base):
    """A sellable SKU under a parent :class:`Product`.

    Every catalog row eventually resolves to a variant: either a real
    one (Salla options like "Size:M / Color:Red") or a synthetic
    ``is_default=True`` row that mirrors the parent for legacy
    one-SKU products. This keeps every downstream consumer (WhatsApp /
    Meta sender, brain decision engine, Google Merchant feed) on a
    single uniform contract: "pick a variant, then send its
    ``retailer_id``" — no special-casing for "old products without
    variants" anywhere in the runtime.

    The ``retailer_id`` here is the per-variant Meta product identifier
    Salla / the merchant publishes against Meta Catalog. We default it
    to ``nahla_v_<id>`` when Salla didn't carry one so the catalog
    sender can still attempt a send (the fallback image+CTA path will
    catch the failure on Meta's side without breaking the user).
    """
    __tablename__ = "product_variants"
    id                  = Column(Integer, primary_key=True)
    tenant_id           = Column(Integer, ForeignKey('tenants.id'),
                                 nullable=False, index=True)
    # ``ondelete="CASCADE"`` is the right behaviour: when a parent is
    # purged (manual delete or tenant wipe) the variants go too. We
    # also configure ORM-level cascade on the parent relationship so
    # in-Python deletes do the same without relying on the DB.
    product_id          = Column(Integer,
                                 ForeignKey("products.id", ondelete="CASCADE"),
                                 nullable=False, index=True)
    # Upstream platform's variant identifier. Nullable for synthetic
    # default variants (one-SKU products created by the backfill).
    salla_variant_id    = Column(String(64),  nullable=True, index=True)
    sku                 = Column(String(128), nullable=True)
    # Meta ``product_retailer_id`` used in catalog sends. Set per
    # variant so a customer who picked "size M" gets the Meta card
    # for *that* SKU, not for the parent. Falls back via the
    # ``effective_variant_retailer_id`` helper at send-time.
    retailer_id         = Column(String(255), nullable=True, index=True)
    price               = Column(String(32),  nullable=True)
    currency            = Column(String(8),   nullable=True)
    stock_quantity      = Column(Integer,     nullable=True)
    in_stock            = Column(Boolean,     nullable=False,
                                 server_default=sa.text("true"), default=True)
    # Option map (e.g. ``{"size": "M", "color": "red"}``) — read by the
    # brain to render "which size?" prompts and by the Google Merchant
    # feed to populate the size/color/material columns.
    options             = Column(JSONB, nullable=True)
    # Human-readable single-line summary of ``options`` (e.g.
    # ``"M / Red"``). Denormalised so templates don't have to re-join
    # option-name lookups on every send.
    option_summary      = Column(String(255), nullable=True)
    image_url           = Column(String(2048), nullable=True)
    # True for the synthetic single variant created when a parent has
    # no real variants (legacy one-SKU products). Lets the sender go
    # straight to ``product.default_variant`` without branching.
    is_default          = Column(Boolean, nullable=False,
                                 server_default=sa.text("false"), default=False)
    extra_metadata      = Column('metadata', JSONB, nullable=True)
    created_at          = Column(DateTime(timezone=True),
                                 server_default=sa.text("CURRENT_TIMESTAMP"))
    updated_at          = Column(DateTime(timezone=True),
                                 server_default=sa.text("CURRENT_TIMESTAMP"))

    product = relationship(
        'Product',
        back_populates='variants',
        foreign_keys=[product_id],
    )

    __table_args__ = (
        # A given parent can only carry a given salla_variant_id once.
        # NULL is allowed to repeat (synthetic default variants).
        UniqueConstraint('product_id', 'salla_variant_id',
                         name='uq_variants_product_salla'),
        Index('ix_variants_tenant_retailer', 'tenant_id', 'retailer_id'),
    )


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
    # Coupon taxonomy (added in migration 0038). source_type tells the
    # merchant who created the coupon; coupon_level groups codes by
    # discount tier; allocation_channel pins the delivery surface so the
    # AI can't accidentally hand out a campaign-only code.
    source_type = Column(String, nullable=False, default='manual', index=True)
    coupon_level = Column(String, nullable=True, index=True)
    allocation_channel = Column(String, nullable=True)
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
    __table_args__ = (
        # ── Migration 0032 (replaces 0031 phone index) ────────────────────────
        # Uniqueness is enforced on E.164 normalized_phone, NOT on raw phone.
        # This guarantees:
        #   - +966570000000, 0570000000, 966570000000 all resolve to one row.
        #   - Cross-tenant: same normalized_phone at tenant A ≠ tenant B (separate rows).
        Index(
            'ix_customers_tenant_normalized_phone',
            'tenant_id', 'normalized_phone',
            unique=True,
            postgresql_where=sa.text(
                "normalized_phone IS NOT NULL AND normalized_phone != ''"
            ),
        ),
        # ── Migration 0031 ────────────────────────────────────────────────────
        # Kept as a non-unique covering index for display-value queries.
        # Uniqueness responsibility was moved to ix_customers_tenant_normalized_phone.
        Index('ix_customers_tenant_id', 'tenant_id'),
        # Partial unique index: one Salla customer per (tenant, salla_customer_id).
        Index(
            'ix_customers_tenant_salla_id',
            'tenant_id', 'salla_customer_id',
            unique=True,
            postgresql_where=sa.text(
                "salla_customer_id IS NOT NULL AND salla_customer_id != ''"
            ),
        ),
    )
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=True)
    email = Column(String, nullable=True)

    # phone: raw display value as received from the caller (kept for debugging/display).
    # Do NOT use for identity lookups — use normalized_phone instead.
    phone = Column(String, nullable=True)

    extra_metadata = Column('metadata', JSONB, nullable=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False)
    tenant = relationship('Tenant')
    addresses = relationship('CustomerAddress', back_populates='customer')

    # ── Migration 0032: E.164 normalized phone (canonical identity key) ───────
    # Always stored in E.164 format (+[country_code][number]).
    # This is the authoritative identity column for deduplication.
    # The UNIQUE INDEX ix_customers_tenant_normalized_phone enforces one
    # customer per (tenant, E.164 number) at the DB level.
    normalized_phone = Column(String, nullable=True, index=True)

    # ── Migration 0031: first-class columns promoted from JSONB ──────────────
    salla_customer_id   = Column(String, nullable=True, index=True)
    acquisition_channel = Column(String, nullable=True, index=True)
    first_seen_at       = Column(DateTime(timezone=True), nullable=True)
    last_interaction_at = Column(DateTime(timezone=True), nullable=True)

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

class CustomerImportBatch(Base):
    """One import session created when the merchant uploads a CSV/XLSX
    of customers. Persists across the four wizard steps (upload →
    mapping → preview → commit) so the dashboard can resume / drill
    into any step. The full classified row payload is kept in
    `rows_payload` (JSONB) so we never re-parse the file twice."""
    __tablename__ = 'customer_import_batches'

    # NOTE on server_defaults: we use `func.now()` / Python literals here
    # instead of `sa.text('now()')` so the same model can be reflected by
    # SQLite-backed unit tests *and* by Postgres in production. Raw
    # `text('now()')` is emitted verbatim into the CREATE TABLE DDL and
    # SQLite (which has no `now()` function) refuses it with
    # `OperationalError: near "(": syntax error`. `func.now()` is dialect-
    # aware — Postgres renders `now()`, SQLite renders `CURRENT_TIMESTAMP`.
    id          = Column(Integer, primary_key=True)
    tenant_id   = Column(Integer, ForeignKey('tenants.id'), nullable=False, index=True)
    created_by  = Column(Integer, ForeignKey('users.id'), nullable=True)
    created_at  = Column(DateTime(timezone=True), nullable=False,
                         server_default=sa.func.now())
    committed_at = Column(DateTime(timezone=True), nullable=True)

    filename    = Column(String, nullable=True)
    file_kind   = Column(String, nullable=True)        # csv | xlsx
    status      = Column(String, nullable=False, server_default='parsed')
    # parsed → mapping submitted → previewed → committed | failed

    # Column mapping submitted by the user on step 2:
    #   {"name": "<header>", "phone": "<header>", ...}
    column_mapping = Column(JSONB, nullable=True)

    # Aggregate counters populated after dedupe classification.
    total_rows     = Column(Integer, nullable=False, server_default='0')
    new_count      = Column(Integer, nullable=False, server_default='0')
    match_count    = Column(Integer, nullable=False, server_default='0')
    suspect_count  = Column(Integer, nullable=False, server_default='0')
    invalid_count  = Column(Integer, nullable=False, server_default='0')

    # Final commit results (populated only on successful commit).
    created_count  = Column(Integer, nullable=False, server_default='0')
    updated_count  = Column(Integer, nullable=False, server_default='0')
    skipped_count  = Column(Integer, nullable=False, server_default='0')
    error_count    = Column(Integer, nullable=False, server_default='0')

    # Full classified payload — array of row dicts. Each row has at
    # least: row_index, raw, normalized, classification, suggestion.
    rows_payload   = Column(JSONB, nullable=True)

    # Free-form notes / structured errors for failed parses.
    error_message  = Column(Text, nullable=True)


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
    # ── AI pause state (loop guard) ─────────────────────────────────────────
    # When ai_paused is True the webhook records the inbound message and
    # returns BEFORE calling any LLM. ai_paused_reason carries one of:
    #   manual | human_handoff | bot_loop_detected | rate_limit | internal_number
    # Set/cleared by core/ai_pause_guard. Independent from `paused_by_human`
    # which is the legacy flag for dashboard takeover.
    ai_paused = Column(Boolean, default=False, nullable=False, server_default='false')
    ai_paused_reason = Column(String, nullable=True)
    ai_paused_at = Column(DateTime(timezone=True), nullable=True)
    ai_paused_by = Column(String, nullable=True)
    # ── Unified human-takeover state ────────────────────────────────────────
    # Filled when the merchant clicks "تولّي / تحويل لموظف" from the
    # conversations panel. Any of these (or the legacy is_human_handoff /
    # paused_by_human columns) flips the inbox row into the human filter.
    needs_human = Column(Boolean, default=False, nullable=False, server_default='false')
    handoff_active = Column(Boolean, default=False, nullable=False, server_default='false')
    taken_over_at = Column(DateTime(timezone=True), nullable=True)
    taken_over_by = Column(String, nullable=True)
    # Set by the dashboard when the merchant opens this conversation.
    # The unread counter excludes inbound messages older than this
    # timestamp, so opening the conversation zeros the badge even
    # without a manual reply.
    last_read_at = Column(DateTime(timezone=True), nullable=True)
    # ── Paid-order signal ──────────────────────────────────────────────────
    # Stamped when payment evidence is confirmed (the
    # ``maybe_handle_receipt_inbound`` short-circuit fires, or any other
    # code path that flips ``payment_evidence_status='confirmed'`` /
    # ``payment_receipt_received=True``). Drives the "طلبات مدفوعة" inbox
    # filter so the merchant can jump straight to conversations with a
    # confirmed transfer attached. NULL = no confirmed receipt yet.
    last_payment_confirmed_at = Column(DateTime(timezone=True), nullable=True)
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


class CustomerNameCleanupDraft(Base):
    """In-progress review session for the bulk customer-name cleanup tool.

    Reviewing 1 500+ customer names in one sitting is unrealistic. This
    table backs an **incremental** workflow: the merchant opens the
    modal, edits some chips, closes the modal, comes back later — and
    every edit is restored exactly where it was left off.

    One row per ``(tenant_id, customer_id)`` that the merchant has
    touched. Customers the cleaner thinks need work but the merchant
    hasn't reviewed yet do NOT have a row here — they appear in the
    preview with their cleaner-default state. Rows are deleted on
    apply (the row is no longer interesting once the name is clean)
    or via the "تجاهل المسودة" action.

    Edit state shape:
      * ``removed_word_indices`` — JSON list of int indices (into the
        whitespace-split tokens of ``original_name``) that the merchant
        flipped OFF. When ``None``, the cleaner's default removal set
        is used.
      * ``cleared`` — when True, the row is force-cleared regardless
        of which individual words were flipped.
      * ``status`` — ``"edited"`` for any merchant-touched row;
        ``"skipped"`` if the merchant explicitly opted out so the row
        doesn't surface in future review sessions.

    Tenant isolation is enforced at the application layer (the
    endpoints always filter by ``tenant_id = resolve_tenant_id(request)``)
    and reinforced by the unique constraint on
    ``(tenant_id, customer_id)`` so a draft cannot be aliased across
    stores even if the application leaks a stale id.
    """
    __tablename__ = 'customer_name_cleanup_drafts'
    __table_args__ = (
        UniqueConstraint(
            'tenant_id', 'customer_id',
            name='uq_cleanup_draft_tenant_customer',
        ),
        Index(
            'ix_cleanup_draft_tenant_updated',
            'tenant_id', 'updated_at',
        ),
        Index(
            'ix_cleanup_draft_tenant_status',
            'tenant_id', 'status',
        ),
    )
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False)
    customer_id = Column(Integer, ForeignKey('customers.id'), nullable=False)
    # Snapshot of Customer.name at the moment the row was created or
    # last refreshed. If the underlying customer name changes (rare
    # — usually only happens when the merchant edits in another tab),
    # the preview endpoint clears the draft so the merchant is not
    # confused by stale state.
    original_name = Column(String, nullable=True)
    # ``removed_word_indices`` is the merchant's chip-edit set.
    # SQLite tests use it as JSON via SA's portable JSON layer;
    # Postgres stores it as JSONB.
    removed_word_indices = Column(JSONB, nullable=True)
    cleared = Column(Boolean, default=False, nullable=False)
    # ``status`` — "edited" or "skipped". We never write a "pending"
    # row; pending == no row.
    status = Column(String, nullable=False, default='edited')
    actor_user_id = Column(Integer, ForeignKey('users.id'), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    tenant = relationship('Tenant')
    customer = relationship('Customer')


class CustomerNameAuditLog(Base):
    """Row-level audit trail for the bulk customer-name cleanup tool.

    One row per ``Customer.name`` mutation triggered by the
    "تنظيف أسماء العملاء" button on the customers page. The cleanup
    is destructive — once applied, the previous value is overwritten
    on ``customers.name`` — so this table is the only place to look
    when a merchant asks "what did my customer's name USED to be?".

    Scope:
      * Always tenant-scoped: the cleanup endpoint refuses to mutate
        customers belonging to a different tenant, and rows here
        always carry the requesting tenant_id.
      * ``new_name`` is nullable because a high-confidence clean
        verdict can be "clear the row" (phone-only, pure noise,
        religious phrase). Empty-string and ``NULL`` mean the same
        thing on read; we store ``NULL`` for clarity.
      * ``reason`` is the Arabic explanation shown in the preview
        modal — kept verbatim so support can quote it back.
    """
    __tablename__ = 'customer_name_audit_logs'
    __table_args__ = (
        Index(
            'ix_customer_name_audit_tenant_created',
            'tenant_id', 'created_at',
        ),
        Index(
            'ix_customer_name_audit_customer',
            'customer_id',
        ),
    )
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False)
    customer_id = Column(Integer, ForeignKey('customers.id'), nullable=False)
    old_name = Column(String, nullable=True)
    new_name = Column(String, nullable=True)
    reason = Column(String, nullable=True)
    confidence = Column(String, nullable=True)   # "high" | "low"
    actor_user_id = Column(Integer, ForeignKey('users.id'), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    tenant = relationship('Tenant')
    customer = relationship('Customer')


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
    # Nahla display & management (migration 0035)
    display_name_ar = Column(String, nullable=True)              # human-readable Arabic name shown to merchant
    service_key = Column(String, nullable=True)                  # maps to SERVICE_CATALOG (e.g. cart_recovery)
    nahla_source_key = Column(String, nullable=True)             # original Nahla library key used at import
    is_active = Column(Boolean, default=True, nullable=False)    # active within Nahla (can be toggled)
    is_hidden = Column(Boolean, default=False, nullable=False)   # hidden from merchant's template list
    step_number = Column(Integer, nullable=True)                 # sequence step (multi-step flows like cart recovery)
    has_coupon = Column(Boolean, default=False, nullable=True)   # template includes a coupon/discount code
    trigger_delay_hours = Column(Float, nullable=True)           # delay before auto-send (hours)


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
    # ── Send strategy (Wave/Batch architecture, Phase: Meta-aware pacing) ───
    # ``immediate`` — legacy behaviour: all recipients dispatched in one
    #                  background thread, paced only by the in-process
    #                  inter-message delay. Default for small campaigns.
    # ``batched``   — explicit wave plan provided by the merchant
    #                  (``batch_size`` + ``delay_between_batches_sec``).
    #                  One ``CampaignWave`` row per wave.
    # ``adaptive``  — Nahla computes the wave plan automatically from
    #                  the current Quality Score / Meta tier (see
    #                  ``services/wave_scheduler.compute_adaptive_strategy``).
    #                  ``batch_size`` / ``delay_between_batches_sec`` are
    #                  populated at plan time so the merchant sees the
    #                  resolved values, not just the strategy name.
    send_strategy = Column(String, default='immediate', nullable=False)
    batch_size = Column(Integer, nullable=True)
    delay_between_batches_sec = Column(Integer, nullable=True)
    # ``waves`` relationship populated when ``send_strategy != 'immediate'``.
    # See ``CampaignWave`` below.

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    launched_at = Column(DateTime, nullable=True)


class CampaignWave(Base):
    """One scheduled batch (wave) of a campaign send.

    Why this exists
    ───────────────
    The legacy dispatcher loop (``dispatch_campaign``) already paces
    individual sends inside one process. But Meta's published
    guidance for marketing messages explicitly recommends staggering
    larger sends to protect number reputation — and inviting the
    merchant to pause / resume between batches once they see
    delivery rates. Doing that inside the in-memory loop is
    impossible: the process can restart, the merchant has no UI
    to inspect "wave 2 of 8", and we can't react to a tier drop
    mid-send.

    So we elevate "batch" from a private loop variable to a
    first-class persisted concept. Every wave has its own row,
    its own scheduled time, its own counters, and can be paused
    or skipped independently. The wave scheduler
    (``core/scheduler.run_campaign_wave_scheduler``) wakes up
    periodically and dispatches whichever waves are due, reusing
    the existing ``dispatch_campaign`` pipeline — no rewrite.

    Small campaigns
    ───────────────
    Campaigns under ``WAVE_THRESHOLD_RECIPIENTS`` (default 500)
    deliberately do NOT get wave rows. They use the historic
    immediate path. We do not want to add operational complexity
    for a coffee shop sending to 80 customers.

    Wave membership of a recipient
    ──────────────────────────────
    Each ``CampaignSendLog`` row optionally carries
    ``wave_id``. NULL = belongs to a campaign that was sent
    immediately (or to a wave-mode campaign's pre-snapshot
    skipped rows). Populated = the wave scheduler will pick it
    up when that wave becomes due.
    """

    __tablename__ = 'campaign_waves'
    __table_args__ = (
        Index('ix_campaign_waves_due', 'status', 'scheduled_at'),
        Index('ix_campaign_waves_campaign', 'campaign_id', 'wave_index'),
        UniqueConstraint(
            'campaign_id', 'wave_index',
            name='uq_campaign_waves_campaign_index',
        ),
    )

    id = Column(Integer, primary_key=True)
    campaign_id = Column(
        Integer, ForeignKey('campaigns.id', ondelete='CASCADE'),
        nullable=False,
    )
    # Denormalised for tenant-scoped queries without a JOIN to ``campaigns``.
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False)

    # 1-based for display ("الدفعة 2 من 8"). ``total_waves`` is
    # denormalised onto every wave row so a single row carries the
    # information the UI needs to render its "wave N of M" label
    # without a count query.
    wave_index = Column(Integer, nullable=False)
    total_waves = Column(Integer, nullable=False)

    # ``pending``      — not yet due. The scheduler ignores these.
    # ``dispatching``  — picked up by the scheduler; the dispatcher
    #                    is currently iterating over its rows.
    # ``completed``    — every queued row in this wave has terminal
    #                    state (sent / failed / skipped_*).
    # ``failed``       — the wave's dispatcher run raised before
    #                    completing. Operator inspection required.
    # ``paused``       — merchant explicitly paused; scheduler skips.
    # ``cancelled``    — wave never runs (e.g. tenant cancelled the
    #                    rest of a partially-sent campaign).
    status = Column(String, default='pending', nullable=False)

    # When this wave is supposed to start. Set at plan time
    # (now + wave_index * delay_between_batches_sec).
    scheduled_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    # Planned at materialisation. Sent/failed are populated by the
    # dispatcher as it works through this wave's queued rows so the
    # UI can render "1,247 / 2,000 sent" without aggregating logs.
    planned_recipients = Column(Integer, default=0, nullable=False)
    sent_count = Column(Integer, default=0, nullable=False)
    failed_count = Column(Integer, default=0, nullable=False)

    # When the wave plan was created we record the strategy that
    # produced it. Useful for analytics ("how often does adaptive
    # pick small batches?") and for the wave-detail view.
    plan_strategy = Column(String, nullable=True)
    plan_rationale = Column(Text, nullable=True)

    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


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


class GovernorSendLog(Base):
    """
    سجل إرسال حقيقي يستخدمه Global Send Governor لحساب الحدود (Limits) بدقة.
    صف واحد = رسالة واحدة تم إرسالها فعلياً من أي خدمة طيار آلي.
    """
    __tablename__ = 'governor_send_logs'
    id             = Column(Integer, primary_key=True)
    tenant_id      = Column(Integer, ForeignKey('tenants.id'), nullable=False)
    customer_id    = Column(Integer, ForeignKey('customers.id'), nullable=False)
    automation_type = Column(String, nullable=False)
    execution_id   = Column(Integer, ForeignKey('automation_executions.id'), nullable=True)
    sent_at        = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        # Index سريع لاستعلامات الحدود (per-customer per-tenant)
        Index('ix_gov_log_tenant_cust_sent', 'tenant_id', 'customer_id', 'sent_at'),
    )


class CampaignSendLog(Base):
    """Per-recipient idempotency log for manual marketing campaigns.

    One row per ``(campaign_id, customer_phone_e164)`` — enforced by a
    unique constraint at the DB level. The dispatcher inserts a snapshot
    row in ``status='queued'`` for every recipient *before* contacting
    Meta, then transitions each row through ``sending → sent / failed /
    skipped_*``. A row in ``status='sent'`` is the source of truth that
    a recipient already received this campaign, even if the dispatcher
    later crashes or is restarted — the snapshot insert is a NO-OP for
    rows that already exist (``ON CONFLICT DO NOTHING``).

    Frequency cap (default 14 days) is implemented by a single query:
    *"is there any row for this tenant + this phone with status='sent'
    and sent_at >= now() - interval '14 days'?"*. If yes, the new row
    flips to ``status='skipped_duplicate'`` and the campaign report
    surfaces it under ``skipped_duplicate``.

    Scope: this log only covers manual marketing campaigns dispatched
    via :func:`services.campaign_dispatcher.dispatch_campaign`. Cart
    recovery (``core/automation_engine``), order messages, generic
    automations, and 24h-service replies use their own audit trails.
    """
    __tablename__ = 'campaign_send_logs'

    # ``BigInteger`` is the right call for production Postgres (the
    # send log can grow very fast for active merchants). On SQLite —
    # used by the test suite — BIGINT does NOT alias ROWID, so
    # autoincrement breaks unless we fall back to plain INTEGER.
    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False)
    campaign_id = Column(Integer, ForeignKey('campaigns.id', ondelete='CASCADE'), nullable=False)
    customer_id = Column(Integer, ForeignKey('customers.id'), nullable=True)
    customer_phone_e164 = Column(String, nullable=False)
    template_name = Column(String, nullable=True)
    template_language = Column(String, nullable=True)
    payload_hash = Column(String(64), nullable=True)
    status = Column(String(32), nullable=False, default='queued')
    provider_message_id = Column(String, nullable=True)
    error_code = Column(String, nullable=True)
    error_message = Column(Text, nullable=True)
    skip_reason = Column(String(64), nullable=True)
    attempt_count = Column(Integer, nullable=False, default=0)
    sent_at = Column(DateTime, nullable=True)
    # Per-recipient delivery tracking populated by the WhatsApp status
    # webhook (see `_handle_message_status`). Each is independently
    # nullable:
    #   * `delivered_at` — Meta delivered to the customer's device.
    #   * `read_at`      — customer opened the chat / read receipt.
    #   * `failed_at`    — Meta reported failure AFTER initially
    #                      accepting the message ("failed_after_accept").
    # All three NULL = "unknown delivery" (e.g. Meta never echoed
    # back a status, or the row is from before this column existed).
    delivered_at = Column(DateTime, nullable=True)
    read_at      = Column(DateTime, nullable=True)
    failed_at    = Column(DateTime, nullable=True)
    # Optional wave membership — populated when the parent campaign
    # uses ``send_strategy != 'immediate'``. NULL means the row
    # belongs to the legacy immediate path. The wave-aware dispatch
    # query filters on this so each wave only dispatches its own
    # slice. ON DELETE SET NULL keeps the row alive if the wave is
    # removed (e.g. campaign cancelled) — we still want the
    # idempotency anchor.
    wave_id = Column(
        Integer,
        ForeignKey('campaign_waves.id', ondelete='SET NULL'),
        nullable=True,
    )
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        # Idempotency: every (campaign, phone) tuple appears at most once.
        # Snapshot inserts use this to skip recipients that were already
        # captured on a prior dispatch attempt.
        Index(
            'uq_campaign_send_log_campaign_phone',
            'tenant_id', 'campaign_id', 'customer_phone_e164',
            unique=True,
        ),
        # Frequency-cap lookup: per-tenant, per-phone, status-filtered.
        Index(
            'ix_campaign_send_log_tenant_phone_status_sent',
            'tenant_id', 'customer_phone_e164', 'status', 'sent_at',
        ),
        # Report aggregation: COUNT(*) GROUP BY status WHERE campaign_id=?
        Index('ix_campaign_send_log_campaign_status', 'campaign_id', 'status'),
        # Status webhook lookup: attribute incoming Meta status events
        # back to the right send-log row by provider_message_id.
        # Without this, every status webhook becomes a full scan.
        Index('ix_campaign_send_log_provider_message_id', 'provider_message_id'),
        # Wave-aware dispatch: the per-wave dispatcher pulls
        # ``WHERE wave_id=? AND status='queued'`` once per tick.
        # Without a composite index this scans the full campaign.
        Index('ix_campaign_send_log_wave_status', 'wave_id', 'status'),
    )


class CustomerSegmentManual(Base):
    """Merchant-curated link between a Customer and a *Nahla official*
    marketing cohort (vip, new, unsubscribed, …).

    Crucially, ``segment_key`` is NOT a free-form tag — the API layer
    validates every insert against ``services.nahla_segments.SEGMENTS``
    so merchants can only pin customers to cohorts that exist in the
    canonical registry. Anything else (including a typo) returns 422.

    The table coexists with the auto-classifier output stored on
    ``CustomerProfile`` (``customer_status`` / ``rfm_segment``). A
    customer can simultaneously be auto-classified as ``new`` AND
    manually tagged as ``vip`` — the campaign snapshot honours both
    sources via UNION semantics so a "VIP" campaign reaches both groups.

    The unique index on ``(tenant_id, customer_id, segment_key)`` makes
    a re-tag a no-op, which lets the API endpoint be safely retried
    on flaky networks.
    """
    __tablename__ = 'customer_segments_manual'

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False)
    customer_id = Column(Integer, ForeignKey('customers.id', ondelete='CASCADE'), nullable=False)
    segment_key = Column(String(64), nullable=False)
    source = Column(String(16), nullable=False, default='manual')
    # ``include`` (default) pins the customer to the segment.
    # ``exclude`` hides them from segment-membership queries even
    # when the auto classifier matched. Filter formula is
    #   member ⇔ (auto_match ∨ manual_include) ∧ ¬ manual_exclude
    # See migration 0053.
    mode = Column(String(16), nullable=False, default='include')
    created_by = Column(Integer, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        Index(
            'uq_customer_segments_manual_tenant_customer_segment',
            'tenant_id', 'customer_id', 'segment_key',
            unique=True,
        ),
        Index(
            'ix_customer_segments_manual_tenant_segment',
            'tenant_id', 'segment_key',
        ),
        Index(
            'ix_customer_segments_manual_tenant_segment_mode',
            'tenant_id', 'segment_key', 'mode',
        ),
        Index(
            'ix_customer_segments_manual_customer',
            'customer_id',
        ),
    )


class NotificationLog(Base):
    """
    سجل بسيط لكل إشعار يُرسَل أو يُتجاهَل.

    يُستخدم لـ:
    1. منع إرسال إيميلات متكررة (spam throttle).
    2. عرض سجل الإشعارات للتاجر في لوحة التحكم.
    3. تشخيص سبب عدم إرسال إشعار.

    Rules:
    - type: 'email' | 'in_app' | 'sms'
    - event: 'new_whatsapp_message' | 'returning_customer' | 'new_order' | 'support_request'
    - status: 'sent' | 'skipped'
    - reason: optional human-readable reason (Arabic) stored when status='skipped'
    """
    __tablename__ = 'notification_logs'
    id          = Column(Integer, primary_key=True)
    tenant_id   = Column(Integer, ForeignKey('tenants.id'), nullable=False)
    customer_id = Column(Integer, ForeignKey('customers.id'), nullable=True)
    type        = Column(String(20), nullable=False)     # email | in_app
    event       = Column(String(60), nullable=False)     # new_whatsapp_message | ...
    status      = Column(String(10), nullable=False)     # sent | skipped
    reason      = Column(String(255), nullable=True)     # Arabic reason for skip
    details     = Column(JSONB, nullable=True)           # extra context (phone, preview, ...)
    created_at  = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index('ix_notif_log_tenant_created', 'tenant_id', 'created_at'),
        Index('ix_notif_log_tenant_cust_event', 'tenant_id', 'customer_id', 'event'),
    )


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

    # Per-endpoint receipt (360dialog multi-URL setup) — avoids rewriting JSONB on every ping.
    webhook_coexistence_received_at = Column(DateTime(timezone=True), nullable=True)
    webhook_status_received_at = Column(DateTime(timezone=True), nullable=True)

    # Inbound messages with WhatsApp business timestamp *before* this instant are
    # historical-only: persisted for inbox visibility but MUST NOT run Brain / AI.
    # Set once when the integration first reaches ``connected`` (NULL → now).
    # Advanced only via explicit merchant/admin reset endpoint.
    whatsapp_ai_live_since = Column(DateTime(timezone=True), nullable=True)

    # Bulk WhatsApp history import bookkeeping (explicit sync phase).
    # pending | syncing | completed | failed — default ``completed`` keeps legacy behaviour.
    whatsapp_history_sync_status = Column(String, nullable=False, server_default="completed")
    history_sync_started_at = Column(DateTime(timezone=True), nullable=True)
    history_sync_completed_at = Column(DateTime(timezone=True), nullable=True)
    synced_conversations_count = Column(Integer, nullable=False, server_default="0")
    synced_messages_count = Column(Integer, nullable=False, server_default="0")

    # Meta account health (fetched periodically from Graph API)
    meta_messaging_limit = Column(String, nullable=True)     # e.g. "TIER_1K", "TIER_10K", "TIER_100K", "UNLIMITED"
    meta_quality_rating  = Column(String, nullable=True)     # "GREEN", "YELLOW", "RED"
    meta_tier_updated_at = Column(DateTime, nullable=True)

    # ── Meta WhatsApp Catalog identity (migration 0061) ───────────────────
    # The Meta Commerce Catalog id linked to this WABA. Every
    # ``interactive.type = "product"`` / ``"product_list"`` payload carries
    # it inside ``action.catalog_id``. Per-WABA (one catalog can back
    # many phone numbers), so we store it on the connection — not on
    # TenantSettings. NULL means "merchant hasn't linked a catalog yet";
    # the catalog sender will fall back to the legacy image+CTA path.
    meta_catalog_id = Column(String(255), nullable=True)
    # Per-connection kill-switch for catalog message sending. When False
    # (the default) the catalog sender silently degrades to the legacy
    # image+CTA path even if ``meta_catalog_id`` is populated — useful
    # when a merchant wants to pause catalog rendering without losing
    # the catalog binding. Plan-level gating
    # (``PlanFeatures.meta_catalog_sync``) is enforced separately.
    catalog_enabled = Column(
        Boolean, nullable=False, server_default=sa.text("false"),
    )

    # Meta catalog import diagnostics (migration 0071) — operational
    # visibility only; does not affect send / AI resolution paths.
    meta_import_status       = Column(String(32), nullable=True)
    meta_import_last_at      = Column(DateTime(timezone=True), nullable=True)
    meta_import_last_error   = Column(Text, nullable=True)
    meta_import_last_report  = Column(JSONB, nullable=True)
    meta_import_token_source = Column(String(64), nullable=True)

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


class AiQualityEvent(Base):
    """Per-turn answer-alignment mismatch event (May 2026 #12).

    Append-only audit trail written by the brain pipeline whenever
    ``modules.ai.brain.postprocess.answer_alignment.check_alignment``
    detects a reply that does not actually answer the customer's
    last message. Powers the in-product "AI Quality Monitor" so
    merchants can see misclassifications in their own dashboard
    instead of grepping Railway logs.

    Privacy contract:
      * ``customer_phone`` stores a MASKED form (e.g. ``+9665***430``).
        Never write the full E.164 number here — full numbers live on
        ``conversations.customer_id → customers.phone`` only.
      * ``inbound_preview`` / ``reply_preview`` are truncated to 200
        chars. Full bodies live on ``message_events``.
      * ``resolved_status`` is one of: ``open`` (default), ``reviewed``,
        ``ignored``, ``fixed``.
    """
    __tablename__ = 'ai_quality_events'
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False, index=True)
    conversation_id = Column(Integer, ForeignKey('conversations.id'), nullable=True, index=True)
    # ── Privacy-safe identifiers ────────────────────────────────────
    customer_phone_masked = Column(String, nullable=False, index=True)
    # ── Event family (May 2026 #22 — pre-brain visibility) ──────────
    # Before this column existed, the table only held brain-side
    # answer-alignment mismatches and the owner dashboard showed
    # all-zeros whenever the failure happened BEFORE the brain ran
    # (unsupported message types, empty text after normalize, 360dialog
    # routing failures, dispatcher exceptions). Adding ``category`` lets
    # the same table audit those silent drops too, without forking the
    # dashboard or duplicating the triage workflow. Legacy rows default
    # to ``ai_mismatch`` via the server default in migration 0070, so
    # the historical reader is unaffected.
    #
    # Allowed values (string, NOT an enum — we want to add new ones
    # without a migration):
    #   * ``ai_mismatch``       — the original use case (alignment fail)
    #   * ``inbound_drop``      — silent drop in
    #     ``routers/whatsapp_webhook._dispatch_message`` /
    #     ``_handle_merchant_message``
    #   * ``webhook_routing``   — 360dialog / Meta unrouted webhook
    #   * ``media_failure``     — reserved for a follow-up if needed
    category = Column(
        String(32),
        nullable=False,
        default='ai_mismatch',
        server_default=sa.text("'ai_mismatch'"),
        index=True,
    )
    # ── Mismatch classification ─────────────────────────────────────
    # For ``category='ai_mismatch'``  this is one of the legacy values
    #   (``question_to_social``, ``delivery_to_receipt``, ...).
    # For ``category='inbound_drop'`` this is the drop kind
    #   (``unsupported_type``, ``empty_text``,
    #    ``pre_brain_handoff_drop``, ``dispatcher_exception``).
    # For ``category='webhook_routing'`` this is the unrouted sub-reason
    #   (``unrouted_missing_phone_id``, ``unrouted_unknown_phone_id``,
    #    ``unrouted_ambiguous``, ``unrouted_wrong_provider``,
    #    ``unrouted_bad_secret``).
    mismatch_type = Column(String, nullable=False, index=True)
    mismatch_reason = Column(Text, nullable=True)
    # ── Brain context snapshot ──────────────────────────────────────
    detected_intent = Column(String, nullable=True)
    social_category = Column(String, nullable=True)
    action_taken = Column(String, nullable=True)
    chosen_path = Column(String, nullable=True)
    fallback_used = Column(Boolean, nullable=True, default=False)
    order_status = Column(String, nullable=True)
    awaiting_payment_receipt = Column(Boolean, nullable=True, default=False)
    model_used = Column(String, nullable=True)
    turn = Column(Integer, nullable=True)
    # ── Truncated content (privacy-safe) ────────────────────────────
    inbound_preview = Column(Text, nullable=True)
    reply_preview = Column(Text, nullable=True)
    # ── Validator outcome (mirrors AlignmentResult) ─────────────────
    alignment_passed = Column(Boolean, nullable=False, default=False)
    regen_fired = Column(Boolean, nullable=False, default=False)
    # ── Operator triage state ───────────────────────────────────────
    resolved_status = Column(String, nullable=False, default='open', index=True)
    resolved_by = Column(String, nullable=True)
    resolved_at = Column(DateTime, nullable=True)
    resolved_note = Column(Text, nullable=True)
    # ── Append-only timestamps ──────────────────────────────────────
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    tenant = relationship('Tenant')


class AIUsageEvent(Base):
    """One row per LLM call — token counts and USD cost, no message content."""
    __tablename__ = "ai_usage_events"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    store_id = Column(Integer, nullable=True)
    conversation_id = Column(Integer, nullable=True)
    turn_id = Column(Integer, nullable=True)
    provider = Column(String(64), nullable=False)
    model = Column(String(128), nullable=False)
    reason = Column(String(128), nullable=False)
    input_tokens = Column(Integer, nullable=True)
    output_tokens = Column(Integer, nullable=True)
    cache_read_tokens = Column(Integer, nullable=True)
    cache_write_tokens = Column(Integer, nullable=True)
    estimated_input_tokens = Column(Integer, nullable=True)
    estimated_output_tokens = Column(Integer, nullable=True)
    token_source = Column(String(16), nullable=False)  # actual | estimated
    input_cost_usd = Column(Numeric(18, 8), nullable=True)
    output_cost_usd = Column(Numeric(18, 8), nullable=True)
    cache_cost_usd = Column(Numeric(18, 8), nullable=True)
    total_cost_usd = Column(Numeric(18, 8), nullable=True)
    pricing_version = Column(String(32), nullable=True)
    request_id = Column(String(128), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    tenant = relationship("Tenant")


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
    # Outcome columns — written by outcome_tracker when Salla fires the
    # confirmation webhook, not at turn-write time.
    order_confirmed = Column(Boolean, nullable=True, default=False)
    coupon_redeemed = Column(Boolean, nullable=True, default=False)
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


# ── Cross-merchant anonymized learning signals ───────────────────────────────
#
# This table is INTENTIONALLY NOT tenant-scoped.  It only stores anonymized
# categorical / bucketed signals derived from many merchants' turns by the
# CrossMerchantLearningStore writer.  No raw tenant_id, customer_id, phone,
# message text, product titles or money values are ever persisted here.
#
# Tenants are represented by ``tenant_hash`` — a salted SHA-256 truncation
# produced by ``modules.ai.security.trace_schema.anonymize_tenant``.
class CrossMerchantSignal(Base):
    __tablename__ = 'cross_merchant_signals'
    __table_args__ = (
        Index('ix_xms_industry_action', 'industry', 'action'),
        Index('ix_xms_action_outcome', 'action', 'outcome'),
        Index('ix_xms_tier_industry', 'tier', 'industry'),
        Index('ix_xms_created_at', 'created_at'),
    )

    id           = Column(Integer, primary_key=True)
    tenant_hash  = Column(String(64), nullable=False, index=True)
    industry     = Column(String(64), nullable=False, default='unknown')
    intent       = Column(String(64), nullable=False, default='unknown')
    action       = Column(String(64), nullable=False, default='unknown')
    ui_mode      = Column(String(32), nullable=False, default='unknown')
    outcome      = Column(String(32), nullable=False, default='unknown')
    value_bucket = Column(String(32), nullable=False, default='unknown')
    turn_index   = Column(Integer, nullable=False, default=0)
    model_path   = Column(String(32), nullable=False, default='rule')
    latency_ms   = Column(Integer, nullable=False, default=0)
    tier         = Column(String(16), nullable=False, default='global')
    extra        = Column(JSONB, nullable=True)
    created_at   = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


# ── Learned cross-merchant sales policies (Phase 1.7) ────────────────────────
#
# Output of the ``PolicyLearner`` that aggregates ``cross_merchant_signals``
# into recommended (action, ui_mode) per (intent[, industry]).
#
# Anti-leak guarantees mirror ``CrossMerchantSignal``:
#   * No tenant_id / customer_id columns.
#   * No raw text — only categorical labels already validated by the
#     anonymized trace schema.
#   * ``industry == "*"`` represents the GLOBAL tier (cross-vertical).
#
# Uniqueness on (scope, industry, intent) lets the learner UPSERT in a
# single statement and lets the runtime store look up by composite key
# without scanning.
class LearnedSalesPolicy(Base):
    __tablename__ = 'learned_sales_policies'
    __table_args__ = (
        UniqueConstraint('scope', 'industry', 'intent', name='uq_lsp_scope_industry_intent'),
        Index('ix_lsp_intent', 'intent'),
        Index('ix_lsp_industry_intent', 'industry', 'intent'),
    )

    id                 = Column(Integer, primary_key=True)
    scope              = Column(String(16), nullable=False, default='global')   # global | vertical
    industry           = Column(String(64), nullable=False, default='*')        # '*' for global
    intent             = Column(String(64), nullable=False, default='unknown')
    recommended_action = Column(String(64), nullable=False, default='unknown')
    recommended_ui     = Column(String(32), nullable=False, default='unknown')
    confidence         = Column(Float, nullable=False, default=0.0)
    sample_size        = Column(Integer, nullable=False, default=0)
    extra              = Column(JSONB, nullable=True)
    updated_at         = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class SallaTrialLedger(Base):
    """
    Permanent, tenant-agnostic ledger of Salla free-trial usage per store.

    Survives tenant/integration deletion intentionally — never hard-deleted.
    Guarantees one free trial per salla_store_id across all time, even if
    the merchant deletes the app and reinstalls.
    """
    __tablename__ = "salla_trial_ledger"
    __table_args__ = (
        UniqueConstraint("salla_store_id", name="uq_salla_trial_ledger_store_id"),
    )

    id                     = Column(Integer, primary_key=True)
    salla_store_id         = Column(String, nullable=False, index=True)
    merchant_id            = Column(String, nullable=True)   # owner email or store_id
    trial_used             = Column(Boolean, default=True, nullable=False)
    first_trial_started_at = Column(DateTime(timezone=True), nullable=True)
    first_trial_plan       = Column(String, nullable=True)
    source                 = Column(String, default="salla", nullable=False)
    created_at             = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at             = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class ManualCoupon(Base):
    """Merchant-curated coupon code the AI can cite verbatim.

    Independent of automatic coupon generators and Salla integration —
    works even for merchants selling manually over WhatsApp only.
    """

    __tablename__ = "manual_coupons"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_manual_coupons_tenant_code"),
        Index("ix_manual_coupons_tenant_active_priority", "tenant_id", "is_active", "priority"),
    )

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    code = Column(String(64), nullable=False)
    title = Column(String(255), nullable=True)
    description = Column(Text, nullable=True)
    discount_text = Column(String(255), nullable=True)
    usage_context = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True, nullable=False, server_default="true")
    priority = Column(Integer, default=100, nullable=False, server_default="100")
    starts_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


# ──────────────────────────────────────────────────────────────────────
# Delivery Quality Intelligence Layer (May 2026)
# ──────────────────────────────────────────────────────────────────────
#
# Four new tables form the data backbone of the platform-wide
# deliverability layer. They are intentionally append-only or
# upsert-only — no UPDATE-in-place destruction of history — because
# the entire point is to let us reconstruct *why* a WABA's quality
# drifted, not just observe the current state.
#
# Read order: WaWebhookRaw → MessageDeliveryEvent → CustomerSuppression
# → WaNumberQualitySnapshot. Each one builds on signal from the prior.


class WaWebhookRaw(Base):
    """Raw archive of every WhatsApp / 360dialog webhook payload.

    Why a separate table (not just ``WebhookEvent``)
    ────────────────────────────────────────────────
    ``WebhookEvent`` exists already but is exclusively used by Salla /
    Zid integrations and carries their FSM (received → processing →
    processed | failed | dead_letter) — wiring WhatsApp into it would
    conflate two retry policies and two operator dashboards. Instead
    this table is **observability-only**: we never retry from it, we
    never delete from it, we never block on it. The webhook handler
    inserts and moves on.

    What's in it
    ────────────
    Everything we need to replay or audit a delivery later: the raw
    body, the raw headers, the parsed wamid for indexing, the status
    (``sent`` / ``delivered`` / ``read`` / ``failed`` / template
    status / coexistence event), and the raw Meta error code + subcode
    if present. The classifier output (``classified_key`` +
    ``quality_tier``) is denormalised so we can run aggregate queries
    without re-running the regex chain.

    Retention
    ─────────
    No TTL at the DB layer — 360dialog/Meta delivery debugging often
    needs months of history. A separate background job will eventually
    move rows older than ~180 d to cold storage; until then keep an
    eye on table size via the ``ix_wa_webhook_raw_received_at`` index.
    """

    __tablename__ = "wa_webhook_raw"
    __table_args__ = (
        Index(
            "ix_wa_webhook_raw_received_at",
            "received_at",
        ),
        Index(
            "ix_wa_webhook_raw_tenant_received",
            "tenant_id", "received_at",
        ),
        # Looking up by wamid is the single hottest path — it's how
        # the dispatcher reconciles ``CampaignSendLog`` rows with
        # later status callbacks ("did this wamid eventually fail?").
        Index(
            "ix_wa_webhook_raw_wamid",
            "wamid",
        ),
        Index(
            "ix_wa_webhook_raw_classified_key",
            "classified_key",
        ),
    )

    id = Column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True, autoincrement=True,
    )
    # Nullable: some 360dialog channel/coexistence events don't carry
    # enough context to attribute to a tenant. We keep them anyway so
    # ops can debug; the analytics queries always filter `tenant_id
    # IS NOT NULL`.
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    # Provider that delivered this webhook. ``"meta"`` = direct Cloud
    # API webhook, ``"360dialog"`` = 360dialog single-URL or its
    # status-only / coexistence subpaths, ``"meta_legacy"`` = the
    # pre-360dialog Meta-direct path still wired for older tenants.
    provider = Column(String(32), nullable=False)
    # The HTTP path the webhook landed on — gives us a clean
    # secondary key when 360dialog rolls out a new subpath without
    # warning ("hey why is /webhook/whatsapp/360dialog/quality
    # suddenly emitting events?").
    source_path = Column(String(255), nullable=True)
    # The parsed ``wamid`` if this payload was a status event for a
    # specific outbound message we sent. NULL for inbound messages
    # and 360dialog coexistence/channel events.
    wamid = Column(String(255), nullable=True)
    # Coarse status family: ``"sent"`` | ``"delivered"`` | ``"read"``
    # | ``"failed"`` | ``"template_status"`` | ``"inbound"`` |
    # ``"coexistence"`` | ``"channel"`` | ``"other"``. NEVER NULL —
    # at minimum we know what kind of payload it is.
    status = Column(String(32), nullable=False)
    # Raw Meta error code as Meta surfaced it (e.g. 131026). Keep
    # as String so we don't lose the long-tail of 7+ digit codes
    # and so ``"unknown"`` from our own dispatcher round-trips.
    raw_error_code = Column(String(32), nullable=True)
    raw_error_subcode = Column(String(32), nullable=True)
    # Output of ``meta_errors.classify_meta_error`` at the moment
    # of ingestion. Stored so analytics queries don't need to
    # re-run the classifier across millions of historical rows.
    classified_key = Column(String(64), nullable=True)
    # Output of ``meta_errors.quality_tier_of`` for the classified
    # key — denormalised for fast roll-ups.
    quality_tier = Column(String(16), nullable=True)
    # The full payload, exactly as Meta/360dialog sent it. Stored as
    # text (not JSONB) because some 360dialog coexistence events
    # ship as form-encoded blobs that aren't valid JSON, and we
    # never want the ingest path to fail on a parse error.
    raw_body = Column(Text, nullable=True)
    raw_headers = Column(JSONB, nullable=True)
    # If we DID manage to parse the body, the structured form. NULL
    # when ``raw_body`` is non-JSON.
    parsed_payload = Column(JSONB, nullable=True)
    # Best-effort attribution: which campaign send / automation
    # execution did this event resolve against? Both nullable
    # because not every webhook ties back to one of our rows
    # (e.g. inbound messages, coexistence sync).
    campaign_send_log_id = Column(Integer, nullable=True)
    automation_execution_id = Column(Integer, nullable=True)
    received_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class MessageDeliveryEvent(Base):
    """Append-only per-status delivery event.

    Why this exists alongside ``CampaignSendLog``
    ─────────────────────────────────────────────
    ``CampaignSendLog`` has ``delivered_at`` / ``read_at`` /
    ``failed_at`` — but they're "first-occurrence" timestamps and
    they're campaign-only. We need:

    1. **Per-attempt history.** A wamid can go ``sent → failed →
       (retry) → sent → delivered`` and we want every transition.
    2. **Coverage outside campaigns.** Automation sends, manual
       conversation replies, and order events all produce status
       callbacks today; none of them get first-class delivery
       timestamps.
    3. **Quality joins.** The Quality Score & Suppression Engine
       both need to query "for tenant X, in the last 7 days, how
       many ``quality_risk`` events per phone?" — that's a cheap
       SQL aggregate against this table, vs. an expensive walk
       across multiple status JSON fields.

    One row per (wamid, status) tuple. Idempotency is enforced via
    the unique index; the webhook handler does ``INSERT … ON
    CONFLICT DO NOTHING``.
    """

    __tablename__ = "message_delivery_events"
    __table_args__ = (
        # Idempotency anchor — Meta will redeliver the same status
        # callback within seconds/minutes if the first 200 was slow.
        UniqueConstraint(
            "wamid", "status",
            name="uq_message_delivery_events_wamid_status",
        ),
        Index(
            "ix_message_delivery_events_tenant_occurred",
            "tenant_id", "occurred_at",
        ),
        Index(
            "ix_message_delivery_events_phone_occurred",
            "tenant_id", "phone_e164", "occurred_at",
        ),
        Index(
            "ix_message_delivery_events_quality_tier",
            "tenant_id", "quality_tier", "occurred_at",
        ),
        Index(
            "ix_message_delivery_events_error_code",
            "tenant_id", "error_code", "occurred_at",
        ),
    )

    id = Column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True, autoincrement=True,
    )
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)
    # The Meta-issued message id. Some events (e.g. provider-internal
    # ``no_message_id`` failures) won't have one — we synthesise
    # ``"synth:{uuid}"`` so the unique constraint still works.
    wamid = Column(String(255), nullable=False)
    phone_e164 = Column(String(32), nullable=True)
    # Status family, same vocabulary as ``WaWebhookRaw.status``.
    status = Column(String(32), nullable=False)
    # Canonical classifier key (``meta_errors.ERRORS``). NULL for
    # non-failure statuses.
    error_code = Column(String(64), nullable=True)
    error_message = Column(Text, nullable=True)
    raw_code = Column(String(32), nullable=True)
    raw_subcode = Column(String(32), nullable=True)
    # Denormalised from the classifier so suppression / dashboard
    # aggregates don't need to re-run ``classify_meta_error``.
    quality_tier = Column(String(16), nullable=True)
    suppress_on_repeat = Column(Boolean, default=False, nullable=False)
    occurred_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    # Best-effort joins — same nullability story as in WaWebhookRaw.
    campaign_send_log_id = Column(Integer, nullable=True)
    automation_execution_id = Column(Integer, nullable=True)
    template_id = Column(Integer, nullable=True)
    source = Column(String(32), nullable=False, server_default="meta")
    raw_id = Column(BigInteger, nullable=True)


class CustomerSuppression(Base):
    """First-class suppression list.

    Replaces the ad-hoc ``Customer.extra_metadata`` JSON keys
    (``is_unsubscribed``, ``marketing_opt_out_manual``, …) for any
    NEW reason — the legacy keys still source the unsubscribe
    workflow, but anything driven by Meta error codes lives here.

    A row exists only when the phone is currently suppressed. When
    the auto-reinstate logic fires (inbound message received, or
    merchant explicitly clears the list), we set ``is_active=False``
    and stamp ``reinstated_at`` — we never DELETE so support can
    still answer "why was this number suppressed last month?".

    Tenant isolation is enforced at the application layer; the
    ``(tenant_id, normalized_phone)`` unique constraint here is
    the second line of defence.
    """

    __tablename__ = "customer_suppressions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "normalized_phone",
            name="uq_customer_suppressions_tenant_phone",
        ),
        Index(
            "ix_customer_suppressions_tenant_active",
            "tenant_id", "is_active",
        ),
        Index(
            "ix_customer_suppressions_last_failure",
            "tenant_id", "last_failure_at",
        ),
    )

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=True, index=True)
    # E.164 digits-only (matches ``Customer.normalized_phone``). Keyed
    # by phone, not customer_id, so that the same number used by two
    # customer rows (rare but happens during dedup) shares the same
    # suppression.
    normalized_phone = Column(String(32), nullable=False)
    # Canonical classifier key that drove the FIRST suppression
    # event (``not_on_whatsapp``, ``blocked_by_user``, …). The full
    # history is in ``reasons``.
    reason_primary = Column(String(64), nullable=False)
    # JSON list of {"key": str, "count": int, "last_seen_at": iso}.
    # Updated in place every time a new quality_risk event lands.
    reasons = Column(JSONB, nullable=True)
    # Total count of quality_risk events that contributed to this
    # row — used by the dashboard for "top suppressed reasons".
    failure_count = Column(Integer, default=0, nullable=False)
    # Who/what created this row. ``"auto"`` = Suppression Engine
    # threshold reached, ``"manual"`` = merchant action, ``"opt_out"``
    # = unsubscribe lifecycle, ``"webhook_block"`` = single-event
    # critical (``blocked_by_user``).
    source = Column(String(32), nullable=False, server_default="auto")
    is_active = Column(Boolean, default=True, nullable=False)
    suppressed_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    last_failure_at = Column(DateTime(timezone=True), nullable=True)
    reinstated_at = Column(DateTime(timezone=True), nullable=True)
    # Why was the row reinstated? Free text + machine key.
    # ``"inbound_message"`` is the auto-reinstate path.
    reinstate_reason = Column(String(64), nullable=True)
    # Optional time-bounded suppression. NULL = indefinite (the
    # default for ``quality_risk`` codes). Set explicitly when the
    # Suppression Engine decides on a cool-down rather than a
    # permanent block (e.g. ``temporary_failure`` chain).
    expires_at = Column(DateTime(timezone=True), nullable=True)
    # JSONB for any extra context (raw error code distribution,
    # last campaign id, …) — keeps the schema stable while we
    # iterate on what the Engine wants to remember.
    extra_metadata = Column(JSONB, nullable=True)


class WaNumberQualitySnapshot(Base):
    """Historical snapshot of a WhatsApp Business number's quality.

    ``WhatsAppConnection`` already carries the **current**
    ``meta_quality_rating`` / ``meta_messaging_limit`` — but it's
    overwritten on every Meta sync, so we have no way to plot
    "quality dropped from GREEN → YELLOW on May 5, then RED on
    May 7" or to alert the merchant on the transition itself.

    One row per (connection, recalculation point). Snapshots come
    from two sources:

    1. The periodic Quality scheduler (every 30 min by default).
    2. Inline writes whenever the dispatcher observes a critical
       quality_tier event — so the trace shows the exact event
       that caused the rating drop, not just the next scheduled
       point.

    Both Meta-reported and Nahla-computed scores live on the same
    row so dashboards can render them side by side.
    """

    __tablename__ = "wa_number_quality_snapshots"
    __table_args__ = (
        Index(
            "ix_wa_quality_snap_connection_taken",
            "connection_id", "taken_at",
        ),
        Index(
            "ix_wa_quality_snap_tenant_taken",
            "tenant_id", "taken_at",
        ),
    )

    id = Column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True, autoincrement=True,
    )
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)
    connection_id = Column(Integer, nullable=False)
    taken_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    # Meta's own labels, copied verbatim — never reinterpreted.
    meta_quality_rating = Column(String(16), nullable=True)
    meta_messaging_limit = Column(String(32), nullable=True)
    # Nahla's internal 0–100 score, computed from the metrics below.
    # NULL on snapshots taken before we had enough data.
    nahla_quality_score = Column(Float, nullable=True)
    # Discretised version of ``nahla_quality_score`` — keep these
    # labels in lockstep with ``services/quality_score.py``:
    # ``"excellent" | "healthy" | "warning" | "risky" | "critical"``.
    nahla_quality_tier = Column(String(16), nullable=True)
    # The lookback window used to compute the metrics, in hours
    # (default 168 = 7d). Stored on the row so a later analyst can
    # tell whether a low score is "really" bad or just based on a
    # narrow window during a slow week.
    metrics_window_hours = Column(Integer, default=168, nullable=False)
    delivery_rate = Column(Float, nullable=True)
    read_rate = Column(Float, nullable=True)
    failure_rate = Column(Float, nullable=True)
    # Suppress rate = (auto-suppressed phones in window) / (total
    # phones messaged in window). The clearest leading indicator
    # for an imminent Meta quality drop.
    suppress_rate = Column(Float, nullable=True)
    complaint_rate = Column(Float, nullable=True)
    sample_size = Column(Integer, nullable=True)
    # Free-form raw numerator/denominator counts — handy for the
    # Quality Dashboard to show "84/3,210 failed" alongside the rate.
    raw_metrics = Column(JSONB, nullable=True)
    # If the snapshot was triggered by a specific critical event,
    # the canonical error_code goes here (``"template_paused"`` …).
    # NULL for routine scheduled snapshots.
    triggered_by = Column(String(64), nullable=True)


class AIMediaItem(Base):
    """Merchant-uploaded media the AI can attach to its WhatsApp replies.

    Each row carries enough metadata for the brain to decide *when* to
    attach it (``usage_context`` + ``tags``) and how to send it via the
    WhatsApp Cloud API (``media_type`` → image/video/document/audio).
    """

    __tablename__ = "ai_media_library"
    __table_args__ = (
        Index("ix_ai_media_library_tenant_active_priority", "tenant_id", "is_active", "priority"),
        # Stable, namespaced lookup key. NULL is allowed (legacy
        # rows + free-form merchant uploads stay relevance-ranked
        # by title/tags as before). When SET, the resolver
        # prefers a `media_key` exact match over relevance scoring
        # — that is the contract the LLM relies on for things like
        # ``[MEDIA_KEY:payment_rajhi_barcode]``.
        Index(
            "ix_ai_media_library_tenant_media_key",
            "tenant_id", "media_key",
            unique=True,
            postgresql_where=sa.text("media_key IS NOT NULL"),
            sqlite_where=sa.text("media_key IS NOT NULL"),
        ),
    )

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    media_type = Column(String(32), nullable=False, server_default="image")
    file_url = Column(Text, nullable=False)
    thumbnail_url = Column(Text, nullable=True)
    usage_context = Column(Text, nullable=True)
    tags = Column(JSONB, nullable=False, default=list, server_default="[]")
    is_active = Column(Boolean, default=True, nullable=False, server_default="true")
    priority = Column(Integer, default=100, nullable=False, server_default="100")
    storage_kind = Column(String(16), nullable=False, server_default="external")
    storage_path = Column(Text, nullable=True)
    mime_type = Column(String(128), nullable=True)
    file_size_bytes = Column(sa.BigInteger, nullable=True)
    # Stable namespaced key (e.g. ``payment_rajhi_barcode``,
    # ``product_usage_video``). Tenant-scoped unique when set,
    # NULL allowed for free-form uploads. See
    # ``services/media_key_registry.py`` for the canonical
    # registry of well-known keys + the Arabic merchant labels
    # the UI presents in the upload form.
    media_key = Column(String(64), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class PasswordSetupToken(Base):
    """Single-use, hashed token for "set your password" links.

    Issued automatically when a merchant lands in Nahla through a Salla /
    Zid OAuth flow that auto-creates a local User with a random password
    hash they cannot know. The token is emailed to the merchant; clicking
    the link routes them through ``/set-password?token=...`` in the
    dashboard which calls the backend to consume the token and set a
    local password.

    Security model
    ──────────────
    * Token is 32 random bytes encoded as 43-char base64url (256 bits).
    * Only the SHA-256 hash is stored; the raw value is sent in the email
      and never persisted. A DB leak therefore cannot be replayed.
    * ``used_at`` enforces single-use — a consumed token cannot be reused
      even if intercepted.
    * ``expires_at`` defaults to 7 days for "welcome" purpose and 1 hour
      for "reset" purpose (caller decides). The verifier rejects expired
      rows even if ``used_at`` is null.
    * One active (non-used, non-expired) token per (user, purpose) at
      a time — issuing a new one invalidates prior unconsumed tokens for
      that user+purpose. Prevents inbox-spray flow confusion.
    * Indexed ``token_hash`` column for O(1) verification lookup.
    """
    __tablename__ = 'password_setup_tokens'

    id          = Column(Integer, primary_key=True)
    user_id     = Column(
        Integer,
        ForeignKey('users.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    # SHA-256 of the raw token value, hex-encoded (64 chars).
    token_hash  = Column(String(64), nullable=False, unique=True, index=True)
    purpose     = Column(String(32), nullable=False, default='welcome')  # welcome | reset
    expires_at  = Column(DateTime(timezone=True), nullable=False)
    used_at     = Column(DateTime(timezone=True), nullable=True)
    created_at  = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    # Audit metadata captured at issue time. Never includes the raw token.
    issued_via  = Column(String(64), nullable=True)   # e.g. "salla_oauth", "manual_admin"
    consumed_ip = Column(String(64), nullable=True)   # filled at consume time


# ── Smart Store Knowledge Hub (Phase 1) ─────────────────────────────────────
#
# Structured replacement for the legacy free-form
# ``ai_settings.manual_knowledge_base`` blob. Each row is a single
# fact/policy/note the merchant wants the AI to know about. Sections are
# grouped by ``kind`` (see ``services/knowledge_section_kinds.py`` for the
# canonical registry), and can attach any number of ``AIMediaItem`` rows
# via the M2M ``MerchantKnowledgeMedia`` link table — so a payment policy
# can carry the bank-transfer barcode image, a product-usage tip can
# carry a tutorial video, etc.
#
# The legacy text field stays untouched for backward compatibility; the
# ``/knowledge/sections/migrate-from-legacy`` endpoint moves it across on
# first use of the redesigned page.


class MerchantKnowledgeSection(Base):
    """One curated piece of merchant knowledge (policy, fact, note, …).

    Together these rows form the merchant-side facts surface the AI
    cites in its WhatsApp replies. Source-of-truth precedence is
    enforced in :mod:`backend.modules.ai.prompts.tenant_overlay`:
    e-commerce platform data (Salla / Zid / Shopify) wins on price,
    inventory, product names and direct URLs; these sections cover
    everything else (story, payment / shipping policy, return rules,
    product usage tips, recipes, FAQ, …).
    """

    __tablename__ = "merchant_knowledge_sections"
    __table_args__ = (
        Index("ix_mks_tenant_id", "tenant_id"),
        Index("ix_mks_tenant_kind_active", "tenant_id", "kind", "is_active"),
        Index("ix_mks_tenant_priority", "tenant_id", "priority"),
        Index("ix_mks_tenant_updated", "tenant_id", "updated_at"),
    )

    id = Column(Integer, primary_key=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Fixed registry — see ``services/knowledge_section_kinds.py``.
    # Stored as VARCHAR (not a Postgres enum type) so the registry can
    # grow without a migration.
    kind = Column(String(64), nullable=False)
    title = Column(String(255), nullable=True)
    body = Column(Text, nullable=False, default="", server_default="")
    metadata_json = Column(JSONB, nullable=True)
    priority = Column(Integer, nullable=False, default=100, server_default="100")
    is_active = Column(
        Boolean, nullable=False, default=True, server_default="true",
    )
    # ``manual`` | ``ai_classified`` | ``imported``
    source = Column(
        String(32), nullable=False, default="manual", server_default="manual",
    )
    # Phase 2 lifecycle hooks — present from day 1 so the follow-up
    # migration is purely additive.
    ai_status = Column(
        String(32), nullable=False, default="approved", server_default="approved",
    )
    classification_confidence = Column(Float, nullable=True)
    conflicts_json = Column(JSONB, nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    media_links = relationship(
        "MerchantKnowledgeMedia",
        back_populates="section",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    product_links = relationship(
        "MerchantKnowledgeSectionProduct",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class MerchantKnowledgeMedia(Base):
    """Link table between a knowledge section and an :class:`AIMediaItem`.

    The same media row can be linked to multiple sections under
    different ``link_role`` values — e.g. a Rajhi-barcode image can
    back both a generic ``payment_method`` section (role=primary) and
    a bank-specific ``bank_transfer`` section (role=barcode).
    """

    __tablename__ = "merchant_knowledge_media"
    __table_args__ = (
        UniqueConstraint(
            "section_id", "media_id", "link_role",
            name="uq_mkm_section_media_role",
        ),
        Index("ix_mkm_section_id", "section_id"),
        Index("ix_mkm_media_id",   "media_id"),
    )

    id = Column(Integer, primary_key=True)
    section_id = Column(
        Integer,
        ForeignKey("merchant_knowledge_sections.id", ondelete="CASCADE"),
        nullable=False,
    )
    media_id = Column(
        Integer,
        ForeignKey("ai_media_library.id", ondelete="CASCADE"),
        nullable=False,
    )
    # ``primary`` | ``evidence`` | ``barcode`` | ``tutorial_video``
    # | ``recipe_video`` | ``policy_pdf`` | ``certificate`` | ``map``
    link_role = Column(
        String(32), nullable=False, default="primary", server_default="primary",
    )
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    section = relationship("MerchantKnowledgeSection", back_populates="media_links")
    media = relationship("AIMediaItem")


class MerchantKnowledgeSectionProduct(Base):
    """M2M link between a knowledge section and a catalog product (Phase 3).

    Sections with at least one product link are "product-scoped": the
    runtime overlay only injects them into the prompt when the
    conversation already mentions one of the linked products (matched
    via :mod:`backend.modules.ai.tooling.product_resolver`). Sections
    with zero product links remain global (return policy, store
    hours, …).

    Links can be ``manual`` (merchant picked a product from the
    dropdown) or ``ai_fuzzy_match`` (the Phase 3 fuzzy matcher
    proposed a match during draft approval — the merchant can drop
    the link later if it was wrong). Confidence is only meaningful
    for ``ai_fuzzy_match`` and NULL otherwise.
    """

    __tablename__ = "merchant_knowledge_section_products"
    __table_args__ = (
        UniqueConstraint(
            "section_id", "product_id",
            name="uq_mksp_section_product",
        ),
        Index("ix_mksp_section_id", "section_id"),
        Index("ix_mksp_product_id", "product_id"),
    )

    id = Column(Integer, primary_key=True)
    section_id = Column(
        Integer,
        ForeignKey(
            "merchant_knowledge_sections.id", ondelete="CASCADE",
        ),
        nullable=False,
    )
    product_id = Column(
        Integer,
        ForeignKey("products.id", ondelete="CASCADE"),
        nullable=False,
    )
    source = Column(
        String(32), nullable=False,
        default="manual", server_default="manual",
    )
    confidence = Column(Float, nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class MerchantKnowledgeDraft(Base):
    """A pending GPT-classified proposal (Phase 2).

    The merchant types a free-form quick-update + (optionally) attaches
    media; the backend asks GPT to classify the text into structured
    ops (create / update / merge / link_media) and detects conflicts
    against the existing sections + Salla snapshot. The result lives
    here until the merchant approves it (per-op or all) from the
    dashboard preview drawer.

    Approved drafts are not deleted — they're kept (status='approved'
    + applied_op_ids) for audit + undo. Failed classifier calls land
    with status='failed' so the dashboard can surface a retry button.
    """

    __tablename__ = "merchant_knowledge_drafts"
    __table_args__ = (
        Index("ix_mkd_tenant_id", "tenant_id"),
        Index("ix_mkd_tenant_status_created", "tenant_id", "status", "created_at"),
    )

    id = Column(Integer, primary_key=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    raw_text = Column(Text, nullable=False)
    attached_media_ids = Column(JSONB, nullable=True)
    status = Column(
        String(32), nullable=False, default="pending", server_default="pending",
    )
    proposal_json = Column(JSONB, nullable=True)
    conflicts_json = Column(JSONB, nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    decided_at = Column(DateTime(timezone=True), nullable=True)
    decided_by_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    applied_op_ids = Column(JSONB, nullable=True)


class MerchantBranch(Base):
    """Structured branch location — Operations Center (PR-A).

    Source of truth for maps URLs, reception contacts, and per-branch
    escalation when ``USE_STRUCTURED_BRANCH_CONTACTS`` is enabled.
    """

    __tablename__ = "merchant_branches"
    __table_args__ = (
        Index("ix_merchant_branches_tenant_active", "tenant_id", "is_active"),
        Index("ix_merchant_branches_tenant_sort", "tenant_id", "sort_order"),
    )

    id = Column(Integer, primary_key=True)
    tenant_id = Column(
        Integer,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    name = Column(String(255), nullable=False)
    city = Column(String(128), nullable=True)
    district = Column(String(128), nullable=True)
    address = Column(Text, nullable=True)
    maps_url = Column(String(2048), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True, server_default="true")
    hours_json = Column(JSONB, nullable=True)
    sort_order = Column(Integer, nullable=False, default=0, server_default="0")
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    tenant = relationship("Tenant", back_populates="merchant_branches")
    contacts = relationship(
        "BranchContact",
        back_populates="branch",
        cascade="all, delete-orphan",
    )
    escalation_steps = relationship(
        "BranchEscalationStep",
        back_populates="branch",
        cascade="all, delete-orphan",
    )


class BranchContact(Base):
    """Reception / branch staff contact — deterministic delivery only."""

    __tablename__ = "branch_contacts"
    __table_args__ = (
        Index("ix_branch_contacts_branch_active", "branch_id", "is_active"),
        Index("ix_branch_contacts_branch_sort", "branch_id", "sort_order"),
    )

    id = Column(Integer, primary_key=True)
    branch_id = Column(
        Integer,
        ForeignKey("merchant_branches.id", ondelete="CASCADE"),
        nullable=False,
    )
    display_name = Column(String(255), nullable=False)
    role = Column(String(128), nullable=True)
    phone_e164 = Column(String(32), nullable=False)
    whatsapp_e164 = Column(String(32), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True, server_default="true")
    is_default_reception = Column(
        Boolean, nullable=False, default=False, server_default="false",
    )
    sort_order = Column(Integer, nullable=False, default=0, server_default="0")

    branch = relationship("MerchantBranch", back_populates="contacts")


class BranchEscalationStep(Base):
    """Per-branch escalation ladder — level-ordered, no LLM."""

    __tablename__ = "branch_escalation_steps"
    __table_args__ = (
        Index(
            "ix_branch_escalation_steps_branch_level",
            "branch_id",
            "escalation_level",
        ),
        Index("ix_branch_escalation_steps_branch_sort", "branch_id", "sort_order"),
    )

    id = Column(Integer, primary_key=True)
    branch_id = Column(
        Integer,
        ForeignKey("merchant_branches.id", ondelete="CASCADE"),
        nullable=False,
    )
    escalation_level = Column(Integer, nullable=False, default=1, server_default="1")
    display_name = Column(String(255), nullable=False)
    role = Column(String(128), nullable=True)
    phone_e164 = Column(String(32), nullable=False)
    contact_id = Column(
        Integer,
        ForeignKey("branch_contacts.id", ondelete="RESTRICT"),
        nullable=True,
    )
    is_active = Column(Boolean, nullable=False, default=True, server_default="true")
    sort_order = Column(Integer, nullable=False, default=0, server_default="0")

    branch = relationship("MerchantBranch", back_populates="escalation_steps")
    contact = relationship("BranchContact", foreign_keys=[contact_id])
