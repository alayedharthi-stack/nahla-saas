/** Branch business hours — UI model ↔ hours_json (Operations Center). */

export type DayKey = 'sat' | 'sun' | 'mon' | 'tue' | 'wed' | 'thu' | 'fri'

export type HoursPeriod = { open: string; close: string }

export type DaySchedule = {
  key: DayKey
  label: string
  open: boolean
  periods: HoursPeriod[]
}

export type HoursJson = Partial<Record<DayKey, HoursPeriod[]>>

const DAY_DEFS: { key: DayKey; label: string }[] = [
  { key: 'sat', label: 'السبت' },
  { key: 'sun', label: 'الأحد' },
  { key: 'mon', label: 'الاثنين' },
  { key: 'tue', label: 'الثلاثاء' },
  { key: 'wed', label: 'الأربعاء' },
  { key: 'thu', label: 'الخميس' },
  { key: 'fri', label: 'الجمعة' },
]

const CLOSED_TOKENS = new Set(['closed', 'close', 'off', 'مغلق', 'مقفل'])

export function normalizeTime(part: string): string {
  const t = part.trim()
  if (!t) return '09:00'
  if (/^\d{1,2}:\d{2}$/.test(t)) {
    const [h, m] = t.split(':')
    return `${h.padStart(2, '0')}:${m}`
  }
  if (/^\d{1,2}$/.test(t)) {
    return `${t.padStart(2, '0')}:00`
  }
  return t
}

function parseLegacyString(raw: string): HoursPeriod[] {
  const s = raw.trim()
  if (!s || CLOSED_TOKENS.has(s.toLowerCase())) return []

  const range = s.match(/^(\d{1,2}(?::\d{2})?)\s*-\s*(\d{1,2}(?::\d{2})?)$/)
  if (range) {
    return [{ open: normalizeTime(range[1]), close: normalizeTime(range[2]) }]
  }
  return [{ open: '09:00', close: '22:00' }]
}

function parseDayValue(val: unknown): HoursPeriod[] {
  if (val == null) return []
  if (Array.isArray(val)) {
    return val
      .map((item) => {
        if (typeof item !== 'object' || item == null) return null
        const row = item as Record<string, unknown>
        const open = normalizeTime(String(row.open ?? ''))
        const close = normalizeTime(String(row.close ?? ''))
        if (!open || !close) return null
        return { open, close }
      })
      .filter((p): p is HoursPeriod => p != null)
  }
  if (typeof val === 'string') return parseLegacyString(val)
  return []
}

export function defaultDaySchedules(): DaySchedule[] {
  return DAY_DEFS.map(({ key, label }) => ({
    key,
    label,
    open: key !== 'fri',
    periods:
      key === 'fri'
        ? []
        : [{ open: '09:00', close: key === 'thu' ? '23:00' : '22:00' }],
  }))
}

/** Parse stored hours_json (legacy strings + structured arrays). */
export function parseHoursJson(
  raw: Record<string, unknown> | null | undefined,
): DaySchedule[] {
  if (!raw || Object.keys(raw).length === 0) {
    return defaultDaySchedules()
  }

  return DAY_DEFS.map(({ key, label }) => {
    const periods = parseDayValue(raw[key])
    const open = periods.length > 0
    return { key, label, open, periods: open ? periods : [] }
  })
}

/** Serialize UI state → hours_json for API (empty array = closed). */
export function serializeHoursJson(schedules: DaySchedule[]): HoursJson {
  const out: HoursJson = {}
  for (const day of schedules) {
    out[day.key] = day.open && day.periods.length > 0 ? day.periods : []
  }
  return out
}

export function cloneSchedules(schedules: DaySchedule[]): DaySchedule[] {
  return schedules.map((d) => ({
    ...d,
    periods: d.periods.map((p) => ({ ...p })),
  }))
}
