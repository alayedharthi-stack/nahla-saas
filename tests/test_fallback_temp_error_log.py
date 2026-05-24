"""
tests/test_fallback_temp_error_log.py
─────────────────────────────────────
Coverage for ``services.fallback_policy.emit_temp_error_fallback_log``
— the structured ``[AI_TEMP_ERROR_FALLBACK]`` log line introduced in
May 2026 #42.

Background
──────────
Before #42, every "حصل خطأ مؤقت 🙏 ممكن تعيد رسالتك؟" emission was a
black box: ops had to grep generic ``[Merchant/Brain]`` lines and pray
they correlated with the customer's timestamp. The merchant's Tenant
33 filed two complaints on May 25 with no greppable root cause.

The fix added one structured log line — emitted at every fallback site
(brain exception, outer exception, normalizer exception) — carrying:

  tenant_id, conversation_id, sender (masked), inbound_msg_id, msg_type,
  intent, stage, exception_class, error_message, fallback_kind,
  response_goal, git_sha.

These tests pin the format so a future refactor can't silently drop a
field that ops has come to rely on.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


# ────────────────────────────────────────────────────────────────────
# Stage vocabulary — exposed publicly for the webhook to import
# ────────────────────────────────────────────────────────────────────


def test_stage_constants_are_pinned_strings() -> None:
    """The stage labels feed log search dashboards; keep them stable."""
    from services.fallback_policy import (
        STAGE_BRAIN_EXCEPTION,
        STAGE_MEDIA_FALLBACK,
        STAGE_NO_AI,
        STAGE_NORMALIZER_EXCEPTION,
        STAGE_OUTER_EXCEPTION,
        STAGE_PRE_BRAIN_HANDOFF,
    )

    assert STAGE_BRAIN_EXCEPTION       == "brain_exception"
    assert STAGE_OUTER_EXCEPTION       == "outer_exception"
    assert STAGE_NORMALIZER_EXCEPTION  == "normalizer_exception"
    assert STAGE_PRE_BRAIN_HANDOFF     == "pre_brain_handoff_send"
    assert STAGE_MEDIA_FALLBACK        == "media_fallback"
    assert STAGE_NO_AI                 == "no_ai_configured"


# ────────────────────────────────────────────────────────────────────
# emit_temp_error_fallback_log — happy path
# ────────────────────────────────────────────────────────────────────


@pytest.fixture()
def caplog_warn(caplog):
    caplog.set_level(logging.WARNING, logger="nahla.fallback")
    return caplog


def test_emit_writes_one_line_with_all_required_fields(caplog_warn) -> None:
    """Every required field must appear in the single log line so
    operations can grep ``[AI_TEMP_ERROR_FALLBACK]`` and pull the full
    context without reading multiple log entries."""
    from services.fallback_policy import emit_temp_error_fallback_log

    boom = RuntimeError("anthropic timeout after 60s")
    emit_temp_error_fallback_log(
        tenant_id=33,
        conversation_id=12345,
        sender="966500000111",
        inbound_msg_id="wamid.HBg",
        msg_type="text",
        intent="ASK_OWNER_CONTACT",
        stage="brain_exception",
        exception=boom,
        fallback_kind="neutral_retry",
        response_goal="retry",
    )

    assert caplog_warn.records, "expected at least one log record"
    text = caplog_warn.records[-1].getMessage()
    assert text.startswith("[AI_TEMP_ERROR_FALLBACK]"), text

    for snippet in (
        "tenant_id=33",
        "conversation_id=12345",
        "inbound_msg_id=wamid.HBg",
        "msg_type=text",
        "intent=ASK_OWNER_CONTACT",
        "stage=brain_exception",
        "exception_class=RuntimeError",
        "error_message=anthropic timeout after 60s",
        "fallback_kind=neutral_retry",
        "response_goal=retry",
        "git_sha=",
    ):
        assert snippet in text, f"missing field {snippet!r} in log: {text}"


def test_emit_masks_sender_phone(caplog_warn) -> None:
    """Sender phone is partially redacted in logs — same masking
    policy as ai_quality_events. Production rotation logs are
    long-lived; we don't want full E.164 numbers in them."""
    from services.fallback_policy import emit_temp_error_fallback_log

    emit_temp_error_fallback_log(
        tenant_id=1,
        sender="966512345678",
        stage="outer_exception",
        exception=ValueError("X"),
    )
    text = caplog_warn.records[-1].getMessage()
    # Original digits in the middle MUST be masked
    assert "966512345678" not in text
    # Some prefix + suffix preserved for matching across logs
    assert "sender=966***678" in text


