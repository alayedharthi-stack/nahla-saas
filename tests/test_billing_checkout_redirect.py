"""Tests for Moyasar checkout redirect URL construction."""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for _p in (REPO_ROOT, BACKEND_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from core.billing_redirects import (  # noqa: E402
    DEFAULT_PAYMENT_RESULT_BASE,
    build_moyasar_checkout_redirects,
)


def test_checkout_redirect_defaults_to_payment_result_page():
    success, error = build_moyasar_checkout_redirects(None, None, 42)
    assert success == f"{DEFAULT_PAYMENT_RESULT_BASE}?status=paid&sub_id=42"
    assert error == f"{DEFAULT_PAYMENT_RESULT_BASE}?status=failed&sub_id=42"
    assert "/app/pricing" not in success


def test_checkout_redirect_honours_frontend_bases():
    base = "https://staging.nahlah.ai/billing/payment-result"
    success, error = build_moyasar_checkout_redirects(base, base, 7)
    assert success == f"{base}?status=paid&sub_id=7"
    assert error == f"{base}?status=failed&sub_id=7"


def test_checkout_redirect_strips_trailing_slash():
    base = "https://app.nahlah.ai/billing/payment-result/"
    success, _ = build_moyasar_checkout_redirects(base, base, 1)
    assert success == "https://app.nahlah.ai/billing/payment-result?status=paid&sub_id=1"
