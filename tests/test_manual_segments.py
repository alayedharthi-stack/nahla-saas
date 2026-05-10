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

    # Reset the module-level mode-column probe cache. Each test gets
    # a fresh in-memory engine, but the cache lives at the module
    # level — without this reset, a test that drops the column would
    # poison the cache for every subsequent test that uses a fresh
    # schema where the column is present.
    try:
        import services.manual_segments as _ms
        _ms._MODE_COLUMN_AVAILABLE = None
    except Exception:
        pass

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


# ── 5. Customer-list filter — the production-bug regression ──────────────
#
# Production scenario reported by tenant 33: merchant adds the
# "promising" (عملاء واعدون) tag to a customer via the drawer, then
# selects the same segment in the filter dropdown — and the customer
# list returns zero rows. This test pins the contract that the two
# code paths must agree.


class TestCustomerListManualFilter:
    """Walks the same data path the dashboard hits:
       1. POST /customers/{id}/segments         (drawer "add tag")
       2. GET  /customers?manual_segment=<key>  (filter dropdown)

    Bypasses HTTP for speed but uses the *same* service helpers the
    routers do, so any divergence between add-write and filter-read
    is caught.
    """

    def test_promising_tag_is_findable_via_filter_immediately(self):
        # The exact production case: tag a customer with `promising`
        # and verify `customer_ids_with_manual_segment(... "promising")`
        # returns that customer's id with no other steps. The filter
        # endpoint reads from this helper, so green here ⇒ green
        # filter.
        db, _ = _make_db()
        t = _seed_tenant(db, "T-prod")
        haitham = _seed_customer(db, t.id, "+966542688511")  # "هيثم الحارثي"
        ali     = _seed_customer(db, t.id, "+966500000099")

        add_manual_segment(
            db, tenant_id=t.id, customer_id=haitham.id, segment_key="promising",
        )

        ids = customer_ids_with_manual_segment(db, t.id, "promising")
        assert ids == [haitham.id], (
            "Customer tagged 'promising' via drawer should appear in "
            "the customers-list manual_segment filter for the same key."
        )
        # Negative — the untagged customer is NOT in the result.
        assert ali.id not in ids

    def test_filter_key_is_normalised_like_drawer(self):
        # Drawer normalises the key via assert_known_segment (lowercase
        # + strip). Filter must do the same so "Promising" / " promising "
        # / "promising" all hit the same row set.
        db, _ = _make_db()
        t = _seed_tenant(db)
        c = _seed_customer(db, t.id, "+966500000001")
        add_manual_segment(
            db, tenant_id=t.id, customer_id=c.id, segment_key="VIP",  # caps
        )
        # All three spellings must resolve to the same canonical key.
        assert customer_ids_with_manual_segment(db, t.id, "vip")   == [c.id]
        assert customer_ids_with_manual_segment(db, t.id, "VIP")   == [c.id]
        assert customer_ids_with_manual_segment(db, t.id, " vip ") == [c.id]

    def test_filter_does_not_leak_across_tenants(self):
        db, _ = _make_db()
        a = _seed_tenant(db, "A")
        b = _seed_tenant(db, "B")
        ca = _seed_customer(db, a.id, "+966500000001")
        cb = _seed_customer(db, b.id, "+966500000002")
        add_manual_segment(db, tenant_id=a.id, customer_id=ca.id, segment_key="promising")
        add_manual_segment(db, tenant_id=b.id, customer_id=cb.id, segment_key="promising")
        # Tenant A's filter must only see ca.
        assert customer_ids_with_manual_segment(db, a.id, "promising") == [ca.id]
        assert customer_ids_with_manual_segment(db, b.id, "promising") == [cb.id]

    def test_unknown_key_passes_validation_layer_in_filter(self):
        # The router rejects unknown keys *before* hitting the helper,
        # but the helper itself must return [] for any key it can't
        # find, never raise. This keeps the helper safe to reuse from
        # campaign dispatch where unknown keys (e.g. legacy data) are
        # treated as "no match" not a hard 500.
        db, _ = _make_db()
        t = _seed_tenant(db)
        _seed_customer(db, t.id, "+966500000001")
        assert customer_ids_with_manual_segment(db, t.id, "nope_not_real") == []