def test_emit_falls_back_to_dash_for_missing_fields(caplog_warn) -> None:
    """When the caller doesn't have a value, the formatter renders
    ``-`` instead of ``None`` — keeps the log easy to parse."""
    from services.fallback_policy import emit_temp_error_fallback_log

    emit_temp_error_fallback_log(
        tenant_id=7,
        stage="brain_exception",
    )
    text = caplog_warn.records[-1].getMessage()
    assert "tenant_id=7" in text
    assert "conversation_id=-" in text
    assert "sender=-" in text
    assert "inbound_msg_id=-" in text
    assert "exception_class=-" in text


def test_emit_truncates_long_error_message(caplog_warn) -> None:
    """A noisy stack-trace string must not blow up a single-line
    greppable entry. Limit is 200 chars — enough for the root error
    but not enough to cause log-shipping breakage."""
    from services.fallback_policy import emit_temp_error_fallback_log

    long_msg = "x" * 5000
    emit_temp_error_fallback_log(
        tenant_id=1,
        stage="outer_exception",
        error_message=long_msg,
    )
    text = caplog_warn.records[-1].getMessage()
    # The 'error_message=xxxx...' segment must be clipped well under 5000
    assert "x" * 5000 not in text
    assert len(text) < 1500


def test_emit_strips_newlines_from_values(caplog_warn) -> None:
    """Multiline values would break ``[AI_TEMP_ERROR_FALLBACK]`` as a
    single greppable line. Every value passes through a sanitiser
    that collapses ``\\n / \\r / \\t`` to spaces."""
    from services.fallback_policy import emit_temp_error_fallback_log

    emit_temp_error_fallback_log(
        tenant_id=1,
        stage="outer_exception",
        error_message="line1\nline2\rline3\tline4",
    )
    text = caplog_warn.records[-1].getMessage()
    assert "\n" not in text or text.count("\n") == 1  # the trailing logger newline only
    assert "line1 line2 line3 line4" in text


def test_emit_never_raises_on_unicode_or_weird_input(caplog_warn) -> None:
    """Observability MUST NOT take down the response path. Even when
    handed bizarre input, the emit call returns cleanly."""
    from services.fallback_policy import emit_temp_error_fallback_log

    weird = {"deeply": {"nested": ["مرحبا", 1, None]}}
    emit_temp_error_fallback_log(
        tenant_id="not-an-int-but-fine",
        conversation_id=weird,
        sender="\u202bRTL\u202cprefix",
        stage="brain_exception",
        exception=ValueError("fail \U0001f600"),
        extra={"foo": "bar", "intent_score": 0.92},
    )
    text = caplog_warn.records[-1].getMessage()
    assert text.startswith("[AI_TEMP_ERROR_FALLBACK]")
    assert "tenant_id=not-an-int-but-fine" in text
    assert "stage=brain_exception" in text
    assert "foo=bar" in text


def test_emit_resolves_git_sha_from_env(monkeypatch, caplog_warn) -> None:
    """``git_sha`` field must reflect the active build so we can
    correlate fallback spikes with deploys."""
    from services import fallback_policy

    monkeypatch.setenv("GIT_SHA", "abcdef1234567890")
    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
    monkeypatch.delenv("SOURCE_COMMIT", raising=False)
    monkeypatch.delenv("COMMIT_SHA", raising=False)

    fallback_policy.emit_temp_error_fallback_log(
        tenant_id=1, stage="outer_exception",
    )
    text = caplog_warn.records[-1].getMessage()
    # Should be clipped to first 12 chars
    assert "git_sha=abcdef123456" in text


def test_emit_falls_back_to_unknown_when_no_sha_env(monkeypatch, caplog_warn) -> None:
    from services import fallback_policy

    for var in (
        "RAILWAY_GIT_COMMIT_SHA",
        "GIT_SHA",
        "SOURCE_COMMIT",
        "COMMIT_SHA",
    ):
        monkeypatch.delenv(var, raising=False)

    fallback_policy.emit_temp_error_fallback_log(
        tenant_id=1, stage="brain_exception",
    )
    text = caplog_warn.records[-1].getMessage()
    assert "git_sha=unknown" in text
