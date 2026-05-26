"""tests/test_d360_dispatch_telemetry.py
─────────────────────────────────────
Wave 2.0 Phase 1.5 (May 2026) — narrow 360dialog dispatch-gap probe.

The module is **observation-only**: it must produce greppable
``[D360_RAW_INBOUND]`` / ``[D360_DISPATCH_GAP]`` / ``[D360_BRANCH]``
log lines without changing any caller's behaviour.

Tests in this file enforce two invariants:

  (a) the module itself does what its docstring promises (closed
      reason vocabulary, kill switch, masking, never-raises contract,
      ``msgs_count <= 0`` is a hard no-op for the gap line), AND
  (b) the line shapes contain the fields operators need to grep
      a masked sender (e.g. ``*2692``) and answer "did this inbound
      arrive at all, and if so, on which field/branch?".
"""
from __future__ import annotations

import logging
from typing import Any, List

import pytest

from core.d360_dispatch_telemetry import (  # noqa: E402
    ALL_BRANCHES,
    ALL_GAP_REASONS,
    BRANCH_COEXISTENCE,
    BRANCH_IGNORED,
    BRANCH_MESSAGES,
    BRANCH_SMB_MESSAGE_ECHOES,
    BRANCH_STATUS,
    REASON_AMBIGUOUS_PHONE_ID,
    REASON_BAD_SECRET,
    REASON_FIELD_IGNORED,
    REASON_FIELD_NOT_MESSAGES,
    REASON_MISSING_PHONE_ID,
    REASON_SCOPE_MISMATCH,
    REASON_UNKNOWN_PHONE_ID,
    REASON_WRONG_PROVIDER,
    emit_branch_decision,
    emit_dispatch_gap,
    emit_raw_inbound,
    is_d360_dispatch_telemetry_enabled,
)


_LOGGER_NAME = "nahla.d360_dispatch_telemetry"


# ════════════════════════════════════════════════════════════════
# Fixtures / helpers
# ════════════════════════════════════════════════════════════════


@pytest.fixture(autouse=True)
def _ensure_default_kill_switch(monkeypatch: pytest.MonkeyPatch):
    """Each test starts with both kill switches unset so behaviour is
    deterministic regardless of environment leak. Tests that need
    a different state set their own values."""
    monkeypatch.delenv("D360_DISPATCH_GAP_TELEMETRY_ENABLED", raising=False)
    monkeypatch.delenv("INBOUND_LIFECYCLE_TELEMETRY_ENABLED", raising=False)
    yield


def _grep(caplog: pytest.LogCaptureFixture, prefix: str) -> List[str]:
    return [
        rec.getMessage()
        for rec in caplog.records
        if rec.name == _LOGGER_NAME and prefix in rec.getMessage()
    ]


# ════════════════════════════════════════════════════════════════
# Architectural invariants
# ════════════════════════════════════════════════════════════════


def test_gap_reason_vocabulary_is_closed_unique_and_lowercase() -> None:
    assert len(ALL_GAP_REASONS) == len(set(ALL_GAP_REASONS))
    for r in ALL_GAP_REASONS:
        assert isinstance(r, str) and r
        assert " " not in r
        assert r == r.lower()
        # Every gap reason must keep the canonical prefix so a single
        # grep ``messages_in_payload_but_`` shows every silent drop.
        assert r.startswith("messages_in_payload_but_")


def test_branch_vocabulary_is_closed_unique_and_lowercase() -> None:
    assert len(ALL_BRANCHES) == len(set(ALL_BRANCHES))
    for b in ALL_BRANCHES:
        assert isinstance(b, str) and b
        assert " " not in b
        assert b == b.lower()


def test_kill_switch_default_is_on() -> None:
    assert is_d360_dispatch_telemetry_enabled() is True


def test_narrow_kill_switch_disables_only_this_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("D360_DISPATCH_GAP_TELEMETRY_ENABLED", "0")
    assert is_d360_dispatch_telemetry_enabled() is False


