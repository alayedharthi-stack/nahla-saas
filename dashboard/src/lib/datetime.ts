/**
 * dashboard/src/lib/datetime.ts
 * ─────────────────────────────
 * Centralised, timezone-locked date/time formatting for the merchant
 * dashboard.
 *
 * Why this exists
 * ───────────────
 * Every merchant operates in **Asia/Riyadh** (UTC+3). The backend stores
 * timestamps in UTC and serialises them either as ``...+00:00`` (when an
 * offset is preserved) or — historically — as a naive ``YYYY-MM-DDTHH:MM:SS``
 * string. Both shapes are valid input to ``new Date(...)``, but the previous
 * per-page formatters called ``Intl.DateTimeFormat('ar-SA', ...)`` **without**
 * a ``timeZone`` option, so the rendered string was the **browser's local
 * time** — which is wrong for any merchant viewing the dashboard from outside
 * Saudi Arabia (e.g. a developer browser in UTC+0 saw orders dated tomorrow
 * because UTC 21:50 became 21:50 local instead of 00:50 next-day Riyadh).
 *
 * All timestamp display in the merchant UI MUST go through this module so a
 * future timezone-policy change is one edit, not thirty.
 */

const RIYADH_TZ = 'Asia/Riyadh' as const;

const PLACEHOLDER = '—';

/**
 * Parse a backend timestamp into a ``Date`` while being lenient about the
 * three shapes we see in practice:
 *
 *   - ``2026-04-20T18:50:00+00:00``      (aware UTC, ``orders.py``)
 *   - ``2026-04-20T18:50:00Z``           (aware UTC alt notation)
 *   - ``2026-04-20T18:50:00``            (naive — must be treated as UTC,
 *                                         since every backend writer uses
 *                                         ``datetime.utcnow``)
 *
 * Plain ``new Date('2026-04-20T18:50:00')`` interprets the third shape as
 * **local browser time**, which is exactly the bug we're avoiding.
 */
function parseToUtc(value: string | number | Date | null | undefined): Date | null {
  if (value == null || value === '') return null;
  if (value instanceof Date) {
    return Number.isNaN(value.getTime()) ? null : value;
  }
  if (typeof value === 'number') {
    const d = new Date(value);
    return Number.isNaN(d.getTime()) ? null : d;
  }
  const trimmed = String(value).trim();
  if (!trimmed) return null;

  // Already has an explicit offset (Z or ±HH:MM) — let the engine parse it.
  const hasOffset = /[zZ]$|[+\-]\d{2}:?\d{2}$/.test(trimmed);
  const isoLike = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/.test(trimmed);

  let candidate: string;
  if (hasOffset) {
    candidate = trimmed.replace(' ', 'T');
  } else if (isoLike) {
    // Naive ISO from a backend that defaulted ``DateTime`` columns to
    // ``datetime.utcnow``. Tag it as UTC so the browser does not interpret
    // the wall-clock as local.
    candidate = `${trimmed.replace(' ', 'T')}Z`;
  } else {
    candidate = trimmed;
  }

  const d = new Date(candidate);
  return Number.isNaN(d.getTime()) ? null : d;
}

interface FormatOptions {
  dateStyle?: 'short' | 'medium' | 'long' | 'full';
  timeStyle?: 'short' | 'medium' | 'long' | 'full';
  hour12?: boolean;
}

/**
 * Format ``value`` in **Asia/Riyadh** time, in Arabic locale, with the
 * given ``dateStyle`` / ``timeStyle``. Defaults to short date + short time
 * (e.g. ``2026/04/20 09:50 م``).
 *
 * Always returns a printable string — never throws — so it is safe to drop
 * straight into JSX cells.
 */
export function formatRiyadh(
  value: string | number | Date | null | undefined,
  opts: FormatOptions = {},
): string {
  const d = parseToUtc(value);
  if (!d) return PLACEHOLDER;
  try {
    return new Intl.DateTimeFormat('ar-SA', {
      dateStyle: opts.dateStyle ?? 'short',
      timeStyle: opts.timeStyle ?? 'short',
      hour12: opts.hour12 ?? true,
      timeZone: RIYADH_TZ,
    }).format(d);
  } catch {
    return d.toISOString();
  }
}

/** Date only, short style — e.g. ``2026/04/20``. */
export function formatRiyadhDate(value: string | number | Date | null | undefined): string {
  const d = parseToUtc(value);
  if (!d) return PLACEHOLDER;
  try {
    return new Intl.DateTimeFormat('ar-SA', {
      dateStyle: 'short',
      timeZone: RIYADH_TZ,
    }).format(d);
  } catch {
    return d.toISOString().slice(0, 10);
  }
}

/** Time only, short style — e.g. ``09:50 م``. */
export function formatRiyadhTime(value: string | number | Date | null | undefined): string {
  const d = parseToUtc(value);
  if (!d) return PLACEHOLDER;
  try {
    return new Intl.DateTimeFormat('ar-SA', {
      timeStyle: 'short',
      hour12: true,
      timeZone: RIYADH_TZ,
    }).format(d);
  } catch {
    return d.toISOString().slice(11, 16);
  }
}

/**
 * Compact "X minutes/hours/days ago" string in Arabic, anchored to **now**.
 * Used in queue rows where absolute timestamps are noisy (e.g. "آخر تذكير
 * منذ 12 د"). Falls back to ``formatRiyadh`` for anything older than a week.
 */
export function formatRelativeRiyadh(
  value: string | number | Date | null | undefined,
  now: Date = new Date(),
): string {
  const d = parseToUtc(value);
  if (!d) return PLACEHOLDER;
  const diffMs = now.getTime() - d.getTime();
  if (diffMs < 0) {
    // Future timestamp (e.g. next scheduled reminder) — show absolute.
    return formatRiyadh(d);
  }
  const sec = Math.floor(diffMs / 1000);
  if (sec < 60) return 'الآن';
  const min = Math.floor(sec / 60);
  if (min < 60) return `منذ ${min} د`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `منذ ${hr} س`;
  const day = Math.floor(hr / 24);
  if (day < 7) return `منذ ${day} ي`;
  return formatRiyadh(d);
}

/** Constants exported for tests / debug overlays. */
export const __riyadh_internals__ = { RIYADH_TZ, parseToUtc };