# ── 7. Auto-chip union with manual tags — UX bug regression ──────────────
#
# Production scenario: merchant tags هيثم as "عملاء واعدون" via the
# drawer (manual). The chip strip "عملاء واعدون" uses RFM logic — and
# Haitham's RFM doesn't qualify, so he doesn't appear under the chip.
# But the chip and the drawer share the SAME label, so the merchant
# can't tell why their manual tag was ignored.
#
# Fix: when filtering by `segment=<key>` (auto chip), include manual
# matches for the same key. This test pins the contract that
# `auto_ids ∪ manual_ids` is the contract.


class TestAutoChipUnionWithManualTags:
    def test_manually_tagged_customer_appears_under_auto_chip(self):
        # End-to-end via the route handler isn't worth booting FastAPI
        # for; we exercise the same union logic directly.
        from services.manual_segments import customer_ids_with_manual_segment
        from services.nahla_segments import build_segment_query

        db, _ = _make_db()
        t = _seed_tenant(db, "T-union")

        # Customer A: manually tagged but RFM doesn't qualify.
        a = _seed_customer(db, t.id, "+966500000001")
        add_manual_segment(db, tenant_id=t.id, customer_id=a.id, segment_key="promising")

        # Customer B: untagged and RFM doesn't qualify either.
        _seed_customer(db, t.id, "+966500000002")

        # Auto-segment query (RFM-based) — both A and B fail it.
        auto_q = build_segment_query("promising", db, t.id, require_reachable=False)
        auto_ids = {row[0] for row in auto_q.with_entities(Customer.id).all()}
        manual_ids = set(customer_ids_with_manual_segment(db, t.id, "promising"))

        # The auto query returns nobody (no profile rows seeded).
        assert a.id not in auto_ids
        # The manual query returns A.
        assert manual_ids == {a.id}

        # Union: A appears, which is what the merchant expects.
        assert (auto_ids | manual_ids) == {a.id}

    def test_chip_filter_is_still_tenant_isolated_after_union(self):
        # The union must NOT leak manual tags from another tenant.
        from services.manual_segments import customer_ids_with_manual_segment

        db, _ = _make_db()
        a = _seed_tenant(db, "A")
        b = _seed_tenant(db, "B")
        ca = _seed_customer(db, a.id, "+966500000001")
        cb = _seed_customer(db, b.id, "+966500000002")
        add_manual_segment(db, tenant_id=a.id, customer_id=ca.id, segment_key="vip")
        add_manual_segment(db, tenant_id=b.id, customer_id=cb.id, segment_key="vip")

        # Tenant A's manual set must not include B's customer.
        assert customer_ids_with_manual_segment(db, a.id, "vip") == [ca.id]
        assert customer_ids_with_manual_segment(db, b.id, "vip") == [cb.id]


# ── 8. Include / Exclude mode (migration 0053) ────────────────────────────
#
# The unified-segment-membership formula is:
#     member ⇔ (auto_match ∨ manual_include) ∧ ¬ manual_exclude
# These tests pin all four corners.


