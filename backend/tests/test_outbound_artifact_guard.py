"""
backend/tests/test_outbound_artifact_guard.py
─────────────────────────────────────────────
May 2026 #37 — D2 / "guard outbound artifact promises".

Regression suite for the hollow-affirmation guard
(:func:`modules.ai.postprocess.safety_nets.apply_outbound_artifact_guard`).

The production trace this guard closes::

    Customer: "عطني رقم أمين"
    Bot:      "أبشر 🌷"
    Customer: "هيا عطني"
    Bot:      "تفضل أبو خلف 🌷"
    Customer: "ما جاني شي"

The guard runs AFTER every other safety net. By the time it sees
a reply, the upstream chain has already had a chance to inject
URLs / phones / contact cards. The guard's role is to catch
"the LLM said it would deliver but didn't" — short standalone
affirmations that imply delivery without carrying the artifact.

Coverage matches the user-facing spec exactly:

  * "عطني رقم أمين" + reply "أبشر" + KB has no phone for أمين →
    rewrite to honest "أحتاج إضافة رقم أمين …".
  * "عطني رقم أمين" + reply "تفضل" + KB has أمين's phone →
    inject ``"تفضل رقم أمين: 0541690226 🌷"``.
  * "أبي باركود الراجحي" + reply has phone but no barcode media →
    rewrite to "المتوفر حاليًا رقم التحويل، ولم تتم إضافة صورة باركود الراجحي بعد".
  * "وين موقعكم" + reply "تفضل" + tenant has maps_url →
    inject the maps URL.
  * "أبي رابط المتجر" + reply "تفضل" + tenant has store_url →
    inject the store URL via the canonical "تفضل رابط متجرنا 🌷" reply.
  * Reply already carrying the artifact → ``action="pass"``.
  * Reply already saying "غير متوفر" / "لم تتم إضافة" → ``action="pass"``.
  * Long natural-prose reply without the artifact → ``action="pass"``
    (we don't override substantive text — we only catch hollow
    affirmations).
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import List

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


class _StubKBSection:
    """Light-weight stand-in for ``MerchantKnowledgeSection``. The
    KB scanner only reads ``id``, ``kind``, ``body``, ``priority``,
    ``updated_at``; everything else can be omitted."""

    def __init__(
        self, *, id: int, kind: str, body: str, title: str = "",
    ) -> None:
        self.id = id
        self.kind = kind
        self.body = body
        self.title = title
        self.is_active = True
        self.priority = 0
        from datetime import datetime, timezone
        self.updated_at = datetime.now(timezone.utc)


class _Filterable:
    """Mimics the SQLAlchemy chain ``query(...).filter(...).order_by(...).limit(...).all()``
    on a fixed list of rows. Just enough surface area for the
    KB scanner; no actual filtering is performed because the
    test fixtures already supply the rows we want returned."""

    def __init__(self, rows: List[_StubKBSection]) -> None:
        self._rows = rows

    def filter(self, *_a, **_k) -> "_Filterable":
        return self

    def order_by(self, *_a, **_k) -> "_Filterable":
        return self

    def limit(self, *_a, **_k) -> "_Filterable":
        return self

    def all(self) -> List[_StubKBSection]:
        return list(self._rows)


class _StubDB:
    """SA-shaped stub with a ``query`` that returns the same
    ``_Filterable`` wrapper regardless of the model. Tests pass
    ``rows`` to control which sections the KB scanner sees."""

    def __init__(self, rows: List[_StubKBSection] | None = None) -> None:
        self._rows = rows or []

    def query(self, *_a, **_k) -> _Filterable:
        return _Filterable(self._rows)


def _patch_url_lookups(
    monkeypatch: pytest.MonkeyPatch,
    *,
    store_url: str = "",
    maps_url: str = "",
) -> None:
    """Stub the tenant-settings URL resolvers so individual tests
    can assert per-artifact behaviour without spinning up a real
    TenantSettings row."""
    from modules.ai.postprocess import safety_nets as sn

    monkeypatch.setattr(
        sn, "_lookup_tenant_store_url", lambda _db, _tid: store_url,
    )
    monkeypatch.setattr(
        sn, "_lookup_tenant_maps_url",
        lambda _db, _tid: (maps_url, "snapshot" if maps_url else "none"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. Staff phone — KB miss → honest "أحتاج إضافة رقم …" rewrite
# ─────────────────────────────────────────────────────────────────────────────


def test_staff_phone_hollow_reply_with_kb_miss_rewrites_to_honest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact production scenario: customer asks for أمين's
    phone, the LLM says ``"أبشر 🌷"``, the KB has أمين's name
    in a section but no Saudi phone next to it. Guard must
    rewrite the reply to ``"أحتاج إضافة رقم أمين في بيانات
    المتجر حتى أرسله لك مباشرة 🌷"``."""
    from modules.ai.postprocess.safety_nets import apply_outbound_artifact_guard

    db = _StubDB(rows=[
        _StubKBSection(
            id=1, kind="branches",
            body="فرع الطائف. بائع المعرض: أمين.",
        ),
    ])
    _patch_url_lookups(monkeypatch)

    res = apply_outbound_artifact_guard(
        db, tenant_id=33,
        customer_msg="عطني رقم أمين",
        reply_text="أبشر 🌷",
        media_attachments=[],
        call_targets=[],
    )

    assert res.fired is True
    assert res.expected_artifact == "staff_phone"
    assert res.artifact_satisfied is False
    assert res.action == "rewrite_missing_staff_phone"
    assert "أمين" in res.new_reply
    assert "أحتاج إضافة" in res.new_reply
    # Must NOT promise delivery the customer never receives.
    assert "أبشر" not in res.new_reply
    assert "تفضل" not in res.new_reply


