"""The test-only redaction cleanup removes what a test added and keeps everything else."""
from __future__ import annotations

import io
import logging

from backend.tests._log_redaction_isolation import (
    remove_added_log_filters,
    restoring_log_filters,
    snapshot_log_filters,
)
from core.log_redaction import DEFAULT_REDACTED_LOGGERS, SecretRedactingFilter, install_log_redaction


class _Sentinel(logging.Filter):
    pass


def _stream_handler() -> tuple[logging.Handler, io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    return handler, stream


def test_only_the_filters_added_after_the_snapshot_are_removed() -> None:
    root = logging.getLogger()
    handler, _ = _stream_handler()
    sentinel = _Sentinel()
    handler.addFilter(sentinel)
    root.addHandler(handler)
    try:
        with restoring_log_filters():
            snapshot = snapshot_log_filters()
            install_log_redaction()
            assert any(isinstance(f, SecretRedactingFilter) for f in handler.filters)
            remove_added_log_filters(snapshot)
            assert handler.filters == [sentinel]
            assert [(t, tuple(t.filters)) for t, _ in snapshot] == snapshot
    finally:
        root.removeHandler(handler)


def test_a_redaction_filter_that_was_already_installed_is_kept_and_still_redacts() -> None:
    root = logging.getLogger()
    handler, stream = _stream_handler()
    earlier = SecretRedactingFilter()
    handler.addFilter(earlier)
    root.addHandler(handler)
    logger = logging.getLogger("nahla.test_isolation.redaction")
    try:
        with restoring_log_filters():
            install_log_redaction()
        assert handler.filters == [earlier]
        logger.warning("callback code=%s", "kept-filter-canary")
        out = stream.getvalue()
        assert "kept-filter-canary" not in out
        assert "code=REDACTED" in out
    finally:
        root.removeHandler(handler)


def test_named_library_loggers_get_back_exactly_their_previous_filters() -> None:
    before = {name: list(logging.getLogger(name).filters) for name in DEFAULT_REDACTED_LOGGERS}
    with restoring_log_filters():
        install_log_redaction()
        for name in DEFAULT_REDACTED_LOGGERS:
            assert any(isinstance(f, SecretRedactingFilter) for f in logging.getLogger(name).filters), name
    assert {name: list(logging.getLogger(name).filters) for name in DEFAULT_REDACTED_LOGGERS} == before