class TestIncludeExcludeMode:
    def test_default_mode_is_include(self):
        from services.manual_segments import MODE_INCLUDE
        db, _ = _make_db()
        t = _seed_tenant(db)
        c = _seed_customer(db, t.id, "+966500000001")
        row = add_manual_segment(
            db, tenant_id=t.id, customer_id=c.id, segment_key="vip",
        )
        assert row.mode == MODE_INCLUDE

    def test_explicit_exclude_creates_exclude_row(self):
        from services.manual_segments import (
            MODE_EXCLUDE, customer_ids_with_manual_segment,
        )
        db, _ = _make_db()
        t = _seed_tenant(db)
        c = _seed_customer(db, t.id, "+966500000001")
        row = add_manual_segment(
            db, tenant_id=t.id, customer_id=c.id,
            segment_key="vip", mode=MODE_EXCLUDE,
        )
        assert row.mode == MODE_EXCLUDE
        # Default-mode (include) lookup misses it.
        assert customer_ids_with_manual_segment(db, t.id, "vip") == []
        # Exclude-mode lookup finds it.
        assert customer_ids_with_manual_segment(
            db, t.id, "vip", mode=MODE_EXCLUDE,
        ) == [c.id]

    def test_re_tag_with_different_mode_flips_in_place(self):
        from services.manual_segments import (
            MODE_EXCLUDE, MODE_INCLUDE,
            customer_ids_with_manual_segment,
        )
        db, _ = _make_db()
        t = _seed_tenant(db)
        c = _seed_customer(db, t.id, "+966500000001")

        add_manual_segment(
            db, tenant_id=t.id, customer_id=c.id,
            segment_key="vip", mode=MODE_INCLUDE,
        )
        # Now flip to exclude — same row, mode field changes.
        add_manual_segment(
            db, tenant_id=t.id, customer_id=c.id,
            segment_key="vip", mode=MODE_EXCLUDE,
        )
        assert customer_ids_with_manual_segment(db, t.id, "vip") == []
        assert customer_ids_with_manual_segment(
            db, t.id, "vip", mode=MODE_EXCLUDE,
        ) == [c.id]
        # Still exactly one row — no duplicates from the re-insert.
        assert (
            db.query(CustomerSegmentManual)
            .filter_by(tenant_id=t.id, customer_id=c.id, segment_key="vip")
            .count() == 1
        )

    def test_smart_remove_when_only_manual_deletes_row(self):
        from services.manual_segments import (
            list_manual_segments_for_customer, smart_remove_manual_segment,
        )
        db, _ = _make_db()
        t = _seed_tenant(db)
        c = _seed_customer(db, t.id, "+966500000001")
        add_manual_segment(db, tenant_id=t.id, customer_id=c.id, segment_key="vip")
        assert list_manual_segments_for_customer(db, t.id, c.id) == ["vip"]

        action = smart_remove_manual_segment(
            db, tenant_id=t.id, customer_id=c.id,
            segment_key="vip", auto_match=False,
        )
        assert action == "deleted"
        assert list_manual_segments_for_customer(db, t.id, c.id) == []

    def test_smart_remove_when_auto_match_creates_exclude(self):
        from services.manual_segments import (
            MODE_EXCLUDE, customer_ids_with_manual_segment,
            list_manual_segments_for_customer, smart_remove_manual_segment,
        )
        db, _ = _make_db()
        t = _seed_tenant(db)
        c = _seed_customer(db, t.id, "+966500000001")
        # No manual row to start; auto classifier matches.
        action = smart_remove_manual_segment(
            db, tenant_id=t.id, customer_id=c.id,
            segment_key="vip", auto_match=True,
        )
        assert action == "excluded"
        # No include row.
        assert list_manual_segments_for_customer(db, t.id, c.id) == []
        # But exclude row exists.
        assert customer_ids_with_manual_segment(
            db, t.id, "vip", mode=MODE_EXCLUDE,
        ) == [c.id]

    def test_smart_remove_flips_existing_include_to_exclude_on_auto_match(self):
        from services.manual_segments import (
            MODE_EXCLUDE, customer_ids_with_manual_segment,
            list_manual_segments_for_customer, smart_remove_manual_segment,
        )
        db, _ = _make_db()
        t = _seed_tenant(db)
        c = _seed_customer(db, t.id, "+966500000001")
        # Existing include row — and auto classifier ALSO matches.
        add_manual_segment(db, tenant_id=t.id, customer_id=c.id, segment_key="vip")

        action = smart_remove_manual_segment(
            db, tenant_id=t.id, customer_id=c.id,
            segment_key="vip", auto_match=True,
        )
        assert action == "excluded"
        # Include is now exclude.
        assert list_manual_segments_for_customer(db, t.id, c.id) == []
        assert customer_ids_with_manual_segment(
            db, t.id, "vip", mode=MODE_EXCLUDE,
        ) == [c.id]

    def test_smart_remove_when_nothing_present_is_noop(self):
        from services.manual_segments import smart_remove_manual_segment
        db, _ = _make_db()
        t = _seed_tenant(db)
        c = _seed_customer(db, t.id, "+966500000001")
        action = smart_remove_manual_segment(
            db, tenant_id=t.id, customer_id=c.id,
            segment_key="vip", auto_match=False,
        )
        assert action == "noop"

    def test_list_manual_sources_returns_mode_per_segment(self):
        from services.manual_segments import (
            MODE_EXCLUDE, list_manual_sources_for_customer,
        )
        db, _ = _make_db()
        t = _seed_tenant(db)
        c = _seed_customer(db, t.id, "+966500000001")
        add_manual_segment(db, tenant_id=t.id, customer_id=c.id, segment_key="vip")
        add_manual_segment(
            db, tenant_id=t.id, customer_id=c.id,
            segment_key="dormant", mode=MODE_EXCLUDE,
        )
        sources = list_manual_sources_for_customer(db, t.id, c.id)
        assert sources == {"vip": "include", "dormant": "exclude"}

    def test_unknown_mode_raises(self):
        db, _ = _make_db()
        t = _seed_tenant(db)
        c = _seed_customer(db, t.id, "+966500000001")
        with pytest.raises(ValueError):
            add_manual_segment(
                db, tenant_id=t.id, customer_id=c.id,
                segment_key="vip", mode="bogus",
            )

    def test_list_manual_segments_bulk_excludes_exclude_rows(self):
        # Bulk helper feeds the customer-list "manual_segments" field
        # which is the *positive* tag list. Exclude rows must NOT
        # leak into it — otherwise the drawer would show "VIP يدوي"
        # for a customer the merchant deliberately removed from VIP.
        from services.manual_segments import (
            MODE_EXCLUDE, list_manual_segments_bulk,
        )
        db, _ = _make_db()
        t = _seed_tenant(db)
        c = _seed_customer(db, t.id, "+966500000001")
        add_manual_segment(
            db, tenant_id=t.id, customer_id=c.id,
            segment_key="vip", mode=MODE_EXCLUDE,
        )
        bulk = list_manual_segments_bulk(db, t.id, [c.id])
        assert bulk == {} or bulk.get(c.id, []) == []