# ─────────────────────────────────────────────────────────────────────────────
# 2. Staff phone — KB hit → inject "تفضل رقم أمين: 05…"
# ─────────────────────────────────────────────────────────────────────────────


def test_staff_phone_hollow_reply_with_kb_hit_injects_phone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the merchant typed ``"أمين - 0541690226"`` in a
    branches section, the LLM may still ship a hollow ``"تفضل"``.
    The guard's KB scan finds the phone and the rewrite contains
    the actual digits."""
    from modules.ai.postprocess.safety_nets import apply_outbound_artifact_guard

    db = _StubDB(rows=[
        _StubKBSection(
            id=2, kind="branches",
            body="بائع المعرض: أمين - 0541690226",
        ),
    ])
    _patch_url_lookups(monkeypatch)

    res = apply_outbound_artifact_guard(
        db, tenant_id=33,
        customer_msg="عطني رقم أمين",
        reply_text="تفضل 🌷",
        media_attachments=[],
        call_targets=[],
    )

    assert res.fired is True
    assert res.action == "inject_staff_phone"
    assert "0541690226" in res.new_reply
    assert "أمين" in res.new_reply


# ─────────────────────────────────────────────────────────────────────────────
# 3. Payment barcode — phone in reply but no barcode media → honest rewrite
# ─────────────────────────────────────────────────────────────────────────────


def test_barcode_request_with_phone_only_reply_rewrites_to_honest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The customer typed ``"أبي باركود الراجحي"`` and the LLM
    replied with the transfer number alone. That's NOT a barcode —
    the guard rewrites to inform the customer about what IS
    available (the phone) and what isn't (the barcode image)."""
    from modules.ai.postprocess.safety_nets import apply_outbound_artifact_guard

    db = _StubDB()
    _patch_url_lookups(monkeypatch)

    reply = "تفضل 🌷 الراجحي 0555906901"
    res = apply_outbound_artifact_guard(
        db, tenant_id=33,
        customer_msg="أبي باركود الراجحي",
        reply_text=reply,
        media_attachments=[],
        call_targets=[],
    )

    assert res.expected_artifact == "payment_barcode"
    assert res.fired is True
    assert res.action == "rewrite_missing_barcode"
    assert "المتوفر حاليًا رقم التحويل" in res.new_reply
    assert "الراجحي" in res.new_reply  # bank label preserved
    assert "غير مضافة" in res.new_reply or "لم تتم إضافة" in res.new_reply


def test_barcode_request_with_attached_barcode_media_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the LLM successfully attached a barcode media item
    (link_role='barcode'), the artifact is satisfied and the guard
    must not rewrite."""
    from modules.ai.postprocess.safety_nets import apply_outbound_artifact_guard

    db = _StubDB()
    _patch_url_lookups(monkeypatch)

    class _M:
        def __init__(self, *, key: str = "", title: str = "") -> None:
            self.media_key = key
            self.title = title

    class _L:
        def __init__(self, *, role: str, media: _M) -> None:
            self.link_role = role
            self.media = media

    media_attachments = [
        _L(role="barcode", media=_M(key="rajhi_barcode", title="باركود الراجحي")),
    ]

    res = apply_outbound_artifact_guard(
        db, tenant_id=33,
        customer_msg="أبي باركود الراجحي",
        reply_text="تفضل الباركود",
        media_attachments=media_attachments,
        call_targets=[],
    )

    assert res.expected_artifact == "payment_barcode"
    assert res.artifact_satisfied is True
    assert res.fired is False
    assert res.action == "pass"


# ─────────────────────────────────────────────────────────────────────────────
# 4. Maps link — hollow reply, tenant has maps URL → inject
# ─────────────────────────────────────────────────────────────────────────────


def test_maps_link_hollow_reply_with_url_configured_injects_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Customer asked for the location, LLM replied "تفضل" with
    no URL, but the tenant has a Google Maps URL on file. The
    guard injects via the canonical location-reply builder."""
    from modules.ai.postprocess.safety_nets import apply_outbound_artifact_guard

    db = _StubDB()
    maps_url = "https://maps.app.goo.gl/abc123"
    _patch_url_lookups(monkeypatch, maps_url=maps_url)

    res = apply_outbound_artifact_guard(
        db, tenant_id=33,
        customer_msg="وين موقعكم",
        reply_text="تفضل 🌷",
        media_attachments=[],
        call_targets=[],
    )

    assert res.expected_artifact == "maps_link"
    assert res.fired is True
    assert res.action == "inject_maps_link"
    assert maps_url in res.new_reply


