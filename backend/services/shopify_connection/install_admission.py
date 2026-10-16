"""Closed pilot admission, independent of Shopify owner authentication.

Only shop-bound hashes and UTC deadlines are configured on the server. The
256-bit bearer code travels in the start POST body, never an OAuth URL or row.
No configuration means no admission; this is not a general DoS defense.
"""
import hashlib
import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone

from services.shopify_connection.shop_domain import canonical_shop_domain

ENV_KEY = "SHOPIFY_INSTALL_PILOT_ADMISSION"
MAX_PILOT_SHOPS = 5
MAX_VALIDITY = timedelta(days=7)
HEX = re.compile(r"[a-f0-9]{64}")


def code_hash(shop, code):
    return hashlib.sha256(f"nahla.shopify.pilot:v1:{shop}:{code}".encode("ascii")).hexdigest()


def _unique_object(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate_admission_key")
        out[key] = value
    return out


def admitted(shop, code, *, now=None, env=None):
    """Fail closed before any DB/lease/quota work, without logging input."""
    env = os.environ if env is None else env
    now = now or datetime.now(timezone.utc)
    if not isinstance(code, str) or not HEX.fullmatch(code):
        return False
    raw = env.get(ENV_KEY, "")
    if not isinstance(raw, str) or not 0 < len(raw) <= 4096:
        return False
    try:
        entries = json.loads(raw, object_pairs_hook=_unique_object)
        if not isinstance(entries, dict) or not 0 < len(entries) <= MAX_PILOT_SHOPS:
            return False
        for domain, entry in entries.items():
            if canonical_shop_domain(domain) != domain or not isinstance(entry, dict) or set(entry) != {"sha256", "expires_at"}:
                return False
            if not isinstance(entry["sha256"], str) or not HEX.fullmatch(entry["sha256"]):
                return False
            expires = entry["expires_at"]
            if not isinstance(expires, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", expires):
                return False
            deadline = datetime.fromisoformat(expires.replace("Z", "+00:00"))
            # Expired peers do not deny valid pilots, but malformed/far-future
            # configuration fails closed. Removing/replacing a hash revokes it.
            if deadline > now + MAX_VALIDITY:
                return False
        entry = entries.get(shop)
        return bool(entry and datetime.fromisoformat(entry["expires_at"].replace("Z", "+00:00")) > now
                    and secrets.compare_digest(entry["sha256"], code_hash(shop, code)))
    except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
        return False
