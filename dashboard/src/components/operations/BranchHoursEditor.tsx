import { Copy, Plus, Trash2 } from 'lucide-react'
import {
  cloneSchedules,
  type DayKey,
  type DaySchedule,
  type HoursPeriod,
} from '../../lib/branchHours'

interface BranchHoursEditorProps {
  value: DaySchedule[]
  onChange: (value: DaySchedule[]) => void
}

function updateDay(
  schedules: DaySchedule[],
  key: DayKey,
  patch: Partial<DaySchedule>,
): DaySchedule[] {
  return schedules.map((d) => (d.key === key ? { ...d, ...patch } : d))
}

function defaultPeriod(): HoursPeriod {
  return { open: '09:00', close: '22:00' }
}

export default function BranchHoursEditor({ value, onChange }: BranchHoursEditorProps) {
  const set = (next: DaySchedule[]) => onChange(cloneSchedules(next))

  const toggleOpen = (key: DayKey, open: boolean) => {
    set(
      updateDay(value, key, {
        open,
        periods: open ? [defaultPeriod()] : [],
      }),
    )
  }

  const updatePeriod = (
    key: DayKey,
    index: number,
    field: keyof HoursPeriod,
    time: string,
  ) => {
    set(
      value.map((d) => {
        if (d.key !== key) return d
        const periods = d.periods.map((p, i) =>
          i === index ? { ...p, [field]: time } : p,
        )
        return { ...d, periods, open: periods.length > 0 }
      }),
    )
  }

  const addPeriod = (key: DayKey) => {
    set(
      value.map((d) => {
        if (d.key !== key || d.periods.length >= 2) return d
        return {
          ...d,
          open: true,
          periods: [...d.periods, { open: '16:00', close: '22:00' }],
        }
      }),
    )
  }

  const removePeriod = (key: DayKey, index: number) => {
    set(
      value.map((d) => {
        if (d.key !== key) return d
        const periods = d.periods.filter((_, i) => i !== index)
        return { ...d, periods, open: periods.length > 0 }
      }),
    )
  }

  const applySameHoursToAll = () => {
    const source = value.find((d) => d.open && d.periods.length > 0)
    if (!source) return
    const template = source.periods.map((p) => ({ ...p }))
    set(value.map((d) => ({ ...d, open: true, periods: template.map((p) => ({ ...p })) })))
  }

  const setAll24Hours = () => {
    const period: HoursPeriod = { open: '00:00', close: '23:59' }
    set(value.map((d) => ({ ...d, open: true, periods: [{ ...period }] })))
  }

  const closeFriday = () => {
    set(updateDay(value, 'fri', { open: false, periods: [] }))
  }

  const copySaturdayToAll = () => {
    const sat = value.find((d) => d.key === 'sat')
    if (!sat) return
    const template = sat.periods.map((p) => ({ ...p }))
    set(
      value.map((d) => ({
        ...d,
        open: sat.open && template.length > 0,
        periods: template.map((p) => ({ ...p })),
      })),
    )
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-2">
        <button type="button" className="btn-secondary text-xs px-2.5 py-1.5" onClick={applySameHoursToAll}>
          تطبيق نفس الوقت على كل الأيام
        </button>
        <button type="button" className="btn-secondary text-xs px-2.5 py-1.5" onClick={setAll24Hours}>
          مفتوح 24 ساعة
        </button>
        <button type="button" className="btn-secondary text-xs px-2.5 py-1.5" onClick={closeFriday}>
          مغلق الجمعة
        </button>
        <button
          type="button"
          className="btn-secondary text-xs px-2.5 py-1.5 inline-flex items-center gap-1"
          onClick={copySaturdayToAll}
        >
          <Copy className="w-3 h-3" />
          نسخ ساعات السبت لبقية الأيام
        </button>
      </div>

      <div className="overflow-x-auto rounded-xl border border-slate-200">
        <table className="w-full text-sm min-w-[520px]">
          <thead>
            <tr className="bg-slate-50 text-slate-500 border-b border-slate-200">
              <th className="text-right p-3 font-medium w-24">اليوم</th>
              <th className="text-right p-3 font-medium w-28">الحالة</th>
              <th className="text-right p-3 font-medium">من</th>
              <th className="text-right p-3 font-medium">إلى</th>
              <th className="text-right p-3 font-medium w-28" />
            </tr>
          </thead>
          <tbody>
            {value.map((day) =>
              !day.open || day.periods.length === 0 ? (
                <tr key={day.key} className="border-b border-slate-100 last:border-0">
                  <td className="p-3 font-medium text-slate-800">{day.label}</td>
                  <td className="p-3">
                    <label className="inline-flex items-center gap-2 cursor-pointer">
                      <input
                        type="checkbox"
                        className="rounded border-slate-300 text-brand-600 focus:ring-brand-500"
                        checked={false}
                        onChange={() => toggleOpen(day.key, true)}
                      />
                      <span className="text-slate-600">مغلق</span>
                    </label>
                  </td>
                  <td className="p-3 text-slate-400" colSpan={3}>
                    —
                  </td>
                </tr>
              ) : (
                day.periods.map((period, idx) => (
                  <tr key={`${day.key}-${idx}`} className="border-b border-slate-100 last:border-0">
                    {idx === 0 && (
                      <td className="p-3 font-medium text-slate-800 align-top" rowSpan={day.periods.length}>
                        {day.label}
                      </td>
                    )}
                    {idx === 0 && (
                      <td className="p-3 align-top" rowSpan={day.periods.length}>
                        <label className="inline-flex items-center gap-2 cursor-pointer">
                          <input
                            type="checkbox"
                            className="rounded border-slate-300 text-brand-600 focus:ring-brand-500"
                            checked
                            onChange={(e) => toggleOpen(day.key, e.target.checked)}
                          />
                          <span className="text-slate-600">مفتوح</span>
                        </label>
                      </td>
                    )}
                    <td className="p-3">
                      <input
                        type="time"
                        dir="ltr"
                        className="input py-1.5 text-xs w-[7.5rem]"
                        value={period.open}
                        onChange={(e) => updatePeriod(day.key, idx, 'open', e.target.value)}
                      />
                    </td>
                    <td className="p-3">
                      <input
                        type="time"
                        dir="ltr"
                        className="input py-1.5 text-xs w-[7.5rem]"
                        value={period.close}
                        onChange={(e) => updatePeriod(day.key, idx, 'close', e.target.value)}
                      />
                    </td>
                    <td className="p-3">
                      {idx === 0 && day.periods.length < 2 && (
                        <button
                          type="button"
                          className="text-xs text-brand-600 hover:underline inline-flex items-center gap-1"
                          onClick={() => addPeriod(day.key)}
                        >
                          <Plus className="w-3 h-3" />
                          فترة ثانية
                        </button>
                      )}
                      {idx > 0 && (
                        <button
                          type="button"
                          className="p-1 text-red-500 hover:bg-red-50 rounded"
                          title="حذف الفترة"
                          onClick={() => removePeriod(day.key, idx)}
                        >
                          <Trash2 className="w-3.5 h-3.5" />
                        </button>
                      )}
                    </td>
                  </tr>
                ))
              ),
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}
