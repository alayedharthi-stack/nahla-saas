"""
tests/test_cart_recovery_flow.py
─────────────────────────────────
Pin three P0 invariants for the abandoned-cart WhatsApp recovery flow,
all triggered by the merchant smoke-test where:

  • The "إكمال الطلب" button replied with the generic AI line
    "ممتاز! نقدر نساعدك تكمل الطلب الآن. أخبرني وش تحتاج 🤝"
    instead of opening a checkout link.
  • Addon recommendations and "سجلت اهتمامك" messages were duplicated.

The three invariants enforced here:

  1. resume_cart NEVER falls back to the fake-AI text when an Order row
     with a checkout_url exists for the same cart id, even if the
     parent ``cart_abandoned`` AutomationEvent was emitted with an empty
     payload. (`_resolve_cart_url` Order-fallback path.)

  2. ``emit_cart_abandoned_if_new`` is idempotent on
     ``Order.extra_metadata.recovery_event_id``. Two consecutive calls
     produce one event — webhook + sweeper can race without duplicating
     the recovery flow.

  3. The Salla webhook + sync paths actually emit ``cart_abandoned``
     events for newly-seen carts, so the recovery automation fires for
     platform-sourced carts (not just storefront-snippet carts).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Dict

from sqlalchemy import JSON, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from database.models import (  # noqa: E402
    Base,
    AutomationEvent,
    Customer,
    Order,
    Tenant,
)


# SQLite needs JSONB → JSON remap (same trick the sibling suites use).
@event.listens_for(Base.metadata, "before_create")
def _remap_jsonb(target, connection, **kw):
    for table in target.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()


def _make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    tenant = Tenant(name="Recovery Flow Test", is_active=True)
    db.add(tenant)
    db.commit()
    return db, tenant.id


def _make_customer(db, tenant_id, *, phone="+966500111222"):
    cust = Customer(tenant_id=tenant_id, name="عميل تجريبي", phone=phone)
    db.add(cust)
    db.commit()
    db.refresh(cust)
    return cust


def _make_cart_row(
    db, tenant_id,
    *, external_id="cart-9001",
    checkout_url="https://store.example/cart/9001",
    phone="+966500111222",
):
    cart = Order(
        tenant_id=tenant_id,
        external_id=external_id,
        external_order_number=external_id,
        status="abandoned",
        total="248.00",
        is_abandoned=True,
        source="salla",
        customer_info={"name": "عميل تجريبي", "phone": phone, "mobile": phone},
        line_items=[{"name": "فستان", "quantity": 1}],
        checkout_url=checkout_url,
    )
    db.add(cart)
    db.commit()
    db.refresh(cart)
    return cart


# ═══════════════════════════════════════════════════════════════════════
# 1. resume_cart NEVER ships the fake-AI fallback when the Order row
#    has a checkout_url, even if the parent event payload is empty.
# ═══════════════════════════════════════════════════════════════════════

def test_resolve_cart_url_falls_back_to_order_row_checkout_url():
    """The bug screenshot showed the bot replying with the generic
    "أخبرني وش تحتاج" line because the parent event payload had no
    checkout_url. The Order row always has one for Salla carts, so the
    resolver MUST fall back to it before giving up."""
    from services.cart_recovery_actions import _resolve_cart_url

    db, tenant_id = _make_db()
    cart = _make_cart_row(
        db, tenant_id,
        external_id="cart-7777",
        checkout_url="https://store.example/cart/7777",
    )

    # Parent event exists but its payload is empty (the silent failure
    # mode that produced the fake-AI text in production).
    parent_event = AutomationEvent(
        tenant_id=tenant_id,
        event_type="cart_abandoned",
        payload={"cart_id": "7777"},  # no checkout_url, no cart_url
        processed=False,
    )
    db.add(parent_event)
    db.commit()

    resolved = _resolve_cart_url(
        parent_event, db=db, tenant_id=tenant_id, cart_id="7777",
    )
    assert resolved == "https://store.example/cart/7777", (
        f"Resolver must fall back to the Order row's checkout_url when "
        f"the event payload has none. Got: {resolved!r}"
    )

    # Sanity: when the event payload DOES carry a URL, that wins (it
    # may carry a coupon-attached variant the merchant wants honored).
    parent_event.payload = {
        "cart_id": "7777",
        "checkout_url": "https://store.example/cart/7777?coupon=SAVE10",
    }
    db.commit()
    resolved2 = _resolve_cart_url(
        parent_event, db=db, tenant_id=tenant_id, cart_id="7777",
    )
    assert resolved2 == "https://store.example/cart/7777?coupon=SAVE10"


def test_resolve_cart_url_returns_none_when_neither_source_has_url():
    """Last resort: no event payload + no Order row. Resolver returns
    None and the resume handler ships the honest fallback text (NOT
    the fake-AI prompt)."""
    from services.cart_recovery_actions import _resolve_cart_url

    db, tenant_id = _make_db()
    resolved = _resolve_cart_url(
        None, db=db, tenant_id=tenant_id, cart_id="does-not-exist",
    )
    assert resolved is None


def test_resume_cart_handler_never_ships_fake_ai_fallback_when_order_has_url():
    """End-to-end: tap a resume_cart button with no parent event but
    with an Order row holding the checkout_url → CTA-URL message goes
    out, NOT the fake-AI text. Records every captured outbound so we
    can prove the wrong text never escaped."""
    from services.cart_recovery_actions import handle_cart_recovery_button
    from services.cart_recovery_buttons import encode_button_id

    db, tenant_id = _make_db()
    _make_customer(db, tenant_id)
    _make_cart_row(
        db, tenant_id,
        external_id="cart-5555",
        checkout_url="https://store.example/cart/5555",
    )

    cta_calls: list[Dict[str, Any]] = []
    text_calls: list[Dict[str, Any]] = []

    async def fake_cta(**kwargs):
        cta_calls.append(kwargs)

    async def fake_text(**kwargs):
        text_calls.append(kwargs)

    async def fake_buttons(**kwargs):
        pass

    btn_id = encode_button_id("resume_cart", cart_id="5555", stage=1)

    handled = asyncio.run(handle_cart_recovery_button(
        db=db,
        button_id=btn_id,
        phone_id="PHONE_NUM_ID",
        to_phone="+966500111222",
        tenant_id=tenant_id,
        send_cta_url=fake_cta,
        send_text=fake_text,
        send_buttons=fake_buttons,
    ))

    assert handled is True, "Cart recovery button must be recognised"
    assert len(cta_calls) == 1, (
        f"Expected exactly one CTA-URL message, got cta={len(cta_calls)} "
        f"text={len(text_calls)}. The Order-row fallback failed to kick in."
    )
    assert cta_calls[0]["btn_url"] == "https://store.example/cart/5555"

    # The fake-AI line MUST NEVER appear regardless of the fallback path.
    fake_ai_line = "أخبرني وش تحتاج"
    for call in text_calls:
        assert fake_ai_line not in call.get("text", ""), (
            f"Fake-AI fallback escaped the resume handler: {call['text']!r}"
        )


def test_resume_cart_handler_uses_honest_fallback_when_no_url_anywhere():
    """When neither the event payload nor the Order row has a URL we
    must NOT ship the fake-AI text. The honest fallback acknowledges
    the cart is saved without pretending to be a free-form assistant."""
    from services.cart_recovery_actions import handle_cart_recovery_button
    from services.cart_recovery_buttons import encode_button_id

    db, tenant_id = _make_db()
    _make_customer(db, tenant_id)
    # Cart row exists but with NO checkout_url (rare draft case).
    cart = Order(
        tenant_id=tenant_id,
        external_id="cart-4444",
        external_order_number="cart-4444",
        status="abandoned",
        total="0.00",
        is_abandoned=True,
        source="salla",
        customer_info={},
        line_items=[],
        checkout_url="",
    )
    db.add(cart)
    db.commit()

    cta_calls: list[Dict[str, Any]] = []
    text_calls: list[Dict[str, Any]] = []

    async def fake_cta(**kwargs):
        cta_calls.append(kwargs)

    async def fake_text(**kwargs):
        text_calls.append(kwargs)

    async def fake_buttons(**kwargs):
        pass

    btn_id = encode_button_id("resume_cart", cart_id="4444", stage=1)

    asyncio.run(handle_cart_recovery_button(
        db=db,
        button_id=btn_id,
        phone_id="PHONE_NUM_ID",
        to_phone="+966500111222",
        tenant_id=tenant_id,
        send_cta_url=fake_cta,
        send_text=fake_text,
        send_buttons=fake_buttons,
    ))

    assert len(cta_calls) == 0, "No checkout URL → no CTA-URL message"
    assert len(text_calls) == 1
    text = text_calls[0]["text"]
    assert "أخبرني وش تحتاج" not in text, (
        "Fake-AI fallback must not be used. Saw: " + text
    )
    assert "محفوظة" in text or "متجر" in text, (
        "Honest fallback should mention the cart is saved or in the store"
    )


# ═══════════════════════════════════════════════════════════════════════
# 2. emit_cart_abandoned_if_new is idempotent and honors the marker.
# ═══════════════════════════════════════════════════════════════════════

def test_emit_cart_abandoned_if_new_creates_event_with_checkout_url():
    from services.cart_recovery_emitter import emit_cart_abandoned_if_new

    db, tenant_id = _make_db()
    cart = _make_cart_row(db, tenant_id, external_id="cart-2001",
                          checkout_url="https://store.example/cart/2001")

    normalised = {
        "external_id":   "cart-2001",
        "raw_cart_id":   "2001",
        "checkout_url":  "https://store.example/cart/2001",
        "customer_info": {"phone": "+966500111222", "mobile": "+966500111222"},
        "customer_name": "عميل تجريبي",
        "line_items":    [{"name": "فستان"}],
        "total":         "248.00",
        "created_at":    "2026-04-20T10:00:00+00:00",
    }

    event_id = emit_cart_abandoned_if_new(
        db, tenant_id=tenant_id, cart_row=cart, normalised=normalised,
        source="store_sync",
    )
    assert event_id is not None, "Should emit a fresh cart_abandoned event"

    db.refresh(cart)
    assert (cart.extra_metadata or {}).get("recovery_event_id") == event_id

    ev = db.query(AutomationEvent).filter_by(id=event_id).first()
    assert ev is not None
    assert ev.event_type == "cart_abandoned"
    assert ev.payload["checkout_url"] == "https://store.example/cart/2001"
    assert ev.payload["cart_id"] == "2001"
    assert ev.payload["source"] == "store_sync"
    assert ev.customer_id is not None, (
        "Event must have a customer_id so the engine knows who to message"
    )


def test_emit_cart_abandoned_if_new_is_idempotent():
    """Webhook AND sweeper can call this for the same cart in close
    succession — must NOT produce two events."""
    from services.cart_recovery_emitter import emit_cart_abandoned_if_new

    db, tenant_id = _make_db()
    cart = _make_cart_row(db, tenant_id, external_id="cart-3001",
                          checkout_url="https://store.example/cart/3001")
    normalised = {
        "external_id":   "cart-3001",
        "raw_cart_id":   "3001",
        "checkout_url":  "https://store.example/cart/3001",
        "customer_info": {"phone": "+966500111222"},
        "customer_name": "عميل",
        "line_items":    [],
        "total":         "100.00",
        "created_at":    "2026-04-20T10:00:00+00:00",
    }

    first = emit_cart_abandoned_if_new(
        db, tenant_id=tenant_id, cart_row=cart,
        normalised=normalised, source="webhook",
    )
    second = emit_cart_abandoned_if_new(
        db, tenant_id=tenant_id, cart_row=cart,
        normalised=normalised, source="store_sync",
    )

    assert first == second, "Repeat call must return same event id"
    assert db.query(AutomationEvent).filter_by(
        tenant_id=tenant_id, event_type="cart_abandoned",
    ).count() == 1, "Idempotency marker must prevent duplicate emit"


def test_emit_cart_abandoned_if_new_skips_carts_without_phone():
    """A cart with no customer phone has no recovery target. Persist
    the row for dashboard visibility, but don't emit an event the engine
    can never deliver against."""
    from services.cart_recovery_emitter import emit_cart_abandoned_if_new

    db, tenant_id = _make_db()
    cart = Order(
        tenant_id=tenant_id,
        external_id="cart-no-phone",
        external_order_number="cart-no-phone",
        status="abandoned",
        total="0.00",
        is_abandoned=True,
        source="salla",
        customer_info={},
        line_items=[],
        checkout_url="https://store.example/cart/x",
    )
    db.add(cart)
    db.commit()

    normalised = {
        "external_id":   "cart-no-phone",
        "raw_cart_id":   "no-phone",
        "checkout_url":  "https://store.example/cart/x",
        "customer_info": {},
        "customer_name": "",
        "line_items":    [],
        "total":         "0.00",
        "created_at":    "2026-04-20T10:00:00+00:00",
    }

    event_id = emit_cart_abandoned_if_new(
        db, tenant_id=tenant_id, cart_row=cart,
        normalised=normalised, source="store_sync",
    )
    assert event_id is None, "No phone → no event"
    assert db.query(AutomationEvent).count() == 0


def test_emit_cart_abandoned_re_emits_when_marker_points_to_purged_event():
    """Operator-initiated AutomationEvent purge must not permanently
    silence the recovery flow for that cart. Stale marker → re-emit."""
    from services.cart_recovery_emitter import emit_cart_abandoned_if_new

    db, tenant_id = _make_db()
    cart = _make_cart_row(db, tenant_id, external_id="cart-purged")
    cart.extra_metadata = {"recovery_event_id": 99999}  # stale id, no row
    db.commit()

    normalised = {
        "external_id":   "cart-purged",
        "raw_cart_id":   "purged",
        "checkout_url":  "https://store.example/cart/purged",
        "customer_info": {"phone": "+966500111222"},
        "customer_name": "عميل",
        "line_items":    [],
        "total":         "100.00",
        "created_at":    "2026-04-20T10:00:00+00:00",
    }

    event_id = emit_cart_abandoned_if_new(
        db, tenant_id=tenant_id, cart_row=cart,
        normalised=normalised, source="store_sync",
    )
    assert event_id is not None and event_id != 99999, (
        "Stale marker must trigger re-emit with a fresh event id"
    )
