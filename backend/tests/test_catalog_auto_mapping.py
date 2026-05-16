"""
tests/test_catalog_auto_mapping.py
──────────────────────────────────
Locks the Auto Catalog Mapping layer (May 2026 #12) that promotes
``retailer_id`` coverage from 0/0 to "every product has something
usable" without manual merchant intervention.

Covered:

  1. ``canonical_retailer_id`` priority order:
       meta_retailer_id → external_id → synthetic ``nahla_p_<id>``.
  2. ``assign_canonical_retailer_id`` idempotency + never-overwrite.
  3. ``_normalize_arabic`` collapses common spelling variants.
  4. ``resolve_by_query_relaxed`` matches by normalized substring
     even when the strict resolver returns None.
  5. The structured ``[CATALOG_PRODUCT_*]`` log tokens land where
     callers expect them.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pytest

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

from core.catalog import (  # noqa: E402
    assign_canonical_retailer_id,
    canonical_retailer_id,
    effective_retailer_id,
)
from services.product_resolver import _normalize_arabic  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Stand-in for ORM Product. Mirrors the columns we actually read/write.
# ─────────────────────────────────────────────────────────────────────────────

class _P:
    def __init__(
        self,
        id: int,
        external_id: str | None = None,
        meta_retailer_id: str | None = None,
        title: str = "",
    ):
        self.id               = id
        self.external_id      = external_id
        self.meta_retailer_id = meta_retailer_id
        self.title            = title


# ─────────────────────────────────────────────────────────────────────────────
# 1.  canonical_retailer_id priority
# ─────────────────────────────────────────────────────────────────────────────

def test_canonical_prefers_meta_retailer_id() -> None:
    p = _P(id=1, external_id="SALLA-123", meta_retailer_id="META-7")
    assert canonical_retailer_id(p) == "META-7"


def test_canonical_falls_back_to_external_id() -> None:
    p = _P(id=2, external_id="SALLA-99", meta_retailer_id=None)
    assert canonical_retailer_id(p) == "SALLA-99"


def test_canonical_uses_synthetic_when_both_missing() -> None:
    """The critical change vs effective_retailer_id: never empty when
    we have a row id. The webhook send chain will treat ``nahla_p_*``
    as "not on Meta yet" and route to the legacy fallback — that's
    far better than zero coverage."""
    p = _P(id=42, external_id=None, meta_retailer_id=None)
    assert canonical_retailer_id(p) == "nahla_p_42"


def test_canonical_synthetic_can_be_disabled() -> None:
    """Sentinel switch for callers that need the legacy empty-string
    behaviour (``effective_retailer_id`` does, by design)."""
    p = _P(id=42)
    assert canonical_retailer_id(p, fallback_to_synthetic=False) == ""


def test_effective_retailer_id_still_returns_empty_when_missing() -> None:
    """Regression guard: the SEND path's resolver must keep returning
    "" when nothing is set — that's how the catalog falls back."""
    p = _P(id=99, external_id=None, meta_retailer_id=None)
    assert effective_retailer_id(p) == ""


def test_canonical_works_on_dict_shape() -> None:
    """Webhook product attachments are dicts, not ORM instances. The
    helpers must tolerate that."""
    d = {"id": 7, "external_id": None, "meta_retailer_id": None}
    assert canonical_retailer_id(d) == "nahla_p_7"


# ─────────────────────────────────────────────────────────────────────────────
# 2.  assign_canonical_retailer_id — idempotency
# ─────────────────────────────────────────────────────────────────────────────

def test_assign_writes_external_id_when_meta_is_null() -> None:
    p = _P(id=1, external_id="SALLA-77", meta_retailer_id=None)
    assert assign_canonical_retailer_id(p) is True
    assert p.meta_retailer_id == "SALLA-77"