# ── 9. Pre-0053 schema fallback (production-deploy regression) ────────────
#
# Background
# ──────────
# Migration 0053 added ``customer_segments_manual.mode``. After
# commit 2352c3f0 deployed, the customers page started showing
# ``لا يوجد عملاء`` even though the chip strip reported 7,996
# customers. Root cause: the new helpers queried ``mode = 'include'``
# unconditionally, which 500'd against any schema where the migration
# hadn't applied yet — taking the entire ``GET /customers`` endpoint
# down with it.
#
# The contract proven below: every helper that can encounter a
# pre-0053 schema must degrade gracefully (treat every row as
# ``include``) instead of raising.


class TestPre0053SchemaFallback:
    def _drop_mode_column(self, engine):
        """Simulate a pre-0053 production database by dropping the
        ``mode`` column from the live SQLite schema. We also clear
        the module-level cache so the next probe re-runs against the
        modified schema."""
        from sqlalchemy import text

        import services.manual_segments as ms
        ms._MODE_COLUMN_AVAILABLE = None
        # SQLite refuses to DROP COLUMN while an index references the
        # column; drop the composite index first, then the column.
        with engine.begin() as conn:
            conn.execute(text(
                "DROP INDEX IF EXISTS ix_customer_segments_manual_tenant_segment_mode"
            ))
            conn.execute(text(
                "ALTER TABLE customer_segments_manual DROP COLUMN mode"
            ))

    def test_list_manual_segments_bulk_does_not_raise_without_mode(self):
        from services.manual_segments import list_manual_segments_bulk
        db, engine = _make_db()
        t = _seed_tenant(db)
        c = _seed_customer(db, t.id, "+966500000001")
        # Snapshot ids before closing the session so the test body
        # doesn't touch detached ORM instances after the DDL.
        tenant_id, customer_id = t.id, c.id
        add_manual_segment(db, tenant_id=tenant_id, customer_id=customer_id, segment_key="vip")
        db.close()
        self._drop_mode_column(engine)

        Session = sessionmaker(bind=engine)
        db2 = Session()
        try:
            result = list_manual_segments_bulk(db2, tenant_id, [customer_id])
            assert result == {customer_id: ["vip"]}
        finally:
            db2.close()

    def test_list_manual_sources_bulk_treats_legacy_rows_as_include(self):
        from services.manual_segments import (
            MODE_INCLUDE, list_manual_sources_bulk,
        )
        db, engine = _make_db()
        t = _seed_tenant(db)
        c = _seed_customer(db, t.id, "+966500000001")
        tenant_id, customer_id = t.id, c.id
        add_manual_segment(db, tenant_id=tenant_id, customer_id=customer_id, segment_key="vip")
        db.close()
        self._drop_mode_column(engine)

        Session = sessionmaker(bind=engine)
        db2 = Session()
        try:
            result = list_manual_sources_bulk(db2, tenant_id, [customer_id])
            assert result == {customer_id: {"vip": MODE_INCLUDE}}
        finally:
            db2.close()

    def test_customer_ids_with_manual_segment_returns_empty_for_exclude_on_legacy(self):
        # On a legacy schema there can't be any exclude rows, so the
        # helper must return [] for mode=exclude rather than crash.
        from services.manual_segments import (
            MODE_EXCLUDE, MODE_INCLUDE, customer_ids_with_manual_segment,
        )
        db, engine = _make_db()
        t = _seed_tenant(db)
        c = _seed_customer(db, t.id, "+966500000001")
        tenant_id, customer_id = t.id, c.id
        add_manual_segment(db, tenant_id=tenant_id, customer_id=customer_id, segment_key="vip")
        db.close()
        self._drop_mode_column(engine)

        Session = sessionmaker(bind=engine)
        db2 = Session()
        try:
            assert customer_ids_with_manual_segment(
                db2, tenant_id, "vip", mode=MODE_INCLUDE,
            ) == [customer_id]
            assert customer_ids_with_manual_segment(
                db2, tenant_id, "vip", mode=MODE_EXCLUDE,
            ) == []
        finally:
            db2.close()

    def test_list_manual_segments_for_customer_falls_back_silently(self):
        from services.manual_segments import list_manual_segments_for_customer
        db, engine = _make_db()
        t = _seed_tenant(db)
        c = _seed_customer(db, t.id, "+966500000001")
        tenant_id, customer_id = t.id, c.id
        add_manual_segment(db, tenant_id=tenant_id, customer_id=customer_id, segment_key="vip")
        db.close()
        self._drop_mode_column(engine)

        Session = sessionmaker(bind=engine)
        db2 = Session()
        try:
            assert list_manual_segments_for_customer(db2, tenant_id, customer_id) == ["vip"]
        finally:
            db2.close()


