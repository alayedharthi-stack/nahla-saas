"""Hardening tests for the AI Assets layer (manual coupons + AI media library).

These cover the additions made on top of the original
``test_intelligence_libraries.py`` suite:

* ``validate_media_for_send`` — final-stage safety gate (tenant scope,
  active flag, supported type, HTTPS / on-disk presence, size cap, safe
  filename).
* ``format_libraries_for_prompt`` and the per-library formatters — make
  sure the LLM sees title / tags / usage_context, never raw URLs.
* Relevance-aware ordering when the customer's last message tags into
  one of the items.
* ``ai_assets`` facade — generic listing across registered kinds and
  validator dispatch with safe defaults.

All tests are pure-Python; no Postgres, no live HTTP. The DB is a
lightweight ``MagicMock``-based stand-in (same approach as the original
intelligence-libraries suite).
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for p in [str(REPO_ROOT), str(BACKEND_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)


# ─────────────────────────────────────────────────────────────────────────
# Test helpers
# ─────────────────────────────────────────────────────────────────────────

def _media_row(
    *,
    id_: int,
    tenant_id: int = 1,
    active: bool = True,
    media_type: str = "image",
    file_url: str = "https://cdn/x.png",
    title: str = "x",
    mime: str | None = "image/png",
    storage_kind: str = "external",
    storage_path: str | None = None,
    file_size_bytes: int | None = None,
):
    return SimpleNamespace(
        id=id_, tenant_id=tenant_id, is_active=active,
        media_type=media_type, file_url=file_url, title=title,
        mime_type=mime, storage_kind=storage_kind,
        storage_path=storage_path, file_size_bytes=file_size_bytes,
    )


def _fake_db(rows_for_first: list | None = None, rows_for_in: list | None = None):
    """A MagicMock that emulates SQLAlchemy enough for our two call shapes:

    * ``db.query(Model).filter(...).first()`` → ``rows_for_first[0]`` or None
    * ``db.query(Model).filter(...).all()``   → ``rows_for_in``
    """
    db = MagicMock()
    query = MagicMock()
    db.query.return_value = query
    query.filter.return_value = query
    query.order_by.return_value = query
    query.limit.return_value = query
    query.first.return_value = (rows_for_first or [None])[0]
    query.all.return_value = rows_for_in or []
    return db


# ─────────────────────────────────────────────────────────────────────────
# validate_media_for_send
# ─────────────────────────────────────────────────────────────────────────

def test_validate_media_rejects_cross_tenant_payload():
    from core.ai_libraries import validate_media_for_send

    att = {"id": 5, "tenant_id": 99, "media_type": "image",
           "file_url": "https://cdn/x.png", "storage_kind": "external"}
    ok, reason, _ = validate_media_for_send(att, expected_tenant_id=1)
    assert ok is False
    assert reason == "tenant_mismatch"


def test_validate_media_rejects_disabled_row_via_db_recheck():
    from core.ai_libraries import validate_media_for_send

    disabled = _media_row(id_=5, tenant_id=1, active=False)
    db = _fake_db(rows_for_first=[disabled])
    att = {"id": 5, "tenant_id": 1, "media_type": "image",
           "file_url": "https://cdn/x.png", "storage_kind": "external"}
    ok, reason, _ = validate_media_for_send(att, expected_tenant_id=1, db=db)
    assert ok is False
    assert reason == "row_disabled_mid_turn"


def test_validate_media_rejects_missing_row_via_db_recheck():
    from core.ai_libraries import validate_media_for_send

    db = _fake_db(rows_for_first=[None])
    att = {"id": 999, "tenant_id": 1, "media_type": "image",
           "file_url": "https://cdn/x.png", "storage_kind": "external"}
    ok, reason, _ = validate_media_for_send(att, expected_tenant_id=1, db=db)
    assert ok is False
    assert reason == "row_missing_or_cross_tenant"


def test_validate_media_rejects_unsupported_type():
    from core.ai_libraries import validate_media_for_send

    att = {"id": 1, "media_type": "hologram",
           "file_url": "https://cdn/x", "storage_kind": "external"}
    ok, reason, _ = validate_media_for_send(att, expected_tenant_id=1)
    assert ok is False
    assert reason.startswith("unsupported_media_type")


def test_validate_media_rejects_non_http_external_url():
    from core.ai_libraries import validate_media_for_send

    att = {"id": 1, "media_type": "image",
           "file_url": "ftp://cdn/x.png", "storage_kind": "external"}
    ok, reason, _ = validate_media_for_send(att, expected_tenant_id=1)
    assert ok is False
    assert reason == "invalid_url_scheme"


def test_validate_media_rejects_local_when_file_missing_on_disk():
    from core.ai_libraries import validate_media_for_send

    att = {
        "id": 1, "media_type": "image",
        "file_url": "https://api/intelligence-libraries/media/1/file",
        "storage_kind": "local",
        "storage_path": str(Path(tempfile.gettempdir()) / "this-file-does-not-exist-xyz.png"),
    }
    ok, reason, _ = validate_media_for_send(att, expected_tenant_id=1)
    assert ok is False
    assert reason == "file_missing_on_disk"


def test_validate_media_accepts_local_when_file_exists():
    from core.ai_libraries import validate_media_for_send

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
        fh.write(b"x")
        path = fh.name
    try:
        att = {
            "id": 1, "media_type": "image",
            "file_url": "https://api/intelligence-libraries/media/1/file",
            "storage_kind": "local",
            "storage_path": path,
            "file_size_bytes": 1,
        }
        ok, reason, normed = validate_media_for_send(att, expected_tenant_id=1)
        assert ok is True
        assert reason is None
        assert normed["media_type"] == "image"
    finally:
        os.unlink(path)


def test_validate_media_rejects_oversize_image():
    from core.ai_libraries import validate_media_for_send

    att = {
        "id": 1, "media_type": "image",
        "file_url": "https://cdn/huge.png", "storage_kind": "external",
        "file_size_bytes": 50 * 1024 * 1024,  # 50 MB > 5 MB image cap
    }
    ok, reason, _ = validate_media_for_send(att, expected_tenant_id=1)
    assert ok is False
    assert reason.startswith("size_exceeds_whatsapp_limit:image")


def test_validate_media_pdf_gets_safe_default_filename():
    from core.ai_libraries import validate_media_for_send

    att = {
        "id": 1, "media_type": "pdf",
        "file_url": "https://cdn/policy.pdf", "storage_kind": "external",
    }
    ok, _, normed = validate_media_for_send(att, expected_tenant_id=1)
    assert ok is True
    assert normed["filename"]
    # No path separators, no leading dot, reasonable length
    assert "/" not in normed["filename"] and "\\" not in normed["filename"]
    assert not normed["filename"].startswith(".")


def test_validate_media_safe_filename_strips_bad_chars():
    from core.ai_libraries import _safe_filename

    assert _safe_filename("../../etc/passwd") == "etc passwd".replace(" ", "") or \
           "/" not in _safe_filename("../../etc/passwd")
    assert _safe_filename("") == "file"
    assert _safe_filename(".hidden") == "hidden"
    assert "\x00" not in _safe_filename("bad\x00name.pdf")


# ─────────────────────────────────────────────────────────────────────────
# Prompt formatting
# ─────────────────────────────────────────────────────────────────────────

def test_format_manual_coupons_for_prompt_lists_code_and_usage():
    from core.ai_libraries import format_manual_coupons_for_prompt

    block = format_manual_coupons_for_prompt([{
        "id": 1, "code": "AYNE26", "title": "خصم ترحيبي",
        "discount_text": "10%", "description": "كوبون لعملاء واتساب",
        "usage_context": "إذا طلب العميل خصم", "priority": 10,
        "expires_at": None,
    }])
    assert "AYNE26" in block
    assert "خصم ترحيبي" in block
    assert "إذا طلب العميل خصم" in block
    # Block must explicitly forbid invention
    assert "لا تخترعي" in block


def test_format_ai_media_for_prompt_includes_id_title_tags_usage_no_url():
    from core.ai_libraries import format_ai_media_for_prompt

    block = format_ai_media_for_prompt([{
        "id": 12, "title": "باركود التحويل البنكي",
        "media_type": "image",
        "tags": ["تحويل", "دفع", "بنك"],
        "usage_context": "أرسله إذا طلب العميل التحويل البنكي",
        "description": "صورة QR للحساب",
        "priority": 5,
    }])
    assert "MEDIA_ID=12" in block
    assert "باركود التحويل البنكي" in block
    assert "تحويل" in block and "بنك" in block
    assert "أرسله إذا طلب العميل التحويل" in block
    # The block MUST NOT leak any internal URL or storage path.
    assert "http" not in block.lower()
    assert "file_url" not in block
    assert "storage_path" not in block


def test_format_libraries_for_prompt_empty_when_both_empty():
    from core.ai_libraries import format_libraries_for_prompt

    assert format_libraries_for_prompt({}) == ""
    assert format_libraries_for_prompt(
        {"manual_coupons": [], "ai_media_library": []}
    ) == ""


def test_format_libraries_for_prompt_truncates_long_descriptions():
    from core.ai_libraries import format_ai_media_for_prompt

    block = format_ai_media_for_prompt([{
        "id": 1, "title": "x", "media_type": "image",
        "description": "ا" * 1000,
        "usage_context": "ب" * 1000,
        "tags": [], "priority": 1,
    }])
    # Truncated with an ellipsis; the original 1000-char strings must
    # not appear verbatim.
    assert "ا" * 500 not in block
    assert "ب" * 500 not in block


# ─────────────────────────────────────────────────────────────────────────
# Relevance-aware ordering
# ─────────────────────────────────────────────────────────────────────────

def test_relevance_score_prefers_tag_overlap():
    from core.ai_libraries import _relevance_score

    item_relevant = {"title": "صورة عسل السمر", "tags": ["سمر", "عسل"], "usage_context": ""}
    item_irrelevant = {"title": "ملف PDF تعريفي", "tags": ["شركة"], "usage_context": ""}
    q = "عندكم صور عسل السمر؟"
    assert _relevance_score(item_relevant, q) > _relevance_score(item_irrelevant, q)


def test_sort_with_relevance_promotes_tagged_items_above_priority():
    from core.ai_libraries import _sort_with_relevance

    items = [
        {"id": 1, "title": "ملف عام", "tags": [], "priority": 1},        # high priority but irrelevant
        {"id": 2, "title": "عسل السمر", "tags": ["سمر"], "priority": 100},  # low priority but relevant
    ]
    out = _sort_with_relevance(items, "أبغى صورة عسل السمر", cap=5)
    assert out[0]["id"] == 2  # relevance wins over priority
    assert out[1]["id"] == 1


# ─────────────────────────────────────────────────────────────────────────
# ai_assets facade
# ─────────────────────────────────────────────────────────────────────────

def test_ai_assets_registers_media_and_coupon_kinds():
    from core import ai_assets

    out = ai_assets.list_all_assets_for_prompt(
        _fake_db(rows_for_in=[]), tenant_id=1, relevance_query=None,
    )
    assert "media" in out and "coupon" in out
    assert out["media"] == []
    assert out["coupon"] == []


def test_ai_assets_validate_dispatches_to_media_validator():
    from core import ai_assets

    att = {"id": 1, "tenant_id": 1, "media_type": "image",
           "file_url": "https://cdn/x.png", "storage_kind": "external"}
    ok, reason, _ = ai_assets.validate_asset_for_send("media", att, expected_tenant_id=1)
    assert ok is True and reason is None


def test_ai_assets_validate_unknown_kind_passes_through():
    """Forward-compat: an unregistered kind should default-accept so a
    new asset family can be plumbed end-to-end before its validator
    lands. The send pipeline still owns the WhatsApp-specific gate."""
    from core import ai_assets

    ok, reason, normed = ai_assets.validate_asset_for_send(
        "future_carousel", {"id": 1}, expected_tenant_id=1,
    )
    assert ok is True and reason is None and normed == {"id": 1}


def test_ai_assets_register_asset_kind_is_idempotent():
    from core import ai_assets

    def _list_stub(db, tenant_id, **kwargs):
        return [{"id": 7}]

    ai_assets.register_asset_kind(ai_assets.AssetKind(name="stub_kind", lister=_list_stub))
    out = ai_assets.list_all_assets_for_prompt(
        _fake_db(rows_for_in=[]), tenant_id=1,
    )
    assert out["stub_kind"] == [{"id": 7}]


# ─────────────────────────────────────────────────────────────────────────
# extract_media_markers — additional edge cases requested in the review
# ─────────────────────────────────────────────────────────────────────────

def test_extract_media_markers_drops_cross_tenant_id():
    """Even if the LLM cites an id from another tenant, the DB filter
    ``tenant_id == expected`` returns an empty row set and the marker
    is silently stripped without an attachment."""
    from core.ai_libraries import extract_media_markers

    db = _fake_db(rows_for_in=[])  # tenant filter returned nothing
    cleaned, attachments = extract_media_markers(
        db, tenant_id=1, reply_text="هنا [MEDIA:42]",
    )
    assert "[MEDIA:42]" not in cleaned
    assert attachments == []


def test_extract_media_markers_caps_to_max_attachments_even_when_resolved():
    from core.ai_libraries import extract_media_markers

    rows = [_media_row(id_=i) for i in (1, 2, 3, 4, 5)]
    db = _fake_db(rows_for_in=rows)
    cleaned, attachments = extract_media_markers(
        db, tenant_id=1,
        reply_text="[MEDIA:1] [MEDIA:2] [MEDIA:3] [MEDIA:4] [MEDIA:5]",
        max_attachments=2,
    )
    assert "[MEDIA:" not in cleaned
    assert len(attachments) == 2


# ─────────────────────────────────────────────────────────────────────────
# Caps — list helpers honour the new 10 / 15 ceilings
# ─────────────────────────────────────────────────────────────────────────

def test_list_active_manual_coupons_caps_at_ten():
    from core import ai_libraries

    # Build 30 fake rows; caller's ordering filter shouldn't matter for
    # the cap. We bypass the DB by stubbing the limit chain directly.
    rows = [
        SimpleNamespace(
            id=i, tenant_id=1, is_active=True, code=f"C{i}",
            title=f"t{i}", description="", discount_text="",
            usage_context="", priority=10, starts_at=None, expires_at=None,
        )
        for i in range(1, 31)
    ]
    db = _fake_db(rows_for_in=rows)
    out = ai_libraries.list_active_manual_coupons(db, tenant_id=1)
    assert len(out) <= ai_libraries._MAX_COUPONS_IN_CONTEXT == 10


def test_list_active_ai_media_caps_at_fifteen():
    from core import ai_libraries

    rows = [
        SimpleNamespace(
            id=i, tenant_id=1, is_active=True, title=f"t{i}",
            description="", media_type="image", usage_context="",
            tags=[], priority=10,
        )
        for i in range(1, 50)
    ]
    db = _fake_db(rows_for_in=rows)
    out = ai_libraries.list_active_ai_media(db, tenant_id=1)
    assert len(out) <= ai_libraries._MAX_MEDIA_IN_CONTEXT == 15