def test_family_kill_switch_disables_this_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operators who flip ``INBOUND_LIFECYCLE_TELEMETRY_ENABLED=0``
    expect the entire Wave 2.0 telemetry family to mute, not just
    W2.0.1. This guards that contract."""
    monkeypatch.setenv("INBOUND_LIFECYCLE_TELEMETRY_ENABLED", "0")
    assert is_d360_dispatch_telemetry_enabled() is False


def test_emitters_never_raise_on_pathological_inputs() -> None:
    # ``messages`` not a list, ``phone_number_id`` not a string,
    # ``msgs_count`` non-int. None of these may bubble up.
    emit_raw_inbound(
        scope="any", field="messages", phone_number_id=object(),  # type: ignore[arg-type]
        msgs_count="lots",  # type: ignore[arg-type]
        statuses_count=None, echoes_count=-1,  # type: ignore[arg-type]
        messages={"not": "a list"},  # type: ignore[arg-type]
    )
    emit_dispatch_gap(
        reason="anything", scope="", field="", phone_number_id="",
        msgs_count=None,  # type: ignore[arg-type]
        messages=None,
    )
    emit_branch_decision(
        branch=BRANCH_MESSAGES, scope="any", field="messages",
        phone_number_id="",
        messages=[None, "x", {}],  # type: ignore[list-item]
    )


# ════════════════════════════════════════════════════════════════
# [D360_RAW_INBOUND] — line shape + presence
# ════════════════════════════════════════════════════════════════


def test_raw_inbound_emits_one_line_with_all_required_fields(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)
    emit_raw_inbound(
        scope="any",
        field="messages",
        phone_number_id="123456",
        msgs_count=2,
        statuses_count=0,
        echoes_count=0,
        messages=[
            {"id": "wamid.A", "type": "video", "from": "9665552692"},
            {"id": "wamid.B", "type": "video", "from": "9665552692"},
        ],
        has_messages_key=True,
        has_message_echoes_key=False,
        has_statuses_key=False,
        entry_idx=0, change_idx=0,
    )
    lines = _grep(caplog, "[D360_RAW_INBOUND]")
    assert len(lines) == 1
    line = lines[0]
    # Must contain every operator-grepped field token exactly once.
    for token in (
        "scope=any", "field=messages", "phone_id=123456",
        "msgs=2", "statuses=0", "echoes=0",
        "first_msg_type=video", "first_sender_masked=*2692",
        "first_msg_id=wamid.A", "message_ids_tail=wamid.A,wamid.B",
        "has_messages_key=true",
    ):
        assert token in line, f"missing {token!r} in: {line}"


def test_raw_inbound_handles_zero_messages_gracefully(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)
    emit_raw_inbound(
        scope="status", field="account_alerts", phone_number_id="X",
        msgs_count=0, statuses_count=0, echoes_count=0,
        messages=None,
    )
    line = _grep(caplog, "[D360_RAW_INBOUND]")[0]
    assert "msgs=0" in line
    assert "first_msg_type=-" in line
    assert "first_sender_masked=-" in line
    assert "message_ids_tail=-" in line


def test_raw_inbound_kill_switch_off_emits_nothing(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INBOUND_LIFECYCLE_TELEMETRY_ENABLED", "0")
    caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)
    emit_raw_inbound(
        scope="any", field="messages", phone_number_id="X",
        msgs_count=1, statuses_count=0, echoes_count=0,
        messages=[{"id": "wamid.X", "type": "image", "from": "9665552692"}],
    )
    assert _grep(caplog, "[D360_RAW_INBOUND]") == []


# ════════════════════════════════════════════════════════════════
# [D360_DISPATCH_GAP] — only fires when msgs_count > 0
# ════════════════════════════════════════════════════════════════


def test_dispatch_gap_no_op_when_msgs_count_zero(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The gap line is meaningful only when a non-empty messages[]
    was riding the change. Zero messages → no log line, even if the
    branch otherwise dropped the change."""
    caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)
    emit_dispatch_gap(
        reason=REASON_FIELD_NOT_MESSAGES, scope="any",
        field="device_sync", phone_number_id="X",
        msgs_count=0, messages=[],
    )
    assert _grep(caplog, "[D360_DISPATCH_GAP]") == []


def test_dispatch_gap_fires_for_field_not_messages(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)
    emit_dispatch_gap(
        reason=REASON_FIELD_NOT_MESSAGES,
        scope="coexistence", field="smb_message_echoes",
        family="coexistence",
        phone_number_id="42",
        msgs_count=1,
        messages=[{"id": "wamid.S", "type": "document", "from": "9665558626"}],
        matched_tenant_id=33,
        detail="field=smb_message_echoes_with_messages_array",
    )
    line = _grep(caplog, "[D360_DISPATCH_GAP]")[0]
    for token in (
        f"reason={REASON_FIELD_NOT_MESSAGES}",
        "scope=coexistence", "field=smb_message_echoes",
        "family=coexistence",
        "phone_id=42", "msgs=1",
        "first_sender_masked=*8626",
        "first_msg_id=wamid.S",
        "matched_tenant=33",
    ):
        assert token in line


