"""Tests for Salla coupon fetch SLA + exception classification."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for p in (REPO_ROOT, BACKEND_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from services.salla_coupon_fetch import (
    classify_fetch_exception,
    empty_fetch_result,
    poll_interval_seconds_for_catalog,
    tenant_poll_due,
)
from store_adapters.salla_adapter import SallaTokenRevokedException


def test_poll_interval_seconds_for_catalog_sla():
    assert poll_interval_seconds_for_catalog(50) == 60
    assert poll_interval_seconds_for_catalog(120) == 60
    assert poll_interval_seconds_for_catalog(121) == 300
    assert poll_interval_seconds_for_catalog(600) == 300
    assert poll_interval_seconds_for_catalog(601) == 900


def test_empty_fetch_result_shape():
    result = empty_fetch_result(failure_class="server_error", http_status=500)
    assert result["ok"] is False
    assert result["partial"] is False
    assert result["items"] == []


def test_classify_fetch_exception_rate_limited():
    response = httpx.Response(429, headers={"Retry-After": "30"}, request=httpx.Request("GET", "https://example.com"))
    exc = httpx.HTTPStatusError("rate limited", request=response.request, response=response)
    info = classify_fetch_exception(exc)
    assert info["failure_class"] == "rate_limited"
    assert info["http_status"] == 429
    assert info["retry_after"] == 30


def test_classify_fetch_exception_needs_reauth():
    info = classify_fetch_exception(SallaTokenRevokedException("revoked"))
    assert info["failure_class"] == "needs_reauth"


def test_tenant_poll_due_without_meta_is_immediate():
    assert tenant_poll_due(None) is True

def test_expiry_riyadh_midnight_boundary():
  from datetime import datetime, timezone

  from services.coupon_salla_push import normalize_salla_coupon_push_dates

  now = datetime(2026, 8, 25, 21, 0, tzinfo=timezone.utc)
  just_before_midnight = datetime(2026, 8, 26, 20, 59, tzinfo=timezone.utc)
  just_after_midnight = datetime(2026, 8, 26, 21, 0, tzinfo=timezone.utc)

  _start, expiry_before = normalize_salla_coupon_push_dates(now, just_before_midnight, now=now)
  _start2, expiry_after = normalize_salla_coupon_push_dates(now, just_after_midnight, now=now)

  assert expiry_before == "2026-08-26"
  assert expiry_after == "2026-08-27"
