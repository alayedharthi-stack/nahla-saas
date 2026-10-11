"""
Flags and fixed bounds of the Shopify privacy compliance slice.

Everything defaults to off:

  NAHLA_SHOPIFY_PRIVACY_INTAKE_ENABLED       truthy (1/true/yes/on): the compliance
                                             webhook routes exist. Off: 404.
  NAHLA_SHOPIFY_PRIVACY_WORKER_ENABLED       truthy: the evaluation worker may run
                                             (and is queued at startup). Off: it
                                             never touches the database.
  NAHLA_SHOPIFY_PRIVACY_DESTRUCTIVE_ENABLED  truthy: the operator-only executor may
                                             run — and still refuses without an
                                             approved policy object, which this
                                             slice does not contain.

The intake does not need ``NAHLA_SHOPIFY_CONNECTION_ENABLED``: compliance
requests must be received even while the connection feature is switched off.
It does need the app's client secret (``SHOPIFY_CLIENT_SECRET``), which signs
every webhook body; without it every delivery is refused with 401.

Timing facts (coordinator-verified Shopify documentation, 2026-10-11): action
deadline 30 days after receipt; whole delivery within 5 seconds; 8 retries
over 4 hours; ``shop/redact`` about 48 hours after uninstall. The worker's
backoff values below are engineering defaults for re-evaluation, not
retention periods; no retention period is defined by this slice.
"""
from __future__ import annotations

import os
from typing import Mapping, Optional

from services.shopify_connection.webhooks import MAX_BODY_BYTES as FOUNDATION_MAX_BODY_BYTES

INTAKE_ENABLED_ENV = "NAHLA_SHOPIFY_PRIVACY_INTAKE_ENABLED"
WORKER_ENABLED_ENV = "NAHLA_SHOPIFY_PRIVACY_WORKER_ENABLED"
DESTRUCTIVE_ENABLED_ENV = "NAHLA_SHOPIFY_PRIVACY_DESTRUCTIVE_ENABLED"
CLIENT_SECRET_ENV = "SHOPIFY_CLIENT_SECRET"

ROUTE_PREFIX = "/webhooks/shopify/privacy"
# Plural "/webhooks/" on purpose: core.middleware._safe_fallback_response
# rewrites failures under the singular "/webhook/" prefix to HTTP 200, which
# would acknowledge a delivery that was never stored.
TOPIC_PATHS = {
    "customers/data_request": f"{ROUTE_PREFIX}/customers-data-request",
    "customers/redact": f"{ROUTE_PREFIX}/customers-redact",
    "shop/redact": f"{ROUTE_PREFIX}/shop-redact",
}

# The cap bounds memory before the HMAC is checked. It is the connection
# foundation's own limit, because the foundation's verified raw-body HMAC
# helper is reused unchanged (it refuses larger bodies). 64 KiB holds roughly
# 4,500 order ids; a larger genuine delivery is answered 413 and Shopify
# redelivers, then stops — raising the bound needs a reviewed change to that
# helper (a rollout prerequisite, see docs/engineering/shopify-privacy-webhooks.md).
MAX_BODY_BYTES = FOUNDATION_MAX_BODY_BYTES
MAX_ORDER_IDS = 10_000
MAX_EMAIL_CHARS = 320
MAX_PHONE_CHARS = 64

DEADLINE_DAYS = 30

# Supported delivery budget (Shopify allows 5 s for the whole delivery):
#   body read            <= RECEIPT_BODY_READ_SECONDS (else 408, redelivered)
#   HMAC + validation    in-memory, bounded by the 64 KiB body (milliseconds)
#   durable receipt      <= RECEIPT_BUDGET_SECONDS end to end: slot admission
#                        (non-blocking), pool checkout, connect, schema
#                        inspection, receipt SQL and commit (else 503)
# => at most about 4 s before the answer, plus framework overhead.
# The receipt runs on a dedicated, bounded thread pool and its own small
# engine (the shared DATABASE_URL with a separate pool and tighter timeouts;
# the shared engine's defaults are unchanged), never on the event loop.
RECEIPT_BODY_READ_SECONDS = 1.0
RECEIPT_BUDGET_SECONDS = 3.0
RECEIPT_WORKERS = 4
RECEIPT_POOL_TIMEOUT_SECONDS = 1
RECEIPT_CONNECT_TIMEOUT_SECONDS = 2
RECEIPT_STATEMENT_TIMEOUT_MS = 2500
RECEIPT_LOCK_TIMEOUT = "1500ms"
RECEIPT_STATEMENT_TIMEOUT = f"{RECEIPT_STATEMENT_TIMEOUT_MS}ms"

WORKER_LEASE_SECONDS = 60
WORKER_BACKOFF_BASE_SECONDS = 300
WORKER_BACKOFF_CAP_SECONDS = 21_600
WORKER_MAX_EVIDENCE_ATTEMPTS = 16
WORKER_TICK_SECONDS = 300
WORKER_BATCH_LIMIT = 10

# The data-flow audit this slice's bounded assessments are recorded against.
# Bump it whenever an audited table's columns or a legacy data flow change.
AUDIT_VERSION = "shopify-product-only-schema-audit-v1"


def _env(env: Optional[Mapping[str, str]]) -> Mapping[str, str]:
    return env if env is not None else os.environ


def _flag(env: Optional[Mapping[str, str]], name: str) -> bool:
    return str(_env(env).get(name) or "").strip().lower() in ("1", "true", "yes", "on")


def intake_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    return _flag(env, INTAKE_ENABLED_ENV)


def worker_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    return _flag(env, WORKER_ENABLED_ENV)


def destructive_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    return _flag(env, DESTRUCTIVE_ENABLED_ENV)


def webhook_secret(env: Optional[Mapping[str, str]] = None) -> str:
    """The app's client secret ('' when missing: every delivery is refused)."""
    return str(_env(env).get(CLIENT_SECRET_ENV) or "").strip()
