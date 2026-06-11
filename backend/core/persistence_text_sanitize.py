"""
core/persistence_text_sanitize.py
─────────────────────────────────
Strip NUL and other unsafe control characters from text before PostgreSQL
persistence.

pypdf (and some bank PDF generators) occasionally embed ``\\x00`` bytes in
extracted text. psycopg2 rejects those in ``TEXT`` columns with
``ValueError``, which rolls back the session and can cascade into
``PendingRollbackError`` on the same request.

Preserves horizontal tab, newline, and carriage return so Arabic/English
receipt bodies stay readable.
"""
from __future__ import annotations

import re
from typing import Optional

# Unsafe for PostgreSQL TEXT / typical JSON string persistence.
# Keep TAB (0x09), LF (0x0A), CR (0x0D).
_UNSAFE_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_persistence_text(text: Optional[str]) -> str:
    """Return ``text`` with NUL and unsafe control chars removed.

    Non-string input is coerced with ``str()``. ``None`` → ``""``.
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    if not text:
        return ""
    return _UNSAFE_CONTROL_RE.sub("", text)


__all__ = ["sanitize_persistence_text"]
