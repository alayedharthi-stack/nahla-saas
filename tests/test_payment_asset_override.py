"""Regression tests for the bank-transfer asset HARD OVERRIDE path.

These cover the layers added on top of the intent/relevance/prompt
changes from ``test_payment_info_media_attach.py``:

  1. ``scrub_internal_markers`` — strips ANY ``[FOO]`` / ``[FOO:bar]``
     leak (``[TRANSFER]``, ``[TEMPLATE:...]``, ``[CTA_URL:...]``, …).
     Customers were literally receiving ``[TRANSFER]`` in WhatsApp
     because GPT hallucinates marker-shaped placeholders it sees in the
     prompt; this scrubber is the last-line guarantee that none reach
     the customer.

  2. HTTPS auto-upgrade — Railway / Heroku / Vercel / Fly / Render
     URLs that come in as ``http://`` get rewritten to ``https://``
     because Meta's WhatsApp Cloud API silently rejects non-HTTPS media.

  3. ``find_best_payment_asset`` — given an inbound that looks like a
     bank/IBAN/QR/transfer request, returns the highest-relevance
     active media item with a payment-word boost. Used by the webhook
     as a hard override when GPT misses the asset.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for p in [str(REPO_ROOT), str(BACKEND_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)


# ─────────────────────────────────────────────────────────────────────────
# 1. scrub_internal_markers
# ─────────────────────────────────────────────────────────────────────────

def test_scrub_strips_bare_transfer_marker():
    from core.ai_libraries import scrub_internal_markers

    # The exact leak the merchant reported.
    out = scrub_internal_markers("أهلاً [TRANSFER] كيف أساعدك؟")
    assert "[TRANSFER]" not in out
    assert "أهلاً" in out and "كيف أساعدك" in out


def test_scrub_strips_template_marker_with_payload():
    from core.ai_libraries import scrub_internal_markers

    out = scrub_internal_markers("شكراً [TEMPLATE:contact_owner] بالخدمة")
    assert "[TEMPLATE" not in out
    assert "contact_owner" not in out
    assert "شكراً" in out and "بالخدمة" in out


def test_scrub_strips_cta_url_marker():
    from core.ai_libraries import scrub_internal_markers

    out = scrub_internal_markers(
        "تفضل [CTA_URL:title=الدفع الآن|url=https://example.com/pay]"
    )
    assert "[CTA_URL" not in out
    assert "https://example.com/pay" not in out
    assert "تفضل" in out


def test_scrub_strips_multiple_markers_in_one_message():
    from core.ai_libraries import scrub_internal_markers

    out = scrub_internal_markers(
        "ابدأ [GREETING] ثم [TRANSFER] وأخيراً [HANDOFF:human]"
    )
    for tok in ("[GREETING]", "[TRANSFER]", "[HANDOFF", "human]"):
        assert tok not in out


def test_scrub_does_not_touch_arabic_brackets():
    """Merchants sometimes wrap real content in brackets like "[ملاحظة]".
    The scrubber must be ALL-CAPS-only so it only catches internal
    markers and never customer-visible Arabic text."""
    from core.ai_libraries import scrub_internal_markers

    out = scrub_internal_markers("هذه رسالة [ملاحظة] من المتجر")
    assert "[ملاحظة]" in out


def test_scrub_does_not_touch_lowercase_brackets():
    from core.ai_libraries import scrub_internal_markers

    out = scrub_internal_markers("see [docs] for details")
    assert "[docs]" in out


def test_scrub_handles_empty_input():
    from core.ai_libraries import scrub_internal_markers

    assert scrub_internal_markers("") == ""
    assert scrub_internal_markers(None) == ""  # type: ignore[arg-type]


def test_scrub_collapses_stranded_whitespace():
    from core.ai_libraries import scrub_internal_markers

    out = scrub_internal_markers("نص   [TRANSFER]   آخر")
    # No double-spaces left where the marker used to be.
    assert "  " not in out
    assert "نص آخر" in out


def test_scrub_runs_after_media_extractor_so_real_media_markers_already_gone():
    """Webhook contract: ``extract_media_markers`` runs first and
    consumes legitimate ``[MEDIA:N]`` tokens, then scrubber runs. So
    a stray ``[MEDIA:...]`` reaching the scrubber means the extractor
    couldn't resolve the id — and stripping it is correct (otherwise
    the customer sees the literal token)."""
    from core.ai_libraries import scrub_internal_markers

    out = scrub_internal_markers("هنا [MEDIA:99] شيء")
    assert "[MEDIA:99]" not in out


# ─────────────────────────────────────────────────────────────────────────
# 2. HTTPS auto-upgrade
# ─────────────────────────────────────────────────────────────────────────

def test_force_https_upgrades_railway_url():
    from routers.intelligence_libraries import _force_https_for_production

    upgraded = _force_https_for_production(
        "http://nahla-saas-production.up.railway.app/intelligence/ai-media/file/12"
    )
    assert upgraded.startswith("https://")
    assert "railway.app" in upgraded


def test_force_https_leaves_localhost_alone():
    from routers.intelligence_libraries import _force_https_for_production

    assert _force_https_for_production("http://localhost:8000/foo").startswith("http://")
    assert _force_https_for_production("http://127.0.0.1:8000/foo").startswith("http://")


def test_force_https_idempotent_on_https_url():
    from routers.intelligence_libraries import _force_https_for_production

    url = "https://nahla-saas-production.up.railway.app/foo"
    assert _force_https_for_production(url) == url


def test_validate_media_auto_upgrades_http_to_https_on_railway(monkeypatch):
    """When validate_media_for_send sees an http:// Railway URL, it
    must rewrite it to https:// in-place so the WhatsApp dispatch uses
    the secure URL — without us having to rewrite every existing DB row.
    """
    from core import ai_libraries

    attachment = {
        "id": 5,
        "tenant_id": 33,
        "media_type": "image",
        "file_url": "http://nahla-saas-production.up.railway.app/intelligence/ai-media/file/5",
        "storage_kind": "external",
        "mime_type": "image/png",
        "file_size_bytes": 1024,
    }
    ok, err, item = ai_libraries.validate_media_for_send(
        attachment, expected_tenant_id=33, db=None,
    )
    assert ok is True, f"unexpected validation error: {err}"
    assert item is not None
    assert item["file_url"].startswith("https://")


def test_validate_media_rejects_invalid_scheme():
    """Non-http(s) schemes (file://, ftp://, gibberish) must still fail."""
    from core import ai_libraries

    attachment = {
        "id": 5,
        "tenant_id": 33,
        "media_type": "image",
        "file_url": "file:///etc/passwd",
        "storage_kind": "external",
        "mime_type": "image/png",
    }
    ok, err, _ = ai_libraries.validate_media_for_send(
        attachment, expected_tenant_id=33, db=None,
    )
    assert ok is False
    assert err == "invalid_url_scheme"


# ─────────────────────────────────────────────────────────────────────────
# 3. find_best_payment_asset HARD OVERRIDE
# ─────────────────────────────────────────────────────────────────────────

class _StubMediaRow:
    def __init__(self, **kw):
        self.id = kw["id"]
        self.tenant_id = kw["tenant_id"]
        self.title = kw.get("title", "")
        self.tags = kw.get("tags", [])
        self.usage_context = kw.get("usage_context", "")
        self.media_type = kw.get("media_type", "image")
        self.file_url = kw.get("file_url", "https://example.com/asset.png")
        self.mime_type = kw.get("mime_type", "image/png")
        self.storage_kind = kw.get("storage_kind", "external")
        self.storage_path = kw.get("storage_path", "")
        self.file_size_bytes = kw.get("file_size_bytes", 1024)
        self.is_active = kw.get("is_active", True)
        self.priority = kw.get("priority", 0)


def _stub_db_with_rows(rows):
    db = MagicMock()
    chain = MagicMock()
    chain.filter.return_value = chain
    chain.all.return_value = rows
    db.query.return_value = chain
    return db


def test_is_payment_query_true_for_rajhi_request():
    from core.ai_libraries import is_payment_query

    assert is_payment_query("ارسل لي حساب الراجحي") is True
    assert is_payment_query("ابغى الآيبان") is True
    assert is_payment_query("بيانات التحويل البنكي") is True
    assert is_payment_query("send me your bank account") is True
    assert is_payment_query("qr code للدفع") is True


def test_is_payment_query_false_for_unrelated():
    from core.ai_libraries import is_payment_query

    assert is_payment_query("كم سعر العسل") is False
    assert is_payment_query("متى يفتح المتجر") is False
    assert is_payment_query("") is False


def test_find_best_payment_asset_returns_bank_barcode_for_rajhi_query():
    from core.ai_libraries import find_best_payment_asset

    rows = [
        _StubMediaRow(
            id=10, tenant_id=33,
            title="باركود التحويل البنكي الراجحي",
            tags=["تحويل", "بنك", "راجحي"],
            usage_context="أرسله إذا طلب العميل التحويل البنكي",
        ),
        _StubMediaRow(
            id=20, tenant_id=33,
            title="صورة عسل السمر",
            tags=["عسل", "سمر"],
            usage_context="أرسلها إذا سأل عن عسل السمر",
        ),
    ]
    db = _stub_db_with_rows(rows)

    asset = find_best_payment_asset(db, tenant_id=33, customer_message="ارسل حساب الراجحي")
    assert asset is not None, "payment asset must be picked for rajhi query"
    assert asset["id"] == 10
    assert asset["title"] == "باركود التحويل البنكي الراجحي"
    assert asset["_relevance_score"] >= 1.5


def test_find_best_payment_asset_returns_none_for_unrelated_query():
    from core.ai_libraries import find_best_payment_asset

    rows = [_StubMediaRow(
        id=10, tenant_id=33,
        title="باركود التحويل البنكي",
        tags=["تحويل", "بنك"],
        usage_context="",
    )]
    db = _stub_db_with_rows(rows)

    asset = find_best_payment_asset(
        db, tenant_id=33, customer_message="كم سعر العسل البلدي؟",
    )
    assert asset is None


def test_find_best_payment_asset_returns_none_when_no_active_rows():
    from core.ai_libraries import find_best_payment_asset

    db = _stub_db_with_rows([])
    asset = find_best_payment_asset(
        db, tenant_id=33, customer_message="ارسل حساب الراجحي",
    )
    assert asset is None


def test_find_best_payment_asset_picks_highest_score_when_multiple_match():
    """Two payment-related items: one has rich tags + payment word in
    title (should win), one has a thin tag only."""
    from core.ai_libraries import find_best_payment_asset

    rows = [
        _StubMediaRow(
            id=1, tenant_id=33,
            title="معلومة عامة",
            tags=["دفع"],
            priority=100,
        ),
        _StubMediaRow(
            id=2, tenant_id=33,
            title="باركود التحويل البنكي الراجحي",
            tags=["تحويل", "بنك", "راجحي", "آيبان"],
            usage_context="أرسله إذا طلب العميل التحويل البنكي",
            priority=1,
        ),
    ]
    db = _stub_db_with_rows(rows)

    asset = find_best_payment_asset(
        db, tenant_id=33, customer_message="ارسل لي حساب الراجحي",
    )
    assert asset is not None
    assert asset["id"] == 2, (
        f"payment-rich asset (id=2) must beat thin one (id=1), got {asset['id']}"
    )


def test_find_best_payment_asset_returns_attachment_compatible_shape():
    """The returned dict must be drop-in compatible with the attachment
    list produced by ``extract_media_markers`` so the webhook can mix
    them together for ``validate_media_for_send`` + dispatch."""
    from core.ai_libraries import find_best_payment_asset

    rows = [_StubMediaRow(
        id=7, tenant_id=33,
        title="باركود التحويل",
        tags=["تحويل", "بنك"],
        media_type="image",
        file_url="https://example.com/barcode.png",
        mime_type="image/png",
        storage_kind="external",
        file_size_bytes=2048,
    )]
    db = _stub_db_with_rows(rows)

    asset = find_best_payment_asset(
        db, tenant_id=33, customer_message="ارسل بيانات التحويل",
    )
    assert asset is not None
    for required_field in (
        "id", "tenant_id", "title", "media_type", "file_url",
        "mime_type", "storage_kind", "storage_path", "file_size_bytes",
    ):
        assert required_field in asset, f"missing required field: {required_field}"
    assert asset["tenant_id"] == 33


# ─────────────────────────────────────────────────────────────────────────
# 4. End-to-end scenario lock
# ─────────────────────────────────────────────────────────────────────────

def test_full_recovery_when_gpt_misses_payment_asset():
    """Full scenario: customer asks for "ارسل حساب الراجحي", GPT
    erroneously generates an owner-contact reply with a hallucinated
    ``[TRANSFER]`` marker. The webhook should:
        1. detect the payment intent
        2. find the bank-barcode asset
        3. attach it as an override
        4. scrub the [TRANSFER] marker from the text
    Validated at the unit level (no live webhook here) — each step
    individually."""
    from core.ai_libraries import (
        find_best_payment_asset,
        is_payment_query,
        scrub_internal_markers,
    )

    customer_msg = "ارسل لي حساب الراجحي"
    gpt_reply = "ما عندي بيانات الحساب البنكي [TRANSFER]"

    # Step 1: intent detected
    assert is_payment_query(customer_msg) is True

    # Step 2-3: asset found
    rows = [_StubMediaRow(
        id=99, tenant_id=33,
        title="باركود التحويل البنكي الراجحي",
        tags=["تحويل", "بنك", "راجحي"],
        usage_context="أرسله للعميل عند طلب التحويل",
    )]
    db = _stub_db_with_rows(rows)
    asset = find_best_payment_asset(db, tenant_id=33, customer_message=customer_msg)
    assert asset is not None and asset["id"] == 99

    # Step 4: scrubber removes the leaked marker
    cleaned = scrub_internal_markers(gpt_reply)
    assert "[TRANSFER]" not in cleaned
    assert "ما عندي بيانات الحساب البنكي" in cleaned
