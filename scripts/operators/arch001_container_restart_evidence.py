"""ARCH-001 container restart evidence collection and parsing (governance-only).

Collects in-container PID1 identity from ``/proc/1/stat`` field 22 (starttime
ticks) and ``/proc/1/cmdline``. Hostname is never used as restart proof.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

PROC1_STAT_PATH = Path("/proc/1/stat")
PROC1_CMDLINE_PATH = Path("/proc/1/cmdline")
PROC1_STARTTIME_FIELD_INDEX = 19  # 0-based index after pid + comm in /proc/[pid]/stat

RESTART_PROOF_CONTAINER_ID_CHANGE = "container_id_change"
RESTART_PROOF_PID1_STARTTIME_CHANGE = "pid1_starttime_change"
RESTART_COLLECTION_METHOD_PROC1_STAT_FIELD22 = "in_container_proc1_stat_field22"

RESTART_EVIDENCE_SNAPSHOT_KEYS: frozenset[str] = frozenset(
    {
        "collected_at_utc",
        "pid1_starttime_ticks",
        "pid1_cmdline",
        "identity_binding",
    }
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_proc_stat_starttime_ticks(stat_line: str) -> int | None:
    """Parse starttime (field 22) from a single ``/proc/[pid]/stat`` line."""
    raw = str(stat_line or "").strip()
    if not raw:
        return None
    closing = raw.rfind(")")
    if closing == -1:
        return None
    tail = raw[closing + 1 :].strip()
    if not tail:
        return None
    fields = tail.split()
    if len(fields) <= PROC1_STARTTIME_FIELD_INDEX:
        return None
    token = fields[PROC1_STARTTIME_FIELD_INDEX].strip()
    if not token.lstrip("-").isdigit():
        return None
    value = int(token)
    if value < 0:
        return None
    return value


def read_proc1_starttime_ticks(*, stat_path: Path = PROC1_STAT_PATH) -> int | None:
    try:
        stat_line = stat_path.read_text(encoding="utf-8", errors="replace").splitlines()[0]
    except (OSError, IndexError):
        return None
    return parse_proc_stat_starttime_ticks(stat_line)


def read_proc1_cmdline(*, cmdline_path: Path = PROC1_CMDLINE_PATH) -> str | None:
    try:
        raw = cmdline_path.read_bytes()
    except OSError:
        return None
    if not raw:
        return None
    text = raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
    return text or None


def collect_pid1_restart_snapshot(
    *,
    identity_binding: Mapping[str, str],
    collected_at_utc: str | None = None,
    stat_path: Path = PROC1_STAT_PATH,
    cmdline_path: Path = PROC1_CMDLINE_PATH,
) -> dict[str, Any]:
    """Collect a non-self-asserting PID1 snapshot from inside the container."""
    starttime = read_proc1_starttime_ticks(stat_path=stat_path)
    cmdline = read_proc1_cmdline(cmdline_path=cmdline_path)
    return {
        "collected_at_utc": collected_at_utc or _utc_now_iso(),
        "pid1_starttime_ticks": starttime,
        "pid1_cmdline": cmdline,
        "identity_binding": {key: str(value) for key, value in identity_binding.items()},
    }


def build_pid1_restart_evidence(
    *,
    pre_restart: Mapping[str, Any],
    post_restart: Mapping[str, Any],
    restart_completed_at_utc: str,
) -> dict[str, Any]:
    return {
        "proof_mode": RESTART_PROOF_PID1_STARTTIME_CHANGE,
        "collection_method": RESTART_COLLECTION_METHOD_PROC1_STAT_FIELD22,
        "pre_restart": dict(pre_restart),
        "post_restart": dict(post_restart),
        "restart_completed_at_utc": restart_completed_at_utc,
    }


def build_container_id_restart_evidence(
    *,
    prior_container_id: str,
    new_container_id: str,
    restart_completed_at_utc: str,
) -> dict[str, Any]:
    return {
        "proof_mode": RESTART_PROOF_CONTAINER_ID_CHANGE,
        "prior_container_id": prior_container_id,
        "new_container_id": new_container_id,
        "restart_completed_at_utc": restart_completed_at_utc,
    }


__all__ = [
    "PROC1_CMDLINE_PATH",
    "PROC1_STARTTIME_FIELD_INDEX",
    "PROC1_STAT_PATH",
    "RESTART_COLLECTION_METHOD_PROC1_STAT_FIELD22",
    "RESTART_EVIDENCE_SNAPSHOT_KEYS",
    "RESTART_PROOF_CONTAINER_ID_CHANGE",
    "RESTART_PROOF_PID1_STARTTIME_CHANGE",
    "build_container_id_restart_evidence",
    "build_pid1_restart_evidence",
    "collect_pid1_restart_snapshot",
    "parse_proc_stat_starttime_ticks",
    "read_proc1_cmdline",
    "read_proc1_starttime_ticks",
]
