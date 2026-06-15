import { useCallback, useEffect, useState } from 'react'
import { Plus, Trash2 } from 'lucide-react'
import {
  operationsCenterApi,
  type ArrivalKeyword,
  type ArrivalKeywordInput,
  type MerchantBranch,
  type TriggerPreviewAction,
  type TriggerPreviewResult,
} from '../../api/operationsCenter'

const LOCATION_MODES = [
  { value: 'location_only', label: 'إرسال الموقع فقط' },
  { value: 'location_plus_reception', label: 'إرسال الموقع + جهة الاستقبال' },
  { value: 'location_plus_instructions', label: 'إرسال الموقع + تعليمات' },
] as const

const ARRIVAL_MODES = [
  { value: 'reception_only', label: 'جهة الاستقبال عند الوصول المؤكد' },
  { value: 'location_and_reception', label: 'ترحيب + إعادة إرسال الموقع' },
  { value: 'ask_branch_first', label: 'السؤال عن الفرع عند تعدد الفروع' },
] as const

const TRIGGER_LABELS: Record<string, string> = {
  location_request: 'طلب موقع',
  arrival_soft: 'وصول عادي',
  arrival_confirmed: 'وصول مؤكد',
  no_response: 'عدم استجابة',
}

const PRESET_MESSAGES = [
  'وين موقعكم؟',
  'أنا في الطريق',
  'وصلت',
  'عند البوابة',
  'ما يرد',
]

type Props = {
  branchId: number
  branch: MerchantBranch | null
  onBranchUpdated: () => void
}