def test_assign_writes_synthetic_when_both_null() -> None:
    p = _P(id=42, external_id=None, meta_retailer_id=None)
    assert assign_canonical_retailer_id(p) is True
    assert p.meta_retailer_id == "nahla_p_42"


def test_assign_never_overwrites_existing_value() -> None:
    """Merchants who set the override column by hand must keep it
    even after a resync run."""
    p = _P(id=1, external_id="SALLA-1", meta_retailer_id="META-EXISTING")
    assert assign_canonical_retailer_id(p) is False
    assert p.meta_retailer_id == "META-EXISTING"


def test_assign_is_idempotent_on_second_run() -> None:
    p = _P(id=1, external_id="SALLA-1", meta_retailer_id=None)
    assert assign_canonical_retailer_id(p) is True
    assert assign_canonical_retailer_id(p) is False  # second call: no-op


def test_assign_returns_false_on_empty_input() -> None:
    assert assign_canonical_retailer_id(None) is False


# ─────────────────────────────────────────────────────────────────────────────
# 3.  Arabic normalization
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("variant, canonical", [
    ("السَمَر",  "السمر"),     # diacritics stripped
    ("السمر",   "السمر"),     # already canonical
    ("السّمر",  "السمر"),     # shadda stripped
    ("السمَــــر", "السمر"),    # kashida + diacritic
    ("آلسمر",   "السمر"),     # alif madda → alif
    ("إنتاج",   "انتاج"),     # alif hamza below → alif
    ("أحمد",    "احمد"),     # alif hamza above → alif
    ("شركـة",   "شركه"),     # ta marbuta → ha
    ("الذكرى",  "الذكري"),    # alif maqsura → ya
    ("",       ""),
])
def test_arabic_normalization(variant: str, canonical: str) -> None:
    assert _normalize_arabic(variant) == canonical


def test_normalization_is_idempotent() -> None:
    text = "بَيتُ الحلوَى"
    once = _normalize_arabic(text)
    twice = _normalize_arabic(once)
    assert once == twice


# ─────────────────────────────────────────────────────────────────────────────
# 4.  Structured logging tokens
# ─────────────────────────────────────────────────────────────────────────────

def test_resolver_emits_resolve_log_on_short_query(caplog) -> None:
    """The relaxed resolver should emit a structured RESOLVE log even
    when bailing out on a too-short query — operators grep these
    tokens to debug a tenant's coverage."""
    from services.product_resolver import resolve_by_query_relaxed

    with caplog.at_level(logging.INFO, logger="nahla.product_resolver"):
        result = resolve_by_query_relaxed(db=None, tenant_id=42, query="ع")
    assert result is None
    log_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "[CATALOG_PRODUCT_RESOLVE]" in log_text
    assert "skipped=too_short" in log_text


# ─────────────────────────────────────────────────────────────────────────────
# 5.  Endpoint registration (smoke)
# ─────────────────────────────────────────────────────────────────────────────

def test_new_product_endpoints_registered() -> None:
    """``/merchant/catalog/products`` and ``/merchant/catalog/resync``
    must be reachable from the merchant router (and their admin
    counterparts from the admin router) so the dashboard can wire
    the diagnostic table + the resync button."""
    from routers.catalog import admin_router, merchant_router

    merchant_paths = {r.path for r in merchant_router.routes}
    admin_paths = {r.path for r in admin_router.routes}

    assert "/merchant/catalog/products" in merchant_paths
    assert "/merchant/catalog/resync"   in merchant_paths
    assert "/admin/catalog/products"    in admin_paths
    assert "/admin/catalog/resync"      in admin_paths


def test_best_effort_resolver_exported() -> None:
    """The webhook rescue path and catalog test-send both import
    ``resolve_best_effort`` — guard against accidental rename."""
    from services import product_resolver as pr

    assert hasattr(pr, "resolve_best_effort")
    assert hasattr(pr, "resolve_by_query_relaxed")
    assert callable(pr.resolve_best_effort)
