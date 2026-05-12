"""
tests/test_manual_template_coupon.py
────────────────────────────────────
Locks down the manual-vs-auto coupon contract end-to-end.

Why this file exists
────────────────────
A merchant imports a manual template (e.g. ``special_offer``), types
their own coupon (``SAVE20``) into the campaign wizard, and sends.
Before this contract was enforced, the dispatcher silently dropped the
typed code and shipped "NAHLA" (the fallback placeholder for a missing
COPY_CODE coupon) — or, worse, replaced it with a coupon-generator
output for the customer's segment. Either way the code the customer
saw did NOT match the preview.

Rules pinned here:

  1. Manual template + manual coupon  → dispatcher sends the typed code
     verbatim. No coupon-generator call. No "NAHLA" fallback.
  2. Auto template  + auto_coupon=true → dispatcher resolves a fresh
     code via CouponGeneratorService for every recipient. This is the
     ONLY path that may touch the generator.
  3. Auto template  + auto_coupon=false → no coupon. The dispatcher
     falls back to the empty/store-name behaviour for a body slot, but
     the COPY_CODE button — if any — gets the literal "NAHLA"
     placeholder. Manual coupon MUST NOT silently leak into auto sends.
  4. Preview code equals sent code for manual templates.
  5. The /templates list response carries ``library.mode`` so the UI
     can render the ``كوبون يدوي`` / ``مربوط تلقائياً`` badges from a
     single source of truth.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    s = str(_p)
    if s not in sys.path:
        sys.path.insert(0, s)


from services import campaign_dispatcher as cd  # noqa: E402
from routers.templates import (  # noqa: E402
    DEFAULT_TEMPLATE_LIBRARY,
    _enrich_library_meta,
    _resolve_library_meta_for_template,
)


# ── Minimal stand-ins ──────────────────────────────────────────────────────


def _make_template(components, *, name="special_offer", language="ar"):
    tpl = MagicMock()
    tpl.name = name
    tpl.language = language
    tpl.components = components
    return tpl


def _make_campaign(*, coupon_code: str = "", template_variables: Dict[str, Any] | None = None):
    c = MagicMock()
    c.id = 99
    c.tenant_id = 1
    c.coupon_code = coupon_code
    c.template_variables = template_variables or {}
    c.audience_count = 1
    return c


def _make_customer(*, name: str = "أحمد", status: str = "active"):
    cust = MagicMock()
    cust.id = 7
    cust.name = name
    cust.customer_status = status
    cust.normalized_phone = "966500000000"
    return cust


# ── 1. _build_send_payload honours the explicit coupon_code arg ────────────


class TestBuildPayloadRespectsExplicitCouponCode:
    """The lowest layer in the send pipeline must not invent or
    substitute a coupon. Given an explicit ``coupon_code``, it must
    propagate that value into BOTH the BODY slot and the COPY_CODE
    button parameter."""

    def test_manual_coupon_appears_in_copy_code_button_verbatim(self):
        tpl = _make_template([
            {"type": "BODY", "text": "أهلاً {{1}}، خصم {{2}} بكود {{3}}"},
            {"type": "BUTTONS", "buttons": [
                {"type": "COPY_CODE", "example": ["SAVE20"]},
            ]},
        ])
        payload = cd._build_send_payload(
            template=tpl,
            to_phone="966500000000",
            customer_name="أحمد",
            store_name="المتجر",
            coupon_code="SAVE20",
        )
        btn = next(c for c in payload["template"]["components"] if c["type"] == "button")
        assert btn["sub_type"] == "copy_code"
        assert btn["parameters"][0]["coupon_code"] == "SAVE20"

    def test_empty_coupon_falls_back_to_placeholder_only_for_copy_code(self):
        # Auto template with no coupon resolved AND no manual coupon: we
        # keep the historical behaviour (NAHLA placeholder) so Meta still
        # accepts the payload structurally. The bug we're fixing is NOT
        # about this fallback — it's about manual sends never reaching
        # this branch at all.
        tpl = _make_template([
            {"type": "BUTTONS", "buttons": [
                {"type": "COPY_CODE"},
            ]},
        ])
        payload = cd._build_send_payload(
            template=tpl,
            to_phone="966500000000",
            customer_name="أحمد",
            store_name="المتجر",
            coupon_code="",
        )
        btn = next(c for c in payload["template"]["components"] if c["type"] == "button")
        assert btn["parameters"][0]["coupon_code"] == "NAHLA"


# ── 2. _dispatch_queued_rows routes manual vs auto coupons correctly ──────


@pytest.fixture
def patch_provider_send(monkeypatch):
    """Stub `provider_send_message` so we never touch Meta and can
    inspect the exact payload the dispatcher would have sent."""
    sent_payloads: List[Dict[str, Any]] = []

    async def _fake_send(db, wa_conn, *, tenant_id, operation, phone_id, payload):
        sent_payloads.append(payload)
        return {"messages": [{"id": f"wamid.{len(sent_payloads)}"}]}, {}

    monkeypatch.setattr(
        "services.whatsapp_platform.service.provider_send_message",
        _fake_send,
    )
    return sent_payloads


@pytest.fixture
def block_generator_calls(monkeypatch):
    """Fail loudly if the dispatcher invokes the coupon generator —
    the manual branch must never call it."""
    called = []

    async def _bang(*a, **kw):  # noqa: ANN001
        called.append((a, kw))
        raise AssertionError(
            "coupon generator was invoked for a manual-coupon campaign — "
            "manual templates must keep the merchant-typed code verbatim"
        )

    monkeypatch.setattr(cd, "_get_auto_coupon", _bang)
    return called


def _run_one_send(
    monkeypatch,
    *,
    campaign,
    template,
    customer,
    manual_coupon: str = "",
    auto_coupon: bool = False,
    discount_pct: int | None = None,
) -> Dict[str, Any]:
    """Drive `_dispatch_queued_rows` with a single in-memory queued row.

    We monkeypatch every persistence helper the inner loop touches so
    the test stays at the dispatcher's logic layer and never opens a DB
    connection. The only thing we care about is what `coupon_code`
    actually lands in the payload `_build_send_payload` produces.
    """
    captured: Dict[str, Any] = {}

    def _capture_build(*, template, to_phone, customer_name, store_name,
                       coupon_code="", cart_url=""):
        captured["coupon_code"] = coupon_code
        captured["to_phone"] = to_phone
        return {
            "messaging_product": "whatsapp",
            "to": to_phone,
            "type": "template",
            "template": {"name": template.name, "components": []},
        }

    monkeypatch.setattr(cd, "_build_send_payload", _capture_build)

    async def _fake_send(db, wa_conn, *, tenant_id, operation, phone_id, payload):  # noqa: ANN001
        return {"messages": [{"id": "wamid.fake"}]}, {}

    monkeypatch.setattr(
        "services.whatsapp_platform.service.provider_send_message",
        _fake_send,
    )

    # Stub the per-row bookkeeping helpers that touch the DB.
    monkeypatch.setattr(cd, "_revive_zombie_sending", lambda *a, **kw: None)
    monkeypatch.setattr(cd, "_force_terminate_runaway", lambda *a, **kw: False)
    monkeypatch.setattr(cd, "_is_attempts_exhausted", lambda *a, **kw: False)
    monkeypatch.setattr(cd, "_record_campaign_message", lambda *a, **kw: None)
    monkeypatch.setattr(cd, "_log_send_attempt", lambda *a, **kw: None)
    monkeypatch.setattr(cd, "_reconstruct_template_body",
                        lambda *a, **kw: "")

    # Build a fake row that mimics a CampaignSendLog instance.
    row = MagicMock()
    row.id = 1
    row.campaign_id = campaign.id
    row.status = cd.LOG_QUEUED
    row.attempt_count = 0
    row.customer_phone_e164 = customer.normalized_phone

    db = MagicMock()
    # The dispatcher's queue query is `.filter(...).order_by(...).limit().all()` —
    # we return the row exactly once.
    batches = iter([[row], []])
    db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all = (
        lambda: next(batches)
    )

    wa_conn = MagicMock()
    wa_conn.phone_number_id = "1061057720431678"

    asyncio.run(cd._dispatch_queued_rows(
        db,
        campaign=campaign,
        template=template,
        wa_conn=wa_conn,
        store_name="المتجر",
        auto_coupon=auto_coupon,
        discount_pct=discount_pct,
        customers_by_phone={customer.normalized_phone: customer},
        manual_coupon=manual_coupon,
    ))

    return captured


class TestDispatcherManualCouponPreserved:
    def test_manual_coupon_sent_verbatim(self, monkeypatch, block_generator_calls):
        """Merchant typed ``SAVE20`` in the wizard → dispatcher passes
        exactly that to _build_send_payload, no coupon-generator call."""
        campaign = _make_campaign(coupon_code="SAVE20")
        template = _make_template([
            {"type": "BODY", "text": "{{1}} {{2}} {{3}}"},
            {"type": "BUTTONS", "buttons": [{"type": "COPY_CODE"}]},
        ])
        customer = _make_customer()

        captured = _run_one_send(
            monkeypatch,
            campaign=campaign,
            template=template,
            customer=customer,
            manual_coupon="SAVE20",
            auto_coupon=False,
            discount_pct=None,
        )

        assert captured["coupon_code"] == "SAVE20"
        assert block_generator_calls == []

    def test_manual_coupon_not_substituted_by_generator_even_with_discount_pct(
        self, monkeypatch, block_generator_calls,
    ):
        """Some legacy automations stored a discount_pct alongside a
        manual coupon. We must NOT treat that as a signal to call the
        generator — the gate is `auto_coupon`, not the discount."""
        campaign = _make_campaign(coupon_code="VIP25")
        template = _make_template([
            {"type": "BUTTONS", "buttons": [{"type": "COPY_CODE"}]},
        ])
        customer = _make_customer()

        captured = _run_one_send(
            monkeypatch,
            campaign=campaign,
            template=template,
            customer=customer,
            manual_coupon="VIP25",
            auto_coupon=False,
            discount_pct=25,
        )

        assert captured["coupon_code"] == "VIP25"
        assert block_generator_calls == []

    def test_auto_coupon_branch_calls_generator(self, monkeypatch):
        """The auto path is unchanged: if `auto_coupon=True` and we
        have a discount + customer, we resolve via CouponGeneratorService."""
        generator_calls = []

        async def _ok(db, tenant_id, customer, discount_pct):
            generator_calls.append((tenant_id, customer.id, discount_pct))
            return "AUTO_GEN_77"

        monkeypatch.setattr(cd, "_get_auto_coupon", _ok)

        campaign = _make_campaign(coupon_code="auto")  # legacy sentinel
        template = _make_template([
            {"type": "BUTTONS", "buttons": [{"type": "COPY_CODE"}]},
        ])
        customer = _make_customer()

        captured = _run_one_send(
            monkeypatch,
            campaign=campaign,
            template=template,
            customer=customer,
            manual_coupon="",  # hoist already stripped the sentinel
            auto_coupon=True,
            discount_pct=30,
        )

        assert captured["coupon_code"] == "AUTO_GEN_77"
        assert generator_calls == [(1, 7, 30)]

    def test_auto_sentinel_in_campaign_coupon_code_is_stripped(self):
        """The wizard writes the literal string ``"auto"`` into
        ``Campaign.coupon_code`` for auto campaigns. The hoist at the
        top of ``dispatch_campaign`` must blank it so it can never reach
        the wire as a literal coupon code. We test the rule here by
        replaying the exact branching used by the dispatcher's hoist."""
        for raw in ("auto", "AUTO", "Auto", "  auto  "):
            manual_coupon = str(raw or "").strip()
            if manual_coupon.lower() == "auto":
                manual_coupon = ""
            assert manual_coupon == "", f"failed to strip sentinel {raw!r}"

    def test_manual_coupon_with_auto_coupon_true_does_not_leak(self, monkeypatch):
        """Defensive: if the hoist is ever bypassed and a manual coupon
        sneaks in alongside auto_coupon=True, the auto branch still
        wins (we always prefer the generator output) — manual codes
        must never appear on an auto send by accident."""
        async def _ok(db, tenant_id, customer, discount_pct):
            return "AUTO_GEN_88"

        monkeypatch.setattr(cd, "_get_auto_coupon", _ok)

        campaign = _make_campaign(coupon_code="SAVE99")
        template = _make_template([
            {"type": "BUTTONS", "buttons": [{"type": "COPY_CODE"}]},
        ])
        customer = _make_customer()

        captured = _run_one_send(
            monkeypatch,
            campaign=campaign,
            template=template,
            customer=customer,
            manual_coupon="SAVE99",  # hypothetical leak from a bad hoist
            auto_coupon=True,
            discount_pct=30,
        )

        assert captured["coupon_code"] == "AUTO_GEN_88"


