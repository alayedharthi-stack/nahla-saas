"""Test-only helpers: undo exactly the log-redaction filters a test attached.

``core.log_redaction.install_log_redaction`` attaches one shared redacting
filter to every root handler that exists at that moment and to the library
loggers in ``DEFAULT_REDACTED_LOGGERS``. ``backend/main.py`` calls it at import
and some tests call it to get the production log path. Inside pytest the root
handlers that exist are pytest's own session-long logging handlers, so a filter
attached there stays for every later test in the process. Because the filter
rewrites the shared ``LogRecord`` in place, a later test that observes a
service's log line directly through ``caplog`` then sees the app-level
redaction instead of what the service emitted.

These helpers snapshot the filter lists of those handlers and loggers and
afterwards remove only the filters that were not there before. Filters that
were already present, including a redaction filter some earlier code
installed, are kept; handlers, levels and other logging state are not touched.
They change no production code and no redaction behaviour: a test that wants
the production log path still installs it, for as long as it runs.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator, List, Tuple

from core.log_redaction import DEFAULT_REDACTED_LOGGERS

FilterSnapshot = List[Tuple[logging.Filterer, Tuple[object, ...]]]


def _filterers() -> List[logging.Filterer]:
    root = logging.getLogger()
    return [*root.handlers, *(logging.getLogger(name) for name in DEFAULT_REDACTED_LOGGERS)]


def snapshot_log_filters() -> FilterSnapshot:
    """The current filters of every root handler and redacted library logger."""
    return [(target, tuple(target.filters)) for target in _filterers()]


def remove_added_log_filters(snapshot: FilterSnapshot) -> None:
    """Remove from each snapshotted target only filters added since the snapshot."""
    for target, before in snapshot:
        for added in [f for f in target.filters if not any(f is kept for kept in before)]:
            target.removeFilter(added)


@contextmanager
def restoring_log_filters() -> Iterator[None]:
    """Run the block, then remove only the log filters it attached."""
    snapshot = snapshot_log_filters()
    try:
        yield
    finally:
        remove_added_log_filters(snapshot)