def test_maps_link_hollow_reply_with_no_url_configured_rewrites_honest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same intent + same hollow reply, but the tenant never
    configured a maps URL. The guard must NOT invent one — it
    rewrites to the honest "غير مضاف حاليًا" line."""
    from modules.ai.postprocess.safety_nets import apply_outbound_artifact_guard

    db = _StubDB()
    _patch_url_lookups(monkeypatch, maps_url="")

    res = apply_outbound_artifact_guard(
        db, tenant_id=33,
        customer_msg="ابي رابط الموقع",
        reply_text="أبشر 🌷",
        media_attachments=[],
        call_targets=[],
    )

    assert res.expected_artifact == "maps_link"
    assert res.fired is True
    assert res.action == "rewrite_missing_maps_link"
    assert "غير مضاف" in res.new_reply
    # No URL ever appears when none is configured.
    assert "https://" not in res.new_reply


# ─────────────────────────────────────────────────────────────────────────────
# 5. Store link — hollow reply, tenant has store URL → inject
# ─────────────────────────────────────────────────────────────────────────────


def test_store_link_hollow_reply_with_url_configured_injects_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Customer asked "أبي رابط المتجر", LLM replied "تفضل"
    without a URL, tenant has a store URL on file. Guard
    injects via :func:`_build_store_link_reply`."""
    from modules.ai.postprocess.safety_nets import apply_outbound_artifact_guard

    db = _StubDB()
    store_url = "https://example.salla.sa"
    _patch_url_lookups(monkeypatch, store_url=store_url)

    res = apply_outbound_artifact_guard(
        db, tenant_id=33,
        customer_msg="أبي رابط المتجر",
        reply_text="تفضل 🌷",
        media_attachments=[],
        call_targets=[],
    )

    assert res.expected_artifact == "store_link"
    assert res.fired is True
    assert res.action == "inject_store_link"
    assert store_url in res.new_reply


# ─────────────────────────────────────────────────────────────────────────────
# 6. Pass-through cases — guard must NOT rewrite when reply is good
# ─────────────────────────────────────────────────────────────────────────────


def test_reply_already_carrying_phone_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the LLM already shipped أمين's phone in the reply
    body, the guard's satisfaction probe sees the digits and
    returns ``action='pass'``."""
    from modules.ai.postprocess.safety_nets import apply_outbound_artifact_guard

    db = _StubDB()
    _patch_url_lookups(monkeypatch)

    res = apply_outbound_artifact_guard(
        db, tenant_id=33,
        customer_msg="عطني رقم أمين",
        reply_text="رقم أمين: 0541690226 🌷",
        media_attachments=[],
        call_targets=[],
    )

    assert res.expected_artifact == "staff_phone"
    assert res.artifact_satisfied is True
    assert res.fired is False
    assert res.action == "pass"


def test_reply_already_honest_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reply that already explains the asset isn't on file
    should NOT be replaced with our canned line — that would be
    redundant and overwrite the merchant's coached voice."""
    from modules.ai.postprocess.safety_nets import apply_outbound_artifact_guard

    db = _StubDB()
    _patch_url_lookups(monkeypatch)

    res = apply_outbound_artifact_guard(
        db, tenant_id=33,
        customer_msg="عطني رقم أمين",
        reply_text=(
            "أبشر — رقم أمين غير مضاف حاليًا في النظام، "
            "أحتاج التواصل معه أولاً 🌷"
        ),
        media_attachments=[],
        call_targets=[],
    )

    assert res.fired is False
    assert res.action == "pass"
    assert res.skipped_reason == "reply_already_honest"


