"""
Unsigned delivery headers, reduced to tightly validated canonical forms.

``X-Shopify-Webhook-Id`` and ``X-Shopify-Triggered-At`` are not covered by
the HMAC: anyone holding a signed body can resend it with arbitrary values.
They are diagnostics only — never evidence, never a dedupe key on their own,
never a timestamp that decides anything — and they are kept only when they
have exactly the expected form:

  * webhook id: a UUID (8-4-4-4-12 hex), stored lower-case;
  * triggered-at: an RFC 3339 UTC/offset timestamp, stored as a parsed
    ``datetime`` (fraction truncated to microseconds).

Anything else (an email, a token, free text, an oversized value) yields
``None``: it is neither stored nor logged, and it never refuses a signed
delivery.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional

_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_TS_RE = re.compile(
    r"(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})T(?P<time>[0-9]{2}:[0-9]{2}:[0-9]{2})"
    r"(?:\.(?P<frac>[0-9]{1,9}))?(?P<zone>Z|[+-][0-9]{2}:[0-9]{2})"
)


def canonical_webhook_id(raw: Optional[str]) -> Optional[str]:
    if not isinstance(raw, str) or len(raw) != 36 or not raw.isascii():
        return None
    return raw.lower() if _UUID_RE.fullmatch(raw) else None


def parse_triggered_at(raw: Optional[str]) -> Optional[datetime]:
    if not isinstance(raw, str) or len(raw) > 40 or not raw.isascii():
        return None
    match = _TS_RE.fullmatch(raw)
    if match is None:
        return None
    frac = (match.group("frac") or "")[:6].ljust(6, "0")
    zone = match.group("zone")
    try:
        value = datetime.strptime(f"{match.group('date')}T{match.group('time')}", "%Y-%m-%dT%H:%M:%S")
        if zone == "Z":
            offset = timedelta(0)
        else:
            sign = 1 if zone[0] == "+" else -1
            hours, minutes = int(zone[1:3]), int(zone[4:6])
            if hours > 23 or minutes > 59:
                return None
            offset = sign * timedelta(hours=hours, minutes=minutes)
        value = value.replace(microsecond=int(frac), tzinfo=timezone(offset)).astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None
    if not 2000 <= value.year <= 2100:
        return None
    return value
