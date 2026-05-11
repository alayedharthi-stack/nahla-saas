"""
tests/test_support_session_billing_reads.py
───────────────────────────────────────────
Locks the contract that platform admins impersonating a merchant
via support-access can call a NARROW set of read-only billing
endpoints (`GET /billing/status`, `/billing/plans`, etc.) while
WRITE endpoints under the same prefix remain blocked.

Why this matters
────────────────
Support's #1 question during an active session is "why are
outbound campaigns being rejected?" — the answer almost always
lives in `GET /billing/status` (trial expired, plan downgraded,
payment failed). Forcing support onto a separate channel just
to read subscription state defeats the whole point of the
impersonation flow.

The fix is intentionally restrictive: every entry in
`_SUPPORT_ALLOWED_READS` is an explicit (METHOD, path) tuple that
has been audited to:
  1. Mutate nothing.
  2. Return no card-PAN-grade secrets.
  3. Not echo bearer tokens / API keys back to the client.

This test enumerates those tuples and assert that:
  * Every read on the allow-list passes the matching gate.
  * Every WRITE under the same prefix is still blocked.
  * Non-billing blocked prefixes (e.g. `/auth/change-password`)
    are NOT accidentally allowed.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in [str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")]:
    if p not in sys.path:
        sys.path.insert(0, p)


def _decide(method: str, path: str) -> bool:
    """Re-implementation of the middleware's two-tier matching
    logic (same expressions, no side-effects). Returns True when
    the support session should be BLOCKED for this request."""
    from core.middleware import (
        _SUPPORT_ALLOWED_READS,
        _SUPPORT_BLOCKED_PATHS,
    )
    is_blocked_prefix = any(
        path.startswith(blocked) for blocked in _SUPPORT_BLOCKED_PATHS
    )
    is_allowed_read = (method.upper(), path) in _SUPPORT_ALLOWED_READS
    return is_blocked_prefix and not is_allowed_read


# ──────────────────────────────────────────────────────────────────────


class TestBillingReadAllowList:
    def test_billing_status_get_is_allowed(self):
        """The canonical case — diagnosing a 'campaigns rejected'
        complaint without dragging the merchant onto a call."""
        assert _decide("GET", "/billing/status") is False

    def test_billing_plans_get_is_allowed(self):
        assert _decide("GET", "/billing/plans") is False

    def test_billing_entitlements_get_is_allowed(self):
        assert _decide("GET", "/billing/entitlements") is False

    def test_billing_payment_result_get_is_allowed(self):
        """Read-only return page after a card redirect. The handler
        consults provider state but never mutates billing rows."""
        assert _decide("GET", "/billing/payment-result") is False

    def test_billing_debug_current_get_is_allowed(self):
        assert _decide("GET", "/billing/debug/current") is False

    def test_billing_subscribe_post_remains_blocked(self):
        """The write endpoint MUST stay blocked — even though
        the allow-list shares its prefix."""
        assert _decide("POST", "/billing/subscribe") is True

    def test_billing_checkout_post_remains_blocked(self):
        assert _decide("POST", "/billing/checkout") is True

    def test_billing_reset_trial_post_remains_blocked(self):
        assert _decide("POST", "/billing/reset-trial") is True

    def test_billing_hyperpay_payment_link_remains_blocked(self):
        assert _decide("POST", "/billing/hyperpay/payment-link") is True

    def test_billing_status_with_wrong_method_is_blocked(self):
        """The allow-list is (method, exact_path). A POST to the
        same path is NOT allowed — defense in depth, in case a
        future refactor adds a write endpoint that happens to
        share the path name."""
        assert _decide("POST", "/billing/status") is True
        assert _decide("PUT",  "/billing/status") is True

    def test_billing_status_with_unrelated_suffix_is_blocked(self):
        """A hypothetical `/billing/status/cancel` write endpoint
        must NOT inherit the allow-list of its parent. The check
        is exact-path, NOT prefix."""
        assert _decide("GET", "/billing/status/cancel") is True


class TestNonBillingBlockedPathsAreNotLeaked:
    def test_auth_change_password_post_still_blocked(self):
        """The allow-list is scoped to read-only billing — it must
        not accidentally permit anything under other blocked
        prefixes."""
        assert _decide("POST", "/auth/change-password") is True

    def test_settings_integrations_get_still_blocked(self):
        assert _decide("GET", "/settings/integrations") is True

    def test_whatsapp_direct_connect_post_still_blocked(self):
        assert _decide("POST", "/whatsapp/direct/connect") is True

    def test_tenant_delete_post_still_blocked(self):
        assert _decide("POST", "/tenant/delete") is True


class TestUnrelatedPathsArePassthrough:
    def test_conversations_messages_passes(self):
        """Non-blocked path → not blocked regardless of method."""
        assert _decide("GET",  "/conversations/messages/+966500000111") is False
        assert _decide("POST", "/conversations/reply") is False

    def test_campaigns_passes(self):
        assert _decide("GET",  "/campaigns") is False
        assert _decide("POST", "/campaigns/test-send") is False

    def test_admin_debug_media_env_passes(self):
        """The internal-debug endpoints are NOT blocked-prefix —
        they're admin-gated server-side via require_admin. The
        support middleware doesn't intercept them."""
        assert _decide("GET", "/admin/debug/media-env") is False


class TestAllowListInvariants:
    def test_every_allow_list_entry_is_under_a_blocked_prefix(self):
        """If we ever add an allow-list entry that DOESN'T match a
        blocked prefix, it's a smell — the allow-list is meant to
        be a narrow exception under blocks, not a global override."""
        from core.middleware import (
            _SUPPORT_ALLOWED_READS,
            _SUPPORT_BLOCKED_PATHS,
        )
        for method, path in _SUPPORT_ALLOWED_READS:
            under_block = any(
                path.startswith(p) for p in _SUPPORT_BLOCKED_PATHS
            )
            assert under_block, (
                f"allow-list entry {method} {path!r} is not under any "
                f"blocked prefix — remove it or add a justification"
            )

    def test_every_allow_list_entry_is_a_read_method(self):
        """Allow-listed entries must be read methods. A write entry
        here is a bug — write paths should never be allowed during
        support sessions, even if they're "safe-looking"."""
        from core.middleware import _SUPPORT_ALLOWED_READS
        for method, path in _SUPPORT_ALLOWED_READS:
            assert method in ("GET", "HEAD"), (
                f"allow-list entry has non-read method: {method} {path}"
            )
