"""
tests/test_catalog_send_wireup.py
─────────────────────────────────
Phase 4 integration tests for the Meta WhatsApp Catalog wire-up in
``routers/whatsapp_webhook.py``.

We test the new private helper ``_try_send_catalog_product`` end-to-end
against a real in-memory SQLite database seeded with the Phase-2
schema (``meta_catalog_id``, ``catalog_enabled``,
``meta_retailer_id``). The only thing we mock is the outbound
provider call — that one symbol
(``services.whatsapp_platform.service.provider_send_message``) is
patched with an ``AsyncMock`` so no real HTTP request leaves the
process. Everything else — eligibility resolution, retailer-id
fallback, payload construction, structured logging — runs against
production code.

The contract under test (from the user's phase-4 instructions):

1. Eligibility miss → helper returns False (caller routes to legacy).
2. Provider rejects payload → helper returns False (caller routes to legacy).
3. Provider transport error → helper returns False.
4. Happy path → helper returns True AND a catalog payload is
   actually dispatched (``interactive.type = "product"`` with the
   correct ``catalog_id`` + ``product_retailer_id``).
5. ``meta_retailer_id`` column overrides the attachment's
   ``external_id`` when both are present.

We use the same SQLite-with-JSONB-downgrade pattern as
``tests/test_admin_debug_whatsapp_send.py`` so test isolation is
identical to the rest of the suite.

Run:
    python -m pytest tests/test_catalog_send_wireup.py -v
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for p in [str(REPO_ROOT), str(BACKEND_DIR), str(DATABASE_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)


def _run(coro):
    return asyncio.run(coro)


# The catalog sender does `from .service import provider_send_message`
# at module load time, so the live reference lives on the
# catalog_sender module — patching `services.whatsapp_platform.service`
# would leave the cached binding untouched. This constant is the
# correct target for all provider mocks below.
_PROVIDER_PATCH_PATH = (
    "services.whatsapp_platform.catalog_sender.provider_send_message"
)


# ──────────────────────────────────────────────────────────────────────
# In-memory DB harness (same pattern as test_admin_debug_whatsapp_send)
# ──────────────────────────────────────────────────────────────────────

def _make_db():
    from sqlalchemy import JSON, create_engine
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.orm import sessionmaker
    from models import Base

    engine = create_engine("sqlite:///:memory:")
    _saved = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                _saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, orig in _saved:
        col.type = orig
    Session = sessionmaker(bind=engine)
    return Session()


def _seed_tenant(db, *, tenant_id=77):
    from models import Tenant
    t = Tenant(id=tenant_id, name=f"tenant-{tenant_id}")
    db.add(t)
    db.commit()
    return t


def _seed_connection(
    db,
    *,
    tenant_id=77,
    catalog_enabled=True,
    meta_catalog_id="CAT-PROD-123",
    phone_number_id="100543193146977",
):
    from models import WhatsAppConnection
    conn = WhatsAppConnection(
        tenant_id=tenant_id,
        phone_number_id=phone_number_id,
        connection_type="direct",
        provider="meta",
        status="connected",
        meta_catalog_id=meta_catalog_id,
        catalog_enabled=catalog_enabled,
        access_token="dummy-token",
        sending_enabled=True,
    )
    db.add(conn)
    db.commit()
    return conn


def _seed_product(
    db,
    *,
    tenant_id=77,
    product_id=501,
    external_id="ext-501",
    meta_retailer_id=None,
    title="عسل السدر الأصلي",
    published=False,
):
    from models import Product
    p = Product(
        id=product_id,
        tenant_id=tenant_id,
        external_id=external_id,
        meta_retailer_id=meta_retailer_id,
        title=title,
        price="150",
        in_stock=True,
        meta_catalog_published_at=(
            datetime(2026, 6, 1, tzinfo=timezone.utc) if published else None
        ),
    )
    db.add(p)
    db.commit()
    return p


def _attachment(
    *,
    product_id=501,
    external_id="ext-501",
    title="عسل السدر الأصلي",
    caption="عسل السدر الأصلي\n150 ر.س — متوفر",
    confidence="fts",
    file_url="https://cdn.example.com/sdr.jpg",
    product_url="https://store.example.com/p/ext-501",
):
    """Build a product-card attachment dict in the exact shape the
    ``[PRODUCT:...]`` resolver writes inside the webhook."""
    return {
        "kind":        "product_card",
        "id":          product_id,
        "title":       title,
        "media_type":  "image",
        "file_url":    file_url,
        "caption":     caption,
        "product_url": product_url,
        "price":       "150",
        "in_stock":    True,
        "external_id": external_id,
        "confidence":  confidence,
    }


# ──────────────────────────────────────────────────────────────────────
# Helper import — done inside tests so sys.path is set up first.
# ──────────────────────────────────────────────────────────────────────

def _get_helper():
    from routers.whatsapp_webhook import _try_send_catalog_product
    return _try_send_catalog_product


# ──────────────────────────────────────────────────────────────────────
# 1. Eligibility short-circuits — provider must NOT be hit
# ──────────────────────────────────────────────────────────────────────

class TestEligibilityShortCircuits:
    """The helper must bail out BEFORE the provider when eligibility
    fails. This is the contract that protects merchants who haven't
    linked a catalog from accidental Meta API calls."""

    def test_no_connection_yields_legacy_path(self):
        helper = _get_helper()
        db = _make_db()
        _seed_tenant(db)
        _seed_product(db)

        sent = []

        async def fake_send(*args, **kwargs):
            sent.append((args, kwargs))
            return {"messages": [{"id": "wamid.XX"}]}, None

        with patch(
            _PROVIDER_PATCH_PATH,
            new=fake_send,
        ):
            result = _run(helper(
                db=db,
                connection=None,
                tenant_id=77,
                phone_id="PH",
                to="+966555111222",
                attachment=_attachment(),
            ))

        assert result is False, "no connection → legacy fallback"
        assert sent == [], "provider must NOT be hit when ineligible"

    def test_catalog_disabled_yields_legacy_path(self):
        helper = _get_helper()
        db = _make_db()
        _seed_tenant(db)
        conn = _seed_connection(db, catalog_enabled=False)
        _seed_product(db)

        fake_send = AsyncMock(return_value=({"messages": [{"id": "x"}]}, None))
        with patch(
            _PROVIDER_PATCH_PATH,
            new=fake_send,
        ):
            result = _run(helper(
                db=db,
                connection=conn,
                tenant_id=77,
                phone_id="PH",
                to="+966555111222",
                attachment=_attachment(),
            ))

        assert result is False
        fake_send.assert_not_called()

    def test_missing_catalog_id_yields_legacy_path(self):
        helper = _get_helper()
        db = _make_db()
        _seed_tenant(db)
        conn = _seed_connection(db, catalog_enabled=True, meta_catalog_id=None)
        _seed_product(db)

        fake_send = AsyncMock()
        with patch(
            _PROVIDER_PATCH_PATH,
            new=fake_send,
        ):
            result = _run(helper(
                db=db,
                connection=conn,
                tenant_id=77,
                phone_id="PH",
                to="+966555111222",
                attachment=_attachment(),
            ))

        assert result is False
        fake_send.assert_not_called()

    def test_wrong_kind_attachment_is_ignored(self):
        """Library media attachments (not product cards) must never
        be routed through the catalog sender."""
        helper = _get_helper()
        db = _make_db()
        _seed_tenant(db)
        conn = _seed_connection(db)

        fake_send = AsyncMock()
        with patch(
            _PROVIDER_PATCH_PATH,
            new=fake_send,
        ):
            result = _run(helper(
                db=db,
                connection=conn,
                tenant_id=77,
                phone_id="PH",
                to="+966555111222",
                attachment={"kind": "library_media", "file_url": "x"},
            ))

        assert result is False
        fake_send.assert_not_called()


# ──────────────────────────────────────────────────────────────────────
# 2. Happy path — payload shape is correct
# ──────────────────────────────────────────────────────────────────────

class TestCatalogHappyPath:
    def test_happy_path_dispatches_product_payload(self):
        helper = _get_helper()
        db = _make_db()
        _seed_tenant(db)
        conn = _seed_connection(
            db, meta_catalog_id="CAT-PROD-123", catalog_enabled=True,
        )
        _seed_product(db, product_id=501, external_id="ext-501", published=True)

        captured = {}

        async def fake_send(db_, conn_, *, tenant_id, operation, phone_id, payload, timeout):
            captured.update(
                tenant_id=tenant_id, operation=operation,
                phone_id=phone_id, payload=payload, timeout=timeout,
            )
            return {"messages": [{"id": "wamid.OKOK"}]}, None

        with patch(
            _PROVIDER_PATCH_PATH,
            new=fake_send,
        ):
            result = _run(helper(
                db=db,
                connection=conn,
                tenant_id=77,
                phone_id="PH",
                to="+966555111222",
                attachment=_attachment(),
            ))

        assert result is True, "catalog send succeeded → caller skips legacy"
        # Payload shape — Meta interactive product message.
        assert captured["operation"] == "send_catalog_product"
        inter = captured["payload"]["interactive"]
        assert inter["type"] == "product"
        assert inter["action"]["catalog_id"] == "CAT-PROD-123"
        # After membership is verified, external_id may still be the
        # projected retailer_id when no explicit meta_retailer_id exists.
        assert inter["action"]["product_retailer_id"] == "ext-501"
        # Body uses the caption produced by the resolver.
        assert "عسل السدر" in inter["body"]["text"]


# ──────────────────────────────────────────────────────────────────────
# 3. meta_retailer_id override takes precedence
# ──────────────────────────────────────────────────────────────────────

class TestRetailerIdOverride:
    def test_explicit_meta_retailer_id_wins_over_external_id(self):
        helper = _get_helper()
        db = _make_db()
        _seed_tenant(db)
        conn = _seed_connection(db)
        # Product has BOTH an external_id AND an explicit meta
        # retailer id — the override must win.
        _seed_product(
            db, product_id=501,
            external_id="ext-501",
            meta_retailer_id="custom-retailer-9",
            published=True,
        )

        captured = {}

        async def fake_send(db_, conn_, *, tenant_id, operation, phone_id, payload, timeout):
            captured["payload"] = payload
            return {"messages": [{"id": "wamid.OK"}]}, None

        with patch(
            _PROVIDER_PATCH_PATH,
            new=fake_send,
        ):
            result = _run(helper(
                db=db,
                connection=conn,
                tenant_id=77,
                phone_id="PH",
                to="+966555111222",
                # Attachment still carries the Salla external_id —
                # the helper must hit the DB to read the override.
                attachment=_attachment(external_id="ext-501"),
            ))

        assert result is True
        assert captured["payload"]["interactive"]["action"]["product_retailer_id"] == "custom-retailer-9"

    def test_external_id_used_when_no_meta_retailer_id_set(self):
        """Verified membership may still project retailer_id from external_id.

        Coincidence between upstream commerce id and Meta retailer id is
        allowed only after ``meta_catalog_published_at`` proves membership.
        """
        helper = _get_helper()
        db = _make_db()
        _seed_tenant(db)
        conn = _seed_connection(db)
        _seed_product(
            db,
            product_id=501,
            external_id="ext-501",
            meta_retailer_id=None,
            published=True,
        )

        captured = {}

        async def fake_send(db_, conn_, *, tenant_id, operation, phone_id, payload, timeout):
            captured["payload"] = payload
            return {"messages": [{"id": "wamid.OK"}]}, None

        with patch(
            _PROVIDER_PATCH_PATH,
            new=fake_send,
        ):
            result = _run(helper(
                db=db,
                connection=conn,
                tenant_id=77,
                phone_id="PH",
                to="+966555111222",
                attachment=_attachment(external_id="ext-501"),
            ))

        assert result is True
        assert captured["payload"]["interactive"]["action"]["product_retailer_id"] == "ext-501"


# ──────────────────────────────────────────────────────────────────────
# 4. Provider failures route to fallback
# ──────────────────────────────────────────────────────────────────────

class TestProviderFailureFallsBack:
    def test_provider_returns_error_envelope(self):
        helper = _get_helper()
        db = _make_db()
        _seed_tenant(db)
        conn = _seed_connection(db)
        _seed_product(db)

        async def fake_send(*args, **kwargs):
            # Meta-shaped error response — no `messages` array.
            return {"error": {"code": 131009, "message": "Bad catalog"}}, None

        with patch(
            _PROVIDER_PATCH_PATH,
            new=fake_send,
        ):
            result = _run(helper(
                db=db,
                connection=conn,
                tenant_id=77,
                phone_id="PH",
                to="+966555111222",
                attachment=_attachment(),
            ))

        assert result is False, "provider error → caller MUST route to legacy"

    def test_provider_raises_transport_exception(self):
        helper = _get_helper()
        db = _make_db()
        _seed_tenant(db)
        conn = _seed_connection(db)
        _seed_product(db)

        async def fake_send(*args, **kwargs):
            raise ConnectionError("upstream 502")

        with patch(
            _PROVIDER_PATCH_PATH,
            new=fake_send,
        ):
            result = _run(helper(
                db=db,
                connection=conn,
                tenant_id=77,
                phone_id="PH",
                to="+966555111222",
                attachment=_attachment(),
            ))

        assert result is False, "transport error → caller MUST route to legacy"

    def test_2xx_with_empty_messages_array_falls_back(self):
        """Some providers occasionally return 200 with no wamid (e.g.
        deferred / queued). We treat that as a failure for catalog
        purposes — the customer must SEE a card now, not eventually."""
        helper = _get_helper()
        db = _make_db()
        _seed_tenant(db)
        conn = _seed_connection(db)
        _seed_product(db)

        async def fake_send(*args, **kwargs):
            return {"messages": []}, None

        with patch(
            _PROVIDER_PATCH_PATH,
            new=fake_send,
        ):
            result = _run(helper(
                db=db,
                connection=conn,
                tenant_id=77,
                phone_id="PH",
                to="+966555111222",
                attachment=_attachment(),
            ))

        assert result is False


# ──────────────────────────────────────────────────────────────────────
# 5. Resolver-only attachment (no DB row) fails closed without membership
# ──────────────────────────────────────────────────────────────────────

class TestAttachmentOnlyPath:
    def test_attachment_external_id_is_not_membership_without_product_row(self):
        """Resolver-only attachment with an external_id is not verified
        Meta catalog membership. Native send fails closed and the caller
        must use legacy fallback for the same canonical attachment."""
        helper = _get_helper()
        db = _make_db()
        _seed_tenant(db)
        conn = _seed_connection(db)
        # NB: no product seeded → DB lookup returns None.

        hit = {"n": 0}

        async def fake_send(db_, conn_, *, tenant_id, operation, phone_id, payload, timeout):
            hit["n"] += 1
            return {"messages": [{"id": "wamid.OK"}]}, None

        with patch(
            _PROVIDER_PATCH_PATH,
            new=fake_send,
        ):
            result = _run(helper(
                db=db,
                connection=conn,
                tenant_id=77,
                phone_id="PH",
                to="+966555111222",
                attachment=_attachment(external_id="ext-LOST"),
            ))

        assert result is False
        assert hit["n"] == 0


# ──────────────────────────────────────────────────────────────────────
# 6. Orchestrator gating (Phase B)
# ──────────────────────────────────────────────────────────────────────

class TestOrchestratorGating:
    def test_retailer_id_collision_skips_provider(self):
        helper = _get_helper()
        db = _make_db()
        _seed_tenant(db)
        conn = _seed_connection(db)
        _seed_product(db, product_id=501, external_id="dup-rid")
        _seed_product(db, product_id=502, external_id="dup-rid", title="Other")

        hit = {"n": 0}

        async def fake_send(*args, **kwargs):
            hit["n"] += 1
            return {"messages": [{"id": "wamid.NO"}]}, None

        with patch(_PROVIDER_PATCH_PATH, new=fake_send):
            result = _run(helper(
                db=db,
                connection=conn,
                tenant_id=77,
                phone_id="PH",
                to="+966555111222",
                attachment=_attachment(product_id=501, external_id="dup-rid"),
            ))

        assert result is False
        assert hit["n"] == 0

    def test_weak_confidence_skips_provider(self, monkeypatch):
        monkeypatch.delenv("CATALOG_WEAK_CONFIDENCE_BLOCK", raising=False)
        helper = _get_helper()
        db = _make_db()
        _seed_tenant(db)
        conn = _seed_connection(db)
        _seed_product(db)

        hit = {"n": 0}

        async def fake_send(*args, **kwargs):
            hit["n"] += 1
            return {"messages": [{"id": "wamid.NO"}]}, None

        att = _attachment(confidence="weak")
        with patch(_PROVIDER_PATCH_PATH, new=fake_send):
            result = _run(helper(
                db=db,
                connection=conn,
                tenant_id=77,
                phone_id="PH",
                to="+966555111222",
                attachment=att,
            ))

        assert result is False
        assert hit["n"] == 0

    def test_attachment_unchanged_after_helper(self):
        import copy

        helper = _get_helper()
        db = _make_db()
        _seed_tenant(db)
        conn = _seed_connection(db)
        _seed_product(db)
        att = _attachment()
        snap = copy.deepcopy(att)

        async def fake_send(*args, **kwargs):
            return {"messages": [{"id": "wamid.OK"}]}, None

        with patch(_PROVIDER_PATCH_PATH, new=fake_send):
            _run(helper(
                db=db,
                connection=conn,
                tenant_id=77,
                phone_id="PH",
                to="+966555111222",
                attachment=att,
            ))

        assert att == snap
        assert "retailer_id" not in att