export default function BranchArrivalRulesPanel({ branchId, branch, onBranchUpdated }: Props) {
  const [keywords, setKeywords] = useState<ArrivalKeyword[]>([])
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  const [locationMode, setLocationMode] = useState('location_only')
  const [arrivalMode, setArrivalMode] = useState('reception_only')
  const [instructions, setInstructions] = useState('')
  const [previewMsg, setPreviewMsg] = useState('وين موقعكم؟')
  const [preview, setPreview] = useState<TriggerPreviewResult | null>(null)
  const [newPhrase, setNewPhrase] = useState('')
  const [newTrigger, setNewTrigger] = useState('arrival_confirmed')

  const load = useCallback(async () => {
    setError('')
    try {
      const res = await operationsCenterApi.listArrivalKeywords(branchId)
      setKeywords(res.keywords || [])
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'تعذّر تحميل الكلمات')
    }
  }, [branchId])

  useEffect(() => { load() }, [load])

  useEffect(() => {
    if (!branch) return
    setLocationMode(branch.location_response_mode || 'location_only')
    setArrivalMode(branch.arrival_response_mode || 'reception_only')
    setInstructions(branch.location_instructions_text || '')
  }, [branch])

  const saveModes = async () => {
    setSaving(true)
    setError('')
    try {
      await operationsCenterApi.updateBranch(branchId, {
        location_response_mode: locationMode,
        arrival_response_mode: arrivalMode,
        location_instructions_text: instructions || undefined,
      })
      onBranchUpdated()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'تعذّر حفظ الإعدادات')
    } finally {
      setSaving(false)
    }
  }

  const addKeyword = async () => {
    if (!newPhrase.trim()) return
    setError('')
    try {
      const body: ArrivalKeywordInput = {
        phrase: newPhrase.trim(),
        trigger_type: newTrigger,
        is_active: true,
      }
      await operationsCenterApi.createArrivalKeyword(branchId, body)
      setNewPhrase('')
      await load()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'تعذّر إضافة العبارة')
    }
  }

  const toggleKeyword = async (kw: ArrivalKeyword) => {
    await operationsCenterApi.updateArrivalKeyword(branchId, kw.id, { is_active: !kw.is_active })
    await load()
  }

  const deleteKeyword = async (kw: ArrivalKeyword) => {
    await operationsCenterApi.deleteArrivalKeyword(branchId, kw.id)
    await load()
  }

  const runPreview = async (msg?: string) => {
    const message = (msg ?? previewMsg).trim()
    if (!message) return
    setPreviewMsg(message)
    try {
      const res = await operationsCenterApi.previewTrigger(branchId, message)
      setPreview(res)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'تعذّر المعاينة')
    }
  }

  return (
    <div className="space-y-6">
      {error && (
        <p className="text-sm text-red-600 bg-red-50 border border-red-100 rounded-lg px-3 py-2">{error}</p>
      )}

      <section className="card p-5 space-y-4">
        <h3 className="text-sm font-semibold text-slate-900">عند طلب الموقع</h3>
        <div className="space-y-2">
          {LOCATION_MODES.map(opt => (
            <label key={opt.value} className="flex items-center gap-2 text-sm cursor-pointer">
              <input
                type="radio"
                name="location_mode"
                checked={locationMode === opt.value}
                onChange={() => setLocationMode(opt.value)}
              />
              {opt.label}
            </label>
          ))}
        </div>
        {locationMode === 'location_plus_instructions' && (
          <textarea
            className="input min-h-[80px] w-full"
            placeholder="نص التعليمات (مثال: المدخل من البوابة الشرقية)"
            value={instructions}
            onChange={e => setInstructions(e.target.value)}
          />
        )}
      </section>

      <section className="card p-5 space-y-3">
        <h3 className="text-sm font-semibold text-slate-900">عند الوصول</h3>
        {ARRIVAL_MODES.map(opt => (
          <label key={opt.value} className="flex items-center gap-2 text-sm cursor-pointer">
            <input
              type="radio"
              name="arrival_mode"
              checked={arrivalMode === opt.value}
              onChange={() => setArrivalMode(opt.value)}
            />
            {opt.label}
          </label>
        ))}
        <button type="button" className="btn-primary text-sm" disabled={saving} onClick={saveModes}>
          {saving ? 'جاري الحفظ…' : 'حفظ قواعد الموقع والوصول'}
        </button>
      </section>

      <section className="card p-5 space-y-4">
        <h3 className="text-sm font-semibold text-slate-900">كلمات الوصول</h3>
        <div className="flex flex-wrap gap-2 items-end">
          <input
            className="input flex-1 min-w-[160px]"
            placeholder="عبارة جديدة (مثل: الحوش)"
            value={newPhrase}
            onChange={e => setNewPhrase(e.target.value)}
          />
          <select className="input" value={newTrigger} onChange={e => setNewTrigger(e.target.value)}>
            {Object.entries(TRIGGER_LABELS).map(([v, l]) => (
              <option key={v} value={v}>{l}</option>
            ))}
          </select>
          <button type="button" className="btn-secondary text-sm inline-flex items-center gap-1" onClick={addKeyword}>
            <Plus className="w-4 h-4" /> إضافة
          </button>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-slate-500 border-b">
                <th className="text-right py-2 font-medium">العبارة</th>
                <th className="text-right py-2 font-medium">النوع</th>
                <th className="text-right py-2 font-medium">نشط</th>
                <th className="py-2" />
              </tr>
            </thead>
            <tbody>
              {keywords.map(kw => (
                <tr key={kw.id} className="border-b border-slate-50">
                  <td className="py-2">{kw.phrase}</td>
                  <td className="py-2">{TRIGGER_LABELS[kw.trigger_type] || kw.trigger_type}</td>
                  <td className="py-2">
                    <input type="checkbox" checked={kw.is_active} onChange={() => toggleKeyword(kw)} />
                  </td>
                  <td className="py-2 text-left">
                    <button type="button" className="text-red-500 hover:text-red-700" onClick={() => deleteKeyword(kw)}>
                      <Trash2 className="w-4 h-4" />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="card p-5 space-y-3">
        <h3 className="text-sm font-semibold text-slate-900">معاينة الرد</h3>
        <div className="flex gap-2">
          <input
            className="input flex-1"
            value={previewMsg}
            onChange={e => setPreviewMsg(e.target.value)}
          />
          <button type="button" className="btn-secondary text-sm" onClick={() => runPreview()}>اختبر</button>
        </div>
        <div className="flex flex-wrap gap-2">
          {PRESET_MESSAGES.map(msg => (
            <button
              key={msg}
              type="button"
              className="text-xs px-2 py-1 rounded-full bg-slate-100 hover:bg-slate-200"
              onClick={() => runPreview(msg)}
            >
              {msg}
            </button>
          ))}
        </div>
        {preview && (
          <div className="text-sm bg-slate-50 border border-slate-100 rounded-lg p-3 space-y-1">
            <p>{preview.matched ? `تطابق: ${preview.matched_phrase} (${TRIGGER_LABELS[preview.trigger_type || ''] || preview.trigger_type})` : 'لا يوجد تطابق'}</p>
            {(preview.actions || []).map((a: TriggerPreviewAction, i: number) => (
              <p key={i} className="text-slate-600">
                → {a.type === 'maps_cta' && 'يرسل الموقع'}
                {a.type === 'reception_vcard' && `يرسل جهة الاستقبال: ${a.display_name || ''}`}
                {a.type === 'text' && `رسالة: ${a.body || ''}`}
                {a.type === 'escalation_advance' && 'ينتقل للمستوى التالي في التصعيد'}
              </p>
            ))}
          </div>
        )}
      </section>
    </div>
  )
}