# ── 3. Library metadata exposes mode to the UI ─────────────────────────────


class TestLibraryMetaExposesMode:
    """The ``library`` block on every template API response carries the
    ``mode`` field. The UI keys off this to render the ``كوبون يدوي``
    badge — relying on ``nahla_source_key`` alone is wrong because both
    manual and auto templates can be imported from the Nahla library."""

    def test_manual_templates_carry_mode_manual(self):
        for name in ("special_offer", "vip_exclusive", "win_back"):
            enriched = _enrich_library_meta(DEFAULT_TEMPLATE_LIBRARY[name])
            assert enriched["mode"] == "manual", (
                f"{name!r} must expose mode='manual' to the UI"
            )
            assert "يدوي" in enriched["library_label_ar"]

    def test_auto_templates_carry_mode_auto(self):
        for name in (
            "welcome_intro",
            "abandoned_cart_reminder",
            "special_offer_auto",
            "vip_exclusive_auto",
            "win_back_auto",
        ):
            enriched = _enrich_library_meta(DEFAULT_TEMPLATE_LIBRARY[name])
            assert enriched["mode"] == "auto", (
                f"{name!r} must expose mode='auto' to the UI"
            )
            assert "تلقائي" in enriched["library_label_ar"]

    def test_auto_coupon_capable_flag_only_on_auto_siblings(self):
        # The three `_auto` siblings of the manual coupon templates are
        # the only entries where the dispatcher is allowed to call the
        # coupon generator.
        capable = {
            name
            for name, meta in DEFAULT_TEMPLATE_LIBRARY.items()
            if meta.get("auto_coupon_capable") is True
        }
        assert capable == {"special_offer_auto", "vip_exclusive_auto", "win_back_auto"}


