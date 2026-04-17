"""
core/calendar_events.py
────────────────────────
Saudi-market promotional calendar consumed by the growth engine.

Why this lives here
───────────────────
The `seasonal_offer` SmartAutomation needs a deterministic answer to the
question "is one of the holidays I care about happening tomorrow?". Letting
each merchant configure their own holiday list would defeat the value of a
shared autopilot — instead Nahla ships a curated list of the dates Saudi
e-commerce stores actually run promos on, and the merchant just toggles the
automation on or off and (optionally) sets the discount.

Date model
──────────
Holidays come in two shapes:

  • Gregorian-fixed (Sep 23 national day, Feb 22 founding day, the last Friday
    of November for white_friday). These are reproducible from year + month
    formulae and are computed exactly.

  • Hijri-derived (Ramadan begins, Eid al-Fitr, Eid al-Adha). The Hijri →
    Gregorian conversion is published every year by the Umm al-Qura calendar
    and depends on lunar observation — there is no formula. We hard-code the
    converted Gregorian dates for the next several years from the official
    Umm al-Qura tables and revisit the table when needed. If the table runs
    out for a given year, those events are skipped (logged once) rather than
    guessed.

The emitter (`automation_emitters.scan_calendar_events`) calls
`events_for_date(target_date)` once per cycle and fires SEASONAL_EVENT_DUE
for each entry returned, with the entry's display name and slug in the
event payload.
"""
from __future__ import annotations

import calendar as _calendar
import logging
from dataclasses import dataclass
from datetime import date as _date
from typing import Dict, List, Tuple

logger = logging.getLogger("nahla.calendar_events")


@dataclass(frozen=True)
class CalendarEvent:
    slug: str         # canonical id, e.g. "national_day"
    name_ar: str      # what we put in the WhatsApp template's {{occasion_name}}
    name_en: str
    category: str     # "national" | "religious" | "shopping"


# ── Catalogue ────────────────────────────────────────────────────────────────

NATIONAL_DAY      = CalendarEvent("national_day",  "اليوم الوطني السعودي", "Saudi National Day",        "national")
FOUNDING_DAY      = CalendarEvent("founding_day",  "يوم التأسيس",         "Saudi Founding Day",        "national")
WHITE_FRIDAY      = CalendarEvent("white_friday",  "الجمعة البيضاء",      "White Friday",              "shopping")
RAMADAN_START     = CalendarEvent("ramadan_start", "بداية رمضان",         "Start of Ramadan",          "religious")
EID_AL_FITR       = CalendarEvent("eid_al_fitr",   "عيد الفطر",           "Eid al-Fitr",               "religious")
EID_AL_ADHA       = CalendarEvent("eid_al_adha",   "عيد الأضحى",          "Eid al-Adha",               "religious")


# ── Hijri-derived dates (Umm al-Qura, Gregorian conversion) ──────────────────
#
# Sourced from the official Saudi Umm al-Qura calendar. Update yearly.
# `(year, month, day)` for the GREGORIAN date these religious events fall on.
HIJRI_EVENT_DATES: Dict[int, Dict[str, Tuple[int, int, int]]] = {
    2026: {
        # 1 Ramadan 1447 = Feb 17, 2026 ; 1 Shawwal = Mar 19, 2026 ; 10 Dhul Hijjah = May 26, 2026
        "ramadan_start": (2026, 2, 17),
        "eid_al_fitr":   (2026, 3, 19),
        "eid_al_adha":   (2026, 5, 26),
    },
    2027: {
        "ramadan_start": (2027, 2, 7),
        "eid_al_fitr":   (2027, 3, 9),
        "eid_al_adha":   (2027, 5, 16),
    },
    2028: {
        "ramadan_start": (2028, 1, 27),
        "eid_al_fitr":   (2028, 2, 26),
        "eid_al_adha":   (2028, 5, 5),
    },
}


def _white_friday_for(year: int) -> _date:
    """Last Friday of November for the given year (Saudi 'الجمعة البيضاء')."""
    last_day = _calendar.monthrange(year, 11)[1]
    d = _date(year, 11, last_day)
    while d.weekday() != _calendar.FRIDAY:  # Mon=0 ... Fri=4
        d = d.replace(day=d.day - 1)
    return d


def events_for_date(target: _date) -> List[CalendarEvent]:
    """
    Return every CalendarEvent whose published date matches `target`.

    Used by the emitter to decide what to fire one day before. Always
    returns a list (possibly empty); never raises. Unknown future years for
    Hijri events are skipped silently — the emitter's scheduler will just
    not produce a SEASONAL_EVENT_DUE for them until the table is updated.
    """
    matches: List[CalendarEvent] = []

    if target.month == 9 and target.day == 23:
        matches.append(NATIONAL_DAY)
    if target.month == 2 and target.day == 22:
        matches.append(FOUNDING_DAY)
    if target == _white_friday_for(target.year):
        matches.append(WHITE_FRIDAY)

    by_year = HIJRI_EVENT_DATES.get(target.year) or {}
    for slug, (yy, mm, dd) in by_year.items():
        if (target.year, target.month, target.day) != (yy, mm, dd):
            continue
        if slug == "ramadan_start":
            matches.append(RAMADAN_START)
        elif slug == "eid_al_fitr":
            matches.append(EID_AL_FITR)
        elif slug == "eid_al_adha":
            matches.append(EID_AL_ADHA)

    return matches


def event_for_slug(slug: str) -> CalendarEvent | None:
    """Lookup helper used by tests + the API layer."""
    for ev in (NATIONAL_DAY, FOUNDING_DAY, WHITE_FRIDAY, RAMADAN_START, EID_AL_FITR, EID_AL_ADHA):
        if ev.slug == slug:
            return ev
    return None


def next_occurrence_for(slug: str, *, today: _date | None = None) -> _date | None:
    """
    Return the next Gregorian date this occasion will fall on, looking
    forward from `today` (default: today). Returns None if the slug is
    unknown or, for Hijri-derived events, the lookup table has no
    matching entry within the next 4 years.

    Used by the dashboard's Seasonal Calendar panel so the merchant
    sees "next time: 22 February 2027" without the frontend having to
    duplicate the holiday formulae.
    """
    today = today or _date.today()

    # Gregorian-fixed: founding day (Feb 22), national day (Sep 23).
    if slug == "founding_day":
        return _next_fixed(today, month=2, day=22)
    if slug == "national_day":
        return _next_fixed(today, month=9, day=23)
    if slug == "white_friday":
        # Last Friday of November — try this year first, else next.
        this_year = _white_friday_for(today.year)
        if this_year >= today:
            return this_year
        return _white_friday_for(today.year + 1)

    # Hijri-derived: walk the lookup table forward until we find a date
    # >= today. If we exhaust the table, return None — the caller will
    # render a "—" rather than fabricate a date.
    if slug in {"ramadan_start", "eid_al_fitr", "eid_al_adha"}:
        for year in sorted(HIJRI_EVENT_DATES.keys()):
            entry = HIJRI_EVENT_DATES[year].get(slug)
            if entry is None:
                continue
            yy, mm, dd = entry
            candidate = _date(yy, mm, dd)
            if candidate >= today:
                return candidate
        return None

    return None


def _next_fixed(today: _date, *, month: int, day: int) -> _date:
    """Next occurrence of a fixed (month, day) anchor, including today."""
    candidate = _date(today.year, month, day)
    if candidate >= today:
        return candidate
    return _date(today.year + 1, month, day)