def test_dispatch_gap_fires_for_unknown_phone_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)
    emit_dispatch_gap(
        reason=REASON_UNKNOWN_PHONE_ID, scope="any", field="messages",
        phone_number_id="9999999",
        msgs_count=3,
        messages=[
            {"id": "wamid.1", "type": "image", "from": "9665555699"},
            {"id": "wamid.2", "type": "image", "from": "9665555699"},
            {"id": "wamid.3", "type": "image", "from": "9665555699"},
        ],
    )
    line = _grep(caplog, "[D360_DISPATCH_GAP]")[0]
    assert f"reason={REASON_UNKNOWN_PHONE_ID}" in line
    assert "first_sender_masked=*5699" in line
    assert "msgs=3" in line


def test_dispatch_gap_kill_switch_off_emits_nothing(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("D360_DISPATCH_GAP_TELEMETRY_ENABLED", "0")
    caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)
    emit_dispatch_gap(
        reason=REASON_UNKNOWN_PHONE_ID, scope="any", field="messages",
        phone_number_id="X", msgs_count=1,
        messages=[{"id": "x", "type": "video", "from": "1234"}],
    )
    assert _grep(caplog, "[D360_DISPATCH_GAP]") == []


# ════════════════════════════════════════════════════════════════
# [D360_BRANCH] — decision markers
# ════════════════════════════════════════════════════════════════


def test_branch_decision_messages_marker(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)
    emit_branch_decision(
        branch=BRANCH_MESSAGES, scope="any", field="messages",
        family="channel", phone_number_id="X",
        msgs_count=1, statuses_count=0, echoes_count=0,
        messages=[{"id": "wamid.M", "type": "text", "from": "9665552692"}],
        matched_tenant_id=33,
    )
    line = _grep(caplog, "[D360_BRANCH]")[0]
    assert f"decision={BRANCH_MESSAGES}" in line
    assert "field=messages" in line
    assert "family=channel" in line
    assert "first_sender_masked=*2692" in line
    assert "matched_tenant=33" in line


def test_branch_decision_for_each_known_branch(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)
    for branch in (
        BRANCH_MESSAGES,
        BRANCH_SMB_MESSAGE_ECHOES,
        BRANCH_COEXISTENCE,
        BRANCH_STATUS,
        BRANCH_IGNORED,
    ):
        emit_branch_decision(
            branch=branch, scope="any", field=branch, family="x",
            phone_number_id="P",
        )
    lines = _grep(caplog, "[D360_BRANCH]")
    assert len(lines) == 5
    decisions = [
        line.split("decision=", 1)[1].split(" ", 1)[0]
        for line in lines
    ]
    assert decisions == [
        BRANCH_MESSAGES, BRANCH_SMB_MESSAGE_ECHOES,
        BRANCH_COEXISTENCE, BRANCH_STATUS, BRANCH_IGNORED,
    ]


# ════════════════════════════════════════════════════════════════
# Production scenario coverage — the three real-world cases
# ════════════════════════════════════════════════════════════════


def test_scenario_2692_video_arriving_under_smb_echoes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """*2692 video case: 360dialog mislabels a customer video as
    ``smb_message_echoes`` so the routing skips ``_dispatch_message``
    even though messages[] is non-empty."""
    caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)
    msgs = [{"id": "wamid.V", "type": "video", "from": "9665552692"}]
    emit_raw_inbound(
        scope="any", field="smb_message_echoes",
        phone_number_id="P",
        msgs_count=1, statuses_count=0, echoes_count=1,
        messages=msgs,
        has_messages_key=True, has_message_echoes_key=True,
    )
    emit_branch_decision(
        branch=BRANCH_SMB_MESSAGE_ECHOES, scope="any",
        field="smb_message_echoes", family="coexistence",
        phone_number_id="P", msgs_count=1, echoes_count=1,
        messages=msgs, matched_tenant_id=33,
    )
    emit_dispatch_gap(
        reason=REASON_FIELD_NOT_MESSAGES, scope="any",
        field="smb_message_echoes", family="coexistence",
        phone_number_id="P", msgs_count=1, messages=msgs,
        matched_tenant_id=33,
    )
    raw = _grep(caplog, "[D360_RAW_INBOUND]")[0]
    branch = _grep(caplog, "[D360_BRANCH]")[0]
    gap = _grep(caplog, "[D360_DISPATCH_GAP]")[0]
    # Sanity: a single grep on `*2692` returns all three lines.
    assert "*2692" in raw and "*2692" in branch and "*2692" in gap
    assert "field=smb_message_echoes" in raw
    assert f"decision={BRANCH_SMB_MESSAGE_ECHOES}" in branch
    assert f"reason={REASON_FIELD_NOT_MESSAGES}" in gap