# ── 10. Customers list endpoint smoke contract ───────────────────────────
#
# These pin the bug we shipped in 2352c3f0 → fixed shortly after:
# "all customers" / no-filter requests must return rows, not [].


class TestCustomersListEndpointContract:
    """In-process FastAPI smoke tests for ``GET /customers``."""

    def _call_list(self, engine, tenant_id: int, **query_kwargs):
        """Direct in-process call to ``list_customers`` — bypasses
        FastAPI's request lifecycle (which adds a per-request thread
        and breaks the in-memory SQLite engine the test fixture
        produces). The test goal is to pin the SQL paths the
        endpoint takes, not the HTTP layer."""
        import asyncio

        from routers import customers as customers_router

        Session = sessionmaker(bind=engine)
        db = Session()

        # Stub resolve_tenant_id on the customers router module
        # because that's the binding the route function actually
        # closes over.
        original_resolve = customers_router.resolve_tenant_id
        customers_router.resolve_tenant_id = (  # type: ignore
            lambda request, db=None: tenant_id
        )

        # Build a minimal "request" stand-in; the route only uses it
        # for resolve_tenant_id, which we've already monkey-patched.
        class _FakeReq:
            headers: dict = {}
            cookies: dict = {}
            state = type("S", (), {})()

        req = _FakeReq()

        # Default values match the FastAPI Query(...) defaults — we
        # pass them explicitly because invoking the route function
        # directly (rather than via FastAPI) bypasses dependency
        # resolution, so unset args become the Query(...) sentinel.
        defaults = dict(
            search="",
            segment="",
            manual_segment="",
            marketing_opt_out=None,
            test_recipient=None,
            page=1,
            per_page=50,
        )
        defaults.update(query_kwargs)

        try:
            return asyncio.run(
                customers_router.list_customers(
                    request=req, db=db, **defaults,
                )
            )
        finally:
            customers_router.resolve_tenant_id = original_resolve
            db.close()

    def test_no_segment_filter_returns_all_customers(self):
        # The exact bug: after the unified-segments commit, the page
        # returned [] for tenants whose customers had no manual rows.
        db, engine = _make_db()
        t = _seed_tenant(db, "Big")
        seeded = [_seed_customer(db, t.id, f"+96650000000{i}") for i in range(5)]
        tenant_id = t.id
        seeded_ids = {c.id for c in seeded}
        db.close()

        result = self._call_list(engine, tenant_id)
        assert result["total"] == 5
        assert {c["id"] for c in result["customers"]} == seeded_ids

    def test_segment_all_is_treated_as_no_filter(self):
        db, engine = _make_db()
        t = _seed_tenant(db, "Big")
        for i in range(3):
            _seed_customer(db, t.id, f"+96650000000{i}")
        tenant_id = t.id
        db.close()

        result = self._call_list(engine, tenant_id, segment="all")
        assert result["total"] == 3

    def test_empty_manual_segment_param_is_no_filter(self):
        # The "كل التصنيفات اليدوية" dropdown sends an empty string;
        # it must NOT trigger the manual-segment filter and collapse
        # the result set to customers with manual tags only.
        db, engine = _make_db()
        t = _seed_tenant(db, "Big")
        for i in range(3):
            _seed_customer(db, t.id, f"+96650000000{i}")
        tenant_id = t.id
        db.close()

        result = self._call_list(engine, tenant_id, manual_segment="")
        assert result["total"] == 3

    def _call_delete(self, engine, tenant_id: int, customer_id: int, segment_key: str):
        """Direct in-process call to ``remove_customer_segment``."""
        import asyncio

        from routers import customers as customers_router

        Session = sessionmaker(bind=engine)
        db = Session()

        original_resolve = customers_router.resolve_tenant_id
        customers_router.resolve_tenant_id = (  # type: ignore
            lambda request, db=None: tenant_id
        )

        class _FakeReq:
            headers: dict = {}
            cookies: dict = {}
            state = type("S", (), {})()

        try:
            return asyncio.run(
                customers_router.remove_customer_segment(
                    customer_id=customer_id,
                    segment_key=segment_key,
                    request=_FakeReq(),
                    db=db,
                )
            )
        finally:
            customers_router.resolve_tenant_id = original_resolve
            db.close()

    def test_delete_segment_on_pre_0053_schema_does_not_crash(self):
        # The exact ticket: clicking "إزالة" on a tagged customer
        # would 500 on a tenant whose database hadn't seen 0053 yet.
        # Now we degrade to a legacy plain-delete and return 200.
        from sqlalchemy import text

        import services.manual_segments as ms

        db, engine = _make_db()
        t = _seed_tenant(db, "Big")
        c = _seed_customer(db, t.id, "+966500000001")
        # Seed a manual include row for "promising" — this is what
        # هيثم's row looks like in production.
        add_manual_segment(
            db, tenant_id=t.id, customer_id=c.id, segment_key="promising",
        )
        tenant_id, customer_id = t.id, c.id
        db.close()

        # Drop the column to simulate the deploy-window state.
        ms._MODE_COLUMN_AVAILABLE = None
        with engine.begin() as conn:
            conn.execute(text(
                "DROP INDEX IF EXISTS ix_customer_segments_manual_tenant_segment_mode"
            ))
            conn.execute(text(
                "ALTER TABLE customer_segments_manual DROP COLUMN mode"
            ))

        try:
            result = self._call_delete(engine, tenant_id, customer_id, "promising")
            # Endpoint must return 200-shaped JSON, not raise / 500.
            assert result["ok"] is True
            assert result["mode_column_available"] is False
            # Action is either "deleted" (no auto match) or
            # "deleted_legacy" (auto match but no exclude support).
            assert result["action"] in {"deleted", "deleted_legacy"}
            assert result["segment_key"] == "promising"
        finally:
            ms._MODE_COLUMN_AVAILABLE = None

    def test_delete_segment_with_modern_schema_converts_to_exclude_when_auto_matches(self):
        # On the modern schema, when the auto classifier still
        # matches, smart-remove must create an exclude row so the
        # customer doesn't pop back into the segment one second
        # after the merchant removed them.
        import services.manual_segments as ms

        db, engine = _make_db()
        t = _seed_tenant(db, "Big")
        c = _seed_customer(db, t.id, "+966500000001")
        add_manual_segment(
            db, tenant_id=t.id, customer_id=c.id, segment_key="promising",
        )
        tenant_id, customer_id = t.id, c.id
        db.close()

        # Force the auto-match path — easiest by monkey-patching
        # the helper, since we don't want to seed RFM data.
        import routers.customers as customers_router
        original_match = customers_router._customer_matches_auto_segment
        customers_router._customer_matches_auto_segment = (  # type: ignore
            lambda db, tid, cid, key: True
        )
        try:
            result = self._call_delete(engine, tenant_id, customer_id, "promising")
            assert result["ok"] is True
            assert result["mode_column_available"] is True
            assert result["auto_match"] is True
            assert result["action"] == "excluded"
        finally:
            customers_router._customer_matches_auto_segment = original_match
            ms._MODE_COLUMN_AVAILABLE = None

    def test_delete_segment_with_arabic_label_returns_clean_error(self):
        # Frontend bug-or-future-feature: if the merchant sends an
        # Arabic label like "عملاء واعدون" instead of the canonical
        # English key, we must NOT 500. We surface ok=false with a
        # clear message and the unrecognised key echoed back.
        db, engine = _make_db()
        t = _seed_tenant(db, "Big")
        c = _seed_customer(db, t.id, "+966500000001")
        tenant_id, customer_id = t.id, c.id
        db.close()

        result = self._call_delete(
            engine, tenant_id, customer_id, "عملاء واعدون",
        )
        assert result["ok"] is False
        assert result["code"] == "unknown_segment"
        assert result["segment_key_received"] == "عملاء واعدون"
        assert result["action"] == "failed"
        # And the message is in Arabic so the UI can render it as-is.
        assert "تصنيف" in result["message"] or "نحلة" in result["message"]

    def test_filter_by_promising_includes_manual_include_only_customers(self):
        # The user's complaint: هيثم was manually tagged as
        # promising but the chip filter "عملاء واعدون" didn't show
        # him. The unified-segment formula must include him via
        # manual_include even when the auto classifier disagrees.
        db, engine = _make_db()
        t = _seed_tenant(db, "Big")
        # Seed two customers — neither will match the auto
        # "promising" classifier (no orders, no profile), so the
        # only way for them to surface under that chip is via the
        # manual include row.
        c1 = _seed_customer(db, t.id, "+966500000001")
        c2 = _seed_customer(db, t.id, "+966500000002")
        add_manual_segment(
            db, tenant_id=t.id, customer_id=c1.id, segment_key="promising",
        )
        # c2 stays untagged — should NOT appear in the filter.
        tenant_id, c1_id, c2_id = t.id, c1.id, c2.id
        db.close()

        result = self._call_list(engine, tenant_id, segment="promising")
        ids = {c["id"] for c in result["customers"]}
        assert c1_id in ids, "manual_include must surface customer in chip filter"
        assert c2_id not in ids, "untagged customer must NOT surface"

    def test_endpoint_does_not_crash_on_pre_0053_schema(self):
        # The exact regression that took the customers page down on
        # tenant 33: helpers that referenced ``mode`` blew up against
        # a schema where the migration hadn't run, and the whole
        # endpoint 500'd → frontend fell back to "لا يوجد عملاء".
        from sqlalchemy import text

        import services.manual_segments as ms

        db, engine = _make_db()
        t = _seed_tenant(db, "Big")
        for i in range(2):
            _seed_customer(db, t.id, f"+96650000000{i}")
        tenant_id = t.id
        db.close()

        # Drop the column AFTER seeding — simulates the deploy where
        # the new code reaches the worker before the migration ran.
        ms._MODE_COLUMN_AVAILABLE = None
        with engine.begin() as conn:
            conn.execute(text(
                "DROP INDEX IF EXISTS ix_customer_segments_manual_tenant_segment_mode"
            ))
            conn.execute(text(
                "ALTER TABLE customer_segments_manual DROP COLUMN mode"
            ))

        try:
            result = self._call_list(engine, tenant_id)
            assert result["total"] == 2
        finally:
            ms._MODE_COLUMN_AVAILABLE = None


