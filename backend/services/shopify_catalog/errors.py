"""
Closed set of import outcomes. Only these codes ever reach a run row, a log
line, an HTTP response or the dashboard — never an upstream message, body,
URL, token or exception text.
"""
from __future__ import annotations

# The run was stopped because the connection changed under it.
R_CONNECTION_UNAVAILABLE = "connection_unavailable"   # not active (disconnect, uninstall, quarantine, reauth)
R_CONNECTION_CHANGED = "connection_changed"           # generation / shop identity changed (reinstall)
R_INTERRUPTED = "interrupted"                          # lease expired without a result (crash, restart)
# Credentials and permissions.
R_CREDENTIAL_UNAVAILABLE = "credential_unavailable"   # the foundation yielded no usable access token
R_CREDENTIALS_REJECTED = "credentials_rejected"       # Shopify answered 401 / 403
R_CREDENTIAL_CHURN = "credential_churn"               # too many rotations during one run
R_SCOPE_MISSING = "scope_missing"                     # read_products not granted
R_SCOPE_DENIED = "scope_denied"                       # Shopify answered ACCESS_DENIED
R_IDENTITY_MISMATCH = "identity_mismatch"             # the token's shop is not the connection's shop
R_SHOP_UNAVAILABLE = "shop_unavailable"               # 402 / 404 / 423 (frozen, missing, locked)
# Transport and protocol.
R_API_VERSION_MISMATCH = "api_version_mismatch"       # served by a version other than 2026-10
R_THROTTLED = "throttled"
R_UPSTREAM_UNAVAILABLE = "upstream_unavailable"       # 5xx / 429 / timeouts after bounded retries
R_QUERY_COST_EXCEEDED = "query_cost_exceeded"
R_GRAPHQL_ERROR = "graphql_error"                     # any GraphQL error (partial data is refused)
R_RESPONSE_TOO_LARGE = "response_too_large"
R_INVALID_RESPONSE = "invalid_response"               # missing field, wrong type, unknown enum value
R_DEADLINE_EXCEEDED = "deadline_exceeded"
# Identity and completeness.
R_INVALID_IDENTITY = "invalid_identity"               # malformed GID, legacy id or parent link
R_DUPLICATE_IDENTITY = "duplicate_identity"
R_IDENTITY_CONFLICT = "identity_conflict"             # an identity contradicts the snapshot or the mirror
R_PAGINATION_LOOP = "pagination_loop"                 # repeated / missing cursor, stalled page
R_CATALOG_TOO_LARGE = "catalog_too_large"             # beyond the 25,000 pagination cap or a fixed bound
R_SOURCE_CHANGED = "source_changed_during_import"     # counts did not reconcile after a bounded re-read
R_TRAVERSAL_INCONSISTENT = "traversal_inconsistent"   # an object absent from the traversal still exists
# Platform.
R_STORAGE_UNAVAILABLE = "storage_unavailable"
R_INTERNAL_ERROR = "internal_error"

FAILURE_REASONS = frozenset({
    R_CONNECTION_UNAVAILABLE, R_CONNECTION_CHANGED, R_INTERRUPTED, R_CREDENTIAL_UNAVAILABLE,
    R_CREDENTIALS_REJECTED, R_CREDENTIAL_CHURN, R_SCOPE_MISSING, R_SCOPE_DENIED, R_IDENTITY_MISMATCH,
    R_SHOP_UNAVAILABLE, R_API_VERSION_MISMATCH, R_THROTTLED, R_UPSTREAM_UNAVAILABLE, R_QUERY_COST_EXCEEDED,
    R_GRAPHQL_ERROR, R_RESPONSE_TOO_LARGE, R_INVALID_RESPONSE, R_DEADLINE_EXCEEDED, R_INVALID_IDENTITY,
    R_DUPLICATE_IDENTITY, R_IDENTITY_CONFLICT, R_PAGINATION_LOOP, R_CATALOG_TOO_LARGE, R_SOURCE_CHANGED,
    R_TRAVERSAL_INCONSISTENT, R_STORAGE_UNAVAILABLE, R_INTERNAL_ERROR,
})

# Reasons recorded as ``cancelled`` (the run was fenced out) rather than ``failed``.
CANCEL_REASONS = frozenset({R_CONNECTION_UNAVAILABLE, R_CONNECTION_CHANGED})


class ImportFailure(Exception):
    """A run-ending outcome with a fixed code. ``str()`` is the code only."""

    def __init__(self, reason: str):
        reason = reason if reason in FAILURE_REASONS else R_INTERNAL_ERROR
        super().__init__(reason)
        self.reason = reason


class LeaseLost(Exception):
    """This worker no longer owns the run: it must stop and write nothing."""


class CredentialRotated(Exception):
    """The stored credential changed after a page was fetched: the page is
    discarded and fetched again with the current credential."""
