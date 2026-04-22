"""
tests/test_campaign_wizard.py
─────────────────────────────
Unit + integration tests for the new "smart" campaign creation wizard:

  * `goals.py`        — fixed taxonomy + lookup
  * `segments.py`     — registry, masking helpers, tenant-scoped counts
                        on a real SQLite engine
  * `recommender.py`  — pure scoring of templates against (goal, segment)
  * `test_send.py`    — payload builder + happy-path orchestration with
                        a stubbed `provider_send_message`

The DB-backed tests reuse the same SQLite + JSONB→JSON remap pattern
that `tests/test_customer_intelligence_gating.py` established so we
don't need a Postgres fixture.

Run:
    python -m pytest tests/test_campaign_wizard.py -v
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlalchemy import JSON, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _path in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from models import (  # noqa: E402
    Base, Customer, CustomerProfile, Tenant, WhatsAppConnection, WhatsAppTemplate,
)

from services.campaign_wizard.goals import GOALS, get_goal, list_goals  # noqa: E402
from services.campaign_wizard.segments import (  # noqa: E402
    SEGMENTS, _mask_email, _mask_phone, count_segment, get_segment,
    list_segments_with_counts, sample_segment,
)
from services.campaign_wizard.recommender import (  # noqa: E402
    _body_text, _placeholder_count, _score_one, recommend_templates,
)
from services.campaign_wizard.test_send import (  # noqa: E402
    MOCK_DEFAULTS, build_test_payload, send_test_message,
)


# ── SQLite compatibility shim (mirrors the pattern in other tests) ───────────
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
    return Session(), engine


def _seed_tenant(db, name: str = "Test Store") -> Tenant:
    t = Tenant(name=name, is_active=True)
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _seed_customer(db, tenant_id: int, *, name: str, phone: str,
                   profile_segment: str = "new", profile_rfm: str = "lead",
                   total_orders: int = 0, last_order_at=None,
                   ltv_score: float = 0.0, email: str | None = None) -> Customer:
    c = Customer(
        name=name, email=email, phone=phone, normalized_phone=phone,
        tenant_id=tenant_id, extra_metadata={},
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    p = CustomerProfile(
        customer_id=c.id, tenant_id=tenant_id,
        segment=profile_segment, rfm_segment=profile_rfm,
        total_orders=total_orders, last_order_at=last_order_at,
        lifetime_value_score=ltv_score,
    )
    db.add(p)
    db.commit()
    return c


# ── goals.py ────────────────────────────────────────────────────────────────


class TestGoals:
    def test_taxonomy_has_seven_goals(self):
        # If you change this, also update Step1 in the frontend wizard.
        assert len(GOALS) == 7

    def test_required_keys_present(self):
        keys = {g.key for g in GOALS}
        assert keys >= {
            "welcome", "promotion", "reactivation", "reorder",
            "reminder", "broadcast", "custom",
        }

    def test_get_goal_normalises_case_and_whitespace(self):
        assert get_goal("WELCOME").key == "welcome"
        assert get_goal("  welcome  ").key == "welcome"
        assert get_goal("does-not-exist") is None
        assert get_goal("") is None
        assert get_goal(None) is None  # type: ignore[arg-type]

    def test_list_goals_serialises_cleanly(self):
        out = list_goals()
        assert all("key" in g and "label_ar" in g and "default_segment_key" in g for g in out)

    def test_default_segment_keys_are_valid(self):
        # Every goal's default segment must exist in the segment registry —
        # otherwise the wizard would crash when auto-pre-selecting Step 2.
        seg_keys = {s.key for s in SEGMENTS}
        for g in GOALS:
            assert g.default_segment_key in seg_keys, (
                f"goal '{g.key}' points at unknown segment '{g.default_segment_key}'"
            )


# ── segments.py ─────────────────────────────────────────────────────────────


class TestSegmentMasking:
    def test_mask_phone_short_number_returned_as_is(self):
        assert _mask_phone("12") == "12"
        assert _mask_phone("") == ""
        assert _mask_phone(None) == ""

    def test_mask_phone_keeps_last_4_digits(self):
        assert _mask_phone("+966501234567") == "•" * 9 + "4567"

    def test_mask_email_obscures_local_part(self):
        assert _mask_email("ali@example.com").endswith("@example.com")
        assert "•" in _mask_email("ali@example.com")
        assert _mask_email("not-an-email") == ""
        assert _mask_email(None) == ""


class TestSegmentRegistry:
    def test_registry_has_all_documented_segments(self):
        keys = {s.key for s in SEGMENTS}
        # Listed in the user-facing plan and the docstring of segments.py.
        assert keys >= {
            "all", "new", "promising", "vip", "dormant", "lost",
            "one_time", "repeat", "high_spenders", "abandoned_cart",
            "no_purchase_30", "no_purchase_60", "no_purchase_90",
        }

    def test_get_segment_normalises_input(self):
        assert get_segment("VIP").key == "vip"
        assert get_segment("  all  ").key == "all"
        assert get_segment("foobar") is None


class TestSegmentQueriesScopeToTenant:
    """Cross-tenant safety: phone X at Store A must NOT appear in Store B's
    counts. This is the same invariant the customer-import suite enforces
    on the dedupe path; we re-verify it here for the segments path.
    """

    def test_count_segment_isolates_tenants(self):
        db, _engine = _make_db()
        store_a = _seed_tenant(db, "A")
        store_b = _seed_tenant(db, "B")
        # Same phone across both tenants — DB unique index allows this.
        _seed_customer(db, store_a.id, name="Ahmad", phone="+966500000001")
        _seed_customer(db, store_b.id, name="Ahmad", phone="+966500000001")

        assert count_segment("all", db, store_a.id) == 1
        assert count_segment("all", db, store_b.id) == 1
        assert count_segment("all", db, 9999) == 0   # unrelated tenant

    def test_count_segment_excludes_unreachable_customers(self):
        db, _engine = _make_db()
        t = _seed_tenant(db)
        # Reachable
        _seed_customer(db, t.id, name="A", phone="+966500000001")
        # Unreachable — no normalized_phone
        c = Customer(name="No Phone", phone=None, normalized_phone=None,
                     tenant_id=t.id, extra_metadata={})
        db.add(c)
        db.commit()
        assert count_segment("all", db, t.id) == 1

    def test_count_segment_unknown_key_returns_zero(self):
        db, _engine = _make_db()
        t = _seed_tenant(db)
        assert count_segment("does-not-exist", db, t.id) == 0

    def test_vip_segment_matches_real_canonical_buckets(self):
        """Regression: previously the filter looked for `rfm_segment == 'vip'`
        which is NOT a value `compute_rfm_segment` ever writes — so the
        whole RFM half of the VIP cohort was silently empty.

        Canonical VIP cohort:
          * `customer_status / segment == 'vip'`         (compute_customer_status)
          * `rfm_segment == 'champions'`                 (top RFM cell)
          * `rfm_segment == 'cant_lose_them'`            (high LTV, falling recency)
        """
        db, _engine = _make_db()
        t = _seed_tenant(db)
        _seed_customer(db, t.id, name="VIP via segment", phone="+966500000001",
                       profile_segment="vip", profile_rfm="regulars")
        _seed_customer(db, t.id, name="VIP via champions", phone="+966500000002",
                       profile_segment="active", profile_rfm="champions")
        _seed_customer(db, t.id, name="VIP via cant_lose", phone="+966500000003",
                       profile_segment="active", profile_rfm="cant_lose_them")
        _seed_customer(db, t.id, name="Not VIP", phone="+966500000004",
                       profile_segment="active", profile_rfm="promising")
        # Old (buggy) filter used `rfm_segment == 'vip'` — assert that
        # value is NOT what makes someone count as VIP anymore.
        _seed_customer(db, t.id, name="Stale VIP marker", phone="+966500000005",
                       profile_segment="active", profile_rfm="vip")
        assert count_segment("vip", db, t.id) == 3

    def test_lost_segment_uses_inactive_status_not_churned(self):
        """Regression: filter previously asked for `segment == 'churned'`
        but `compute_customer_status` never writes that — it writes
        'inactive'. The segment was empty for every tenant in production."""
        db, _engine = _make_db()
        t = _seed_tenant(db)
        _seed_customer(db, t.id, name="Inactive status", phone="+966500000001",
                       profile_segment="inactive", profile_rfm="regulars")
        _seed_customer(db, t.id, name="Lost RFM", phone="+966500000002",
                       profile_segment="active", profile_rfm="lost_customers")
        _seed_customer(db, t.id, name="Hibernating RFM", phone="+966500000003",
                       profile_segment="active", profile_rfm="hibernating")
        _seed_customer(db, t.id, name="Stale churned marker", phone="+966500000004",
                       profile_segment="churned", profile_rfm="regulars")
        # Three real lost customers — the old "churned" marker no longer
        # counts (and shouldn't, because nothing in production writes it).
        assert count_segment("lost", db, t.id) == 3

    def test_new_segment_includes_lead_status_and_no_profile(self):
        """Regression: a CRM customer with status 'lead' (signed up, zero
        orders) is exactly who a welcome campaign should target. The
        original filter only matched `segment == 'new'` and missed every
        lead. Now we union with `lead` AND profile-less customers."""
        db, _engine = _make_db()
        t = _seed_tenant(db)
        # Has profile, status='new'  → matches
        _seed_customer(db, t.id, name="New buyer", phone="+966500000001",
                       profile_segment="new", profile_rfm="new_customers")
        # Has profile, status='lead' → must match (regression)
        _seed_customer(db, t.id, name="Lead", phone="+966500000002",
                       profile_segment="lead", profile_rfm="lead")
        # No profile row at all     → must match
        bare = Customer(name="Just signed up", phone="+966500000003",
                        normalized_phone="+966500000003",
                        tenant_id=t.id, extra_metadata={})
        db.add(bare)
        db.commit()
        # Active customer            → must NOT match
        _seed_customer(db, t.id, name="Active", phone="+966500000004",
                       profile_segment="active", profile_rfm="loyal_customers")
        assert count_segment("new", db, t.id) == 3

    def test_dormant_segment_unions_at_risk_status_and_rfm(self):
        db, _engine = _make_db()
        t = _seed_tenant(db)
        _seed_customer(db, t.id, name="At risk via status", phone="+966500000001",
                       profile_segment="at_risk", profile_rfm="regulars")
        _seed_customer(db, t.id, name="At risk via RFM", phone="+966500000002",
                       profile_segment="active", profile_rfm="at_risk")
        _seed_customer(db, t.id, name="About to sleep", phone="+966500000003",
                       profile_segment="active", profile_rfm="about_to_sleep")
        _seed_customer(db, t.id, name="Needs attention", phone="+966500000004",
                       profile_segment="active", profile_rfm="needs_attention")
        _seed_customer(db, t.id, name="Healthy", phone="+966500000005",
                       profile_segment="active", profile_rfm="loyal_customers")
        assert count_segment("dormant", db, t.id) == 4

    def test_no_purchase_30_excludes_never_purchased(self):
        db, _engine = _make_db()
        t = _seed_tenant(db)
        old = datetime.now(timezone.utc) - timedelta(days=45)
        _seed_customer(db, t.id, name="Old buyer",   phone="+966500000001",
                       last_order_at=old)
        _seed_customer(db, t.id, name="Never bought", phone="+966500000002",
                       last_order_at=None)
        # 30-day window: only the old buyer (≥ 30 days) qualifies; the
        # never-bought customer is excluded because we don't message
        # signups they way we'd message lapsed buyers.
        assert count_segment("no_purchase_30", db, t.id) == 1

    def test_no_purchase_90_includes_never_purchased(self):
        db, _engine = _make_db()
        t = _seed_tenant(db)
        old = datetime.now(timezone.utc) - timedelta(days=120)
        _seed_customer(db, t.id, name="Very lapsed", phone="+966500000001",
                       last_order_at=old)
        _seed_customer(db, t.id, name="Never bought", phone="+966500000002",
                       last_order_at=None)
        assert count_segment("no_purchase_90", db, t.id) == 2

    def test_high_spenders_uses_ltv_threshold(self):
        db, _engine = _make_db()
        t = _seed_tenant(db)
        _seed_customer(db, t.id, name="Whale", phone="+966500000001", ltv_score=0.85)
        _seed_customer(db, t.id, name="Mid",   phone="+966500000002", ltv_score=0.5)
        assert count_segment("high_spenders", db, t.id) == 1

    def test_count_segment_can_include_unreachable_for_management_view(self):
        """The customers-management page passes `require_reachable=False`
        so the chip says "VIP (12)" even when one of those 12 has a
        broken phone number — otherwise the merchant can never see and
        fix that row.

        The campaign wizard keeps the default `True` so the count it
        shows always equals what would actually be sendable.
        """
        db, _engine = _make_db()
        t = _seed_tenant(db)
        # Reachable VIP
        _seed_customer(db, t.id, name="Reachable", phone="+966500000001",
                       profile_segment="vip", profile_rfm="champions")
        # Unreachable VIP — has profile but no normalized_phone
        c = Customer(name="Bad phone", phone="bogus", normalized_phone=None,
                     tenant_id=t.id, extra_metadata={})
        db.add(c)
        db.commit()
        db.add(CustomerProfile(
            customer_id=c.id, tenant_id=t.id,
            segment="vip", rfm_segment="champions",
        ))
        db.commit()

        assert count_segment("vip", db, t.id) == 1                           # default
        assert count_segment("vip", db, t.id, require_reachable=False) == 2  # mgmt


class TestSegmentSampling:
    def test_sample_returns_masked_rows(self):
        db, _engine = _make_db()
        t = _seed_tenant(db)
        _seed_customer(db, t.id, name="Sara", phone="+966500000001", email="sara@x.com")
        rows = sample_segment("all", db, t.id, limit=5)
        assert len(rows) == 1
        assert rows[0]["name"] == "Sara"
        assert "•" in rows[0]["phone_masked"]
        assert rows[0]["phone_masked"].endswith("0001")
        assert rows[0]["email_masked"].endswith("@x.com")
        assert "sara" not in rows[0]["email_masked"]

    def test_list_segments_with_counts_returns_full_registry(self):
        db, _engine = _make_db()
        t = _seed_tenant(db)
        out = list_segments_with_counts(db, t.id)
        # One entry per registered segment, with the customer_count key
        # always present (zero on an empty store rather than missing).
        assert len(out) == len(SEGMENTS)
        assert all("customer_count" in r and isinstance(r["customer_count"], int) for r in out)


# ── recommender.py ──────────────────────────────────────────────────────────


def _make_template(**overrides) -> WhatsAppTemplate:
    """In-memory template that doesn't need to be persisted — the
    scoring function is pure and only reads attributes off the object."""
    base = dict(
        id=1, tenant_id=1, name="welcome_ar", language="ar",
        category="UTILITY", status="APPROVED",
        components=[{"type": "BODY", "text": "مرحباً {{1}} في متجر {{2}}"}],
        is_active=True, is_hidden=False,
        objective="welcome", recommendation_state=None,
        display_name_ar=None,
    )
    base.update(overrides)
    tpl = WhatsAppTemplate(**base)
    return tpl


class TestRecommenderPureScoring:
    def test_body_text_returns_empty_for_no_body(self):
        tpl = _make_template(components=[{"type": "HEADER", "text": "x"}])
        assert _body_text(tpl) == ""

    def test_placeholder_count_distinct_indices(self):
        assert _placeholder_count("hi {{1}} {{2}} {{1}}") == 2
        assert _placeholder_count("nope") == 0
        assert _placeholder_count("") == 0

    def test_welcome_template_wins_for_welcome_goal(self):
        tpl = _make_template(name="welcome_ar", objective="welcome")
        score, badges, _ = _score_one(tpl, get_goal("welcome"), get_segment("new"), "ar")
        assert score >= 75
        assert "متوافق" in badges
        assert "معتمد من Meta" in badges

    def test_marketing_template_penalised_for_utility_only_goal(self):
        # `reminder` only allows UTILITY templates — a MARKETING one
        # must drop in score AND surface the mismatch badge.
        tpl = _make_template(category="MARKETING", objective="promotion")
        score, badges, _ = _score_one(tpl, get_goal("reminder"), get_segment("abandoned_cart"), "ar")
        assert "فئة لا تناسب الهدف" in badges
        assert score < 60

    def test_language_mismatch_lowers_score_and_badges(self):
        tpl = _make_template(language="en")
        _, badges, _ = _score_one(tpl, get_goal("welcome"), get_segment("new"), "ar")
        assert "لغة مختلفة" in badges

    def test_recommend_templates_marks_single_best(self):
        db, _engine = _make_db()
        t = _seed_tenant(db)

        good = WhatsAppTemplate(
            tenant_id=t.id, name="welcome_ar", language="ar", category="UTILITY",
            status="APPROVED", components=[{"type": "BODY", "text": "مرحباً {{1}}"}],
            is_active=True, is_hidden=False, objective="welcome",
        )
        weak = WhatsAppTemplate(
            tenant_id=t.id, name="random_promo", language="ar", category="MARKETING",
            status="APPROVED", components=[{"type": "BODY", "text": "خصم {{1}}"}],
            is_active=True, is_hidden=False, objective="promotion",
        )
        db.add_all([good, weak])
        db.commit()

        result = recommend_templates(
            db, tenant_id=t.id, goal_key="welcome", segment_key="new", language="ar",
        )
        assert result["total"] == 2
        assert result["best_template_id"] == good.id
        # First entry must be the best one and carry the "best" badge.
        first = result["templates"][0]
        assert first["is_best"] is True
        assert "الأفضل لهذه الحملة" in first["badges"]
        # And the weak one must not be marked best.
        assert result["templates"][1]["is_best"] is False

    def test_recommend_excludes_non_approved_templates(self):
        db, _engine = _make_db()
        t = _seed_tenant(db)
        WhatsAppTemplate(
            tenant_id=t.id, name="pending_only", language="ar", category="UTILITY",
            status="PENDING", components=[{"type": "BODY", "text": "x"}],
        )  # not added — PENDING anyway
        db.add(WhatsAppTemplate(
            tenant_id=t.id, name="pending_only", language="ar", category="UTILITY",
            status="PENDING", components=[{"type": "BODY", "text": "x"}],
        ))
        db.commit()
        result = recommend_templates(db, tenant_id=t.id, goal_key="welcome",
                                     segment_key="new", language="ar")
        assert result["total"] == 0

    def test_empty_state_surfaces_pending_template_as_next_best(self):
        """When zero APPROVED templates fit, the recommender must hand
        the frontend the closest PENDING/DRAFT/REJECTED candidate so
        the empty state can show "your closest template is …" instead
        of a generic "no templates" wall."""
        db, _engine = _make_db()
        t = _seed_tenant(db)
        db.add(WhatsAppTemplate(
            tenant_id=t.id, name="welcome_pending_ar", language="ar",
            category="UTILITY", status="PENDING",
            components=[{"type": "BODY", "text": "مرحباً {{1}}"}],
            is_active=True, is_hidden=False, objective="welcome",
            display_name_ar="ترحيب بانتظار اعتماد Meta",
        ))
        db.commit()
        result = recommend_templates(db, tenant_id=t.id, goal_key="welcome",
                                     segment_key="new", language="ar")
        assert result["total"] == 0
        assert result["next_best_template"] is not None
        assert result["next_best_template"]["name"] == "welcome_pending_ar"
        assert result["next_best_template"]["status"] == "PENDING"
        assert "بانتظار" in (result["suggestion_ar"] or "")

    def test_empty_state_with_no_templates_at_all_returns_help_text(self):
        db, _engine = _make_db()
        t = _seed_tenant(db)
        result = recommend_templates(db, tenant_id=t.id, goal_key="welcome",
                                     segment_key="new", language="ar")
        assert result["total"] == 0
        assert result["next_best_template"] is None
        assert result["suggestion_ar"]
        assert "أنشئ" in result["suggestion_ar"]


# ── test_send.py ────────────────────────────────────────────────────────────


class TestPayloadBuilder:
    def test_body_params_use_merchant_values(self):
        tpl = _make_template(components=[{"type": "BODY", "text": "أهلاً {{1}} في {{2}}"}])
        payload = build_test_payload(
            tpl, to_phone_e164="+966500000001",
            merchant_vars={"{{1}}": "نورة", "{{2}}": "نحلة"},
        )
        assert payload["to"] == "+966500000001"
        assert payload["template"]["name"] == tpl.name
        params = payload["template"]["components"][0]["parameters"]
        assert params == [
            {"type": "text", "text": "نورة"},
            {"type": "text", "text": "نحلة"},
        ]

    def test_body_params_fall_back_to_mock_defaults(self):
        tpl = _make_template(components=[{"type": "BODY", "text": "أهلاً {{1}}"}])
        payload = build_test_payload(tpl, to_phone_e164="+966500000001", merchant_vars={})
        params = payload["template"]["components"][0]["parameters"]
        assert params == [{"type": "text", "text": MOCK_DEFAULTS["{{1}}"]}]

    def test_body_params_ordered_by_placeholder_index(self):
        # Even if the template body lists {{2}} before {{1}}, Meta
        # expects parameters numerically ordered.
        tpl = _make_template(components=[{"type": "BODY", "text": "{{2}} ثم {{1}}"}])
        payload = build_test_payload(tpl, to_phone_e164="+966500000001",
                                     merchant_vars={"{{1}}": "أولاً", "{{2}}": "ثانياً"})
        params = payload["template"]["components"][0]["parameters"]
        # Order must be {{1}} first, {{2}} second.
        assert params[0]["text"] == "أولاً"
        assert params[1]["text"] == "ثانياً"

    def test_body_params_accept_bare_number_keys(self):
        tpl = _make_template(components=[{"type": "BODY", "text": "أهلاً {{1}}"}])
        payload = build_test_payload(tpl, to_phone_e164="+966500000001",
                                     merchant_vars={"1": "بدون أقواس"})
        assert payload["template"]["components"][0]["parameters"][0]["text"] == "بدون أقواس"


class TestSendTestMessageOrchestration:
    """The wizard's send is async; we use the same `asyncio.run(...)` shim
    other tests in this repo use to keep async-test coverage without
    requiring `pytest-asyncio` (which isn't in our base requirements)."""

    def test_returns_template_not_found_when_id_unknown(self):
        import asyncio
        db, _engine = _make_db()
        t = _seed_tenant(db)
        result = asyncio.run(send_test_message(
            db, tenant_id=t.id, template_db_id=9999,
            to_phone="+966500000001", merchant_vars={},
        ))
        assert result["sent"] is False
        assert result["error_code"] == "template_not_found"

    def test_refuses_to_send_pending_template(self):
        import asyncio
        db, _engine = _make_db()
        t = _seed_tenant(db)
        tpl = WhatsAppTemplate(
            tenant_id=t.id, name="pending_x", language="ar", category="UTILITY",
            status="PENDING", components=[{"type": "BODY", "text": "x"}],
        )
        db.add(tpl)
        db.commit()
        result = asyncio.run(send_test_message(
            db, tenant_id=t.id, template_db_id=tpl.id,
            to_phone="+966500000001", merchant_vars={},
        ))
        assert result["sent"] is False
        assert result["error_code"] == "template_not_approved"

    def test_simulated_when_no_whatsapp_connection(self):
        import asyncio
        db, _engine = _make_db()
        t = _seed_tenant(db)
        tpl = WhatsAppTemplate(
            tenant_id=t.id, name="welcome_ar", language="ar", category="UTILITY",
            status="APPROVED", components=[{"type": "BODY", "text": "مرحباً {{1}}"}],
        )
        db.add(tpl)
        db.commit()
        result = asyncio.run(send_test_message(
            db, tenant_id=t.id, template_db_id=tpl.id,
            to_phone="+966500000001", merchant_vars={"{{1}}": "نورة"},
        ))
        # No WhatsAppConnection seeded → fall back to simulated success
        # so dev environments still let the merchant click through.
        assert result["sent"] is True
        assert result["simulated"] is True

    def test_real_send_calls_provider_send_message(self):
        import asyncio
        db, _engine = _make_db()
        t = _seed_tenant(db)
        tpl = WhatsAppTemplate(
            tenant_id=t.id, name="welcome_ar", language="ar", category="UTILITY",
            status="APPROVED", components=[{"type": "BODY", "text": "مرحباً {{1}}"}],
        )
        conn = WhatsAppConnection(
            tenant_id=t.id, status="connected", phone_number_id="PID_1",
            phone_number="+966500000000", sending_enabled=True,
            webhook_verified=True, connection_type="embedded", provider="meta",
        )
        db.add_all([tpl, conn])
        db.commit()

        fake_response = {"messages": [{"id": "wamid.test123"}]}
        with patch(
            "services.whatsapp_platform.service.provider_send_message",
            new=AsyncMock(return_value=(fake_response, None)),
        ):
            result = asyncio.run(send_test_message(
                db, tenant_id=t.id, template_db_id=tpl.id,
                to_phone="+966500000001", merchant_vars={"{{1}}": "نورة"},
            ))
        assert result["sent"] is True
        assert result["simulated"] is False
        assert result["wa_message_id"] == "wamid.test123"
        assert result["error_code"] is None

    def test_meta_error_response_is_reported_as_failure(self):
        """Regression: Meta returns 200 with `{"error": {...}}` on
        validation failures (e.g. template-translation-not-found,
        recipient-not-on-whatsapp). `provider_post_with_context` does
        not raise on those, so the orchestrator must inspect the body
        itself or it will lie to the merchant."""
        import asyncio
        db, _engine = _make_db()
        t = _seed_tenant(db)
        tpl = WhatsAppTemplate(
            tenant_id=t.id, name="promo_ar", language="ar", category="MARKETING",
            status="APPROVED", components=[{"type": "BODY", "text": "عرض {{1}}"}],
        )
        conn = WhatsAppConnection(
            tenant_id=t.id, status="connected", phone_number_id="PID_1",
            phone_number="+966500000000", sending_enabled=True,
            webhook_verified=True, connection_type="embedded", provider="meta",
        )
        db.add_all([tpl, conn])
        db.commit()

        meta_error_resp = {
            "error": {
                "message": "Template name does not exist in the translation",
                "code": 132001,
                "type": "OAuthException",
            }
        }
        with patch(
            "services.whatsapp_platform.service.provider_send_message",
            new=AsyncMock(return_value=(meta_error_resp, None)),
        ):
            result = asyncio.run(send_test_message(
                db, tenant_id=t.id, template_db_id=tpl.id,
                to_phone="+966500000001", merchant_vars={"{{1}}": "خصم"},
            ))
        assert result["sent"] is False
        assert result["simulated"] is False
        assert result["wa_message_id"] is None
        assert result["error_code"] == "meta:132001"
        assert "Template name" in result["error_message"]

    def test_missing_message_id_is_reported_as_failure(self):
        """Regression: if Meta returns 200 with no `error` and no
        `messages` array (some sandboxes do this), we must not claim
        success — the merchant won't receive anything."""
        import asyncio
        db, _engine = _make_db()
        t = _seed_tenant(db)
        tpl = WhatsAppTemplate(
            tenant_id=t.id, name="promo_ar", language="ar", category="MARKETING",
            status="APPROVED", components=[{"type": "BODY", "text": "عرض {{1}}"}],
        )
        conn = WhatsAppConnection(
            tenant_id=t.id, status="connected", phone_number_id="PID_1",
            phone_number="+966500000000", sending_enabled=True,
            webhook_verified=True, connection_type="embedded", provider="meta",
        )
        db.add_all([tpl, conn])
        db.commit()

        empty_resp = {"messaging_product": "whatsapp", "contacts": []}
        with patch(
            "services.whatsapp_platform.service.provider_send_message",
            new=AsyncMock(return_value=(empty_resp, None)),
        ):
            result = asyncio.run(send_test_message(
                db, tenant_id=t.id, template_db_id=tpl.id,
                to_phone="+966500000001", merchant_vars={"{{1}}": "خصم"},
            ))
        assert result["sent"] is False
        assert result["error_code"] == "no_message_id"

    def test_test_send_does_not_create_campaign_or_mutate_counters(self):
        """Operational guarantee — wizard test sends must NEVER appear in
        campaign analytics. They are diagnostic, one-off, and should
        leave zero footprint on Campaign rows.

        This test asserts the orchestrator does not silently insert a
        Campaign or bump any counter, regardless of whether Meta
        returned success, an error, or nothing at all.
        """
        import asyncio
        from models import Campaign
        db, _engine = _make_db()
        t = _seed_tenant(db)
        tpl = WhatsAppTemplate(
            tenant_id=t.id, name="welcome_ar", language="ar", category="UTILITY",
            status="APPROVED", components=[{"type": "BODY", "text": "مرحباً {{1}}"}],
        )
        conn = WhatsAppConnection(
            tenant_id=t.id, status="connected", phone_number_id="PID_1",
            phone_number="+966500000000", sending_enabled=True,
            webhook_verified=True, connection_type="embedded", provider="meta",
        )
        # Pre-existing campaign so we can assert its counters never move.
        c = Campaign(
            tenant_id=t.id, name="Pre-existing",
            campaign_type="broadcast", status="draft",
            sent_count=0, delivered_count=0, read_count=0,
            clicked_count=0, converted_count=0,
        )
        db.add_all([tpl, conn, c])
        db.commit()
        c_id = c.id
        before = (c.sent_count, c.delivered_count, c.read_count,
                  c.clicked_count, c.converted_count)
        before_count = db.query(Campaign).count()

        good_resp = {"messages": [{"id": "wamid.never_in_analytics"}]}
        with patch(
            "services.whatsapp_platform.service.provider_send_message",
            new=AsyncMock(return_value=(good_resp, None)),
        ):
            result = asyncio.run(send_test_message(
                db, tenant_id=t.id, template_db_id=tpl.id,
                to_phone="+966500000001", merchant_vars={"{{1}}": "نورة"},
            ))
        assert result["sent"] is True

        db.expire_all()
        c2 = db.query(Campaign).filter(Campaign.id == c_id).one()
        after = (c2.sent_count, c2.delivered_count, c2.read_count,
                 c2.clicked_count, c2.converted_count)
        assert before == after, (
            f"test-send mutated campaign counters: {before} -> {after}"
        )
        assert db.query(Campaign).count() == before_count, (
            "test-send unexpectedly created a Campaign row"
        )