# ── 6. Lazy billing reconcile — guard against re-introducing the bug ─────
#
# Tenant 33 sat at "pending_payment" because no caller ever invoked
# the activation helper after Moyasar's redirect. The fix wires the
# helper into /billing/status and /billing/entitlements with an
# in-memory cooldown. This test pins the cooldown contract so a
# careless future refactor doesn't accidentally call Moyasar once per
# page mount.


class TestLazyReconcileCooldown:
    def test_cooldown_blocks_repeat_attempt_for_pending_sub(self):
        # Importing here so the test file doesn't fail collection on
        # environments where billing isn't installed.
        from services.billing_activation import (
            _LAZY_RECONCILE_LAST,
            _should_attempt_lazy_reconcile,
        )
        from models import BillingSubscription

        _LAZY_RECONCILE_LAST.clear()
        sub = BillingSubscription(
            id=999, tenant_id=33, plan_id=2, status="pending_payment",
            extra_metadata={"moyasar_invoice_id": "inv_xyz"},
        )
        # First call passes — first time we've seen this sub.
        assert _should_attempt_lazy_reconcile(sub) is True
        # Immediate retry must be blocked.
        assert _should_attempt_lazy_reconcile(sub) is False

    def test_active_sub_never_triggers_reconcile(self):
        from services.billing_activation import (
            _LAZY_RECONCILE_LAST,
            _should_attempt_lazy_reconcile,
        )
        from models import BillingSubscription

        _LAZY_RECONCILE_LAST.clear()
        sub = BillingSubscription(
            id=1000, tenant_id=33, plan_id=2, status="active",
            extra_metadata={"moyasar_invoice_id": "inv_xyz"},
        )
        # Already active → no Moyasar call ever needed.
        assert _should_attempt_lazy_reconcile(sub) is False

    def test_pending_without_moyasar_invoice_id_is_skipped(self):
        # A pending sub with no Moyasar invoice (e.g. a manually-created
        # row from the admin panel) has nothing to reconcile against,
        # so we must NOT call Moyasar at all.
        from services.billing_activation import (
            _LAZY_RECONCILE_LAST,
            _should_attempt_lazy_reconcile,
        )
        from models import BillingSubscription

        _LAZY_RECONCILE_LAST.clear()
        sub = BillingSubscription(
            id=1001, tenant_id=33, plan_id=2, status="pending_payment",
            extra_metadata={},  # no moyasar_invoice_id
        )
        assert _should_attempt_lazy_reconcile(sub) is False
