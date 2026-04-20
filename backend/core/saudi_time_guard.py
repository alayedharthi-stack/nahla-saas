"""
core/saudi_time_guard.py
─────────────────────────
Quiet-hours guard for Saudi Arabia.

Saudi shoppers do not want a WhatsApp ping at 3am — and Meta will happily
deliver one at full marketing-conversation cost. This guard centralises
the rule:

    Any send scheduled between 00:00 → 08:00 Asia/Riyadh is shifted to
    08:30 Asia/Riyadh on the same day. Sends outside that window are
    returned untouched.

The function is pure (no I/O, no DB) so it can be exercised from unit
tests and reused by every emitter / engine path that decides "is now a
good time to send?". The automation engine calls it inside the per-step
delay check so a follow-up that becomes due at 02:00 sleeps until 08:30
without any extra plumbing in the emitters.

Both the input and the output are timezone-aware datetimes when the
caller passes one in; if the caller passes a naive datetime we treat it
as UTC (the convention used everywhere else in the engine).
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Tuple

try:                                            # Python ≥ 3.9 stdlib
    from zoneinfo import ZoneInfo
    _SAUDI_TZ = ZoneInfo("Asia/Riyadh")
except Exception:                               # pragma: no cover — fallback
    _SAUDI_TZ = timezone(timedelta(hours=3))


# Inclusive start, exclusive end of the quiet window in Saudi local time.
QUIET_START_HOUR = 0          # 00:00
QUIET_END_HOUR   = 8          # 08:00  (sends due 00:00–07:59 deferred)
RESUME_HOUR      = 8
RESUME_MINUTE    = 30         # 08:30  (the actual replay time)


def _to_saudi(dt: datetime) -> datetime:
    """Coerce dt into Asia/Riyadh. Naive inputs are treated as UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_SAUDI_TZ)


def is_inside_quiet_hours(dt: datetime) -> bool:
    """Return True iff `dt` (any tz) lands in the Saudi quiet window."""
    saudi = _to_saudi(dt)
    return QUIET_START_HOUR <= saudi.hour < QUIET_END_HOUR


def adjust_for_saudi_sleep_window(send_at: datetime) -> datetime:
    """
    Defer a send scheduled inside the Saudi quiet window to 08:30 Saudi
    time on the same calendar day.

    Returns the input unchanged when it falls outside the window.

    The returned datetime preserves the input's timezone awareness:
      • aware in → aware out (in the same tz as the input)
      • naive in → naive out, treated as UTC throughout
    """
    if not is_inside_quiet_hours(send_at):
        return send_at

    saudi = _to_saudi(send_at)
    resumed = saudi.replace(
        hour=RESUME_HOUR, minute=RESUME_MINUTE,
        second=0, microsecond=0,
    )
    if send_at.tzinfo is None:
        return resumed.astimezone(timezone.utc).replace(tzinfo=None)
    return resumed.astimezone(send_at.tzinfo)


def quiet_window_for_day(day: datetime) -> Tuple[datetime, datetime]:
    """
    Return `(start, end)` of the quiet window for the Saudi calendar day
    that contains `day`. Useful for tests and dashboard tooltips that want
    to render "Quiet hours: 00:00 → 08:00 KSA".
    """
    saudi = _to_saudi(day)
    start = saudi.replace(
        hour=QUIET_START_HOUR, minute=0, second=0, microsecond=0,
    )
    end = saudi.replace(
        hour=QUIET_END_HOUR, minute=0, second=0, microsecond=0,
    )
    return start, end
