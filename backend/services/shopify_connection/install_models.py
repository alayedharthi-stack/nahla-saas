"""Install-first identity/transactions; explicit migration only, never Base.create_all."""
from sqlalchemy import BigInteger, CheckConstraint, Column, DateTime, ForeignKey, Integer, String, Text
from services.shopify_connection.models import ShopifyBase


class ShopifyOwnerIdentity(ShopifyBase):
    __tablename__ = "shopify_owner_identities"
    # One initial owner per shop. Ownership transfers require an explicit recovery policy.
    connection_id = Column(BigInteger, ForeignKey("shopify_connections.id"), primary_key=True)
    shop_gid = Column(String(64), unique=True, nullable=False)
    shopify_user_id = Column(BigInteger, nullable=False)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    __table_args__ = (CheckConstraint("shopify_user_id > 0", name="ck_shopify_owner_positive"),)


class ShopifyInstallFlow(ShopifyBase):
    __tablename__ = "shopify_install_flows"
    id = Column(String(64), primary_key=True)
    state_hash = Column(String(64), unique=True, nullable=False)
    browser_hash = Column(String(64), nullable=False)
    shop_domain = Column(String(255), nullable=False)
    redirect_hash = Column(String(64), nullable=False)
    phase = Column(String(24), nullable=False)
    shop_gid = Column(String(64), nullable=True)
    owner_id = Column(BigInteger, nullable=True)
    online_token_enc = Column(Text, nullable=True)
    owner_expires_at = Column(DateTime(timezone=True), nullable=True)
    connection_id = Column(BigInteger, ForeignKey("shopify_connections.id"), nullable=True)
    generation = Column(BigInteger, nullable=False, default=0)
    handoff_hash = Column(String(64), unique=True, nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        CheckConstraint("phase IN ('online','offline','exchanging_online','exchanging_offline','ready','redeeming','redeemed','failed')",
                        name="ck_shopify_install_phase"),
        CheckConstraint("phase NOT IN ('redeemed','failed') OR online_token_enc IS NULL",
                        name="ck_shopify_install_terminal_secrets"),
    )


INSTALL_TABLES = (ShopifyOwnerIdentity.__table__, ShopifyInstallFlow.__table__)