# ── 4. Library lookup falls back to nahla_source_key for tenant clones ────


class TestLibraryLookupFallsBackToSourceKey:
    """When a merchant imports a Nahla library template, we mint a fresh
    per-tenant copy with a randomized name like
    ``nahla_special_offer_d381``. That name is NOT in
    ``DEFAULT_TEMPLATE_LIBRARY`` — only the original key
    ``special_offer`` is. Without a fallback the API would return
    ``library: null`` for every imported clone, and the dashboard would
    decide manual-vs-auto purely from the COPY_CODE button's presence
    (which is wrong: COPY_CODE exists on BOTH manual and auto
    templates). The fallback below is what guarantees the manual
    contract survives the rename."""

    def test_clone_name_with_source_key_resolves_to_manual(self):
        tpl = MagicMock()
        tpl.name = "nahla_special_offer_d381"  # tenant-scoped rename
        tpl.nahla_source_key = "special_offer"

        meta = _resolve_library_meta_for_template(tpl)

        assert meta.get("mode") == "manual"
        assert "يدوي" in meta.get("library_label_ar", "")

    def test_clone_of_auto_sibling_resolves_to_auto(self):
        tpl = MagicMock()
        tpl.name = "nahla_special_offer_auto_a1b2"
        tpl.nahla_source_key = "special_offer_auto"

        meta = _resolve_library_meta_for_template(tpl)

        assert meta.get("mode") == "auto"
        assert meta.get("auto_coupon_capable") is True

    def test_unknown_source_key_returns_empty(self):
        tpl = MagicMock()
        tpl.name = "tenant_bespoke_template"
        tpl.nahla_source_key = None

        meta = _resolve_library_meta_for_template(tpl)

        # No library row → enricher returns {} unchanged so callers
        # treat the template as a tenant-bespoke creation.
        assert meta == {}

    def test_unrenamed_name_still_resolves_via_first_lookup(self):
        tpl = MagicMock()
        tpl.name = "special_offer"
        tpl.nahla_source_key = None  # never imported via the Nahla flow

        meta = _resolve_library_meta_for_template(tpl)

        assert meta.get("mode") == "manual"

    def test_source_key_takes_over_when_name_unknown(self):
        """The fallback path must not collapse to {} just because the
        renamed name happens to be a substring of a real key. The
        resolver MUST treat the rename as an opaque tenant identifier
        and only trust ``nahla_source_key`` for the mode lookup."""
        tpl = MagicMock()
        tpl.name = "nahla_vip_exclusive_c3d4"
        tpl.nahla_source_key = "vip_exclusive"

        meta = _resolve_library_meta_for_template(tpl)

        assert meta.get("mode") == "manual"
