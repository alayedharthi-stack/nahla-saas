"""tests/test_manual_segments.py
─────────────────────────────────
Contract tests for Manual Customer Segments + the marketing-side
flags they rely on.

Coverage:

  * ``services.manual_segments``
      - Validation: only keys from the canonical Nahla registry are
        accepted (UnknownSegmentError otherwise).
      - Idempotent add / silent-no-op remove.
      - Cross-tenant isolation: tagging a customer in tenant B from
        tenant A is rejected as "not found", never silently applied.
      - Bulk + single read helpers agree on the same set of tags.
      - ``set_marketing_opt_out_manual`` / ``set_test_recipient`` toggle
        the right ``Customer.extra_metadata`` flags AND timestamps.

  * ``services.campaign_dispatcher._snapshot_recipients``
      - Customers with ``marketing_opt_out_manual=True`` are written
        as ``skipped_manual_exclusion`` with reason
        ``marketing_opt_out_manual``.
      - Customers tagged with a key in ``excluded_segments`` are
        likewise skipped, with reason ``excluded_by_manual_segment``.
      - Customers in *neither* set still queue normally.

  * ``services.campaign_dispatcher._resolve_audience``
      - The new ``test_recipients`` pseudo-segment returns only
        customers flagged via ``set_test_recipient``.
      - The ``manual:<key>`` audience returns only customers manually
        tagged with that key, and is tenant-isolated.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from models import (  # noqa: E402
    Base,
    Campaign,
    CampaignSendLog,
    Customer,
    CustomerSegmentManual,
    Tenant,
    WhatsAppTemplate,
)
from services.campaign_dispatcher import (  # noqa: E402
    LOG_QUEUED,
    LOG_SKIPPED_MANUAL_EXCLUSION,
    REASON_MARKETING_OPT_OUT,
    REASON_MANUAL_EXCLUDE,
    _resolve_audience,
    _snapshot_recipients,
)
from services.manual_segments import (  # noqa: E402
    META_KEY_OPT_OUT,
    META_KEY_OPT_OUT_AT,
    META_KEY_TEST_AT,
    META_KEY_TEST_RECIPIENT,
    UnknownSegmentError,
    add_manual_segment,
    assert_known_segment,
    customer_ids_with_manual_segment,
    is_marketing_opted_out,
    is_test_recipient,
    list_manual_segments_bulk,
    list_manual_segments_for_customer,
    remove_manual_segment,
    set_marketing_opt_out_manual,
    set_test_recipient,
    list_test_recipient_customer_ids,
)


# ── SQLite shim — same JSONB → JSON downgrade as test_campaign_send_log ──


def _make_db():
    engine = create_engine("sqlite:///:memory:")
    _saved: list = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                _saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, orig in _saved:
        col.type = orig
    Session = sessionmaker(bind=engine)
    return Session(), engine


def _seed_tenant(db, name="T") -> Tenant:
    t = Tenant(name=name, is_active=True)
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _seed_customer(db, tenant_id, phone, *, extra=None) -> Customer:
    c = Customer(
        tenant_id=tenant_id,
        phone=phone,
        normalized_phone=phone,
        name=f"C{phone[-3:]}",
        extra_metadata=extra or {},
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _seed_template(db, tenant_id) -> WhatsAppTemplate:
    tpl = WhatsAppTemplate(
        tenant_id=tenant_id, name="tpl_x", language="ar",
        category="MARKETING", status="APPROVED",
        components=[{"type": "BODY", "text": "Hi {{1}}"}],
    )
    db.add(tpl); db.commit(); db.refresh(tpl)
    return tpl


def _seed_campaign(db, tenant_id, template) -> Campaign:
    c = Campaign(
        tenant_id=tenant_id, name="C", campaign_type="broadcast",
        template_id=str(template.id), template_name=template.name,
        template_language="ar", template_category="MARKETING",
        audience_type="all", status="scheduled",
    )
    db.add(c); db.commit(); db.refresh(c)
    return c


# ── 1. Validation ────────────────────────────────────────────────────────


class TestSegmentKeyValidation:
    def test_unknown_segment_key_is_rejected(self):
        # Free-form tags must be rejected — the whole point of the
        # design is that merchants cannot invent new cohorts.
        with pytest.raises(UnknownSegmentError):
            assert_known_segment("my_super_secret_tag")

    def test_empty_segment_key_is_rejected(self):
        with pytest.raises(UnknownSegmentError):
            assert_known_segment("")

    def test_canonical_keys_are_accepted_case_insensitive(self):
        # All keys land lowercased so the merchant can paste from
        # anywhere without us caring about case.
        assert assert_known_segment("VIP") == "vip"
        assert assert_known_segment("Unsubscribed") == "unsubscribed"
        assert assert_known_segment("no_purchase_60") == "no_purchase_60"


# ── 2. Manual segment CRUD ───────────────────────────────────────────────


class TestManualSegmentCRUD:
    def test_add_creates_one_row_and_is_idempotent(self):
        db, _ = _make_db()
        t = _seed_tenant(db)
        c = _seed_customer(db, t.id, "+966500000001")

        row1 = add_manual_segment(
            db, tenant_id=t.id, customer_id=c.id, segment_key="vip",
        )
        row2 = add_manual_segment(
            db, tenant_id=t.id, customer_id=c.id, segment_key="vip",
        )
        # Same row returned, no duplicate inserted — UNIQUE index +
        # the explicit pre-check both should hold.
        assert row1.id == row2.id
        rows = db.query(CustomerSegmentManual).all()
        assert len(rows) == 1
        assert rows[0].segment_key == "vip"
        assert rows[0].source == "manual"

    def test_add_rejects_unknown_key_with_422_friendly_error(self):
        db, _ = _make_db()
        t = _seed_tenant(db)
        c = _seed_customer(db, t.id, "+966500000001")
        with pytest.raises(UnknownSegmentError):
            add_manual_segment(
                db, tenant_id=t.id, customer_id=c.id, segment_key="pirate",
            )
        # Nothing should have been written — a 422 must not leak rows.
        assert db.query(CustomerSegmentManual).count() == 0

    def test_add_blocks_cross_tenant_tagging(self):
        # Tenant A cannot tag a customer that belongs to tenant B —
        # the path must raise LookupError so the router returns 404.
        db, _ = _make_db()
        a = _seed_tenant(db, "A")
        b = _seed_tenant(db, "B")
        cust_in_b = _seed_customer(db, b.id, "+966500000111")

        with pytest.raises(LookupError):
            add_manual_segment(
                db, tenant_id=a.id, customer_id=cust_in_b.id,
                segment_key="vip",
            )
        assert db.query(CustomerSegmentManual).count() == 0

    def test_remove_returns_false_when_pin_was_absent(self):
        db, _ = _make_db()
        t = _seed_tenant(db)
        c = _seed_customer(db, t.id, "+966500000001")
        # Never added — remove should still succeed (idempotent UX)
        # but report "removed=False" so the caller can decide whether
        # to surface a "not found" toast.
        assert remove_manual_segment(
            db, tenant_id=t.id, customer_id=c.id, segment_key="vip",
        ) is False

    def test_remove_drops_existing_pin(self):
        db, _ = _make_db()
        t = _seed_tenant(db)
        c = _seed_customer(db, t.id, "+966500000001")
        add_manual_segment(db, tenant_id=t.id, customer_id=c.id, segment_key="vip")
        assert remove_manual_segment(
            db, tenant_id=t.id, customer_id=c.id, segment_key="vip",
        ) is True
        assert db.query(CustomerSegmentManual).count() == 0

    def test_list_for_customer_returns_sorted_keys(self):
        db, _ = _make_db()
        t = _seed_tenant(db)
        c = _seed_customer(db, t.id, "+966500000001")
        add_manual_segment(db, tenant_id=t.id, customer_id=c.id, segment_key="vip")
        add_manual_segment(db, tenant_id=t.id, customer_id=c.id, segment_key="dormant")
        assert list_manual_segments_for_customer(db, t.id, c.id) == ["dormant", "vip"]

    def test_bulk_list_groups_by_customer(self):
        db, _ = _make_db()
        t = _seed_tenant(db)
        c1 = _seed_customer(db, t.id, "+966500000001")
        c2 = _seed_customer(db, t.id, "+966500000002")
        add_manual_segment(db, tenant_id=t.id, customer_id=c1.id, segment_key="vip")
        add_manual_segment(db, tenant_id=t.id, customer_id=c2.id, segment_key="dormant")
        out = list_manual_segments_bulk(db, t.id, [c1.id, c2.id])
        assert out == {c1.id: ["vip"], c2.id: ["dormant"]}

    def test_customer_ids_with_segment_is_tenant_scoped(self):
        # Two tenants, both with a customer pinned as "vip" — each
        # should only see its own.
        db, _ = _make_db()
        a = _seed_tenant(db, "A")
        b = _seed_tenant(db, "B")
        ca = _seed_customer(db, a.id, "+966500000010")
        cb = _seed_customer(db, b.id, "+966500000020")
        add_manual_segment(db, tenant_id=a.id, customer_id=ca.id, segment_key="vip")
        add_manual_segment(db, tenant_id=b.id, customer_id=cb.id, segment_key="vip")
        assert customer_ids_with_manual_segment(db, a.id, "vip") == [ca.id]
        assert customer_ids_with_manual_segment(db, b.id, "vip") == [cb.id]


# ── 3. Marketing preference flags ────────────────────────────────────────


class TestMarketingPrefs:
    def test_set_opt_out_writes_flag_and_timestamp(self):
        db, _ = _make_db()
        t = _seed_tenant(db)
        c = _seed_customer(db, t.id, "+966500000001")
        set_marketing_opt_out_manual(
            db, tenant_id=t.id, customer_id=c.id, opted_out=True,
        )
        db.refresh(c)
        meta = c.extra_metadata or {}
        assert meta[META_KEY_OPT_OUT] is True
        # Timestamp must accompany the flag for audit.
        assert meta.get(META_KEY_OPT_OUT_AT)
        assert is_marketing_opted_out(c) is True

    def test_unset_opt_out_drops_timestamp(self):
        db, _ = _make_db()
        t = _seed_tenant(db)
        c = _seed_customer(db, t.id, "+966500000001",
                           extra={META_KEY_OPT_OUT: True,
                                  META_KEY_OPT_OUT_AT: "2025-01-01T00:00:00+00:00"})
        set_marketing_opt_out_manual(
            db, tenant_id=t.id, customer_id=c.id, opted_out=False,
        )
        db.refresh(c)
        meta = c.extra_metadata or {}
        assert meta[META_KEY_OPT_OUT] is False
        assert META_KEY_OPT_OUT_AT not in meta
        assert is_marketing_opted_out(c) is False

    def test_set_test_recipient_independent_of_opt_out(self):
        # Toggling the test-recipient flag must NOT touch opt-out — a
        # merchant can dry-run on a customer who is normally
        # eligible, then opt them out separately.
        db, _ = _make_db()
        t = _seed_tenant(db)
        c = _seed_customer(db, t.id, "+966500000001")
        set_test_recipient(db, tenant_id=t.id, customer_id=c.id, is_test=True)
        db.refresh(c)
        meta = c.extra_metadata or {}
        assert meta[META_KEY_TEST_RECIPIENT] is True
        assert meta.get(META_KEY_TEST_AT)
        assert META_KEY_OPT_OUT not in meta
        assert is_test_recipient(c) is True

    def test_test_recipient_customer_ids_returns_only_flagged(self):
        db, _ = _make_db()
        t = _seed_tenant(db)
        c1 = _seed_customer(db, t.id, "+966500000001")
        c2 = _seed_customer(db, t.id, "+966500000002")
        c3 = _seed_customer(db, t.id, "+966500000003")
        set_test_recipient(db, tenant_id=t.id, customer_id=c1.id, is_test=True)
        set_test_recipient(db, tenant_id=t.id, customer_id=c3.id, is_test=True)
        assert set(list_test_recipient_customer_ids(db, t.id)) == {c1.id, c3.id}


# ── 4. Snapshot integration ──────────────────────────────────────────────


class TestSnapshotIntegration:
    def test_marketing_opt_out_marks_skipped_manual_exclusion(self):
        db, _ = _make_db()
        t = _seed_tenant(db)
        tpl = _seed_template(db, t.id)
        camp = _seed_campaign(db, t.id, tpl)
        c = _seed_customer(
            db, t.id, "+966500000001",
            extra={META_KEY_OPT_OUT: True},
        )
        _snapshot_recipients(db, t.id, camp.id, [c], tpl)
        db.commit()
        row = db.query(CampaignSendLog).filter_by(campaign_id=camp.id).one()
        assert row.status == LOG_SKIPPED_MANUAL_EXCLUSION
        assert row.skip_reason == REASON_MARKETING_OPT_OUT

    def test_excluded_segment_marks_skipped_manual_exclusion(self):
        db, _ = _make_db()
        t = _seed_tenant(db)
        tpl = _seed_template(db, t.id)
        camp = _seed_campaign(db, t.id, tpl)
        c1 = _seed_customer(db, t.id, "+966500000001")
        c2 = _seed_customer(db, t.id, "+966500000002")
        # Tag c1 as VIP — and exclude VIP at snapshot time.
        add_manual_segment(db, tenant_id=t.id, customer_id=c1.id, segment_key="vip")
        _snapshot_recipients(
            db, t.id, camp.id, [c1, c2], tpl,
            excluded_segments=["vip"],
        )
        db.commit()
        rows = {r.customer_id: r for r in
                db.query(CampaignSendLog).filter_by(campaign_id=camp.id).all()}
        assert rows[c1.id].status == LOG_SKIPPED_MANUAL_EXCLUSION
        assert rows[c1.id].skip_reason == REASON_MANUAL_EXCLUDE
        # Untagged customer still queues — exclusion is targeted, not
        # blanket.
        assert rows[c2.id].status == LOG_QUEUED


# ── 5. Audience resolution for manual segments / test recipients ─────────


class TestResolveAudience:
    def test_test_recipients_pseudo_segment_returns_only_flagged(self):
        db, _ = _make_db()
        t = _seed_tenant(db)
        c1 = _seed_customer(db, t.id, "+966500000001")
        _seed_customer(db, t.id, "+966500000002")  # not flagged
        set_test_recipient(db, tenant_id=t.id, customer_id=c1.id, is_test=True)
        out = _resolve_audience(db, t.id, "test_recipients")
        assert [c.id for c in out] == [c1.id]

    def test_manual_audience_returns_only_tagged(self):
        db, _ = _make_db()
        t = _seed_tenant(db)
        c1 = _seed_customer(db, t.id, "+966500000001")
        _seed_customer(db, t.id, "+966500000002")
        add_manual_segment(db, tenant_id=t.id, customer_id=c1.id, segment_key="vip")
        out = _resolve_audience(db, t.id, "manual:vip")
        assert [c.id for c in out] == [c1.id]

    def test_manual_audience_is_tenant_isolated(self):
        # Tagging "vip" in tenant B must NOT leak into tenant A's
        # "manual:vip" audience.
        db, _ = _make_db()
        a = _seed_tenant(db, "A")
        b = _seed_tenant(db, "B")
        # Customer in A — untagged
        _seed_customer(db, a.id, "+966500000010")
        # Customer in B — tagged VIP
        cb = _seed_customer(db, b.id, "+966500000020")
        add_manual_segment(db, tenant_id=b.id, customer_id=cb.id, segment_key="vip")
        assert _resolve_audience(db, a.id, "manual:vip") == []
        assert [c.id for c in _resolve_audience(db, b.id, "manual:vip")] == [cb.id]