def test_long_natural_prose_reply_not_overwritten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A long, substantive reply (even one that doesn't carry the
    requested artifact) should NOT be rewritten — the guard only
    catches HOLLOW affirmations, not natural prose. Otherwise we
    risk replacing a paragraph of useful context with a one-liner.
    """
    from modules.ai.postprocess.safety_nets import apply_outbound_artifact_guard

    db = _StubDB()
    _patch_url_lookups(monkeypatch)

    long_reply = (
        "تواصل مع أمين بائع المعرض مباشرةً عند الفرع، "
        "وهو يستقبلكم من 9 صباحاً حتى 11 مساءً. "
        "للمنتجات التي تحتاج تأكيد توفر، الرجاء طلبها قبل الزيارة "
        "بساعة على الأقل لتجهيزها."
    )
    res = apply_outbound_artifact_guard(
        db, tenant_id=33,
        customer_msg="عطني رقم أمين",
        reply_text=long_reply,
        media_attachments=[],
        call_targets=[],
    )

    assert res.fired is False
    assert res.action == "pass"
    assert res.skipped_reason == "reply_not_hollow"


def test_no_artifact_intent_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A casual greeting carries no artifact intent — the guard
    classifies as ``"none"`` and returns immediately. This is the
    common-case fast path."""
    from modules.ai.postprocess.safety_nets import apply_outbound_artifact_guard

    db = _StubDB()
    _patch_url_lookups(monkeypatch)

    res = apply_outbound_artifact_guard(
        db, tenant_id=33,
        customer_msg="السلام عليكم",
        reply_text="وعليكم السلام، كيف نقدر نخدمك؟ 🌷",
        media_attachments=[],
        call_targets=[],
    )

    assert res.expected_artifact == "none"
    assert res.fired is False
    assert res.skipped_reason == "no_artifact_intent"


# ─────────────────────────────────────────────────────────────────────────────
# 7. Negative control — generic "وش رقم" without role/name does NOT fire
# ─────────────────────────────────────────────────────────────────────────────


def test_generic_phone_request_without_role_or_name_does_not_classify_as_staff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Customer asks "وش رقمكم؟" with no role/name. This is a
    generic store-contact request — the guard must NOT misclassify
    it as a staff-phone ask, otherwise it would rewrite to "أحتاج
    إضافة رقم …" on a perfectly normal "we're at 0XX" reply.
    """
    from modules.ai.postprocess.safety_nets import apply_outbound_artifact_guard

    db = _StubDB()
    _patch_url_lookups(monkeypatch)

    res = apply_outbound_artifact_guard(
        db, tenant_id=33,
        customer_msg="وش رقمكم",
        reply_text="أبشر 🌷",
        media_attachments=[],
        call_targets=[],
    )

    assert res.expected_artifact == "none"
    assert res.fired is False


def test_call_target_with_phone_satisfies_staff_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the upstream staff safety net resolved a CallTarget
    (the LLM emitted a [CALL:…] marker that the marker extractor
    turned into a CallTarget), the artifact is satisfied — the
    contact card will go out as a separate WhatsApp ``contacts``
    message even though the reply text doesn't carry the digits.
    """
    from modules.ai.postprocess.safety_nets import apply_outbound_artifact_guard
    from services.call_resolver import CallTarget

    db = _StubDB()
    _patch_url_lookups(monkeypatch)

    target = CallTarget(
        name="أمين",
        wa_id="966541690226",
        phone_display="+966 54 169 0226",
        raw_phone="0541690226",
    )

    res = apply_outbound_artifact_guard(
        db, tenant_id=33,
        customer_msg="عطني رقم أمين",
        reply_text="تواصل مع أمين 🌷",
        media_attachments=[],
        call_targets=[target],
    )

    assert res.expected_artifact == "staff_phone"
    assert res.artifact_satisfied is True
    assert res.fired is False
    assert res.action == "pass"


# ─────────────────────────────────────────────────────────────────────────────
# 8. Telemetry — to_log_dict carries every field a triage view needs
# ─────────────────────────────────────────────────────────────────────────────


def test_to_log_dict_carries_full_diagnostic_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production triage greps the structured logs by
    ``[OUTBOUND_ARTIFACT_GUARD]``. The result's ``to_log_dict``
    must include every field the operator dashboard charts."""
    from modules.ai.postprocess.safety_nets import apply_outbound_artifact_guard

    db = _StubDB()
    _patch_url_lookups(monkeypatch, store_url="https://x.salla.sa")

    res = apply_outbound_artifact_guard(
        db, tenant_id=33,
        customer_msg="أبي رابط المتجر",
        reply_text="تفضل 🌷",
    )
    payload = res.to_log_dict()

    for required in (
        "kind",
        "fired",
        "expected_artifact",
        "artifact_satisfied",
        "rewrote_reply",
        "action",
        "skipped_reason",
    ):
        assert required in payload, (
            f"to_log_dict missing required field {required!r}: {payload!r}"
        )
    assert payload["kind"] == "outbound_artifact_guard"
    assert payload["fired"] is True
    assert payload["action"] == "inject_store_link"
