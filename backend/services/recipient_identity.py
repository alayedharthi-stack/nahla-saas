"""One campaign recipient, whatever spelling a row, attempt, customer or
conversation stored for it.

The identity is the platform's own validated E.164 form —
``utils.phone_utils.normalize_to_e164`` (libphonenumber, with the platform's
local-format rules: ``05…`` / ``5…`` / ``966…`` / ``00966…``) — the same
function customer phones are normalised with. Stripping punctuation is not
enough: ``+966…``, ``966…``, ``00966…`` and ``05…`` are one person, and
``+9665…`` with a missing digit is nobody.

Matching a stored value against a recipient is conservative: a value that
has its own valid identity matches only that identity; a value that cannot
be validated (a truncated or local spelling the rules do not recognise) is
treated as *possibly* the recipient when its significant digits are the
tail of the recipient's number. Callers that guard sends read "possibly" as
"yes".

Used by the campaign send guard (``campaign_send_ledger``) and by the
read-only incident RCA, so both see the same recipients.
"""
from __future__ import annotations

import hashlib
import unicodedata
from typing import Any, Optional

from utils.phone_utils import normalize_to_e164

# Shortest tail of digits a stored spelling must share with a recipient's
# number before it is considered possibly that recipient (and the length of
# the SQL prefilter suffix, which is therefore a superset).
MATCH_SUFFIX_DIGITS = 7


def digits(raw: Any) -> str:
    """ASCII digits of ``raw``, with every Unicode decimal digit (Arabic-Indic
    ``٠٥…``, Persian ``۰۵…``, fullwidth ``０５…``) mapped to its value — the
    same digits libphonenumber reads."""
    return "".join(str(unicodedata.decimal(ch)) for ch in str(raw or "") if ch.isdecimal())


def canonical_recipient(raw: Any) -> Optional[str]:
    """Validated E.164 identity, or ``None`` when the value is not a
    recognisable phone number."""
    text = str(raw or "").strip()
    if not text or not digits(text):
        return None
    # normalize_to_e164 returns None for anything it cannot parse; any other
    # error propagates, so a guard fails closed rather than guessing.
    return normalize_to_e164(text)


def may_be_recipient(stored: Any, canonical: str) -> bool:
    """Whether ``stored`` is, or may be, the recipient ``canonical``."""
    if not canonical:
        return False
    own = canonical_recipient(stored)
    if own is not None:
        return own == canonical
    significant = digits(stored).lstrip("0")
    return len(significant) >= MATCH_SUFFIX_DIGITS and digits(canonical).endswith(significant)


def match_suffix(canonical: str) -> str:
    """Digits every spelling of ``canonical`` ends with (SQL prefilter)."""
    return digits(canonical)[-MATCH_SUFFIX_DIGITS:]


def lock_key(scope: str, canonical: str) -> int:
    """Stable signed 64-bit key for a PostgreSQL advisory lock."""
    return int.from_bytes(hashlib.sha1(f"{scope}:{canonical}".encode()).digest()[:8],
                          "big", signed=True)