def test_scenario_8626_document_unknown_phone_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """*8626 document case: phone_number_id arrives but no
    WhatsAppConnection row matches (channel re-paired with new
    phone_number_id; old row still stored)."""
    caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)
    msgs = [{"id": "wamid.D", "type": "document", "from": "9665558626"}]
    emit_raw_inbound(
        scope="any", field="messages", phone_number_id="OLD_PID",
        msgs_count=1, statuses_count=0, echoes_count=0,
        messages=msgs, has_messages_key=True,
    )
    emit_dispatch_gap(
        reason=REASON_UNKNOWN_PHONE_ID, scope="any", field="messages",
        phone_number_id="OLD_PID", msgs_count=1, messages=msgs,
    )
    raw = _grep(caplog, "[D360_RAW_INBOUND]")[0]
    gap = _grep(caplog, "[D360_DISPATCH_GAP]")[0]
    assert "*8626" in raw and "*8626" in gap
    assert f"reason={REASON_UNKNOWN_PHONE_ID}" in gap
    assert "phone_id=OLD_PID" in gap


def test_scenario_5699_document_scope_mismatch(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """*5699 document case: messages arriving on a status-scoped
    URL (merchant misconfigured the 360dialog dashboard)."""
    caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)
    msgs = [{"id": "wamid.X", "type": "document", "from": "9665555699"}]
    emit_raw_inbound(
        scope="status", field="messages", phone_number_id="P",
        msgs_count=1, statuses_count=0, echoes_count=0,
        messages=msgs, has_messages_key=True,
    )
    emit_dispatch_gap(
        reason=REASON_SCOPE_MISMATCH, scope="status", field="messages",
        family="channel", phone_number_id="P",
        msgs_count=1, messages=msgs, matched_tenant_id=33,
    )
    raw = _grep(caplog, "[D360_RAW_INBOUND]")[0]
    gap = _grep(caplog, "[D360_DISPATCH_GAP]")[0]
    assert "*5699" in raw and "*5699" in gap
    assert f"reason={REASON_SCOPE_MISMATCH}" in gap
    assert "scope=status" in gap


def test_kill_switch_simultaneously_silences_all_three_emitters(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INBOUND_LIFECYCLE_TELEMETRY_ENABLED", "0")
    caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)
    msgs = [{"id": "x", "type": "video", "from": "9665552692"}]
    emit_raw_inbound(
        scope="any", field="messages", phone_number_id="X",
        msgs_count=1, statuses_count=0, echoes_count=0, messages=msgs,
    )
    emit_branch_decision(
        branch=BRANCH_MESSAGES, scope="any", field="messages",
        phone_number_id="X", msgs_count=1, messages=msgs,
    )
    emit_dispatch_gap(
        reason=REASON_FIELD_NOT_MESSAGES, scope="any",
        field="device_sync", phone_number_id="X",
        msgs_count=1, messages=msgs,
    )
    assert _grep(caplog, "[D360_RAW_INBOUND]") == []
    assert _grep(caplog, "[D360_BRANCH]") == []
    assert _grep(caplog, "[D360_DISPATCH_GAP]") == []


# ════════════════════════════════════════════════════════════════
# Field redaction / safety
# ════════════════════════════════════════════════════════════════


def test_no_full_phone_number_ever_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The masked sender contract is the most important PII rule for
    these probes. A full Saudi number must never reach the log."""
    caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)
    full = "966555512345"
    msgs = [{"id": "wamid.x", "type": "image", "from": full}]
    emit_raw_inbound(
        scope="any", field="messages", phone_number_id="X",
        msgs_count=1, statuses_count=0, echoes_count=0,
        messages=msgs,
    )
    emit_dispatch_gap(
        reason=REASON_FIELD_NOT_MESSAGES, scope="any",
        field="device_sync", phone_number_id="X",
        msgs_count=1, messages=msgs,
    )
    emit_branch_decision(
        branch=BRANCH_IGNORED, scope="any", field="device_sync",
        phone_number_id="X", msgs_count=1, messages=msgs,
    )
    for rec in caplog.records:
        if rec.name == _LOGGER_NAME:
            assert full not in rec.getMessage()
